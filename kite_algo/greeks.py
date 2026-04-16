"""Black-Scholes option pricing and Greeks.

Kite Connect does not provide Greeks — compute them locally from spot price,
strike, time-to-expiry, risk-free rate, and either IV (for pricing) or market
price (for IV-solve).

Default risk-free rate: 6.5% (approximate RBI repo rate). Override via
--risk-free-rate in CLI commands or KITE_RISK_FREE_RATE env var.
"""

from __future__ import annotations

import math
import os
from typing import Literal

_SQRT_2 = math.sqrt(2.0)
_SQRT_2PI = math.sqrt(2.0 * math.pi)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / _SQRT_2))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / _SQRT_2PI


# ---------------------------------------------------------------------------
# Core Black-Scholes
# ---------------------------------------------------------------------------

def _d1d2(S: float, K: float, T: float, r: float, sigma: float) -> tuple[float, float]:
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0, 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return d1, d2


def bs_price(
    S: float, K: float, T: float, r: float, sigma: float,
    right: Literal["CE", "PE", "C", "P"],
) -> float:
    """Black-Scholes European option price."""
    if T <= 0:
        is_call = right in ("CE", "C")
        return max(0.0, S - K) if is_call else max(0.0, K - S)
    d1, d2 = _d1d2(S, K, T, r, sigma)
    is_call = right in ("CE", "C")
    if is_call:
        return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def bs_delta(
    S: float, K: float, T: float, r: float, sigma: float,
    right: Literal["CE", "PE", "C", "P"],
) -> float:
    if T <= 0 or sigma <= 0:
        is_call = right in ("CE", "C")
        return (1.0 if S > K else 0.0) if is_call else (-1.0 if S < K else 0.0)
    d1, _ = _d1d2(S, K, T, r, sigma)
    if right in ("CE", "C"):
        return _norm_cdf(d1)
    return _norm_cdf(d1) - 1.0


def bs_gamma(S: float, K: float, T: float, r: float, sigma: float) -> float:
    if T <= 0 or sigma <= 0 or S <= 0:
        return 0.0
    d1, _ = _d1d2(S, K, T, r, sigma)
    return _norm_pdf(d1) / (S * sigma * math.sqrt(T))


def bs_theta(
    S: float, K: float, T: float, r: float, sigma: float,
    right: Literal["CE", "PE", "C", "P"],
) -> float:
    """Theta per calendar day (divide annual by 365)."""
    if T <= 0 or sigma <= 0:
        return 0.0
    d1, d2 = _d1d2(S, K, T, r, sigma)
    common = -(S * _norm_pdf(d1) * sigma) / (2.0 * math.sqrt(T))
    if right in ("CE", "C"):
        return (common - r * K * math.exp(-r * T) * _norm_cdf(d2)) / 365.0
    return (common + r * K * math.exp(-r * T) * _norm_cdf(-d2)) / 365.0


def bs_vega(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Vega per 1% move in IV (i.e. multiply raw vega by 0.01)."""
    if T <= 0 or sigma <= 0:
        return 0.0
    d1, _ = _d1d2(S, K, T, r, sigma)
    return S * _norm_pdf(d1) * math.sqrt(T) * 0.01


def bs_rho(
    S: float, K: float, T: float, r: float, sigma: float,
    right: Literal["CE", "PE", "C", "P"],
) -> float:
    """Rho per 1% move in interest rate."""
    if T <= 0 or sigma <= 0:
        return 0.0
    _, d2 = _d1d2(S, K, T, r, sigma)
    if right in ("CE", "C"):
        return K * T * math.exp(-r * T) * _norm_cdf(d2) * 0.01
    return -K * T * math.exp(-r * T) * _norm_cdf(-d2) * 0.01


def greeks(
    S: float, K: float, T: float, r: float, sigma: float,
    right: Literal["CE", "PE", "C", "P"],
) -> dict[str, float]:
    """All Greeks in one call."""
    return {
        "price": bs_price(S, K, T, r, sigma, right),
        "delta": bs_delta(S, K, T, r, sigma, right),
        "gamma": bs_gamma(S, K, T, r, sigma),
        "theta": bs_theta(S, K, T, r, sigma, right),
        "vega": bs_vega(S, K, T, r, sigma),
        "rho": bs_rho(S, K, T, r, sigma, right),
        "iv": sigma,
    }


# ---------------------------------------------------------------------------
# Implied Volatility solver (Newton-Raphson)
# ---------------------------------------------------------------------------

def implied_vol(
    market_price: float,
    S: float, K: float, T: float, r: float,
    right: Literal["CE", "PE", "C", "P"],
    tol: float = 1e-6,
    max_iter: int = 100,
) -> float | None:
    """Solve for IV given the observed market price.

    Newton-Raphson primary; Brent's method fallback for deep-OTM /
    low-vega regimes where Newton diverges. Step size is clamped to
    prevent runaway.

    Returns None if the price is below the risk-free-discounted intrinsic
    (impossible to price any valid IV) or if both solvers fail.
    """
    if T <= 0:
        return None

    is_call = right in ("CE", "C")
    # Risk-free-discounted intrinsic: for a European option, the price
    # cannot be below S - K·e^{-rT} (call) or K·e^{-rT} - S (put).
    discount = math.exp(-r * T)
    intrinsic_discounted = max(0.0, S - K * discount) if is_call else max(0.0, K * discount - S)
    if market_price < intrinsic_discounted - 0.01:
        return None

    # --- Newton-Raphson with clamped step ---
    sigma = 0.30
    for _ in range(max_iter):
        price = bs_price(S, K, T, r, sigma, right)
        if abs(price - market_price) < tol:
            return sigma
        d1, _ = _d1d2(S, K, T, r, sigma)
        vega_raw = S * _norm_pdf(d1) * math.sqrt(T)
        if vega_raw < 1e-10:
            break  # vega too small, Newton diverges — fall through to Brent
        # Clamp the step to ±0.5 volatility points to prevent overshoot
        step = (price - market_price) / vega_raw
        step = max(-0.5, min(0.5, step))
        sigma -= step
        sigma = max(0.001, min(5.0, sigma))

    # --- Brent's method fallback ---
    try:
        from scipy.optimize import brentq
    except ImportError:
        return None

    def f(s: float) -> float:
        return bs_price(S, K, T, r, s, right) - market_price

    try:
        # Bracket IV in [0.001, 5.0]. If f doesn't change sign, Brent fails.
        return float(brentq(f, 0.001, 5.0, xtol=tol, maxiter=200))
    except (ValueError, RuntimeError):
        return None


# ---------------------------------------------------------------------------
# Helper: default risk-free rate from env
# ---------------------------------------------------------------------------

def default_risk_free_rate() -> float:
    return float(os.getenv("KITE_RISK_FREE_RATE", "0.065"))
