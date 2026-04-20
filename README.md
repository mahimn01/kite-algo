# kite-algo

Indian-market trading system on Kite Connect (Zerodha). Sibling to [trading-algo](https://github.com/mahimn01/trading-algo), which covers US equities and crypto through Interactive Brokers. Wired for NSE, BSE, NFO, MCX, and CDS.

Right now it's mostly scaffolding and the read-only side. Order routing, strategies, and the engine loop are stubbed and being filled in.

## Quickstart

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# fill in KITE_API_KEY and KITE_API_SECRET

python -m kite_algo.kite_tool login
python -m kite_algo.kite_tool margins
python -m kite_algo.kite_tool ltp --symbols NSE:RELIANCE,NSE:INFY
```

Every command supports `--format json|csv|table`.

## Kite vs IBKR (the gotchas)

| Topic | IBKR | Kite |
|---|---|---|
| Session | Persistent while TWS/Gateway runs | Token rotates ~6am IST daily, OAuth re-login each morning |
| Historical statements | Flex Web Service, 365 days | None, pull from Console web UI CSV |
| Live data | Single call for snap + stream | REST `/quote` for snapshots, KiteTicker WebSocket for streaming |
| Greeks | From API | Compute locally with Black-Scholes |
| Rate limits | ~50 req/s | 3 req/s most endpoints, 10 req/s on `/quote` |
| Instruments | Live lookup | 70MB CSV dump refreshed daily ~8:30am IST |
| Product types | Margin vs cash | `CNC` (delivery), `MIS` (intraday, auto-square 15:20), `NRML` (F&O carry) |
| Market hours | US 09:30–16:00 ET | NSE 09:15–15:30 IST, MCX 09:00–23:30 IST, CDS 09:00–17:00 IST |

## kite_tool commands

| Group | Commands |
|---|---|
| Auth | `login`, `profile`, `session`, `logout` |
| Account | `margins`, `holdings`, `positions`, `orders`, `trades`, `order-history`, `order-trades` |
| Quotes | `ltp` (fastest, up to 500 symbols), `ohlc`, `quote` (full depth + OI), `stream` (WebSocket ticks) |
| Historical | `history`, `instruments`, `search` |
| Options | `chain` (with locally computed Greeks), `option-quote`, `expiries` |
| Orders (gated) | `place`, `cancel`, `modify`, `cancel-all` |
| GTT | `gtt-list`, `gtt-get`, `gtt-create`, `gtt-modify`, `gtt-delete` |
| Margin calc | `margin-calc`, `basket-margin` |
| Mutual funds | `mf-holdings`, `mf-orders`, `mf-sips` |

## Safety rails

Same layered pattern as trading-algo. `TRADING_ALLOW_LIVE=true` is required. `TRADING_DRY_RUN=true` stages orders without transmitting. Every order-placing CLI command needs `--yes` plus a matching `TRADING_ORDER_TOKEN` / `--confirm-token`. The broker calls prompt for `YES` at the terminal.

Full safety model in `docs/SAFETY.md`.

## Daily auth flow

Kite's access token expires daily between **06:45–07:30 IST**. No refresh tokens.

`python -m kite_algo.kite_tool login` supports three modes — local listener (default), remote SSH-tunneled listener, and paste fallback. Full recipes (including how to log in without being at your trading machine) in [`docs/LOGIN.md`](docs/LOGIN.md).

Quick version:

```bash
# Default — binds 127.0.0.1:5000, catches Kite's 302 automatically.
# Your app profile at developers.kite.trade must register http://127.0.0.1:5000/.
python -m kite_algo.kite_tool login

# Logging in from your laptop while the trading box is remote:
ssh -L 5000:127.0.0.1:5000 user@trading-box
# then on the trading box:
python -m kite_algo.kite_tool login
# open the printed URL in your laptop's browser → the SSH tunnel delivers
# the callback to the trading box's listener.

# Paste fallback when the listener can't be reached:
python -m kite_algo.kite_tool login --paste
```

Don't try to automate the sign-in itself with Selenium + TOTP. It's fragile, defeats 2FA, is against Zerodha's ToS, and is the thing Zerodha has banned API keys for. The listener above is NOT credential automation — you still sign in manually; we just catch the OAuth callback, the same way `gh auth login` does.

## Env

Minimum needed, full list in `.env.example`.

```bash
KITE_API_KEY=...
KITE_API_SECRET=...
TRADING_BROKER=kite
TRADING_ALLOW_LIVE=false
TRADING_DRY_RUN=true
```

## Status

| Component | Status |
|---|---|
| Package skeleton + config | done |
| `kite_tool` CLI parser (68 subcommands) | done |
| Auth + daily-rotation login flow | done |
| Read-only commands (profile, margins, holdings, quotes, history, chain, ...) | done |
| `KiteBroker` adapter (read + write) | done |
| Order placement (validated, idempotent, rate-limited) | done |
| GTT commands (list/get/create/modify/delete, single + OCO) | done |
| WebSocket streaming (with `--buffer-to` + `tail-ticks`) | done |
| Options chain with local BSM Greeks | done |
| Engine / OMS / Risk / Persistence | done |
| Kill switch (`halt` / `resume`) | done |
| Structured envelope + exit-code taxonomy | done |
| SEBI-compliant audit log (`data/audit/*.jsonl`) + `events` | done |
| Multi-leg transaction groups + `reconcile` | done |
| Alerts API (raw HTTP) | done |
| Instruments cache (daily dump, atomic writes) | done |
| Market-hours + freeze-qty + lot-size + MIS-cutoff guards | done |
| `market_protection` plumbed (SEBI April 2026) | done |
| `status`, `watch`, `time`, `tools-describe` agent commands | done |
| `--idempotency-key` crash-safe replay cache | done |
| Strategies (agent-driven; no in-repo strategies) | out of scope |
| Backtesting | out of scope |
| Tests (731 passing) | done |

## Related

- [trading-algo](https://github.com/mahimn01/trading-algo), the IBKR sibling
- [Kite Connect API docs](https://kite.trade/docs/connect/v3/)
- [pykiteconnect](https://github.com/zerodha/pykiteconnect), official Python SDK
