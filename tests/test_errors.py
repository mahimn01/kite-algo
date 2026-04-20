"""Tests for the structured error emitter."""

from __future__ import annotations

import io
import json

import pytest

from kite_algo.envelope import new_envelope
from kite_algo.errors import (
    build_error_payload,
    emit_error,
    suggested_action,
    with_error_envelope,
)
from kite_algo.exit_codes import ClassifiedError, classify_exception


# -----------------------------------------------------------------------------
# build_error_payload
# -----------------------------------------------------------------------------

def _fake_exc(name: str, message: str = "boom", **attrs) -> Exception:
    cls = type(name, (Exception,), {})
    exc = cls(message)
    for k, v in attrs.items():
        setattr(exc, k, v)
    return exc


class TestBuildErrorPayload:
    def test_basic_fields(self) -> None:
        payload = build_error_payload(_fake_exc("TokenException", "expired"))
        assert payload["code"] == "AUTH"
        assert payload["class"] == "TokenException"
        assert payload["retryable"] is False
        assert "suggested_action" in payload
        assert payload["exit_code_name"] == "AUTH"

    def test_kite_request_id_extracted(self) -> None:
        exc = _fake_exc("InputException", "bad", request_id="kite-123")
        payload = build_error_payload(exc)
        assert payload["kite_request_id"] == "kite-123"

    def test_kite_request_id_absent_when_missing(self) -> None:
        payload = build_error_payload(_fake_exc("InputException"))
        assert "kite_request_id" not in payload

    def test_field_errors_from_dataclass(self) -> None:
        from kite_algo.validation import ValidationError
        exc = _fake_exc("ValidationFailure")
        exc.field_errors = [
            ValidationError("quantity", "must be positive"),
            ValidationError("price", "required for LIMIT"),
        ]
        payload = build_error_payload(exc)
        assert payload["field_errors"] == [
            {"field": "quantity", "message": "must be positive"},
            {"field": "price", "message": "required for LIMIT"},
        ]

    def test_secret_redaction_in_message(self, monkeypatch) -> None:
        monkeypatch.setenv("KITE_ACCESS_TOKEN", "AtOkEnValueAbCdEfGh123456")
        payload = build_error_payload(
            _fake_exc("TokenException", "bad token AtOkEnValueAbCdEfGh123456 x")
        )
        assert "AtOkEnValueAbCdEfGh123456" not in payload["message"]
        assert "REDACTED" in payload["message"]

    def test_empty_message_fallback_to_class(self) -> None:
        payload = build_error_payload(_fake_exc("NetworkException", ""))
        # Empty string str(exc) → we fall back to class name so it's not blank.
        assert payload["message"]


# -----------------------------------------------------------------------------
# emit_error end-to-end
# -----------------------------------------------------------------------------

class TestEmitError:
    def test_writes_structured_json_to_stream(self) -> None:
        env = new_envelope("place")
        buf = io.StringIO()
        code = emit_error(_fake_exc("TokenException", "expired"), env=env, stream=buf)
        out = buf.getvalue().strip()
        parsed = json.loads(out)
        assert parsed["ok"] is False
        assert parsed["cmd"] == "place"
        assert parsed["data"] is None
        assert parsed["error"]["code"] == "AUTH"
        assert code == 5  # AUTH

    def test_elapsed_ms_populated(self) -> None:
        env = new_envelope("place")
        buf = io.StringIO()
        emit_error(_fake_exc("TokenException"), env=env, stream=buf)
        parsed = json.loads(buf.getvalue())
        assert "elapsed_ms" in parsed["meta"]

    def test_broken_pipe_does_not_crash(self) -> None:
        """Agent-style subprocess with `| head` can close stdout/stderr mid-write."""
        class BrokenStream:
            def write(self, s):
                raise BrokenPipeError()
            def flush(self):
                pass
        env = new_envelope("cmd")
        # Should not raise.
        emit_error(_fake_exc("TokenException"), env=env, stream=BrokenStream())

    def test_internal_code_for_unknown_exception(self) -> None:
        env = new_envelope("cmd")
        buf = io.StringIO()
        code = emit_error(Exception("???"), env=env, stream=buf)
        parsed = json.loads(buf.getvalue())
        assert parsed["error"]["code"] == "INTERNAL"
        assert parsed["error"]["retryable"] is False


# -----------------------------------------------------------------------------
# with_error_envelope decorator
# -----------------------------------------------------------------------------

class TestWithErrorEnvelope:
    def test_success_path(self) -> None:
        @with_error_envelope("place")
        def cmd(args, *, env):
            env.data = {"ok": True, "id": "X"}
            return 0

        rc = cmd(None)
        assert rc == 0

    def test_exception_becomes_structured_error(self, capsys) -> None:
        @with_error_envelope("place")
        def cmd(args, *, env):
            raise _fake_exc("OrderException", "margin insufficient")

        rc = cmd(None)
        assert rc == 4  # HARD_REJECT
        captured = capsys.readouterr()
        parsed = json.loads(captured.err)
        assert parsed["ok"] is False
        assert parsed["error"]["code"] == "HARD_REJECT"
        assert parsed["error"]["class"] == "OrderException"

    def test_system_exit_propagates(self) -> None:
        """SystemExit (argparse, _require_yes) must propagate so the normal
        exit path handles it — we don't want to catch argparse's own exit.
        """
        @with_error_envelope("place")
        def cmd(args, *, env):
            raise SystemExit(2)

        with pytest.raises(SystemExit) as exc_info:
            cmd(None)
        assert exc_info.value.code == 2

    def test_keyboard_interrupt_returns_sigint(self, capsys) -> None:
        """Ctrl+C during a long command becomes a structured 130 exit."""
        @with_error_envelope("place")
        def cmd(args, *, env):
            raise KeyboardInterrupt()

        rc = cmd(None)
        assert rc == 130


# -----------------------------------------------------------------------------
# suggested_action coverage
# -----------------------------------------------------------------------------

class TestSuggestedAction:
    def test_known_codes(self) -> None:
        assert "login" in suggested_action("AUTH")
        assert "yes" in suggested_action("USAGE").lower()
        assert "retry" in suggested_action("TRANSIENT").lower()

    def test_unknown_code_generic(self) -> None:
        assert suggested_action("MADE_UP_CODE")  # returns non-empty fallback
