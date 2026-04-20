"""Tests for `events` — audit log tail command."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

import pytest

from kite_algo.audit import log_command
from kite_algo.kite_tool import build_parser, cmd_events


@pytest.fixture
def parser():
    return build_parser()


@pytest.fixture
def audit_with_entries(tmp_path, monkeypatch):
    """Seed an audit root with a handful of mixed-outcome entries."""
    root = tmp_path / "audit"
    monkeypatch.setenv("KITE_AUDIT_DIR", str(root))
    log_command(cmd="place", request_id="R1", args={}, exit_code=0, root=root)
    log_command(cmd="place", request_id="R2", args={}, exit_code=4, root=root)
    log_command(cmd="cancel", request_id="R3", args={}, exit_code=0, root=root)
    log_command(cmd="modify", request_id="R4", args={}, exit_code=5, root=root)
    return root


class TestEventsFiltering:
    def test_no_filter_returns_all(self, parser, audit_with_entries) -> None:
        args = parser.parse_args(["events", "--format", "json"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cmd_events(args)
        assert rc == 0
        entries = json.loads(buf.getvalue())["data"]
        assert {e["request_id"] for e in entries} == {"R1", "R2", "R3", "R4"}

    def test_cmd_filter(self, parser, audit_with_entries) -> None:
        args = parser.parse_args([
            "events", "--cmd-filter", "place", "--format", "json",
        ])
        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_events(args)
        entries = json.loads(buf.getvalue())["data"]
        ids = {e["request_id"] for e in entries}
        assert ids == {"R1", "R2"}

    def test_outcome_ok(self, parser, audit_with_entries) -> None:
        args = parser.parse_args(["events", "--outcome", "ok", "--format", "json"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_events(args)
        ids = {e["request_id"] for e in json.loads(buf.getvalue())["data"]}
        assert ids == {"R1", "R3"}

    def test_outcome_error(self, parser, audit_with_entries) -> None:
        args = parser.parse_args(["events", "--outcome", "error", "--format", "json"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_events(args)
        ids = {e["request_id"] for e in json.loads(buf.getvalue())["data"]}
        assert ids == {"R2", "R4"}

    def test_tail(self, parser, audit_with_entries) -> None:
        args = parser.parse_args(["events", "--tail", "2", "--format", "json"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_events(args)
        entries = json.loads(buf.getvalue())["data"]
        assert len(entries) == 2

    def test_tail_with_cmd_filter(self, parser, audit_with_entries) -> None:
        args = parser.parse_args([
            "events", "--tail", "10", "--cmd-filter", "place", "--format", "json",
        ])
        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_events(args)
        entries = json.loads(buf.getvalue())["data"]
        assert all(e["cmd"] == "place" for e in entries)
        assert len(entries) == 2

    def test_since_rejects_bad_date(self, parser, audit_with_entries) -> None:
        args = parser.parse_args([
            "events", "--since", "not-a-date", "--format", "json",
        ])
        with pytest.raises(SystemExit):
            cmd_events(args)

    def test_empty_audit_yields_empty_list(self, parser, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("KITE_AUDIT_DIR", str(tmp_path / "empty"))
        args = parser.parse_args(["events", "--format", "json"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_events(args)
        assert json.loads(buf.getvalue())["data"] == []
