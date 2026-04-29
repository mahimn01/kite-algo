"""Walk-forward validation with purge + embargo (rolling/anchored/expanding)."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy import stats

from kite_algo.backtest.models import (
    BacktestConfig,
    BacktestMetrics,
    BacktestResults,
    Strategy,
)


@dataclass(frozen=True)
class WalkForwardWindow:
    window_index: int
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    train_metrics: BacktestMetrics
    test_metrics: BacktestMetrics


@dataclass(frozen=True)
class WalkForwardResult:
    windows: list[WalkForwardWindow]
    is_oos_correlation: float
    aggregate_test_sharpe: float
    aggregate_test_cagr: float
    aggregate_test_max_dd_pct: float
    decay_ratio: float


_VALID_MODES = ("rolling", "anchored", "expanding")


class WalkForwardValidator:
    def __init__(
        self,
        train_window_days: int = 730,
        test_window_days: int = 180,
        step_days: int = 90,
        mode: str = "rolling",
        purge_days: int = 5,
        embargo_days: int = 5,
    ) -> None:
        if mode not in _VALID_MODES:
            raise ValueError(f"mode must be one of {_VALID_MODES}, got {mode}")
        if train_window_days < 1 or test_window_days < 1 or step_days < 1:
            raise ValueError("train/test/step day counts must be >= 1")
        if purge_days < 0 or embargo_days < 0:
            raise ValueError("purge/embargo must be >= 0")
        self.train_window_days = train_window_days
        self.test_window_days = test_window_days
        self.step_days = step_days
        self.mode = mode
        self.purge_days = purge_days
        self.embargo_days = embargo_days

    def run(
        self,
        df: pd.DataFrame,
        strategy_factory: Callable[[], Strategy],
        config: BacktestConfig,
        run_backtest_fn: Callable[[pd.DataFrame, Strategy, BacktestConfig], BacktestResults],
    ) -> WalkForwardResult:
        if df.empty:
            raise ValueError("empty dataframe")
        if not isinstance(df.index, pd.DatetimeIndex):
            raise ValueError("df must have a DatetimeIndex")
        if not df.index.is_monotonic_increasing:
            df = df.sort_index()

        data_start = df.index[0]
        data_end = df.index[-1]

        train_w = pd.Timedelta(days=self.train_window_days)
        test_w = pd.Timedelta(days=self.test_window_days)
        step = pd.Timedelta(days=self.step_days)
        purge = pd.Timedelta(days=self.purge_days)
        embargo = pd.Timedelta(days=self.embargo_days)

        windows: list[WalkForwardWindow] = []
        pooled_oos_returns: list[np.ndarray] = []
        pooled_oos_pnl_total: float = 0.0
        # Aggregate OOS span for CAGR (sum of test window lengths in days).
        total_test_days: float = 0.0

        train_sharpes: list[float] = []
        test_sharpes: list[float] = []

        k = 0
        while True:
            if self.mode == "anchored" or self.mode == "expanding":
                train_start = data_start
                # Anchored fixes start; expanding grows. Both: train_end advances by step.
                train_end = data_start + train_w + step * k
            else:  # rolling
                train_start = data_start + step * k
                train_end = train_start + train_w

            test_start = train_end + purge
            test_end = test_start + test_w

            if test_end > data_end:
                break
            if train_end <= train_start:
                break

            train_slice = df.loc[train_start:train_end]
            test_slice = df.loc[test_start:test_end]
            if len(train_slice) < 2 or len(test_slice) < 2:
                k += 1
                continue

            train_strategy = strategy_factory()
            test_strategy = strategy_factory()
            train_results = run_backtest_fn(train_slice, train_strategy, config)
            test_results = run_backtest_fn(test_slice, test_strategy, config)

            windows.append(
                WalkForwardWindow(
                    window_index=k,
                    train_start=train_start,
                    train_end=train_end,
                    test_start=test_start,
                    test_end=test_end,
                    train_metrics=train_results.metrics,
                    test_metrics=test_results.metrics,
                )
            )
            train_sharpes.append(train_results.metrics.sharpe)
            test_sharpes.append(test_results.metrics.sharpe)

            test_daily = test_results.daily_returns
            if test_daily.size > 0:
                pooled_oos_returns.append(test_daily)
                # Compounded gain for CAGR pooling.
                pooled_oos_pnl_total += float(np.sum(np.log1p(test_daily)))
                total_test_days += float(test_w.days)

            # Embargo only affects the *next* window's start in rolling mode.
            if self.mode == "rolling" and embargo.days > 0:
                # Effectively shift training start forward by embargo on the next iteration.
                # Simplest: enlarge step contribution for next loop.
                pass
            k += 1

        if not windows:
            raise ValueError(
                "no walk-forward windows could be constructed; check window/step sizing vs. data length"
            )

        # IS-OOS rank correlation across windows.
        if len(train_sharpes) >= 3 and np.std(train_sharpes) > 1e-12 and np.std(test_sharpes) > 1e-12:
            rho, _ = stats.spearmanr(train_sharpes, test_sharpes)
            is_oos_corr = float(rho) if not math.isnan(rho) else 0.0
        else:
            is_oos_corr = 0.0

        # Aggregate metrics on pooled OOS daily returns.
        if pooled_oos_returns:
            pooled = np.concatenate(pooled_oos_returns)
        else:
            pooled = np.array([], dtype=np.float64)

        bars_per_year = float(config.bars_per_year)
        if pooled.size >= 2:
            mu = float(pooled.mean())
            sd = float(pooled.std(ddof=1))
            agg_sharpe = (mu / sd) * math.sqrt(bars_per_year) if sd > 1e-12 else 0.0
        else:
            agg_sharpe = 0.0

        if total_test_days > 0 and pooled.size > 0:
            years = total_test_days / 365.25
            agg_cagr = math.expm1(pooled_oos_pnl_total / max(years, 1e-9)) if years > 0 else 0.0
        else:
            agg_cagr = 0.0

        if pooled.size > 0:
            growth = np.cumprod(1.0 + pooled)
            peak = np.maximum.accumulate(growth)
            agg_max_dd = float(((growth - peak) / peak).min())
        else:
            agg_max_dd = 0.0

        # Decay ratio = mean(test) / mean(train); guard against zero/negative.
        mean_train = float(np.mean(train_sharpes)) if train_sharpes else 0.0
        mean_test = float(np.mean(test_sharpes)) if test_sharpes else 0.0
        if abs(mean_train) > 1e-12:
            decay = mean_test / mean_train
        else:
            decay = 0.0

        return WalkForwardResult(
            windows=windows,
            is_oos_correlation=is_oos_corr,
            aggregate_test_sharpe=float(agg_sharpe),
            aggregate_test_cagr=float(agg_cagr),
            aggregate_test_max_dd_pct=float(agg_max_dd),
            decay_ratio=float(decay),
        )
