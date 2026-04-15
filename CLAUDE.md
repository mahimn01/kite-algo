# Kite Algo — India Market Trading System

## Stack
Python 3.11+, Kite Connect API (kiteconnect), asyncio, argparse CLI

## Commands
```bash
python -m kite_algo.kite_tool login           # Interactive daily auth
python -m kite_algo.kite_tool profile         # Verify session
python -m kite_algo.kite_tool margins         # Account margins
python -m kite_algo.kite_tool ltp --symbols NSE:RELIANCE,NSE:INFY
python -m kite_algo.kite_tool holdings
python -m kite_algo.cli --help                # Main trading engine CLI
pytest                                        # Run tests
```

## Architecture
- Entry: `kite_algo/cli.py` (trading engine), `kite_algo/kite_tool.py` (data/ops CLI)
- Broker: `kite_algo/broker/kite.py` (KiteConnect adapter)
- Config: `kite_algo/config.py` (KiteConfig + TradingConfig with safety rails)
- Session: `data/session.json` (Kite access token, rotates daily ~6am IST)

## Rules
- **Access token expires daily.** Every morning ~6am IST, all existing tokens are invalidated by Kite. First command each day will fail until you run `kite-algo login` again. Do NOT try to auto-refresh via Selenium/TOTP — it's brittle and against Kite's ToS.
- **Never commit `.env` or `data/session.json`** — both are gitignored. Both contain live credentials.
- **Kite rate limits are strict**: 3 req/s most endpoints, 10 req/s for /quote. Always throttle batch operations.
- **Greeks are not provided by Kite.** Options Greeks must be computed locally via Black-Scholes. Use the shared quant code from `trading-algo` or reimplement minimal BSM here.
- **Market hours are IST**: 9:15-15:30 equity, 9:00-23:30 MCX, 9:00-17:00 CDS. All times in `Asia/Kolkata`.
- **Product types matter**: `CNC` (delivery), `MIS` (intraday), `NRML` (normal F&O / carry overnight). Choose deliberately — `MIS` positions are auto-squared-off at 3:20pm.
- **Order variety**: `regular`, `amo` (after-market), `co` (cover), `iceberg`. Default is `regular`.
- **Always use Kite API first for live data** before falling back to WorldMonitor or web scraping.
- **Type hints on ALL function signatures.**
- **`--paper` and `--live` are mutually exclusive CLI flags** (though Kite has no paper mode — `--paper` routes to `SimBroker`).
- **All risk limits enforced at config level, not strategy level.**

## Self-Improvement
After every bug fix or correction, add a rule here to prevent repeating it.
