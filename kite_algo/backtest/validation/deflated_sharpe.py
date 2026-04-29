"""Probabilistic Sharpe Ratio (PSR) and Deflated Sharpe Ratio (DSR).

Reference: Bailey & López de Prado (2012, 2014).
"The Sharpe Ratio Efficient Frontier"
"The Deflated Sharpe Ratio: Correcting for Selection Bias..."
https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551

PSR(SR*) = Phi( (SR_obs - SR*) * sqrt(N - 1)
                / sqrt(1 - skew*SR_obs + ((kurt - 1)/4)*SR_obs**2) )

DSR uses a trial-corrected threshold:
SR0 = sqrt(V[SR]) * ((1 - gamma) * Phi^-1(1 - 1/N_trials)
                     + gamma * Phi^-1(1 - 1/(N_trials * e)))
where gamma is the Euler-Mascheroni constant.

All SR inputs/outputs in *per-period* units unless explicitly annualized.
"""

from __future__ import annotations

import math

import numpy as np
from scipy import stats

_EULER_MASCHERONI = 0.5772156649015329


def _per_period_sharpe(returns: np.ndarray) -> float:
    if returns.size < 2:
        return 0.0
    mu = float(np.mean(returns))
    sigma = float(np.std(returns, ddof=1))
    if sigma < 1e-12:
        return 0.0
    return mu / sigma


def _moments(returns: np.ndarray) -> tuple[float, float]:
    """Sample skewness and kurtosis (NOT excess; raw kurt where normal=3)."""
    if returns.size < 4:
        return 0.0, 3.0
    skew = float(stats.skew(returns, bias=False))
    # scipy.stats.kurtosis returns excess kurtosis by default; we want raw.
    kurt = float(stats.kurtosis(returns, fisher=False, bias=False))
    return skew, kurt


def probabilistic_sharpe_ratio(
    returns: np.ndarray,
    benchmark_sr: float = 0.0,
) -> float:
    """PSR(SR*): probability that the *true* per-period SR exceeds benchmark_sr.

    benchmark_sr must be in the same per-period units as the returns.
    """
    returns = np.asarray(returns, dtype=np.float64)
    n = returns.size
    if n < 2:
        return 0.5
    sr = _per_period_sharpe(returns)
    skew, kurt = _moments(returns)
    denom_sq = 1.0 - skew * sr + ((kurt - 1.0) / 4.0) * sr ** 2
    # Numerical guard: must stay positive for sqrt.
    denom_sq = max(denom_sq, 1e-12)
    z = (sr - benchmark_sr) * math.sqrt(max(n - 1, 1)) / math.sqrt(denom_sq)
    return float(stats.norm.cdf(z))


def _expected_max_sr(n_trials: int) -> float:
    """E[max of N iid standard normals], asymptotic approximation per BLdP."""
    if n_trials <= 1:
        return 0.0
    n = float(n_trials)
    a = stats.norm.ppf(1.0 - 1.0 / n)
    b = stats.norm.ppf(1.0 - 1.0 / (n * math.e))
    return (1.0 - _EULER_MASCHERONI) * a + _EULER_MASCHERONI * b


def deflated_sharpe_ratio(
    returns: np.ndarray,
    n_trials: int,
    sr_variance: float | None = None,
) -> tuple[float, float]:
    """Returns (DSR, SR0) where SR0 is the implied threshold per period.

    sr_variance: variance of *per-period* Sharpe across trials. If None, a
    conservative default of 1.0 is used (corresponding to iid normal trials
    with unit variance), which matches the standard E[max] derivation.
    """
    returns = np.asarray(returns, dtype=np.float64)
    if n_trials < 1:
        raise ValueError(f"n_trials must be >= 1, got {n_trials}")
    if sr_variance is None:
        sr_var = 1.0
    else:
        sr_var = float(sr_variance)
        if sr_var < 0.0:
            raise ValueError(f"sr_variance must be >= 0, got {sr_var}")
    sr0 = math.sqrt(max(sr_var, 0.0)) * _expected_max_sr(n_trials)
    dsr = probabilistic_sharpe_ratio(returns, benchmark_sr=sr0)
    return dsr, sr0


def annualized_psr(
    returns: np.ndarray,
    benchmark_annual_sr: float,
    periods_per_year: float,
) -> float:
    if periods_per_year <= 0:
        raise ValueError(f"periods_per_year must be positive, got {periods_per_year}")
    benchmark_per_period = benchmark_annual_sr / math.sqrt(periods_per_year)
    return probabilistic_sharpe_ratio(returns, benchmark_sr=benchmark_per_period)


def annualized_dsr(
    returns: np.ndarray,
    n_trials: int,
    periods_per_year: float,
    sr_variance_annual: float | None = None,
) -> tuple[float, float]:
    """Returns (DSR, annualized SR0).

    sr_variance_annual: variance of *annualized* Sharpe across trials.
    """
    if periods_per_year <= 0:
        raise ValueError(f"periods_per_year must be positive, got {periods_per_year}")
    sr_var_pp: float | None
    if sr_variance_annual is None:
        sr_var_pp = None
    else:
        sr_var_pp = sr_variance_annual / periods_per_year
    dsr, sr0_pp = deflated_sharpe_ratio(returns, n_trials, sr_variance=sr_var_pp)
    sr0_annual = sr0_pp * math.sqrt(periods_per_year)
    return dsr, sr0_annual
