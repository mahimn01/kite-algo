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

## Options Position Management — NIFTY/index CE/PE shorts (the 2026-06-15 23900CE lesson)
- **An OI-wall short is valid ONLY while the wall holds.** Re-check the anchoring wall every session (intraday if price nears the strike). OI migrates toward expiry — if the wall has eroded or price has breached the strike, the thesis is INVALIDATED → exit on the defensive trigger. Never hold-to-expiry on hope.
- **Set the defensive stop AT ENTRY** (price or NIFTY level). Exit on the trigger, not by chasing the intraday spike top — closing 23900CE at the 211.90 high cost ~₹1,100 vs a defined exit.
- **NEVER remove a defensive stop to "hold for a dip,"** especially against your own directional view. Short call + bullish = incoherent; close it. (Cancelling the 23900CE stop on 2026-06-15 to wait for a pullback turned a ~₹5k exit into ₹9.7k.)
- **A breached (ITM) short is NOT a "hold to expiry" candidate.** "Hold working shorts for theta" applies ONLY to OTM, profitable shorts. Remaining DTE is never a reason to sit in a losing breached short.
- **GIFT Nifty / international cues are an exit-TIMING overlay, never an exit-WAIVER.** GIFT Nifty (GIFT City / NSE IX — formerly SGX Nifty, Singapore; migrated 2023) trades ~21h and by ~8–9am IST predicts the NIFTY opening gap from the US close, crude, and DXY. Available via the Kite Connect API on the **NSEIX** exchange (NSE International Exchange; segment INDICES, symbol "GIFT NIFTY") — fetch with `quote(["NSEIX:GIFT NIFTY"])`. Also visible in the app via Streak. (Earlier note that it was API-absent was wrong — it lives under NSEIX, not NSE/NFO/BSE/BFO.) Use it to time the exit and gauge gap risk:
  - *Not-yet-breached* short: cues against you → reduce/hedge before the open; cues with you → hold to the pre-set stop.
  - *Already-breached (ITM)* short: the defensive trigger still mandates exit. Only latitude — if GIFT Nifty signals a *favorable* gap (e.g. gap-down), wait for that open to exit at a better price, but ACT at that open; a hard intraday stop still binds. No open-ended waiting — GIFT Nifty predicts the next OPEN, not a multi-day path.
  - "Patience affordable" ≈ f(DTE × distance-OTM × delta), and ≈ 0 once ITM regardless of DTE.
- **These rules are DECISION-SUPPORT, never auto-execution.** No rule, signal, or GIFT Nifty read ever fires an order automatically. Claude stages the exact order and the user transmits with an explicit "send it." Rules sharpen the recommendation; the human pulls the trigger.

## Self-Improvement
After every bug fix or correction, add a rule here to prevent repeating it.
- **Every live-write surface must call the same centralized authorization gate.** Tests must prove `TRADING_ALLOW_LIVE=false`, `TRADING_LIVE_ENABLED=false`, dry-run, and confirmation-token mismatch all stop before client construction; never rely on documentation or a parallel broker adapter to protect a direct SDK path.
- **Claims about compliance must name their boundary.** Local audit files are reconciliation evidence, not immutable regulatory storage; broker/exchange records and external static-IP configuration remain separate dependencies.
- **Documented path overrides must have one resolver.** Auth, config, redaction, status, and logout must all use `session_path()` so multi-account sessions cannot silently diverge.
