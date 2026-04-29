from __future__ import annotations

import math

import numpy as np

from kite_algo.backtest.validation.deflated_sharpe import (
    annualized_dsr,
    annualized_psr,
    deflated_sharpe_ratio,
    probabilistic_sharpe_ratio,
)


def test_psr_zero_sharpe_is_half() -> None:
    # Construct a series with sample mean exactly zero so PSR(0) = Phi(0) = 0.5.
    rng = np.random.default_rng(0)
    r = rng.normal(0.0, 0.01, size=1000)
    r = r - r.mean()  # force sample mean = 0 -> sample SR = 0
    psr = probabilistic_sharpe_ratio(r, benchmark_sr=0.0)
    assert math.isclose(psr, 0.5, abs_tol=1e-6)


def test_psr_pure_noise_is_centered_across_seeds() -> None:
    # Across many seeds, mean PSR(0) for true-zero-SR returns should be ~0.5.
    psrs: list[float] = []
    for s in range(50):
        rng = np.random.default_rng(s)
        r = rng.normal(0.0, 0.01, size=500)
        psrs.append(probabilistic_sharpe_ratio(r, 0.0))
    mean_psr = float(np.mean(psrs))
    assert 0.40 <= mean_psr <= 0.60, f"mean PSR(0) under null was {mean_psr}"


def test_psr_strong_signal_near_one() -> None:
    rng = np.random.default_rng(1)
    # per-period SR ~ 0.2 with N=2000 -> z ~ 8.9, PSR -> 1.
    r = rng.normal(0.002, 0.01, size=2000)
    psr = probabilistic_sharpe_ratio(r, benchmark_sr=0.0)
    assert psr > 0.99, f"got {psr}"


def test_dsr_deflates_with_more_trials() -> None:
    rng = np.random.default_rng(2)
    r = rng.normal(0.001, 0.01, size=1000)
    dsr_one, sr0_one = deflated_sharpe_ratio(r, n_trials=1)
    dsr_many, sr0_many = deflated_sharpe_ratio(r, n_trials=100)
    assert dsr_many <= dsr_one
    assert sr0_many > sr0_one


def test_dsr_one_trial_equals_psr_zero() -> None:
    rng = np.random.default_rng(3)
    r = rng.normal(0.0005, 0.01, size=800)
    psr0 = probabilistic_sharpe_ratio(r, 0.0)
    dsr1, sr01 = deflated_sharpe_ratio(r, n_trials=1)
    assert sr01 == 0.0
    assert math.isclose(dsr1, psr0, rel_tol=1e-9, abs_tol=1e-9)


def test_annualized_psr_dsr_roundtrip() -> None:
    rng = np.random.default_rng(4)
    r = rng.normal(0.001, 0.01, size=1000)
    psr_a = annualized_psr(r, benchmark_annual_sr=0.0, periods_per_year=252.0)
    psr_p = probabilistic_sharpe_ratio(r, benchmark_sr=0.0)
    assert math.isclose(psr_a, psr_p, rel_tol=1e-9, abs_tol=1e-9)

    dsr_val, sr0_annual = annualized_dsr(r, n_trials=50, periods_per_year=252.0)
    assert 0.0 <= dsr_val <= 1.0
    assert sr0_annual > 0.0
