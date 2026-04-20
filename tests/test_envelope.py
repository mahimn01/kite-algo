"""Tests for the output envelope + ULID-style request IDs."""

from __future__ import annotations

import io
import json
import os
import time

import pytest

from kite_algo.envelope import (
    ENV_FORCE_JSON,
    ENV_NO_ENVELOPE,
    ENV_PARENT_REQUEST_ID,
    SCHEMA_VERSION,
    Envelope,
    envelope_to_json,
    envelopes_disabled,
    finalize_envelope,
    json_is_default_for,
    new_envelope,
    new_request_id,
    parent_request_id,
)


# -----------------------------------------------------------------------------
# Request ID
# -----------------------------------------------------------------------------

class TestRequestId:
    def test_length_is_26(self) -> None:
        rid = new_request_id()
        assert len(rid) == 26

    def test_alphabet_is_crockford_base32(self) -> None:
        rid = new_request_id()
        ok = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
        assert all(c in ok for c in rid), f"invalid chars in {rid}"

    def test_excludes_confusing_chars(self) -> None:
        """Crockford avoids I / L / O / U to prevent humans misreading IDs."""
        rid = new_request_id()
        assert "I" not in rid and "L" not in rid
        assert "O" not in rid and "U" not in rid

    def test_time_ordering(self) -> None:
        """Later IDs must sort lexicographically after earlier IDs."""
        a = new_request_id(clock_ms=1_000_000_000_000)
        b = new_request_id(clock_ms=1_000_000_001_000)
        assert a < b

    def test_uniqueness_within_same_millisecond(self) -> None:
        """At the same ms, 80 random bits of entropy make collisions negligible."""
        ids = {new_request_id(clock_ms=1_700_000_000_000) for _ in range(200)}
        assert len(ids) == 200

    def test_monotonic_over_wall_clock(self) -> None:
        a = new_request_id()
        time.sleep(0.002)  # 2 ms wait
        b = new_request_id()
        assert a < b


# -----------------------------------------------------------------------------
# parent_request_id from env
# -----------------------------------------------------------------------------

class TestParentRequestId:
    def test_none_when_unset(self, monkeypatch) -> None:
        monkeypatch.delenv(ENV_PARENT_REQUEST_ID, raising=False)
        assert parent_request_id() is None

    def test_reads_env(self, monkeypatch) -> None:
        monkeypatch.setenv(ENV_PARENT_REQUEST_ID, "PARENT_ID_123")
        assert parent_request_id() == "PARENT_ID_123"

    def test_strips_whitespace(self, monkeypatch) -> None:
        monkeypatch.setenv(ENV_PARENT_REQUEST_ID, "  PID  ")
        assert parent_request_id() == "PID"


# -----------------------------------------------------------------------------
# Envelope dataclass
# -----------------------------------------------------------------------------

class TestEnvelope:
    def test_new_envelope_has_required_fields(self) -> None:
        env = new_envelope("place")
        assert env.ok is True
        assert env.cmd == "place"
        assert env.schema_version == SCHEMA_VERSION
        assert len(env.request_id) == 26
        assert "started_at_epoch_ms" in env.meta

    def test_to_dict_canonical_shape(self) -> None:
        env = new_envelope("ltp")
        env.data = [{"symbol": "RELIANCE", "last": 1340}]
        out = env.to_dict()
        # All required keys present.
        for k in ("ok", "cmd", "schema_version", "request_id", "data", "warnings", "meta"):
            assert k in out
        # error field omitted unless set.
        assert "error" not in out

    def test_error_included_when_set(self) -> None:
        env = Envelope(ok=False, cmd="place", request_id="ID", error={"code": "AUTH"})
        out = env.to_dict()
        assert out["ok"] is False
        assert out["error"] == {"code": "AUTH"}

    def test_add_warning(self) -> None:
        env = new_envelope("place")
        env.add_warning("MIS_APPROACHING_CUTOFF", "Close to 15:20", severity="warn")
        assert env.warnings == [
            {"code": "MIS_APPROACHING_CUTOFF", "message": "Close to 15:20", "severity": "warn"}
        ]

    def test_multiple_warnings(self) -> None:
        env = new_envelope("place")
        env.add_warning("A", "first")
        env.add_warning("B", "second")
        assert len(env.warnings) == 2

    def test_parent_request_id_propagated(self, monkeypatch) -> None:
        monkeypatch.setenv(ENV_PARENT_REQUEST_ID, "PARENT_ABC")
        env = new_envelope("cmd")
        assert env.meta.get("parent_request_id") == "PARENT_ABC"

    def test_parent_request_id_absent_when_not_set(self, monkeypatch) -> None:
        monkeypatch.delenv(ENV_PARENT_REQUEST_ID, raising=False)
        env = new_envelope("cmd")
        assert "parent_request_id" not in env.meta


# -----------------------------------------------------------------------------
# finalize_envelope — elapsed_ms
# -----------------------------------------------------------------------------

class TestFinalize:
    def test_adds_elapsed_ms(self) -> None:
        env = new_envelope("cmd")
        time.sleep(0.005)
        finalize_envelope(env)
        assert env.meta["elapsed_ms"] >= 4

    def test_is_idempotent(self) -> None:
        env = new_envelope("cmd")
        finalize_envelope(env)
        first = env.meta["elapsed_ms"]
        finalize_envelope(env)
        # Should not re-compute — same value.
        assert env.meta["elapsed_ms"] == first

    def test_handles_missing_start(self) -> None:
        env = Envelope(ok=True, cmd="c", request_id="r")  # no started_at
        finalize_envelope(env)
        assert "elapsed_ms" not in env.meta


# -----------------------------------------------------------------------------
# envelopes_disabled + json_is_default_for
# -----------------------------------------------------------------------------

class TestEnvelopesDisabled:
    def test_unset_is_false(self, monkeypatch) -> None:
        monkeypatch.delenv(ENV_NO_ENVELOPE, raising=False)
        assert envelopes_disabled() is False

    def test_truthy_values(self, monkeypatch) -> None:
        for val in ("1", "true", "TRUE", "yes", "on"):
            monkeypatch.setenv(ENV_NO_ENVELOPE, val)
            assert envelopes_disabled() is True, f"failed for {val!r}"

    def test_falsy_values(self, monkeypatch) -> None:
        for val in ("0", "false", "no", "off", ""):
            monkeypatch.setenv(ENV_NO_ENVELOPE, val)
            assert envelopes_disabled() is False, f"failed for {val!r}"


class TestJsonDefault:
    def test_force_json_env(self, monkeypatch) -> None:
        monkeypatch.setenv(ENV_FORCE_JSON, "1")
        # Even a TTY-looking stream should yield JSON.
        tty = io.StringIO()
        tty.isatty = lambda: True  # type: ignore[method-assign]
        assert json_is_default_for(tty) is True

    def test_non_tty_defaults_to_json(self, monkeypatch) -> None:
        monkeypatch.delenv(ENV_FORCE_JSON, raising=False)
        pipe = io.StringIO()
        pipe.isatty = lambda: False  # type: ignore[method-assign]
        assert json_is_default_for(pipe) is True

    def test_tty_defaults_to_table(self, monkeypatch) -> None:
        monkeypatch.delenv(ENV_FORCE_JSON, raising=False)
        tty = io.StringIO()
        tty.isatty = lambda: True  # type: ignore[method-assign]
        assert json_is_default_for(tty) is False

    def test_broken_stream_defaults_to_json(self, monkeypatch) -> None:
        """If the stream's `isatty` raises, default to JSON (safer for agents)."""
        monkeypatch.delenv(ENV_FORCE_JSON, raising=False)
        class Broken:
            def isatty(self):
                raise ValueError("closed")
        assert json_is_default_for(Broken()) is True


# -----------------------------------------------------------------------------
# envelope_to_json
# -----------------------------------------------------------------------------

class TestEnvelopeSerialization:
    def test_round_trip(self) -> None:
        env = new_envelope("ltp")
        env.data = [{"symbol": "RELIANCE", "last": 1340.5}]
        text = envelope_to_json(env)
        parsed = json.loads(text)
        assert parsed["ok"] is True
        assert parsed["cmd"] == "ltp"
        assert parsed["data"] == [{"symbol": "RELIANCE", "last": 1340.5}]

    def test_non_serialisable_falls_through_to_str(self) -> None:
        """We use `default=str` so unknown objects don't crash emission."""
        env = new_envelope("cmd")

        class Weird:
            def __str__(self):
                return "weird-thing"

        env.data = {"x": Weird()}
        text = envelope_to_json(env)
        assert "weird-thing" in text
