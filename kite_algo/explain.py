"""`--explain` descriptions per command.

`--explain` emits a structured description of what the command would do,
without making any API call or side effect. This differs from `--dry-run`,
which for `place` actually hits `order_margins()` — that consumes a rate
limit token and is pointless when the agent only wants to check its
understanding of a call.

Shape:

    {
      "action": "place_order",
      "side_effects": ["live order transmitted to Kite OMS", ...],
      "preconditions": ["market open on NSE", "session valid", ...],
      "reversibility": "cancellable while OPEN; post-fill requires offset trade",
      "idempotency": "tag-based orderbook dedup; add --idempotency-key for cross-process",
      "data_required": ["instrument existed in today's /instruments dump"],
      "rate_limit_bucket": "orders (10/s, 200/min, 3000/day)",
      "cost_bps": null,
      "notes": [...]
    }

One entry per subcommand; fallback is a minimal stub so `--explain` works
everywhere even if the detailed description isn't filled in yet.
"""

from __future__ import annotations

from typing import Any


# Per-command explanations. Add as commands land.
_EXPLANATIONS: dict[str, dict[str, Any]] = {
    # -----------------------------------------------------------------------
    # Auth
    # -----------------------------------------------------------------------
    "login": {
        "action": "open OAuth redirect, exchange request_token for access_token",
        "side_effects": [
            "opens a browser tab to kite.zerodha.com/connect/login",
            "writes data/session.json at mode 0o600",
            "invalidates the previous access_token (server-side)",
        ],
        "preconditions": [
            "KITE_API_KEY + KITE_API_SECRET env set",
            "a human available to complete the 2FA challenge",
        ],
        "reversibility": "session can be invalidated via `logout`; token auto-rotates ~06:45–07:30 IST daily",
        "idempotency": "each login invalidates the prior token",
        "rate_limit_bucket": "general (10/s)",
    },
    "logout": {
        "action": "invalidate access_token server-side + remove local session file",
        "side_effects": [
            "deletes data/session.json",
            "server-side invalidates the token",
        ],
        "preconditions": ["local session exists"],
        "reversibility": "re-run `login` to re-auth",
        "idempotency": "safe to call repeatedly; second call is a no-op",
    },
    "profile": {
        "action": "GET /user/profile",
        "side_effects": ["none — read-only"],
        "preconditions": ["valid session"],
        "reversibility": "n/a — read-only",
        "idempotency": "safe",
        "rate_limit_bucket": "general (10/s)",
    },
    "session": {
        "action": "print cached session metadata locally",
        "side_effects": ["none — reads data/session.json"],
        "preconditions": ["session file exists"],
        "reversibility": "n/a",
        "idempotency": "safe",
    },
    "health": {
        "action": "run 6 end-to-end checks: session, credentials, API, margins, market-data, instruments cache",
        "side_effects": ["small number of read-only Kite API calls"],
        "preconditions": ["valid session + credentials"],
        "reversibility": "n/a — read-only",
        "idempotency": "safe",
        "rate_limit_bucket": "general (10/s) + quote (1/s)",
    },

    # -----------------------------------------------------------------------
    # Account
    # -----------------------------------------------------------------------
    "margins": {
        "action": "GET /user/margins",
        "side_effects": ["none"],
        "preconditions": ["valid session"],
        "reversibility": "n/a",
        "idempotency": "safe",
        "rate_limit_bucket": "general (10/s)",
    },
    "holdings": {
        "action": "GET /portfolio/holdings (demat long positions)",
        "side_effects": ["none"],
        "preconditions": ["valid session"],
        "reversibility": "n/a",
        "idempotency": "safe",
        "rate_limit_bucket": "general (10/s)",
        "notes": ["Summary rollup available via --summary (count, invested, pnl, best/worst performer)"],
    },
    "positions": {
        "action": "GET /portfolio/positions (day + net)",
        "side_effects": ["none"],
        "preconditions": ["valid session"],
        "reversibility": "n/a",
        "idempotency": "safe",
        "rate_limit_bucket": "general (10/s)",
    },
    "orders": {
        "action": "GET /orders (today)",
        "side_effects": ["none"],
        "preconditions": ["valid session"],
        "reversibility": "n/a",
        "idempotency": "safe",
        "rate_limit_bucket": "general (10/s)",
        "notes": ["--summary rolls up status counts + open-oldest timestamp"],
    },
    "trades": {
        "action": "GET /trades (today's fills)",
        "side_effects": ["none"],
        "preconditions": ["valid session"],
        "reversibility": "n/a",
        "idempotency": "safe",
        "rate_limit_bucket": "general (10/s)",
    },

    # -----------------------------------------------------------------------
    # Market data
    # -----------------------------------------------------------------------
    "ltp": {
        "action": "GET /quote/ltp for one or more symbols (auto-batched 500 per call)",
        "side_effects": ["none"],
        "preconditions": ["valid session"],
        "reversibility": "n/a",
        "idempotency": "safe; data is point-in-time",
        "rate_limit_bucket": "quote (1/s)",
    },
    "quote": {
        "action": "GET /quote (full with OHLC + depth + OI)",
        "side_effects": ["none"],
        "preconditions": ["valid session"],
        "reversibility": "n/a",
        "idempotency": "safe",
        "rate_limit_bucket": "quote (1/s)",
    },
    "ohlc": {
        "action": "GET /quote/ohlc",
        "side_effects": ["none"],
        "preconditions": ["valid session"],
        "reversibility": "n/a",
        "idempotency": "safe",
        "rate_limit_bucket": "quote (1/s)",
    },
    "stream": {
        "action": "open WebSocket KiteTicker; subscribe to tokens; emit one NDJSON tick per line",
        "side_effects": [
            "holds a WebSocket connection (max 3 per API key)",
            "subscribes up to 3000 tokens per connection",
        ],
        "preconditions": ["valid session", "wss://ws.kite.trade reachable"],
        "reversibility": "Ctrl+C to close",
        "idempotency": "re-running duplicates ticks on the second connection",
        "rate_limit_bucket": "n/a — WebSocket is separate",
        "notes": [
            "Exits with code 2 on token-related errors so a supervisor can re-auth.",
            "Stream bypasses the envelope — emits bare NDJSON, not wrapped.",
        ],
    },
    "history": {
        "action": "GET /instruments/historical/:token/:interval (auto-chunked per Kite's per-interval lookback cap)",
        "side_effects": ["none"],
        "preconditions": ["historical data subscription", "valid instrument_token"],
        "reversibility": "n/a",
        "idempotency": "safe",
        "rate_limit_bucket": "historical (3/s)",
        "notes": [
            "Caps: minute=60d/call, 5m=100d, 15m=200d, 60m=400d, day=2000d.",
            "Intraday data for expired F&O contracts is NOT available.",
        ],
    },
    "instruments": {
        "action": "GET /instruments (gzipped CSV dump, cached to data/instruments/<EXCH>.json)",
        "side_effects": ["writes data/instruments/<EXCH>.json atomically"],
        "preconditions": ["valid session"],
        "reversibility": "n/a (cache auto-refreshes on TTL)",
        "idempotency": "safe; TTL = KITE_INSTRUMENTS_TTL_SECONDS (default 86400)",
        "rate_limit_bucket": "general (but dump is regenerated once/day ~08:00 IST)",
    },

    # -----------------------------------------------------------------------
    # Options
    # -----------------------------------------------------------------------
    "chain": {
        "action": "filter /instruments for one underlying+expiry, optionally enrich with /quote + BSM greeks",
        "side_effects": ["none"],
        "preconditions": [
            "instruments cache present (else fetches once)",
            "with --greeks: underlying spot resolvable via NSE equity LTP or index LTP",
        ],
        "reversibility": "n/a",
        "idempotency": "safe; point-in-time snapshot",
        "rate_limit_bucket": "quote (1/s) × ceil(strikes/500) when --quote is set",
        "notes": [
            "Greeks computed locally via Black-Scholes (Kite does not provide them).",
            "Default risk-free rate 6.5% (RBI repo); override via --risk-free-rate.",
            "--summary returns ATM / IV / put-call OI ratio / max-pain rollup instead of per-strike rows.",
        ],
    },

    # -----------------------------------------------------------------------
    # Orders — write
    # -----------------------------------------------------------------------
    "place": {
        "action": "POST /orders/:variety",
        "side_effects": [
            "live order transmitted to Kite OMS",
            "capital reservation per margin calculator",
            "position created on fill",
        ],
        "preconditions": [
            "valid session",
            "market open for the exchange (or --variety amo)",
            "sufficient margin in the appropriate segment",
            "instrument exists and is tradeable today",
            "freeze-qty and lot-size rules met",
            "static IP whitelisted for orders (SEBI Apr 2026)",
        ],
        "reversibility": "cancellable while OPEN via `cancel`; post-fill requires offset trade",
        "idempotency": (
            "tag-based orderbook dedup on in-process retry; "
            "cross-process via --idempotency-key backed by SQLite"
        ),
        "rate_limit_bucket": "orders (10/s, 200/min, 3000/day)",
        "notes": [
            "--dry-run previews margin via order_margins (uses one API call) without transmitting.",
            "--wait-for-fill N polls order_history until terminal or timeout.",
            "MARKET / SL-M orders now MUST include market_protection (SEBI Apr 2026).",
        ],
    },
    "cancel": {
        "action": "DELETE /orders/:variety/:order_id",
        "side_effects": ["cancels an open order"],
        "preconditions": ["order is in OPEN or TRIGGER PENDING state"],
        "reversibility": "n/a — re-place to replace",
        "idempotency": "repeated cancel on a terminal order returns InputException",
        "rate_limit_bucket": "orders (10/s)",
    },
    "modify": {
        "action": "PUT /orders/:variety/:order_id",
        "side_effects": ["changes qty/price/trigger/validity of an open order"],
        "preconditions": [
            "order is OPEN or TRIGGER PENDING",
            "fewer than ~20 prior modifications on this order_id (Kite cap is ~25)",
        ],
        "reversibility": "cancel to revert",
        "idempotency": (
            "per-order-id counter tracks modification count; "
            "repeated identical mod beyond 20 raises ModificationLimitExceeded"
        ),
        "rate_limit_bucket": "orders (10/s, 200/min)",
    },
    "cancel-all": {
        "action": "fetch /orders, cancel every OPEN / TRIGGER PENDING",
        "side_effects": ["destructive — cancels the entire book"],
        "preconditions": ["valid session"],
        "reversibility": "n/a",
        "idempotency": "repeated calls cancel nothing more; safe",
        "rate_limit_bucket": "orders (10/s × count)",
        "notes": [
            "Requires --yes.",
            "Wave 3 will add a stricter --confirm-panic token.",
        ],
    },

    # -----------------------------------------------------------------------
    # GTT
    # -----------------------------------------------------------------------
    "gtt-create": {
        "action": "POST /gtt/triggers (single or two-leg OCO)",
        "side_effects": [
            "persists a server-side trigger on Kite (up to 50 active per user)",
            "triggered order fires asynchronously when LTP crosses condition",
        ],
        "preconditions": ["trigger prices within a reasonable distance from current LTP"],
        "reversibility": "delete via `gtt-delete` or modify via `gtt-modify`",
        "idempotency": "each create emits a new trigger_id",
        "rate_limit_bucket": "general (10/s)",
        "notes": [
            "Expiry: 365 days from creation.",
            "OCO non-firing leg removal is best-effort, not atomic.",
        ],
    },
    "gtt-delete": {
        "action": "DELETE /gtt/triggers/:id",
        "side_effects": ["removes the trigger"],
        "preconditions": ["trigger exists and is active"],
        "reversibility": "n/a",
        "idempotency": "repeated delete returns a server-side not-found",
        "rate_limit_bucket": "general (10/s)",
    },
}


_FALLBACK = {
    "action": "(command description not yet filled in)",
    "side_effects": ["see CLI_REFERENCE.md"],
    "preconditions": ["valid session"],
    "reversibility": "see CLI_REFERENCE.md",
    "idempotency": "see CLI_REFERENCE.md",
}


def explain(cmd: str) -> dict[str, Any]:
    """Return the explanation record for a subcommand name."""
    if cmd in _EXPLANATIONS:
        return dict(_EXPLANATIONS[cmd])
    return dict(_FALLBACK, command=cmd)


def all_explanations() -> dict[str, dict[str, Any]]:
    """Every command name → its explanation. For `tools describe`."""
    return {name: dict(body) for name, body in _EXPLANATIONS.items()}
