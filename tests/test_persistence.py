"""Tests for the SQLite audit store."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from kite_algo.persistence import SqliteStore


@pytest.fixture
def store(tmp_path) -> SqliteStore:
    return SqliteStore(tmp_path / "trading.sqlite")


# -----------------------------------------------------------------------------
# Runs
# -----------------------------------------------------------------------------

class TestRuns:
    def test_start_returns_id(self, store) -> None:
        rid = store.start_run(cfg={"dry_run": True}, strategy="s1")
        assert isinstance(rid, int) and rid > 0

    def test_end_run_sets_timestamp(self, store) -> None:
        rid = store.start_run(cfg={})
        store.end_run(rid)
        runs = store.list_runs()
        r = next(x for x in runs if x["id"] == rid)
        assert r["ended_at_ms"] is not None

    def test_list_runs_desc(self, store) -> None:
        ids = [store.start_run(cfg={}) for _ in range(3)]
        got = [r["id"] for r in store.list_runs()]
        assert got == list(reversed(ids))

    def test_strategy_fields_persisted(self, store) -> None:
        rid = store.start_run(
            cfg={}, strategy="MeanReverter", strategy_id="S1",
            agent_id="claude-turn-42", parent_request_id="PARENT_X",
        )
        runs = store.list_runs()
        r = next(x for x in runs if x["id"] == rid)
        assert r["strategy"] == "MeanReverter"
        assert r["strategy_id"] == "S1"
        assert r["agent_id"] == "claude-turn-42"


# -----------------------------------------------------------------------------
# Decisions
# -----------------------------------------------------------------------------

class TestDecisions:
    def test_log_decision(self, store) -> None:
        rid = store.start_run(cfg={})
        did = store.log_decision(
            rid, strategy="s", intent={"symbol": "RELIANCE", "qty": 1},
            accepted=True, reason=None,
        )
        assert did > 0

        rows = store.query("SELECT * FROM decisions WHERE id=?", (did,))
        assert rows[0]["accepted"] == 1
        intent = json.loads(rows[0]["intent_json"])
        assert intent["symbol"] == "RELIANCE"

    def test_rejected_decision_with_reason(self, store) -> None:
        rid = store.start_run(cfg={})
        store.log_decision(rid, strategy="s", intent={}, accepted=False,
                          reason="risk: max_order_quantity")
        rows = store.query("SELECT * FROM decisions WHERE accepted=0")
        assert rows[0]["reason"] == "risk: max_order_quantity"


# -----------------------------------------------------------------------------
# Orders
# -----------------------------------------------------------------------------

class TestOrders:
    def test_log_order_flattens_request(self, store) -> None:
        rid = store.start_run(cfg={})
        store.log_order(
            rid, broker="kite", order_id="ORD_1",
            request={
                "exchange": "NSE", "tradingsymbol": "RELIANCE",
                "transaction_type": "BUY", "order_type": "LIMIT",
                "product": "CNC", "variety": "regular",
                "quantity": 10, "price": 1340,
                "tag": "KA01",
            },
            status="OPEN",
            tag="KA01",
            group_id="G1", leg_name="leg1",
        )
        row = store.get_order("ORD_1")
        assert row is not None
        assert row["exchange"] == "NSE"
        assert row["tradingsymbol"] == "RELIANCE"
        assert row["price"] == 1340
        assert row["quantity"] == 10
        assert row["group_id"] == "G1"
        assert row["leg_name"] == "leg1"

    def test_update_order_status(self, store) -> None:
        rid = store.start_run(cfg={})
        store.log_order(rid, broker="kite", order_id="X", request={}, status="OPEN")
        store.update_order_status("X", "COMPLETE")
        assert store.get_order("X")["status"] == "COMPLETE"

    def test_list_non_terminal(self, store) -> None:
        rid = store.start_run(cfg={})
        store.log_order(rid, broker="kite", order_id="A", request={}, status="OPEN")
        store.log_order(rid, broker="kite", order_id="B", request={}, status="COMPLETE")
        store.log_order(rid, broker="kite", order_id="C", request={}, status="CANCELLED")
        store.log_order(rid, broker="kite", order_id="D", request={}, status=None)
        non_terminal = store.list_non_terminal_order_ids()
        assert set(non_terminal) == {"A", "D"}


# -----------------------------------------------------------------------------
# Order status events
# -----------------------------------------------------------------------------

class TestStatusEvents:
    def test_log_status_event(self, store) -> None:
        rid = store.start_run(cfg={})
        store.log_order_status_event(rid, "kite", {
            "order_id": "X", "status": "OPEN", "filled_quantity": 0,
            "pending_quantity": 10, "average_price": 0,
        })
        latest = store.get_latest_status("X")
        assert latest["status"] == "OPEN"
        assert latest["filled"] == 0
        assert latest["remaining"] == 10

    def test_latest_status_picks_newest(self, store) -> None:
        rid = store.start_run(cfg={})
        store.log_order_status_event(rid, "kite", {"order_id": "X", "status": "OPEN"})
        time.sleep(0.005)
        store.log_order_status_event(rid, "kite", {
            "order_id": "X", "status": "COMPLETE",
            "filled_quantity": 10, "average_price": 1340,
        })
        latest = store.get_latest_status("X")
        assert latest["status"] == "COMPLETE"


# -----------------------------------------------------------------------------
# Errors + actions
# -----------------------------------------------------------------------------

class TestErrorsActions:
    def test_log_error(self, store) -> None:
        rid = store.start_run(cfg={})
        store.log_error(rid, where="cmd_place", message="InputException",
                        error_code="HARD_REJECT", request_id="R")
        rows = store.query("SELECT * FROM errors WHERE run_id=?", (rid,))
        assert rows[0]["where_text"] == "cmd_place"
        assert rows[0]["error_code"] == "HARD_REJECT"

    def test_log_action(self, store) -> None:
        rid = store.start_run(cfg={})
        store.log_action(rid, actor="operator",
                         payload={"type": "halt", "reason": "test"},
                         accepted=True)
        rows = store.query("SELECT * FROM actions")
        assert rows[0]["actor"] == "operator"
        assert rows[0]["accepted"] == 1


# -----------------------------------------------------------------------------
# Concurrency
# -----------------------------------------------------------------------------

class TestConcurrency:
    def test_concurrent_writes(self, store) -> None:
        rid = store.start_run(cfg={})

        def w(i: int) -> None:
            for j in range(10):
                store.log_decision(
                    rid, strategy=f"t{i}", intent={"j": j},
                    accepted=True,
                )

        threads = [threading.Thread(target=w, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        count = store.query("SELECT COUNT(*) AS n FROM decisions WHERE run_id=?", (rid,))
        assert count[0]["n"] == 80

    def test_second_instance_sees_data(self, tmp_path) -> None:
        path = tmp_path / "trading.sqlite"
        s1 = SqliteStore(path)
        rid = s1.start_run(cfg={})
        s1.log_order(rid, broker="kite", order_id="X", request={}, status="OPEN")

        s2 = SqliteStore(path)
        row = s2.get_order("X")
        assert row is not None
        assert row["status"] == "OPEN"


# -----------------------------------------------------------------------------
# Env-driven path
# -----------------------------------------------------------------------------

class TestEnvPath:
    def test_respects_trading_db_path(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("TRADING_DB_PATH", str(tmp_path / "custom.sqlite"))
        s = SqliteStore()  # no path arg
        assert s._path == tmp_path / "custom.sqlite"  # type: ignore[attr-defined]
