# Safety Model

Kite is live-money from day one; there is no broker-provided paper account.
Use `SimBroker` for offline execution tests.

## Live-write authorization

Every live write through either `kite_tool` or the strategy engine must pass
all applicable layers:

1. **Environment gates**
   - `TRADING_BROKER=kite`
   - `TRADING_ALLOW_LIVE=true`
   - `TRADING_LIVE_ENABLED=true`
   - `TRADING_DRY_RUN=false`
2. **Invocation gate**
   - Every `kite_tool` write command requires `--yes`.
   - If `TRADING_CONFIRM_TOKEN_REQUIRED=true`, `--confirm-token` must match
     `TRADING_ORDER_TOKEN`.
3. **Destructive-operation acknowledgements**
   - `cancel-all` additionally requires `--confirm-panic`.
   - `convert-position` additionally requires `--confirm-convert`.
4. **Kill switch**
   - A `data/HALTED` sentinel blocks every trading write while leaving
     read-only reconciliation commands available.
5. **Broker adapter gate**
   - `KiteBroker._require_live()` independently rechecks dry-run, both live
     flags, and the halt sentinel.

`TRADING_DRY_RUN=true` forces `place` into the read-only `order_margins()`
preview path even when `--dry-run` was omitted. Other write commands fail
closed while dry-run is active because cancel/modify/GTT/MF writes do not have
a meaningful preview operation.

Defaults are deliberately inert:

```dotenv
TRADING_BROKER=kite
TRADING_ALLOW_LIVE=false
TRADING_LIVE_ENABLED=false
TRADING_DRY_RUN=true
TRADING_CONFIRM_TOKEN_REQUIRED=false
```

## Validation and resilience

- Local order validation rejects malformed payloads before any API call.
- Market-hours, MIS-cutoff, freeze-quantity, and lot-size checks run before
  placement unless an operator explicitly uses `--skip-market-rules`.
- MARKET and SL-M orders receive `market_protection=-1` unless overridden.
- Placement uses deterministic/idempotent tags and a durable replay cache.
- Write traffic is limited to 10 requests/second, 200/minute, and 3000/day;
  historical, quote, and general traffic use separate buckets.
- Hard broker errors are never retried. Transient placement failures reconcile
  against the live orderbook by tag before any retry.

## Audit and reconciliation

Every normal `kite_tool` invocation appends one redacted NDJSON record under
`data/audit/YYYY-MM-DD.jsonl` with mode `0600`. Write outcomes include broker
order IDs or the relevant GTT/SIP/alert identifiers when available, plus the
configured Kite user ID. Confirmation tokens and broker secrets are never
persisted.

The strategy engine can additionally persist decisions, requests, responses,
and status transitions to SQLite when `TRADING_DB_PATH` is configured.

The local NDJSON files are an operator-side observability and reconciliation
artifact. They are not WORM storage and do not replace Zerodha/exchange audit
records. Production operators should back them up to access-controlled,
retention-managed storage and periodically run `reconcile`.

## Authentication

Kite sessions expire daily. Login remains a human OAuth/2FA action: the tool
may capture the loopback callback and exchange the one-time request token, but
it never automates credentials or TOTP. The session is written atomically with
mode `0600` to `data/session.json`, or to `KITE_SESSION_PATH` when configured.

## External compliance dependency

As of April 2026, Kite order placement must originate from a static public IP
whitelisted in the Kite developer account. Read-only endpoints do not prove
that this is configured. Verify primary/secondary IPs in
`developers.kite.trade`; the broker will reject order writes from unapproved
IPs.
