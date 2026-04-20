"""Tests for `status` and `time` commands."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

import pytest

from kite_algo.kite_tool import build_parser, cmd_status, cmd_time


@pytest.fixture
def parser():
    return build_parser()


# -----------------------------------------------------------------------------
# status
# -----------------------------------------------------------------------------

class TestStatus:
    def test_emits_envelope_with_all_sections(self, parser, monkeypatch) -> None:
        from kite_algo.envelope import ENV_NO_ENVELOPE
        monkeypatch.delenv(ENV_NO_ENVELOPE, raising=False)

        args = parser.parse_args(["status", "--skip-account", "--format", "json"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cmd_status(args)
        assert rc == 0
        parsed = json.loads(buf.getvalue())
        data = parsed["data"]
        for section in ("session", "market", "rate_limit", "account", "live_window", "halt"):
            assert section in data

    def test_halt_reflected(self, parser, tmp_path, monkeypatch) -> None:
        """If the HALTED sentinel exists, status.halt.is_halted is True."""
        halt_file = tmp_path / "HALTED"
        monkeypatch.setenv("KITE_HALT_PATH", str(halt_file))
        from kite_algo.halt import write_halt
        write_halt(reason="test", by="agent-X")

        args = parser.parse_args(["status", "--skip-account", "--format", "json"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_status(args)
        parsed = json.loads(buf.getvalue())
        halt = parsed["data"]["halt"]
        assert halt["is_halted"] is True
        assert halt["reason"] == "test"
        assert halt["by"] == "agent-X"

    def test_skip_account_avoids_broker_call(self, parser, monkeypatch) -> None:
        """--skip-account means _new_client is NEVER called."""
        from kite_algo import kite_tool as kt
        called = {"n": 0}
        monkeypatch.setattr(
            kt, "_new_client",
            lambda: (_ for _ in ()).throw(AssertionError("must not call")),
        )

        args = parser.parse_args(["status", "--skip-account", "--format", "json"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = kt.cmd_status(args)
        assert rc == 0

    def test_market_hours_per_exchange(self, parser) -> None:
        args = parser.parse_args(["status", "--skip-account", "--format", "json"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_status(args)
        parsed = json.loads(buf.getvalue())
        market = parsed["data"]["market"]
        for key in ("nse_open", "bse_open", "nfo_open", "bfo_open",
                    "mcx_open", "cds_open", "bcd_open"):
            assert key in market
            assert isinstance(market[key], bool)

    def test_rate_limit_state_exposed(self, parser) -> None:
        args = parser.parse_args(["status", "--skip-account", "--format", "json"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_status(args)
        rate = json.loads(buf.getvalue())["data"]["rate_limit"]
        for k in (
            "general_tokens_remaining", "historical_tokens_remaining",
            "quote_tokens_remaining", "orders_sec_tokens_remaining",
            "orders_per_min_used", "orders_per_min_cap",
            "orders_per_day_used", "orders_per_day_cap",
        ):
            assert k in rate
        # Caps are the values we committed to in Wave 1.
        assert rate["orders_per_min_cap"] == 200
        assert rate["orders_per_day_cap"] == 3000


# -----------------------------------------------------------------------------
# time
# -----------------------------------------------------------------------------

class TestTime:
    def test_emits_expected_shape(self, parser) -> None:
        args = parser.parse_args(["time", "--format", "json"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cmd_time(args)
        assert rc == 0
        data = json.loads(buf.getvalue())["data"]
        for k in ("ist_now", "utc_now", "ist_date", "weekday",
                  "token_rotation", "market_hours_ist",
                  "mis_squareoff_ist", "next_weekly_expiry"):
            assert k in data

    def test_token_rotation_window_string(self, parser) -> None:
        args = parser.parse_args(["time", "--format", "json"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_time(args)
        rot = json.loads(buf.getvalue())["data"]["token_rotation"]
        assert rot["window_ist"] == "06:45 - 07:30"
        assert "in_window_now" in rot
        assert "next_safe_login" in rot

    def test_mis_cutoffs(self, parser) -> None:
        args = parser.parse_args(["time", "--format", "json"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_time(args)
        mis = json.loads(buf.getvalue())["data"]["mis_squareoff_ist"]
        assert mis["equity"] == "15:20:00"
        assert mis["mcx"] == "23:25:00"

    def test_time_makes_no_api_calls(self, parser, monkeypatch) -> None:
        """time must be pure local — no _new_client."""
        from kite_algo import kite_tool as kt
        monkeypatch.setattr(
            kt, "_new_client",
            lambda: (_ for _ in ()).throw(AssertionError("must not call")),
        )
        args = parser.parse_args(["time", "--format", "json"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = kt.cmd_time(args)
        assert rc == 0

    def test_next_weekly_expiry_nse_and_bse(self, parser) -> None:
        args = parser.parse_args(["time", "--format", "json"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_time(args)
        exp = json.loads(buf.getvalue())["data"]["next_weekly_expiry"]
        assert "nse" in exp and exp["nse"]
        assert "bse" in exp and exp["bse"]
