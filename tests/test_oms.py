"""Tests for OrderManager."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from kite_algo.broker.base import OrderResult
from kite_algo.config import KiteConfig, TradingConfig
from kite_algo.instruments import InstrumentSpec
from kite_algo.oms import OMSResult, OrderManager
from kite_algo.orders import TradeIntent
from kite_algo.persistence import SqliteStore


def _intent(**overrides) -> TradeIntent:
    d = dict(
        instrument=InstrumentSpec(symbol="RELIANCE", exchange="NSE"),
        side="BUY", quantity=1, order_type="LIMIT",
        product="CNC", limit_price=1340.0,
    )
    d.update(overrides)
    return TradeIntent(**d)


def _cfg(dry_run: bool = False) -> TradingConfig:
    return TradingConfig(
        broker="kite", dry_run=dry_run,
        kite=KiteConfig(api_key="K", api_secret="S", access_token="T", user_id="U"),
    )


# -----------------------------------------------------------------------------
# Submit
# -----------------------------------------------------------------------------

class TestSubmit:
    def test_submit_persists(self, tmp_path) -> None:
        broker = Mock()
        broker.place_order.return_value = OrderResult(
            order_id="ORD_1", status="SUBMITTED",
            avg_price=0, filled=0, remaining=1,
        )
        broker.get_order_status.return_value = OrderResult(
            order_id="ORD_1", status="OPEN",
            avg_price=0, filled=0, remaining=1,
        )
        store = SqliteStore(tmp_path / "t.sqlite")
        rid = store.start_run(cfg={})

        oms = OrderManager(broker, _cfg(), store=store, run_id=rid)
        result = oms.submit(_intent(strategy="s1"))
        assert result.accepted is True
        assert result.order_id == "ORD_1"

        # Persisted order + status event.
        row = store.get_order("ORD_1")
        assert row is not None
        assert row["tradingsymbol"] == "RELIANCE"
        evt = store.get_latest_status("ORD_1")
        assert evt is not None
        assert evt["status"] == "OPEN"

    def test_dry_run_noop(self) -> None:
        broker = Mock()
        oms = OrderManager(broker, _cfg(dry_run=True))
        result = oms.submit(_intent())
        assert result.action == "noop"
        assert result.accepted is False
        broker.place_order.assert_not_called()

    def test_tracks_in_memory(self) -> None:
        broker = Mock()
        broker.place_order.return_value = OrderResult(
            order_id="X", status="SUBMITTED",
            avg_price=0, filled=0, remaining=1,
        )
        broker.get_order_status.return_value = OrderResult(
            order_id="X", status="OPEN",
            avg_price=0, filled=0, remaining=1,
        )
        oms = OrderManager(broker, _cfg())
        oms.submit(_intent())
        assert "X" in oms.tracked_order_ids()
        assert "X" in oms.active_order_ids()

    def test_broker_error_propagates(self) -> None:
        broker = Mock()
        broker.place_order.side_effect = RuntimeError("bad")
        oms = OrderManager(broker, _cfg())
        with pytest.raises(RuntimeError):
            oms.submit(_intent())


# -----------------------------------------------------------------------------
# Modify / cancel
# -----------------------------------------------------------------------------

class TestModifyCancel:
    def test_modify_updates_tracked(self) -> None:
        broker = Mock()
        broker.place_order.return_value = OrderResult(
            order_id="X", status="SUBMITTED", filled=0, remaining=1,
        )
        broker.get_order_status.return_value = OrderResult(
            order_id="X", status="OPEN",
            avg_price=0, filled=0, remaining=1,
        )
        broker.modify_order.return_value = OrderResult(
            order_id="X", status="MODIFY_SUBMITTED", filled=0, remaining=2,
        )
        oms = OrderManager(broker, _cfg())
        oms.submit(_intent())
        result = oms.modify("X", _intent(quantity=2, limit_price=1345))
        assert result.accepted is True

    def test_cancel(self) -> None:
        broker = Mock()
        oms = OrderManager(broker, _cfg())
        result = oms.cancel("ORD_1", variety="regular")
        broker.cancel_order.assert_called_once()
        assert result.action == "cancel"

    def test_dry_run_skips_cancel(self) -> None:
        broker = Mock()
        oms = OrderManager(broker, _cfg(dry_run=True))
        result = oms.cancel("X")
        assert result.action == "noop"
        broker.cancel_order.assert_not_called()


# -----------------------------------------------------------------------------
# Reconciliation
# -----------------------------------------------------------------------------

class TestReconcile:
    def test_active_orders_polled(self) -> None:
        """No store attached → submit doesn't call get_order_status. Only
        the two reconcile() calls consume the side_effect list.
        """
        broker = Mock()
        broker.place_order.side_effect = [
            OrderResult(order_id="A", status="SUBMITTED", filled=0, remaining=1),
            OrderResult(order_id="B", status="SUBMITTED", filled=0, remaining=1),
        ]
        broker.get_order_status.side_effect = [
            OrderResult(order_id="A", status="COMPLETE", filled=1, remaining=0),
            OrderResult(order_id="B", status="OPEN", filled=0, remaining=1),
        ]

        oms = OrderManager(broker, _cfg())
        oms.submit(_intent())
        oms.submit(_intent())
        summary = oms.reconcile()
        assert summary["checked"] == 2
        assert summary["terminal_now"] == 1
        assert summary["updates"]["A"] == "COMPLETE"
        assert summary["updates"]["B"] == "OPEN"

    def test_track_open_returns_once_all_terminal(self) -> None:
        broker = Mock()
        broker.place_order.return_value = OrderResult(
            order_id="X", status="SUBMITTED", filled=0, remaining=1,
        )
        # Without a store, submit doesn't call get_order_status. The first
        # reconcile sees OPEN; the second flips to COMPLETE and track_open
        # exits.
        states = iter([
            OrderResult(order_id="X", status="OPEN", filled=0, remaining=1),
            OrderResult(order_id="X", status="COMPLETE", filled=1, remaining=0),
        ])
        broker.get_order_status.side_effect = lambda oid: next(states)

        oms = OrderManager(broker, _cfg())
        oms.submit(_intent())
        summary = oms.track_open_orders(poll_seconds=0.01, timeout_seconds=0.5)
        assert summary["checked"] == 1
        assert summary["terminal_now"] == 1
