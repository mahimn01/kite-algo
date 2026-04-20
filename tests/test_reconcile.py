"""Tests for `reconcile` — local-vs-Kite drift detection."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from unittest.mock import Mock

import pytest

from kite_algo.audit import log_command
from kite_algo.groups import GroupStore
from kite_algo.idempotency import IdempotencyStore
from kite_algo.kite_tool import build_parser, cmd_reconcile


@pytest.fixture
def parser():
    return build_parser()


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """Point audit + idempotency + groups at fresh temp files."""
    monkeypatch.setenv("KITE_AUDIT_DIR", str(tmp_path / "audit"))

    idem_path = tmp_path / "idem.sqlite"

    from kite_algo import kite_tool as kt
    monkeypatch.setattr(
        kt, "IdempotencyStore",
        lambda path=None: IdempotencyStore(idem_path),
    )
    from kite_algo import idempotency as idem_mod
    monkeypatch.setattr(idem_mod, "IdempotencyStore",
                        lambda path=None: IdempotencyStore(idem_path))
    from kite_algo import groups as groups_mod
    monkeypatch.setattr(groups_mod, "GroupStore",
                        lambda path=None: GroupStore(idem_path))
    monkeypatch.setattr(kt, "GroupStore",
                        lambda path=None: GroupStore(idem_path), raising=False)
    return tmp_path


# -----------------------------------------------------------------------------
# Reconcile
# -----------------------------------------------------------------------------

class TestReconcile:
    def test_clean_when_nothing_local_or_remote(self, parser, isolated_state, monkeypatch) -> None:
        from kite_algo import kite_tool as kt
        # Stub broker with empty orders.
        client = Mock()
        client.orders.return_value = []
        monkeypatch.setattr(kt, "_new_client", lambda: client)

        args = parser.parse_args(["reconcile", "--format", "json"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cmd_reconcile(args)
        assert rc == 0  # clean
        data = json.loads(buf.getvalue())["data"]
        assert data["clean"] is True

    def test_missing_locally_is_flagged(self, parser, isolated_state, monkeypatch) -> None:
        """Kite has an order our audit doesn't know about → drift."""
        from kite_algo import kite_tool as kt
        client = Mock()
        client.orders.return_value = [
            {"order_id": "KITE_ONLY_1", "status": "OPEN",
             "tradingsymbol": "RELIANCE", "transaction_type": "BUY",
             "quantity": 10, "tag": "EXTERNAL"},
        ]
        monkeypatch.setattr(kt, "_new_client", lambda: client)

        args = parser.parse_args(["reconcile", "--format", "json"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cmd_reconcile(args)
        assert rc == 1
        data = json.loads(buf.getvalue())["data"]
        assert data["clean"] is False
        assert len(data["missing_locally"]) == 1
        assert data["missing_locally"][0]["order_id"] == "KITE_ONLY_1"

    def test_missing_remotely_is_flagged(self, parser, isolated_state, monkeypatch) -> None:
        """We have an audit record for an order that Kite no longer shows."""
        from kite_algo import kite_tool as kt

        # Seed an audit entry that mentions a kite_order_id.
        log_command(
            cmd="place", request_id="R1", args={"symbol": "X"},
            exit_code=0, kite_order_id="STALE_ORDER_1",
        )
        client = Mock()
        client.orders.return_value = []  # Kite has no such order
        monkeypatch.setattr(kt, "_new_client", lambda: client)

        args = parser.parse_args(["reconcile", "--format", "json"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cmd_reconcile(args)
        assert rc == 1
        data = json.loads(buf.getvalue())["data"]
        ids = [r["order_id"] for r in data["missing_remotely"]]
        assert "STALE_ORDER_1" in ids

    def test_orphan_groups(self, parser, isolated_state, monkeypatch) -> None:
        """Group with expected_legs=3 but only 1 member → orphan."""
        from kite_algo import kite_tool as kt

        store = GroupStore(isolated_state / "idem.sqlite")
        g = store.start(name="HALF_SPREAD", expected_legs=3)
        store.add_member(group_id=g.id, order_id="ORD_ONLY_1", leg_name="leg1")

        client = Mock()
        client.orders.return_value = []
        monkeypatch.setattr(kt, "_new_client", lambda: client)

        args = parser.parse_args(["reconcile", "--skip-kite", "--format", "json"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cmd_reconcile(args)
        assert rc == 1
        data = json.loads(buf.getvalue())["data"]
        orphans = data["orphan_groups"]
        assert any(o["group_id"] == g.id and o["expected_legs"] == 3
                   and o["actual_legs"] == 1 for o in orphans)

    def test_token_exception_returns_auth_exit_code(self, parser, isolated_state, monkeypatch) -> None:
        """TokenException → exit 5 (AUTH), with a clear stderr message."""
        from kite_algo import kite_tool as kt
        TokenException = type("TokenException", (Exception,), {})
        client = Mock()
        client.orders.side_effect = TokenException("token expired")
        monkeypatch.setattr(kt, "_new_client", lambda: client)

        args = parser.parse_args(["reconcile"])
        rc = cmd_reconcile(args)
        assert rc == 5

    def test_skip_kite(self, parser, isolated_state, monkeypatch) -> None:
        """--skip-kite must NOT call _new_client."""
        from kite_algo import kite_tool as kt
        def boom():
            raise AssertionError("must not call _new_client")
        monkeypatch.setattr(kt, "_new_client", boom)

        args = parser.parse_args(["reconcile", "--skip-kite", "--format", "json"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cmd_reconcile(args)
        assert rc == 0  # nothing local, nothing remote, no orphans → clean
