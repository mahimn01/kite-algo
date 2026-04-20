"""Tests for the idempotency store.

Semantics under test:
- Repeated `record_attempt` with same key is a no-op (INSERT OR IGNORE).
- `record_completion` is idempotent.
- `lookup` returns the stored record; `completed` flag works.
- `purge_older_than` respects incomplete rows.
- `derive_tag_from_key` is deterministic and alphanumeric <=20 chars.
"""

from __future__ import annotations

import json
import time

import pytest

from kite_algo.idempotency import (
    IdempotencyStore,
    WriteRecord,
    derive_tag_from_key,
)


@pytest.fixture
def store(tmp_path):
    return IdempotencyStore(tmp_path / "idem.sqlite")


# -----------------------------------------------------------------------------
# record_attempt + lookup
# -----------------------------------------------------------------------------

class TestRecordAttempt:
    def test_first_insert_returns_true(self, store) -> None:
        assert store.record_attempt(
            key="K1", cmd="place", request={"a": 1}, request_id="R1"
        ) is True

    def test_duplicate_insert_returns_false(self, store) -> None:
        store.record_attempt(key="K1", cmd="place", request={"a": 1})
        assert store.record_attempt(
            key="K1", cmd="place", request={"a": 2}  # different payload
        ) is False
        # The original row is preserved — not overwritten.
        rec = store.lookup("K1")
        parsed = json.loads(rec.request_json)
        assert parsed == {"a": 1}

    def test_lookup_absent(self, store) -> None:
        assert store.lookup("NOPE") is None

    def test_lookup_returns_started_not_completed(self, store) -> None:
        store.record_attempt(key="K1", cmd="place", request={})
        rec = store.lookup("K1")
        assert rec is not None
        assert rec.completed is False
        assert rec.result is None
        assert rec.exit_code is None


# -----------------------------------------------------------------------------
# record_completion
# -----------------------------------------------------------------------------

class TestRecordCompletion:
    def test_marks_completed(self, store) -> None:
        store.record_attempt(key="K1", cmd="place", request={})
        store.record_completion(key="K1", result={"order_id": "X"}, exit_code=0)

        rec = store.lookup("K1")
        assert rec.completed is True
        assert rec.exit_code == 0
        assert rec.result == {"order_id": "X"}

    def test_kite_order_id_captured(self, store) -> None:
        store.record_attempt(key="K1", cmd="place", request={})
        store.record_completion(
            key="K1", result={}, exit_code=0, kite_order_id="260421000123456"
        )
        rec = store.lookup("K1")
        assert rec.kite_order_id == "260421000123456"

    def test_completion_is_idempotent(self, store) -> None:
        """Calling twice with the same result is a no-op; second call's
        timestamp overrides but nothing bad happens.
        """
        store.record_attempt(key="K", cmd="place", request={})
        store.record_completion(key="K", result={"r": 1}, exit_code=0)
        rec1 = store.lookup("K")
        time.sleep(0.005)
        store.record_completion(key="K", result={"r": 1}, exit_code=0)
        rec2 = store.lookup("K")
        assert rec2.result == rec1.result
        assert rec2.completed_at_ms >= rec1.completed_at_ms

    def test_completion_without_attempt_is_noop(self, store) -> None:
        """Writing a completion for an unknown key: UPDATE matches 0 rows,
        no exception, store remains empty for that key."""
        store.record_completion(key="NEW", result={}, exit_code=0)
        assert store.lookup("NEW") is None


# -----------------------------------------------------------------------------
# Cross-process / durability
# -----------------------------------------------------------------------------

class TestDurability:
    def test_two_stores_see_same_data(self, tmp_path) -> None:
        """Two store instances on the same path are consistent (SQLite WAL)."""
        path = tmp_path / "idem.sqlite"
        s1 = IdempotencyStore(path)
        s1.record_attempt(key="K", cmd="place", request={"x": 1})
        s1.record_completion(key="K", result={"r": 42}, exit_code=0)

        s2 = IdempotencyStore(path)
        rec = s2.lookup("K")
        assert rec is not None
        assert rec.result == {"r": 42}


# -----------------------------------------------------------------------------
# Purge
# -----------------------------------------------------------------------------

class TestPurge:
    def test_purges_old_completed(self, store) -> None:
        store.record_attempt(key="OLD", cmd="place", request={})
        store.record_completion(key="OLD", result={}, exit_code=0)
        # Simulate an old row by bumping the cutoff 1h into the future.
        cutoff_ms = int(time.time() * 1000) + 60 * 60 * 1000
        deleted = store.purge_older_than(cutoff_ms)
        assert deleted == 1
        assert store.lookup("OLD") is None

    def test_does_not_purge_incomplete(self, store) -> None:
        """Incomplete rows represent ghost orders; never purge them."""
        store.record_attempt(key="GHOST", cmd="place", request={})
        cutoff_ms = int(time.time() * 1000) + 60 * 60 * 1000
        store.purge_older_than(cutoff_ms)
        assert store.lookup("GHOST") is not None

    def test_future_cutoff_no_delete(self, store) -> None:
        store.record_attempt(key="K", cmd="place", request={})
        store.record_completion(key="K", result={}, exit_code=0)
        deleted = store.purge_older_than(0)  # far past → nothing old enough
        assert deleted == 0


# -----------------------------------------------------------------------------
# derive_tag_from_key
# -----------------------------------------------------------------------------

class TestDeriveTag:
    def test_deterministic(self) -> None:
        assert derive_tag_from_key("same-key") == derive_tag_from_key("same-key")

    def test_length_and_alphanumeric(self) -> None:
        t = derive_tag_from_key("any-key-123")
        assert len(t) <= 20
        assert t.isalnum()
        assert t.startswith("KA")

    def test_different_keys_different_tags(self) -> None:
        t1 = derive_tag_from_key("k1")
        t2 = derive_tag_from_key("k2")
        assert t1 != t2

    def test_collision_resistance_over_large_space(self) -> None:
        """10_000 random keys produce 10_000 unique tags."""
        import secrets
        keys = [secrets.token_hex(8) for _ in range(10_000)]
        tags = {derive_tag_from_key(k) for k in keys}
        assert len(tags) == len(keys)

    def test_custom_prefix(self) -> None:
        t = derive_tag_from_key("x", prefix="T")
        assert t.startswith("T")


# -----------------------------------------------------------------------------
# Threading
# -----------------------------------------------------------------------------

class TestConcurrency:
    def test_many_threads_same_key_only_one_wins(self, store) -> None:
        import threading
        first_inserts = []

        def w():
            inserted = store.record_attempt(
                key="RACE", cmd="place", request={"x": 1}
            )
            first_inserts.append(inserted)

        threads = [threading.Thread(target=w) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Exactly one thread got True; the rest got False.
        assert first_inserts.count(True) == 1
        assert first_inserts.count(False) == 19
