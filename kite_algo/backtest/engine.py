"""BacktestEngine — bar loop, position bookkeeping, fills, costs, equity curve.

Single-position long-only for v1. The strategy emits Signals on each bar
(post-warmup); buy/sell signals schedule a fill at bar_{t+1} open (default).
Mark-to-market equity is recorded on every bar close.

Sizing modes: fixed_lots (default), fixed_notional, vol_target. For futures,
qty stored on the position is in CONTRACTS (lots); notional uses lot_size.
For ETF, qty is in shares and lot_size=1.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta, timezone

import numpy as np
import pandas as pd

from kite_algo.backtest.costs import IndianCostModel
from kite_algo.backtest.fills import FillModel
from kite_algo.backtest.indicators import ema, supertrend, wilder_atr
from kite_algo.backtest.metrics import compute_metrics
from kite_algo.backtest.models import (
    Bar,
    BacktestConfig,
    BacktestResults,
    DailyResult,
    EquityPoint,
    Signal,
    Strategy,
    Trade,
)
from kite_algo.backtest.regime import RegimeTagger


log = logging.getLogger(__name__)
_IST = timezone(timedelta(hours=5, minutes=30))


@dataclass
class _OpenPosition:
    entry_ts: pd.Timestamp
    entry_price: float
    qty: int                       # contracts (futures) or shares (etf)
    entry_costs: float
    entry_atr: float
    entry_st_band: float
    entry_bar_idx: int
    best_unreal: float = 0.0       # MFE (>=0) in P&L units
    worst_unreal: float = 0.0      # MAE (<=0) in P&L units
    regime_tag: str = ""


@dataclass
class _PendingOrder:
    side: str          # "buy" | "sell"
    reason: str        # exit_reason (only meaningful for sells)
    qty: int           # 0 = use sizing; only for buys
    stop_loss_price: float | None = None


class BacktestEngine:
    def __init__(
        self,
        config: BacktestConfig,
        strategy: Strategy,
        regime_tagger: RegimeTagger | None = None,
    ) -> None:
        self.config = config
        self.strategy = strategy
        self.regime_tagger = regime_tagger
        self.cost_model = IndianCostModel(config.cost_model)
        self.fill_model = FillModel(config.slippage_bps_per_side, config.fill_at)
        self.multiplier = float(config.lot_size) if config.cost_model == "futures" else 1.0

        self.cash: float = config.initial_capital
        self.position: _OpenPosition | None = None
        self.high_water_mark: float = config.initial_capital
        self.peak_ts: pd.Timestamp | None = None
        self.max_dd_duration_bars: int = 0
        self._cur_dd_start_ts: pd.Timestamp | None = None

        self.trades: list[Trade] = []
        self.equity_curve: list[EquityPoint] = []
        self.daily_results: list[DailyResult] = []
        self._regime_pnl: dict[str, float] = defaultdict(float)

        self._daily_loss_blocked_dates: set[date] = set()

        self._daily_start_equity: dict[date, float] = {}
        self._daily_end_equity: dict[date, float] = {}
        self._daily_trade_count: dict[date, int] = defaultdict(int)

    # ---- internal helpers ------------------------------------------------

    def _row_to_bar(self, ts: pd.Timestamp, row: pd.Series) -> Bar:
        return Bar(
            ts=ts,
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=int(row["volume"]),
        )

    def _size_position(self, price: float, atr: float) -> int:
        mode = self.config.sizing_mode
        if mode == "fixed_lots":
            return self.config.fixed_lots
        if mode == "fixed_notional":
            denom = price * self.multiplier
            return max(0, int(self.config.fixed_notional_inr // denom))
        if mode == "vol_target":
            if atr <= 0 or math.isnan(atr):
                return 0
            target = self.config.target_daily_vol_pct * self.config.initial_capital
            return max(0, int(target // (atr * self.multiplier)))
        raise ValueError(f"unknown sizing_mode {mode!r}")

    def _mark_to_market(self, close: float) -> tuple[float, float]:
        if self.position is None:
            return self.cash, 0.0
        if self.config.cost_model == "futures":
            # Futures: cash retained at entry (only entry cost was deducted), so
            # the position contributes only its unrealized P&L to equity. Adding
            # full notional here would double-count and produce nonsensical equity.
            position_value = (close - self.position.entry_price) * self.position.qty * self.multiplier
        else:
            # ETF/cash: full notional was deducted at entry, so position value is mark-to-market notional.
            position_value = close * self.position.qty * self.multiplier
        return self.cash + position_value, position_value

    def _record_equity(self, ts: pd.Timestamp, close: float) -> None:
        equity, position_value = self._mark_to_market(close)
        if equity > self.high_water_mark:
            self.high_water_mark = equity
            self._cur_dd_start_ts = None
        else:
            if self._cur_dd_start_ts is None:
                self._cur_dd_start_ts = ts
        dd_pct = (
            (equity - self.high_water_mark) / self.high_water_mark
            if self.high_water_mark > 0
            else 0.0
        )
        self.equity_curve.append(
            EquityPoint(
                ts=ts,
                equity=equity,
                cash=self.cash,
                position_value=position_value,
                drawdown_pct=dd_pct,
            )
        )

    def _open_position(
        self,
        bar_t: Bar,
        bar_t1: Bar | None,
        atr: float,
        st_band: float,
        qty_override: int,
        bar_idx: int,
    ) -> None:
        # Fill happens at bar_t1's open by default. If we're at last bar with
        # no t1, skip the open — too late to enter.
        if bar_t1 is None and self.config.fill_at == "next_bar_open":
            return

        fill_price, fill_ts = self.fill_model.fill_buy(bar_t, bar_t1)

        qty = qty_override if qty_override > 0 else self._size_position(fill_price, atr)
        if qty <= 0:
            return

        cost_bd = self.cost_model.compute_cost(
            fill_price, qty, "buy", lot_size=int(self.multiplier) if self.config.cost_model == "futures" else 1
        )

        # Margin / cash check. For futures, ~10% SPAN+Exposure approximation.
        notional = fill_price * qty * self.multiplier
        margin_required = notional * 0.10 if self.config.cost_model == "futures" else notional
        if margin_required + cost_bd.total > self.cash:
            log.debug("skip entry — insufficient cash %.2f vs need %.2f", self.cash, margin_required)
            return

        if self.config.cost_model == "futures":
            # For futures we don't lock the full notional — only deduct entry costs.
            self.cash -= cost_bd.total
        else:
            self.cash -= notional + cost_bd.total

        regime = ""
        if self.regime_tagger is not None:
            regime = self.regime_tagger.tag_for(fill_ts).composite_key

        self.position = _OpenPosition(
            entry_ts=fill_ts,
            entry_price=fill_price,
            qty=qty,
            entry_costs=cost_bd.total,
            entry_atr=atr,
            entry_st_band=st_band,
            entry_bar_idx=bar_idx,
            regime_tag=regime,
        )

    def _close_position(
        self,
        bar_t: Bar,
        bar_t1: Bar | None,
        reason: str,
        bar_idx: int,
        force_close_at: float | None = None,
    ) -> None:
        if self.position is None:
            return

        if force_close_at is not None:
            fill_price = force_close_at
            fill_ts = bar_t.ts
        else:
            fill_price, fill_ts = self.fill_model.fill_sell(bar_t, bar_t1)

        qty = self.position.qty
        cost_bd = self.cost_model.compute_cost(
            fill_price, qty, "sell", lot_size=int(self.multiplier) if self.config.cost_model == "futures" else 1
        )

        gross_pnl = (fill_price - self.position.entry_price) * qty * self.multiplier
        total_costs = self.position.entry_costs + cost_bd.total
        net_pnl = gross_pnl - cost_bd.total  # entry costs already taken from cash
        # For return_pct we use full notional and full round-trip costs.
        entry_notional = self.position.entry_price * qty * self.multiplier
        return_pct = (gross_pnl - total_costs) / entry_notional if entry_notional > 0 else 0.0

        if self.config.cost_model == "futures":
            self.cash += gross_pnl - cost_bd.total
        else:
            self.cash += fill_price * qty * self.multiplier - cost_bd.total

        bars_held = bar_idx - self.position.entry_bar_idx
        trade_net_for_records = gross_pnl - total_costs

        trade = Trade(
            entry_ts=self.position.entry_ts,
            entry_price=self.position.entry_price,
            exit_ts=fill_ts,
            exit_price=fill_price,
            qty=qty,
            side="long",
            gross_pnl=gross_pnl,
            costs=total_costs,
            net_pnl=trade_net_for_records,
            return_pct=return_pct,
            bars_held=bars_held,
            mae=self.position.worst_unreal,
            mfe=self.position.best_unreal,
            exit_reason=reason,
            entry_atr=self.position.entry_atr,
            entry_st_band=self.position.entry_st_band,
            regime_tag=self.position.regime_tag,
        )
        self.trades.append(trade)
        self._regime_pnl[self.position.regime_tag] += trade_net_for_records
        d = fill_ts.tz_convert(_IST).date()
        self._daily_trade_count[d] += 1
        self.position = None

    def _update_excursions(self, bar_t: Bar) -> None:
        if self.position is None:
            return
        unreal_high = (bar_t.high - self.position.entry_price) * self.position.qty * self.multiplier
        unreal_low = (bar_t.low - self.position.entry_price) * self.position.qty * self.multiplier
        if unreal_high > self.position.best_unreal:
            self.position.best_unreal = unreal_high
        if unreal_low < self.position.worst_unreal:
            self.position.worst_unreal = unreal_low

    # ---- main loop -------------------------------------------------------

    def run(self, df: pd.DataFrame) -> BacktestResults:
        if df.empty:
            raise ValueError("empty data")
        if not isinstance(df.index, pd.DatetimeIndex):
            raise ValueError("df must have a DatetimeIndex")
        if df.index.tz is None:
            raise ValueError("df index must be tz-aware")

        n = len(df)
        warmup = min(self.config.warmup_bars, n)

        opens = df["open"].to_numpy(dtype=np.float64)
        highs = df["high"].to_numpy(dtype=np.float64)
        lows = df["low"].to_numpy(dtype=np.float64)
        closes = df["close"].to_numpy(dtype=np.float64)
        vols = df["volume"].to_numpy(dtype=np.int64)
        ts_index = df.index

        # Pre-compute ATR(14) and Supertrend(10,3) for use by sizing/exit logic.
        atr14 = wilder_atr(df, 14).to_numpy(dtype=np.float64)
        st = supertrend(df, period=10, mult=3.0)
        st_band_arr = st["st_value"].to_numpy(dtype=np.float64)

        pending: _PendingOrder | None = None
        prev_day: date | None = None

        for i in range(n):
            ts = ts_index[i]
            bar_t = Bar(ts=ts, open=opens[i], high=highs[i], low=lows[i], close=closes[i], volume=int(vols[i]))
            bar_t1: Bar | None = None
            if i + 1 < n:
                ts1 = ts_index[i + 1]
                bar_t1 = Bar(
                    ts=ts1,
                    open=opens[i + 1],
                    high=highs[i + 1],
                    low=lows[i + 1],
                    close=closes[i + 1],
                    volume=int(vols[i + 1]),
                )

            # Apply pending order at this bar (scheduled at i-1 for next-bar-open fills).
            if pending is not None:
                if pending.side == "buy" and self.position is None:
                    # Pending buy was scheduled on bar i-1 to fill at bar i open.
                    prev_bar = Bar(
                        ts=ts_index[i - 1],
                        open=opens[i - 1],
                        high=highs[i - 1],
                        low=lows[i - 1],
                        close=closes[i - 1],
                        volume=int(vols[i - 1]),
                    )
                    atr_at_decision = atr14[i - 1] if i - 1 >= 0 else float("nan")
                    st_band_at_decision = st_band_arr[i - 1] if i - 1 >= 0 else float("nan")
                    self._open_position(
                        bar_t=prev_bar,
                        bar_t1=bar_t,
                        atr=atr_at_decision,
                        st_band=st_band_at_decision,
                        qty_override=pending.qty,
                        bar_idx=i,
                    )
                elif pending.side == "sell" and self.position is not None:
                    prev_bar = Bar(
                        ts=ts_index[i - 1],
                        open=opens[i - 1],
                        high=highs[i - 1],
                        low=lows[i - 1],
                        close=closes[i - 1],
                        volume=int(vols[i - 1]),
                    )
                    self._close_position(
                        bar_t=prev_bar,
                        bar_t1=bar_t,
                        reason=pending.reason,
                        bar_idx=i,
                    )
                pending = None

            # Update excursions based on this bar's range while position is open.
            self._update_excursions(bar_t)

            # Daily bookkeeping (track equity at each day boundary).
            day = ts.tz_convert(_IST).date()
            if day not in self._daily_start_equity:
                eq_now, _ = self._mark_to_market(bar_t.close)
                self._daily_start_equity[day] = eq_now
                # Daily-loss circuit breaker reset happens implicitly via dict.

            # Strategy decision (only after warmup).
            if i >= warmup:
                # Daily-loss check.
                if day in self._daily_loss_blocked_dates:
                    signals: list[Signal] = []
                else:
                    history = df.iloc[: i + 1]
                    signals = self.strategy.on_bar(bar_t, history)

                for sig in signals:
                    if sig.action == "buy" and self.position is None and pending is None:
                        pending = _PendingOrder(
                            side="buy",
                            reason="entry",
                            qty=sig.qty,
                            stop_loss_price=sig.stop_loss_price,
                        )
                    elif sig.action == "sell" and self.position is not None and pending is None:
                        pending = _PendingOrder(side="sell", reason=sig.reason or "signal_exit", qty=0)

            # Mark-to-market & record equity.
            self._record_equity(ts, bar_t.close)

            # Daily-loss breaker: if intraday DD from start-of-day exceeds 5%, block new entries today.
            if day in self._daily_start_equity:
                eq_now = self.equity_curve[-1].equity
                day_dd = (eq_now - self._daily_start_equity[day]) / self._daily_start_equity[day]
                if day_dd <= -0.05:
                    self._daily_loss_blocked_dates.add(day)

            # End-of-day equity bookkeeping.
            if prev_day is not None and day != prev_day:
                # Day rolled — close out the previous day's record using last bar of that day.
                self._daily_end_equity[prev_day] = self.equity_curve[-2].equity
            prev_day = day

        # Force-close any open position at last close, then refresh the final
        # equity-curve point so the post-close cash is reflected.
        if self.position is not None:
            last_bar = Bar(
                ts=ts_index[-1],
                open=opens[-1],
                high=highs[-1],
                low=lows[-1],
                close=closes[-1],
                volume=int(vols[-1]),
            )
            self._close_position(
                bar_t=last_bar,
                bar_t1=None,
                reason="warmup_end",
                bar_idx=n - 1,
                force_close_at=last_bar.close * (1.0 - self.fill_model.slippage),
            )
            # Refresh last equity point to reflect realized P&L from the force-close.
            if self.equity_curve:
                last_pt = self.equity_curve[-1]
                eq_now, pos_val = self._mark_to_market(last_bar.close)
                dd_pct = (
                    (eq_now - self.high_water_mark) / self.high_water_mark
                    if self.high_water_mark > 0
                    else 0.0
                )
                self.equity_curve[-1] = EquityPoint(
                    ts=last_pt.ts, equity=eq_now, cash=self.cash,
                    position_value=pos_val, drawdown_pct=dd_pct,
                )

        # Build daily_results: group equity_curve by IST date, take last point of each day,
        # chain returns through previous day's end (so overnight gaps are captured exactly once).
        eod_equity: dict[date, float] = {}
        eod_ts: dict[date, pd.Timestamp] = {}
        for pt in self.equity_curve:
            d = pt.ts.tz_convert(_IST).date()
            eod_equity[d] = pt.equity      # overwrite — last write per day wins
            eod_ts[d] = pt.ts
        sorted_days = sorted(eod_equity.keys())
        prev_end = self.config.initial_capital
        for d in sorted_days:
            end_eq = eod_equity[d]
            ret = (end_eq - prev_end) / prev_end if prev_end > 0 else 0.0
            self.daily_results.append(
                DailyResult(
                    d=d,
                    daily_return=ret,
                    daily_pnl=end_eq - prev_end,
                    end_equity=end_eq,
                    n_trades_closed=self._daily_trade_count.get(d, 0),
                )
            )
            prev_end = end_eq

        metrics = compute_metrics(
            trades=self.trades,
            equity_curve=self.equity_curve,
            daily_results=self.daily_results,
            config=self.config,
        )

        return BacktestResults(
            config=self.config,
            trades=self.trades,
            equity_curve=self.equity_curve,
            daily_results=self.daily_results,
            metrics=metrics,
            regime_pnl=dict(self._regime_pnl),
        )


def _buy_and_hold_curve(df: pd.DataFrame, config: BacktestConfig) -> list[EquityPoint]:
    """Buy at first close, hold to end. Costs applied once entry, once exit. No slippage detail."""
    if df.empty:
        return []
    cost_model = IndianCostModel(config.cost_model)
    multiplier = float(config.lot_size) if config.cost_model == "futures" else 1.0
    first_close = float(df["close"].iloc[0])
    qty = max(1, int(config.initial_capital // (first_close * multiplier)))
    entry_cost = cost_model.compute_cost(
        first_close, qty, "buy", lot_size=int(multiplier) if config.cost_model == "futures" else 1
    ).total

    cash = config.initial_capital - entry_cost
    if config.cost_model != "futures":
        cash -= first_close * qty * multiplier

    closes = df["close"].to_numpy(dtype=np.float64)
    out: list[EquityPoint] = []
    hwm = config.initial_capital
    for i, ts in enumerate(df.index):
        c = closes[i]
        if config.cost_model == "futures":
            equity = cash + (c - first_close) * qty * multiplier + first_close * qty * multiplier - first_close * qty * multiplier
            equity = cash + (c - first_close) * qty * multiplier
        else:
            equity = cash + c * qty * multiplier
        if equity > hwm:
            hwm = equity
        dd = (equity - hwm) / hwm if hwm > 0 else 0.0
        out.append(EquityPoint(ts=ts, equity=equity, cash=cash, position_value=equity - cash, drawdown_pct=dd))
    return out


def run_backtest(
    df: pd.DataFrame,
    strategy: Strategy,
    config: BacktestConfig,
    regime_tagger: RegimeTagger | None = None,
    benchmark: str | None = "buy_and_hold",
) -> BacktestResults:
    engine = BacktestEngine(config=config, strategy=strategy, regime_tagger=regime_tagger)
    results = engine.run(df)

    bench_curve: list[EquityPoint] | None = None
    bench_metrics = None
    if benchmark == "buy_and_hold":
        bench_curve = _buy_and_hold_curve(df, config)
    elif benchmark == "sma200_cross":
        # Long when close > SMA200, flat otherwise. Daily-equivalent SMA on the bar series.
        sma = df["close"].rolling(200, min_periods=200).mean().to_numpy(dtype=np.float64)
        closes = df["close"].to_numpy(dtype=np.float64)
        multiplier = float(config.lot_size) if config.cost_model == "futures" else 1.0
        cost_model = IndianCostModel(config.cost_model)
        cash = config.initial_capital
        qty = 0
        entry_price = 0.0
        bench_curve = []
        hwm = config.initial_capital
        for i, ts in enumerate(df.index):
            c = closes[i]
            target_long = (not math.isnan(sma[i])) and c > sma[i]
            if target_long and qty == 0:
                qty = max(1, int(cash // (c * multiplier)))
                entry_price = c
                cash -= cost_model.compute_cost(
                    c, qty, "buy", lot_size=int(multiplier) if config.cost_model == "futures" else 1
                ).total
                if config.cost_model != "futures":
                    cash -= c * qty * multiplier
            elif (not target_long) and qty > 0:
                cash += cost_model.compute_cost(
                    c, qty, "sell", lot_size=int(multiplier) if config.cost_model == "futures" else 1
                ).total * -1
                if config.cost_model == "futures":
                    cash += (c - entry_price) * qty * multiplier
                else:
                    cash += c * qty * multiplier
                qty = 0
                entry_price = 0.0

            if qty > 0:
                if config.cost_model == "futures":
                    equity = cash + (c - entry_price) * qty * multiplier
                else:
                    equity = cash + c * qty * multiplier
            else:
                equity = cash
            if equity > hwm:
                hwm = equity
            dd = (equity - hwm) / hwm if hwm > 0 else 0.0
            bench_curve.append(EquityPoint(ts=ts, equity=equity, cash=cash, position_value=equity - cash, drawdown_pct=dd))
    elif benchmark is None:
        bench_curve = None
    else:
        raise ValueError(f"unknown benchmark {benchmark!r}")

    if bench_curve is not None and bench_curve:
        # Build daily-results from bench equity curve (chained EOD).
        bench_daily: list[DailyResult] = []
        bench_eod: dict[date, float] = {}
        for pt in bench_curve:
            d = pt.ts.tz_convert(_IST).date()
            bench_eod[d] = pt.equity
        prev_end = config.initial_capital
        for d in sorted(bench_eod.keys()):
            end_eq = bench_eod[d]
            ret = (end_eq - prev_end) / prev_end if prev_end > 0 else 0.0
            bench_daily.append(
                DailyResult(d=d, daily_return=ret, daily_pnl=end_eq - prev_end, end_equity=end_eq, n_trades_closed=0)
            )
            prev_end = end_eq
        bench_metrics = compute_metrics(trades=[], equity_curve=bench_curve, daily_results=bench_daily, config=config)

    return BacktestResults(
        config=results.config,
        trades=results.trades,
        equity_curve=results.equity_curve,
        daily_results=results.daily_results,
        metrics=results.metrics,
        benchmark_equity_curve=bench_curve,
        benchmark_metrics=bench_metrics,
        regime_pnl=results.regime_pnl,
    )
