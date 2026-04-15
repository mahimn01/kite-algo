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
import json
import math
import os
import sys
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


# -----------------------------------------------------------------------------
# Bootstrap
# -----------------------------------------------------------------------------

load_dotenv()


def _import_kiteconnect() -> Any:
    try:
        from kiteconnect import KiteConnect  # type: ignore
    except ImportError as exc:
        print(f"ERROR: kiteconnect is not installed: {exc}", file=sys.stderr)
        print("Install: pip install kiteconnect", file=sys.stderr)
        sys.exit(2)
    return KiteConnect


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


def _emit(data: Any, fmt: str) -> None:
    if fmt == "json":
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
    # table
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
    """Instantiate a KiteConnect client with credentials + access token from env."""
    KiteConnect = _import_kiteconnect()
    cfg = KiteConfig.from_env()
    if require_session:
        cfg.require_session()
    else:
        cfg.require_credentials()
    client = KiteConnect(api_key=cfg.api_key)
    if cfg.access_token:
        client.set_access_token(cfg.access_token)
    return client


# =============================================================================
# AUTH / SESSION
# =============================================================================

def cmd_login(args: argparse.Namespace) -> int:
    """Interactive OAuth login.

    1. Build the Kite login URL and open it in a browser.
    2. User signs in with Zerodha creds + 2FA.
    3. Kite redirects to the configured redirect_uri with ?request_token=...
    4. User copies the request_token and pastes it into the prompt.
    5. We exchange request_token + api_secret → access_token via generate_session().
    6. Write data/session.json.
    """
    KiteConnect = _import_kiteconnect()
    cfg = KiteConfig.from_env()
    cfg.require_credentials()

    client = KiteConnect(api_key=cfg.api_key)
    login_url = client.login_url()
    print(f"Opening Kite login URL:\n  {login_url}", file=sys.stderr)

    if not args.no_browser:
        try:
            webbrowser.open(login_url)
        except Exception:
            pass

    if args.request_token:
        request_token = args.request_token
    else:
        print(
            "\nAfter signing in, copy the `request_token` from the redirect URL "
            "and paste it below.",
            file=sys.stderr,
        )
        request_token = input("request_token: ").strip()

    if not request_token:
        print("ERROR: empty request_token", file=sys.stderr)
        return 1

    try:
        data = client.generate_session(request_token, api_secret=cfg.api_secret)
    except Exception as exc:
        print(f"ERROR: generate_session failed: {exc}", file=sys.stderr)
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
    _emit({k: v for k, v in session.items() if k not in ("access_token", "public_token")}, args.format)
    return 0


def cmd_profile(args: argparse.Namespace) -> int:
    client = _new_client()
    _emit(client.profile(), args.format)
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
    _emit(out, args.format)
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
    _emit(data, args.format)
    return 0


def cmd_holdings(args: argparse.Namespace) -> int:
    client = _new_client()
    _emit(client.holdings(), args.format)
    return 0


def cmd_positions(args: argparse.Namespace) -> int:
    client = _new_client()
    data = client.positions() or {}
    which = args.which or "net"
    _emit(data.get(which, []), args.format)
    return 0


def cmd_orders(args: argparse.Namespace) -> int:
    client = _new_client()
    _emit(client.orders(), args.format)
    return 0


def cmd_trades(args: argparse.Namespace) -> int:
    client = _new_client()
    _emit(client.trades(), args.format)
    return 0


def cmd_order_history(args: argparse.Namespace) -> int:
    client = _new_client()
    _emit(client.order_history(args.order_id), args.format)
    return 0


def cmd_order_trades(args: argparse.Namespace) -> int:
    client = _new_client()
    _emit(client.order_trades(args.order_id), args.format)
    return 0


# =============================================================================
# QUOTES
# =============================================================================

def _split_symbols(raw: str) -> list[str]:
    return [s.strip() for s in raw.split(",") if s.strip()]


def cmd_ltp(args: argparse.Namespace) -> int:
    client = _new_client()
    symbols = _split_symbols(args.symbols)
    data = client.ltp(symbols)
    rows = [
        {"symbol": k, "instrument_token": v.get("instrument_token"), "last_price": v.get("last_price")}
        for k, v in (data or {}).items()
    ]
    _emit(rows, args.format)
    return 0


def cmd_ohlc(args: argparse.Namespace) -> int:
    client = _new_client()
    symbols = _split_symbols(args.symbols)
    data = client.ohlc(symbols)
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
    _emit(rows, args.format)
    return 0


def cmd_quote(args: argparse.Namespace) -> int:
    client = _new_client()
    symbols = _split_symbols(args.symbols)
    data = client.quote(symbols)
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
        _emit(rows, args.format)
    else:
        _emit(data, args.format)
    return 0


def cmd_stream(args: argparse.Namespace) -> int:
    print(
        "ERROR: `stream` (KiteTicker WebSocket) is not yet implemented. "
        "Coming next — will stream live ticks for the given instrument tokens.",
        file=sys.stderr,
    )
    return 2


# =============================================================================
# HISTORICAL / INSTRUMENTS
# =============================================================================

def cmd_history(args: argparse.Namespace) -> int:
    client = _new_client()
    if args.instrument_token is None:
        # Resolve symbol via the local instruments cache
        token = _resolve_token(client, args.symbol, args.exchange)
        if token is None:
            print(f"ERROR: could not resolve instrument token for {args.exchange}:{args.symbol}", file=sys.stderr)
            return 1
    else:
        token = args.instrument_token

    to_d = datetime.fromisoformat(args.to) if args.to else datetime.now()
    from_d = datetime.fromisoformat(args.from_) if args.from_ else (to_d - timedelta(days=args.days))

    bars = client.historical_data(
        instrument_token=token,
        from_date=from_d,
        to_date=to_d,
        interval=args.interval,
        continuous=args.continuous,
        oi=args.oi,
    ) or []
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
    _emit(rows, args.format)
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
    INSTRUMENTS_CACHE.mkdir(parents=True, exist_ok=True)
    path = _instruments_cache_path(exchange)
    # dates are not JSON-serializable out of the box
    clean = []
    for r in rows:
        c = dict(r)
        for k, v in list(c.items()):
            if isinstance(v, (date, datetime)):
                c[k] = v.isoformat()
        clean.append(c)
    path.write_text(json.dumps({"fetched_at": time.time(), "rows": clean}), encoding="utf-8")
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
        _emit(rows, args.format)
        return 0
    # Default: print a summary
    by_segment: dict[str, int] = {}
    for r in rows:
        by_segment[r.get("segment", "UNKNOWN")] = by_segment.get(r.get("segment", "UNKNOWN"), 0) + 1
    out = {
        "exchange": args.exchange or "ALL",
        "count": len(rows),
        "by_segment": by_segment,
        "cache_path": str(_instruments_cache_path(args.exchange)),
    }
    _emit(out, args.format)
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
    _emit(out, args.format)
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
    _emit([{"expiry": e} for e in expiries], args.format)
    return 0


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
        quote_data: dict[str, Any] = {}
        BATCH = 500
        for i in range(0, len(symbols), BATCH):
            quote_data.update(client.quote(symbols[i:i + BATCH]) or {})

        out = []
        for r in chain:
            key = f"NFO:{r.get('tradingsymbol')}"
            q = quote_data.get(key, {}) or {}
            out.append({
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
            })
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
    _emit(out, args.format)
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
    out = {
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
        # TODO: locally computed greeks via Black-Scholes using underlying spot + IV solve
    }
    _emit(out, args.format)
    return 0


# =============================================================================
# ORDERS (write path — stubbed, gated)
# =============================================================================

def _require_yes(args: argparse.Namespace, action: str) -> None:
    if not getattr(args, "yes", False):
        raise SystemExit(
            f"Refusing to {action} without --yes. All order-placing commands "
            f"require an explicit --yes confirmation flag."
        )


def cmd_place(args: argparse.Namespace) -> int:
    _require_yes(args, "place an order")
    print(
        "ERROR: `place` is a safety-gated stub pending KiteBroker write-path "
        "implementation. Edit kite_algo/broker/kite.py to enable.",
        file=sys.stderr,
    )
    return 2


def cmd_cancel(args: argparse.Namespace) -> int:
    _require_yes(args, "cancel an order")
    client = _new_client()
    client.cancel_order(variety=args.variety, order_id=args.order_id)
    print(f"cancel sent: variety={args.variety} order_id={args.order_id}", file=sys.stderr)
    return 0


def cmd_modify(args: argparse.Namespace) -> int:
    _require_yes(args, "modify an order")
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
    client.modify_order(variety=args.variety, order_id=args.order_id, **kwargs)
    print(f"modify sent: variety={args.variety} order_id={args.order_id}", file=sys.stderr)
    return 0


def cmd_cancel_all(args: argparse.Namespace) -> int:
    _require_yes(args, "cancel ALL open orders")
    client = _new_client()
    orders = client.orders() or []
    cancelled = []
    for o in orders:
        if o.get("status") in ("OPEN", "TRIGGER PENDING"):
            try:
                client.cancel_order(variety=o.get("variety", "regular"), order_id=o.get("order_id"))
                cancelled.append(o.get("order_id"))
            except Exception as exc:
                print(f"WARN: failed to cancel {o.get('order_id')}: {exc}", file=sys.stderr)
    _emit({"cancelled": cancelled, "count": len(cancelled)}, args.format)
    return 0


# =============================================================================
# GTT
# =============================================================================

def cmd_gtt_list(args: argparse.Namespace) -> int:
    client = _new_client()
    _emit(client.get_gtts(), args.format)
    return 0


def cmd_gtt_get(args: argparse.Namespace) -> int:
    client = _new_client()
    _emit(client.get_gtt(trigger_id=args.trigger_id), args.format)
    return 0


def cmd_gtt_delete(args: argparse.Namespace) -> int:
    _require_yes(args, "delete a GTT")
    client = _new_client()
    client.delete_gtt(trigger_id=args.trigger_id)
    print(f"deleted gtt trigger_id={args.trigger_id}", file=sys.stderr)
    return 0


def cmd_gtt_create(args: argparse.Namespace) -> int:
    _require_yes(args, "create a GTT")
    print(
        "ERROR: `gtt-create` stub pending — the full create flow needs order "
        "leg JSON parsing. Coming next.",
        file=sys.stderr,
    )
    return 2


def cmd_gtt_modify(args: argparse.Namespace) -> int:
    _require_yes(args, "modify a GTT")
    print("ERROR: `gtt-modify` stub pending.", file=sys.stderr)
    return 2


# =============================================================================
# MARGIN CALC
# =============================================================================

def cmd_margin_calc(args: argparse.Namespace) -> int:
    client = _new_client()
    try:
        orders = json.loads(args.orders_json)
    except json.JSONDecodeError as exc:
        print(f"ERROR: --orders-json must be valid JSON: {exc}", file=sys.stderr)
        return 1
    data = client.order_margins(orders)
    _emit(data, args.format)
    return 0


def cmd_basket_margin(args: argparse.Namespace) -> int:
    client = _new_client()
    try:
        orders = json.loads(args.orders_json)
    except json.JSONDecodeError as exc:
        print(f"ERROR: --orders-json must be valid JSON: {exc}", file=sys.stderr)
        return 1
    data = client.basket_order_margins(orders)
    _emit(data, args.format)
    return 0


# =============================================================================
# MUTUAL FUNDS
# =============================================================================

def cmd_mf_holdings(args: argparse.Namespace) -> int:
    client = _new_client()
    _emit(client.mf_holdings(), args.format)
    return 0


def cmd_mf_orders(args: argparse.Namespace) -> int:
    client = _new_client()
    _emit(client.mf_orders(), args.format)
    return 0


def cmd_mf_sips(args: argparse.Namespace) -> int:
    client = _new_client()
    _emit(client.mf_sips(), args.format)
    return 0


# =============================================================================
# Argparse wiring
# =============================================================================

def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--format", choices=["json", "csv", "table"], default="table")


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

    # --- auth ---
    s = add("login", cmd_login, "Interactive OAuth login (writes data/session.json)")
    s.add_argument("--request-token", default=None, help="Skip prompt; use this request_token")
    s.add_argument("--no-browser", action="store_true")

    add("profile", cmd_profile, "User profile (verify session)")
    add("session", cmd_session, "Current session status + approx expiry")
    add("logout", cmd_logout, "Invalidate access token + remove local session file")

    # --- account ---
    s = add("margins", cmd_margins, "Account margins")
    s.add_argument("--segment", choices=["equity", "commodity"], default=None)

    add("holdings", cmd_holdings, "Demat holdings")

    s = add("positions", cmd_positions, "Day or net positions")
    s.add_argument("--which", choices=["net", "day"], default="net")

    add("orders", cmd_orders, "Today's orders")
    add("trades", cmd_trades, "Today's trades")

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

    s = add("stream", cmd_stream, "Live WebSocket tick stream (KiteTicker) — stub")
    s.add_argument("--symbols", default="")
    s.add_argument("--duration", type=float, default=30.0)

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

    # --- options ---
    s = add("expiries", cmd_expiries, "List NFO expiries for an underlying")
    s.add_argument("--symbol", required=True, help="Underlying name (e.g. NIFTY, BANKNIFTY, RELIANCE)")

    s = add("chain", cmd_chain, "Full option chain for one expiry")
    s.add_argument("--symbol", required=True)
    s.add_argument("--expiry", required=True, help="YYYY-MM-DD")
    s.add_argument("--quote", action="store_true", help="Also fetch live quotes for every strike")

    s = add("option-quote", cmd_option_quote, "Quote for a single option leg")
    s.add_argument("--symbol", required=True)
    s.add_argument("--expiry", required=True)
    s.add_argument("--strike", type=float, required=True)
    s.add_argument("--right", choices=["CE", "PE"], required=True)

    # --- orders (write path) ---
    def _add_yes(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--yes", action="store_true", help="Required live confirmation flag")

    s = add("place", cmd_place, "Place a single order (pending implementation)")
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
    _add_yes(s)

    s = add("cancel-all", cmd_cancel_all, "Cancel every open order")
    _add_yes(s)

    # --- GTT ---
    add("gtt-list", cmd_gtt_list, "List active GTTs")

    s = add("gtt-get", cmd_gtt_get, "Get one GTT by trigger id")
    s.add_argument("--trigger-id", type=int, required=True)

    s = add("gtt-create", cmd_gtt_create, "Create a GTT (pending implementation)")
    _add_yes(s)

    s = add("gtt-modify", cmd_gtt_modify, "Modify a GTT (pending implementation)")
    s.add_argument("--trigger-id", type=int, required=True)
    _add_yes(s)

    s = add("gtt-delete", cmd_gtt_delete, "Delete a GTT")
    s.add_argument("--trigger-id", type=int, required=True)
    _add_yes(s)

    # --- margin calc ---
    s = add("margin-calc", cmd_margin_calc, "Pre-trade margin for an order list")
    s.add_argument("--orders-json", required=True, help="JSON list of order legs")

    s = add("basket-margin", cmd_basket_margin, "Margin benefit for a basket")
    s.add_argument("--orders-json", required=True)

    # --- MF ---
    add("mf-holdings", cmd_mf_holdings, "Mutual fund holdings")
    add("mf-orders", cmd_mf_orders, "Mutual fund orders")
    add("mf-sips", cmd_mf_sips, "Active mutual fund SIPs")

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except SystemExit:
        raise
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
