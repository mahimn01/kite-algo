# kite-algo

Indian-market trading system built on Kite Connect (Zerodha). It's the sibling to [trading-algo](https://github.com/mahimn01/trading-algo), which covers US equities and crypto through Interactive Brokers. Same overall design, wired up for NSE, BSE, NFO, MCX, and CDS.

Right now it's mostly scaffolding and the read-only side of things. The package structure, safety rails, and data/ops CLI are in place. Order routing, strategies, and the engine loop are stubbed and being filled in.

## Getting it running

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# fill in KITE_API_KEY and KITE_API_SECRET from https://developers.kite.trade/apps

python -m kite_algo.kite_tool login
python -m kite_algo.kite_tool profile
python -m kite_algo.kite_tool margins
python -m kite_algo.kite_tool ltp --symbols NSE:RELIANCE,NSE:INFY
```

Every command supports `--format json|csv|table`.

## How Kite is different from IBKR

Coming from the IBKR side, this was the part that surprised me the most. Kite is a different animal.

Sessions don't persist. The access token rotates at roughly 6 AM IST every single morning, and there's no refresh token flow. You have to re-login each day through Kite's OAuth (browser sign-in, 2FA, paste the `request_token` back into the CLI). I looked into automating it with Selenium and TOTP and decided it wasn't worth it. Storing TOTP secrets defeats the point of 2FA and Zerodha actively discourages browser automation.

There's no Flex equivalent. IBKR's Flex Web Service gives you 365 days of XML statements on demand. Kite doesn't have anything like that. You pull today's orders, trades, holdings, and positions from the API, and anything historical has to come from the Console web UI as CSV exports.

Live data is split. You use the REST `/quote` endpoint for snapshots and the KiteTicker WebSocket for streaming. IBKR bundles both into one call.

No greeks from the API. If you want them you compute them locally with Black-Scholes.

Rate limits are tight. Around 3 requests per second on most endpoints, 10 per second on `/quote`. IBKR will let you hammer it at roughly 50 per second.

Instruments come as a 70 MB CSV dump refreshed daily around 8:30 AM IST. You grep it locally instead of using a live lookup. Clunky but it works.

Product types matter in a way they don't on IBKR. `CNC` is delivery (longer-term). `MIS` is intraday and auto-squares-off at 15:20. `NRML` is for F&O carry. Mixing these up will cost you real money.

Market hours are different too. NSE is 09:15 to 15:30 IST. MCX runs 09:00 to 23:30 IST. CDS is 09:00 to 17:00 IST.

## The kite_tool CLI

`kite_tool` handles the data and operations side of things.

### Auth and session

`login`, `profile`, `session`, `logout`.

### Account

`margins`, `holdings`, `positions`, `orders`, `trades`, `order-history`, `order-trades`.

### Quotes and live data

`ltp` (fastest, up to 500 symbols at once), `ohlc`, `quote` (full depth plus open interest and volume), `stream` (KiteTicker WebSocket tick feed).

### Historical

`history` (OHLC bars at minute through day intervals), `instruments` (cached CSV dump), `search` (local grep against the cached instruments file).

### Options

`chain` (full option chain with locally computed Greeks), `option-quote`, `expiries`.

### Orders (safety-gated)

`place`, `cancel`, `modify`, `cancel-all`. All order-placing commands need `--yes` and a confirmation token.

### GTT (Good Till Triggered)

`gtt-list`, `gtt-get`, `gtt-create`, `gtt-modify`, `gtt-delete`. Supports single and OCO orders.

### Margin calculator

`margin-calc` for a list of legs, `basket-margin` for multi-leg benefit calculations.

### Mutual funds

`mf-holdings`, `mf-orders`, `mf-sips`.

## Safety rails

Same layered pattern as trading-algo. `TRADING_ALLOW_LIVE` has to be true for any live command to run. `TRADING_LIVE_ENABLED` is a second gate. `TRADING_DRY_RUN` defaults to true and stages orders without transmitting. Every order-placing CLI command needs `--yes`. The broker calls (`place_order`, `modify_order`, `cancel_order`) prompt for `YES` at the terminal. `TRADING_ORDER_TOKEN` is an extra confirmation that has to match a `--confirm-token` passed in. There's an optional sqlite audit trail at `TRADING_DB_PATH`.

The full safety model lives in `docs/SAFETY.md`.

## The daily auth flow, in detail

Kite's access token expires around 6 AM IST every morning. No exceptions. No refresh tokens. Here's what actually happens.

You run `python -m kite_algo.kite_tool login`. The CLI opens the Kite login URL (`https://kite.zerodha.com/connect/login?v=3&api_key=...`) in your browser. You sign in with your Zerodha credentials and 2FA. Kite redirects to your configured `redirect_uri` with a `request_token` in the query string. You paste the `request_token` back into the CLI (or the CLI auto-catches it if you've got a local HTTP listener wired in). The CLI exchanges `request_token` plus `api_secret` for an `access_token`. That `access_token` gets written to `data/session.json` (gitignored). Every command after that reads the token from there.

Don't try to automate this with Selenium and TOTP. It's fragile, it defeats 2FA, and Zerodha really doesn't like it.

## Env vars

See `.env.example` for the full list. Minimum to get going.

```bash
KITE_API_KEY=your_api_key
KITE_API_SECRET=your_api_secret

TRADING_BROKER=kite
TRADING_ALLOW_LIVE=false
TRADING_DRY_RUN=true
```

Start with `ALLOW_LIVE=false` and `DRY_RUN=true` until you're sure everything behaves.

## Where things stand

Package skeleton and config are scaffolded. `kite_tool` CLI parser is up. Auth and login, read-only commands (profile, margins, holdings, quotes, history), the KiteBroker adapter, order placement, GTT, WebSocket streaming, options chain with local Greeks, and the engine/OMS/risk loop are all in various states of stub or implementation-in-progress. Strategies, instruments caching, backtesting, and tests are planned but not started.

Commit history is in `CHANGELOG.md`.

## Related

- [trading-algo](https://github.com/mahimn01/trading-algo), the IBKR sibling (US equities, crypto, options)
- [Kite Connect API docs](https://kite.trade/docs/connect/v3/)
- [pykiteconnect](https://github.com/zerodha/pykiteconnect), the official Python SDK
