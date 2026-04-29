"""Production backtest engine for kite-algo (Indian markets)."""

from __future__ import annotations

from kite_algo.backtest.costs import IndianCostModel
from kite_algo.backtest.data import DataLoader
from kite_algo.backtest.engine import BacktestEngine, run_backtest
from kite_algo.backtest.fills import FillModel
from kite_algo.backtest.indicators import ema, supertrend, wilder_atr
from kite_algo.backtest.metrics import compute_metrics
from kite_algo.backtest.models import (
    Bar,
    BacktestConfig,
    BacktestMetrics,
    BacktestResults,
    CostBreakdown,
    DailyResult,
    EquityPoint,
    RegimeTag,
    Signal,
    Strategy,
    Trade,
)
from kite_algo.backtest.regime import RegimeTagger


__all__ = [
    "Bar",
    "Trade",
    "EquityPoint",
    "DailyResult",
    "BacktestConfig",
    "BacktestMetrics",
    "BacktestResults",
    "CostBreakdown",
    "RegimeTag",
    "Signal",
    "Strategy",
    "BacktestEngine",
    "run_backtest",
    "IndianCostModel",
    "FillModel",
    "DataLoader",
    "RegimeTagger",
    "supertrend",
    "ema",
    "wilder_atr",
    "compute_metrics",
]
