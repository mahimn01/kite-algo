"""Tests for group-start/status/cancel + --group-id wiring on place."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock

import pytest

from kite_algo.groups import GroupStore


@pytest.fixture
def store(tmp_path) -> GroupStore:
    return GroupStore(tmp_path / "idem.sqlite")


# -----------------------------------------------------------------------------
# GroupStore unit
# -----------------------------------------------------------------------------

class TestGroupStore:
    def test_start_and_get(self, store) -> None:
        g = store.start(name="BEAR_PUT", expected_legs=2)
        got = store.get(g.id)
        assert got is not None
        assert got.id == g.id
        assert got.name == "BEAR_PUT"
        assert got.expected_legs == 2
        assert got.closed_at_ms is None

    def test_group_ids_are_ulid_sortable(self, store) -> None:
        import time
        g1 = store.start(name="A")
        time.sleep(0.002)
        g2 = store.start(name="B")
        assert g1.id < g2.id

    def test_get_missing_returns_none(self, store) -> None:
        assert store.get("NOSUCHGROUP") is None

    def test_add_member_and_list(self, store) -> None:
        g = store.start(name="SPREAD")
        assert store.add_member(
            group_id=g.id, order_id="ORD_1", leg_name="long_put", tag="KAAAA",
        ) is True
        assert store.add_member(
            group_id=g.id, order_id="ORD_2", leg_name="short_put", tag="KABBB",
        ) is True
        members = store.members(g.id)
        assert [m.order_id for m in members] == ["ORD_1", "ORD_2"]
        assert members[0].leg_name == "long_put"

    def test_duplicate_member_ignored(self, store) -> None:
        g = store.start(name="X")
        assert store.add_member(group_id=g.id, order_id="ORD_1") is True
        # Second attempt with same order_id: INSERT OR IGNORE returns 0 rows.
        assert store.add_member(group_id=g.id, order_id="ORD_1") is False
        assert len(store.members(g.id)) == 1

    def test_close_marks_group(self, store) -> None:
        g = store.start(name="X")
        assert store.close(g.id) is True
        assert store.get(g.id).closed_at_ms is not None

    def test_close_nonexistent_returns_false(self, store) -> None:
        assert store.close("NOPE") is False

    def test_list_active_excludes_closed(self, store) -> None:
        g1 = store.start(name="A")
        g2 = store.start(name="B")
        store.close(g1.id)
        active = store.list_active()
        assert {g.id for g in active} == {g2.id}


# -----------------------------------------------------------------------------
# cmd_group_start / status / cancel
# -----------------------------------------------------------------------------

@pytest.fixture
def parser():
    from kite_algo.kite_tool import build_parser
    return build_parser()


@pytest.fixture
def fresh_store(tmp_path, monkeypatch):
    """Point GroupStore at a fresh temp SQLite so tests don't bleed into each
    other or the real data/idempotency.sqlite file.
    """
    path = tmp_path / "idem.sqlite"
    from kite_algo import groups as g

    def _factory(p=None):
        return GroupStore(path)
    monkeypatch.setattr(g, "GroupStore", _factory)
    from kite_algo import kite_tool as kt
    monkeypatch.setattr(kt, "GroupStore", _factory, raising=False)
    return path


class TestGroupCLI:
    def test_group_start_emits_id(self, parser, fresh_store) -> None:
        from kite_algo.kite_tool import cmd_group_start
        args = parser.parse_args([
            "group-start", "--name", "BEAR_PUT", "--legs", "2", "--format", "json",
        ])
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cmd_group_start(args)
        assert rc == 0
        data = json.loads(buf.getvalue())["data"]
        assert len(data["group_id"]) == 26
        assert data["name"] == "BEAR_PUT"
        assert data["expected_legs"] == 2

    def test_group_status_missing_returns_1(self, parser, fresh_store) -> None:
        from kite_algo.kite_tool import cmd_group_status
        args = parser.parse_args([
            "group-status", "--group-id", "NOSUCH", "--skip-kite",
        ])
        assert cmd_group_status(args) == 1

    def test_group_status_with_legs(self, parser, fresh_store, monkeypatch) -> None:
        # First create a group + two legs.
        from kite_algo.kite_tool import cmd_group_start, cmd_group_status

        args1 = parser.parse_args([
            "group-start", "--name", "SPREAD", "--legs", "2", "--format", "json",
        ])
        buf1 = io.StringIO()
        with redirect_stdout(buf1):
            cmd_group_start(args1)
        gid = json.loads(buf1.getvalue())["data"]["group_id"]

        # Add two members directly via the store.
        from kite_algo.groups import GroupStore
        store = GroupStore(fresh_store)
        store.add_member(group_id=gid, order_id="ORD_A", leg_name="long")
        store.add_member(group_id=gid, order_id="ORD_B", leg_name="short")

        # Skip live Kite lookup.
        args2 = parser.parse_args([
            "group-status", "--group-id", gid, "--skip-kite", "--format", "json",
        ])
        buf2 = io.StringIO()
        with redirect_stdout(buf2):
            cmd_group_status(args2)
        data = json.loads(buf2.getvalue())["data"]
        assert data["actual_legs"] == 2
        assert {leg["order_id"] for leg in data["legs"]} == {"ORD_A", "ORD_B"}

    def test_group_cancel_requires_yes(self, parser, fresh_store) -> None:
        from kite_algo.kite_tool import cmd_group_cancel
        args = parser.parse_args([
            "group-cancel", "--group-id", "X",
        ])
        with pytest.raises(SystemExit, match="Refusing"):
            cmd_group_cancel(args)


class TestPlaceGroupWiring:
    """`place --group-id G` registers the resulting order as a member."""

    def test_place_attaches_to_group(self, parser, fresh_store, monkeypatch) -> None:
        from kite_algo import kite_tool as kt
        from kite_algo.kite_tool import cmd_group_start

        # Create a group first.
        g_args = parser.parse_args([
            "group-start", "--name", "X", "--format", "json",
        ])
        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_group_start(g_args)
        gid = json.loads(buf.getvalue())["data"]["group_id"]

        # Stub the broker layer so no real API call happens.
        class FakePlacer:
            def __init__(self, *a, **kw): pass
            def place(self, **kwargs):
                return "ORD_REAL_123"

        monkeypatch.setattr(kt, "_new_client", lambda: object())
        monkeypatch.setattr(kt, "IdempotentOrderPlacer", FakePlacer)

        args = parser.parse_args([
            "place", "--exchange", "NSE", "--tradingsymbol", "RELIANCE",
            "--transaction-type", "BUY", "--order-type", "LIMIT",
            "--quantity", "1", "--product", "CNC", "--price", "1340",
            "--group-id", gid, "--leg-name", "leg1",
            "--yes", "--skip-market-rules",
        ])
        buf2 = io.StringIO()
        with redirect_stdout(buf2):
            rc = kt.cmd_place(args)
        assert rc == 0

        # The order should now be a member of the group.
        from kite_algo.groups import GroupStore
        members = GroupStore(fresh_store).members(gid)
        assert len(members) == 1
        assert members[0].order_id == "ORD_REAL_123"
        assert members[0].leg_name == "leg1"
