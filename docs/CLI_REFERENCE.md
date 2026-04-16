# kite-algo CLI Reference

Complete command reference for `kite_algo.kite_tool` — the Kite Connect (Zerodha)
CLI. This document is designed for agents and humans alike: every command is
documented with exact syntax, required/optional arguments, expected output,
common errors, and examples.

---

## Table of contents

1. [Setup & daily ritual](#setup--daily-ritual)
2. [Invocation conventions](#invocation-conventions)
3. [Global flags](#global-flags)
4. [Safety model](#safety-model)
5. [Rate limits & error classification](#rate-limits--error-classification)
6. [Commands](#commands)
   - [Auth / session](#auth--session)
   - [Account / portfolio](#account--portfolio)
   - [Orders (read)](#orders-read)
   - [Orders (write)](#orders-write)
   - [Market data](#market-data)
   - [Historical & instruments](#historical--instruments)
   - [Options](#options)
   - [GTT (good-till-triggered)](#gtt-good-till-triggered)
   - [Margin calc](#margin-calc)
   - [Mutual funds](#mutual-funds)
7. [Workflows](#workflows)
8. [Error handling patterns](#error-handling-patterns)
9. [Appendix: enum values](#appendix-enum-values)

---

## Setup & daily ritual

### One-time setup

```bash
cd /Users/mahimnpatel/Documents/Dev/kite-algo
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
# Fill .env with KITE_API_KEY, KITE_API_SECRET, KITE_USER_ID
```

### Every morning (Kite access tokens rotate at ~6 AM IST)

```bash
.venv/bin/python -m kite_algo.kite_tool login
# → browser opens, sign in with Zerodha creds + TOTP
# → URL redirects to http://127.0.0.1/?request_token=XXXXX&...
# → paste request_token at prompt (input is hidden via getpass)

.venv/bin/python -m kite_algo.kite_tool health
# → expect 6/6 green:
#   session_file, credentials, api_reachable, margins, market_data, instruments_cache
```

If any `health` check fails, fix before placing any orders.

---

## Invocation conventions

```bash
cd /Users/mahimnpatel/Documents/Dev/kite-algo
.venv/bin/python -m kite_algo.kite_tool <command> [options]
```

All commands:
- Accept `--format {json,csv,table}`, default `table`.
- Read credentials from `.env` + `data/session.json` automatically.
- Are rate-limited automatically via `RateLimitedKiteClient`.
- Log to stderr; structured data to stdout (so you can pipe `--format json | jq`).

Set `KITE_DEBUG=1` to enable INFO-level logs (rate-limit waits, retry attempts,
orderbook polls).

---

## Global flags

| Flag | Applies to | Purpose |
|------|-----------|---------|
| `--format {json,csv,table}` | all | Output format. `json` is safest for downstream parsing. |
| `--yes` | every write cmd | Explicit confirmation gate. All commands that place / modify / cancel **require** this. |

---

## Safety model

This CLI has four independent safety layers. An agent must respect all of them.

1. **`--yes` gate** — every write command refuses to execute without it.
   Commands that require `--yes`: `place`, `cancel`, `modify`, `cancel-all`,
   `convert-position`, `gtt-create`, `gtt-modify`, `gtt-delete`, `mf-place`,
   `mf-cancel`, `mf-sip-create`, `mf-sip-modify`, `mf-sip-cancel`.

2. **Pre-flight validation** — `place` runs 19+ client-side checks before any
   API call. Invalid orders exit 1 with field-specific messages and **zero
   API quota consumed**.

3. **Quantity guardrail** — `KITE_MAX_QUANTITY` env (default 100,000) catches
   `--quantity` typos. Override for genuinely larger clips.

4. **Idempotent placement** — every `place` auto-generates a unique tag (or
   accepts `--tag`). On transient failure, the placer polls the orderbook by
   tag before retrying. Never double-fills.

5. **Dry-run** — `place --dry-run` calls `order_margins()` for a margin
   preview only. The payload is field-whitelisted; `price` is omitted for
   MARKET orders. Banner reads `=== DRY RUN — margin preview only. NO order
   transmitted. ===`

**A trade is only actually sent if all of these are true:**
- `--yes` present
- All 19 validation rules pass
- Quantity ≤ `KITE_MAX_QUANTITY`
- `--dry-run` NOT set
- Idempotent placer succeeds (or the order appears in the orderbook after a
  transient failure, in which case we just return its id)

---

## Rate limits & error classification

### Kite limits (per API key, 2026)

| Bucket | Limit |
|--------|-------|
| General GETs | 10 req/s |
| Historical data | 3 req/s |
| Order placement | 10 req/s + 200/min + 3000–5000/day |
| WebSocket connections | 3 per API key |
| Subscribed instruments | 3000 per connection |

The CLI's `RateLimitedKiteClient` enforces these automatically — you cannot
accidentally exceed them even in tight loops.

### Exception classification

| Kite SDK exception | Classification | Retry? |
|---|---|---|
| `TokenException` | Hard — session rotated/invalidated (daily at 6am IST). | Never |
| `InputException` | Hard — bad parameters / price outside circuit. | Never |
| `OrderException` | Hard — OMS reject (margin, holdings, risk). | Never |
| `PermissionException` | Hard — account lacks feature. | Never |
| `NetworkException` | Transient. | Yes, with orderbook check for orders |
| `DataException` | Transient. | Yes |
| `GeneralException` | Hard — catch-all for account blocks. NOT retried. | Never |

String patterns also classified as transient: `timeout`, `timed out`, `connection reset`,
`429`, `500`, `502`, `503`, `504`, `rate limit`, `too many requests`.

---

## Commands

### Auth / session

#### `login`
Interactive OAuth login. Opens browser, prompts for `request_token` via
`getpass` (no echo, no shell history), exchanges for access token, writes
`data/session.json` with mode 0600.

```bash
.venv/bin/python -m kite_algo.kite_tool login [--no-browser]
```

`--no-browser` prints URL without launching (useful for headless / SSH).

#### `profile`
```bash
.venv/bin/python -m kite_algo.kite_tool profile [--format json]
```
Returns user_id, user_name, email, broker, exchanges, product_types,
order_types.

#### `session`
```bash
.venv/bin/python -m kite_algo.kite_tool session
```
Shows cached session info + approximate expiry time (IST 06:00 next day).

#### `health`
End-to-end probe. 6 checks: session file, credentials, API reachable, margins,
market data, instruments cache. Exit 0 if all pass, 1 otherwise.

```bash
.venv/bin/python -m kite_algo.kite_tool health
```

#### `logout`
```bash
.venv/bin/python -m kite_algo.kite_tool logout
```
Invalidates the access token server-side and removes `data/session.json`.

---

### Account / portfolio

#### `margins [--segment {equity,commodity}]`
Full margin breakdown. Default: all segments.

#### `holdings`
Demat holdings (CNC-style long positions carried overnight).

#### `positions [--which {net,day}]`
`net` = position + current quantity. `day` = today's trades only.

#### `convert-position --yes`
Convert product type (MIS ↔ CNC ↔ NRML). Use before 15:20 IST to avoid
MIS auto-squareoff.

```bash
.venv/bin/python -m kite_algo.kite_tool convert-position \
  --exchange NSE --tradingsymbol RELIANCE \
  --transaction-type BUY --position-type day \
  --quantity 10 \
  --old-product MIS --new-product CNC --yes
```

Position types: `day`, `overnight`. Products: `CNC | MIS | NRML | MTF`.

#### `pnl`
Aggregated P&L: realised, unrealised, day m2m, day buy/sell value, open count.

#### `portfolio`
Combined view: holdings (CNC) + non-zero positions. One table, MTM values,
day change.

---

### Orders (read)

#### `orders`
Today's orders, all statuses (OPEN, COMPLETE, CANCELLED, REJECTED, etc.).

#### `open-orders`
Only `OPEN` / `TRIGGER PENDING` — filtered subset.

#### `trades`
Today's fills.

#### `order-history --order-id X`
State transitions for one order. Chronologically sorted. Example statuses:

```
PUT ORDER REQ RECEIVED → VALIDATION PENDING → OPEN PENDING → OPEN →
MODIFY VALIDATION PENDING → MODIFIED → OPEN (new price) →
CANCEL PENDING → CANCELLED
```

#### `order-trades --order-id X`
Fills for one order (multiple if partial).

---

### Orders (write)

#### `place` — the main write command

```bash
.venv/bin/python -m kite_algo.kite_tool place \
  --exchange {NSE,BSE,NFO,BFO,MCX,CDS,BCD} \
  --tradingsymbol <SYMBOL> \
  --transaction-type {BUY,SELL} \
  --order-type {MARKET,LIMIT,SL,SL-M} \
  --quantity <INT> \
  --product {CNC,MIS,NRML,MTF} \
  [--price <FLOAT>]          # required for LIMIT/SL
  [--trigger-price <FLOAT>]  # required for SL/SL-M
  [--validity {DAY,IOC,TTL}]
  [--validity-ttl <INT>]     # minutes, required for validity=TTL
  [--variety {regular,amo,co,iceberg,auction}]
  [--disclosed-quantity <INT>]
  [--iceberg-legs <INT>]     # 2..50, required for iceberg
  [--iceberg-quantity <INT>] # legs * qty must equal quantity
  [--tag <STR>]              # auto-generated if omitted; used for idempotency
  [--dry-run]                # preview margin/charges, DO NOT transmit
  [--wait-for-fill <SEC>]    # poll until COMPLETE/REJECTED/CANCELLED
  --yes
```

**Pipeline** on every invocation:
1. Validate 19 rules locally. Reject on failure (no API call).
2. If `--dry-run`: call `order_margins()` with whitelisted fields; print
   preview; return. `price` omitted for MARKET orders.
3. Generate `tag` if omitted (14-char `KA` + random alphanumeric).
4. `IdempotentOrderPlacer.place()`:
   - Rate-limit via `wait_order()`.
   - Submit `place_order`.
   - On transient error: poll orderbook by tag with exponential delays
     (0.5s, 1.0s, 1.5s, 2.0s, 2.5s). If tag found → return its order_id.
     If not found after ~7.5s → retry (max 3 attempts).
   - On hard error (InputException, OrderException, TokenException,
     PermissionException): surface immediately, no retry.
5. If `--wait-for-fill N`: poll `order_history` with 100ms → 1s exponential
   backoff. Return terminal state (COMPLETE / REJECTED / CANCELLED) or
   `TIMEOUT`.

**Dry-run example:**
```bash
.venv/bin/python -m kite_algo.kite_tool place \
  --exchange NSE --tradingsymbol RELIANCE \
  --transaction-type BUY --order-type LIMIT \
  --quantity 1 --product CNC --price 1300 \
  --dry-run --yes --format json
```
Returns margin breakdown: span, exposure, option_premium, var, total, charges
(STT, exchange fees, SEBI, GST, stamp duty, brokerage).

**Live limit example (with auto-tag and fill wait):**
```bash
.venv/bin/python -m kite_algo.kite_tool place \
  --exchange NSE --tradingsymbol RELIANCE \
  --transaction-type BUY --order-type LIMIT \
  --quantity 1 --product CNC --price 1340 \
  --wait-for-fill 30 --yes --format json
```
Returns: `order_id`, `tag`, all placement params, `final_status`,
`filled_quantity`, `average_price`, `status_message`.

**Iceberg example (split 1000 qty across 5 legs of 200):**
```bash
.venv/bin/python -m kite_algo.kite_tool place \
  --exchange NSE --tradingsymbol RELIANCE \
  --transaction-type BUY --order-type LIMIT \
  --quantity 1000 --product CNC --price 1340 \
  --variety iceberg --iceberg-legs 5 --iceberg-quantity 200 \
  --yes
```

#### `cancel --order-id X --variety regular --yes`
Cancel by id.

#### `modify --order-id X --variety regular --yes`
Modify any of `--quantity`, `--price`, `--trigger-price`, `--order-type`, `--validity`.

#### `cancel-all --yes`
Cancel every OPEN / TRIGGER PENDING order. Rate-limited. Returns structured
result:
```json
{
  "cancelled": ["260416190703951", "..."],
  "failed": [{"order_id": "...", "reason": "..."}],
  "total_cancelled": 2,
  "total_failed": 0
}
```
Exit 1 if any cancel failed.

---

### Market data

#### `ltp --symbols NSE:RELIANCE,NSE:INFY`
Fastest. Up to 500 symbols per call. Returns last_price + instrument_token.

#### `ohlc --symbols ...`
LTP + today's OHLC.

#### `quote --symbols ... [--flat]`
Full quote: depth (5-level), OI, volume, average_price, net_change. `--flat`
flattens top-of-book into one row per symbol.

#### `depth --symbols NSE:RELIANCE`
Pretty-printed 5-level order book with bid/ask order counts.

#### `stream`
Live WebSocket tick stream via KiteTicker.

```bash
.venv/bin/python -m kite_algo.kite_tool stream \
  --symbols NSE:RELIANCE,NSE:INFY \
  --mode {ltp,quote,full} \
  [--duration SEC]               # 0 = until Ctrl+C
  [--order-updates]              # emit order_update events too
  [--reconnect-max-tries 50]
  [--reconnect-max-delay 60]
```

Or resolve tokens yourself:
```bash
.venv/bin/python -m kite_algo.kite_tool stream --tokens 738561,408065
```

**Modes:**
- `ltp` (8 bytes/tick) — just the last price. Cheapest.
- `quote` (44 bytes/tick) — no depth.
- `full` (184 bytes/tick) — includes market depth.

**Behaviour:**
- Auto-reconnects via KiteTicker's built-in logic.
- Token-related errors (6am IST rotation, 401/403) → **exit 2** (not silent
  zero-tick streams).
- `BrokenPipeError` in consumer (e.g. `| head`) → clean shutdown.
- `--duration` uses `threading.Timer` (portable, supports floats).

Output: one JSON object per tick on stdout. Log messages on stderr.

---

### Historical & instruments

#### `history`
```bash
.venv/bin/python -m kite_algo.kite_tool history \
  [--symbol <SYM> --exchange NSE | --instrument-token <INT>] \
  --interval {minute,3minute,5minute,10minute,15minute,30minute,60minute,day} \
  [--days 30 | --from YYYY-MM-DDTHH:MM:SS --to ...] \
  [--continuous]  # continuous futures contract
  [--oi]          # include OI for F&O
  [--format json]
```

Rate-limited to 3/s via historical bucket automatically.

#### `instruments --exchange {NSE,BSE,NFO,BFO,MCX,CDS,BCD} [--dump] [--refresh]`
Refreshes the local cache at `data/instruments/<exchange>.json`. Default TTL
86400s (see `KITE_INSTRUMENTS_TTL_SECONDS`). `--dump` emits all rows;
otherwise prints summary counts by segment.

#### `search --query X --exchange NSE --limit 50`
Grep the local instrument cache by tradingsymbol or name.

#### `contract --tradingsymbol X --exchange NSE`
Full instrument row for one symbol: instrument_token, ISIN, tick_size,
lot_size, instrument_type, expiry/strike (if derivative).

---

### Options

Kite does NOT provide option greeks. This CLI computes them locally via
Black-Scholes (Newton-Raphson IV with Brent fallback). Default risk-free rate
is 6.5% (RBI repo); override via `--risk-free-rate` or env
`KITE_RISK_FREE_RATE`.

#### `expiries --symbol NIFTY`
List available F&O expiries for an underlying from the NFO instruments dump.

#### `chain`
```bash
.venv/bin/python -m kite_algo.kite_tool chain \
  --symbol NIFTY --expiry 2026-04-21 \
  [--quote]        # also fetch live quotes per strike (batches of 500)
  [--greeks]       # compute BSM greeks (requires --quote)
  [--risk-free-rate 0.065]
  [--format json]
```

With `--greeks`, the underlying spot is auto-resolved (NSE equity; falls back
to index symbols NIFTY 50 / NIFTY BANK / NIFTY FIN SERVICE).

Output columns per strike: strike, right (CE/PE), symbol, last_price, oi,
volume, avg_price, net_change, lot_size, instrument_token, + when
`--greeks`: iv (%), delta, gamma, theta (per day), vega (per 1% IV).

#### `option-quote`
```bash
.venv/bin/python -m kite_algo.kite_tool option-quote \
  --symbol NIFTY --expiry 2026-04-21 \
  --strike 24400 --right {CE,PE} \
  [--greeks] [--risk-free-rate 0.065]
```

#### `calc-iv`
Solve for IV given observed market price (no API call).
```bash
.venv/bin/python -m kite_algo.kite_tool calc-iv \
  --spot 24356 --strike 24400 --dte 5 \
  --market-price 162.0 --right CE \
  [--risk-free-rate 0.065]
```

#### `calc-price`
Theoretical price + all greeks from a given IV (no API call).
```bash
.venv/bin/python -m kite_algo.kite_tool calc-price \
  --spot 24356 --strike 24400 --dte 5 \
  --iv 15 --right CE \
  [--risk-free-rate 0.065]
```

---

### GTT (good-till-triggered)

GTTs wait server-side for a trigger price, then auto-submit an order. Two types:

- **Single**: one trigger → one order.
- **OCO (two-leg)**: two triggers (stop + target) → one-cancels-other. When
  either fires, the other is cancelled.

#### `gtt-list`
All active GTTs.

#### `gtt-get --trigger-id X`

#### `gtt-create`
```bash
# Single-leg stop-loss:
.venv/bin/python -m kite_algo.kite_tool gtt-create \
  --exchange NSE --tradingsymbol RELIANCE \
  --trigger-values 1280 --last-price 1340 \
  --transaction-type SELL --quantity 10 \
  --order-type LIMIT --product CNC --price 1275 --yes

# OCO stop + target:
.venv/bin/python -m kite_algo.kite_tool gtt-create \
  --exchange NSE --tradingsymbol RELIANCE \
  --trigger-values 1280,1360 --last-price 1340 \
  --transaction-type SELL --quantity 10 \
  --order-type LIMIT --product CNC \
  --price 1275 --price2 1365 --yes
```

For fully custom legs (different transaction types per leg, etc.), use
`--orders-json '[{...}, {...}]'` instead of the simple args.

#### `gtt-modify`
Requires full replacement: `--trigger-values`, `--last-price`, `--orders-json`.

#### `gtt-delete --trigger-id X --yes`

---

### Margin calc

#### `margin-calc`
Pre-trade margin for a hypothetical order. Two forms:

```bash
# Structured (convenient):
.venv/bin/python -m kite_algo.kite_tool margin-calc \
  --exchange NFO --tradingsymbol NIFTY26APR24400CE \
  --transaction-type SELL --quantity 25 \
  --product NRML --price 160

# Full JSON (for multi-field edge cases):
.venv/bin/python -m kite_algo.kite_tool margin-calc \
  --orders-json '[{"exchange":"NFO","tradingsymbol":"...",...}]'
```

#### `basket-margin --orders-json [...]`
Margin benefit when multiple orders are netted (e.g. spread = long + short).

---

### Mutual funds

> ⚠️ MF APIs require a separate Kite Connect MF subscription. Without it,
> `mf-holdings`, `mf-orders`, `mf-sips` will fail with a helpful hint.
> `mf-instruments` works regardless.

#### Read
- `mf-holdings` — MF units held.
- `mf-orders` — today's MF orders.
- `mf-sips` — active systematic investment plans.
- `mf-instruments` — all ~7,400 Zerodha MF schemes.

#### Write (all require `--yes`)

```bash
# Buy by amount:
mf-place --tradingsymbol INF00XX01135 --transaction-type BUY --amount 5000 --yes

# Sell by units:
mf-place --tradingsymbol INF00XX01135 --transaction-type SELL --quantity 10.5 --yes

mf-cancel --order-id X --yes

mf-sip-create \
  --tradingsymbol INF00XX01135 \
  --amount 1000 --frequency {monthly,weekly} \
  --instalments 12 [--initial-amount 500] [--tag MY_SIP] --yes

mf-sip-modify --sip-id X \
  [--amount 1500] [--frequency monthly] [--instalments 24] \
  [--status {active,paused}] --yes

mf-sip-cancel --sip-id X --yes
```

---

## Workflows

### 1. Morning readiness check

```bash
.venv/bin/python -m kite_algo.kite_tool login
.venv/bin/python -m kite_algo.kite_tool health
.venv/bin/python -m kite_algo.kite_tool portfolio --format json > /tmp/start_of_day.json
```

### 2. Get greeks for an option chain

```bash
# 1. Expiries:
.venv/bin/python -m kite_algo.kite_tool expiries --symbol NIFTY

# 2. Chain with live quotes + greeks:
.venv/bin/python -m kite_algo.kite_tool chain \
  --symbol NIFTY --expiry 2026-04-21 \
  --quote --greeks --format json > /tmp/nifty_chain.json
```

### 3. Safe test order (open, modify, cancel — NO fill)

```bash
# Place FAR below market (within circuit):
.venv/bin/python -m kite_algo.kite_tool place \
  --exchange NSE --tradingsymbol RELIANCE \
  --transaction-type BUY --order-type LIMIT \
  --quantity 1 --product CNC --price 1210 \
  --tag TEST01 --yes --format json

# Modify:
.venv/bin/python -m kite_algo.kite_tool modify \
  --order-id <ID> --quantity 1 --price 1215 --yes

# Cancel:
.venv/bin/python -m kite_algo.kite_tool cancel \
  --order-id <ID> --yes

# Verify history + no fills:
.venv/bin/python -m kite_algo.kite_tool order-history --order-id <ID>
.venv/bin/python -m kite_algo.kite_tool trades --format json \
  | jq '[.[] | select(.order_id == "<ID>")] | length'
```

### 4. Place real limit with idempotent retry + fill confirmation

```bash
.venv/bin/python -m kite_algo.kite_tool place \
  --exchange NSE --tradingsymbol RELIANCE \
  --transaction-type BUY --order-type LIMIT \
  --quantity 1 --product CNC --price 1340 \
  --tag STRAT_MOMENTUM01 \
  --wait-for-fill 30 --yes --format json
```

### 5. Multi-leg spread via single orders

Kite has no native spread order type. Place legs as separate orders with
matching tag prefix for reconciliation:

```bash
TAG_BASE="BEAR_PUT_$(date +%s)"
# Short put:
.venv/bin/python -m kite_algo.kite_tool place \
  --exchange NFO --tradingsymbol NIFTY26APR23800PE \
  --transaction-type SELL --order-type LIMIT \
  --quantity 65 --product NRML --price 50 \
  --tag "${TAG_BASE}_S" --yes

# Long put (hedge):
.venv/bin/python -m kite_algo.kite_tool place \
  --exchange NFO --tradingsymbol NIFTY26APR23500PE \
  --transaction-type BUY --order-type LIMIT \
  --quantity 65 --product NRML --price 25 \
  --tag "${TAG_BASE}_L" --yes
```

### 6. Streaming with token-rotation safety

```bash
# Will exit with code 2 if token rotates (past 6am IST) — restart your loop.
.venv/bin/python -m kite_algo.kite_tool stream \
  --symbols NSE:RELIANCE --mode full --order-updates \
  > /var/log/kite_ticks.jsonl 2>/var/log/kite_stream.err &
STREAM_PID=$!

# In a supervisor loop:
wait $STREAM_PID
if [ $? -eq 2 ]; then
  .venv/bin/python -m kite_algo.kite_tool login  # re-auth
  # restart
fi
```

---

## Error handling patterns

### InputException on place
**Cause**: bad params — price outside circuit, qty exceeds freeze limit,
symbol/exchange mismatch.
**Action**: do NOT retry. Fix the params. Look up circuit limits via `quote`.

```bash
# Find circuit bands before placing:
.venv/bin/python -m kite_algo.kite_tool quote --symbols NSE:RELIANCE --format json \
  | jq '.["NSE:RELIANCE"] | {ltp: .last_price, lc: .lower_circuit_limit, uc: .upper_circuit_limit}'
```

### TokenException mid-session
**Cause**: 6am IST rotation, or another device signed in to the same user.
**Action**: re-run `login`. `data/session.json` will be overwritten.

### OrderException: "insufficient margin"
**Cause**: not enough free cash/collateral.
**Action**: check `margins`. Reduce quantity or free up capital. NOT retried.

### NetworkException on place
**Cause**: transient network issue.
**Action**: `IdempotentOrderPlacer` handles this automatically — polls the
orderbook by tag for ~7.5s, retries if not found. You do nothing.

### 429 Too Many Requests
**Cause**: rate limit exceeded.
**Action**: `RateLimitedKiteClient` prevents this proactively. If you see it,
multiple processes are sharing the API key — consolidate into one.

### MF commands fail with "attribute name must be string, not 'NoneType'"
**Cause**: SDK bug when account lacks MF subscription.
**Action**: enable MF at developers.kite.trade. `mf-instruments` works
regardless.

### cancel-all partial failure
**Cause**: some orders filled/cancelled between orderbook fetch and cancel.
**Action**: read `failed` array. Exit code 1. Retry the full command (idempotent).

---

## Appendix: enum values

### Exchanges
| Code | Meaning |
|------|---------|
| NSE | National Stock Exchange (equity) |
| BSE | Bombay Stock Exchange (equity) |
| NFO | NSE F&O (equity derivatives) |
| BFO | BSE F&O |
| MCX | Multi Commodity Exchange |
| CDS | NSE Currency Derivatives |
| BCD | BSE Currency Derivatives |

### Products
| Code | Meaning | Exchanges |
|------|---------|-----------|
| CNC | Cash & Carry (delivery) | NSE, BSE only |
| MIS | Margin Intraday Squareoff (auto-squared at 15:20 IST) | All equity + F&O + commodity |
| NRML | Normal (carry overnight) | F&O, commodity, currency |
| MTF | Margin Trading Facility | NSE, BSE only |

### Order types
| Code | Meaning | Required |
|------|---------|----------|
| MARKET | Fill at best available price | quantity |
| LIMIT | Fill at price or better | `--price` |
| SL | Stop-loss LIMIT | `--price` + `--trigger-price` |
| SL-M | Stop-loss MARKET | `--trigger-price` only |

### Varieties
| Code | Meaning |
|------|---------|
| regular | Normal order during market hours |
| amo | After-market order (queued for next open) |
| co | Cover order (built-in SL) |
| iceberg | Split large order into chunks (2-50 legs) |
| auction | Auction market participation |

### Validities
| Code | Meaning |
|------|---------|
| DAY | Good for the day (default) |
| IOC | Immediate-or-Cancel |
| TTL | Time-to-live in minutes (pair with `--validity-ttl N`) |

### Order statuses (terminal)
| Status | Meaning |
|--------|---------|
| COMPLETE | Fully filled |
| CANCELLED | Cancelled (by user or system) |
| REJECTED | Rejected by OMS (see status_message) |

### Order statuses (active)
`PUT ORDER REQ RECEIVED`, `VALIDATION PENDING`, `OPEN PENDING`, `OPEN`,
`TRIGGER PENDING`, `MODIFY VALIDATION PENDING`, `MODIFY PENDING`, `MODIFIED`,
`CANCEL PENDING`.

### Market hours (IST)
| Segment | Pre-open | Regular | Close |
|---------|----------|---------|-------|
| NSE/BSE equity | 09:00–09:15 | 09:15–15:30 | post-close 15:40–16:00 |
| NFO/BFO | — | 09:15–15:30 | — |
| MCX | — | 09:00–23:30 (or 23:55) | — |
| CDS/BCD | — | 09:00–17:00 | — |

### Timezone
All server timestamps are IST (Asia/Kolkata, UTC+05:30). The CLI preserves
them as returned by the API.
