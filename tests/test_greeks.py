"""Black-Scholes greeks and IV solver."""

from __future__ import annotations

import math

import pytest

from kite_algo.greeks import (
    bs_delta,
    bs_gamma,
    bs_price,
    bs_theta,
    bs_vega,
    greeks,
    implied_vol,
)


class TestBSPrice:
    def test_atm_call_positive(self) -> None:
        p = bs_price(100, 100, 0.25, 0.05, 0.20, "CE")
        assert 4.0 < p < 5.5

    def test_atm_put_positive(self) -> None:
        p = bs_price(100, 100, 0.25, 0.05, 0.20, "PE")
        assert 2.5 < p < 4.0

    def test_put_call_parity(self) -> None:
        S, K, T, r, sigma = 100, 100, 0.5, 0.05, 0.25
        c = bs_price(S, K, T, r, sigma, "CE")
        p = bs_price(S, K, T, r, sigma, "PE")
        parity = c - p - (S - K * math.exp(-r * T))
        assert abs(parity) < 1e-6

    def test_expired_itm_call(self) -> None:
        assert bs_price(110, 100, 0, 0.05, 0.2, "CE") == 10.0

    def test_expired_otm_call(self) -> None:
        assert bs_price(90, 100, 0, 0.05, 0.2, "CE") == 0.0

    def test_expired_itm_put(self) -> None:
        assert bs_price(90, 100, 0, 0.05, 0.2, "PE") == 10.0

    def test_deep_itm_call_equals_intrinsic_plus_discount(self) -> None:
        S, K, T, r = 200, 100, 0.5, 0.05
        p = bs_price(S, K, T, r, 0.05, "CE")
        intrinsic_pv = S - K * math.exp(-r * T)
        assert abs(p - intrinsic_pv) < 0.5

    def test_deep_otm_call_near_zero(self) -> None:
        assert bs_price(100, 200, 0.25, 0.05, 0.15, "CE") < 0.1


class TestBSDelta:
    def test_atm_call_delta_near_half(self) -> None:
        d = bs_delta(100, 100, 0.25, 0.05, 0.20, "CE")
        assert 0.5 < d < 0.65

    def test_atm_put_delta_near_negative_half(self) -> None:
        d = bs_delta(100, 100, 0.25, 0.05, 0.20, "PE")
        assert -0.5 < d < -0.35

    def test_deep_itm_call_delta_one(self) -> None:
        d = bs_delta(200, 100, 0.25, 0.05, 0.20, "CE")
        assert d > 0.98

    def test_deep_otm_call_delta_zero(self) -> None:
        d = bs_delta(50, 100, 0.25, 0.05, 0.20, "CE")
        assert d < 0.02

    def test_put_call_delta_relationship(self) -> None:
        S, K, T, r, sigma = 100, 105, 0.3, 0.05, 0.25
        cd = bs_delta(S, K, T, r, sigma, "CE")
        pd = bs_delta(S, K, T, r, sigma, "PE")
        assert abs(cd - pd - 1.0) < 1e-6


class TestBSGamma:
    def test_gamma_positive_at_atm(self) -> None:
        g = bs_gamma(100, 100, 0.25, 0.05, 0.20)
        assert g > 0

    def test_gamma_peak_near_atm(self) -> None:
        g_atm = bs_gamma(100, 100, 0.25, 0.05, 0.20)
        g_itm = bs_gamma(150, 100, 0.25, 0.05, 0.20)
        g_otm = bs_gamma(50, 100, 0.25, 0.05, 0.20)
        assert g_atm > g_itm
        assert g_atm > g_otm

    def test_gamma_same_for_call_and_put(self) -> None:
        g = bs_gamma(100, 105, 0.3, 0.05, 0.25)
        assert g == bs_gamma(100, 105, 0.3, 0.05, 0.25)


class TestBSTheta:
    def test_theta_negative_for_long_option(self) -> None:
        assert bs_theta(100, 100, 0.25, 0.05, 0.20, "CE") < 0
        assert bs_theta(100, 100, 0.25, 0.05, 0.20, "PE") < 0

    def test_theta_zero_at_expiry(self) -> None:
        assert bs_theta(100, 100, 0, 0.05, 0.20, "CE") == 0.0


class TestBSVega:
    def test_vega_positive(self) -> None:
        assert bs_vega(100, 100, 0.25, 0.05, 0.20) > 0

    def test_vega_max_near_atm(self) -> None:
        v_atm = bs_vega(100, 100, 0.5, 0.05, 0.25)
        v_itm = bs_vega(150, 100, 0.5, 0.05, 0.25)
        v_otm = bs_vega(50, 100, 0.5, 0.05, 0.25)
        assert v_atm > v_itm
        assert v_atm > v_otm

    def test_vega_zero_at_expiry(self) -> None:
        assert bs_vega(100, 100, 0, 0.05, 0.20) == 0.0


class TestImpliedVol:
    def test_iv_roundtrip_atm_call(self) -> None:
        S, K, T, r, true_sigma = 100, 100, 0.25, 0.05, 0.30
        price = bs_price(S, K, T, r, true_sigma, "CE")
        recovered = implied_vol(price, S, K, T, r, "CE")
        assert recovered is not None
        assert abs(recovered - true_sigma) < 1e-4

    def test_iv_roundtrip_otm_put(self) -> None:
        S, K, T, r, true_sigma = 100, 95, 0.5, 0.05, 0.40
        price = bs_price(S, K, T, r, true_sigma, "PE")
        recovered = implied_vol(price, S, K, T, r, "PE")
        assert recovered is not None
        assert abs(recovered - true_sigma) < 1e-4

    def test_iv_roundtrip_itm_call(self) -> None:
        S, K, T, r, true_sigma = 100, 90, 0.3, 0.05, 0.25
        price = bs_price(S, K, T, r, true_sigma, "CE")
        recovered = implied_vol(price, S, K, T, r, "CE")
        assert recovered is not None
        assert abs(recovered - true_sigma) < 1e-4

    def test_iv_below_intrinsic_returns_none(self) -> None:
        # Impossible price — below intrinsic
        iv = implied_vol(0.01, 150, 100, 0.5, 0.05, "CE")
        assert iv is None

    def test_iv_expired_option_returns_none(self) -> None:
        assert implied_vol(5.0, 100, 100, 0, 0.05, "CE") is None

    def test_iv_nifty_weekly_realistic(self) -> None:
        # NIFTY 24400CE Apr-21 — real market values from live test
        S, K, T, r, market = 24356.65, 24400, 5 / 365.0, 0.065, 162.0
        iv = implied_vol(market, S, K, T, r, "CE")
        assert iv is not None
        assert 0.10 < iv < 0.25  # realistic NIFTY weekly IV range


class TestGreeksBundle:
    def test_all_fields_present(self) -> None:
        g = greeks(100, 100, 0.25, 0.05, 0.20, "CE")
        for key in ("price", "delta", "gamma", "theta", "vega", "rho", "iv"):
            assert key in g

    def test_greeks_consistent_with_individual_functions(self) -> None:
        S, K, T, r, sigma = 100, 105, 0.3, 0.05, 0.25
        g = greeks(S, K, T, r, sigma, "CE")
        assert g["price"] == bs_price(S, K, T, r, sigma, "CE")
        assert g["delta"] == bs_delta(S, K, T, r, sigma, "CE")
        assert g["gamma"] == bs_gamma(S, K, T, r, sigma)
        assert g["theta"] == bs_theta(S, K, T, r, sigma, "CE")
        assert g["vega"] == bs_vega(S, K, T, r, sigma)


class TestEdgeCases:
    def test_zero_vol_intrinsic_only(self) -> None:
        # At zero vol, call = max(0, S - K*e^-rT)
        p = bs_price(110, 100, 0.5, 0.05, 0.001, "CE")
        expected = max(0, 110 - 100 * math.exp(-0.05 * 0.5))
        assert abs(p - expected) < 0.1

    def test_c_and_ce_equivalent(self) -> None:
        # Both right codes should produce identical output
        assert bs_price(100, 100, 0.25, 0.05, 0.20, "C") == bs_price(100, 100, 0.25, 0.05, 0.20, "CE")
        assert bs_price(100, 100, 0.25, 0.05, 0.20, "P") == bs_price(100, 100, 0.25, 0.05, 0.20, "PE")
