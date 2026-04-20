"""Tests for the OAuth callback listener.

These fire real HTTP GETs at a live listener (bound to an ephemeral port
on loopback) so the full accept→parse→respond path is exercised, not
mocked.
"""

from __future__ import annotations

import socket
import threading
import time
import urllib.parse
import urllib.request

import pytest

from kite_algo.oauth_callback import (
    CallbackResult,
    CallbackServer,
    LocalBindOnlyError,
    _is_loopback,
    login_url_with_state,
    new_state_nonce,
    pick_free_port,
)


STATE = "a" * 64  # placeholder 64-hex nonce


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _ephemeral_port() -> int:
    """Kernel-picks a free port we can bind."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(url: str, timeout: float = 3.0) -> tuple[int, str]:
    """GET — returns (status_code, body_str). Accepts 4xx responses."""
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return exc.code, body


# -----------------------------------------------------------------------------
# Security: bind only loopback
# -----------------------------------------------------------------------------

class TestLoopbackOnly:
    def test_is_loopback(self) -> None:
        assert _is_loopback("127.0.0.1")
        assert _is_loopback("localhost")
        assert _is_loopback("::1")

    def test_not_loopback(self) -> None:
        assert not _is_loopback("0.0.0.0")
        assert not _is_loopback("192.168.1.10")
        assert not _is_loopback("10.0.0.1")
        assert not _is_loopback("")

    def test_server_rejects_non_loopback_bind(self) -> None:
        with pytest.raises(LocalBindOnlyError):
            CallbackServer(port=5000, expected_state=STATE, host="0.0.0.0")

    def test_server_rejects_public_ip_bind(self) -> None:
        with pytest.raises(LocalBindOnlyError):
            CallbackServer(port=5000, expected_state=STATE, host="192.168.1.5")


# -----------------------------------------------------------------------------
# State nonce hygiene
# -----------------------------------------------------------------------------

class TestStateNonce:
    def test_nonce_length(self) -> None:
        nonce = new_state_nonce()
        # 32 bytes → 64 hex chars
        assert len(nonce) == 64
        assert all(c in "0123456789abcdef" for c in nonce)

    def test_nonces_are_unique(self) -> None:
        nonces = {new_state_nonce() for _ in range(500)}
        assert len(nonces) == 500

    def test_server_rejects_short_state(self) -> None:
        with pytest.raises(ValueError, match="16 chars"):
            CallbackServer(port=5000, expected_state="abc", host="127.0.0.1")


# -----------------------------------------------------------------------------
# login_url_with_state round-trip
# -----------------------------------------------------------------------------

class TestLoginUrlBuilder:
    def test_state_appended_as_redirect_params(self) -> None:
        base = "https://kite.zerodha.com/connect/login?v=3&api_key=abc"
        out = login_url_with_state(base, STATE)
        parsed = urllib.parse.urlparse(out)
        qs = urllib.parse.parse_qs(parsed.query)
        # redirect_params is itself a urlencoded KV string.
        assert "redirect_params" in qs
        inner = urllib.parse.parse_qs(qs["redirect_params"][0])
        assert inner["state"] == [STATE]

    def test_handles_base_url_without_query(self) -> None:
        base = "https://x.example/login"
        out = login_url_with_state(base, STATE)
        assert "?redirect_params=" in out


# -----------------------------------------------------------------------------
# Happy path
# -----------------------------------------------------------------------------

class TestHappyPath:
    def test_success_callback_captured(self) -> None:
        port = _ephemeral_port()
        with CallbackServer(port=port, expected_state=STATE) as server:
            url = (
                f"http://127.0.0.1:{port}/?"
                f"action=login&type=login&status=success&"
                f"request_token=XYZ123REAL&state={STATE}"
            )
            # Fire the GET in a thread so .wait() can run in the main thread.
            t = threading.Thread(target=lambda: _get(url), daemon=True)
            t.start()
            result = server.wait(timeout_s=3.0)
            t.join(timeout=2)

        assert result.request_token == "XYZ123REAL"
        assert result.action == "login"
        assert result.error is None

    def test_response_page_says_login_captured(self) -> None:
        port = _ephemeral_port()
        with CallbackServer(port=port, expected_state=STATE) as server:
            url = (
                f"http://127.0.0.1:{port}/?"
                f"status=success&request_token=T&state={STATE}"
            )
            code, body = _get(url)
            server.wait(timeout_s=1.0)  # drain the result so the test is deterministic

        assert code == 200
        assert "Login captured" in body


# -----------------------------------------------------------------------------
# CSRF: stale / wrong state
# -----------------------------------------------------------------------------

class TestCSRF:
    def test_state_mismatch_rejected(self) -> None:
        port = _ephemeral_port()
        with CallbackServer(port=port, expected_state=STATE) as server:
            bad_url = (
                f"http://127.0.0.1:{port}/?"
                f"status=success&request_token=T&state=WRONG_NONCE"
            )
            t = threading.Thread(target=lambda: _get(bad_url), daemon=True)
            t.start()
            result = server.wait(timeout_s=2.0)
            t.join(timeout=2)

        assert result.error == "csrf_mismatch"
        assert result.request_token is None

    def test_missing_state_rejected(self) -> None:
        port = _ephemeral_port()
        with CallbackServer(port=port, expected_state=STATE) as server:
            bad_url = (
                f"http://127.0.0.1:{port}/?status=success&request_token=T"
            )
            t = threading.Thread(target=lambda: _get(bad_url), daemon=True)
            t.start()
            result = server.wait(timeout_s=2.0)
            t.join(timeout=2)

        assert result.error == "csrf_mismatch"


# -----------------------------------------------------------------------------
# Malformed / partial callbacks
# -----------------------------------------------------------------------------

class TestMalformed:
    def test_explicit_error_status_rejected(self) -> None:
        port = _ephemeral_port()
        with CallbackServer(port=port, expected_state=STATE) as server:
            url = (
                f"http://127.0.0.1:{port}/?"
                f"status=error&error_type=TokenException&state={STATE}"
            )
            t = threading.Thread(target=lambda: _get(url), daemon=True)
            t.start()
            result = server.wait(timeout_s=2.0)
            t.join(timeout=2)

        assert result.error is not None
        assert result.error.startswith("bad_status")

    def test_favicon_ignored(self) -> None:
        """Browsers hit /favicon.ico after a successful redirect — we
        must not treat that as a callback attempt, and we must not set a
        bad result."""
        port = _ephemeral_port()
        with CallbackServer(port=port, expected_state=STATE) as server:
            # First hit: unrelated (favicon-like)
            t1 = threading.Thread(
                target=lambda: _get(f"http://127.0.0.1:{port}/favicon.ico"),
                daemon=True,
            )
            t1.start(); t1.join(timeout=2)
            # Then the real callback:
            url = (
                f"http://127.0.0.1:{port}/?"
                f"status=success&request_token=GOOD&state={STATE}"
            )
            t2 = threading.Thread(target=lambda: _get(url), daemon=True)
            t2.start()
            result = server.wait(timeout_s=3.0)
            t2.join(timeout=2)

        assert result.request_token == "GOOD"


# -----------------------------------------------------------------------------
# Timeout + lifecycle
# -----------------------------------------------------------------------------

class TestLifecycle:
    def test_timeout_when_no_callback(self) -> None:
        port = _ephemeral_port()
        with CallbackServer(port=port, expected_state=STATE) as server:
            result = server.wait(timeout_s=0.2)
        assert result.error == "timeout"

    def test_second_callback_ignored(self) -> None:
        port = _ephemeral_port()
        with CallbackServer(port=port, expected_state=STATE) as server:
            url1 = (
                f"http://127.0.0.1:{port}/?"
                f"status=success&request_token=FIRST&state={STATE}"
            )
            url2 = (
                f"http://127.0.0.1:{port}/?"
                f"status=success&request_token=SECOND&state={STATE}"
            )
            t1 = threading.Thread(target=lambda: _get(url1), daemon=True)
            t1.start(); t1.join(timeout=2)
            result = server.wait(timeout_s=2.0)
            # Fire a second callback AFTER we've captured the first.
            t2 = threading.Thread(target=lambda: _get(url2), daemon=True)
            t2.start(); t2.join(timeout=2)

        assert result.request_token == "FIRST"

    def test_redirect_uri_property(self) -> None:
        server = CallbackServer(port=49732, expected_state=STATE)
        assert server.redirect_uri == "http://127.0.0.1:49732/"

    def test_port_collision_raises(self) -> None:
        port = _ephemeral_port()
        # Hog the port with a live socket so the server's bind fails.
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", port))
        s.listen(1)
        try:
            server = CallbackServer(port=port, expected_state=STATE)
            with pytest.raises(OSError, match="already in use"):
                server.start()
        finally:
            s.close()

    def test_stop_is_idempotent(self) -> None:
        port = _ephemeral_port()
        server = CallbackServer(port=port, expected_state=STATE)
        server.start()
        server.stop()
        server.stop()  # second call no-ops


# -----------------------------------------------------------------------------
# pick_free_port
# -----------------------------------------------------------------------------

class TestPickFreePort:
    def test_returns_usable_port(self) -> None:
        port = pick_free_port(start=_ephemeral_port())
        # The returned port should bind cleanly.
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", port))
        finally:
            s.close()
