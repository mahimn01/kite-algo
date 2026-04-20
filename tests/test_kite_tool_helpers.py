"""Tests for helper functions in kite_tool.py.

These are the small, correctness-critical utilities that the CLI's happy path
depends on — timestamp parsing, secret redaction, symbol splitting, and the
_emit envelope wrapping.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from datetime import datetime

import pytest

from kite_algo.kite_tool import _emit, _parse_order_timestamp, _resolve_format, _split_symbols


# -----------------------------------------------------------------------------
# _parse_order_timestamp
# -----------------------------------------------------------------------------

class TestParseOrderTimestamp:
    def test_parses_kite_standard_format(self) -> None:
        # Kite's usual wire format: "YYYY-MM-DD HH:MM:SS"
        dt = _parse_order_timestamp("2026-04-19 15:30:45")
        assert dt.year == 2026
        assert dt.hour == 15

    def test_parses_iso_with_offset(self) -> None:
        dt = _parse_order_timestamp("2026-04-19T15:30:45+05:30")
        assert dt.year == 2026
        assert dt.hour == 15

    def test_parses_iso_space_sep(self) -> None:
        dt = _parse_order_timestamp("2026-04-19 15:30:45.123456")
        assert dt.microsecond == 123456

    def test_accepts_datetime_passthrough(self) -> None:
        src = datetime(2026, 4, 19, 15, 30)
        assert _parse_order_timestamp(src) is src

    def test_malformed_returns_min(self) -> None:
        assert _parse_order_timestamp("not a timestamp") == datetime.min
        assert _parse_order_timestamp(None) == datetime.min
        assert _parse_order_timestamp("") == datetime.min

    def test_sorts_correctly_across_single_digit_hour(self) -> None:
        """The original bug: sorting by string put '9:00' AFTER '10:00' because
        '1' < '9'. Parsed datetimes order correctly.
        """
        rows = [
            {"order_timestamp": "2026-04-19 10:00:00", "status": "LATER"},
            {"order_timestamp": "2026-04-19 9:00:00", "status": "EARLIER"},
        ]
        rows.sort(key=lambda h: _parse_order_timestamp(h["order_timestamp"]))
        assert rows[0]["status"] == "EARLIER"
        assert rows[-1]["status"] == "LATER"

    def test_stable_microsecond_sort(self) -> None:
        """Same-second events with differing microseconds sort correctly."""
        rows = [
            {"order_timestamp": "2026-04-19 15:30:45.000001", "n": 1},
            {"order_timestamp": "2026-04-19 15:30:45.000002", "n": 2},
            {"order_timestamp": "2026-04-19 15:30:45.000003", "n": 3},
        ]
        rows.sort(
            key=lambda h: _parse_order_timestamp(h["order_timestamp"]),
            reverse=True,
        )
        assert [r["n"] for r in rows] == [3, 2, 1]


# -----------------------------------------------------------------------------
# _split_symbols
# -----------------------------------------------------------------------------

class TestSplitSymbols:
    def test_basic(self) -> None:
        assert _split_symbols("NSE:RELIANCE,NSE:INFY") == ["NSE:RELIANCE", "NSE:INFY"]

    def test_trims_whitespace(self) -> None:
        assert _split_symbols(" NSE:X , NSE:Y ") == ["NSE:X", "NSE:Y"]

    def test_drops_empty(self) -> None:
        assert _split_symbols("NSE:X,,NSE:Y,") == ["NSE:X", "NSE:Y"]


# -----------------------------------------------------------------------------
# _emit — envelope wrapping + format resolution
# -----------------------------------------------------------------------------

class TestEmitEnvelope:
    def test_json_with_cmd_wraps_in_envelope(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            _emit([{"symbol": "RELIANCE", "last": 1340.5}], "json", cmd="ltp")
        parsed = json.loads(buf.getvalue())
        assert parsed["ok"] is True
        assert parsed["cmd"] == "ltp"
        assert parsed["data"] == [{"symbol": "RELIANCE", "last": 1340.5}]
        assert "request_id" in parsed
        assert "schema_version" in parsed
        assert "elapsed_ms" in parsed["meta"]

    def test_json_without_cmd_emits_raw_data(self) -> None:
        """Backwards-compat: any `_emit(..., 'json')` without cmd emits bare."""
        buf = io.StringIO()
        with redirect_stdout(buf):
            _emit([{"x": 1}], "json")
        parsed = json.loads(buf.getvalue())
        assert parsed == [{"x": 1}]

    def test_kite_no_envelope_disables_wrapping(self, monkeypatch) -> None:
        monkeypatch.setenv("KITE_NO_ENVELOPE", "1")
        buf = io.StringIO()
        with redirect_stdout(buf):
            _emit([{"x": 1}], "json", cmd="ltp")
        parsed = json.loads(buf.getvalue())
        # Raw data — no ok/cmd/request_id.
        assert parsed == [{"x": 1}]

    def test_csv_format_unaffected(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            _emit([{"a": 1, "b": 2}], "csv", cmd="cmd")
        out = buf.getvalue()
        # CSV rows are raw; envelope doesn't apply.
        assert "a,b" in out
        assert "1,2" in out

    def test_table_format_unaffected(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            _emit([{"a": 1, "b": 2}], "table", cmd="cmd")
        out = buf.getvalue()
        assert "a" in out and "b" in out
        assert "1" in out and "2" in out
        # Not wrapped.
        assert "request_id" not in out

    def test_dict_data_wrapped(self) -> None:
        """Single-object (non-list) data is wrapped as-is in envelope.data."""
        buf = io.StringIO()
        with redirect_stdout(buf):
            _emit({"user_id": "AB1234"}, "json", cmd="profile")
        parsed = json.loads(buf.getvalue())
        assert parsed["data"] == {"user_id": "AB1234"}


class TestResolveFormat:
    def test_explicit_passthrough(self) -> None:
        assert _resolve_format("json") == "json"
        assert _resolve_format("csv") == "csv"
        assert _resolve_format("table") == "table"

    def test_auto_in_non_tty(self, monkeypatch) -> None:
        """Under pytest stdout is not a TTY → auto == json."""
        monkeypatch.delenv("KITE_JSON", raising=False)
        # Pytest captures stdout so it's definitely not a TTY.
        assert _resolve_format("auto") == "json"

    def test_kite_json_forces_json(self, monkeypatch) -> None:
        monkeypatch.setenv("KITE_JSON", "1")
        assert _resolve_format("auto") == "json"
