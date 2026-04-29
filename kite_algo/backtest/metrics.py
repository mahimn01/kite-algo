"""compute_metrics — turn (trades, equity_curve, daily_results) into BacktestMetrics.

Edge cases handled: 0 trades, all wins, all losses, 0 std, single-day runs.
Returns are computed from the daily_results' daily_return field, so the
engine controls how returns are bucketed (per IST trading day).
"""

from __future__ import annotations

import math
from datetime import timedelta

import numpy as np
from scipy import stats

from kite_algo.backtest.models import (
    BacktestConfig,
    BacktestMetrics,
    DailyResult,
    EquityPoint,
    Trade,
)


def _safe_div(num: float, den: float, default: float = 0.0) -> float:
    if den == 0 or math.isnan(den):
        return default
    return num / den


def _max_consec(flags: list[bool]) -> int:
    best = 0
    cur = 0
    for f in flags:
        if f:
            cur += 1
            if cur > best:
                best = cur
        else:
            cur = 0
    return best


def compute_metrics(
    trades: list[Trade],
    equity_curve: list[EquityPoint],
    daily_results: list[DailyResult],
    config: BacktestConfig,
) -> BacktestMetrics:
    initial = config.initial_capital
    if equity_curve:
        final_equity = equity_curve[-1].equity
    else:
        final_equity = initial

    total_return = (final_equity / initial) - 1.0 if initial > 0 else 0.0

    if daily_results:
        first_d = daily_results[0].d
        last_d = daily_results[-1].d
        n_calendar_days = max(1, (last_d - first_d).days + 1)
        n_years = n_calendar_days / 365.25
    else:
        n_years = 0.0

    cagr = ((final_equity / initial) ** (1.0 / n_years) - 1.0) if (n_years > 0 and initial > 0 and final_equity > 0) else 0.0

    daily_returns = np.array([d.daily_return for d in daily_results], dtype=np.float64)
    daily_rf = config.risk_free_rate_annual / 252.0

    if daily_returns.size > 1:
        std = float(np.std(daily_returns, ddof=1))
        mean = float(np.mean(daily_returns))
        sharpe = _safe_div(mean - daily_rf, std) * math.sqrt(252.0)

        downside = daily_returns[daily_returns < 0]
        if downside.size > 1:
            d_std = float(np.std(downside, ddof=1))
            sortino = _safe_div(mean - daily_rf, d_std) * math.sqrt(252.0)
        else:
            sortino = 0.0

        skew = float(stats.skew(daily_returns, bias=False)) if daily_returns.size > 2 else 0.0
        kurt = float(stats.kurtosis(daily_returns, bias=False, fisher=True)) if daily_returns.size > 3 else 0.0

        var_95 = float(-np.quantile(daily_returns, 0.05))
        tail = daily_returns[daily_returns <= -var_95]
        cvar_95 = float(-tail.mean()) if tail.size > 0 else var_95

        annual_threshold = 0.01
        daily_threshold = annual_threshold / 252.0
        excess = daily_returns - daily_threshold
        upside = float(np.sum(np.clip(excess, 0, None)))
        downsum = float(-np.sum(np.clip(excess, None, 0)))
        omega_1pct = _safe_div(upside, downsum) if downsum > 0 else (float("inf") if upside > 0 else 0.0)
    else:
        sharpe = 0.0
        sortino = 0.0
        skew = 0.0
        kurt = 0.0
        var_95 = 0.0
        cvar_95 = 0.0
        omega_1pct = 0.0

    # Drawdowns from equity curve.
    equity_arr = np.array([p.equity for p in equity_curve], dtype=np.float64)
    if equity_arr.size > 0:
        running_max = np.maximum.accumulate(equity_arr)
        dd_pct = (equity_arr - running_max) / running_max
        max_dd_pct = float(dd_pct.min()) if dd_pct.size > 0 else 0.0
    else:
        dd_pct = np.array([], dtype=np.float64)
        max_dd_pct = 0.0

    # Drawdown duration (peak → recovery in calendar days). Find the longest gap
    # between consecutive new equity highs.
    max_dd_duration_days = 0
    if equity_curve:
        last_peak_ts = equity_curve[0].ts
        last_peak = equity_curve[0].equity
        for ep in equity_curve:
            if ep.equity >= last_peak:
                gap = (ep.ts - last_peak_ts).days
                if gap > max_dd_duration_days:
                    max_dd_duration_days = gap
                last_peak = ep.equity
                last_peak_ts = ep.ts
        # Final gap if we never recovered.
        if equity_curve:
            tail_gap = (equity_curve[-1].ts - last_peak_ts).days
            if tail_gap > max_dd_duration_days:
                max_dd_duration_days = tail_gap

    # Average drawdown depth across distinct drawdown episodes.
    avg_dd_pct = 0.0
    if dd_pct.size > 0:
        # Episode = contiguous run where dd < 0; take the trough of each.
        troughs: list[float] = []
        in_dd = False
        cur_min = 0.0
        for v in dd_pct:
            if v < 0:
                if not in_dd:
                    in_dd = True
                    cur_min = v
                elif v < cur_min:
                    cur_min = v
            else:
                if in_dd:
                    troughs.append(cur_min)
                    in_dd = False
                    cur_min = 0.0
        if in_dd:
            troughs.append(cur_min)
        avg_dd_pct = float(np.mean(troughs)) if troughs else 0.0

    ulcer_index = float(np.sqrt(np.mean(dd_pct ** 2))) if dd_pct.size > 0 else 0.0

    # Trade-level.
    n_trades = len(trades)
    wins = [t for t in trades if t.net_pnl > 0]
    losses = [t for t in trades if t.net_pnl < 0]
    n_wins = len(wins)
    n_losses = len(losses)
    win_rate = _safe_div(n_wins, n_trades)

    gross_win = sum(t.net_pnl for t in wins)
    gross_loss = sum(t.net_pnl for t in losses)
    profit_factor = _safe_div(gross_win, abs(gross_loss), default=float("inf") if gross_win > 0 else 0.0)

    expectancy_pct = float(np.mean([t.return_pct for t in trades])) if trades else 0.0
    avg_win_pct = float(np.mean([t.return_pct for t in wins])) if wins else 0.0
    avg_loss_pct = float(np.mean([t.return_pct for t in losses])) if losses else 0.0
    win_loss_ratio = _safe_div(avg_win_pct, abs(avg_loss_pct), default=float("inf") if avg_win_pct > 0 else 0.0)

    # R-multiple = net_pnl_per_unit / risk_per_unit, where risk_per_unit = entry_price - entry_st_band.
    r_mults: list[float] = []
    multiplier = float(config.lot_size) if config.cost_model == "futures" else 1.0
    for t in trades:
        risk_per_unit = t.entry_price - t.entry_st_band
        if risk_per_unit > 0 and t.qty > 0:
            net_per_unit = t.net_pnl / (t.qty * multiplier)
            r_mults.append(net_per_unit / risk_per_unit)
    avg_r_multiple = float(np.mean(r_mults)) if r_mults else 0.0

    avg_bars_held = float(np.mean([t.bars_held for t in trades])) if trades else 0.0

    avg_mae_pct = 0.0
    avg_mfe_pct = 0.0
    if trades:
        mae_pcts = []
        mfe_pcts = []
        for t in trades:
            entry_notional = t.entry_price * t.qty * multiplier
            if entry_notional > 0:
                mae_pcts.append(t.mae / entry_notional)
                mfe_pcts.append(t.mfe / entry_notional)
        avg_mae_pct = float(np.mean(mae_pcts)) if mae_pcts else 0.0
        avg_mfe_pct = float(np.mean(mfe_pcts)) if mfe_pcts else 0.0

    # Payoff capture = mean(net_pnl) / mean(mfe) over winning trades only.
    payoff_capture = 0.0
    if wins:
        mean_net_w = float(np.mean([t.net_pnl for t in wins]))
        mean_mfe_w = float(np.mean([t.mfe for t in wins]))
        payoff_capture = _safe_div(mean_net_w, mean_mfe_w)

    win_flags = [t.net_pnl > 0 for t in trades]
    loss_flags = [t.net_pnl < 0 for t in trades]
    max_consec_wins = _max_consec(win_flags)
    max_consec_losses = _max_consec(loss_flags)

    trades_per_year = _safe_div(n_trades, n_years)

    # Time-in-market — fraction of equity points where a position was held (position_value != 0).
    if equity_curve:
        in_market = sum(1 for p in equity_curve if abs(p.position_value) > 1e-9)
        time_in_market_pct = in_market / len(equity_curve)
    else:
        time_in_market_pct = 0.0

    # Daily distribution.
    if daily_returns.size > 0:
        daily_win_rate = float(np.mean(daily_returns > 0))
        best_day_pct = float(daily_returns.max())
        worst_day_pct = float(daily_returns.min())
    else:
        daily_win_rate = 0.0
        best_day_pct = 0.0
        worst_day_pct = 0.0

    # Costs.
    total_costs_inr = float(sum(t.costs for t in trades))
    cost_drag_pct = _safe_div(total_costs_inr, initial)
    cost_per_trade_inr = _safe_div(total_costs_inr, n_trades)

    calmar = _safe_div(cagr, abs(max_dd_pct), default=0.0)

    return BacktestMetrics(
        total_return=total_return,
        cagr=cagr,
        n_years=n_years,
        sharpe=sharpe,
        sortino=sortino,
        calmar=calmar,
        omega_1pct=omega_1pct,
        max_dd_pct=max_dd_pct,
        max_dd_duration_days=max_dd_duration_days,
        avg_dd_pct=avg_dd_pct,
        ulcer_index=ulcer_index,
        n_trades=n_trades,
        n_wins=n_wins,
        n_losses=n_losses,
        win_rate=win_rate,
        profit_factor=profit_factor,
        expectancy_pct=expectancy_pct,
        avg_win_pct=avg_win_pct,
        avg_loss_pct=avg_loss_pct,
        win_loss_ratio=win_loss_ratio,
        avg_r_multiple=avg_r_multiple,
        avg_bars_held=avg_bars_held,
        avg_mae_pct=avg_mae_pct,
        avg_mfe_pct=avg_mfe_pct,
        payoff_capture=payoff_capture,
        max_consec_wins=max_consec_wins,
        max_consec_losses=max_consec_losses,
        trades_per_year=trades_per_year,
        time_in_market_pct=time_in_market_pct,
        skew=skew,
        kurtosis=kurt,
        daily_win_rate=daily_win_rate,
        best_day_pct=best_day_pct,
        worst_day_pct=worst_day_pct,
        var_95=var_95,
        cvar_95=cvar_95,
        total_costs_inr=total_costs_inr,
        cost_drag_pct=cost_drag_pct,
        cost_per_trade_inr=cost_per_trade_inr,
    )
