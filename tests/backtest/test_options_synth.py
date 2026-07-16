"""Sanity checks for the BSM-on-real-ticks synthetic option pricer."""

from __future__ import annotations

import pandas as pd
import pytest

from kite_algo.backtest.options_synth import (
    atm_strike,
    price_path,
    strike_at_delta,
    vertical_spread_path,
    years_to_expiry,
)


def _underlying(prices: list[float], expiry: pd.Timestamp) -> pd.DataFrame:
    idx = pd.date_range(expiry - pd.Timedelta(days=1), periods=len(prices), freq="15min", tz="UTC")
    return pd.DataFrame({"close": prices}, index=idx)


def test_atm_strike_rounds_to_step() -> None:
    assert atm_strike(24_039.0) == 24_050
    assert atm_strike(24_024.0) == 24_000
    assert atm_strike(2_914.0, step=50) == 2_900
    with pytest.raises(ValueError):
        atm_strike(100.0, step=0)


def test_years_to_expiry_floors_at_zero() -> None:
    now = pd.Timestamp("2026-06-29 09:15", tz="UTC")
    exp = pd.Timestamp("2026-06-30 09:15", tz="UTC")
    assert years_to_expiry(now, exp) == pytest.approx(1 / 365, rel=1e-6)
    assert years_to_expiry(exp, now) == 0.0  # past expiry never negative


def test_price_path_put_higher_when_spot_falls() -> None:
    exp = pd.Timestamp("2026-07-07 10:00", tz="UTC")
    df = _underlying([24_100.0, 24_000.0], exp)
    pe = price_path(df, strike=24_050, expiry=exp, right="PE", iv=0.15)
    assert len(pe) == 2
    assert pe.iloc[1] > pe.iloc[0]  # put gains as spot drops 24100 -> 24000
    assert (pe > 0).all()


def test_price_path_decays_toward_intrinsic_at_expiry() -> None:
    exp = pd.Timestamp("2026-07-07 15:30", tz="UTC")
    # last bar sits at expiry: ATM call value -> intrinsic max(S-K,0)
    idx = pd.DatetimeIndex([exp - pd.Timedelta(hours=2), exp])
    df = pd.DataFrame({"close": [24_200.0, 24_200.0]}, index=idx)
    ce = price_path(df, strike=24_000, expiry=exp, right="CE", iv=0.15)
    assert ce.iloc[0] > 200.0          # time value present 2h before expiry
    assert ce.iloc[1] == pytest.approx(200.0, abs=1e-6)  # intrinsic only at expiry


def test_strike_at_delta_picks_otm_compliant_strike() -> None:
    from kite_algo.greeks import bs_delta

    now = pd.Timestamp("2026-07-01 09:45", tz="UTC")
    exp = pd.Timestamp("2026-07-07 15:30", tz="UTC")
    spot, iv = 24_000.0, 0.14
    T = years_to_expiry(now, exp)

    kp = strike_at_delta(spot, exp, now, iv, "PE", 0.10)
    kc = strike_at_delta(spot, exp, now, iv, "CE", 0.10)
    assert kp is not None and kc is not None
    assert kp < spot < kc
    # chosen strike compliant, one step closer to spot is not
    assert abs(bs_delta(spot, kp, T, 0.065, iv, "PE")) <= 0.10
    assert abs(bs_delta(spot, kp + 50, T, 0.065, iv, "PE")) > 0.10
    assert bs_delta(spot, kc, T, 0.065, iv, "CE") <= 0.10
    assert bs_delta(spot, kc - 50, T, 0.065, iv, "CE") > 0.10

    with pytest.raises(ValueError):
        strike_at_delta(spot, exp, now, iv, "PE", 0.75)


def test_vertical_spread_bounded_by_width() -> None:
    exp = pd.Timestamp("2026-07-07 10:00", tz="UTC")
    df = _underlying([24_100.0, 23_700.0, 24_300.0], exp)
    spread = vertical_spread_path(df, short_strike=24_000, long_strike=23_850, expiry=exp, right="PE", iv=0.18)
    assert (spread >= -1e-9).all()
    assert (spread <= 150.0 + 1e-6).all()  # bull-put credit spread capped at width
