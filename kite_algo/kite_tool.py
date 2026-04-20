"""Comprehensive Kite Connect data + operations CLI.

Parallel to `trading_algo.ibkr_tool`. Reads Kite credentials from `.env` and
the daily-rotating access token from `data/session.json` (written by the
`login` command).

Usage:
    python -m kite_algo.kite_tool <command> [args]

All commands support `--format json|csv|table`.
"""

from __future__ import annotations

import argparse
import csv
import getpass
import json
import logging
import math
import os
import sys
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from kite_algo.config import (
    DEFAULT_SESSION_PATH,
    KiteConfig,
    load_dotenv,
    load_session,
    save_session,
)
from kite_algo.logging_setup import configure_logging
from kite_algo.resilience import (
    IdempotentOrderPlacer,
    KiteRateLimiter,
    ModificationLimitExceeded,
    RateLimitedKiteClient,
    find_order_by_tag,
    new_order_tag,
    record_modification,
    retry_with_backoff,
)
from kite_algo.halt import (
    assert_not_halted,
    clear_halt,
    parse_duration,
    read_halt,
    write_halt,
)
from kite_algo.idempotency import (
    IdempotencyStore,
    derive_tag_from_key,
)
from kite_algo.market_rules import check_market_rules
from kite_algo.projection import (
    parse_fields,
    project_rows,
    summarize_holdings,
    summarize_option_chain,
    summarize_orders,
    summarize_positions,
)
from kite_algo.redaction import install_logging_filter, redact_text
from kite_algo.validation import format_errors, validate_order


def _redact_secrets(text: str) -> str:
    """Redact secrets from any text about to hit stderr/stdout/logs.

    Thin alias over `kite_algo.redaction.redact_text`. Kept as a module-level
    function for backwards-compat with existing call sites.
    """
    return redact_text(text)


# -----------------------------------------------------------------------------
# Bootstrap
# -----------------------------------------------------------------------------

load_dotenv()
configure_logging(level=logging.INFO if os.getenv("KITE_DEBUG") else logging.WARNING)
# Install the secret-redacting filter at the root logger BEFORE any module
# emits its first log line. Third-party SDKs (kiteconnect, urllib3) log via
# their own loggers; the root filter intercepts everything.
install_logging_filter()
log = logging.getLogger("kite_tool")

_RATE_LIMITER = KiteRateLimiter()


def _import_kiteconnect() -> Any:
    try:
        from kiteconnect import KiteConnect  # type: ignore
    except ImportError as exc:
        print(f"ERROR: kiteconnect is not installed: {exc}", file=sys.stderr)
        print("Install: pip install kiteconnect", file=sys.stderr)
        sys.exit(2)
    return KiteConnect


def _import_kiteticker() -> Any:
    try:
        from kiteconnect import KiteTicker  # type: ignore
    except ImportError as exc:
        print(f"ERROR: kiteconnect is not installed: {exc}", file=sys.stderr)
        sys.exit(2)
    return KiteTicker


# -----------------------------------------------------------------------------
# Output helpers
# -----------------------------------------------------------------------------

def _to_jsonable(obj: Any) -> Any:
    if obj is None or isinstance(obj, (str, int, bool)):
        return obj
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, (list, tuple, set)):
        return [_to_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if is_dataclass(obj):
        return {k: _to_jsonable(v) for k, v in asdict(obj).items()}
    if hasattr(obj, "__dict__"):
        return {k: _to_jsonable(v) for k, v in vars(obj).items() if not k.startswith("_")}
    return str(obj)


def _resolve_format(fmt: str) -> str:
    """Resolve `auto` to `json` when stdout is not a TTY, `table` otherwise.

    Rule of thumb for an agent-driven CLI:
    - Piped / subprocess / agent invocation → JSON envelope.
    - Interactive human in a terminal → table.
    Override via `--format json|csv|table` or `KITE_JSON=1` env.
    """
    if fmt == "auto":
        from kite_algo.envelope import json_is_default_for
        return "json" if json_is_default_for() else "table"
    return fmt


def _emit(
    data: Any,
    fmt: str,
    *,
    cmd: str | None = None,
    env: Any = None,
    warnings: list[dict] | None = None,
) -> None:
    """Emit `data` to stdout in the requested format.

    When `fmt` resolves to `json` and `cmd` is provided (and the escape-hatch
    `KITE_NO_ENVELOPE` is not set), `data` is wrapped in the canonical
    envelope (`kite_algo.envelope`):

        {ok, cmd, schema_version, request_id, data, warnings, meta}

    Agents key off the envelope to detect partial outputs, propagate trace
    IDs, and branch on success vs error without regex-parsing text. Humans
    using `--format table` still get plain column-aligned output.

    For non-wrapped modes (csv, table, or `KITE_NO_ENVELOPE=1`), `data` is
    emitted as before.

    `env` is an optional pre-built Envelope (from a `with_error_envelope`
    decorator). When supplied, we reuse its `request_id` and warnings; this
    preserves the trace ID across an entire command's execution rather than
    minting a fresh one at emission time.
    """
    from kite_algo.envelope import (
        envelope_to_json,
        envelopes_disabled,
        finalize_envelope,
        new_envelope,
    )

    fmt = _resolve_format(fmt)

    if fmt == "json" and cmd is not None and not envelopes_disabled():
        # Envelope path: build or reuse, then serialise with full meta.
        if env is None:
            env_obj = new_envelope(cmd)
        else:
            env_obj = env
        env_obj.data = _to_jsonable(data)
        if warnings:
            for w in warnings:
                if isinstance(w, dict):
                    env_obj.warnings.append(w)
        finalize_envelope(env_obj)
        print(envelope_to_json(env_obj))
        return

    if fmt == "json":
        # Raw-JSON mode: envelope disabled or cmd not provided. Still safe
        # for scripts that opted out of the envelope; they get the bare data.
        print(json.dumps(_to_jsonable(data), indent=2, default=str))
        return

    if fmt == "csv":
        rows = data if isinstance(data, list) else [data]
        rows = [_to_jsonable(r) for r in rows if r is not None]
        rows = [r if isinstance(r, dict) else {"value": r} for r in rows]
        if not rows:
            return
        keys = sorted({k for r in rows for k in r.keys()})
        writer = csv.DictWriter(sys.stdout, fieldnames=keys)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in keys})
        return

    # table (default for interactive terminals)
    rows = data if isinstance(data, list) else [data]
    rows = [_to_jsonable(r) for r in rows if r is not None]
    if not rows:
        print("(no rows)")
        return
    if isinstance(rows[0], dict):
        keys = sorted({k for r in rows for k in r.keys()})
        widths = {k: max(len(k), *(len(str(r.get(k, ""))) for r in rows)) for k in keys}
        widths = {k: min(v, 50) for k, v in widths.items()}
        print("  ".join(k.ljust(widths[k]) for k in keys))
        print("  ".join("-" * widths[k] for k in keys))
        for r in rows:
            print("  ".join(
                str(r.get(k, "") if r.get(k) is not None else "")[: widths[k]].ljust(widths[k])
                for k in keys
            ))
    else:
        for r in rows:
            print(r)


# -----------------------------------------------------------------------------
# Kite client factory
# -----------------------------------------------------------------------------

def _new_client(require_session: bool = True) -> Any:
    """Return a rate-limited Kite client.

    Every method call goes through KiteRateLimiter before hitting the API,
    automatically picking the right bucket (general / historical / orders).
    """
    KiteConnect = _import_kiteconnect()
    cfg = KiteConfig.from_env()
    if require_session:
        cfg.require_session()
    else:
        cfg.require_credentials()
    raw = KiteConnect(api_key=cfg.api_key)
    if cfg.access_token:
        raw.set_access_token(cfg.access_token)
    return RateLimitedKiteClient(raw, _RATE_LIMITER)


# =============================================================================
# AUTH / SESSION
# =============================================================================

def _exchange_and_save(
    client: Any, cfg: KiteConfig, request_token: str, args: argparse.Namespace,
) -> int:
    """Shared final step for both the listener and paste login paths.

    Exchanges `request_token` for an access_token, writes the session
    file, emits the non-secret bits of the response through the envelope.
    """
    try:
        data = client.generate_session(request_token, api_secret=cfg.api_secret)
    except Exception as exc:
        print(f"ERROR: generate_session failed: {_redact_secrets(str(exc))}",
              file=sys.stderr)
        return 1

    session = {
        "access_token": data.get("access_token"),
        "public_token": data.get("public_token"),
        "user_id": data.get("user_id"),
        "user_name": data.get("user_name"),
        "user_type": data.get("user_type"),
        "email": data.get("email"),
        "broker": data.get("broker"),
        "api_key": cfg.api_key,
        "login_time": datetime.now().isoformat(timespec="seconds"),
    }
    save_session(session)
    print(f"\nAccess token saved to {DEFAULT_SESSION_PATH}", file=sys.stderr)

    # Refresh the redaction filter so the brand-new access_token is
    # redacted from any subsequent log line within this process.
    try:
        from kite_algo.redaction import install_logging_filter
        install_logging_filter(reset=True)
    except Exception:
        pass

    _emit(
        {k: v for k, v in session.items() if k not in ("access_token", "public_token")},
        args.format, cmd=args.cmd,
    )
    return 0


def cmd_login(args: argparse.Namespace) -> int:
    """Interactive OAuth login.

    Two modes:

    - **Listener (default)**: bind `http://127.0.0.1:<port>/` on this
      machine, open the Kite login URL, wait for Kite's 302 to hit our
      listener, verify CSRF state, and exchange the captured
      `request_token` for an access_token. Matches the `gh auth login`
      / `stripe login` / `gcloud auth login` pattern. Requires the Kite
      app profile at developers.kite.trade to register
      `http://127.0.0.1:<port>/` as its redirect URI (Zerodha explicitly
      allows this — HTTPS is waived for loopback).

    - **Paste (`--paste`)**: fall back to the original copy/paste flow
      for when the listener can't be reached (agent sandbox, exotic
      network, user signing in on a different device without SSH port
      forwarding).
    """
    KiteConnect = _import_kiteconnect()
    cfg = KiteConfig.from_env()
    cfg.require_credentials()

    client = KiteConnect(api_key=cfg.api_key)
    base_login_url = client.login_url()

    if args.paste:
        print(f"Opening Kite login URL:\n  {base_login_url}", file=sys.stderr)
        if not args.no_browser:
            try:
                webbrowser.open(base_login_url)
            except Exception:
                pass

        # getpass so the token never echoes or lands in shell history.
        print(
            "\nAfter signing in, copy `request_token=...` from the redirect URL\n"
            "and paste it below (input will not echo).",
            file=sys.stderr,
        )
        try:
            request_token = getpass.getpass("request_token: ").strip()
        except EOFError:
            request_token = ""

        if not request_token:
            print("ERROR: empty request_token", file=sys.stderr)
            return 1
        return _exchange_and_save(client, cfg, request_token, args)

    # Default: local loopback listener.
    from kite_algo.oauth_callback import (
        CallbackServer,
        login_url_with_state,
        new_state_nonce,
    )

    state = new_state_nonce()
    login_url = login_url_with_state(base_login_url, state)

    try:
        server = CallbackServer(port=args.listen_port, expected_state=state)
        server.start()
    except OSError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    try:
        redirect_uri = server.redirect_uri
        print(
            "Kite login — listener mode.\n"
            f"  Redirect URI to register at developers.kite.trade: {redirect_uri}\n"
            f"  Listening on:                                     {redirect_uri}\n"
            f"  Timeout:                                          {args.timeout:.0f}s\n"
            f"\nOpen this URL in ANY browser (your phone works over an SSH tunnel\n"
            f"— see docs/LOGIN.md for the recipe):\n\n  {login_url}\n",
            file=sys.stderr,
        )
        if not args.no_browser:
            try:
                webbrowser.open(login_url)
            except Exception:
                pass

        print(
            f"Waiting for callback (up to {args.timeout:.0f}s)...",
            file=sys.stderr,
        )
        result = server.wait(timeout_s=args.timeout)
    finally:
        server.stop()

    if result.request_token is None:
        reason = result.error or "unknown"
        hint = ""
        if reason == "timeout":
            hint = (
                "\nNo callback arrived. Common causes:\n"
                f"  - Your Kite app's redirect URI is not {redirect_uri!r}\n"
                "    (must match exactly, including port + trailing slash).\n"
                "  - You signed in on a different machine and the 302 didn't\n"
                "    reach this listener. Re-run on the same machine, or set\n"
                "    up `ssh -L 5000:127.0.0.1:5000` and sign in on your laptop.\n"
                "  - You cancelled the login / closed the tab.\n"
                "Retry, or use `--paste` to skip the listener."
            )
        elif reason == "csrf_mismatch":
            hint = (
                "\nCSRF state mismatch: the callback did not carry the nonce\n"
                "this process generated. Re-run `login`; do not reuse a login\n"
                "URL from a previous attempt."
            )
        elif reason.startswith("bad_status"):
            hint = (
                "\nKite returned an error status in the redirect (usually\n"
                "because the user is not enabled on the app, or the api_key\n"
                "is wrong). Check the developer console."
            )
        print(f"ERROR: callback {reason}{hint}", file=sys.stderr)
        return 1

    return _exchange_and_save(client, cfg, result.request_token, args)


def cmd_profile(args: argparse.Namespace) -> int:
    client = _new_client()
    _emit(client.profile(), args.format, cmd=args.cmd)
    return 0


def cmd_session(args: argparse.Namespace) -> int:
    data = load_session()
    if not data:
        print("No session cached at", DEFAULT_SESSION_PATH)
        return 1
    login_time_str = data.get("login_time") or ""
    try:
        login_time = datetime.fromisoformat(login_time_str)
    except ValueError:
        login_time = None
    expires_hint = None
    if login_time:
        tomorrow_6am = (login_time + timedelta(days=1)).replace(hour=6, minute=0, second=0, microsecond=0)
        if login_time.hour >= 6:
            expires_hint = tomorrow_6am.isoformat()
        else:
            expires_hint = login_time.replace(hour=6, minute=0, second=0, microsecond=0).isoformat()
    out = {
        "user_id": data.get("user_id"),
        "user_name": data.get("user_name"),
        "broker": data.get("broker"),
        "login_time": login_time_str,
        "expires_approx_ist": expires_hint,
        "access_token_present": bool(data.get("access_token")),
    }
    _emit(out, args.format, cmd=args.cmd)
    return 0


def cmd_health(args: argparse.Namespace) -> int:
    """End-to-end health check: session, profile, margins, market data.

    Exits 0 if all checks pass, 1 if any fail. Useful for monitoring and
    pre-trade verification.
    """
    checks: list[dict[str, Any]] = []
    overall_ok = True

    def record(name: str, ok: bool, detail: str = "") -> None:
        nonlocal overall_ok
        if not ok:
            overall_ok = False
        checks.append({"check": name, "ok": ok, "detail": detail[:100]})

    # 1. Session file present
    sess = load_session()
    record("session_file", bool(sess), DEFAULT_SESSION_PATH.name if sess else "missing — run `login`")

    if not sess:
        _emit(checks, args.format, cmd=args.cmd)
        return 1

    # 2. Credentials in env
    cfg = KiteConfig.from_env()
    record("credentials", bool(cfg.api_key and cfg.api_secret), "api_key+secret present" if cfg.api_key else "missing")

    # 3. API reachable + session valid
    try:
        client = _new_client()
        profile = client.profile()
        record("api_reachable", True, f"user={profile.get('user_id')}")
    except Exception as exc:
        record("api_reachable", False, f"{type(exc).__name__}: {exc}")
        _emit(checks, args.format, cmd=args.cmd)
        return 1

    # 4. Margins endpoint
    try:
        margins = client.margins(segment="equity")
        avail = margins.get("net", 0)
        record("margins", True, f"equity net ₹{avail:,.0f}")
    except Exception as exc:
        record("margins", False, f"{type(exc).__name__}: {exc}")

    # 5. Market data endpoint (LTP)
    try:
        quote = client.ltp(["NSE:RELIANCE"])
        reliance = (quote or {}).get("NSE:RELIANCE", {})
        ltp = reliance.get("last_price")
        record("market_data", bool(ltp), f"RELIANCE LTP {ltp}")
    except Exception as exc:
        record("market_data", False, f"{type(exc).__name__}: {exc}")

    # 6. Instruments cache
    try:
        path = _instruments_cache_path("NSE")
        record("instruments_cache", path.exists(), str(path))
    except Exception as exc:
        record("instruments_cache", False, str(exc))

    _emit(checks, args.format, cmd=args.cmd)
    return 0 if overall_ok else 1


# =============================================================================
# TAIL-TICKS — read from a buffer written by `stream --buffer-to`
# =============================================================================

def cmd_tail_ticks(args: argparse.Namespace) -> int:
    """Read buffered WebSocket ticks from a file previously populated by
    `kite-algo stream --buffer-to FILE`.

    Output is NDJSON (one tick per line) — same shape as `stream` emits to
    stdout. Supports resume via `--from-seq N` so an agent that crashed at
    sequence N can pick up with sequence N+1. Separate process from the
    live stream; the streamer keeps the file open append-mode while any
    number of tail-ticks readers can consume concurrently.

    `--follow` behaves like `tail -f` — keeps reading as new lines arrive.
    Without `--follow` we return once the current file is exhausted.
    """
    path = Path(args.path)
    if not path.exists():
        print(f"ERROR: no such file: {path}", file=sys.stderr)
        return 1

    def _filter_pass(tick: dict) -> bool:
        if args.symbols:
            want = set(_split_symbols(args.symbols))
            # Ticks may carry tradingsymbol or instrument_token; symbols can
            # be either 'NSE:RELIANCE' or just 'RELIANCE'.
            sym = tick.get("tradingsymbol") or ""
            token = str(tick.get("instrument_token") or "")
            if not (sym in want or token in want):
                return False
        if args.from_seq is not None:
            seq = tick.get("_seq", 0)
            if seq < args.from_seq:
                return False
        return True

    count = 0
    limit = args.limit or 0

    def _emit_line(line: str) -> None:
        nonlocal count
        try:
            tick = json.loads(line)
        except json.JSONDecodeError:
            return
        if not _filter_pass(tick):
            return
        try:
            print(json.dumps(tick, default=str), flush=True)
        except BrokenPipeError:
            raise
        count += 1

    try:
        with open(path, encoding="utf-8") as f:
            # Drain existing lines.
            for line in f:
                line = line.strip()
                if not line:
                    continue
                _emit_line(line)
                if limit and count >= limit:
                    return 0
            if args.follow:
                while True:
                    where = f.tell()
                    line = f.readline()
                    if not line:
                        time.sleep(args.poll_interval)
                        f.seek(where)
                        continue
                    line = line.strip()
                    if not line:
                        continue
                    _emit_line(line)
                    if limit and count >= limit:
                        return 0
    except BrokenPipeError:
        return 0
    except KeyboardInterrupt:
        return 130

    return 0


# =============================================================================
# ALERTS (raw HTTP — pykiteconnect does not wrap this endpoint)
# =============================================================================

def _new_alerts_client() -> Any:
    """Build an AlertsClient with session credentials + rate limiter."""
    from kite_algo.alerts import AlertsClient
    cfg = KiteConfig.from_env()
    cfg.require_session()
    return AlertsClient(
        api_key=cfg.api_key,
        access_token=cfg.access_token,
        rate_limiter=_RATE_LIMITER,
    )


def cmd_alerts_list(args: argparse.Namespace) -> int:
    client = _new_alerts_client()
    data = client.list(status=args.status, page=args.page, page_size=args.page_size)
    _emit(data, args.format, cmd=args.cmd)
    return 0


def cmd_alerts_get(args: argparse.Namespace) -> int:
    client = _new_alerts_client()
    data = client.get(args.uuid)
    _emit(data, args.format, cmd=args.cmd)
    return 0


def cmd_alerts_create(args: argparse.Namespace) -> int:
    _require_yes(args, "create an alert")
    _require_not_halted("create an alert")
    client = _new_alerts_client()

    payload: dict[str, Any] = {
        "name": args.name,
        "type": args.type,
        "lhs_exchange": args.lhs_exchange,
        "lhs_tradingsymbol": args.lhs_tradingsymbol,
        "lhs_attribute": args.lhs_attribute,
        "operator": args.operator,
        "rhs_type": args.rhs_type,
    }
    if args.rhs_type == "constant":
        payload["rhs_constant"] = args.rhs_constant
    else:
        payload["rhs_exchange"] = args.rhs_exchange
        payload["rhs_tradingsymbol"] = args.rhs_tradingsymbol
        payload["rhs_attribute"] = args.rhs_attribute
    if args.type == "ato":
        if not args.basket_json:
            print("ERROR: type=ato requires --basket-json", file=sys.stderr)
            return 2
        try:
            payload["basket"] = json.loads(args.basket_json)
        except json.JSONDecodeError as exc:
            print(f"ERROR: --basket-json parse failed: {exc}", file=sys.stderr)
            return 2
    try:
        data = client.create(payload)
    except Exception as exc:
        print(f"ERROR: alerts create failed: {redact_text(str(exc))}", file=sys.stderr)
        return 1
    _emit(data, args.format, cmd=args.cmd)
    return 0


def cmd_alerts_modify(args: argparse.Namespace) -> int:
    _require_yes(args, "modify an alert")
    _require_not_halted("modify an alert")
    client = _new_alerts_client()

    payload: dict[str, Any] = {}
    for attr in ("name", "operator", "rhs_type", "lhs_attribute", "type",
                 "lhs_exchange", "lhs_tradingsymbol",
                 "rhs_exchange", "rhs_tradingsymbol", "rhs_attribute"):
        v = getattr(args, attr, None)
        if v is not None:
            payload[attr] = v
    if args.rhs_constant is not None:
        payload["rhs_constant"] = args.rhs_constant
    if args.basket_json is not None:
        try:
            payload["basket"] = json.loads(args.basket_json)
        except json.JSONDecodeError as exc:
            print(f"ERROR: --basket-json parse failed: {exc}", file=sys.stderr)
            return 2

    data = client.modify(args.uuid, payload)
    _emit(data, args.format, cmd=args.cmd)
    return 0


def cmd_alerts_delete(args: argparse.Namespace) -> int:
    _require_yes(args, "delete an alert")
    _require_not_halted("delete an alert")
    client = _new_alerts_client()
    data = client.delete(args.uuid)
    _emit({"deleted": args.uuid, "response": data}, args.format, cmd=args.cmd)
    return 0


def cmd_alerts_history(args: argparse.Namespace) -> int:
    client = _new_alerts_client()
    data = client.history(args.uuid)
    _emit(data, args.format, cmd=args.cmd)
    return 0


# =============================================================================
# RECONCILE — diff local state vs Kite server state
# =============================================================================

def cmd_reconcile(args: argparse.Namespace) -> int:
    """Diff: what we THINK we did (local audit + idempotency store) vs what
    Kite ACTUALLY has (orders/trades). Emits four buckets:

      missing_remotely  — we have a record with a kite_order_id that no
                          longer appears on Kite's orderbook. Possible
                          causes: order already terminal and cleaned up,
                          or cancelled by the user outside our CLI.
      missing_locally   — Kite has an order our local records don't know
                          about. Happens if another agent / device / human
                          placed the order, or if our audit log was
                          truncated.
      mismatched        — local status ≠ live status. Usually just means
                          the live state has advanced past what we recorded.
      orphan_groups     — groups whose legs have fewer entries than
                          `expected_legs` — a crashed multi-leg transaction.

    Exit code: 0 if everything matches, 1 if any drift detected, 5 if the
    session is invalid.
    """
    from kite_algo.audit import iter_entries
    from datetime import date

    since = None
    if args.since:
        try:
            since = date.fromisoformat(args.since)
        except ValueError:
            print(f"ERROR: --since must be YYYY-MM-DD: {args.since!r}",
                  file=sys.stderr)
            return 2

    # 1. Gather local view: every successful place we've logged that
    #    produced a kite_order_id.
    local_orders: dict[str, dict] = {}
    for entry in iter_entries(since=since, cmd="place"):
        oid = entry.get("kite_order_id")
        if oid:
            local_orders[str(oid)] = entry

    # 2. Also look at the idempotency store for any `place` rows that
    #    completed — these are the most recent source of truth for
    #    kite_order_id since they survive crashes between API-return and
    #    audit-write.
    from kite_algo.idempotency import IdempotencyStore
    idem = IdempotencyStore()
    try:
        import sqlite3
        with idem._conn() as c:
            for row in c.execute(
                "SELECT kite_order_id, key, cmd, result_json FROM writes "
                "WHERE cmd='place' AND kite_order_id IS NOT NULL",
            ).fetchall():
                oid = str(row[0])
                local_orders.setdefault(oid, {
                    "kite_order_id": oid,
                    "source": "idempotency",
                    "idempotency_key": row[1],
                })
    except Exception as exc:
        log.warning("idempotency snapshot failed: %s", redact_text(str(exc)))

    # 3. Fetch Kite's live state.
    if getattr(args, "skip_kite", False):
        kite_orders: list[dict] = []
    else:
        try:
            client = _new_client()
            kite_orders = list(client.orders() or [])
        except Exception as exc:
            cls = type(exc).__name__
            # Session errors are the common case — make the exit code
            # reflect that distinctly.
            if cls in ("TokenException", "KiteSessionError"):
                print(f"ERROR: session invalid: {redact_text(str(exc))}",
                      file=sys.stderr)
                return 5
            raise

    remote_map: dict[str, dict] = {
        str(o.get("order_id")): o for o in kite_orders
    }

    missing_remotely = [
        {"order_id": oid, "local": local_orders[oid]}
        for oid in local_orders
        if oid not in remote_map
    ]
    missing_locally = [
        {"order_id": oid, "status": o.get("status"),
         "tradingsymbol": o.get("tradingsymbol"),
         "transaction_type": o.get("transaction_type"),
         "quantity": o.get("quantity"),
         "tag": o.get("tag")}
        for oid, o in remote_map.items()
        if oid not in local_orders
    ]
    mismatched = []
    for oid, local in local_orders.items():
        if oid not in remote_map:
            continue
        live = remote_map[oid]
        live_status = live.get("status")
        # Local view carries the status at the time of the write. Live status
        # advancing (OPEN → COMPLETE) is expected and benign; we only flag
        # when both sides carry terminal statuses that differ.
        local_status = None
        if isinstance(local.get("args"), dict):
            # The audit stores the request, not the result status; so we
            # can only compare if the idempotency row had a stored result.
            pass
        if local.get("result_json"):
            try:
                local_status = (
                    (__import__('json').loads(local["result_json"]) or {})
                    .get("final_status")
                )
            except Exception:
                pass
        if local_status and live_status and local_status != live_status:
            if (local_status in ("COMPLETE", "REJECTED", "CANCELLED")
                    and live_status in ("COMPLETE", "REJECTED", "CANCELLED")
                    and local_status != live_status):
                mismatched.append({
                    "order_id": oid,
                    "local_status": local_status,
                    "live_status": live_status,
                })

    # 4. Orphan groups — started but have fewer actual members than expected.
    from kite_algo.groups import GroupStore
    store = GroupStore()
    orphan_groups = []
    for g in store.list_active():
        members = store.members(g.id)
        if g.expected_legs is not None and len(members) < g.expected_legs:
            orphan_groups.append({
                "group_id": g.id,
                "name": g.name,
                "expected_legs": g.expected_legs,
                "actual_legs": len(members),
                "age_ms": int(time.time() * 1000) - g.created_at_ms,
            })

    drift = bool(missing_remotely or missing_locally or mismatched or orphan_groups)

    out = {
        "clean": not drift,
        "missing_remotely": missing_remotely,
        "missing_locally": missing_locally,
        "mismatched": mismatched,
        "orphan_groups": orphan_groups,
        "totals": {
            "local_orders": len(local_orders),
            "remote_orders": len(remote_map),
            "missing_remotely": len(missing_remotely),
            "missing_locally": len(missing_locally),
            "mismatched": len(mismatched),
            "orphan_groups": len(orphan_groups),
        },
    }
    _emit(out, args.format, cmd=args.cmd)
    return 0 if not drift else 1


# =============================================================================
# GROUPS — multi-leg transactions
# =============================================================================

def cmd_group_start(args: argparse.Namespace) -> int:
    """Begin a new multi-leg transaction. Returns the group_id an agent
    attaches to subsequent `place --group-id G` calls.
    """
    from kite_algo.groups import GroupStore
    store = GroupStore()
    g = store.start(name=args.name, expected_legs=args.legs)
    _emit({
        "group_id": g.id, "name": g.name,
        "expected_legs": g.expected_legs,
        "created_at_ms": g.created_at_ms,
    }, args.format, cmd=args.cmd)
    return 0


def cmd_group_status(args: argparse.Namespace) -> int:
    """Show a group + all its legs + their live Kite order state.

    For each leg we look up the order on Kite and report status +
    filled_quantity. Helps agents reconcile which legs landed after a
    crash or flaky network.
    """
    from kite_algo.groups import GroupStore
    store = GroupStore()
    g = store.get(args.group_id)
    if g is None:
        print(f"ERROR: group not found: {args.group_id}", file=sys.stderr)
        return 1

    members = store.members(args.group_id)

    client = None
    order_states: dict[str, dict] = {}
    if members and not getattr(args, "skip_kite", False):
        try:
            client = _new_client()
            # One orders() call, index by order_id — avoids N round-trips.
            orders = client.orders() or []
            order_states = {
                str(o.get("order_id")): o for o in orders
            }
        except Exception as exc:
            log.warning("group-status could not fetch live orders: %s",
                        redact_text(str(exc)))

    leg_rows = []
    for m in members:
        live = order_states.get(str(m.order_id), {})
        leg_rows.append({
            "order_id": m.order_id,
            "leg_name": m.leg_name,
            "tag": m.tag,
            "status": live.get("status"),
            "filled_quantity": live.get("filled_quantity"),
            "pending_quantity": live.get("pending_quantity"),
            "average_price": live.get("average_price"),
        })

    out = {
        "group_id": g.id,
        "name": g.name,
        "expected_legs": g.expected_legs,
        "actual_legs": len(members),
        "closed": g.closed_at_ms is not None,
        "legs": leg_rows,
    }
    _emit(out, args.format, cmd=args.cmd)
    return 0


def cmd_group_cancel(args: argparse.Namespace) -> int:
    """Cancel every still-open leg of a group. --yes required."""
    _require_yes(args, "cancel all open legs in a group")
    _require_not_halted("cancel all open legs in a group")

    from kite_algo.groups import GroupStore
    store = GroupStore()
    g = store.get(args.group_id)
    if g is None:
        print(f"ERROR: group not found: {args.group_id}", file=sys.stderr)
        return 1

    members = store.members(args.group_id)
    if not members:
        _emit({"group_id": g.id, "cancelled": [], "failed": []},
              args.format, cmd=args.cmd)
        return 0

    client = _new_client()
    try:
        live_orders = {str(o.get("order_id")): o for o in (client.orders() or [])}
    except Exception as exc:
        print(f"ERROR: could not fetch orderbook: {redact_text(str(exc))}",
              file=sys.stderr)
        return 1

    cancelled: list[str] = []
    failed: list[dict] = []
    for m in members:
        live = live_orders.get(str(m.order_id))
        if not live or live.get("status") not in ("OPEN", "TRIGGER PENDING"):
            continue
        variety = live.get("variety") or "regular"
        try:
            client.cancel_order(variety=variety, order_id=m.order_id)
            cancelled.append(m.order_id)
        except Exception as exc:
            failed.append({
                "order_id": m.order_id,
                "reason": redact_text(str(exc))[:200],
            })

    # Mark the group closed since we've attempted to flatten it.
    store.close(g.id)

    _emit({
        "group_id": g.id,
        "cancelled": cancelled,
        "failed": failed,
        "total_cancelled": len(cancelled),
        "total_failed": len(failed),
    }, args.format, cmd=args.cmd)
    return 0 if not failed else 1


# =============================================================================
# EVENTS — tail local audit log
# =============================================================================

def cmd_events(args: argparse.Namespace) -> int:
    """Read the SEBI-compliant audit log under data/audit/*.jsonl.

    This is where agents reconstruct their own history after a crash,
    verify that a given request_id actually ran, or diff their assumptions
    against what really happened. No API call — purely local.

    Filters:
      --since YYYY-MM-DD    lower date bound (IST)
      --until YYYY-MM-DD    upper date bound (IST)
      --cmd NAME            only entries for this subcommand
      --outcome ok|error    filter on exit code (0 vs non-zero)
      --tail N              only the most recent N matching entries
    """
    from datetime import date
    from kite_algo.audit import iter_entries, tail as audit_tail

    def _parse_day(raw: str | None) -> date | None:
        if not raw:
            return None
        try:
            return date.fromisoformat(raw)
        except ValueError as exc:
            raise SystemExit(f"--since/--until must be YYYY-MM-DD: {raw!r} ({exc})")

    since = _parse_day(args.since)
    until = _parse_day(args.until)

    if args.tail is not None and args.tail > 0:
        entries = audit_tail(
            args.tail, cmd=args.cmd_filter, outcome=args.outcome,
        )
        # Even with --tail we still respect date bounds.
        if since or until:
            entries = [
                e for e in entries
                if (not since or date.fromisoformat(e["ts"][:10]) >= since)
                and (not until or date.fromisoformat(e["ts"][:10]) <= until)
            ]
    else:
        entries = list(iter_entries(
            since=since, until=until,
            cmd=args.cmd_filter, outcome=args.outcome,
        ))

    _emit(entries, args.format, cmd=args.cmd)
    return 0


# =============================================================================
# WATCH — poll-until-condition
# =============================================================================

def cmd_watch(args: argparse.Namespace) -> int:
    """Poll a named resource every N seconds; exit 0 with the snapshot when
    `--until EXPR` evaluates True; exit 124 if the deadline elapses first.

    Agents use this to fold "poll with condition" into one atomic call
    rather than spawning and coordinating a polling loop. Much cheaper than
    WebSockets for turn-based agents.

    Resource types:
      quote NSE:RELIANCE → {last_price, volume, oi, ...}
      ltp   NSE:RELIANCE → {last_price}
      ohlc  NSE:RELIANCE → {last_price, open, high, low, close}
      order ORD_ID       → latest order_history entry
    """
    from kite_algo.exit_codes import TIMEOUT
    from kite_algo.watch_expr import UnsafeExpression, evaluate

    resource = args.resource
    interval = max(0.1, float(args.every))
    timeout = float(args.timeout)
    deadline = time.monotonic() + timeout if timeout > 0 else float("inf")

    client = _new_client()

    def _fetch() -> dict:
        if resource == "quote":
            data = client.quote([args.symbol]) or {}
            row = data.get(args.symbol, {}) or {}
            # Flatten ohlc into the top level for convenient expressions.
            ohlc = row.get("ohlc", {}) or {}
            return {
                "symbol": args.symbol,
                "last_price": row.get("last_price"),
                "volume": row.get("volume"),
                "oi": row.get("oi"),
                "open": ohlc.get("open"),
                "high": ohlc.get("high"),
                "low": ohlc.get("low"),
                "close": ohlc.get("close"),
                "avg_price": row.get("average_price"),
                "net_change": row.get("net_change"),
            }
        if resource == "ltp":
            data = client.ltp([args.symbol]) or {}
            row = data.get(args.symbol, {}) or {}
            return {"symbol": args.symbol, "last_price": row.get("last_price")}
        if resource == "ohlc":
            data = client.ohlc([args.symbol]) or {}
            row = data.get(args.symbol, {}) or {}
            ohlc = row.get("ohlc", {}) or {}
            return {
                "symbol": args.symbol,
                "last_price": row.get("last_price"),
                "open": ohlc.get("open"),
                "high": ohlc.get("high"),
                "low": ohlc.get("low"),
                "close": ohlc.get("close"),
            }
        if resource == "order":
            history = client.order_history(args.order_id) or []
            if not history:
                return {"order_id": args.order_id, "status": None}
            # Parse-then-sort for correctness (W1.2).
            history = sorted(
                history, key=lambda h: _parse_order_timestamp(h.get("order_timestamp")),
            )
            last = history[-1]
            return {
                "order_id": args.order_id,
                "status": last.get("status"),
                "filled_quantity": last.get("filled_quantity"),
                "average_price": last.get("average_price"),
                "status_message": last.get("status_message"),
            }
        raise SystemExit(f"unknown watch resource: {resource!r}")

    # Sanity-check the expression before we start the loop — invalid expr
    # shouldn't waste API quota or tick-by-tick time.
    try:
        # Dry-eval with an empty snapshot. May return anything; we only
        # care that the AST parses + nodes are allowed.
        evaluate(args.until, {})
    except UnsafeExpression as exc:
        print(f"ERROR: unsafe --until expression: {exc}", file=sys.stderr)
        return 2
    except SyntaxError as exc:
        print(f"ERROR: --until expression does not parse: {exc}", file=sys.stderr)
        return 2
    except Exception:
        # Tolerant — some expressions only succeed when a field is bound.
        pass

    last_snapshot: dict = {}
    polls = 0
    while time.monotonic() < deadline:
        try:
            last_snapshot = _fetch()
        except Exception as exc:
            # Transient error: log, sleep, retry — don't abort the watch.
            log.warning("watch poll failed: %s", redact_text(str(exc)))
        polls += 1
        try:
            if evaluate(args.until, last_snapshot):
                out = {
                    "matched": True,
                    "polls": polls,
                    "snapshot": last_snapshot,
                    "expression": args.until,
                }
                _emit(out, args.format, cmd=args.cmd)
                return 0
        except Exception as exc:
            log.debug("watch eval failed (likely missing field): %s", exc)
        # Sleep until the next poll or the deadline, whichever comes first.
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(interval, remaining))

    out = {
        "matched": False,
        "polls": polls,
        "snapshot": last_snapshot,
        "expression": args.until,
        "reason": "timeout",
    }
    _emit(out, args.format, cmd=args.cmd)
    return TIMEOUT


# =============================================================================
# STATUS — single-blob state introspection
# =============================================================================

def cmd_status(args: argparse.Namespace) -> int:
    """Return one JSON blob summarising everything an agent needs to decide
    what to do next: session validity, market hours per exchange, rate-limit
    headroom, account state, live-window enforcement, halt state.

    Designed for agent loops that start every cycle with "what is the state
    of the world?" — one call, one blob, stable shape.

    Does not fail if parts are unavailable (e.g. no live broker). Each
    section is independent; a section with no data shows null or a
    plausible default and moves on.
    """
    from datetime import datetime, timedelta
    from kite_algo.market_rules import (
        IST, is_market_open, market_close_time,
        mis_cutoff_for, now_ist,
        safe_login_time_today, in_token_rotation_window,
    )

    nowt = now_ist()

    # ---- Session state --------------------------------------------------
    cfg = KiteConfig.from_env()
    session_block: dict[str, Any] = {
        "valid": bool(cfg.access_token),
        "user_id": cfg.user_id or None,
        "in_rotation_window": in_token_rotation_window(nowt),
    }
    sess = load_session()
    if sess.get("login_time"):
        try:
            lt = datetime.fromisoformat(sess["login_time"])
            # Next rotation window starts 06:45 IST next day.
            tomorrow = (lt + timedelta(days=1)).replace(
                hour=6, minute=45, second=0, microsecond=0, tzinfo=IST,
            )
            session_block["expires_approx_ist"] = tomorrow.isoformat(timespec="seconds")
            session_block["login_time"] = sess["login_time"]
        except ValueError:
            pass

    # ---- Market hours per exchange -------------------------------------
    market_block: dict[str, Any] = {
        "ist_now": nowt.isoformat(timespec="seconds"),
        "weekday": nowt.strftime("%A"),
    }
    for exch in ("NSE", "BSE", "NFO", "BFO", "MCX", "CDS", "BCD"):
        market_block[f"{exch.lower()}_open"] = is_market_open(exch, nowt)

    # ---- Rate limiter headroom -----------------------------------------
    lim = _RATE_LIMITER
    # Non-destructive peek at remaining tokens / window capacity.
    import time as _time
    # Refill the general/historical/quote/orders_sec buckets without
    # acquiring (no mutation). We can't read _tokens safely without the
    # lock, but it's cheap to grab.
    def _peek(bucket) -> float:
        with bucket._cond:
            # Refill math.
            now = _time.monotonic()
            elapsed = now - bucket._last
            return min(bucket.capacity, bucket._tokens + elapsed * bucket.rate)
    rate_block = {
        "general_tokens_remaining": round(_peek(lim.general), 2),
        "historical_tokens_remaining": round(_peek(lim.historical), 2),
        "quote_tokens_remaining": round(_peek(lim.quote), 2),
        "orders_sec_tokens_remaining": round(_peek(lim.orders_sec), 2),
        "orders_per_min_used": len(lim.orders_min._events),
        "orders_per_min_cap": lim.orders_min.max,
        "orders_per_day_used": len(lim.orders_day._events),
        "orders_per_day_cap": lim.orders_day.max,
    }

    # ---- Account snapshot ----------------------------------------------
    account_block: dict[str, Any] = {
        "available": None,
        "open_orders": None,
        "open_positions": None,
        "holdings_count": None,
        "day_m2m": None,
    }
    # Only try reading account state if we have a valid session AND the
    # caller hasn't passed --skip-account.
    if session_block["valid"] and not getattr(args, "skip_account", False):
        try:
            client = _new_client()
            margins = client.margins(segment="equity") or {}
            account_block["available"] = float(
                (margins.get("available") or {}).get("live_balance")
                or (margins.get("available") or {}).get("cash")
                or 0
            )
            orders = client.orders() or []
            account_block["open_orders"] = sum(
                1 for o in orders
                if o.get("status") in ("OPEN", "TRIGGER PENDING")
            )
            pos = client.positions() or {}
            net_positions = pos.get("net", [])
            account_block["open_positions"] = sum(
                1 for p in net_positions if int(p.get("quantity") or 0) != 0
            )
            day_positions = pos.get("day", [])
            account_block["day_m2m"] = round(
                sum(float(p.get("m2m") or 0) for p in day_positions), 2,
            )
            account_block["holdings_count"] = len(client.holdings() or [])
        except Exception as exc:
            account_block["error"] = f"{type(exc).__name__}: {redact_text(str(exc))}"

    # ---- Live-window + MIS cutoff --------------------------------------
    live_block: dict[str, Any] = {
        "nse_mis_cutoff_ist": mis_cutoff_for("NSE").isoformat(),
        "mcx_mis_cutoff_ist": mis_cutoff_for("MCX").isoformat(),
        "safe_login_after_ist": safe_login_time_today(nowt).isoformat(timespec="seconds"),
    }

    # ---- Halt --------------------------------------------------
    from kite_algo.halt import read_halt
    halt_state = read_halt()
    halt_block = {
        "is_halted": halt_state is not None,
    }
    if halt_state is not None:
        halt_block.update(halt_state.to_dict())

    out = {
        "session": session_block,
        "market": market_block,
        "rate_limit": rate_block,
        "account": account_block,
        "live_window": live_block,
        "halt": halt_block,
    }
    _emit(out, args.format, cmd=args.cmd)
    return 0


# =============================================================================
# TIME — clocks, market open/close, expiries (pure-local, no API call)
# =============================================================================

def cmd_time(args: argparse.Namespace) -> int:
    """Emit all the clocks an agent needs to plan actions:
    IST now, UTC now, next token rotation window, per-exchange open/close
    times, and next weekly expiry.  No API call.
    """
    from datetime import datetime, timezone
    from kite_algo.market_rules import (
        IST, in_token_rotation_window,
        market_close_time, market_open_time,
        mis_cutoff_for, next_weekly_expiry, now_ist,
        safe_login_time_today,
    )

    nowt = now_ist()
    ist_date = nowt.date()

    out = {
        "ist_now": nowt.isoformat(timespec="seconds"),
        "utc_now": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        "ist_date": ist_date.isoformat(),
        "weekday": nowt.strftime("%A"),
        "token_rotation": {
            "window_ist": "06:45 - 07:30",
            "in_window_now": in_token_rotation_window(nowt),
            "next_safe_login": safe_login_time_today(nowt).isoformat(timespec="seconds"),
        },
        "market_hours_ist": {
            "NSE": {
                "open": market_open_time("NSE").isoformat(),
                "close": market_close_time("NSE").isoformat(),
            },
            "BSE": {
                "open": market_open_time("BSE").isoformat(),
                "close": market_close_time("BSE").isoformat(),
            },
            "NFO": {
                "open": market_open_time("NFO").isoformat(),
                "close": market_close_time("NFO").isoformat(),
            },
            "MCX": {
                "open": market_open_time("MCX").isoformat(),
                "close": market_close_time("MCX").isoformat(),
            },
            "CDS": {
                "open": market_open_time("CDS").isoformat(),
                "close": market_close_time("CDS").isoformat(),
            },
        },
        "mis_squareoff_ist": {
            "equity": mis_cutoff_for("NSE").isoformat(),
            "mcx": mis_cutoff_for("MCX").isoformat(),
        },
        "next_weekly_expiry": {
            "nse": (next_weekly_expiry("NSE", ist_date) or "").__str__() or None,
            "bse": (next_weekly_expiry("BSE", ist_date) or "").__str__() or None,
        },
    }
    _emit(out, args.format, cmd=args.cmd)
    return 0


# =============================================================================
# KILL SWITCH (halt / resume)
# =============================================================================

def cmd_halt(args: argparse.Namespace) -> int:
    """Write the HALTED sentinel — every subsequent write command refuses
    until the sentinel is removed. Safe to call while already halted (the
    new reason / expires-in overwrite the existing sentinel).
    """
    expires_seconds = None
    if args.expires_in:
        try:
            expires_seconds = parse_duration(args.expires_in)
        except ValueError as exc:
            print(f"ERROR: --expires-in: {exc}", file=sys.stderr)
            return 2
    state = write_halt(
        reason=args.reason,
        by=(args.by or os.getenv("KITE_OPERATOR", "operator")),
        expires_in_seconds=expires_seconds,
    )
    out = state.to_dict()
    out["halted"] = True
    _emit(out, args.format, cmd=args.cmd)
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    """Clear the HALTED sentinel. Requires `--confirm-resume` — distinct
    from `--yes` so an agent that accidentally retries `halt --yes` won't
    flip the state back on.
    """
    if not getattr(args, "confirm_resume", False):
        print(
            "ERROR: `resume` requires --confirm-resume. This is intentionally "
            "a different token from --yes to prevent accidental lift of a halt.",
            file=sys.stderr,
        )
        return 2
    cleared = clear_halt()
    _emit({"resumed": cleared}, args.format, cmd=args.cmd)
    return 0


def cmd_logout(args: argparse.Namespace) -> int:
    client = _new_client()
    try:
        client.invalidate_access_token()
    except Exception as exc:
        print(f"WARN: invalidate_access_token failed: {exc}", file=sys.stderr)
    if DEFAULT_SESSION_PATH.exists():
        DEFAULT_SESSION_PATH.unlink()
        print(f"Removed {DEFAULT_SESSION_PATH}", file=sys.stderr)
    return 0


# =============================================================================
# ACCOUNT
# =============================================================================

def cmd_margins(args: argparse.Namespace) -> int:
    client = _new_client()
    if args.segment:
        data = client.margins(segment=args.segment)
    else:
        data = client.margins()
    _emit(data, args.format, cmd=args.cmd)
    return 0


def cmd_holdings(args: argparse.Namespace) -> int:
    client = _new_client()
    rows = client.holdings() or []
    if args.summary:
        _emit(summarize_holdings(rows), args.format, cmd=args.cmd)
        return 0
    rows = project_rows(rows, parse_fields(args.fields))
    _emit(rows, args.format, cmd=args.cmd)
    return 0


def cmd_positions(args: argparse.Namespace) -> int:
    client = _new_client()
    data = client.positions() or {}
    if args.summary:
        _emit(summarize_positions(data), args.format, cmd=args.cmd)
        return 0
    which = args.which or "net"
    rows = data.get(which, [])
    rows = project_rows(rows, parse_fields(args.fields))
    _emit(rows, args.format, cmd=args.cmd)
    return 0


def cmd_convert_position(args: argparse.Namespace) -> int:
    """Convert position product type (e.g. MIS → CNC, NRML → MIS)."""
    _require_yes(args, "convert a position")
    if not getattr(args, "confirm_convert", False):
        raise SystemExit(
            "Refusing to convert-position without --confirm-convert. "
            "This flag is distinct from --yes: unintended conversion can "
            "reallocate margin or strand an intraday position overnight. "
            "--confirm-convert forces explicit acknowledgment."
        )
    _require_not_halted("convert a position")
    client = _new_client()
    try:
        client.convert_position(
            exchange=args.exchange,
            tradingsymbol=args.tradingsymbol,
            transaction_type=args.transaction_type,
            position_type=args.position_type,
            quantity=args.quantity,
            old_product=args.old_product,
            new_product=args.new_product,
        )
    except Exception as exc:
        print(f"ERROR: convert_position failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"converted: {args.tradingsymbol} {args.quantity}x "
        f"{args.old_product}→{args.new_product}",
        file=sys.stderr,
    )
    return 0


def cmd_pnl(args: argparse.Namespace) -> int:
    """Aggregate P&L from positions (net) — day P&L, unrealised, realised."""
    client = _new_client()
    data = client.positions() or {}
    net = data.get("net", [])
    day = data.get("day", [])

    total_pnl = sum(float(p.get("pnl", 0) or 0) for p in net)
    total_m2m = sum(float(p.get("m2m", 0) or 0) for p in day)
    total_realised = sum(float(p.get("realised", 0) or 0) for p in net)
    total_unrealised = sum(float(p.get("unrealised", 0) or 0) for p in net)
    total_day_buy = sum(float(p.get("day_buy_value", 0) or 0) for p in net)
    total_day_sell = sum(float(p.get("day_sell_value", 0) or 0) for p in net)

    out = {
        "net_pnl": round(total_pnl, 2),
        "day_m2m": round(total_m2m, 2),
        "realised": round(total_realised, 2),
        "unrealised": round(total_unrealised, 2),
        "day_buy_value": round(total_day_buy, 2),
        "day_sell_value": round(total_day_sell, 2),
        "open_positions": sum(1 for p in net if int(p.get("quantity", 0) or 0) != 0),
    }
    _emit(out, args.format, cmd=args.cmd)
    return 0


def cmd_portfolio(args: argparse.Namespace) -> int:
    """Combined portfolio view: holdings + open positions with MTM values."""
    client = _new_client()
    holdings = client.holdings() or []
    pos_data = client.positions() or {}
    net_positions = pos_data.get("net", [])

    rows = []
    for h in holdings:
        rows.append({
            "type": "holding",
            "tradingsymbol": h.get("tradingsymbol"),
            "exchange": h.get("exchange"),
            "quantity": h.get("quantity"),
            "avg_price": h.get("average_price"),
            "last_price": h.get("last_price"),
            "pnl": h.get("pnl"),
            "day_change_pct": h.get("day_change_percentage"),
            "product": "CNC",
        })

    for p in net_positions:
        qty = int(p.get("quantity", 0) or 0)
        if qty == 0:
            continue
        rows.append({
            "type": "position",
            "tradingsymbol": p.get("tradingsymbol"),
            "exchange": p.get("exchange"),
            "quantity": qty,
            "avg_price": p.get("average_price"),
            "last_price": p.get("last_price"),
            "pnl": p.get("pnl"),
            "day_change_pct": None,
            "product": p.get("product"),
        })

    _emit(rows, args.format, cmd=args.cmd)
    return 0


def cmd_orders(args: argparse.Namespace) -> int:
    client = _new_client()
    rows = client.orders() or []
    if args.summary:
        _emit(summarize_orders(rows), args.format, cmd=args.cmd)
        return 0
    rows = project_rows(rows, parse_fields(args.fields))
    _emit(rows, args.format, cmd=args.cmd)
    return 0


def cmd_open_orders(args: argparse.Namespace) -> int:
    """Show only OPEN / TRIGGER PENDING orders."""
    client = _new_client()
    orders = client.orders() or []
    open_only = [o for o in orders if o.get("status") in ("OPEN", "TRIGGER PENDING")]
    if args.summary:
        _emit(summarize_orders(open_only), args.format, cmd=args.cmd)
        return 0
    open_only = project_rows(open_only, parse_fields(args.fields))
    _emit(open_only, args.format, cmd=args.cmd)
    return 0


def cmd_trades(args: argparse.Namespace) -> int:
    client = _new_client()
    rows = client.trades() or []
    rows = project_rows(rows, parse_fields(args.fields))
    _emit(rows, args.format, cmd=args.cmd)
    return 0


def cmd_order_history(args: argparse.Namespace) -> int:
    client = _new_client()
    _emit(client.order_history(args.order_id), args.format, cmd=args.cmd)
    return 0


def cmd_order_trades(args: argparse.Namespace) -> int:
    client = _new_client()
    _emit(client.order_trades(args.order_id), args.format, cmd=args.cmd)
    return 0


# =============================================================================
# QUOTES
# =============================================================================

def _split_symbols(raw: str) -> list[str]:
    return [s.strip() for s in raw.split(",") if s.strip()]


# Kite's `/quote`, `/ohlc`, `/ltp` cap at 500 symbols per call. Above that
# the API responds with a 400 InputException. Historically users had to
# pre-chunk — we do it transparently.
# Source: https://kite.trade/docs/connect/v3/market-quotes/
QUOTE_BATCH_SIZE = 500


def _batched_quote_call(
    client: Any,
    method_name: str,
    symbols: list[str],
    *,
    batch_size: int = QUOTE_BATCH_SIZE,
) -> dict:
    """Call `client.{method_name}(symbols)` in ≤batch_size chunks and merge
    the returned dicts.

    Rate-limiting: the `quote` bucket (1 req/s) enforces pacing automatically
    via `RateLimitedKiteClient` when the wrapped client is used. Batching
    just keeps each call payload under Kite's 500-symbol ceiling.

    Merging: Kite returns `{symbol: quote}`. When batches don't overlap (the
    common case) merging is lossless. For overlapping batches later wins —
    but we deduplicate the input first so overlap is impossible.
    """
    if not symbols:
        return {}
    # Deduplicate while preserving order so the agent's expected ordering is
    # respected in table output.
    seen: set[str] = set()
    uniq: list[str] = []
    for s in symbols:
        if s not in seen:
            seen.add(s)
            uniq.append(s)

    fn = getattr(client, method_name)
    result: dict = {}
    for i in range(0, len(uniq), batch_size):
        chunk = uniq[i:i + batch_size]
        resp = fn(chunk) or {}
        if isinstance(resp, dict):
            result.update(resp)
        else:
            # Some methods might theoretically return a list — fall through.
            result[f"_batch_{i}"] = resp
    return result


def cmd_ltp(args: argparse.Namespace) -> int:
    client = _new_client()
    symbols = _split_symbols(args.symbols)
    data = _batched_quote_call(client, "ltp", symbols)
    rows = [
        {"symbol": k, "instrument_token": v.get("instrument_token"), "last_price": v.get("last_price")}
        for k, v in (data or {}).items()
    ]
    _emit(rows, args.format, cmd=args.cmd)
    return 0


def cmd_ohlc(args: argparse.Namespace) -> int:
    client = _new_client()
    symbols = _split_symbols(args.symbols)
    data = _batched_quote_call(client, "ohlc", symbols)
    rows = []
    for k, v in (data or {}).items():
        ohlc = v.get("ohlc", {}) or {}
        rows.append({
            "symbol": k,
            "instrument_token": v.get("instrument_token"),
            "last_price": v.get("last_price"),
            "open": ohlc.get("open"),
            "high": ohlc.get("high"),
            "low": ohlc.get("low"),
            "close": ohlc.get("close"),
        })
    _emit(rows, args.format, cmd=args.cmd)
    return 0


def cmd_quote(args: argparse.Namespace) -> int:
    client = _new_client()
    symbols = _split_symbols(args.symbols)
    data = _batched_quote_call(client, "quote", symbols)
    if args.flat:
        rows = []
        for k, v in (data or {}).items():
            depth = v.get("depth", {}) or {}
            buy0 = (depth.get("buy") or [{}])[0] or {}
            sell0 = (depth.get("sell") or [{}])[0] or {}
            rows.append({
                "symbol": k,
                "last_price": v.get("last_price"),
                "bid": buy0.get("price"),
                "bid_qty": buy0.get("quantity"),
                "ask": sell0.get("price"),
                "ask_qty": sell0.get("quantity"),
                "volume": v.get("volume"),
                "avg_price": v.get("average_price"),
                "last_quantity": v.get("last_quantity"),
                "oi": v.get("oi"),
                "net_change": v.get("net_change"),
            })
        _emit(rows, args.format, cmd=args.cmd)
    else:
        _emit(data, args.format, cmd=args.cmd)
    return 0


def cmd_depth(args: argparse.Namespace) -> int:
    """Market depth (5-level order book) for a single symbol."""
    client = _new_client()
    symbols = _split_symbols(args.symbols)
    data = client.quote(symbols)
    for sym, v in (data or {}).items():
        depth = v.get("depth", {}) or {}
        buys = depth.get("buy", []) or []
        sells = depth.get("sell", []) or []
        print(f"\n=== {sym}  LTP: {v.get('last_price')} ===", file=sys.stderr)
        rows = []
        for i in range(max(len(buys), len(sells))):
            b = buys[i] if i < len(buys) else {}
            s = sells[i] if i < len(sells) else {}
            rows.append({
                "bid_orders": b.get("orders"),
                "bid_qty": b.get("quantity"),
                "bid": b.get("price"),
                "ask": s.get("price"),
                "ask_qty": s.get("quantity"),
                "ask_orders": s.get("orders"),
            })
        _emit(rows, args.format, cmd=args.cmd)
    return 0


def cmd_stream(args: argparse.Namespace) -> int:
    """Live WebSocket tick stream via KiteTicker.

    Cross-platform stopping: uses threading.Timer + Event (not SIGALRM
    which is POSIX-only and re-entrancy-unsafe with Twisted's reactor).
    Token-related errors set the stop event and cause a non-zero exit
    (important — tokens rotate at ~6am IST, and we do NOT want a stream
    to silently emit zero ticks for hours after expiry).
    """
    KiteTicker = _import_kiteticker()
    cfg = KiteConfig.from_env()
    cfg.require_session()

    client = _new_client()

    # Resolve symbols → instrument tokens
    tokens: list[int] = []
    if args.tokens:
        tokens = [int(t.strip()) for t in args.tokens.split(",") if t.strip()]
    elif args.symbols:
        syms = _split_symbols(args.symbols)
        ltp_data = client.ltp(syms)
        for sym, v in (ltp_data or {}).items():
            tok = v.get("instrument_token")
            if tok:
                tokens.append(int(tok))
            else:
                log.warning("could not resolve token for %s", sym)
    if not tokens:
        print("ERROR: no instrument tokens. Use --symbols or --tokens.", file=sys.stderr)
        return 1

    mode_map = {"ltp": "ltp", "quote": "quote", "full": "full"}
    subscribe_mode = mode_map.get(args.mode, "full")
    duration = float(args.duration)

    kws = KiteTicker(
        cfg.api_key,
        cfg.access_token,
        reconnect=True,
        reconnect_max_tries=args.reconnect_max_tries,
        reconnect_max_delay=args.reconnect_max_delay,
    )

    stop_event = threading.Event()
    exit_code = 0

    def _shutdown(code: int = 0) -> None:
        nonlocal exit_code
        exit_code = code
        stop_event.set()
        try:
            kws.close()
        except Exception as exc:
            log.debug("ws close raised: %s", exc)

    def _looks_like_token_error(*parts: Any) -> bool:
        blob = " ".join(str(p) for p in parts).lower()
        return any(
            marker in blob
            for marker in ("token", "401", "403", "invalidaccesstoken", "access token")
        )

    # Stream buffering: if --buffer-to is given, every tick is also
    # appended to that file as NDJSON. A separate `tail-ticks` command
    # reads from the same file, letting an agent consume aggregated
    # state rather than holding an open WebSocket.
    #
    # Each tick carries a monotonically increasing sequence number, so
    # `tail-ticks --from SEQ` can resume without duplication.
    buffer_file: Any = None
    if getattr(args, "buffer_to", None):
        buf_path = Path(args.buffer_to)
        buf_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        buffer_file = open(buf_path, "a", encoding="utf-8", buffering=1)  # line-buffered

    seq_counter = [0]

    def on_ticks(ws: Any, ticks: list[dict]) -> None:
        for tick in ticks:
            seq_counter[0] += 1
            payload = _to_jsonable(tick)
            if isinstance(payload, dict):
                payload.setdefault("_seq", seq_counter[0])
                payload.setdefault("_ts_epoch_ms", int(time.time() * 1000))
            line = json.dumps(payload, default=str)
            try:
                print(line, flush=True)
            except BrokenPipeError:
                # stdout closed → shut down, but keep writing to the buffer
                # file if one is attached so downstream consumers still get
                # a full record.
                _shutdown(0)
            if buffer_file is not None:
                try:
                    buffer_file.write(line + "\n")
                except Exception as exc:
                    log.warning("buffer write failed: %s", redact_text(str(exc)))

    def on_connect(ws: Any, response: Any) -> None:
        log.info("ws connected — subscribing %d tokens mode=%s", len(tokens), subscribe_mode)
        ws.subscribe(tokens)
        ws.set_mode(subscribe_mode, tokens)

    def on_close(ws: Any, code: Any, reason: Any) -> None:
        log.warning("ws closed: code=%s reason=%s", code, reason)
        if _looks_like_token_error(code, reason):
            log.error("token-related close — triggering shutdown (exit 2)")
            _shutdown(2)

    def on_error(ws: Any, code: Any, reason: Any) -> None:
        log.error("ws error: code=%s reason=%s", code, reason)
        if _looks_like_token_error(code, reason):
            log.error("token-related error — triggering shutdown (exit 2)")
            _shutdown(2)

    def on_reconnect(ws: Any, attempts: int) -> None:
        log.warning("ws reconnecting (attempt %d)", attempts)

    def on_noreconnect(ws: Any) -> None:
        log.error("ws gave up reconnecting after %d tries", args.reconnect_max_tries)
        _shutdown(2)

    def on_order_update(ws: Any, data: dict) -> None:
        try:
            print(
                json.dumps({"_type": "order_update", **_to_jsonable(data)}, default=str),
                flush=True,
            )
        except BrokenPipeError:
            _shutdown(0)

    kws.on_ticks = on_ticks
    kws.on_connect = on_connect
    kws.on_close = on_close
    kws.on_error = on_error
    kws.on_reconnect = on_reconnect
    kws.on_noreconnect = on_noreconnect
    if args.order_updates:
        kws.on_order_update = on_order_update

    # Duration stop: use a Timer (portable, re-entrancy-safe, supports float).
    timer: threading.Timer | None = None
    if duration > 0:
        def _elapsed() -> None:
            log.info("%.1fs elapsed — disconnecting", duration)
            _shutdown(0)
        timer = threading.Timer(duration, _elapsed)
        timer.daemon = True
        timer.start()

    try:
        # threaded=True spawns Twisted in a background thread; the main
        # thread then waits on the Event.
        kws.connect(threaded=True)
        while not stop_event.is_set():
            if stop_event.wait(timeout=1.0):
                break
    except KeyboardInterrupt:
        _shutdown(0)
    finally:
        if timer:
            timer.cancel()
        try:
            kws.close()
        except Exception:
            pass
        if buffer_file is not None:
            try:
                buffer_file.close()
            except Exception:
                pass

    return exit_code


# =============================================================================
# HISTORICAL / INSTRUMENTS
# =============================================================================

# Per-interval max lookback-per-request. Forum-sourced (not in docs), stable
# since at least 2023. Exceeding these yields an InputException "date range is
# too large". We auto-chunk rather than surface the error.
# Source: Kite forum 8899 + confirmations on community issue trackers.
HISTORICAL_MAX_LOOKBACK_DAYS = {
    "minute": 60,
    "3minute": 100,
    "5minute": 100,
    "10minute": 100,
    "15minute": 200,
    "30minute": 200,
    "60minute": 400,
    "day": 2000,
}


def _fetch_historical_chunked(
    client: Any,
    *,
    token: int,
    from_d: datetime,
    to_d: datetime,
    interval: str,
    continuous: bool,
    oi: bool,
) -> list[dict]:
    """Fetch `historical_data` in chunks that respect Kite's per-call lookback
    cap, stitching the results together. No call spans more than the interval's
    documented maximum.

    If the interval is unknown, falls back to a single call (let Kite decide).
    Rate limiting is enforced by `RateLimitedKiteClient`'s historical bucket —
    each chunk waits ~3/s.
    """
    max_days = HISTORICAL_MAX_LOOKBACK_DAYS.get(interval)
    if max_days is None:
        return client.historical_data(
            instrument_token=token,
            from_date=from_d, to_date=to_d,
            interval=interval, continuous=continuous, oi=oi,
        ) or []

    chunks: list[dict] = []
    # Walk forward from `from_d` in max_days windows.
    cursor = from_d
    window = timedelta(days=max_days)
    while cursor < to_d:
        end = min(cursor + window, to_d)
        # A single-day call uses inclusive intervals; no off-by-one adjustment
        # needed — Kite returns bars with timestamp in [from_date, to_date].
        bars = client.historical_data(
            instrument_token=token,
            from_date=cursor, to_date=end,
            interval=interval, continuous=continuous, oi=oi,
        ) or []
        chunks.extend(bars)
        if end >= to_d:
            break
        # Next window starts the next second to avoid duplicating boundary bar.
        cursor = end + timedelta(seconds=1)
    return chunks


def cmd_history(args: argparse.Namespace) -> int:
    client = _new_client()
    if args.instrument_token is None:
        token = _resolve_token(client, args.symbol, args.exchange)
        if token is None:
            print(f"ERROR: could not resolve instrument token for {args.exchange}:{args.symbol}", file=sys.stderr)
            return 1
    else:
        token = args.instrument_token

    to_d = datetime.fromisoformat(args.to) if args.to else datetime.now()
    from_d = datetime.fromisoformat(args.from_) if args.from_ else (to_d - timedelta(days=args.days))

    bars = _fetch_historical_chunked(
        client,
        token=token, from_d=from_d, to_d=to_d,
        interval=args.interval,
        continuous=args.continuous, oi=args.oi,
    )
    rows = [
        {
            "date": b["date"].isoformat() if hasattr(b["date"], "isoformat") else str(b["date"]),
            "open": b.get("open"),
            "high": b.get("high"),
            "low": b.get("low"),
            "close": b.get("close"),
            "volume": b.get("volume"),
            "oi": b.get("oi"),
        }
        for b in bars
    ]
    _emit(rows, args.format, cmd=args.cmd)
    return 0


INSTRUMENTS_CACHE = Path("data/instruments")


def _instruments_cache_path(exchange: str) -> Path:
    return INSTRUMENTS_CACHE / f"{exchange or 'ALL'}.json"


def _load_cached_instruments(exchange: str) -> list[dict] | None:
    path = _instruments_cache_path(exchange)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    ttl = int(os.getenv("KITE_INSTRUMENTS_TTL_SECONDS") or 86400)
    age = time.time() - payload.get("fetched_at", 0)
    if age > ttl:
        return None
    return payload.get("rows", [])


def _save_cached_instruments(exchange: str, rows: list[dict]) -> Path:
    """Persist the instruments dump atomically.

    Kite's `/instruments` response is a multi-MB list. Writing it non-atomically
    risks a corrupted cache if the process is killed mid-write, forcing an
    expensive re-fetch on the next run (and racing parallel readers into
    JSONDecodeError).
    """
    from kite_algo.config import atomic_write_text

    path = _instruments_cache_path(exchange)
    clean = []
    for r in rows:
        c = dict(r)
        for k, v in list(c.items()):
            if isinstance(v, (date, datetime)):
                c[k] = v.isoformat()
        clean.append(c)
    payload = json.dumps({"fetched_at": time.time(), "rows": clean})
    atomic_write_text(path, payload, mode=0o644)
    return path


def _fetch_instruments(client: Any, exchange: str, refresh: bool = False) -> list[dict]:
    if not refresh:
        cached = _load_cached_instruments(exchange)
        if cached is not None:
            return cached
    rows = client.instruments(exchange) if exchange else client.instruments()
    _save_cached_instruments(exchange, rows)
    return rows


def cmd_instruments(args: argparse.Namespace) -> int:
    client = _new_client()
    rows = _fetch_instruments(client, args.exchange, refresh=args.refresh)
    if args.dump:
        _emit(rows, args.format, cmd=args.cmd)
        return 0
    by_segment: dict[str, int] = {}
    for r in rows:
        by_segment[r.get("segment", "UNKNOWN")] = by_segment.get(r.get("segment", "UNKNOWN"), 0) + 1
    out = {
        "exchange": args.exchange or "ALL",
        "count": len(rows),
        "by_segment": by_segment,
        "cache_path": str(_instruments_cache_path(args.exchange)),
    }
    _emit(out, args.format, cmd=args.cmd)
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    client = _new_client()
    rows = _fetch_instruments(client, args.exchange or "")
    query = args.query.upper()
    out = []
    for r in rows:
        sym = (r.get("tradingsymbol") or "").upper()
        name = (r.get("name") or "").upper()
        if query in sym or query in name:
            out.append({
                "tradingsymbol": r.get("tradingsymbol"),
                "name": r.get("name"),
                "exchange": r.get("exchange"),
                "segment": r.get("segment"),
                "instrument_token": r.get("instrument_token"),
                "expiry": r.get("expiry"),
                "strike": r.get("strike"),
                "instrument_type": r.get("instrument_type"),
                "lot_size": r.get("lot_size"),
                "tick_size": r.get("tick_size"),
            })
    if args.limit:
        out = out[: args.limit]
    _emit(out, args.format, cmd=args.cmd)
    return 0


def cmd_contract(args: argparse.Namespace) -> int:
    """Full instrument details for a single tradingsymbol."""
    client = _new_client()
    rows = _fetch_instruments(client, args.exchange or "")
    sym_upper = args.tradingsymbol.upper()
    match = None
    for r in rows:
        if (r.get("tradingsymbol") or "").upper() == sym_upper:
            match = r
            break
    if not match:
        print(f"ERROR: instrument not found: {args.exchange}:{args.tradingsymbol}", file=sys.stderr)
        return 1
    _emit(match, args.format, cmd=args.cmd)
    return 0


def _resolve_token(client: Any, symbol: str, exchange: str) -> int | None:
    rows = _fetch_instruments(client, exchange or "NSE")
    for r in rows:
        if (r.get("tradingsymbol") or "").upper() == symbol.upper() and (r.get("exchange") or "").upper() == (exchange or "NSE").upper():
            return int(r.get("instrument_token") or 0) or None
    return None


# =============================================================================
# OPTIONS
# =============================================================================

def cmd_expiries(args: argparse.Namespace) -> int:
    client = _new_client()
    rows = _fetch_instruments(client, "NFO")
    expiries = sorted({r.get("expiry") for r in rows if (r.get("name") or "").upper() == args.symbol.upper() and r.get("expiry")})
    _emit([{"expiry": e} for e in expiries], args.format, cmd=args.cmd)
    return 0


def _compute_greeks_for_option(
    spot: float, strike: float, expiry_str: str, right: str,
    last_price: float | None, risk_free_rate: float,
) -> dict[str, float | None]:
    """Compute BSM greeks for a single option leg."""
    from kite_algo.greeks import implied_vol, greeks as compute_greeks

    if not last_price or last_price <= 0 or not spot or spot <= 0:
        return {"iv": None, "delta": None, "gamma": None, "theta": None, "vega": None}

    try:
        exp_date = datetime.strptime(expiry_str, "%Y-%m-%d").date() if isinstance(expiry_str, str) else expiry_str
    except (ValueError, TypeError):
        return {"iv": None, "delta": None, "gamma": None, "theta": None, "vega": None}

    T = max((exp_date - date.today()).days, 0) / 365.0
    if T <= 0:
        return {"iv": None, "delta": None, "gamma": None, "theta": None, "vega": None}

    iv = implied_vol(last_price, spot, strike, T, risk_free_rate, right)
    if iv is None:
        return {"iv": None, "delta": None, "gamma": None, "theta": None, "vega": None}

    g = compute_greeks(spot, strike, T, risk_free_rate, iv, right)
    return {
        "iv": round(iv * 100, 2),
        "delta": round(g["delta"], 4),
        "gamma": round(g["gamma"], 6),
        "theta": round(g["theta"], 4),
        "vega": round(g["vega"], 4),
    }


def cmd_chain(args: argparse.Namespace) -> int:
    client = _new_client()
    rows = _fetch_instruments(client, "NFO")
    target_expiry = args.expiry
    chain = [
        r for r in rows
        if (r.get("name") or "").upper() == args.symbol.upper()
        and str(r.get("expiry") or "") == target_expiry
        and r.get("instrument_type") in ("CE", "PE")
    ]
    if not chain:
        print(f"ERROR: no instruments for {args.symbol} expiry {target_expiry} in NFO dump", file=sys.stderr)
        return 1

    chain.sort(key=lambda r: (float(r.get("strike") or 0), r.get("instrument_type") or ""))

    if args.quote:
        symbols = [f"NFO:{r.get('tradingsymbol')}" for r in chain]
        # Auto-batch at 500 (Kite /quote cap). Rate limiter pacing is
        # enforced per-batch by the RateLimitedKiteClient proxy.
        quote_data = _batched_quote_call(client, "quote", symbols)

        # Get underlying spot for greeks
        spot = None
        if args.greeks:
            underlying_sym = args.symbol.upper()
            spot_data = client.ltp([f"NSE:{underlying_sym}"])
            spot_val = (spot_data or {}).get(f"NSE:{underlying_sym}", {})
            spot = spot_val.get("last_price")
            if not spot:
                # Try index (NIFTY, BANKNIFTY)
                idx_map = {"NIFTY": "NSE:NIFTY 50", "BANKNIFTY": "NSE:NIFTY BANK", "FINNIFTY": "NSE:NIFTY FIN SERVICE"}
                idx_key = idx_map.get(underlying_sym)
                if idx_key:
                    idx_data = client.ltp([idx_key])
                    spot = (idx_data or {}).get(idx_key, {}).get("last_price")

        from kite_algo.greeks import default_risk_free_rate
        rfr = args.risk_free_rate if hasattr(args, "risk_free_rate") and args.risk_free_rate is not None else default_risk_free_rate()

        out = []
        for r in chain:
            key = f"NFO:{r.get('tradingsymbol')}"
            q = quote_data.get(key, {}) or {}
            row: dict[str, Any] = {
                "strike": r.get("strike"),
                "right": r.get("instrument_type"),
                "symbol": r.get("tradingsymbol"),
                "last_price": q.get("last_price"),
                "oi": q.get("oi"),
                "volume": q.get("volume"),
                "avg_price": q.get("average_price"),
                "net_change": q.get("net_change"),
                "lot_size": r.get("lot_size"),
                "instrument_token": r.get("instrument_token"),
            }
            if args.greeks and spot:
                g = _compute_greeks_for_option(
                    spot, float(r.get("strike", 0)),
                    str(r.get("expiry", "")),
                    r.get("instrument_type", "CE"),
                    q.get("last_price"), rfr,
                )
                row.update(g)
            out.append(row)
    else:
        out = [
            {
                "strike": r.get("strike"),
                "right": r.get("instrument_type"),
                "symbol": r.get("tradingsymbol"),
                "lot_size": r.get("lot_size"),
                "instrument_token": r.get("instrument_token"),
            }
            for r in chain
        ]
    if args.summary:
        # Use the just-fetched spot (if any) as the reference for ATM strike.
        spot_ref = locals().get("spot") or None
        summary = summarize_option_chain(out, spot=spot_ref)
        _emit(summary, args.format, cmd=args.cmd)
        return 0
    out = project_rows(out, parse_fields(args.fields))
    _emit(out, args.format, cmd=args.cmd)
    return 0


def cmd_option_quote(args: argparse.Namespace) -> int:
    client = _new_client()
    rows = _fetch_instruments(client, "NFO")
    match = None
    for r in rows:
        if (
            (r.get("name") or "").upper() == args.symbol.upper()
            and str(r.get("expiry") or "") == args.expiry
            and float(r.get("strike") or 0) == float(args.strike)
            and r.get("instrument_type") == args.right.upper()
        ):
            match = r
            break
    if not match:
        print(
            f"ERROR: option not found: {args.symbol} {args.expiry} {args.strike} {args.right}",
            file=sys.stderr,
        )
        return 1
    key = f"NFO:{match.get('tradingsymbol')}"
    q = (client.quote([key]) or {}).get(key, {}) or {}

    out: dict[str, Any] = {
        "tradingsymbol": match.get("tradingsymbol"),
        "instrument_token": match.get("instrument_token"),
        "lot_size": match.get("lot_size"),
        "last_price": q.get("last_price"),
        "oi": q.get("oi"),
        "volume": q.get("volume"),
        "ohlc": q.get("ohlc"),
        "depth": q.get("depth"),
        "avg_price": q.get("average_price"),
        "net_change": q.get("net_change"),
    }

    # Compute greeks if --greeks flag set
    if args.greeks:
        underlying_sym = args.symbol.upper()
        spot_data = client.ltp([f"NSE:{underlying_sym}"])
        spot = (spot_data or {}).get(f"NSE:{underlying_sym}", {}).get("last_price")
        if not spot:
            idx_map = {"NIFTY": "NSE:NIFTY 50", "BANKNIFTY": "NSE:NIFTY BANK", "FINNIFTY": "NSE:NIFTY FIN SERVICE"}
            idx_key = idx_map.get(underlying_sym)
            if idx_key:
                idx_data = client.ltp([idx_key])
                spot = (idx_data or {}).get(idx_key, {}).get("last_price")

        if spot:
            from kite_algo.greeks import default_risk_free_rate
            rfr = args.risk_free_rate if hasattr(args, "risk_free_rate") and args.risk_free_rate is not None else default_risk_free_rate()
            g = _compute_greeks_for_option(
                spot, float(args.strike), args.expiry,
                args.right.upper(), q.get("last_price"), rfr,
            )
            out["spot"] = spot
            out.update(g)
        else:
            print(f"WARN: could not resolve underlying spot for {underlying_sym}", file=sys.stderr)

    _emit(out, args.format, cmd=args.cmd)
    return 0


def cmd_calc_iv(args: argparse.Namespace) -> int:
    """Calculate implied volatility from market price."""
    from kite_algo.greeks import implied_vol, default_risk_free_rate

    rfr = args.risk_free_rate if args.risk_free_rate is not None else default_risk_free_rate()
    T = args.dte / 365.0
    iv = implied_vol(args.market_price, args.spot, args.strike, T, rfr, args.right)
    if iv is None:
        print("ERROR: IV solver did not converge", file=sys.stderr)
        return 1
    _emit({"iv_pct": round(iv * 100, 2), "iv_decimal": round(iv, 6)}, args.format, cmd=args.cmd)
    return 0


def cmd_calc_price(args: argparse.Namespace) -> int:
    """Calculate theoretical option price + greeks from IV."""
    from kite_algo.greeks import greeks as compute_greeks, default_risk_free_rate

    rfr = args.risk_free_rate if args.risk_free_rate is not None else default_risk_free_rate()
    T = args.dte / 365.0
    sigma = args.iv / 100.0
    g = compute_greeks(args.spot, args.strike, T, rfr, sigma, args.right)
    out = {k: round(v, 6) for k, v in g.items()}
    out["iv_pct"] = round(args.iv, 2)
    _emit(out, args.format, cmd=args.cmd)
    return 0


# =============================================================================
# ORDERS (write path)
# =============================================================================

def _require_yes(args: argparse.Namespace, action: str) -> None:
    if not getattr(args, "yes", False):
        raise SystemExit(
            f"Refusing to {action} without --yes. All order-placing commands "
            f"require an explicit --yes confirmation flag."
        )


def _require_not_halted(action: str) -> None:
    """Check the HALTED sentinel at the top of every write command.

    Raises HaltActive which classifies as exit code 11. The message in the
    error envelope explains how to resume.
    """
    state = read_halt()
    if state is not None:
        raise SystemExit(
            f"Refusing to {action}: trading is HALTED "
            f"(reason: {state.reason!r} by {state.by}). "
            f"Run `kite-algo resume --confirm-resume` to clear."
        )


def cmd_place(args: argparse.Namespace) -> int:
    """Place a single order via Kite Connect.

    Pipeline:
      1. Pre-flight validation (local — no API call).
      2. --dry-run: skip to order_margins() preview and return.
      3. Auto-generate tag if not provided (for idempotent retry).
      4. IdempotentOrderPlacer: rate-limit, place, check orderbook on transient failure.
      5. --wait-for-fill: poll order_history until COMPLETE / REJECTED / CANCELLED.
    """
    _require_yes(args, "place an order")
    _require_not_halted("place an order")

    # Default market_protection for MARKET/SL-M when not explicitly set: -1 =
    # Kite auto. Post-SEBI April 2026, omitting market_protection from MARKET
    # orders is a server-side reject. pykiteconnect v5.1.0 doesn't auto-fill
    # it either, so we set it here.
    effective_market_protection = args.market_protection
    if args.order_type in ("MARKET", "SL-M") and effective_market_protection is None:
        effective_market_protection = -1

    # --- 1. Pre-flight validation -------------------------------------------
    errors = validate_order(
        exchange=args.exchange,
        tradingsymbol=args.tradingsymbol,
        transaction_type=args.transaction_type,
        order_type=args.order_type,
        quantity=args.quantity,
        product=args.product,
        variety=args.variety,
        price=args.price,
        trigger_price=args.trigger_price,
        validity=args.validity,
        validity_ttl=args.validity_ttl,
        disclosed_quantity=args.disclosed_quantity,
        iceberg_legs=args.iceberg_legs,
        iceberg_quantity=args.iceberg_quantity,
        tag=args.tag,
        market_protection=effective_market_protection,
    )
    if errors:
        print(format_errors(errors), file=sys.stderr)
        return 1

    # --- 1b. Market-rule checks (hours, MIS cutoff, freeze qty, lot size) ---
    # Any "error" severity violation blocks the order; "warn" is surfaced but
    # allowed through. --skip-market-rules bypasses for testing / special
    # cases (e.g. intentional AMO at night).
    underlying = None
    if args.exchange in ("NFO", "BFO"):
        # Derivative tradingsymbols start with the underlying. Heuristic but
        # good enough to cover NIFTY/BANKNIFTY/FINNIFTY/SENSEX etc.
        for u in ("MIDCPNIFTY", "NIFTYNXT50", "BANKNIFTY", "FINNIFTY", "NIFTY", "SENSEX", "BANKEX"):
            if args.tradingsymbol.upper().startswith(u):
                underlying = u
                break
    if not getattr(args, "skip_market_rules", False):
        violations = check_market_rules(
            exchange=args.exchange,
            product=args.product,
            quantity=args.quantity,
            tradingsymbol=args.tradingsymbol,
            underlying=underlying,
            allow_amo=(args.variety == "amo"),
        )
        errors_market = [v for v in violations if v.severity == "error"]
        warnings_market = [v for v in violations if v.severity == "warn"]
        for w in warnings_market:
            print(f"WARN [{w.code}]: {w.message}", file=sys.stderr)
        if errors_market:
            for e in errors_market:
                print(f"ERROR [{e.code}]: {e.message}", file=sys.stderr)
            return 1

    client = _new_client()

    # --- 2. Build payload ---------------------------------------------------
    extras: dict[str, Any] = {}
    if args.price is not None:
        extras["price"] = args.price
    if args.trigger_price is not None:
        extras["trigger_price"] = args.trigger_price
    if args.disclosed_quantity is not None:
        extras["disclosed_quantity"] = args.disclosed_quantity
    if args.validity:
        extras["validity"] = args.validity
    if args.validity_ttl is not None:
        extras["validity_ttl"] = args.validity_ttl
    if args.iceberg_legs is not None:
        extras["iceberg_legs"] = args.iceberg_legs
    if args.iceberg_quantity is not None:
        extras["iceberg_quantity"] = args.iceberg_quantity
    if effective_market_protection is not None:
        extras["market_protection"] = effective_market_protection

    # --- 3. --dry-run: preview margin + charges, no transmission ------------
    # Build a margin-calc payload with ONLY the fields order_margins accepts.
    # This endpoint is read-only — it computes margin requirements without
    # transmitting. We whitelist fields explicitly and omit price for MARKET
    # orders (order_margins rejects price=0 with an InputException).
    if args.dry_run:
        preview_order: dict[str, Any] = {
            "exchange": args.exchange,
            "tradingsymbol": args.tradingsymbol,
            "transaction_type": args.transaction_type,
            "variety": args.variety,
            "product": args.product,
            "order_type": args.order_type,
            "quantity": args.quantity,
        }
        if args.order_type in ("LIMIT", "SL") and args.price is not None:
            preview_order["price"] = args.price
        if args.order_type in ("SL", "SL-M") and args.trigger_price is not None:
            preview_order["trigger_price"] = args.trigger_price
        if args.order_type in ("MARKET", "SL-M"):
            # Mandatory for margin preview too, else Kite rejects with the
            # same "market_protection required" input error.
            preview_order["market_protection"] = effective_market_protection

        try:
            preview = client.order_margins([preview_order])
        except Exception as exc:
            print(f"ERROR: dry-run margin preview failed: {_redact_secrets(str(exc))}", file=sys.stderr)
            return 1
        print(
            "=== DRY RUN — margin preview only. NO order transmitted. ===",
            file=sys.stderr,
        )
        _emit(preview, args.format, cmd=args.cmd)
        return 0

    # --- 4. Idempotent placement -------------------------------------------
    #
    # Two layers of idempotency:
    #   a) --idempotency-key KEY: durable, survives process restart.
    #      Stored in data/idempotency.sqlite. On retry with the same key
    #      we short-circuit if the prior attempt completed, or derive the
    #      same tag to coax IdempotentOrderPlacer's orderbook lookup into
    #      finding the in-flight order.
    #   b) tag / IdempotentOrderPlacer: in-process retry with orderbook
    #      polling on transient failure. Covers the single-invocation case.
    #
    # If --tag is given explicitly, it wins over the key-derived tag.

    idem_store: IdempotencyStore | None = None
    idem_key = getattr(args, "idempotency_key", None)
    if idem_key:
        idem_store = IdempotencyStore()
        existing = idem_store.lookup(idem_key)
        if existing is not None and existing.completed:
            # Previous attempt already completed — replay the stored result.
            replayed = existing.result or {}
            if isinstance(replayed, dict):
                replayed = {**replayed, "replayed": True,
                            "first_seen_at_ms": existing.first_seen_at_ms}
            _emit(replayed, args.format, cmd=args.cmd)
            return existing.exit_code or 0

    tag = args.tag
    if tag is None:
        tag = derive_tag_from_key(idem_key) if idem_key else new_order_tag()

    request_snapshot = {
        "exchange": args.exchange,
        "tradingsymbol": args.tradingsymbol,
        "transaction_type": args.transaction_type,
        "order_type": args.order_type,
        "quantity": args.quantity,
        "product": args.product,
        "variety": args.variety,
        "tag": tag,
        **extras,
    }
    if idem_store is not None and idem_key:
        idem_store.record_attempt(
            key=idem_key, cmd="place", request=request_snapshot, tag=tag,
        )

    placer = IdempotentOrderPlacer(client, rate_limiter=_RATE_LIMITER)
    try:
        order_id = placer.place(
            variety=args.variety,
            exchange=args.exchange,
            tradingsymbol=args.tradingsymbol,
            transaction_type=args.transaction_type,
            quantity=args.quantity,
            product=args.product,
            order_type=args.order_type,
            tag=tag,
            **extras,
        )
    except Exception as exc:
        # On failure the idempotency row stays incomplete — a retry with the
        # same key will try again (and benefit from orderbook-lookup dedup).
        print(f"ERROR: place_order failed: {exc}", file=sys.stderr)
        return 1

    result: dict[str, Any] = {
        "order_id": order_id,
        "tag": tag,
        "exchange": args.exchange,
        "tradingsymbol": args.tradingsymbol,
        "transaction_type": args.transaction_type,
        "quantity": args.quantity,
        "product": args.product,
        "order_type": args.order_type,
        **extras,
    }

    # --- 5. --wait-for-fill -------------------------------------------------
    if args.wait_for_fill > 0:
        final_status = _wait_for_fill(client, order_id, timeout=args.wait_for_fill)
        result["final_status"] = final_status.get("status")
        result["filled_quantity"] = final_status.get("filled_quantity")
        result["average_price"] = final_status.get("average_price")
        result["status_message"] = final_status.get("status_message")

    # Record completion in the idempotency cache before we emit, so a crash
    # between emission and subsequent retries still reconciles correctly.
    if idem_store is not None and idem_key:
        idem_store.record_completion(
            key=idem_key, result=result, exit_code=0,
            kite_order_id=str(order_id),
        )

    # Attach this order to a group if the agent provided --group-id.
    group_id = getattr(args, "group_id", None)
    if group_id:
        from kite_algo.groups import GroupStore
        try:
            GroupStore().add_member(
                group_id=group_id,
                order_id=str(order_id),
                leg_name=getattr(args, "leg_name", None),
                tag=tag,
                idempotency_key=idem_key,
            )
            result["group_id"] = group_id
            if getattr(args, "leg_name", None):
                result["leg_name"] = args.leg_name
        except Exception as exc:
            log.warning("could not attach order to group %s: %s",
                        group_id, redact_text(str(exc)))

    _emit(result, args.format, cmd=args.cmd)
    return 0


def _parse_order_timestamp(raw: Any) -> datetime:
    """Parse a Kite order_timestamp to a `datetime` for correct sorting.

    Kite returns timestamps in local IST, typically as either:
      - `datetime` (pykiteconnect sometimes materialises these)
      - `"YYYY-MM-DD HH:MM:SS"` (space-separated, no TZ; IST implied)
      - ISO-8601 with offset (rare; e.g. `"2026-04-19T15:30:45+05:30"`)

    We need the microsecond-safe comparison: string-sort of `"15:30:00"` vs
    `"15:30:01"` happens to work, but e.g. `"2026-04-19 9:05:00"` vs
    `"2026-04-19 10:05:00"` DOES NOT (leading zero missing on hour 9 can
    reorder). Always parse before comparing.

    Falls back to `datetime.min` on parse failure so malformed rows sort
    FIRST (conservative — we pick the last valid row as the "latest state").
    """
    if isinstance(raw, datetime):
        return raw
    if not raw:
        return datetime.min
    s = str(raw).strip()
    # Try ISO-8601 (with or without offset) — Python 3.11+ accepts space sep.
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        pass
    # Try Kite's usual "YYYY-MM-DD HH:MM:SS"
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return datetime.min


def _wait_for_fill(client: Any, order_id: str, *, timeout: float) -> dict:
    """Poll order_history until order reaches a terminal state or timeout.

    Uses exponential backoff (100ms → 1s cap) so fast fills aren't polled
    slowly. Bails immediately on fatal errors (TokenException,
    PermissionException).

    History rows are sorted by parsed `order_timestamp` (datetime), not by
    raw string — otherwise same-second events with differing zero-padding
    or timezone format can end up reordered.
    """
    terminal = {"COMPLETE", "REJECTED", "CANCELLED"}
    deadline = time.monotonic() + timeout
    last: dict = {}
    delay = 0.1
    while time.monotonic() < deadline:
        try:
            history = client.order_history(order_id) or []
            if history:
                history = sorted(history, key=lambda h: _parse_order_timestamp(h.get("order_timestamp")))
                last = history[-1]
                if last.get("status") in terminal:
                    return last
        except Exception as exc:
            name = type(exc).__name__
            if name in ("TokenException", "PermissionException"):
                log.error("fatal error polling order: %s", _redact_secrets(str(exc)))
                return last or {"status": "FATAL", "status_message": _redact_secrets(str(exc))}
            log.warning("order_history poll failed: %s", _redact_secrets(str(exc)))
        time.sleep(delay)
        delay = min(1.0, delay * 2)
    return last or {"status": "TIMEOUT"}


def cmd_cancel(args: argparse.Namespace) -> int:
    _require_yes(args, "cancel an order")
    _require_not_halted("cancel an order")
    client = _new_client()
    client.cancel_order(variety=args.variety, order_id=args.order_id)
    print(f"cancel sent: variety={args.variety} order_id={args.order_id}", file=sys.stderr)
    return 0


def cmd_modify(args: argparse.Namespace) -> int:
    _require_yes(args, "modify an order")
    _require_not_halted("modify an order")
    # Kite caps modifications at ~25 per order lifetime. Track locally so we
    # fail fast rather than hit the server-side "Maximum allowed order
    # modifications exceeded" InputException.
    try:
        count = record_modification(args.order_id)
    except ModificationLimitExceeded as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    client = _new_client()
    kwargs: dict[str, Any] = {}
    if args.quantity is not None:
        kwargs["quantity"] = args.quantity
    if args.price is not None:
        kwargs["price"] = args.price
    if args.trigger_price is not None:
        kwargs["trigger_price"] = args.trigger_price
    if args.order_type is not None:
        kwargs["order_type"] = args.order_type
    if args.validity is not None:
        kwargs["validity"] = args.validity
    if getattr(args, "market_protection", None) is not None:
        kwargs["market_protection"] = args.market_protection
    client.modify_order(variety=args.variety, order_id=args.order_id, **kwargs)
    print(
        f"modify sent: variety={args.variety} order_id={args.order_id} "
        f"(modification #{count} of {20})",
        file=sys.stderr,
    )
    return 0


def cmd_cancel_all(args: argparse.Namespace) -> int:
    """Cancel every open / trigger-pending order.

    Rate-limits every cancel through the orders bucket. Reports structured
    success/failure. Exits non-zero if any cancel failed.
    """
    _require_yes(args, "cancel ALL open orders")
    if not getattr(args, "confirm_panic", False):
        raise SystemExit(
            "Refusing to cancel ALL open orders without --confirm-panic. "
            "This flag is distinct from --yes: a stray `cancel-all --yes` "
            "retry would destroy the entire book. --confirm-panic forces "
            "the agent to acknowledge the broader blast radius explicitly."
        )
    _require_not_halted("cancel ALL open orders")
    client = _new_client()
    orders = client.orders() or []
    cancelled: list[str] = []
    failed: list[dict[str, str]] = []

    for o in orders:
        if o.get("status") not in ("OPEN", "TRIGGER PENDING"):
            continue
        order_id = o.get("order_id", "")
        variety = o.get("variety")
        if not variety:
            # Don't guess — surface the inconsistency.
            failed.append({"order_id": order_id, "reason": "missing variety in orderbook row"})
            continue
        try:
            # client is already rate-limited via RateLimitedKiteClient,
            # which automatically routes cancel_order through wait_order.
            client.cancel_order(variety=variety, order_id=order_id)
            cancelled.append(order_id)
        except Exception as exc:
            failed.append({"order_id": order_id, "reason": _redact_secrets(str(exc))[:200]})

    result = {
        "cancelled": cancelled,
        "failed": failed,
        "total_cancelled": len(cancelled),
        "total_failed": len(failed),
    }
    _emit(result, args.format, cmd=args.cmd)
    return 0 if not failed else 1


# =============================================================================
# GTT
# =============================================================================

def cmd_gtt_list(args: argparse.Namespace) -> int:
    client = _new_client()
    _emit(client.get_gtts(), args.format, cmd=args.cmd)
    return 0


def cmd_gtt_get(args: argparse.Namespace) -> int:
    client = _new_client()
    _emit(client.get_gtt(trigger_id=args.trigger_id), args.format, cmd=args.cmd)
    return 0


def cmd_gtt_delete(args: argparse.Namespace) -> int:
    _require_yes(args, "delete a GTT")
    _require_not_halted("delete a GTT")
    client = _new_client()
    client.delete_gtt(trigger_id=args.trigger_id)
    print(f"deleted gtt trigger_id={args.trigger_id}", file=sys.stderr)
    return 0


def cmd_gtt_create(args: argparse.Namespace) -> int:
    """Create a GTT (Good Till Triggered) order.

    Single-leg: one trigger value + one order.
    Two-leg (OCO): two trigger values (stoploss, target) + two orders.
    """
    _require_yes(args, "create a GTT")
    _require_not_halted("create a GTT")
    client = _new_client()

    trigger_values = [float(v.strip()) for v in args.trigger_values.split(",")]
    if len(trigger_values) == 1:
        trigger_type = client.GTT_TYPE_SINGLE
    elif len(trigger_values) == 2:
        trigger_type = client.GTT_TYPE_OCO
    else:
        print("ERROR: --trigger-values must be 1 (single) or 2 (OCO) comma-separated values", file=sys.stderr)
        return 1

    # Build order legs
    orders = []
    if args.orders_json:
        try:
            orders = json.loads(args.orders_json)
        except json.JSONDecodeError as exc:
            print(f"ERROR: --orders-json parse failed: {exc}", file=sys.stderr)
            return 1
    else:
        # Build from simple args (single-leg shorthand)
        order = {
            "exchange": args.exchange,
            "tradingsymbol": args.tradingsymbol,
            "transaction_type": args.transaction_type,
            "quantity": args.quantity,
            "order_type": args.order_type or "LIMIT",
            "product": args.product or "CNC",
            "price": args.price or 0,
        }
        orders.append(order)
        # For OCO, second leg
        if len(trigger_values) == 2:
            order2 = dict(order)
            if args.price2 is not None:
                order2["price"] = args.price2
            orders.append(order2)

    try:
        trigger_id = client.place_gtt(
            trigger_type=trigger_type,
            tradingsymbol=args.tradingsymbol,
            exchange=args.exchange,
            trigger_values=trigger_values,
            last_price=args.last_price,
            orders=orders,
        )
    except Exception as exc:
        print(f"ERROR: place_gtt failed: {exc}", file=sys.stderr)
        return 1

    print(f"GTT created: trigger_id={trigger_id}", file=sys.stderr)
    _emit({"trigger_id": trigger_id}, args.format, cmd=args.cmd)
    return 0


def cmd_gtt_modify(args: argparse.Namespace) -> int:
    """Modify an existing GTT trigger."""
    _require_yes(args, "modify a GTT")
    _require_not_halted("modify a GTT")
    client = _new_client()

    trigger_values = [float(v.strip()) for v in args.trigger_values.split(",")]
    if len(trigger_values) == 1:
        trigger_type = client.GTT_TYPE_SINGLE
    elif len(trigger_values) == 2:
        trigger_type = client.GTT_TYPE_OCO
    else:
        print("ERROR: --trigger-values must be 1 or 2 values", file=sys.stderr)
        return 1

    try:
        orders = json.loads(args.orders_json)
    except json.JSONDecodeError as exc:
        print(f"ERROR: --orders-json parse failed: {exc}", file=sys.stderr)
        return 1

    try:
        trigger_id = client.modify_gtt(
            trigger_id=args.trigger_id,
            trigger_type=trigger_type,
            tradingsymbol=args.tradingsymbol,
            exchange=args.exchange,
            trigger_values=trigger_values,
            last_price=args.last_price,
            orders=orders,
        )
    except Exception as exc:
        print(f"ERROR: modify_gtt failed: {exc}", file=sys.stderr)
        return 1

    print(f"GTT modified: trigger_id={trigger_id}", file=sys.stderr)
    _emit({"trigger_id": trigger_id}, args.format, cmd=args.cmd)
    return 0


# =============================================================================
# MARGIN CALC
# =============================================================================

def cmd_margin_calc(args: argparse.Namespace) -> int:
    client = _new_client()
    if args.orders_json:
        try:
            orders = json.loads(args.orders_json)
        except json.JSONDecodeError as exc:
            print(f"ERROR: --orders-json must be valid JSON: {exc}", file=sys.stderr)
            return 1
    else:
        orders = [{
            "exchange": args.exchange,
            "tradingsymbol": args.tradingsymbol,
            "transaction_type": args.transaction_type,
            "variety": args.variety or "regular",
            "product": args.product,
            "order_type": args.order_type or "MARKET",
            "quantity": args.quantity,
            "price": args.price or 0,
        }]
    data = client.order_margins(orders)
    _emit(data, args.format, cmd=args.cmd)
    return 0


def cmd_basket_margin(args: argparse.Namespace) -> int:
    client = _new_client()
    try:
        orders = json.loads(args.orders_json)
    except json.JSONDecodeError as exc:
        print(f"ERROR: --orders-json must be valid JSON: {exc}", file=sys.stderr)
        return 1
    data = client.basket_order_margins(orders)
    _emit(data, args.format, cmd=args.cmd)
    return 0


# =============================================================================
# MUTUAL FUNDS
# =============================================================================

def _mf_subscription_hint() -> str:
    return (
        "Mutual fund API requires a separate Kite Connect MF subscription. "
        "Enable at https://kite.zerodha.com/connect/apps (MF add-on)."
    )


def cmd_mf_holdings(args: argparse.Namespace) -> int:
    client = _new_client()
    try:
        _emit(client.mf_holdings(), args.format, cmd=args.cmd)
    except Exception as exc:
        print(f"ERROR: mf_holdings failed: {exc}", file=sys.stderr)
        print(_mf_subscription_hint(), file=sys.stderr)
        return 1
    return 0


def cmd_mf_orders(args: argparse.Namespace) -> int:
    client = _new_client()
    try:
        _emit(client.mf_orders(), args.format, cmd=args.cmd)
    except Exception as exc:
        print(f"ERROR: mf_orders failed: {exc}", file=sys.stderr)
        print(_mf_subscription_hint(), file=sys.stderr)
        return 1
    return 0


def cmd_mf_sips(args: argparse.Namespace) -> int:
    client = _new_client()
    try:
        _emit(client.mf_sips(), args.format, cmd=args.cmd)
    except Exception as exc:
        print(f"ERROR: mf_sips failed: {exc}", file=sys.stderr)
        print(_mf_subscription_hint(), file=sys.stderr)
        return 1
    return 0


def cmd_mf_instruments(args: argparse.Namespace) -> int:
    client = _new_client()
    data = client.mf_instruments()
    _emit(data, args.format, cmd=args.cmd)
    return 0


def cmd_mf_place(args: argparse.Namespace) -> int:
    _require_yes(args, "place a mutual fund order")
    _require_not_halted("place a mutual fund order")
    client = _new_client()
    try:
        order_id = client.place_mf_order(
            tradingsymbol=args.tradingsymbol,
            transaction_type=args.transaction_type,
            quantity=args.quantity if args.quantity else None,
            amount=args.amount if args.amount else None,
            tag=args.tag,
        )
    except Exception as exc:
        print(f"ERROR: place_mf_order failed: {exc}", file=sys.stderr)
        return 1
    print(f"MF order placed: order_id={order_id}", file=sys.stderr)
    _emit({"order_id": order_id}, args.format, cmd=args.cmd)
    return 0


def cmd_mf_cancel(args: argparse.Namespace) -> int:
    _require_yes(args, "cancel a mutual fund order")
    _require_not_halted("cancel a mutual fund order")
    client = _new_client()
    try:
        client.cancel_mf_order(order_id=args.order_id)
    except Exception as exc:
        print(f"ERROR: cancel_mf_order failed: {exc}", file=sys.stderr)
        return 1
    print(f"MF order cancelled: order_id={args.order_id}", file=sys.stderr)
    return 0


def cmd_mf_sip_create(args: argparse.Namespace) -> int:
    _require_yes(args, "create a mutual fund SIP")
    _require_not_halted("create a mutual fund SIP")
    client = _new_client()
    try:
        sip_id = client.place_mf_sip(
            tradingsymbol=args.tradingsymbol,
            amount=args.amount,
            initial_amount=args.initial_amount,
            frequency=args.frequency,
            instalments=args.instalments,
            tag=args.tag,
        )
    except Exception as exc:
        print(f"ERROR: place_mf_sip failed: {exc}", file=sys.stderr)
        return 1
    print(f"SIP created: sip_id={sip_id}", file=sys.stderr)
    _emit({"sip_id": sip_id}, args.format, cmd=args.cmd)
    return 0


def cmd_mf_sip_modify(args: argparse.Namespace) -> int:
    _require_yes(args, "modify a mutual fund SIP")
    _require_not_halted("modify a mutual fund SIP")
    client = _new_client()
    kwargs: dict[str, Any] = {"sip_id": args.sip_id}
    if args.amount is not None:
        kwargs["amount"] = args.amount
    if args.frequency:
        kwargs["frequency"] = args.frequency
    if args.instalments is not None:
        kwargs["instalments"] = args.instalments
    if args.status:
        kwargs["status"] = args.status
    try:
        client.modify_mf_sip(**kwargs)
    except Exception as exc:
        print(f"ERROR: modify_mf_sip failed: {exc}", file=sys.stderr)
        return 1
    print(f"SIP modified: sip_id={args.sip_id}", file=sys.stderr)
    return 0


def cmd_mf_sip_cancel(args: argparse.Namespace) -> int:
    _require_yes(args, "cancel a mutual fund SIP")
    _require_not_halted("cancel a mutual fund SIP")
    client = _new_client()
    try:
        client.cancel_mf_sip(sip_id=args.sip_id)
    except Exception as exc:
        print(f"ERROR: cancel_mf_sip failed: {exc}", file=sys.stderr)
        return 1
    print(f"SIP cancelled: sip_id={args.sip_id}", file=sys.stderr)
    return 0


# =============================================================================
# Argparse wiring
# =============================================================================

def _add_common(p: argparse.ArgumentParser) -> None:
    """Flags that every subcommand inherits.

    - `--format auto` (the default) resolves to `json` when stdout is not a
      TTY (pipes, subprocesses, agent runs) and `table` when it is. JSON
      output is wrapped in the canonical envelope; disable via
      `KITE_NO_ENVELOPE=1` during migration.
    - `--fields a,b,c` keeps only the named columns on list-returning
      commands. Cuts agent context cost 60–90% on high-cardinality endpoints
      like `chain` or `instruments --dump`.
    - `--summary` emits a pre-aggregated rollup in place of the full list.
      Understood by `orders`, `holdings`, `positions`, `chain`.
    """
    p.add_argument(
        "--format",
        choices=["auto", "json", "csv", "table"],
        default="auto",
        help="Output format (default: auto — json when piped, table when TTY). "
             "JSON includes the stable envelope with request_id, warnings, "
             "and meta. KITE_NO_ENVELOPE=1 disables envelope wrapping.",
    )
    p.add_argument(
        "--fields",
        default=None,
        metavar="a,b,c",
        help="Comma-separated list of fields to include on list-returning "
             "commands. Missing fields are emitted as null (for CSV header "
             "stability). Ignored by commands that emit single objects.",
    )
    p.add_argument(
        "--summary",
        action="store_true",
        help="Emit a compact summary rollup instead of the full list. "
             "Supported by: orders, holdings, positions, chain. Reduces "
             "agent context cost 60-90%% on high-cardinality endpoints.",
    )
    p.add_argument(
        "--explain",
        action="store_true",
        help="Describe what this command would do, without making any API "
             "call or side effect. Emits a structured description including "
             "side effects, preconditions, reversibility, and idempotency. "
             "Different from --dry-run: --explain is purely local; --dry-run "
             "calls order_margins() to preview capital reservation.",
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="kite_algo.kite_tool",
        description="Comprehensive Kite Connect data + operations CLI.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name: str, fn: Callable[[argparse.Namespace], int], help_text: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help_text)
        _add_common(sp)
        sp.set_defaults(func=fn)
        return sp

    def _add_yes(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--yes", action="store_true", help="Required live confirmation flag")

    # --- meta (tools describe) ---
    # Emits JSONSchema for every subcommand — ready to register directly as
    # Claude/GPT tools. Introspects the live parser so this never drifts
    # from the actual CLI shape.
    def _cmd_tools_describe(args_: argparse.Namespace) -> int:
        from kite_algo.tool_schema import describe_tools
        # Defer: the parser is returned by build_parser(), which is us —
        # pass p via closure.
        _emit(describe_tools(p), args_.format, cmd=args_.cmd)
        return 0

    add("tools-describe", _cmd_tools_describe,
        "Emit JSONSchema for every subcommand (for Claude/GPT tool use)")

    # --- auth ---
    s = add("login", cmd_login,
            "OAuth login: local 127.0.0.1 listener auto-captures the callback "
            "(like `gh auth login`). Use --paste for the old copy/paste flow.")
    s.add_argument("--no-browser", action="store_true",
                   help="Don't auto-open a browser; print the URL and wait.")
    s.add_argument("--listen-port", type=int, default=5000, metavar="PORT",
                   help="Port for the callback listener (default 5000). MUST "
                        "match the port in your Kite app profile's registered "
                        "redirect URI (Kite matches ports literally, no wildcard).")
    s.add_argument("--timeout", type=float, default=300.0, metavar="SECS",
                   help="Max seconds to wait for the callback (default 300 = 5m). "
                        "Kite's request_token itself expires in ~5m — no point "
                        "waiting longer.")
    s.add_argument("--paste", action="store_true",
                   help="Skip the listener; print the login URL and prompt to "
                        "paste request_token. Use this when the listener can't "
                        "be reached (no SSH tunnel available, sandboxed runner).")

    add("profile", cmd_profile, "User profile (verify session)")
    add("session", cmd_session, "Current session status + approx expiry")
    add("health", cmd_health, "End-to-end health check (session, API, margins, market data)")
    add("logout", cmd_logout, "Invalidate access token + remove local session file")

    # --- alerts (raw HTTP) ---
    s = add("alerts-list", cmd_alerts_list,
            "List alerts (pykiteconnect doesn't wrap /alerts — this uses raw HTTP)")
    s.add_argument("--status", default=None, help="Filter by alert status")
    s.add_argument("--page", type=int, default=1)
    s.add_argument("--page-size", type=int, default=50)

    s = add("alerts-get", cmd_alerts_get, "Get a single alert by uuid")
    s.add_argument("--uuid", required=True)

    s = add("alerts-create", cmd_alerts_create,
            "Create a simple or ATO alert (ato = Alert-Triggers-Order)")
    s.add_argument("--name", required=True)
    s.add_argument("--type", required=True, choices=["simple", "ato"])
    s.add_argument("--lhs-exchange", required=True,
                   choices=["NSE", "BSE", "NFO", "BFO", "MCX", "CDS", "BCD"])
    s.add_argument("--lhs-tradingsymbol", required=True)
    s.add_argument("--lhs-attribute", default="LastTradedPrice",
                   help="Usually LastTradedPrice (default)")
    s.add_argument("--operator", required=True, choices=["<", ">", "<=", ">=", "=="])
    s.add_argument("--rhs-type", required=True, choices=["constant", "instrument"])
    s.add_argument("--rhs-constant", type=float, default=None,
                   help="Value threshold when --rhs-type=constant")
    s.add_argument("--rhs-exchange", default=None)
    s.add_argument("--rhs-tradingsymbol", default=None)
    s.add_argument("--rhs-attribute", default=None)
    s.add_argument("--basket-json", default=None,
                   help="For --type=ato: JSON list of order specs to auto-place")
    _add_yes(s)

    s = add("alerts-modify", cmd_alerts_modify, "Modify an alert")
    s.add_argument("--uuid", required=True)
    s.add_argument("--name", default=None)
    s.add_argument("--type", default=None, choices=["simple", "ato"])
    s.add_argument("--lhs-exchange", default=None)
    s.add_argument("--lhs-tradingsymbol", default=None)
    s.add_argument("--lhs-attribute", default=None)
    s.add_argument("--operator", default=None, choices=["<", ">", "<=", ">=", "=="])
    s.add_argument("--rhs-type", default=None, choices=["constant", "instrument"])
    s.add_argument("--rhs-constant", type=float, default=None)
    s.add_argument("--rhs-exchange", default=None)
    s.add_argument("--rhs-tradingsymbol", default=None)
    s.add_argument("--rhs-attribute", default=None)
    s.add_argument("--basket-json", default=None)
    _add_yes(s)

    s = add("alerts-delete", cmd_alerts_delete, "Delete an alert")
    s.add_argument("--uuid", required=True)
    _add_yes(s)

    s = add("alerts-history", cmd_alerts_history, "Trigger history for an alert")
    s.add_argument("--uuid", required=True)

    # --- reconcile local vs kite ---
    s = add("reconcile", cmd_reconcile,
            "Diff local audit/idempotency records against Kite's live orderbook")
    s.add_argument("--since", default=None, metavar="YYYY-MM-DD",
                   help="Only consider local records since this date (IST)")
    s.add_argument("--skip-kite", action="store_true",
                   help="Skip live orderbook fetch; only report orphan groups + local state")

    # --- multi-leg transaction groups ---
    s = add("group-start", cmd_group_start,
            "Begin a new multi-leg transaction; emits group_id for subsequent `place --group-id`")
    s.add_argument("--name", required=True,
                   help="Human-readable name of the group (e.g. BEAR_PUT_NIFTY)")
    s.add_argument("--legs", type=int, default=None,
                   help="Expected number of legs (optional; used by group-status to flag incomplete groups)")

    s = add("group-status", cmd_group_status,
            "Show a group + live Kite status of every leg")
    s.add_argument("--group-id", required=True)
    s.add_argument("--skip-kite", action="store_true",
                   help="Skip the live /orders lookup; just show recorded legs")

    s = add("group-cancel", cmd_group_cancel,
            "Cancel every still-open leg of a group (requires --yes)")
    s.add_argument("--group-id", required=True)
    _add_yes(s)

    # --- audit log tail ---
    s = add("events", cmd_events,
            "Tail the local SEBI-compliant audit log (data/audit/*.jsonl)")
    s.add_argument("--since", default=None, metavar="YYYY-MM-DD",
                   help="Lower date bound (inclusive, IST)")
    s.add_argument("--until", default=None, metavar="YYYY-MM-DD",
                   help="Upper date bound (inclusive, IST)")
    s.add_argument("--cmd-filter", default=None, metavar="NAME",
                   help="Only entries for this subcommand (e.g. `place`)")
    s.add_argument("--outcome", default=None, choices=["ok", "error"],
                   help="Filter on exit code: ok (0) or error (non-zero)")
    s.add_argument("--tail", type=int, default=None, metavar="N",
                   help="Only the last N matching entries")

    # --- poll-until ---
    s = add("watch", cmd_watch,
            "Poll a resource until an expression is true or timeout")
    s.add_argument("resource", choices=["quote", "ltp", "ohlc", "order"],
                   help="What to poll")
    s.add_argument("--symbol", default=None,
                   help="For quote/ltp/ohlc: exchange-qualified symbol "
                        "(e.g. NSE:RELIANCE)")
    s.add_argument("--order-id", default=None,
                   help="For resource=order: the order_id to poll")
    s.add_argument("--every", type=float, default=2.0,
                   help="Polling interval in seconds (default 2, min 0.1)")
    s.add_argument("--until", required=True,
                   metavar="EXPR",
                   help="Exit 0 when this expression is truthy. Restricted "
                        "AST: names, comparisons, and/or/not, arithmetic. "
                        'E.g. "last_price > 1300 and volume > 100000"')
    s.add_argument("--timeout", type=float, default=300.0,
                   help="Max wall-clock seconds to wait (default 300; 0 = forever)")

    # --- state introspection ---
    s = add("status", cmd_status,
            "Single-blob status: session, market, rate-limit, account, halt")
    s.add_argument("--skip-account", action="store_true",
                   help="Skip the account-state section (no live broker call). "
                        "Useful when the Kite session is dead but you still "
                        "want to see halt / market-hours state.")

    add("time", cmd_time,
        "Clocks, market open/close times, next weekly expiry, token rotation window")

    # --- kill switch (halt / resume) ---
    s = add("halt", cmd_halt,
            "Set the HALTED sentinel — refuses all write commands until cleared")
    s.add_argument("--reason", required=True,
                   help="Short description of why trading is halted (stored in sentinel)")
    s.add_argument("--by", default=None,
                   help="Operator / agent identifier. Defaults to $KITE_OPERATOR or 'operator'.")
    s.add_argument("--expires-in", default=None, metavar="DURATION",
                   help="Auto-clear after duration (e.g. 30s, 5m, 1h, 2d). "
                        "Without this flag the halt persists until `resume`.")

    s = add("resume", cmd_resume,
            "Clear the HALTED sentinel (requires --confirm-resume)")
    s.add_argument("--confirm-resume", action="store_true",
                   help="Required. Distinct from --yes to prevent accidental resume.")

    # --- account ---
    s = add("margins", cmd_margins, "Account margins")
    s.add_argument("--segment", choices=["equity", "commodity"], default=None)

    s = add("holdings", cmd_holdings, "Demat holdings")

    s = add("positions", cmd_positions, "Day or net positions")
    s.add_argument("--which", choices=["net", "day"], default="net")

    s = add("convert-position", cmd_convert_position, "Convert position product (MIS↔CNC↔NRML)")
    s.add_argument("--exchange", required=True, choices=["NSE", "BSE", "NFO", "BFO", "MCX", "CDS", "BCD"])
    s.add_argument("--tradingsymbol", required=True)
    s.add_argument("--transaction-type", required=True, choices=["BUY", "SELL"])
    s.add_argument("--position-type", required=True, choices=["day", "overnight"])
    s.add_argument("--quantity", type=int, required=True)
    s.add_argument("--old-product", required=True, choices=["CNC", "MIS", "NRML", "MTF"])
    s.add_argument("--new-product", required=True, choices=["CNC", "MIS", "NRML", "MTF"])
    _add_yes(s)
    s.add_argument("--confirm-convert", action="store_true",
                   help="Required. Distinct from --yes — explicit acknowledgment "
                        "that product conversion can reallocate margin / strand "
                        "an intraday position overnight.")

    add("pnl", cmd_pnl, "Aggregate P&L from positions (day + net)")
    add("portfolio", cmd_portfolio, "Combined holdings + positions MTM view")

    add("orders", cmd_orders, "Today's orders (all statuses)")
    add("open-orders", cmd_open_orders, "Only OPEN / TRIGGER PENDING orders")
    add("trades", cmd_trades, "Today's trades (fills)")

    s = add("order-history", cmd_order_history, "State history for one order")
    s.add_argument("--order-id", required=True)

    s = add("order-trades", cmd_order_trades, "Fills for one order")
    s.add_argument("--order-id", required=True)

    # --- quotes ---
    s = add("ltp", cmd_ltp, "Last traded price (fastest, up to 500 symbols)")
    s.add_argument("--symbols", required=True, help="Comma list: NSE:RELIANCE,NSE:INFY")

    s = add("ohlc", cmd_ohlc, "OHLC + LTP")
    s.add_argument("--symbols", required=True)

    s = add("quote", cmd_quote, "Full quote: OHLC, depth, OI, volume, avg price")
    s.add_argument("--symbols", required=True)
    s.add_argument("--flat", action="store_true", help="Flatten top-of-book to one row per symbol")

    s = add("depth", cmd_depth, "Market depth (5-level order book)")
    s.add_argument("--symbols", required=True, help="Single or comma-separated: NSE:RELIANCE")

    s = add("stream", cmd_stream, "Live WebSocket tick stream (KiteTicker, auto-reconnect)")
    s.add_argument("--symbols", default="", help="Comma list: NSE:RELIANCE,NFO:NIFTY...")
    s.add_argument("--tokens", default="", help="Comma list of instrument_token ints")
    s.add_argument("--mode", default="full", choices=["ltp", "quote", "full"])
    s.add_argument("--duration", type=float, default=0, help="Seconds to stream (0=until Ctrl+C)")
    s.add_argument("--order-updates", action="store_true", help="Also stream order update events")
    s.add_argument("--reconnect-max-tries", type=int, default=50, help="Max reconnection attempts")
    s.add_argument("--reconnect-max-delay", type=int, default=60, help="Max backoff between reconnects (s)")
    s.add_argument("--buffer-to", default=None, metavar="PATH",
                   help="Also append every emitted tick to PATH as NDJSON. "
                        "Lets a separate `tail-ticks` reader consume the stream "
                        "without holding a WebSocket.")

    s = add("tail-ticks", cmd_tail_ticks,
            "Read NDJSON ticks from a file populated by `stream --buffer-to`")
    s.add_argument("path", help="NDJSON buffer file path")
    s.add_argument("--symbols", default=None,
                   help="Comma list of tradingsymbols OR instrument_tokens to keep")
    s.add_argument("--from-seq", type=int, default=None,
                   help="Resume: skip ticks with _seq < N")
    s.add_argument("--limit", type=int, default=None,
                   help="Stop after emitting N ticks")
    s.add_argument("--follow", action="store_true",
                   help="Keep reading as new lines arrive (like `tail -f`)")
    s.add_argument("--poll-interval", type=float, default=0.25,
                   help="Sleep between read attempts when --follow is set")

    # --- historical ---
    s = add("history", cmd_history, "Historical OHLC bars")
    s.add_argument("--symbol", default=None)
    s.add_argument("--exchange", default="NSE")
    s.add_argument("--instrument-token", type=int, default=None)
    s.add_argument("--interval", default="day",
                   choices=["minute", "3minute", "5minute", "10minute", "15minute", "30minute", "60minute", "day"])
    s.add_argument("--days", type=int, default=30)
    s.add_argument("--from", dest="from_", default=None)
    s.add_argument("--to", default=None)
    s.add_argument("--continuous", action="store_true")
    s.add_argument("--oi", action="store_true")

    s = add("instruments", cmd_instruments, "Instruments dump (cached locally with TTL)")
    s.add_argument("--exchange", default="NSE")
    s.add_argument("--dump", action="store_true", help="Emit all rows instead of a summary")
    s.add_argument("--refresh", action="store_true", help="Force re-fetch, bypass cache")

    s = add("search", cmd_search, "Grep local instruments cache")
    s.add_argument("--query", required=True)
    s.add_argument("--exchange", default="")
    s.add_argument("--limit", type=int, default=50)

    s = add("contract", cmd_contract, "Full instrument details for a single symbol")
    s.add_argument("--tradingsymbol", required=True)
    s.add_argument("--exchange", default="NSE")

    # --- options ---
    s = add("expiries", cmd_expiries, "List NFO expiries for an underlying")
    s.add_argument("--symbol", required=True, help="Underlying name (e.g. NIFTY, BANKNIFTY, RELIANCE)")

    s = add("chain", cmd_chain, "Full option chain for one expiry")
    s.add_argument("--symbol", required=True)
    s.add_argument("--expiry", required=True, help="YYYY-MM-DD")
    s.add_argument("--quote", action="store_true", help="Also fetch live quotes for every strike")
    s.add_argument("--greeks", action="store_true", help="Compute BSM greeks (requires --quote)")
    s.add_argument("--risk-free-rate", type=float, default=None, help="Risk-free rate (default 0.065)")

    s = add("option-quote", cmd_option_quote, "Quote for a single option leg (with optional greeks)")
    s.add_argument("--symbol", required=True)
    s.add_argument("--expiry", required=True)
    s.add_argument("--strike", type=float, required=True)
    s.add_argument("--right", choices=["CE", "PE"], required=True)
    s.add_argument("--greeks", action="store_true", help="Compute BSM greeks")
    s.add_argument("--risk-free-rate", type=float, default=None)

    s = add("calc-iv", cmd_calc_iv, "Calculate implied volatility from market price")
    s.add_argument("--spot", type=float, required=True)
    s.add_argument("--strike", type=float, required=True)
    s.add_argument("--dte", type=float, required=True, help="Days to expiry")
    s.add_argument("--market-price", type=float, required=True, help="Observed option price")
    s.add_argument("--right", choices=["CE", "PE"], required=True)
    s.add_argument("--risk-free-rate", type=float, default=None)

    s = add("calc-price", cmd_calc_price, "Theoretical price + greeks from IV")
    s.add_argument("--spot", type=float, required=True)
    s.add_argument("--strike", type=float, required=True)
    s.add_argument("--dte", type=float, required=True)
    s.add_argument("--iv", type=float, required=True, help="IV in percent (e.g. 30 for 30%%)")
    s.add_argument("--right", choices=["CE", "PE"], required=True)
    s.add_argument("--risk-free-rate", type=float, default=None)

    # --- orders (write path) ---
    s = add("place", cmd_place, "Place a single order (validated, idempotent, rate-limited)")
    s.add_argument("--exchange", required=True, choices=["NSE", "BSE", "NFO", "BFO", "MCX", "CDS", "BCD"])
    s.add_argument("--tradingsymbol", required=True, help="e.g. RELIANCE, NIFTY2550822700CE")
    s.add_argument("--transaction-type", required=True, choices=["BUY", "SELL"])
    s.add_argument("--order-type", required=True, choices=["MARKET", "LIMIT", "SL", "SL-M"])
    s.add_argument("--quantity", type=int, required=True)
    s.add_argument("--product", required=True, choices=["CNC", "MIS", "NRML", "MTF"])
    s.add_argument("--price", type=float, default=None, help="Limit price (for LIMIT/SL)")
    s.add_argument("--trigger-price", type=float, default=None, help="Trigger price (for SL/SL-M)")
    s.add_argument("--validity", default="DAY", choices=["DAY", "IOC", "TTL"])
    s.add_argument("--validity-ttl", type=int, default=None, help="TTL minutes (for TTL validity)")
    s.add_argument("--variety", default="regular", choices=["regular", "amo", "co", "iceberg", "auction"])
    s.add_argument("--disclosed-quantity", type=int, default=None)
    s.add_argument("--iceberg-legs", type=int, default=None)
    s.add_argument("--iceberg-quantity", type=int, default=None)
    s.add_argument("--tag", default=None, help="Order tag (auto-generated if omitted; used for idempotency)")
    s.add_argument(
        "--idempotency-key",
        default=None,
        metavar="KEY",
        help="Durable idempotency key. On retry with the same key the "
             "prior attempt's result is replayed from the local SQLite cache. "
             "Also used to deterministically derive the Kite tag, so "
             "orderbook-based dedup still works across process restarts.",
    )
    s.add_argument("--group-id", default=None,
                   help="Attach this order to a multi-leg group (see `group-start`). "
                        "Lets `group-status` and `group-cancel` operate over related legs.")
    s.add_argument("--leg-name", default=None,
                   help="Human-readable leg label within the group, e.g. 'short_put'.")
    s.add_argument("--market-protection", type=float, default=None,
                   help="MARKET/SL-M slippage guard (SEBI-mandatory as of Apr 2026). "
                        "Defaults to -1 (Kite auto). Positive values = percent; e.g. 1.0 = +/-1%% of LTP.")
    s.add_argument("--skip-market-rules", action="store_true",
                   help="Bypass local hour/MIS-cutoff/freeze-qty/lot-size checks. "
                        "Kite's OMS will still enforce server-side — this just "
                        "skips the pre-flight. Use with care.")
    s.add_argument("--dry-run", action="store_true", help="Preview margin/charges via order_margins(); DO NOT place")
    s.add_argument("--wait-for-fill", type=float, default=0, help="Poll for N seconds until COMPLETE/REJECTED/CANCELLED (0=return immediately)")
    _add_yes(s)

    s = add("cancel", cmd_cancel, "Cancel one order by id")
    s.add_argument("--order-id", required=True)
    s.add_argument("--variety", default="regular", choices=["regular", "amo", "co", "iceberg"])
    _add_yes(s)

    s = add("modify", cmd_modify, "Modify an existing order")
    s.add_argument("--order-id", required=True)
    s.add_argument("--variety", default="regular", choices=["regular", "amo", "co", "iceberg"])
    s.add_argument("--quantity", type=int, default=None)
    s.add_argument("--price", type=float, default=None)
    s.add_argument("--trigger-price", type=float, default=None)
    s.add_argument("--order-type", default=None, choices=["MARKET", "LIMIT", "SL", "SL-M"])
    s.add_argument("--validity", default=None, choices=["DAY", "IOC", "TTL"])
    s.add_argument("--market-protection", type=float, default=None,
                   help="Update market_protection; only meaningful when modifying a MARKET/SL-M order.")
    _add_yes(s)

    s = add("cancel-all", cmd_cancel_all, "Cancel every open order")
    _add_yes(s)
    s.add_argument("--confirm-panic", action="store_true",
                   help="Required. Distinct from --yes — explicit acknowledgment "
                        "that this will cancel the ENTIRE open book.")

    # --- GTT ---
    add("gtt-list", cmd_gtt_list, "List active GTTs")

    s = add("gtt-get", cmd_gtt_get, "Get one GTT by trigger id")
    s.add_argument("--trigger-id", type=int, required=True)

    s = add("gtt-create", cmd_gtt_create, "Create a GTT (single or OCO two-leg)")
    s.add_argument("--exchange", required=True, choices=["NSE", "BSE", "NFO", "BFO", "MCX", "CDS", "BCD"])
    s.add_argument("--tradingsymbol", required=True)
    s.add_argument("--transaction-type", default="SELL", choices=["BUY", "SELL"])
    s.add_argument("--trigger-values", required=True, help="Comma-separated: '300' (single) or '280,320' (OCO)")
    s.add_argument("--last-price", type=float, required=True, help="Current/reference price")
    s.add_argument("--quantity", type=int, default=None)
    s.add_argument("--order-type", default="LIMIT", choices=["MARKET", "LIMIT", "SL", "SL-M"])
    s.add_argument("--product", default="CNC", choices=["CNC", "MIS", "NRML", "MTF"])
    s.add_argument("--price", type=float, default=None, help="Limit price for leg 1")
    s.add_argument("--price2", type=float, default=None, help="Limit price for OCO leg 2")
    s.add_argument("--orders-json", default=None, help="Full order legs as JSON (overrides simple args)")
    _add_yes(s)

    s = add("gtt-modify", cmd_gtt_modify, "Modify an existing GTT")
    s.add_argument("--trigger-id", type=int, required=True)
    s.add_argument("--exchange", required=True)
    s.add_argument("--tradingsymbol", required=True)
    s.add_argument("--trigger-values", required=True)
    s.add_argument("--last-price", type=float, required=True)
    s.add_argument("--orders-json", required=True, help="Full order legs as JSON")
    _add_yes(s)

    s = add("gtt-delete", cmd_gtt_delete, "Delete a GTT")
    s.add_argument("--trigger-id", type=int, required=True)
    _add_yes(s)

    # --- margin calc ---
    s = add("margin-calc", cmd_margin_calc, "Pre-trade margin for an order (or use --orders-json)")
    s.add_argument("--orders-json", default=None, help="JSON list of order legs (full form)")
    s.add_argument("--exchange", default=None)
    s.add_argument("--tradingsymbol", default=None)
    s.add_argument("--transaction-type", default=None, choices=["BUY", "SELL"])
    s.add_argument("--quantity", type=int, default=None)
    s.add_argument("--product", default=None, choices=["CNC", "MIS", "NRML", "MTF"])
    s.add_argument("--order-type", default=None, choices=["MARKET", "LIMIT", "SL", "SL-M"])
    s.add_argument("--variety", default=None)
    s.add_argument("--price", type=float, default=None)

    s = add("basket-margin", cmd_basket_margin, "Margin benefit for a basket of orders")
    s.add_argument("--orders-json", required=True)

    # --- MF ---
    add("mf-holdings", cmd_mf_holdings, "Mutual fund holdings")
    add("mf-orders", cmd_mf_orders, "Mutual fund orders")
    add("mf-sips", cmd_mf_sips, "Active mutual fund SIPs")
    add("mf-instruments", cmd_mf_instruments, "All mutual fund schemes")

    s = add("mf-place", cmd_mf_place, "Place a mutual fund order")
    s.add_argument("--tradingsymbol", required=True, help="MF scheme tradingsymbol")
    s.add_argument("--transaction-type", required=True, choices=["BUY", "SELL"])
    s.add_argument("--quantity", type=float, default=None, help="Units (for SELL)")
    s.add_argument("--amount", type=float, default=None, help="Amount in INR (for BUY)")
    s.add_argument("--tag", default=None)
    _add_yes(s)

    s = add("mf-cancel", cmd_mf_cancel, "Cancel a mutual fund order")
    s.add_argument("--order-id", required=True)
    _add_yes(s)

    s = add("mf-sip-create", cmd_mf_sip_create, "Create a mutual fund SIP")
    s.add_argument("--tradingsymbol", required=True)
    s.add_argument("--amount", type=float, required=True, help="SIP amount per instalment")
    s.add_argument("--initial-amount", type=float, default=None, help="First instalment amount")
    s.add_argument("--frequency", required=True, choices=["monthly", "weekly"])
    s.add_argument("--instalments", type=int, required=True)
    s.add_argument("--tag", default=None)
    _add_yes(s)

    s = add("mf-sip-modify", cmd_mf_sip_modify, "Modify a mutual fund SIP")
    s.add_argument("--sip-id", required=True)
    s.add_argument("--amount", type=float, default=None)
    s.add_argument("--frequency", default=None, choices=["monthly", "weekly"])
    s.add_argument("--instalments", type=int, default=None)
    s.add_argument("--status", default=None, choices=["active", "paused"])
    _add_yes(s)

    s = add("mf-sip-cancel", cmd_mf_sip_cancel, "Cancel a mutual fund SIP")
    s.add_argument("--sip-id", required=True)
    _add_yes(s)

    return p


def main(argv: list[str] | None = None) -> int:
    from kite_algo.audit import log_command
    from kite_algo.envelope import new_request_id, parent_request_id
    from kite_algo.exit_codes import classify_exception

    parser = build_parser()
    args = parser.parse_args(argv)
    # --explain is a meta-flag: skip the real command, emit a structured
    # description. This is purely local — no API call, no session, no risk.
    if getattr(args, "explain", False):
        from kite_algo.explain import explain
        _emit(explain(args.cmd), args.format, cmd=args.cmd)
        return 0

    # SEBI-compliant audit: log every invocation exactly once, with outcome.
    request_id = new_request_id()
    started_ms = int(time.time() * 1000)
    args_for_log = {
        k: v for k, v in vars(args).items()
        if k not in ("func",) and not callable(v)
    }
    rc: int = 0
    err_code: str | None = None
    try:
        rc = args.func(args)
    except KeyboardInterrupt:
        rc = 130
        err_code = "SIGINT"
    except SystemExit as exc:
        # Preserve explicit exit code; argparse returns 2 for usage errors.
        if isinstance(exc.code, int):
            rc = exc.code
        elif isinstance(exc.code, str):
            # Our _require_yes / _require_not_halted use SystemExit(str).
            print(exc.code, file=sys.stderr)
            rc = 2
            err_code = "USAGE"
        else:
            rc = 0
    except Exception as exc:
        cls = classify_exception(exc)
        rc = cls.exit_code
        err_code = cls.error_code
        print(f"ERROR: {_redact_secrets(str(exc))}", file=sys.stderr)
    finally:
        try:
            elapsed = int(time.time() * 1000) - started_ms
            log_command(
                cmd=args.cmd,
                request_id=request_id,
                args=args_for_log,
                exit_code=rc,
                error_code=err_code,
                elapsed_ms=elapsed,
                parent_request_id=parent_request_id(),
                strategy_id=os.getenv("KITE_STRATEGY_ID"),
                agent_id=os.getenv("KITE_AGENT_ID"),
            )
        except Exception:
            # Never let an audit-log failure crash the process.
            pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
