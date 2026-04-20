"""End-to-end tests for `cmd_login --listen-port` flow.

Spins up a real CallbackServer, fakes the browser by making an HTTP GET to
the redirect URI from a worker thread, mocks `KiteConnect.generate_session`,
and verifies that the session is written correctly.
"""

from __future__ import annotations

import socket
import threading
import time
import urllib.parse
import urllib.request
from unittest.mock import MagicMock

import pytest

from kite_algo.kite_tool import build_parser


@pytest.fixture
def parser():
    return build_parser()


def _ephemeral_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _simulate_browser(port: int, url_emitted: threading.Event, state_box: dict,
                      *, request_token: str = "TOK_REAL",
                      status: str = "success") -> None:
    """In a worker thread, wait for the listener to be ready then fire the
    callback GET.
    """
    # Wait for the main thread to have started the listener.
    if not url_emitted.wait(timeout=5.0):
        return
    state = state_box.get("state")
    if not state:
        return
    qs = urllib.parse.urlencode({
        "action": "login", "type": "login", "status": status,
        "request_token": request_token, "state": state,
    })
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/?{qs}", timeout=3).read()
    except Exception:
        pass


def _stub_kiteconnect(monkeypatch, state_capture: dict,
                      *, api_key: str = "KEY", api_secret: str = "SEC"):
    """Replace kite_algo.kite_tool._import_kiteconnect with a fake that
    captures the login URL's state nonce (so the test can feed it back)
    and stubs generate_session.
    """
    from kite_algo import kite_tool as kt

    class FakeClient:
        def __init__(self, api_key=None):
            self._api_key = api_key

        def login_url(self):
            return f"https://kite.zerodha.com/connect/login?v=3&api_key={self._api_key}"

        def generate_session(self, request_token, api_secret=None):
            state_capture["exchanged_token"] = request_token
            return {
                "access_token": "AT_NEW_123456789012345",
                "public_token": "PT_NEW",
                "user_id": "AB1234",
                "user_name": "Test User",
                "user_type": "individual",
                "email": "test@example.com",
                "broker": "ZERODHA",
            }

    monkeypatch.setattr(kt, "_import_kiteconnect", lambda: FakeClient)

    # Also stub creds so cfg.require_credentials() passes.
    monkeypatch.setenv("KITE_API_KEY", api_key)
    monkeypatch.setenv("KITE_API_SECRET", api_secret)


def _capture_login_url(monkeypatch, state_box: dict, url_emitted: threading.Event) -> None:
    """Intercept webbrowser.open so the test can read the state nonce out
    of the login URL *after* cmd_login has built it.
    """
    import webbrowser

    def fake_open(url):
        parsed = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed.query)
        rp = qs.get("redirect_params", [""])[0]
        inner = urllib.parse.parse_qs(rp)
        state_box["state"] = (inner.get("state") or [""])[0]
        url_emitted.set()
        return True

    monkeypatch.setattr(webbrowser, "open", fake_open)


# -----------------------------------------------------------------------------
# Happy path
# -----------------------------------------------------------------------------

class TestHappyPath:
    def test_listener_captures_token_and_saves_session(
        self, parser, monkeypatch, tmp_path,
    ) -> None:
        from kite_algo import kite_tool as kt
        from kite_algo.config import save_session

        # Isolate session path.
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data").mkdir()

        state_capture: dict = {}
        state_box: dict = {}
        url_emitted = threading.Event()
        _stub_kiteconnect(monkeypatch, state_capture)
        _capture_login_url(monkeypatch, state_box, url_emitted)

        port = _ephemeral_port()
        simulator = threading.Thread(
            target=_simulate_browser,
            args=(port, url_emitted, state_box),
            kwargs={"request_token": "REQ_GOOD"},
            daemon=True,
        )
        simulator.start()

        args = parser.parse_args([
            "login", "--listen-port", str(port), "--timeout", "5",
        ])
        rc = kt.cmd_login(args)

        simulator.join(timeout=3)
        assert rc == 0
        assert state_capture.get("exchanged_token") == "REQ_GOOD"

        # Session file is written with the returned tokens.
        from kite_algo.config import load_session
        saved = load_session(tmp_path / "data" / "session.json")
        assert saved["access_token"] == "AT_NEW_123456789012345"
        assert saved["user_id"] == "AB1234"


# -----------------------------------------------------------------------------
# CSRF / bad state
# -----------------------------------------------------------------------------

class TestCSRF:
    def test_wrong_state_rejected(self, parser, monkeypatch, tmp_path) -> None:
        from kite_algo import kite_tool as kt

        monkeypatch.chdir(tmp_path)
        (tmp_path / "data").mkdir()

        state_capture: dict = {}
        state_box: dict = {}
        url_emitted = threading.Event()
        _stub_kiteconnect(monkeypatch, state_capture)
        _capture_login_url(monkeypatch, state_box, url_emitted)

        port = _ephemeral_port()

        def bad_state_simulator() -> None:
            if not url_emitted.wait(timeout=5):
                return
            # Spoof a wrong state nonce.
            qs = urllib.parse.urlencode({
                "action": "login", "status": "success",
                "request_token": "REQ_SPOOF", "state": "WRONG_STATE_" + "X" * 50,
            })
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/?{qs}",
                                       timeout=3).read()
            except Exception:
                pass

        t = threading.Thread(target=bad_state_simulator, daemon=True)
        t.start()

        args = parser.parse_args([
            "login", "--listen-port", str(port), "--timeout", "5",
        ])
        rc = kt.cmd_login(args)
        t.join(timeout=3)

        assert rc == 1
        # `generate_session` must never have been called.
        assert "exchanged_token" not in state_capture


# -----------------------------------------------------------------------------
# Timeout
# -----------------------------------------------------------------------------

class TestTimeout:
    def test_timeout_returns_1(self, parser, monkeypatch, tmp_path) -> None:
        from kite_algo import kite_tool as kt
        monkeypatch.chdir(tmp_path)

        state_capture: dict = {}
        _stub_kiteconnect(monkeypatch, state_capture)
        # Don't open any browser; don't fire any callback. Listener must timeout.
        monkeypatch.setattr("webbrowser.open", lambda _url: True)

        port = _ephemeral_port()
        args = parser.parse_args([
            "login", "--listen-port", str(port), "--timeout", "0.3",
            "--no-browser",
        ])
        rc = kt.cmd_login(args)
        assert rc == 1
        assert "exchanged_token" not in state_capture


# -----------------------------------------------------------------------------
# Port in use
# -----------------------------------------------------------------------------

class TestPortInUse:
    def test_collision_reported(self, parser, monkeypatch, tmp_path) -> None:
        from kite_algo import kite_tool as kt
        monkeypatch.chdir(tmp_path)

        state_capture: dict = {}
        _stub_kiteconnect(monkeypatch, state_capture)

        port = _ephemeral_port()
        # Hog the port.
        hog = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        hog.bind(("127.0.0.1", port))
        hog.listen(1)
        try:
            args = parser.parse_args([
                "login", "--listen-port", str(port), "--timeout", "1",
                "--no-browser",
            ])
            rc = kt.cmd_login(args)
        finally:
            hog.close()
        assert rc == 1


# -----------------------------------------------------------------------------
# Paste fallback
# -----------------------------------------------------------------------------

class TestPasteFallback:
    def test_paste_mode_bypasses_listener(self, parser, monkeypatch, tmp_path) -> None:
        from kite_algo import kite_tool as kt
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data").mkdir()

        state_capture: dict = {}
        _stub_kiteconnect(monkeypatch, state_capture)
        monkeypatch.setattr("webbrowser.open", lambda _url: True)
        # Fake getpass → immediately return a request_token.
        import getpass as _gp
        monkeypatch.setattr(_gp, "getpass", lambda prompt="": "PASTED_TOKEN")

        # Use a port no listener binds — if the listener branch were
        # accidentally taken, the test would hang.
        port = _ephemeral_port()
        args = parser.parse_args([
            "login", "--paste", "--listen-port", str(port), "--no-browser",
        ])
        rc = kt.cmd_login(args)
        assert rc == 0
        assert state_capture["exchanged_token"] == "PASTED_TOKEN"


# -----------------------------------------------------------------------------
# Argparse sanity
# -----------------------------------------------------------------------------

class TestArgparse:
    def test_default_listen_port_5000(self, parser) -> None:
        args = parser.parse_args(["login"])
        assert args.listen_port == 5000
        assert args.paste is False
        assert args.timeout == 300.0

    def test_paste_flag(self, parser) -> None:
        args = parser.parse_args(["login", "--paste"])
        assert args.paste is True
