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
import logging
import math
import os
import signal
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
from kite_algo.logging_setup import configure_logging
from kite_algo.resilience import (
    IdempotentOrderPlacer,
    KiteRateLimiter,
    find_order_by_tag,
    new_order_tag,
    retry_with_backoff,
)
from kite_algo.validation import format_errors, validate_order


# -----------------------------------------------------------------------------
# Bootstrap
# -----------------------------------------------------------------------------

load_dotenv()
configure_logging(level=logging.INFO if os.getenv("KITE_DEBUG") else logging.WARNING)
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
        _emit(checks, args.format)
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
        _emit(checks, args.format)
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

    _emit(checks, args.format)
    return 0 if overall_ok else 1


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


def cmd_convert_position(args: argparse.Namespace) -> int:
    """Convert position product type (e.g. MIS → CNC, NRML → MIS)."""
    _require_yes(args, "convert a position")
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
    _emit(out, args.format)
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

    _emit(rows, args.format)
    return 0


def cmd_orders(args: argparse.Namespace) -> int:
    client = _new_client()
    _emit(client.orders(), args.format)
    return 0


def cmd_open_orders(args: argparse.Namespace) -> int:
    """Show only OPEN / TRIGGER PENDING orders."""
    client = _new_client()
    orders = client.orders() or []
    open_only = [o for o in orders if o.get("status") in ("OPEN", "TRIGGER PENDING")]
    _emit(open_only, args.format)
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
        _emit(rows, args.format)
    return 0


def cmd_stream(args: argparse.Namespace) -> int:
    """Live WebSocket tick stream via KiteTicker."""
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
                print(f"WARN: could not resolve token for {sym}", file=sys.stderr)
    if not tokens:
        print("ERROR: no instrument tokens. Use --symbols or --tokens.", file=sys.stderr)
        return 1

    mode_map = {"ltp": "ltp", "quote": "quote", "full": "full"}
    subscribe_mode = mode_map.get(args.mode, "full")

    duration = args.duration
    start_time = time.time()

    # KiteTicker has built-in reconnection (reconnect=True by default, 50
    # retries, 60s max delay). We bump tries and expose reconnect callbacks.
    kws = KiteTicker(
        cfg.api_key,
        cfg.access_token,
        reconnect=True,
        reconnect_max_tries=args.reconnect_max_tries,
        reconnect_max_delay=args.reconnect_max_delay,
    )

    def on_ticks(ws: Any, ticks: list[dict]) -> None:
        for tick in ticks:
            print(json.dumps(_to_jsonable(tick), default=str), flush=True)

    def on_connect(ws: Any, response: Any) -> None:
        log.info("ws connected — subscribing %d tokens mode=%s", len(tokens), subscribe_mode)
        ws.subscribe(tokens)
        ws.set_mode(subscribe_mode, tokens)

    def on_close(ws: Any, code: Any, reason: Any) -> None:
        log.warning("ws closed: code=%s reason=%s", code, reason)

    def on_error(ws: Any, code: Any, reason: Any) -> None:
        log.error("ws error: code=%s reason=%s", code, reason)

    def on_reconnect(ws: Any, attempts: int) -> None:
        log.warning("ws reconnecting (attempt %d)", attempts)

    def on_noreconnect(ws: Any) -> None:
        log.error("ws gave up reconnecting after %d tries", args.reconnect_max_tries)

    def on_order_update(ws: Any, data: dict) -> None:
        print(json.dumps({"_type": "order_update", **_to_jsonable(data)}, default=str), flush=True)

    kws.on_ticks = on_ticks
    kws.on_connect = on_connect
    kws.on_close = on_close
    kws.on_error = on_error
    kws.on_reconnect = on_reconnect
    kws.on_noreconnect = on_noreconnect
    if args.order_updates:
        kws.on_order_update = on_order_update

    # Duration-based stop
    if duration > 0:
        def _alarm(signum: int, frame: Any) -> None:
            print(f"\n{duration}s elapsed, disconnecting.", file=sys.stderr)
            kws.close()
        signal.signal(signal.SIGALRM, _alarm)
        signal.alarm(int(duration))

    try:
        kws.connect(threaded=False)
    except KeyboardInterrupt:
        kws.close()
    return 0


# =============================================================================
# HISTORICAL / INSTRUMENTS
# =============================================================================

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
    _emit(match, args.format)
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
        quote_data: dict[str, Any] = {}
        BATCH = 500
        for i in range(0, len(symbols), BATCH):
            quote_data.update(client.quote(symbols[i:i + BATCH]) or {})

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

    _emit(out, args.format)
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
    _emit({"iv_pct": round(iv * 100, 2), "iv_decimal": round(iv, 6)}, args.format)
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
    _emit(out, args.format)
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
    )
    if errors:
        print(format_errors(errors), file=sys.stderr)
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

    # --- 3. --dry-run: preview margin + charges, no transmission ------------
    if args.dry_run:
        preview_order = {
            "exchange": args.exchange,
            "tradingsymbol": args.tradingsymbol,
            "transaction_type": args.transaction_type,
            "variety": args.variety,
            "product": args.product,
            "order_type": args.order_type,
            "quantity": args.quantity,
            "price": args.price or 0,
        }
        try:
            preview = client.order_margins([preview_order])
        except Exception as exc:
            print(f"ERROR: dry-run margin preview failed: {exc}", file=sys.stderr)
            return 1
        print("=== DRY RUN — no order transmitted ===", file=sys.stderr)
        _emit(preview, args.format)
        return 0

    # --- 4. Idempotent placement -------------------------------------------
    tag = args.tag or new_order_tag()
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

    _emit(result, args.format)
    return 0


def _wait_for_fill(client: Any, order_id: str, *, timeout: float) -> dict:
    """Poll order_history until order reaches a terminal state or timeout."""
    terminal = {"COMPLETE", "REJECTED", "CANCELLED"}
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        try:
            _RATE_LIMITER.wait_general()
            history = client.order_history(order_id) or []
            if history:
                last = history[-1]
                if last.get("status") in terminal:
                    return last
        except Exception as exc:
            log.warning("order_history poll failed: %s", exc)
        time.sleep(0.5)
    return last or {"status": "TIMEOUT"}


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
    """Create a GTT (Good Till Triggered) order.

    Single-leg: one trigger value + one order.
    Two-leg (OCO): two trigger values (stoploss, target) + two orders.
    """
    _require_yes(args, "create a GTT")
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
    _emit({"trigger_id": trigger_id}, args.format)
    return 0


def cmd_gtt_modify(args: argparse.Namespace) -> int:
    """Modify an existing GTT trigger."""
    _require_yes(args, "modify a GTT")
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
    _emit({"trigger_id": trigger_id}, args.format)
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

def _mf_subscription_hint() -> str:
    return (
        "Mutual fund API requires a separate Kite Connect MF subscription. "
        "Enable at https://kite.zerodha.com/connect/apps (MF add-on)."
    )


def cmd_mf_holdings(args: argparse.Namespace) -> int:
    client = _new_client()
    try:
        _emit(client.mf_holdings(), args.format)
    except Exception as exc:
        print(f"ERROR: mf_holdings failed: {exc}", file=sys.stderr)
        print(_mf_subscription_hint(), file=sys.stderr)
        return 1
    return 0


def cmd_mf_orders(args: argparse.Namespace) -> int:
    client = _new_client()
    try:
        _emit(client.mf_orders(), args.format)
    except Exception as exc:
        print(f"ERROR: mf_orders failed: {exc}", file=sys.stderr)
        print(_mf_subscription_hint(), file=sys.stderr)
        return 1
    return 0


def cmd_mf_sips(args: argparse.Namespace) -> int:
    client = _new_client()
    try:
        _emit(client.mf_sips(), args.format)
    except Exception as exc:
        print(f"ERROR: mf_sips failed: {exc}", file=sys.stderr)
        print(_mf_subscription_hint(), file=sys.stderr)
        return 1
    return 0


def cmd_mf_instruments(args: argparse.Namespace) -> int:
    client = _new_client()
    data = client.mf_instruments()
    _emit(data, args.format)
    return 0


def cmd_mf_place(args: argparse.Namespace) -> int:
    _require_yes(args, "place a mutual fund order")
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
    _emit({"order_id": order_id}, args.format)
    return 0


def cmd_mf_cancel(args: argparse.Namespace) -> int:
    _require_yes(args, "cancel a mutual fund order")
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
    _emit({"sip_id": sip_id}, args.format)
    return 0


def cmd_mf_sip_modify(args: argparse.Namespace) -> int:
    _require_yes(args, "modify a mutual fund SIP")
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

    def _add_yes(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--yes", action="store_true", help="Required live confirmation flag")

    # --- auth ---
    s = add("login", cmd_login, "Interactive OAuth login (writes data/session.json)")
    s.add_argument("--request-token", default=None, help="Skip prompt; use this request_token")
    s.add_argument("--no-browser", action="store_true")

    add("profile", cmd_profile, "User profile (verify session)")
    add("session", cmd_session, "Current session status + approx expiry")
    add("health", cmd_health, "End-to-end health check (session, API, margins, market data)")
    add("logout", cmd_logout, "Invalidate access token + remove local session file")

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
    _add_yes(s)

    s = add("cancel-all", cmd_cancel_all, "Cancel every open order")
    _add_yes(s)

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
