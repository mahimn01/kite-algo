"""Monte Carlo equity-curve simulation by trade-return resampling.

iid bootstrap on the trade-return distribution. Each path compounds
n_trades_per_path resampled returns starting from initial_capital.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class MonteCarloResult:
    n_simulations: int
    final_equity_quantiles: dict[float, float]
    max_dd_quantiles: dict[float, float]
    sharpe_quantiles: dict[float, float]
    ruin_probability: float
    sample_paths: np.ndarray  # shape (k, T+1) starting at initial_capital


def _quantiles(arr: np.ndarray, qs: tuple[float, ...]) -> dict[float, float]:
    out: dict[float, float] = {}
    for q in qs:
        out[q] = float(np.quantile(arr, q))
    return out


def monte_carlo_trade_paths(
    trade_returns: np.ndarray,
    initial_capital: float,
    n_simulations: int = 10_000,
    n_trades_per_path: int | None = None,
    ruin_floor_pct: float = 0.5,
    seed: int | None = None,
    sample_paths_to_keep: int = 20,
) -> MonteCarloResult:
    trade_returns = np.asarray(trade_returns, dtype=np.float64)
    if trade_returns.size < 2:
        raise ValueError(f"need >= 2 trade returns, got {trade_returns.size}")
    if initial_capital <= 0.0:
        raise ValueError(f"initial_capital must be positive, got {initial_capital}")
    if not 0.0 < ruin_floor_pct < 1.0:
        raise ValueError(f"ruin_floor_pct must be in (0,1), got {ruin_floor_pct}")
    n_per_path = trade_returns.size if n_trades_per_path is None else int(n_trades_per_path)
    if n_per_path < 1:
        raise ValueError(f"n_trades_per_path must be >= 1, got {n_per_path}")

    rng = np.random.default_rng(seed)
    # iid resample: shape (n_simulations, n_per_path)
    idx = rng.integers(0, trade_returns.size, size=(n_simulations, n_per_path), dtype=np.int64)
    sampled = trade_returns[idx]

    # Compound. Floor at 0 to avoid negative equity from extreme draws.
    # Each step: equity *= (1 + r). Track running min for ruin detection.
    growth = np.cumprod(1.0 + sampled, axis=1)
    equity_paths = initial_capital * growth
    # Prepend initial column so length is n_per_path + 1.
    full_paths = np.concatenate(
        [np.full((n_simulations, 1), initial_capital, dtype=np.float64), equity_paths],
        axis=1,
    )

    final_equity = full_paths[:, -1]
    # Max drawdown per path (negative number).
    peaks = np.maximum.accumulate(full_paths, axis=1)
    dd = (full_paths - peaks) / peaks
    max_dd = dd.min(axis=1)

    # Per-path Sharpe (using sampled trade returns, no annualization — these
    # are trade-level returns, scale doesn't matter for relative ranking).
    mu = sampled.mean(axis=1)
    sd = sampled.std(axis=1, ddof=1)
    sd_safe = np.where(sd < 1e-12, np.nan, sd)
    path_sharpe = np.nan_to_num(mu / sd_safe, nan=0.0)

    ruin_floor = initial_capital * ruin_floor_pct
    min_equity = full_paths.min(axis=1)
    ruin_prob = float(np.mean(min_equity <= ruin_floor))

    qs = (0.05, 0.25, 0.5, 0.75, 0.95)
    final_q = _quantiles(final_equity, qs)
    dd_q = _quantiles(max_dd, qs)
    sr_q = _quantiles(path_sharpe, qs)

    keep = max(0, min(sample_paths_to_keep, n_simulations))
    if keep > 0:
        # Deterministic-ish sample using the rng so seed reproduces.
        sample_idx = rng.choice(n_simulations, size=keep, replace=False)
        sample_paths = full_paths[sample_idx].copy()
    else:
        sample_paths = np.empty((0, n_per_path + 1), dtype=np.float64)

    return MonteCarloResult(
        n_simulations=n_simulations,
        final_equity_quantiles=final_q,
        max_dd_quantiles=dd_q,
        sharpe_quantiles=sr_q,
        ruin_probability=ruin_prob,
        sample_paths=sample_paths,
    )
