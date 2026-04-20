"""One-shot HTTP callback listener for the Kite OAuth flow.

Why:
Kite redirects the browser to the app's registered redirect URI with
`?action=login&type=login&status=success&request_token=<tok>` after a
successful sign-in. The default `cmd_login` asks the user to paste the
`request_token` from their address bar; that's tedious and error-prone.

This module binds a tiny HTTP server on `127.0.0.1:PORT` that catches the
302 automatically and hands the token back to `cmd_login`. It matches the
pattern `gh auth login`, `stripe login`, and `gcloud auth login` use.

Security properties that matter:

1. **Loopback only.** `host` is `127.0.0.1` by default and we refuse to
   bind `0.0.0.0` without an explicit, awkward flag — otherwise anyone on
   the LAN can race to intercept the request_token during the login
   window.
2. **CSRF state.** A 256-bit random nonce is passed through
   `redirect_params=state=<nonce>` in the Kite login URL. On callback we
   verify the nonce matches before accepting the token. Prevents a
   stray/stale redirect from another tab from completing our flow.
3. **Non-blocking handler.** Kite never retries the 302 — if our handler
   500s, the token is stranded in the user's address bar (expiring in
   minutes). The handler therefore returns 200 immediately and does NOT
   exchange the token inline; the main thread picks it up via the shared
   result slot.
4. **One-shot.** The server shuts down after the first valid callback so
   a second visit can't overwrite the captured token with a stale value.

Kite-specific behavior documented in `research`:
- Kite only redirects on SUCCESS. On cancel / 2FA abort / bad credentials
  the user stays on `kite.zerodha.com` with an inline error — the
  callback is never hit. The main thread should therefore treat timeout
  as "user did not complete the flow" rather than "failed".
- `request_token` is single-use and expires in "a few minutes" (no
  exact number documented). Exchange it immediately on capture.
- Port in the redirect URI is matched **literally** by Kite — you must
  register the exact port the listener binds. Dynamic-port listeners do
  not work because the URI won't match.
"""

from __future__ import annotations

import http.server
import logging
import secrets
import socket
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


SUCCESS_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>kite-algo — login captured</title>
<style>
  :root { color-scheme: dark; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    background: #0b0d10; color: #e6e8eb; margin: 0;
    display: flex; align-items: center; justify-content: center;
    min-height: 100vh;
  }
  .card {
    text-align: center; padding: 56px 40px; max-width: 420px;
    background: #131519; border: 1px solid #2a2d32; border-radius: 14px;
  }
  .check { font-size: 48px; color: #7ee787; margin-bottom: 8px; }
  h1 { font-size: 18px; margin: 0 0 8px; }
  p { color: #8b949e; font-size: 14px; line-height: 1.5; margin: 4px 0; }
  code { background: #1c1f25; padding: 1px 6px; border-radius: 4px; font-size: 13px; }
</style></head><body>
<div class="card">
  <div class="check">✓</div>
  <h1>Login captured</h1>
  <p>You can close this tab and return to the terminal.</p>
  <p><code>kite-algo</code> will now exchange the token and save your session.</p>
</div></body></html>
"""


ERROR_PAGE_TMPL = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>kite-algo — login rejected</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    background: #0b0d10; color: #e6e8eb; margin: 0;
    display: flex; align-items: center; justify-content: center;
    min-height: 100vh;
  }}
  .card {{
    text-align: center; padding: 56px 40px; max-width: 520px;
    background: #131519; border: 1px solid #2a2d32; border-radius: 14px;
  }}
  .x {{ font-size: 48px; color: #ff8080; margin-bottom: 8px; }}
  h1 {{ font-size: 18px; margin: 0 0 8px; }}
  p {{ color: #8b949e; font-size: 14px; line-height: 1.5; margin: 4px 0; }}
  code {{ background: #1c1f25; padding: 1px 6px; border-radius: 4px; font-size: 13px; }}
</style></head><body>
<div class="card">
  <div class="x">✗</div>
  <h1>Login rejected</h1>
  <p>{message}</p>
  <p>Check the terminal for details.</p>
</div></body></html>
"""


@dataclass
class CallbackResult:
    """Outcome of a single callback attempt.

    Either `request_token` is set (happy path), or `error` is set with a
    machine-readable code (timeout, csrf_mismatch, bad_status, bad_request).
    Both None is impossible — one field is always populated before the
    main thread reads the result.
    """
    request_token: str | None = None
    action: str | None = None
    error: str | None = None
    raw_query: str | None = None  # for debugging / logging when things go sideways


class LocalBindOnlyError(RuntimeError):
    """Raised when the caller asks for a non-loopback bind.

    Exposing the listener on 0.0.0.0 makes the request_token harvestable
    by anyone on the network. Block it at the API layer, not just with a
    flag — a bug one level up must not accidentally open it.
    """


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

def _is_loopback(host: str) -> bool:
    """Accept only true loopback addresses. `::1`, IPv6 loopback, allowed."""
    if host in ("127.0.0.1", "localhost", "::1"):
        return True
    # A resolved 127.0.0.0/8 address is also loopback, but we reject
    # anything but the canonical forms to avoid DNS-rebind shenanigans.
    return False


def pick_free_port(start: int = 5000, attempts: int = 50) -> int:
    """Find a free port in [start, start+attempts). Not used by default
    because Kite requires the port to match the registered redirect URI
    exactly, but useful for tests.
    """
    for port in range(start, start + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"no free port in [{start}, {start + attempts})")


class _QuietHTTPServer(http.server.HTTPServer):
    allow_reuse_address = True


class CallbackServer:
    """Binds a one-shot HTTP server that captures Kite's OAuth redirect.

    Usage:

        server = CallbackServer(port=5000, expected_state="<nonce>")
        server.start()
        # …open the browser to the Kite login URL that encodes `expected_state`…
        result = server.wait(timeout_s=300)
        server.stop()

        if result.request_token:
            exchange_and_save(result.request_token)
        else:
            raise SystemExit(f"callback failed: {result.error}")

    `expected_state` is REQUIRED — we don't want a default fallback that
    silently accepts any redirect. Callers must mint a random nonce and
    include it in the login URL's `redirect_params`.
    """

    def __init__(
        self,
        *,
        port: int,
        expected_state: str,
        host: str = "127.0.0.1",
    ):
        if not _is_loopback(host):
            raise LocalBindOnlyError(
                f"CallbackServer refuses non-loopback bind {host!r}. "
                f"Use 127.0.0.1; a LAN-exposed listener leaks the request_token."
            )
        if not expected_state or len(expected_state) < 16:
            raise ValueError(
                f"expected_state must be at least 16 chars; got {len(expected_state or '')}"
            )
        self.host = host
        self.port = port
        self._expected_state = expected_state
        self._result: CallbackResult | None = None
        self._got_result = threading.Event()
        self._server: _QuietHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -----------------------------------------------------------------
    # Public
    # -----------------------------------------------------------------

    @property
    def redirect_uri(self) -> str:
        """The exact URI the Kite app profile must register for this
        listener to catch. Trailing slash included — Kite appends the
        query string after whatever is registered.
        """
        return f"http://{self.host}:{self.port}/"

    def start(self) -> None:
        """Bind the port + start serving in a background thread.

        Raises OSError with a helpful message if the port is already in
        use (another login in progress, or a leftover listener).
        """
        if self._server is not None:
            raise RuntimeError("server already started")

        handler = self._make_handler()
        try:
            self._server = _QuietHTTPServer((self.host, self.port), handler)
        except OSError as exc:
            if exc.errno in (48, 98):  # EADDRINUSE on macOS / Linux
                raise OSError(
                    f"port {self.port} is already in use. Is another "
                    f"`kite-algo login` running, or a leftover listener? "
                    f"Pick a different port with --listen-port (and update "
                    f"your Kite app profile's redirect URI to match)."
                ) from exc
            raise

        self._thread = threading.Thread(
            target=self._server.serve_forever, name="kite-oauth-listener",
            daemon=True,
        )
        self._thread.start()

    def wait(self, timeout_s: float) -> CallbackResult:
        """Block until a valid callback arrives or `timeout_s` elapses.

        Safe to call from the main thread; the HTTP handler runs on a
        daemon worker. Always returns a CallbackResult — never raises on
        timeout (caller inspects `.error`).
        """
        if self._server is None:
            raise RuntimeError("call start() before wait()")
        got = self._got_result.wait(timeout=timeout_s)
        if not got:
            return CallbackResult(error="timeout")
        assert self._result is not None
        return self._result

    def stop(self) -> None:
        if self._server is None:
            return
        try:
            self._server.shutdown()
        except Exception as exc:
            log.debug("server.shutdown raised: %s", exc)
        try:
            self._server.server_close()
        except Exception:
            pass
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._server = None
        self._thread = None

    def __enter__(self) -> "CallbackServer":
        self.start()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.stop()

    # -----------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------

    def _set_result(self, result: CallbackResult) -> None:
        """Store the first result; subsequent callbacks are ignored."""
        if self._result is not None:
            return
        self._result = result
        self._got_result.set()

    def _make_handler(self):
        """Build a BaseHTTPRequestHandler class closed over `self`.

        The class has to be generated inside the instance method because
        BaseHTTPRequestHandler is instantiated by the server for every
        connection and takes no user args — the only way to plumb our
        state through is via class-level references.
        """
        server_self = self

        class Handler(http.server.BaseHTTPRequestHandler):
            # Silence access-log noise. We redirect error_message too; the
            # default writes to stderr which pollutes the CLI output.
            def log_message(self, *args, **kwargs):
                return

            def log_error(self, *args, **kwargs):
                return

            def do_GET(h_self) -> None:  # type: ignore[override]
                # Respond 200 IMMEDIATELY so the browser doesn't see a 500
                # and the user doesn't panic. Parse after.
                parsed = urllib.parse.urlparse(h_self.path)
                query = urllib.parse.parse_qs(parsed.query)
                token = (query.get("request_token") or [""])[0]
                state = (query.get("state") or [""])[0]
                status = (query.get("status") or [""])[0]
                action = (query.get("action") or [""])[0]

                # Ignore non-callback hits (favicon.ico, preflight, etc.)
                if not token and not status:
                    h_self.send_response(204)
                    h_self.end_headers()
                    return

                # CSRF check — must match the nonce we minted.
                if not secrets.compare_digest(state, server_self._expected_state):
                    page = ERROR_PAGE_TMPL.format(message="state mismatch (CSRF)")
                    h_self.send_response(400)
                    h_self.send_header("Content-Type", "text/html; charset=utf-8")
                    h_self.send_header("Content-Length", str(len(page)))
                    h_self.end_headers()
                    h_self.wfile.write(page.encode("utf-8"))
                    server_self._set_result(CallbackResult(
                        error="csrf_mismatch",
                        raw_query=parsed.query,
                    ))
                    return

                if status != "success" or not token:
                    page = ERROR_PAGE_TMPL.format(
                        message=f"Kite returned status={status or '(missing)'}"
                    )
                    h_self.send_response(400)
                    h_self.send_header("Content-Type", "text/html; charset=utf-8")
                    h_self.send_header("Content-Length", str(len(page)))
                    h_self.end_headers()
                    h_self.wfile.write(page.encode("utf-8"))
                    server_self._set_result(CallbackResult(
                        error=f"bad_status:{status or 'missing'}",
                        raw_query=parsed.query,
                    ))
                    return

                # Happy path.
                body = SUCCESS_PAGE
                h_self.send_response(200)
                h_self.send_header("Content-Type", "text/html; charset=utf-8")
                h_self.send_header("Content-Length", str(len(body)))
                h_self.end_headers()
                h_self.wfile.write(body.encode("utf-8"))
                server_self._set_result(CallbackResult(
                    request_token=token,
                    action=action,
                ))

        return Handler


# ---------------------------------------------------------------------------
# State nonce + URL helper
# ---------------------------------------------------------------------------

def new_state_nonce() -> str:
    """32 bytes of crypto randomness → 64-char hex. Passed through the
    OAuth round-trip via `redirect_params` and checked on callback.
    """
    return secrets.token_hex(32)


def login_url_with_state(base_login_url: str, state: str) -> str:
    """Append `redirect_params=state=<state>` to a Kite login URL.

    `base_login_url` is what `KiteConnect.login_url()` returns —
    `https://kite.zerodha.com/connect/login?v=3&api_key=<key>`. We add
    `redirect_params` as a separate query param whose value is itself a
    urlencoded `state=<hex>`.
    """
    inner = urllib.parse.urlencode({"state": state})
    # `urlencode` returns `state=<hex>`; that becomes the VALUE of
    # `redirect_params`, which in the outer URL we must also urlencode.
    joiner = "&" if "?" in base_login_url else "?"
    return f"{base_login_url}{joiner}redirect_params={urllib.parse.quote(inner, safe='')}"
