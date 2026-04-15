# Changelog

## 2026-04

### Unreleased — initial scaffold

- **2026-04-14** Initial scaffold. Package skeleton (`kite_algo/`) mirroring `trading-algo`: `config.py` with `KiteConfig` + `TradingConfig` safety rails, `broker/` with base interface and `KiteBroker` stub, `cli.py` trading-engine skeleton, `kite_tool.py` comprehensive Kite CLI with argparse-wired commands for auth (login / profile / session), account (margins / holdings / positions / orders / trades), quotes (ltp / quote / ohlc), historical bars, instruments dump + local search, options (chain / option-quote / expiries), order ops (place / modify / cancel / cancel-all), GTT, margin calc, and mutual funds. Most command bodies are stubbed pending Kite integration.
- **2026-04-14** Scaffolded docs (`docs/ARCHITECTURE.md`, `SAFETY.md`, `WORKFLOWS.md`), `.github/` PR + issue templates, `.gitignore` excluding session files and data caches, `.env.example` with Kite credentials + safety rails + optional LLM section, `requirements.txt` (kiteconnect, pandas, numpy, scipy, pytest), and `CLAUDE.md` documenting project rules (daily token rotation, rate limits, product types, market hours).
- **2026-04-14** Created public GitHub repo [`mahimn01/kite-algo`](https://github.com/mahimn01/kite-algo).
