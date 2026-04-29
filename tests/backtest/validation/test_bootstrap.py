from __future__ import annotations

import math

import numpy as np

from kite_algo.backtest.validation.bootstrap import (
    bootstrap_max_dd_ci,
    bootstrap_sharpe_ci,
    stationary_bootstrap_returns,
)


def test_stationary_bootstrap_shape_and_range() -> None:
    r = np.arange(100, dtype=np.float64)
    samples = stationary_bootstrap_returns(r, mean_block_length=10.0, n_resamples=50, seed=1)
    assert samples.shape == (50, 100)
    # Values must come from r (which is a permutation of [0..99]).
    assert set(np.unique(samples)).issubset(set(r.tolist()))


def test_bootstrap_sharpe_ci_brackets_sample_point() -> None:
    rng = np.random.default_rng(42)
    r = rng.normal(0.0005, 0.01, size=2000)
    lo, hi, point = bootstrap_sharpe_ci(
        r, periods_per_year=252.0, n_resamples=1000, ci=0.95,
        mean_block_length=10.0, seed=7,
    )
    assert lo < hi
    # The CI should bracket the sample point estimate (centering property).
    # Bootstrap CIs are about the sample statistic, not the population parameter.
    assert lo <= point <= hi, f"point {point:.3f} outside CI [{lo:.3f}, {hi:.3f}]"


def test_bootstrap_sharpe_population_coverage() -> None:
    """Across many independent samples, the CI should cover the population SR
    roughly (1-alpha) of the time. We use 50 trials and require coverage in
    a generous band to keep the test fast and stable."""
    rng = np.random.default_rng(123)
    mu, sigma = 0.0008, 0.01
    pop_annual_sr = (mu / sigma) * math.sqrt(252.0)
    n_trials = 50
    covered = 0
    for k in range(n_trials):
        r = rng.normal(mu, sigma, size=2000)
        lo, hi, _ = bootstrap_sharpe_ci(
            r, periods_per_year=252.0, n_resamples=400, ci=0.95,
            mean_block_length=1.0, seed=k,
        )
        if lo <= pop_annual_sr <= hi:
            covered += 1
    coverage = covered / n_trials
    assert coverage >= 0.80, f"empirical coverage {coverage:.2f} below 0.80 (pop SR {pop_annual_sr:.3f})"


def test_bootstrap_max_dd_ci_is_negative() -> None:
    rng = np.random.default_rng(11)
    r = rng.normal(0.0005, 0.01, size=2000)
    eq = 1_000_000.0 * np.cumprod(1.0 + r)
    eq = np.concatenate([[1_000_000.0], eq])
    lo, hi, point = bootstrap_max_dd_ci(
        eq, n_resamples=500, ci=0.95, mean_block_length=10.0, seed=3,
    )
    assert lo <= hi <= 0.0
    assert point <= 0.0
