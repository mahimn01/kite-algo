from __future__ import annotations

import numpy as np

from kite_algo.backtest.validation.pbo import PBOCalculator


def test_pbo_pure_noise_near_half() -> None:
    # The CSCV estimator has substantial per-matrix variance even with hundreds
    # of combinations because the combinations share groups. Average over many
    # independent matrices to estimate the population PBO under the null.
    rng = np.random.default_rng(0)
    pbo_values: list[float] = []
    for _ in range(20):
        M = rng.normal(0.0, 0.01, size=(40, 1200))
        res = PBOCalculator(metric="sharpe", n_groups=12).calculate(M)
        pbo_values.append(res.pbo)
    mean_pbo = float(np.mean(pbo_values))
    assert 0.40 <= mean_pbo <= 0.60, (
        f"PBO under null should average ~0.5, got {mean_pbo} (samples {pbo_values})"
    )


def test_pbo_with_one_genuine_edge_is_low() -> None:
    rng = np.random.default_rng(13)
    n_variants, n_periods = 60, 1500
    M = rng.normal(0.0, 0.01, size=(n_variants, n_periods))
    # One variant has a clear, persistent positive drift.
    M[0] += 0.003
    res = PBOCalculator(metric="sharpe", n_groups=12).calculate(M)
    assert res.pbo < 0.30, f"PBO with real edge should be low, got {res.pbo}"


def test_pbo_invalid_inputs() -> None:
    rng = np.random.default_rng(1)
    M = rng.normal(0.0, 0.01, size=(5, 1000))
    try:
        PBOCalculator(n_groups=7)
        raised = False
    except ValueError:
        raised = True
    assert raised

    try:
        PBOCalculator(n_groups=8).calculate(M[0])  # 1D
        raised = False
    except ValueError:
        raised = True
    assert raised
