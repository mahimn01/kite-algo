"""Tests for --fields projection + --summary rollups.

These reduce an agent's context cost on high-cardinality endpoints; the
shape must be stable because agents consume it directly.
"""

from __future__ import annotations

import pytest

from kite_algo.projection import (
    parse_fields,
    project_rows,
    summarize_holdings,
    summarize_option_chain,
    summarize_orders,
    summarize_positions,
)


# -----------------------------------------------------------------------------
# parse_fields
# -----------------------------------------------------------------------------

class TestParseFields:
    def test_basic(self) -> None:
        assert parse_fields("a,b,c") == ["a", "b", "c"]

    def test_whitespace_trimmed(self) -> None:
        assert parse_fields(" a , b ,c ") == ["a", "b", "c"]

    def test_empty_is_none(self) -> None:
        assert parse_fields("") is None
        assert parse_fields(None) is None

    def test_commas_only_is_none(self) -> None:
        assert parse_fields(",,,") is None


# -----------------------------------------------------------------------------
# project_rows
# -----------------------------------------------------------------------------

class TestProjectRows:
    def test_keeps_only_named(self) -> None:
        rows = [{"a": 1, "b": 2, "c": 3}, {"a": 4, "b": 5, "c": 6}]
        assert project_rows(rows, ["a", "c"]) == [{"a": 1, "c": 3}, {"a": 4, "c": 6}]

    def test_missing_emits_none(self) -> None:
        """Missing fields come out as None for CSV-header stability."""
        rows = [{"a": 1}, {"a": 2, "b": 99}]
        out = project_rows(rows, ["a", "b", "c"])
        assert out == [{"a": 1, "b": None, "c": None}, {"a": 2, "b": 99, "c": None}]

    def test_none_fields_passthrough(self) -> None:
        rows = [{"a": 1, "b": 2}]
        assert project_rows(rows, None) is rows

    def test_empty_fields_passthrough(self) -> None:
        rows = [{"a": 1}]
        assert project_rows(rows, []) is rows


# -----------------------------------------------------------------------------
# summarize_orders
# -----------------------------------------------------------------------------

class TestSummarizeOrders:
    def test_empty(self) -> None:
        s = summarize_orders([])
        assert s == {
            "total": 0, "by_status": {}, "open_count": 0,
            "oldest_open_timestamp": None,
            "total_buy_value": 0.0, "total_sell_value": 0.0,
        }

    def test_mixed_statuses(self) -> None:
        orders = [
            {"status": "OPEN", "transaction_type": "BUY", "quantity": 10, "price": 100,
             "order_timestamp": "2026-04-21 10:00:00"},
            {"status": "COMPLETE", "transaction_type": "BUY", "quantity": 5,
             "average_price": 200},
            {"status": "CANCELLED", "transaction_type": "SELL", "quantity": 1, "price": 500},
            {"status": "OPEN", "transaction_type": "SELL", "quantity": 3, "price": 300,
             "order_timestamp": "2026-04-21 11:00:00"},
        ]
        s = summarize_orders(orders)
        assert s["total"] == 4
        assert s["by_status"] == {"OPEN": 2, "COMPLETE": 1, "CANCELLED": 1}
        assert s["open_count"] == 2
        assert s["oldest_open_timestamp"] == "2026-04-21 10:00:00"
        # total_buy_value = 10*100 (OPEN buy) + 5*200 (filled buy) = 2000
        assert s["total_buy_value"] == 2000.0
        # total_sell_value = 1*500 + 3*300 = 1400
        assert s["total_sell_value"] == 1400.0


# -----------------------------------------------------------------------------
# summarize_holdings
# -----------------------------------------------------------------------------

class TestSummarizeHoldings:
    def test_empty(self) -> None:
        s = summarize_holdings([])
        assert s["count"] == 0
        assert s["total_invested_inr"] == 0

    def test_best_worst(self) -> None:
        holdings = [
            {"tradingsymbol": "RELIANCE", "quantity": 10, "average_price": 1000, "last_price": 1500, "pnl": 5000},
            {"tradingsymbol": "INFY", "quantity": 5, "average_price": 1500, "last_price": 1200, "pnl": -1500},
            {"tradingsymbol": "TCS", "quantity": 2, "average_price": 4000, "last_price": 4100, "pnl": 200},
        ]
        s = summarize_holdings(holdings)
        assert s["count"] == 3
        assert s["best_performer"]["symbol"] == "RELIANCE"
        assert s["worst_performer"]["symbol"] == "INFY"
        assert s["total_invested_inr"] == pytest.approx(10 * 1000 + 5 * 1500 + 2 * 4000)
        assert s["total_value_inr"] == pytest.approx(10 * 1500 + 5 * 1200 + 2 * 4100)


# -----------------------------------------------------------------------------
# summarize_positions
# -----------------------------------------------------------------------------

class TestSummarizePositions:
    def test_empty(self) -> None:
        s = summarize_positions({})
        assert s["open_count"] == 0
        assert s["net_pnl_inr"] == 0

    def test_open_and_closed(self) -> None:
        payload = {
            "net": [
                {"quantity": 100, "pnl": 1500, "realised": 0, "unrealised": 1500},
                {"quantity": 0, "pnl": 250, "realised": 250, "unrealised": 0},  # closed
            ],
            "day": [
                {"m2m": 800},
                {"m2m": -200},
            ],
        }
        s = summarize_positions(payload)
        assert s["open_count"] == 1
        assert s["net_count"] == 2
        assert s["day_m2m_inr"] == 600
        assert s["net_pnl_inr"] == 1750


# -----------------------------------------------------------------------------
# summarize_option_chain
# -----------------------------------------------------------------------------

class TestSummarizeOptionChain:
    def test_empty(self) -> None:
        s = summarize_option_chain([])
        assert s["strike_count"] == 0
        assert s["atm_strike"] is None

    def _chain(self):
        # Symmetric NIFTY chain around 24000 spot.
        rows = []
        for strike in (23900, 24000, 24100):
            for right, oi in [("CE", 1000), ("PE", 800)]:
                rows.append({
                    "strike": strike, "right": right, "oi": oi,
                    "iv": 15.0 if strike == 24000 else 16.0,
                })
        return rows

    def test_atm_nearest_to_spot(self) -> None:
        # 24080 is unambiguously closer to 24100 (distance 20) than to 24000 (80).
        s = summarize_option_chain(self._chain(), spot=24080)
        assert s["atm_strike"] == 24100

    def test_atm_exact(self) -> None:
        s = summarize_option_chain(self._chain(), spot=24000)
        assert s["atm_strike"] == 24000
        assert s["atm_ce_iv"] == 15.0
        assert s["atm_pe_iv"] == 15.0

    def test_oi_totals(self) -> None:
        s = summarize_option_chain(self._chain(), spot=24000)
        assert s["total_ce_oi"] == 3000
        assert s["total_pe_oi"] == 2400
        # 2400 / 3000 = 0.8
        assert s["put_call_oi_ratio"] == 0.8

    def test_max_pain_is_finite(self) -> None:
        s = summarize_option_chain(self._chain(), spot=24000)
        assert s["max_pain"] in (23900, 24000, 24100)

    def test_no_spot_falls_back_to_middle(self) -> None:
        s = summarize_option_chain(self._chain(), spot=None)
        assert s["atm_strike"] == 24000  # middle of 3 strikes

    def test_strike_count(self) -> None:
        s = summarize_option_chain(self._chain(), spot=24000)
        assert s["strike_count"] == 3
