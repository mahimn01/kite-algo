"""Worked-example cost line items: futures + ETF round trips."""

from __future__ import annotations

import pytest

from kite_algo.backtest.costs import IndianCostModel


def test_futures_round_trip_22000_1lot() -> None:
    model = IndianCostModel("futures")
    buy = model.compute_cost(price=22_000.0, qty_units=1, side="buy", lot_size=75)
    sell = model.compute_cost(price=22_000.0, qty_units=1, side="sell", lot_size=75)

    # Notional per leg = 22000 * 1 * 75 = 1,650,000
    notional = 1_650_000.0
    # Brokerage
    assert buy.brokerage == pytest.approx(20.0)
    assert sell.brokerage == pytest.approx(20.0)
    # STT (sell-only on futures)
    assert buy.stt == pytest.approx(0.0)
    assert sell.stt == pytest.approx(0.0002 * notional)  # 330
    # Exchange (per leg)
    assert buy.exchange == pytest.approx(0.0000173 * notional)
    assert sell.exchange == pytest.approx(0.0000173 * notional)
    # SEBI
    assert buy.sebi == pytest.approx(0.000001 * notional)
    assert sell.sebi == pytest.approx(0.000001 * notional)
    # Stamp (buy-only)
    assert buy.stamp == pytest.approx(0.00002 * notional)  # 33
    assert sell.stamp == pytest.approx(0.0)
    # IPFT
    assert buy.ipft == pytest.approx(0.000001 * notional)
    assert sell.ipft == pytest.approx(0.000001 * notional)

    # Round trip aggregate
    rt = model.round_trip_cost(22_000.0, 22_000.0, qty_units=1, lot_size=75)
    assert rt.brokerage == pytest.approx(40.0)
    assert rt.stt == pytest.approx(330.0)
    assert rt.exchange == pytest.approx(57.09, abs=0.5)
    assert rt.sebi == pytest.approx(3.30, abs=0.01)
    assert rt.stamp == pytest.approx(33.0)
    assert rt.ipft == pytest.approx(3.30, abs=0.01)
    # GST = 18% of (brokerage + exchange + sebi) summed across the two legs.
    expected_gst = 0.18 * (40.0 + 57.09 + 3.30)
    assert rt.gst == pytest.approx(expected_gst, abs=0.05)
    assert rt.total == pytest.approx(484.77, abs=5.0)


def test_etf_round_trip_niftybees_400sh_at_250() -> None:
    model = IndianCostModel("etf")
    rt = model.round_trip_cost(entry_price=250.0, exit_price=250.0, qty_units=400, lot_size=1)

    # Notional per leg = 250 * 400 = 100,000
    assert rt.brokerage == pytest.approx(0.0)
    assert rt.stt == pytest.approx(0.001 * 100_000 * 2)  # 200
    assert rt.exchange == pytest.approx(0.0000297 * 100_000 * 2)  # 5.94
    assert rt.sebi == pytest.approx(0.000001 * 100_000 * 2)  # 0.20
    assert rt.stamp == pytest.approx(0.00015 * 100_000)  # 15 (buy only)
    assert rt.ipft == pytest.approx(0.000001 * 100_000 * 2)
    assert rt.dp_charge == pytest.approx(15.93)
    # Total ≈ 238
    assert rt.total == pytest.approx(238.0, abs=5.0)


def test_none_mode_zero_cost() -> None:
    model = IndianCostModel("none")
    bd = model.compute_cost(price=22_000.0, qty_units=1, side="buy", lot_size=75)
    assert bd.total == 0.0
    assert all(v == 0.0 for v in (bd.brokerage, bd.stt, bd.exchange, bd.sebi, bd.stamp, bd.ipft, bd.dp_charge, bd.gst))


def test_options_round_trip_premium_100_to_80_1lot() -> None:
    model = IndianCostModel("options")
    # Buy-to-open at premium 100, sell-to-close at 80; 1 lot of 75.
    buy = model.compute_cost(price=100.0, qty_units=1, side="buy", lot_size=75)
    sell = model.compute_cost(price=80.0, qty_units=1, side="sell", lot_size=75)

    assert buy.brokerage == pytest.approx(20.0)
    assert sell.brokerage == pytest.approx(20.0)
    # STT 0.1% on the SELL leg only (notional = 80 * 75 = 6000)
    assert buy.stt == pytest.approx(0.0)
    assert sell.stt == pytest.approx(0.001 * 6_000)  # 6.0
    # Exchange 0.03503% per leg
    assert buy.exchange == pytest.approx(0.0003503 * 100 * 75)
    assert sell.exchange == pytest.approx(0.0003503 * 80 * 75)
    # Stamp 0.003% on the BUY leg only (notional = 100 * 75 = 7500)
    assert buy.stamp == pytest.approx(0.00003 * 7_500)  # 0.225
    assert sell.stamp == pytest.approx(0.0)

    rt = model.round_trip_cost(entry_price=100.0, exit_price=80.0, qty_units=1, lot_size=75)
    assert rt.brokerage == pytest.approx(40.0)
    assert rt.stt == pytest.approx(6.0)
    assert rt.stamp == pytest.approx(0.225)
    expected_gst = 0.18 * (rt.brokerage + rt.exchange + rt.sebi)
    assert rt.gst == pytest.approx(expected_gst, abs=0.01)
    assert rt.total > rt.brokerage  # sanity: charges accumulate


def test_invalid_inputs() -> None:
    with pytest.raises(ValueError):
        IndianCostModel("invalid")
    m = IndianCostModel("futures")
    with pytest.raises(ValueError):
        m.compute_cost(0.0, 1, "buy", 75)
    with pytest.raises(ValueError):
        m.compute_cost(100.0, 1, "hold", 75)
