"""Tests for the NDJSON audit log."""

from __future__ import annotations

import json
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from kite_algo.audit import (
    AuditEntry,
    audit_path_for,
    iter_entries,
    log_command,
    purge_older_than,
    tail,
    write_entry,
)


@pytest.fixture
def audit_root(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "audit"
    monkeypatch.setenv("KITE_AUDIT_DIR", str(root))
    return root


# -----------------------------------------------------------------------------
# Basic write + read
# -----------------------------------------------------------------------------

class TestWriteRead:
    def test_writes_one_line_per_entry(self, audit_root) -> None:
        log_command(cmd="place", request_id="R1", args={"a": 1}, exit_code=0, root=audit_root)
        log_command(cmd="place", request_id="R2", args={"a": 2}, exit_code=0, root=audit_root)
        files = list(audit_root.glob("*.jsonl"))
        assert len(files) == 1
        lines = files[0].read_text().strip().split("\n")
        assert len(lines) == 2

    def test_each_line_is_valid_json(self, audit_root) -> None:
        log_command(cmd="place", request_id="R1", args={"a": 1}, root=audit_root)
        files = list(audit_root.glob("*.jsonl"))
        entry = json.loads(files[0].read_text().strip())
        assert entry["cmd"] == "place"
        assert entry["request_id"] == "R1"
        assert entry["args"] == {"a": 1}

    def test_filename_is_today_ist(self, audit_root) -> None:
        log_command(cmd="place", request_id="R", args={}, root=audit_root)
        from datetime import date
        from kite_algo.market_rules import IST
        today_ist = datetime.now(tz=IST).date()
        expected = audit_root / f"{today_ist.isoformat()}.jsonl"
        assert expected.exists()

    def test_has_ts_and_epoch_ms(self, audit_root) -> None:
        log_command(cmd="x", request_id="R", args={}, root=audit_root)
        entry = json.loads(next(audit_root.glob("*.jsonl")).read_text().strip())
        assert "ts" in entry
        assert "ts_epoch_ms" in entry
        assert entry["ts_epoch_ms"] > 0


# -----------------------------------------------------------------------------
# Secret redaction in args
# -----------------------------------------------------------------------------

class TestRedaction:
    def test_redacts_long_token_in_args(self, audit_root, monkeypatch) -> None:
        """An agent that accidentally passes an access_token in a payload
        must not have it persisted to the audit log."""
        monkeypatch.setenv("KITE_ACCESS_TOKEN", "TokenValueMustBeRedactedABCD123")
        log_command(
            cmd="some",
            request_id="R",
            args={"note": "see token TokenValueMustBeRedactedABCD123"},
            root=audit_root,
        )
        content = next(audit_root.glob("*.jsonl")).read_text()
        assert "TokenValueMustBeRedactedABCD123" not in content

    def test_redacts_nested_dict(self, audit_root, monkeypatch) -> None:
        monkeypatch.setenv("KITE_API_SECRET", "SecretAbcdef1234567890XYZ")
        log_command(
            cmd="x",
            request_id="R",
            args={"nested": {"k": "includes SecretAbcdef1234567890XYZ"}},
            root=audit_root,
        )
        content = next(audit_root.glob("*.jsonl")).read_text()
        assert "SecretAbcdef1234567890XYZ" not in content


# -----------------------------------------------------------------------------
# Concurrent appends (atomic under POSIX)
# -----------------------------------------------------------------------------

class TestConcurrency:
    def test_concurrent_appends_no_interleave(self, audit_root) -> None:
        """20 threads writing 50 lines each = 1000 well-formed lines."""
        def w(i: int) -> None:
            for j in range(50):
                log_command(
                    cmd="place", request_id=f"T{i}_J{j}",
                    args={"i": i, "j": j},
                    root=audit_root,
                )

        threads = [threading.Thread(target=w, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        files = list(audit_root.glob("*.jsonl"))
        assert len(files) == 1
        lines = files[0].read_text().strip().split("\n")
        assert len(lines) == 1000
        # Every line parses.
        for line in lines:
            json.loads(line)


# -----------------------------------------------------------------------------
# iter_entries filtering
# -----------------------------------------------------------------------------

class TestIterEntries:
    def test_iter_all(self, audit_root) -> None:
        for i in range(5):
            log_command(cmd="place", request_id=f"R{i}", args={"n": i}, exit_code=0, root=audit_root)
        got = list(iter_entries(root=audit_root))
        assert len(got) == 5

    def test_filter_by_cmd(self, audit_root) -> None:
        log_command(cmd="place", request_id="R1", args={}, exit_code=0, root=audit_root)
        log_command(cmd="cancel", request_id="R2", args={}, exit_code=0, root=audit_root)
        log_command(cmd="place", request_id="R3", args={}, exit_code=0, root=audit_root)
        got = list(iter_entries(cmd="place", root=audit_root))
        assert len(got) == 2

    def test_filter_outcome_ok(self, audit_root) -> None:
        log_command(cmd="x", request_id="R1", args={}, exit_code=0, root=audit_root)
        log_command(cmd="x", request_id="R2", args={}, exit_code=4, root=audit_root)
        got = list(iter_entries(outcome="ok", root=audit_root))
        assert len(got) == 1
        assert got[0]["request_id"] == "R1"

    def test_filter_outcome_error(self, audit_root) -> None:
        log_command(cmd="x", request_id="R1", args={}, exit_code=0, root=audit_root)
        log_command(cmd="x", request_id="R2", args={}, exit_code=4, root=audit_root)
        log_command(cmd="x", request_id="R3", args={}, exit_code=5, root=audit_root)
        got = list(iter_entries(outcome="error", root=audit_root))
        ids = [e["request_id"] for e in got]
        assert set(ids) == {"R2", "R3"}

    def test_date_range_filter(self, audit_root) -> None:
        """Files outside the range are skipped even without opening them."""
        # Create two files with fake dates.
        old = audit_root / "2025-01-01.jsonl"
        new = audit_root / "2026-04-20.jsonl"
        old.parent.mkdir(parents=True, exist_ok=True)
        old.write_text(json.dumps({
            "cmd": "x", "request_id": "OLD", "exit_code": 0,
            "ts_epoch_ms": 0, "ts": "", "args": {},
        }) + "\n")
        new.write_text(json.dumps({
            "cmd": "x", "request_id": "NEW", "exit_code": 0,
            "ts_epoch_ms": 0, "ts": "", "args": {},
        }) + "\n")
        got = list(iter_entries(
            since=date(2026, 1, 1), until=date(2026, 12, 31),
            root=audit_root,
        ))
        ids = [e["request_id"] for e in got]
        assert ids == ["NEW"]

    def test_skips_malformed_lines(self, audit_root) -> None:
        """A truncated line doesn't halt iteration."""
        f = audit_root / "2026-04-21.jsonl"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(
            '{"cmd":"ok","request_id":"A","exit_code":0,"ts_epoch_ms":1,"ts":"","args":{}}\n'
            'this is not json\n'
            '{"cmd":"ok","request_id":"B","exit_code":0,"ts_epoch_ms":2,"ts":"","args":{}}\n'
        )
        got = list(iter_entries(root=audit_root))
        assert [e["request_id"] for e in got] == ["A", "B"]

    def test_iter_on_empty_root(self, tmp_path) -> None:
        """Non-existent audit dir yields nothing, no crash."""
        got = list(iter_entries(root=tmp_path / "does-not-exist"))
        assert got == []


# -----------------------------------------------------------------------------
# tail
# -----------------------------------------------------------------------------

class TestTail:
    def test_tail_returns_last_n(self, audit_root) -> None:
        for i in range(10):
            log_command(cmd="x", request_id=f"R{i}", args={}, exit_code=0, root=audit_root)
        got = tail(3, root=audit_root)
        ids = [e["request_id"] for e in got]
        assert ids == ["R7", "R8", "R9"]

    def test_tail_fewer_than_n(self, audit_root) -> None:
        log_command(cmd="x", request_id="R1", args={}, exit_code=0, root=audit_root)
        got = tail(100, root=audit_root)
        assert len(got) == 1


# -----------------------------------------------------------------------------
# File permissions
# -----------------------------------------------------------------------------

class TestPermissions:
    def test_file_mode_0o600(self, audit_root) -> None:
        import os
        log_command(cmd="x", request_id="R", args={}, root=audit_root)
        f = next(audit_root.glob("*.jsonl"))
        if os.name == "posix":
            mode = f.stat().st_mode & 0o777
            assert mode == 0o600, f"audit file mode is {oct(mode)}"


# -----------------------------------------------------------------------------
# Retention / purge
# -----------------------------------------------------------------------------

class TestPurge:
    def test_purges_old(self, audit_root) -> None:
        # Create a fake-old file.
        old = audit_root / "2020-01-01.jsonl"
        audit_root.mkdir(parents=True, exist_ok=True)
        old.write_text("")
        deleted = purge_older_than(days=365, root=audit_root)
        assert deleted == 1
        assert not old.exists()

    def test_does_not_purge_recent(self, audit_root) -> None:
        log_command(cmd="x", request_id="R", args={}, root=audit_root)
        deleted = purge_older_than(days=30, root=audit_root)
        assert deleted == 0

    def test_ignores_non_date_filenames(self, audit_root) -> None:
        """Files with non-ISO filenames are safely ignored."""
        audit_root.mkdir(parents=True, exist_ok=True)
        (audit_root / "README.md").write_text("hi")
        deleted = purge_older_than(days=0, root=audit_root)
        # No exception; README.md left alone (wasn't a *.jsonl anyway).
        assert deleted == 0
        assert (audit_root / "README.md").exists()
