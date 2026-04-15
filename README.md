# Kite Algo

India-market trading system built on [Kite Connect](https://kite.trade/) (Zerodha). Parallel to [`trading-algo`](https://github.com/mahimn01/trading-algo) (IBKR / US + crypto) but structured for NSE / BSE / NFO / MCX / CDS.

Comprehensive Kite data + operations CLI (`kite_tool`), a broker adapter that matches the `trading-algo` interface, and room to iterate on Indian-market strategies (equity / F&O / commodities).

## Status

**Scaffold — iterating on integration.** The package skeleton, safety rails, and read-only CLI commands are in place. Order routing, strategies, and the engine loop are stubbed and being built out incrementally.

## Quickstart

```bash
# 1. Install
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Fill in KITE_API_KEY and KITE_API_SECRET from https://developers.kite.trade/apps

# 3. Daily login (Kite access tokens rotate at ~6am IST)
python -m kite_algo.kite_tool login

# 4. Verify
python -m kite_algo.kite_tool profile
python -m kite_algo.kite_tool margins
python -m kite_algo.kite_tool ltp --symbols NSE:RELIANCE,NSE:INFY
```

## Architecture

```
kite-algo/
├── kite_algo/                       # Core package
│   ├── cli.py                       # Main trading engine CLI (orders, brackets, safety rails)
│   ├── kite_tool.py                 # Comprehensive Kite data/ops CLI (auth, quotes, history, orders, GTT, margins)
│   ├── config.py                    # KiteConfig + TradingConfig with daily-token session management
│   ├── instruments.py               # InstrumentSpec for NSE / BSE / NFO / MCX / CDS
│   ├── orders.py                    # Order validation + TradeIntent dataclass
│   ├── engine.py                    # Polling engine + risk manager
│   ├── risk.py                      # Position limits + margin checks
│   ├── oms.py                       # Order manager with state machine
│   ├── persistence.py               # sqlite audit trail
│   ├── logging_setup.py
│   └── broker/
│       ├── base.py                  # Broker interface (matches trading-algo contract)
│       ├── kite.py                  # KiteConnect adapter
│       └── sim.py                   # Deterministic simulation broker for tests/paper
├── scripts/
│   ├── login.py                     # Standalone auth helper
│   └── smoke_test.py                # Connection + read-only sanity checks
├── tests/
├── docs/
│   ├── ARCHITECTURE.md
│   ├── SAFETY.md
│   └── WORKFLOWS.md
├── data/                            # (gitignored) session.json, instruments cache, historical bars
├── .github/
│   ├── PULL_REQUEST_TEMPLATE.md
│   └── ISSUE_TEMPLATE/
├── .env.example
├── CLAUDE.md                        # Project rules (daily token, rate limits, product types)
├── CHANGELOG.md
├── requirements.txt
└── README.md
```

## Kite ↔ IBKR — structural differences

| Topic | IBKR / `trading-algo` | Kite / `kite-algo` |
|---|---|---|
| **Session** | Persistent while TWS / Gateway runs | **Access token rotates at ~6am IST daily.** Manual re-login via OAuth each morning. |
| **Reporting API** | Flex Web Service → 365-day XML statements | **No Flex equivalent.** Today's `/orders`, `/trades`, `/holdings`, `/positions` from API; historical P&L from Console web UI CSV. |
| **Live data** | `reqMktData` streams snap + live on one call | **Split**: REST `/quote` for snapshots, **KiteTicker WebSocket** for live stream. |
| **Greeks** | Delivered by TWS | **Not provided.** Computed locally via Black-Scholes. |
| **Rate limits** | ~50 req/s | **3 req/s** most endpoints, **10 req/s** `/quote`. |
| **Instruments** | `reqMatchingSymbols` live | **~70MB CSV** dump refreshed daily ~8:30am IST; grepped locally. |
| **Market hours** | US 09:30–16:00 ET, crypto 24/7 | NSE 09:15–15:30 IST, MCX 09:00–23:30 IST, CDS 09:00–17:00 IST |
| **Product types** | Margin vs cash accounts | `CNC` (delivery), `MIS` (intraday, auto-square-off 15:20), `NRML` (F&O carry) |
| **Order variety** | MKT / LMT / STP / STPLMT / bracket | `regular`, `amo`, `co`, `iceberg`, GTT |

## `kite_tool` — CLI command catalog

All commands support `--format json|csv|table`. Examples assume the `.env` has a valid session (run `login` first each morning).

### Auth / session

| Command | Description |
|---|---|
| `login` | Interactive OAuth: opens login URL → paste `request_token` → exchanges for `access_token` → writes `data/session.json` |
| `profile` | User profile (name, email, user id, broker) |
| `session` | Current session validity + expiry countdown |
| `logout` | Invalidates access token on the server |

### Account

| Command | Description |
|---|---|
| `margins [--segment equity\|commodity]` | Cash / margin breakdown |
| `holdings` | Demat holdings (long-term) |
| `positions` | Day + net intraday positions |
| `orders` | Today's orders |
| `trades` | Today's executed trades |
| `order-history --order-id N` | Full state history for one order |
| `order-trades --order-id N` | Fills for one order |

### Quotes / live data

| Command | Description |
|---|---|
| `ltp --symbols NSE:RELIANCE,NSE:INFY` | Last traded price (fastest, up to 500 symbols) |
| `ohlc --symbols ...` | OHLC + LTP |
| `quote --symbols ...` | Full quote: OHLC, depth (5 levels), OI, LTQ, volume, avg price |
| `stream --symbols ...` | Live WebSocket tick stream (KiteTicker) |

### Historical

| Command | Description |
|---|---|
| `history --symbol NSE:RELIANCE --interval day --days 30` | OHLC bars (minute / 3minute / 5minute / 10minute / 15minute / 30minute / 60minute / day) |
| `instruments --exchange NSE [--dump] [--refresh]` | Instruments CSV dump (cached locally with TTL) |
| `search --query RELIANCE` | Local grep against cached instruments dump |

### Options

| Command | Description |
|---|---|
| `chain --symbol NIFTY --expiry 2026-05-29` | Full option chain with LTP / OI / volume + locally computed Greeks |
| `option-quote --symbol NIFTY --expiry 2026-05-29 --strike 24000 --right CE` | Single option quote with Greeks |
| `expiries --symbol NIFTY` | All listed expiries for an underlying |

### Orders (gated by safety rails)

| Command | Description |
|---|---|
| `place` | Place a single order (requires `--yes` + token) |
| `cancel --order-id N` | Cancel one order |
| `modify --order-id N ...` | Modify quantity / price |
| `cancel-all` | Cancel all open orders |

### GTT (Good Till Triggered)

| Command | Description |
|---|---|
| `gtt-list` | All active GTTs |
| `gtt-get --trigger-id N` | Single GTT details |
| `gtt-create --symbol ... --trigger-price ... --last-price ... --orders ...` | Create single or OCO GTT |
| `gtt-modify --trigger-id N ...` | Modify a GTT |
| `gtt-delete --trigger-id N` | Delete |

### F&O margin calculator

| Command | Description |
|---|---|
| `margin-calc --orders ...` | Pre-trade margin for a list of legs |
| `basket-margin --orders ...` | Margin benefit for a multi-leg basket |

### Mutual funds

| Command | Description |
|---|---|
| `mf-holdings` | Current MF holdings |
| `mf-orders` | MF orders |
| `mf-sips` | Active SIPs |

## Safety rails

The same multi-layer guard pattern from `trading-algo`:

| Guard | Description |
|---|---|
| `TRADING_ALLOW_LIVE` | Must be `true` to allow any live-trading commands |
| `TRADING_LIVE_ENABLED` | Second gate, must be explicit |
| `TRADING_DRY_RUN` | Defaults to `true`; stage orders only, never transmit |
| `--yes` flag | Every order-placing CLI command requires it |
| Interactive confirmation | `place_order`, `modify_order`, `cancel_order` prompt for `YES` at the terminal |
| `TRADING_ORDER_TOKEN` | Second confirmation token required alongside `--confirm-token` |
| Sqlite audit trail | Optional, configured via `TRADING_DB_PATH` |

See `docs/SAFETY.md` for the full safety model.

## Daily authentication flow

Kite's access token expires at ~6am IST every morning — no exceptions, no refresh tokens. Auth flow:

1. You run `python -m kite_algo.kite_tool login`
2. CLI opens the Kite login URL (`https://kite.zerodha.com/connect/login?v=3&api_key=...`) in your browser
3. You sign in with your Zerodha credentials + 2FA
4. Kite redirects to your configured `redirect_uri` with a `request_token` in the query string
5. You paste the `request_token` back into the CLI (or the CLI auto-catches it if running a local HTTP listener — optional)
6. CLI exchanges `request_token` + `api_secret` → `access_token`
7. `access_token` is written to `data/session.json` (gitignored)
8. All subsequent commands read the token from the session file

**Do not** try to automate this with Selenium/TOTP. It's fragile, storing TOTP secrets defeats the point of 2FA, and Zerodha actively discourages it.

## Environment variables

See `.env.example` for the full list. Minimum required:

```bash
KITE_API_KEY=your_api_key
KITE_API_SECRET=your_api_secret

TRADING_BROKER=kite
TRADING_ALLOW_LIVE=false  # start here
TRADING_DRY_RUN=true      # start here
```

## Development status

| Component | Status |
|---|---|
| Package skeleton + config | ✅ scaffolded |
| `kite_tool` CLI parser | ✅ scaffolded |
| Auth / login flow | 🚧 implementation pending |
| Read-only commands (profile, margins, holdings, quotes, history) | 🚧 implementation pending |
| `KiteBroker` adapter | 🚧 stub |
| Order placement commands | 🚧 stub (safety-gated) |
| GTT commands | 🚧 stub |
| WebSocket streaming (`stream`) | 🚧 stub |
| Options chain + local Greeks | 🚧 stub |
| Engine / OMS / risk loop | 🚧 stub |
| Strategies | 🔲 planned |
| Instruments cache | 🔲 planned |
| Backtesting | 🔲 planned |
| Tests | 🔲 planned |

See `CHANGELOG.md` for commit history.

## Related

- [`trading-algo`](https://github.com/mahimn01/trading-algo) — parallel repo for IBKR (US equities, crypto, options)
- [Kite Connect API docs](https://kite.trade/docs/connect/v3/)
- [pykiteconnect](https://github.com/zerodha/pykiteconnect) — official Python SDK
