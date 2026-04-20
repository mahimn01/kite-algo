"""Tests for secret redaction + logging filter.

A trading CLI is a disaster waiting to happen if secrets leak into logs or
error output. The redactor is defence-in-depth — it runs on every stderr
path, every log record, every error envelope. False positives (over-redact)
are preferred over false negatives (under-redact).
"""

from __future__ import annotations

import io
import logging

import pytest

from kite_algo.redaction import (
    REDACTED,
    install_logging_filter,
    known_secrets,
    redact_text,
)


# -----------------------------------------------------------------------------
# redact_text with known env secrets
# -----------------------------------------------------------------------------

class TestRedactTextWithKnownSecrets:
    def test_redacts_api_secret_literally(self, monkeypatch) -> None:
        monkeypatch.setenv("KITE_API_SECRET", "s3cretapiSECRETvalue123")
        msg = "kite rejected: bad api_secret=s3cretapiSECRETvalue123 see docs"
        out = redact_text(msg)
        assert "s3cretapiSECRETvalue123" not in out
        assert REDACTED in out

    def test_redacts_access_token_literally(self, monkeypatch) -> None:
        monkeypatch.setenv("KITE_ACCESS_TOKEN", "AtoKeN_1234567890ABCDEF")
        out = redact_text("login: got AtoKeN_1234567890ABCDEF from OAuth")
        assert "AtoKeN_1234567890ABCDEF" not in out

    def test_redacts_session_file_token(self, monkeypatch, tmp_path) -> None:
        sess = tmp_path / "session.json"
        sess.write_text('{"access_token": "MySeSsIoNToKen12345ABCD"}')
        monkeypatch.setenv("KITE_SESSION_PATH", str(sess))
        out = redact_text("error: MySeSsIoNToKen12345ABCD is invalid")
        assert "MySeSsIoNToKen12345ABCD" not in out

    def test_short_secret_not_redacted_literally(self, monkeypatch) -> None:
        """Secrets <8 chars are too risky to literal-replace (false positives
        would nuke ordinary words).
        """
        monkeypatch.setenv("KITE_API_SECRET", "ab12")
        out = redact_text("message containing ab12 somewhere")
        # Literal secret "ab12" short enough to skip; regex also shouldn't match
        # a 4-char token.
        assert "ab12" in out


# -----------------------------------------------------------------------------
# Pattern-based redaction (no known secret needed)
# -----------------------------------------------------------------------------

class TestRedactTextByPattern:
    def test_authorization_header(self) -> None:
        raw = "Authorization: token abc_api_key:xyzTOKENvalue"
        out = redact_text(raw)
        assert "xyzTOKENvalue" not in out
        assert REDACTED in out

    def test_access_token_kv_double_quoted(self) -> None:
        raw = '{"access_token": "SomeReallyLongSecretString12345"}'
        out = redact_text(raw)
        assert "SomeReallyLongSecretString12345" not in out

    def test_access_token_kv_single_quoted(self) -> None:
        raw = "{'access_token': 'SomeReallyLongSecretString12345'}"
        out = redact_text(raw)
        assert "SomeReallyLongSecretString12345" not in out

    def test_request_token_kv(self) -> None:
        raw = "error: request_token=abc123xyzVeryLongValueString45678"
        out = redact_text(raw)
        assert "abc123xyzVeryLongValueString45678" not in out

    def test_long_token_catchall(self) -> None:
        """A 32+ alphanumeric blob with no key context is still redacted."""
        raw = "something funny abcdef1234567890ABCDEF1234567890abcd ok"
        out = redact_text(raw)
        assert "abcdef1234567890ABCDEF1234567890abcd" not in out

    def test_bearer(self) -> None:
        raw = "header: Bearer eyJtypical-jwt-looking.string.here"
        out = redact_text(raw)
        assert "eyJtypical-jwt-looking.string.here" not in out

    def test_short_words_survive(self) -> None:
        """Normal English prose must not get mangled."""
        raw = "the quick brown fox jumps over the lazy dog"
        out = redact_text(raw)
        assert out == raw

    def test_non_string_passes_through(self) -> None:
        assert redact_text(None) == "None"  # type: ignore[arg-type]
        assert redact_text(123) == "123"  # type: ignore[arg-type]
        assert redact_text("") == ""


# -----------------------------------------------------------------------------
# Logging filter installation + redaction
# -----------------------------------------------------------------------------

class TestLoggingFilter:
    def test_filter_redacts_log_message(self, monkeypatch) -> None:
        monkeypatch.setenv("KITE_API_SECRET", "s3cretAPIsecretVALUE99")
        install_logging_filter(reset=True)  # refresh to pick up env var

        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setLevel(logging.DEBUG)
        root = logging.getLogger()
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        try:
            log = logging.getLogger("test_redaction")
            log.info("secret is s3cretAPIsecretVALUE99")
            handler.flush()
            out = buf.getvalue()
            assert "s3cretAPIsecretVALUE99" not in out, (
                f"token leaked through log filter: {out!r}"
            )
            assert REDACTED in out
        finally:
            root.removeHandler(handler)

    def test_filter_redacts_log_args(self, monkeypatch) -> None:
        """`log.info("token=%s", token)` is the most common leakage pattern."""
        monkeypatch.setenv("KITE_ACCESS_TOKEN", "ToKenABCDefghiJklMnoPqrS12345")
        install_logging_filter(reset=True)

        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setLevel(logging.DEBUG)
        root = logging.getLogger()
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        try:
            log = logging.getLogger("test_redaction2")
            log.warning("got token: %s", "ToKenABCDefghiJklMnoPqrS12345")
            handler.flush()
            out = buf.getvalue()
            assert "ToKenABCDefghiJklMnoPqrS12345" not in out
        finally:
            root.removeHandler(handler)

    def test_filter_is_idempotent(self) -> None:
        install_logging_filter()
        install_logging_filter()
        install_logging_filter()
        # Should have at most one SecretRedactingFilter on the root.
        from kite_algo.redaction import _SecretRedactingFilter
        root = logging.getLogger()
        count = sum(
            1 for f in root.filters if isinstance(f, _SecretRedactingFilter)
        )
        assert count == 1

    def test_filter_reset_rereads_secrets(self, monkeypatch) -> None:
        """After token rotation, reset=True must force a re-read."""
        monkeypatch.setenv("KITE_ACCESS_TOKEN", "OldToken111111111111")
        install_logging_filter(reset=True)

        monkeypatch.setenv("KITE_ACCESS_TOKEN", "NewToken222222222222")
        install_logging_filter(reset=True)

        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            logging.getLogger("x").warning("NewToken222222222222 was used")
            handler.flush()
            assert "NewToken222222222222" not in buf.getvalue()
        finally:
            root.removeHandler(handler)


# -----------------------------------------------------------------------------
# known_secrets harvests env + session
# -----------------------------------------------------------------------------

class TestKnownSecrets:
    def test_picks_up_env(self, monkeypatch, tmp_path) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("KITE_API_SECRET", "env_sec_abc12345xyz")
        monkeypatch.delenv("KITE_ACCESS_TOKEN", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("TRADING_ORDER_TOKEN", raising=False)
        monkeypatch.delenv("KITE_API_KEY", raising=False)
        secrets = known_secrets()
        assert "env_sec_abc12345xyz" in secrets

    def test_picks_up_session_file(self, monkeypatch, tmp_path) -> None:
        sess = tmp_path / "session.json"
        sess.write_text('{"access_token": "session_tok_abcdef1234567890"}')
        monkeypatch.setenv("KITE_SESSION_PATH", str(sess))
        secrets = known_secrets()
        assert "session_tok_abcdef1234567890" in secrets

    def test_missing_session_no_crash(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("KITE_SESSION_PATH", str(tmp_path / "nope.json"))
        # Should not raise.
        known_secrets()
