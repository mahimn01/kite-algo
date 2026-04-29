"""Shared fixtures + helpers for validation tests.

We intentionally do NOT depend on a fully wired backtest engine. We build
minimal `BacktestResults` mocks via the same dataclasses defined in the
contract module.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from kite_algo.backtest.models import (
    BacktestConfig,
    BacktestMetrics,
    BacktestResults,
    DailyResult,
    EquityPoint,
    Trade,
)


def _zero_metrics(
    sharpe: float = 0.0,
    n_years: float = 1.0,
    cagr: float = 0.0,
    n_trades: int = 0,
) -> BacktestMetrics:
    return BacktestMetrics(
        total_return=0.0,
        cagr=cagr,
        n_years=n_years,
        sharpe=sharpe,
        sortino=0.0,
        calmar=0.0,
        omega_1pct=0.0,
        max_dd_pct=0.0,
        max_dd_duration_days=0,
        avg_dd_pct=0.0,
        ulcer_index=0.0,
        n_trades=n_trades,
        n_wins=0,
        n_losses=0,
        win_rate=0.0,
        profit_factor=0.0,
        expectancy_pct=0.0,
        avg_win_pct=0.0,
        avg_loss_pct=0.0,
        win_loss_ratio=0.0,
        avg_r_multiple=0.0,
        avg_bars_held=0.0,
        avg_mae_pct=0.0,
        avg_mfe_pct=0.0,
        payoff_capture=0.0,
        max_consec_wins=0,
        max_consec_losses=0,
        trades_per_year=0.0,
        time_in_market_pct=0.0,
        skew=0.0,
        kurtosis=3.0,
        daily_win_rate=0.0,
        best_day_pct=0.0,
        worst_day_pct=0.0,
        var_95=0.0,
        cvar_95=0.0,
        total_costs_inr=0.0,
        cost_drag_pct=0.0,
        cost_per_trade_inr=0.0,
    )


@pytest.fixture
def zero_metrics_factory():
    return _zero_metrics


def make_results_from_returns(
    daily_returns: np.ndarray,
    initial_capital: float = 1_000_000.0,
    start: date = date(2022, 1, 3),
    trade_returns: np.ndarray | None = None,
) -> BacktestResults:
    cfg = BacktestConfig(initial_capital=initial_capital)
    eq = initial_capital
    daily: list[DailyResult] = []
    equity_points: list[EquityPoint] = []
    peak = initial_capital
    d = start
    ts0 = pd.Timestamp(start)
    equity_points.append(EquityPoint(ts=ts0, equity=eq, cash=eq, position_value=0.0, drawdown_pct=0.0))
    for r in daily_returns:
        eq *= (1.0 + float(r))
        peak = max(peak, eq)
        dd = (eq - peak) / peak
        d = d + timedelta(days=1)
        ts = pd.Timestamp(d)
        daily.append(DailyResult(d=d, daily_return=float(r), daily_pnl=float(r) * eq, end_equity=eq, n_trades_closed=0))
        equity_points.append(EquityPoint(ts=ts, equity=eq, cash=eq, position_value=0.0, drawdown_pct=dd))

    trades: list[Trade] = []
    if trade_returns is not None:
        ts = pd.Timestamp(start)
        for tr in trade_returns:
            trades.append(
                Trade(
                    entry_ts=ts,
                    entry_price=100.0,
                    exit_ts=ts + pd.Timedelta(days=1),
                    exit_price=100.0 * (1.0 + float(tr)),
                    qty=1,
                    side="long",
                    gross_pnl=float(tr) * 100.0,
                    costs=0.0,
                    net_pnl=float(tr) * 100.0,
                    return_pct=float(tr),
                    bars_held=1,
                    mae=min(0.0, float(tr)),
                    mfe=max(0.0, float(tr)),
                    exit_reason="signal",
                    entry_atr=1.0,
                    entry_st_band=99.0,
                    regime_tag="",
                )
            )
            ts = ts + pd.Timedelta(days=1)

    metrics = _zero_metrics(n_trades=len(trades))
    return BacktestResults(
        config=cfg,
        trades=trades,
        equity_curve=equity_points,
        daily_results=daily,
        metrics=metrics,
    )
