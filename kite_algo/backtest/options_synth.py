"""BSM-on-real-ticks synthetic option pricing for backtests.

Expired weekly/monthly option contracts are de-listed by Kite, so their
historical ticks cannot be fetched — an options-strategy backtest therefore
has no real option prices to replay. This module reconstructs them: it replays
the REAL underlying bar series and prices the option at each bar with
Black-Scholes (`kite_algo.greeks.bs_price`), using India VIX as the implied-vol
input and a clock that decays time-to-expiry through the session.

This is directionally faithful — the price PATH is the actual market day that
happened — but not penny-accurate: BSM ignores the vol smile/skew and the
weekly-vs-30-day term premium. Validated against one live contract (NIFTY
29 Jun 2026 weekly): the modelled price tracked the real one in direction and
target-touch while under-pricing the absolute premium by ~18%. Use it to judge
whether a strategy has an edge (sign of expectancy, win rate, regime behaviour),
not for exact rupee P&L.
"""

from __future__ import annotations

from typing import Literal

import pandas as pd

from kite_algo.greeks import bs_price

Right = Literal["CE", "PE"]

_SECONDS_PER_DAY = 86_400.0


def atm_strike(spot: float, step: int = 50) -> int:
    """Nearest tradeable strike to spot (NIFTY strike step = 50)."""
    if step <= 0:
        raise ValueError(f"step must be positive, got {step}")
    return int(round(spot / step) * step)


def years_to_expiry(now: pd.Timestamp, expiry: pd.Timestamp, basis: float = 365.0) -> float:
    """Annualised time-to-expiry between two instants, floored at 0."""
    return max((expiry - now).total_seconds() / (basis * _SECONDS_PER_DAY), 0.0)


def price_at(
    spot: float,
    strike: float,
    expiry: pd.Timestamp,
    now: pd.Timestamp,
    iv: float,
    right: Right,
    r: float = 0.065,
) -> float:
    """BSM price of one option at a single instant from the real spot + IV."""
    return bs_price(spot, strike, years_to_expiry(now, expiry), r, iv, right)


def price_path(
    underlying: pd.DataFrame,
    strike: float,
    expiry: pd.Timestamp,
    right: Right,
    iv: float,
    r: float = 0.065,
    price_col: str = "close",
) -> pd.Series:
    """Synthetic option price for every bar of a real underlying series.

    `underlying` needs a tz-aware DatetimeIndex and `price_col`; `iv` is the
    implied vol as a decimal (e.g. India VIX / 100), held constant across the
    session while time-to-expiry decays. Returns a Series on the same index.
    """
    if price_col not in underlying.columns:
        raise ValueError(f"underlying missing column {price_col!r}")
    if iv <= 0:
        raise ValueError(f"iv must be positive, got {iv}")
    idx = underlying.index
    px = underlying[price_col].astype(float).to_numpy()
    values = [
        bs_price(float(px[i]), strike, years_to_expiry(idx[i], expiry), r, iv, right)
        for i in range(len(idx))
    ]
    return pd.Series(values, index=idx, name=f"{right}{int(strike)}")


def vertical_spread_path(
    underlying: pd.DataFrame,
    short_strike: float,
    long_strike: float,
    expiry: pd.Timestamp,
    right: Right,
    iv: float,
    r: float = 0.065,
    price_col: str = "close",
) -> pd.Series:
    """Net value of a short vertical (short_strike - long_strike), same `right`.

    For a bull-put spread use right="PE" with long_strike < short_strike; for a
    bear-call spread use right="CE" with long_strike > short_strike. The series
    is the credit-spread value, bounded in [0, |strike width|].
    """
    short_leg = price_path(underlying, short_strike, expiry, right, iv, r, price_col)
    long_leg = price_path(underlying, long_strike, expiry, right, iv, r, price_col)
    out = short_leg - long_leg
    out.name = f"{right}{int(short_strike)}/{int(long_strike)}"
    return out
