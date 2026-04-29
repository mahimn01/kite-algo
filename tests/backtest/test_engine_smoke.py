"""End-to-end smoke test on a synthetic 100-bar series with a trivial strategy."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from kite_algo.backtest.engine import BacktestEngine, run_backtest
from kite_algo.backtest.models import Bar, BacktestConfig, Signal


class AlternatingStrategy:
    """Buys on bar N, sells on bar N+5; repeats. Lets us prove fills + costs work."""

    name = "alt_test"

    def __init__(self) -> None:
        self._holding = False
        self._entry_idx = -1
        self._counter = 0

    def on_bar(self, bar: Bar, history: pd.DataFrame) -> list[Signal]:
        self._counter += 1
        signals: list[Signal] = []
        if not self._holding and self._counter % 10 == 0:
            signals.append(Signal(action="buy", reason="alt_entry"))
            self._holding = True
            self._entry_idx = self._counter
        elif self._holding and self._counter - self._entry_idx >= 5:
            signals.append(Signal(action="sell", reason="alt_exit"))
            self._holding = False
        return signals


def _synthetic_bars(n: int = 100) -> pd.DataFrame:
    rng = np.random.default_rng(seed=42)
    idx = pd.date_range("2024-04-01 09:00", periods=n, freq="h", tz="UTC")
    # Drift upward with noise; ensures both wins and losses.
    closes = 22_000.0 + np.cumsum(rng.normal(loc=2.0, scale=20.0, size=n))
    opens = closes + rng.normal(scale=5.0, size=n)
    highs = np.maximum(opens, closes) + rng.uniform(0, 10, size=n)
    lows = np.minimum(opens, closes) - rng.uniform(0, 10, size=n)
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": [0] * n},
        index=idx,
    )


def test_engine_runs_end_to_end() -> None:
    df = _synthetic_bars(100)
    config = BacktestConfig(
        initial_capital=1_000_000.0,
        instrument="NIFTY_FUT",
        lot_size=75,
        sizing_mode="fixed_lots",
        fixed_lots=1,
        cost_model="futures",
        warmup_bars=20,
    )
    engine = BacktestEngine(config=config, strategy=AlternatingStrategy())
    results = engine.run(df)

    assert results.metrics.n_trades > 0
    assert len(results.equity_curve) == len(df)
    assert len(results.daily_results) >= 1
    # Costs must be positive and recorded on every trade.
    for t in results.trades:
        assert t.costs > 0
        assert t.qty == 75 * 1 if False else t.qty == 1  # 1 lot
    # Final equity should be finite.
    assert np.isfinite(results.equity_curve[-1].equity)


def test_engine_with_buy_and_hold_benchmark() -> None:
    df = _synthetic_bars(100)
    config = BacktestConfig(warmup_bars=20, sizing_mode="fixed_lots", fixed_lots=1)
    results = run_backtest(df, AlternatingStrategy(), config, benchmark="buy_and_hold")
    assert results.benchmark_equity_curve is not None
    assert len(results.benchmark_equity_curve) == len(df)


def test_engine_zero_trades_strategy() -> None:
    class HoldStrategy:
        name = "hold"

        def on_bar(self, bar: Bar, history: pd.DataFrame) -> list[Signal]:
            return []

    df = _synthetic_bars(100)
    config = BacktestConfig(warmup_bars=20)
    engine = BacktestEngine(config=config, strategy=HoldStrategy())
    results = engine.run(df)
    assert results.metrics.n_trades == 0
    # No-trade run: equity curve flat at initial capital.
    assert results.equity_curve[-1].equity == pytest.approx(config.initial_capital, abs=1e-6)


def test_engine_rejects_naive_index() -> None:
    df = _synthetic_bars(20)
    df.index = df.index.tz_localize(None)
    engine = BacktestEngine(config=BacktestConfig(warmup_bars=5), strategy=AlternatingStrategy())
    with pytest.raises(ValueError):
        engine.run(df)
