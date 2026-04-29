"""Stationary bootstrap (Politis & Romano 1994) and bootstrap CIs.

For dependent time series, we resample blocks of random length L ~ Geom(1/p)
where p = 1/mean_block_length. The resulting series is stationary and
asymptotically captures the dependence structure.
"""

from __future__ import annotations

import math

import numpy as np


def _make_rng(seed: int | None) -> np.random.Generator:
    return np.random.default_rng(seed)


def stationary_bootstrap_indices(
    n: int,
    mean_block_length: float,
    n_resamples: int,
    rng: np.random.Generator,
) -> np.ndarray:
    """Generate (n_resamples, n) index matrix via stationary bootstrap.

    Politis-Romano: at each step, with prob p = 1/L start a new block at a
    uniformly random index; otherwise advance one step (mod n).
    """
    if mean_block_length <= 1.0:
        # iid bootstrap
        return rng.integers(0, n, size=(n_resamples, n), dtype=np.int64)
    p = 1.0 / mean_block_length
    # Pre-generate uniform draws and random starts.
    starts = rng.integers(0, n, size=(n_resamples, n), dtype=np.int64)
    new_block = rng.random((n_resamples, n)) < p
    new_block[:, 0] = True  # always start fresh

    out = np.empty((n_resamples, n), dtype=np.int64)
    out[:, 0] = starts[:, 0]
    # Vectorize across resamples; loop over time (n is moderate).
    for t in range(1, n):
        prev = out[:, t - 1]
        next_idx = (prev + 1) % n
        out[:, t] = np.where(new_block[:, t], starts[:, t], next_idx)
    return out


def stationary_bootstrap_returns(
    returns: np.ndarray,
    mean_block_length: float,
    n_resamples: int = 1000,
    seed: int | None = None,
) -> np.ndarray:
    """Returns shape (n_resamples, len(returns)) of stationary-bootstrapped series."""
    returns = np.asarray(returns, dtype=np.float64)
    n = returns.size
    if n < 2:
        raise ValueError(f"need >= 2 returns, got {n}")
    rng = _make_rng(seed)
    idx = stationary_bootstrap_indices(n, mean_block_length, n_resamples, rng)
    return returns[idx]


def _annualized_sharpe_vec(samples: np.ndarray, periods_per_year: float) -> np.ndarray:
    mu = samples.mean(axis=1)
    sd = samples.std(axis=1, ddof=1)
    sd = np.where(sd < 1e-12, np.nan, sd)
    sr = (mu / sd) * math.sqrt(periods_per_year)
    return np.nan_to_num(sr, nan=0.0)


def bootstrap_sharpe_ci(
    returns: np.ndarray,
    periods_per_year: float,
    n_resamples: int = 1000,
    ci: float = 0.95,
    mean_block_length: float = 10.0,
    seed: int | None = None,
) -> tuple[float, float, float]:
    """Returns (lower, upper, point_estimate) for annualized Sharpe."""
    if not 0.0 < ci < 1.0:
        raise ValueError(f"ci must be in (0,1), got {ci}")
    returns = np.asarray(returns, dtype=np.float64)
    samples = stationary_bootstrap_returns(
        returns, mean_block_length, n_resamples, seed
    )
    boot_sr = _annualized_sharpe_vec(samples, periods_per_year)
    alpha = (1.0 - ci) / 2.0
    lower = float(np.quantile(boot_sr, alpha))
    upper = float(np.quantile(boot_sr, 1.0 - alpha))
    point = float(_annualized_sharpe_vec(returns[None, :], periods_per_year)[0])
    return lower, upper, point


def _max_dd_pct(equity: np.ndarray) -> float:
    if equity.size == 0:
        return 0.0
    peak = np.maximum.accumulate(equity)
    dd = (equity - peak) / peak
    return float(dd.min())


def _max_dd_pct_vec(equity_paths: np.ndarray) -> np.ndarray:
    """Max drawdown (negative number) per row."""
    peaks = np.maximum.accumulate(equity_paths, axis=1)
    dd = (equity_paths - peaks) / peaks
    return dd.min(axis=1)


def bootstrap_max_dd_ci(
    equity: np.ndarray,
    n_resamples: int = 1000,
    ci: float = 0.95,
    mean_block_length: float = 10.0,
    seed: int | None = None,
) -> tuple[float, float, float]:
    """Bootstraps max drawdown by resampling *returns* and reconstructing equity.

    Returns (lower, upper, point_estimate). Drawdowns are negative numbers
    (e.g. -0.20 for 20% DD).
    """
    equity = np.asarray(equity, dtype=np.float64)
    if equity.size < 3:
        raise ValueError(f"equity series too short, got {equity.size}")
    if not np.all(equity > 0):
        raise ValueError("equity must be strictly positive")
    initial = float(equity[0])
    returns = np.diff(equity) / equity[:-1]

    samples = stationary_bootstrap_returns(
        returns, mean_block_length, n_resamples, seed
    )
    # Reconstruct equity: cumulative product of (1 + r), prefixed with 1.
    growth = np.cumprod(1.0 + samples, axis=1) * initial
    equity_paths = np.concatenate([np.full((samples.shape[0], 1), initial), growth], axis=1)
    boot_dd = _max_dd_pct_vec(equity_paths)
    alpha = (1.0 - ci) / 2.0
    lower = float(np.quantile(boot_dd, alpha))
    upper = float(np.quantile(boot_dd, 1.0 - alpha))
    point = _max_dd_pct(equity)
    return lower, upper, point
