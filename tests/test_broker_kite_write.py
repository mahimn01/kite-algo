"""Tests for KiteBroker write-path: place/modify/cancel/get_order_status.

Uses a stub kiteconnect client (Mock) — no real HTTP.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from kite_algo.broker.base import OrderRequest, OrderResult
from kite_algo.broker.kite import KiteBroker
from kite_algo.config import KiteConfig, TradingConfig
from kite_algo.instruments import InstrumentSpec


def _broker(*, dry_run=False, live_enabled=True, allow_live=True,
            kite_client=None) -> KiteBroker:
    cfg = TradingConfig(
        dry_run=dry_run,
        live_enabled=live_enabled,
        allow_live=allow_live,
        kite=KiteConfig(
            api_key="KEY", api_secret="SEC", access_token="TOK", user_id="U",
        ),
    )
    b = KiteBroker(cfg)
    b._client = kite_client or Mock()  # type: ignore[attr-defined]
    return b


def _request(**overrides) -> OrderRequest:
    d = dict(
        instrument=InstrumentSpec(symbol="RELIANCE", exchange="NSE"),
        side="BUY", quantity=1, order_type="LIMIT",
        product="CNC", variety="regular", validity="DAY",
        limit_price=1340.0,
    )
    d.update(overrides)
    return OrderRequest(**d)


# -----------------------------------------------------------------------------
# Gates
# -----------------------------------------------------------------------------

class TestGates:
    def test_dry_run_blocks_place(self) -> None:
        b = _broker(dry_run=True)
        with pytest.raises(RuntimeError, match="dry_run"):
            b.place_order(_request())

    def test_live_enabled_required(self) -> None:
        b = _broker(live_enabled=False)
        with pytest.raises(RuntimeError, match="TRADING_ALLOW_LIVE"):
            b.place_order(_request())

    def test_halt_blocks_place(self, tmp_path, monkeypatch) -> None:
        halt_file = tmp_path / "HALTED"
        monkeypatch.setenv("KITE_HALT_PATH", str(halt_file))
        from kite_algo.halt import write_halt
        write_halt(reason="emergency", by="op")

        b = _broker()
        with pytest.raises(RuntimeError, match="HALTED"):
            b.place_order(_request())

    def test_halt_blocks_modify(self, tmp_path, monkeypatch) -> None:
        halt_file = tmp_path / "HALTED"
        monkeypatch.setenv("KITE_HALT_PATH", str(halt_file))
        from kite_algo.halt import write_halt
        write_halt(reason="x", by="op")
        b = _broker()
        with pytest.raises(RuntimeError, match="HALTED"):
            b.modify_order("123", _request())

    def test_halt_blocks_cancel(self, tmp_path, monkeypatch) -> None:
        halt_file = tmp_path / "HALTED"
        monkeypatch.setenv("KITE_HALT_PATH", str(halt_file))
        from kite_algo.halt import write_halt
        write_halt(reason="x", by="op")
        b = _broker()
        with pytest.raises(RuntimeError, match="HALTED"):
            b.cancel_order("123")


# -----------------------------------------------------------------------------
# place_order
# -----------------------------------------------------------------------------

class TestPlace:
    def test_place_returns_result(self) -> None:
        kc = Mock()
        kc.place_order.return_value = "KITE_ORDER_123"
        b = _broker(kite_client=kc)
        result = b.place_order(_request())
        assert isinstance(result, OrderResult)
        assert result.order_id == "KITE_ORDER_123"
        assert result.status == "SUBMITTED"

    def test_market_order_gets_market_protection(self) -> None:
        kc = Mock()
        kc.place_order.return_value = "ORD_1"
        b = _broker(kite_client=kc)
        b.place_order(_request(order_type="MARKET", limit_price=None, product="MIS"))
        kwargs = kc.place_order.call_args.kwargs
        assert kwargs.get("market_protection") == -1

    def test_slm_order_gets_market_protection(self) -> None:
        kc = Mock()
        kc.place_order.return_value = "ORD_1"
        b = _broker(kite_client=kc)
        b.place_order(_request(order_type="SL-M", limit_price=None,
                               trigger_price=1300, product="MIS"))
        kwargs = kc.place_order.call_args.kwargs
        assert kwargs.get("market_protection") == -1

    def test_limit_order_no_market_protection(self) -> None:
        kc = Mock()
        kc.place_order.return_value = "ORD_1"
        b = _broker(kite_client=kc)
        b.place_order(_request())
        kwargs = kc.place_order.call_args.kwargs
        assert "market_protection" not in kwargs

    def test_tag_auto_generated_when_absent(self) -> None:
        kc = Mock()
        kc.place_order.return_value = "ORD_1"
        b = _broker(kite_client=kc)
        b.place_order(_request())
        kwargs = kc.place_order.call_args.kwargs
        assert kwargs["tag"].startswith("KA")

    def test_explicit_tag_respected(self) -> None:
        kc = Mock()
        kc.place_order.return_value = "ORD_1"
        b = _broker(kite_client=kc)
        b.place_order(_request(tag="MY_TAG_01"))
        assert kc.place_order.call_args.kwargs["tag"] == "MY_TAG_01"


# -----------------------------------------------------------------------------
# modify / cancel / get_order_status
# -----------------------------------------------------------------------------

class TestModifyCancelStatus:
    def test_modify_tracks_count(self) -> None:
        from kite_algo.resilience import reset_modification_counts, get_modification_count
        reset_modification_counts()

        kc = Mock()
        kc.modify_order.return_value = "ORD_1"
        b = _broker(kite_client=kc)
        b.modify_order("ORD_1", _request(quantity=2))
        assert get_modification_count("ORD_1") == 1

    def test_cancel_invokes_client(self) -> None:
        kc = Mock()
        b = _broker(kite_client=kc)
        b.cancel_order("ORD_1", variety="regular")
        kc.cancel_order.assert_called_once_with(variety="regular", order_id="ORD_1")

    def test_get_order_status_picks_last_history_entry(self) -> None:
        kc = Mock()
        kc.order_history.return_value = [
            {"order_timestamp": "2026-04-21 10:00:00", "status": "OPEN",
             "filled_quantity": 0, "pending_quantity": 1},
            {"order_timestamp": "2026-04-21 10:00:02", "status": "COMPLETE",
             "filled_quantity": 1, "pending_quantity": 0,
             "average_price": 1340.5},
        ]
        b = _broker(kite_client=kc)
        r = b.get_order_status("ORD_1")
        assert r.status == "COMPLETE"
        assert r.filled == 1
        assert r.avg_price == 1340.5

    def test_get_order_status_empty_history(self) -> None:
        kc = Mock()
        kc.order_history.return_value = []
        b = _broker(kite_client=kc)
        r = b.get_order_status("ORD_1")
        assert r.status == "UNKNOWN"
