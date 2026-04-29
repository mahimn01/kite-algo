from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

from kite_algo.backtest.models import (
    BacktestConfig,
    BacktestResults,
    DailyResult,
    EquityPoint,
)
from kite_algo.backtest.validation.walk_forward import WalkForwardValidator

from tests.backtest.validation.conftest import _zero_metrics


def _fake_run(df: pd.DataFrame, strategy: object, cfg: BacktestConfig) -> BacktestResults:
    # Deterministic synthetic Sharpe based on slice mean of column "x".
    if "x" in df.columns and len(df) >= 2:
        x = df["x"].to_numpy(dtype=float)
        mu = float(x.mean())
        sd = float(x.std(ddof=1))
        sharpe = (mu / sd) if sd > 1e-12 else 0.0
    else:
        sharpe = 0.0

    daily_results: list[DailyResult] = []
    equity = cfg.initial_capital
    eq_points: list[EquityPoint] = [
        EquityPoint(ts=df.index[0], equity=equity, cash=equity, position_value=0.0, drawdown_pct=0.0)
    ]
    for ts, row in df.iterrows():
        r = float(row.get("x", 0.0)) * 0.001
        equity *= (1.0 + r)
        daily_results.append(
            DailyResult(d=ts.date(), daily_return=r, daily_pnl=r * equity, end_equity=equity, n_trades_closed=0)
        )
        eq_points.append(
            EquityPoint(ts=ts, equity=equity, cash=equity, position_value=0.0, drawdown_pct=0.0)
        )

    metrics = _zero_metrics(sharpe=sharpe, n_years=len(df) / 252.0)
    return BacktestResults(
        config=cfg,
        trades=[],
        equity_curve=eq_points,
        daily_results=daily_results,
        metrics=metrics,
    )


class _DummyStrategy:
    name = "dummy"

    def on_bar(self, bar, history):  # noqa: ANN001
        return []


def _strategy_factory() -> _DummyStrategy:
    return _DummyStrategy()


def test_walk_forward_partitions_correctly() -> None:
    n = 1000
    rng = np.random.default_rng(0)
    idx = pd.date_range("2022-01-03", periods=n, freq="D")
    df = pd.DataFrame({"x": rng.normal(0.0, 1.0, size=n)}, index=idx)

    wfv = WalkForwardValidator(
        train_window_days=200,
        test_window_days=60,
        step_days=60,
        mode="rolling",
        purge_days=2,
        embargo_days=0,
    )
    cfg = BacktestConfig(initial_capital=1_000_000.0, bars_per_year=252.0)
    res = wfv.run(df, _strategy_factory, cfg, _fake_run)

    assert len(res.windows) >= 5
    for w in res.windows:
        assert w.train_end > w.train_start
        assert w.test_start >= w.train_end + pd.Timedelta(days=2)  # purge
        assert w.test_end > w.test_start
    # Aggregate fields populated.
    assert isinstance(res.aggregate_test_sharpe, float)
    assert -1.0 <= res.is_oos_correlation <= 1.0
    assert res.aggregate_test_max_dd_pct <= 0.0


def test_walk_forward_anchored_keeps_train_start_fixed() -> None:
    n = 800
    idx = pd.date_range("2022-01-03", periods=n, freq="D")
    df = pd.DataFrame({"x": np.zeros(n)}, index=idx)
    wfv = WalkForwardValidator(
        train_window_days=180, test_window_days=60, step_days=60,
        mode="anchored", purge_days=1, embargo_days=0,
    )
    cfg = BacktestConfig()
    res = wfv.run(df, _strategy_factory, cfg, _fake_run)
    starts = {w.train_start for w in res.windows}
    assert len(starts) == 1, f"anchored mode must have one train_start, got {starts}"
