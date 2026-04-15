# Architecture

## Package layout

```
kite_algo/
├── cli.py           # Main trading-engine CLI (scaffold)
├── kite_tool.py     # Comprehensive Kite data/ops CLI
├── config.py        # KiteConfig, TradingConfig, .env loader, session file I/O
├── instruments.py   # InstrumentSpec for NSE/BSE/NFO/BFO/MCX/CDS
├── orders.py        # TradeIntent + order validation
├── engine.py        # Polling loop (stub)
├── risk.py          # Risk limits (stub)
├── oms.py           # Order manager (stub)
├── persistence.py   # sqlite audit trail (stub)
├── logging_setup.py
└── broker/
    ├── base.py      # Broker protocol + OrderRequest/Position/MarketDataSnapshot/AccountSnapshot
    ├── kite.py      # KiteBroker — live adapter (read path implemented, write path stubbed)
    └── sim.py       # SimBroker — deterministic simulator for tests/paper
```

## Broker protocol parity with `trading-algo`

`kite_algo.broker.base` intentionally mirrors `trading_algo.broker.base` so
any engine/OMS/risk code that targets one can be retargeted to the other
with minimal glue:

- `OrderRequest` — same fields, Kite-specific `product` (CNC/MIS/NRML) and
  `variety` (regular/amo/co/iceberg).
- `Position`, `MarketDataSnapshot`, `AccountSnapshot`, `Bar` — same shapes.
- `Broker` protocol — same methods (`connect`, `get_positions`,
  `place_order`, `get_market_data_snapshot`, `get_historical_bars`, …).

This means strategies can theoretically run against either broker, with
exchange/instrument differences handled by the `InstrumentSpec` adapter.

## Session lifecycle

Kite Connect access tokens **expire at ~6am IST every day**. There is no
refresh token. Our flow:

1. User runs `kite_tool login`
2. CLI opens Kite's login URL and prompts for the `request_token` from the
   redirect
3. Exchange with `generate_session(request_token, api_secret)` → `access_token`
4. Write to `data/session.json` (gitignored)
5. All subsequent commands call `KiteConfig.from_env()` which reads the
   access token from the session file (or `KITE_ACCESS_TOKEN` env, if set)
6. Session-using commands call `cfg.require_session()` which raises with a
   clear "run login" message if the token is missing

## Safety rails

Two layers, mirrored from `trading-algo`:

1. **Env gates**: `TRADING_ALLOW_LIVE`, `TRADING_LIVE_ENABLED`, `TRADING_DRY_RUN`
2. **CLI gates**: every order-placing command requires `--yes`. The optional
   `--confirm-token` must match `TRADING_ORDER_TOKEN` if
   `TRADING_CONFIRM_TOKEN_REQUIRED=true`.

`KiteBroker._require_live(action)` asserts both env gates before any write
call reaches the Kite API.

## Instruments cache

`client.instruments(exchange)` returns a large list (NFO alone is ~100k
rows). We persist it to `data/instruments/<EXCHANGE>.json` with a TTL
(`KITE_INSTRUMENTS_TTL_SECONDS`, default 86400s). All option chain / search /
history-by-symbol commands route through this local cache.

The cache is gitignored.
