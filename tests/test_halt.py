"""Tests for the halt / resume kill-switch.

Coverage:
- write_halt creates a sentinel; read_halt reflects it.
- clear_halt removes it; subsequent read returns None.
- Expiry auto-clears on the next read.
- Malformed sentinel fails closed (stays halted).
- parse_duration accepts canonical suffix forms and bare seconds.
- CLI integration: write commands refuse while halted; resume requires
  --confirm-resume (not --yes).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from kite_algo.halt import (
    DEFAULT_PATH,
    HaltActive,
    HaltState,
    assert_not_halted,
    clear_halt,
    halt_path,
    is_halted,
    parse_duration,
    read_halt,
    write_halt,
)


@pytest.fixture
def halt_file(tmp_path, monkeypatch) -> Path:
    p = tmp_path / "HALTED"
    monkeypatch.setenv("KITE_HALT_PATH", str(p))
    return p


# -----------------------------------------------------------------------------
# write / read / clear
# -----------------------------------------------------------------------------

class TestWriteReadClear:
    def test_write_then_read(self, halt_file) -> None:
        write_halt(reason="test", by="agent-A")
        state = read_halt()
        assert state is not None
        assert state.reason == "test"
        assert state.by == "agent-A"
        assert state.expires_epoch_ms is None

    def test_clear_returns_true_when_existed(self, halt_file) -> None:
        write_halt(reason="x", by="me")
        assert clear_halt() is True
        assert read_halt() is None

    def test_clear_returns_false_when_absent(self, halt_file) -> None:
        assert clear_halt() is False

    def test_is_halted(self, halt_file) -> None:
        assert is_halted() is False
        write_halt(reason="x", by="me")
        assert is_halted() is True

    def test_overwrite(self, halt_file) -> None:
        write_halt(reason="first", by="A")
        write_halt(reason="second", by="B")
        state = read_halt()
        assert state.reason == "second"
        assert state.by == "B"


# -----------------------------------------------------------------------------
# Expiry
# -----------------------------------------------------------------------------

class TestExpiry:
    def test_unexpired_is_halted(self, halt_file) -> None:
        write_halt(reason="x", by="me", expires_in_seconds=60)
        assert read_halt() is not None

    def test_expired_auto_clears(self, halt_file) -> None:
        # Write with already-past expiry.
        state = HaltState(
            reason="x", since_epoch_ms=0, by="me",
            expires_epoch_ms=1,  # long past
        )
        halt_file.parent.mkdir(parents=True, exist_ok=True)
        halt_file.write_text(json.dumps(state.to_dict()))
        assert read_halt() is None
        assert not halt_file.exists(), "expired sentinel must be auto-unlinked"

    def test_expiry_serialises(self, halt_file) -> None:
        write_halt(reason="x", by="me", expires_in_seconds=30)
        raw = json.loads(halt_file.read_text())
        assert "expires_epoch_ms" in raw
        assert raw["expires_epoch_ms"] > 0


# -----------------------------------------------------------------------------
# Corrupt sentinel fails closed
# -----------------------------------------------------------------------------

class TestCorrupt:
    def test_malformed_json_stays_halted(self, halt_file) -> None:
        halt_file.parent.mkdir(parents=True, exist_ok=True)
        halt_file.write_text("not-json")
        state = read_halt()
        assert state is not None
        # Reason says "corrupt" so operators can see why.
        assert "corrupt" in state.reason.lower()

    def test_empty_file_stays_halted(self, halt_file) -> None:
        halt_file.parent.mkdir(parents=True, exist_ok=True)
        halt_file.write_text("")
        assert is_halted() is True


# -----------------------------------------------------------------------------
# parse_duration
# -----------------------------------------------------------------------------

class TestParseDuration:
    def test_seconds_suffix(self) -> None:
        assert parse_duration("30s") == 30
        assert parse_duration("0s") == 0

    def test_minutes_suffix(self) -> None:
        assert parse_duration("5m") == 300
        assert parse_duration("0.5m") == 30

    def test_hours_suffix(self) -> None:
        assert parse_duration("1h") == 3600
        assert parse_duration("2h") == 7200

    def test_days_suffix(self) -> None:
        assert parse_duration("1d") == 86400

    def test_bare_float(self) -> None:
        assert parse_duration("42") == 42.0
        assert parse_duration("42.5") == 42.5

    def test_malformed(self) -> None:
        with pytest.raises(ValueError):
            parse_duration("five minutes")
        with pytest.raises(ValueError):
            parse_duration("")
        with pytest.raises(ValueError):
            parse_duration("30x")


# -----------------------------------------------------------------------------
# assert_not_halted guard
# -----------------------------------------------------------------------------

class TestAssertNotHalted:
    def test_no_sentinel_ok(self, halt_file) -> None:
        assert_not_halted()  # should not raise

    def test_raises_when_halted(self, halt_file) -> None:
        write_halt(reason="x", by="me")
        with pytest.raises(HaltActive) as exc_info:
            assert_not_halted()
        assert exc_info.value.state.reason == "x"


# -----------------------------------------------------------------------------
# CLI integration
# -----------------------------------------------------------------------------

class TestCliIntegration:
    def test_halt_subcommand_writes_sentinel(self, halt_file) -> None:
        from kite_algo.kite_tool import build_parser, cmd_halt
        parser = build_parser()
        args = parser.parse_args([
            "halt", "--reason", "circuit breaker", "--by", "agent-A",
        ])
        rc = cmd_halt(args)
        assert rc == 0
        assert is_halted()
        assert read_halt().reason == "circuit breaker"

    def test_resume_requires_confirm_resume(self, halt_file, capsys) -> None:
        from kite_algo.kite_tool import build_parser, cmd_halt, cmd_resume
        parser = build_parser()
        # First halt so there's something to resume.
        cmd_halt(parser.parse_args(["halt", "--reason", "x"]))

        # Resume without --confirm-resume fails.
        args = parser.parse_args(["resume"])
        rc = cmd_resume(args)
        assert rc == 2
        assert is_halted()  # still halted

    def test_resume_with_confirm_resume_clears(self, halt_file) -> None:
        from kite_algo.kite_tool import build_parser, cmd_halt, cmd_resume
        parser = build_parser()
        cmd_halt(parser.parse_args(["halt", "--reason", "x"]))
        args = parser.parse_args(["resume", "--confirm-resume"])
        rc = cmd_resume(args)
        assert rc == 0
        assert not is_halted()

    def test_resume_yes_is_not_valid_alias(self, halt_file) -> None:
        """Agents must not be able to use --yes in place of --confirm-resume."""
        from kite_algo.kite_tool import build_parser
        parser = build_parser()
        # argparse accepts --yes on other commands but `resume` doesn't
        # declare it — so it should error.
        with pytest.raises(SystemExit):
            parser.parse_args(["resume", "--yes"])

    def test_place_refuses_when_halted(self, halt_file, monkeypatch) -> None:
        """Every write command must check the halt sentinel at entry."""
        from kite_algo import kite_tool as kt
        parser = kt.build_parser()

        kt.write_halt(reason="emergency", by="me")

        args = parser.parse_args([
            "place", "--exchange", "NSE", "--tradingsymbol", "RELIANCE",
            "--transaction-type", "BUY", "--order-type", "LIMIT",
            "--quantity", "1", "--product", "CNC", "--price", "1340",
            "--yes", "--skip-market-rules",
        ])
        # _require_not_halted raises SystemExit with a Refusing message.
        with pytest.raises(SystemExit) as exc:
            kt.cmd_place(args)
        assert "HALTED" in str(exc.value)

    def test_cancel_refuses_when_halted(self, halt_file) -> None:
        from kite_algo import kite_tool as kt
        parser = kt.build_parser()
        kt.write_halt(reason="x", by="me")
        args = parser.parse_args(["cancel", "--order-id", "123", "--yes"])
        with pytest.raises(SystemExit) as exc:
            kt.cmd_cancel(args)
        assert "HALTED" in str(exc.value)

    def test_gtt_create_refuses_when_halted(self, halt_file) -> None:
        from kite_algo import kite_tool as kt
        parser = kt.build_parser()
        kt.write_halt(reason="x", by="me")
        args = parser.parse_args([
            "gtt-create", "--exchange", "NSE", "--tradingsymbol", "RELIANCE",
            "--trigger-values", "1300", "--last-price", "1340",
            "--quantity", "1", "--price", "1295", "--yes",
        ])
        with pytest.raises(SystemExit) as exc:
            kt.cmd_gtt_create(args)
        assert "HALTED" in str(exc.value)

    def test_read_only_commands_ignore_halt(self, halt_file, monkeypatch) -> None:
        """Halt only blocks WRITES. Agents still need to query state
        (positions, orders, margins) so they can reconcile + reason about
        whether to resume.
        """
        from kite_algo import kite_tool as kt
        kt.write_halt(reason="x", by="me")
        # We can't test actual commands without a live Kite client; but we
        # can confirm read command handlers don't have _require_not_halted.
        import inspect
        for name in ("cmd_orders", "cmd_positions", "cmd_holdings",
                    "cmd_ltp", "cmd_quote", "cmd_health"):
            src = inspect.getsource(getattr(kt, name))
            assert "_require_not_halted" not in src, (
                f"{name} should not block on halt — it's read-only"
            )
