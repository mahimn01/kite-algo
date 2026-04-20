# Changelog

## 2026-04

### Unreleased — OAuth callback listener (remote login)

- **New `kite_algo/oauth_callback.py`**: one-shot HTTP listener on `http://127.0.0.1:<port>/` that captures Kite's `?action=login&status=success&request_token=...&state=...` 302 redirect automatically. Matches the pattern `gh auth login`, `stripe login`, `gcloud auth login` use. Zerodha explicitly permits `http://127.0.0.1` as a registered redirect URI — HTTPS is waived for loopback (forum 6583, 10096).
- **CSRF defense**: every `login` invocation mints a 256-bit random nonce, passes it through Kite via `redirect_params=state=<nonce>`, and rejects any callback whose `state` doesn't match (`secrets.compare_digest`). A stale/stranded redirect from a prior attempt can't complete someone else's flow.
- **Security-first defaults**: `CallbackServer` refuses non-loopback binds at the API layer (`LocalBindOnlyError`) — not just a flag — so a bug one level up can't accidentally expose the listener to the LAN. Handler returns 200 **before** parsing (Kite never retries 302s; a 500 would strand the request_token in the user's address bar). Session file refreshes the redaction filter after save so the new `access_token` can't leak into subsequent log lines.
- **`cmd_login` now has three modes**:
  - **default**: listener on 127.0.0.1:5000 + auto-capture.
  - **`--paste`**: original copy/paste flow, for sandboxes / exotic setups.
  - **SSH-tunneled** (documented, no new flag): `ssh -L 5000:127.0.0.1:5000 user@box` from a laptop or phone; sign in on the laptop's browser; the tunnel carries the callback back to the remote listener. Solves the "log in to a trading box without being at it" use case without any Selenium / credential automation.
- **New flags on `login`**: `--listen-port PORT` (default 5000), `--timeout SECS` (default 300), `--paste`. `--no-browser` retained.
- **New doc `docs/LOGIN.md`**: three-mode workflow, SSH tunnel recipe, security properties, troubleshooting.
- **Test suite**: 731 → 759 tests (+28). New files: `test_oauth_callback.py` (21 tests — happy path, CSRF, timeout, port-in-use, lifecycle, 0.0.0.0 rejection), `test_cmd_login_listener.py` (7 end-to-end tests — real listener + simulated browser + mocked `generate_session`).

### Unreleased — Wave 4: engine / OMS / risk / persistence

- **MarketDataClient** (`kite_algo/market_data.py`): TTL-cached snapshot wrapper with optional global min-interval throttle. Keyed by `(exchange, symbol)` so two `InstrumentSpec`s with different tokens but the same key share a cache entry. Validates each snapshot (rejects crossed books; allows `bid=None/ask=None` for closed markets).
- **SqliteStore** (`kite_algo/persistence.py`) — complete rewrite from stub. Tables: `runs, decisions, orders, order_status_events, errors, actions`. Schema extensions beyond trading-algo: `tag`, `product`, `variety`, `trigger_price`, `validity_ttl`, `disclosed_quantity`, `iceberg_legs`, `market_protection`, `group_id`, `leg_name`, `idempotency_key`, `kite_request_id`, `request_id`, `strategy_id`, `agent_id`, `parent_request_id`. WAL + synchronous=NORMAL + indexed on order_id/symbol/run/tag/group/idempotency_key. Path defaults to `data/trading.sqlite`; override via `TRADING_DB_PATH`.
- **RiskManager + RiskLimits** (`kite_algo/risk.py`) — expanded from the single-rule stub. Checks: order quantity, order notional INR, position ceiling, short-allowed toggle, account notional exposure, margin utilisation, daily-loss circuit breaker, MIS cutoff, market-hours, freeze quantity × 10 (autoslice ceiling), lot-size multiples, per-strategy notional cap. Tracks session-start NetLiquidation for daily-loss. `risk_limits_from_env()` reads `KITE_RISK_*` env vars. Raises typed `RiskViolation(code, message)`; engine records as rejected decision.
- **TradeIntent** (`kite_algo/orders.py`): enriched from stub — carries `product`, `variety`, `validity`, `trigger_price`, `disclosed_quantity`, `iceberg_*`, `market_protection`, `tag`, `strategy`, `group_id`, `leg_name`. `to_order_request()` lowers to the broker's `OrderRequest`.
- **KiteBroker write path** (`kite_algo/broker/kite.py`) — stubs replaced. `place_order` routes through `IdempotentOrderPlacer` with auto-generated tag + auto `market_protection=-1` for MARKET/SL-M (SEBI April 2026). `modify_order` tracks per-order modification count via `record_modification`. `get_order_status` sorts history by parsed timestamp. All write methods assert the HALTED sentinel + dry-run + live-enabled gates before any network call.
- **OrderManager (OMS)** (`kite_algo/oms.py`) — rewrite from 18-LOC stub. Tracks in-memory state, persists decisions / orders / status events to SqliteStore, implements `submit / modify / cancel / status / reconcile / track_open_orders`. Dry-run returns noop. TokenException propagates (engine catches and halts).
- **Engine** (`kite_algo/engine.py`) — rewrite from NotImplementedError. `run_once()` and `run_forever()` with graceful shutdown. Per-tick flow: `strategy.on_tick(ctx)` → `risk.validate` → `oms.submit` → persist. TokenException during submit triggers `write_halt` + raises (caller exits, re-login needed). Strategy exceptions logged, don't crash the loop. Uses `MarketDataClient` for snapshot access.
- **Engine CLI** (`kite_algo/cli.py`) — three subcommands: `status`, `run-once`, `run`. `--strategy module:Class` dynamic import, `--dry-run` / `--db-path` / `--confirm-token` / `--market-data-ttl` flags.
- **Test suite**: 642 → 731 tests (+89). New files: `test_market_data.py`, `test_persistence.py`, `test_risk.py`, `test_broker_kite_write.py`, `test_oms.py`, `test_engine.py`, `test_cli_engine.py`.
- **Parity with trading-algo**: kite-algo now covers every infrastructure layer the sibling has — broker protocol, broker adapter, market data, persistence, risk, OMS, engine, CLI — in addition to the Indian-market-specific layers that trading-algo doesn't need.

### Unreleased — Wave 3: missing commands + state ops

- **halt / resume kill-switch** (`kite_algo/halt.py`): `kite-algo halt --reason "..." [--expires-in 1h]` writes a sentinel at `data/HALTED`. Every write command checks at entry and refuses with exit 11 (HALTED). `resume --confirm-resume` clears (distinct from `--yes` so retries can't accidentally lift it). Malformed sentinels fail closed. 13 write commands now halt-gated.
- **cancel-all --confirm-panic** + **convert-position --confirm-convert**: two destructive commands now require a distinct confirmation token on top of `--yes`, so a stray `--yes` retry from an agent loop cannot wipe the book or strand positions.
- **NDJSON audit log** (`kite_algo/audit.py`): every CLI invocation appends one line to `data/audit/YYYY-MM-DD.jsonl`. Fields: ts, ts_epoch_ms, request_id, parent_request_id, cmd, args (redacted), exit_code, error_code, elapsed_ms, kite_request_id, kite_order_id, strategy_id, agent_id. POSIX-atomic `O_APPEND` single-`write()` syscall. 8-year retention (SEBI broker regs). `main()` wraps every command so audit is always written, even on exception. `KITE_STRATEGY_ID` and `KITE_AGENT_ID` env vars propagate through every row for multi-agent attribution.
- **`status` command**: single-blob state introspection — `{session, market, rate_limit, account, live_window, halt}`. `--skip-account` bypasses broker call for offline diagnostics. Agent loops start every cycle with one call instead of six.
- **`time` command**: IST now, UTC now, token rotation window, market open/close per exchange, next weekly expiry (NSE Tuesday / BSE Thursday), MIS auto-squareoff cutoffs. Pure-local — no API call.
- **`watch` command**: `watch {quote|ltp|ohlc|order} --until "EXPR" --every N --timeout T`. Poll-until-condition with safe AST-restricted expression evaluator (`kite_algo/watch_expr.py`). No `eval()`, no attribute access, no function calls. Exit 0 with snapshot on match, exit 124 on timeout. Transient fetch errors don't abort — log and retry.
- **`events` command**: tail the SEBI-compliant audit log. `--since/--until YYYY-MM-DD --cmd-filter NAME --outcome ok|error --tail N`.
- **`group-start` / `group-status` / `group-cancel`** (`kite_algo/groups.py`): multi-leg transaction tracking. `group-start --name BEAR_PUT --legs 2` returns ULID group_id; `place --group-id G --leg-name short_put` associates orders. `group-status` shows all legs + live Kite state; `group-cancel` flattens every still-open leg. SQLite tables in the shared idempotency DB.
- **`reconcile` command**: diffs local audit + idempotency records against Kite's live orderbook. Buckets: `missing_locally`, `missing_remotely`, `mismatched`, `orphan_groups`. Exit 0 if clean, 1 if drift, 5 on TokenException. Foundational for agents surviving their own crashes.
- **Alerts API** (`kite_algo/alerts.py`): pykiteconnect v5.1.0 doesn't wrap `/alerts` (added server-side June 2025). Raw-HTTP client with subcommands `alerts-list/get/create/modify/delete/history`. Simple + ATO (Alert-Triggers-Order) supported. Rate-limited via the general bucket. 500/user cap documented.
- **Stream buffering + `tail-ticks`**: `stream --buffer-to FILE` also writes NDJSON to disk (each tick carries `_seq` + `_ts_epoch_ms`). `tail-ticks FILE --from-seq N --symbols ... --limit N --follow` reads the buffer; agents consume aggregated state without holding a WebSocket.
- **HaltActive exception** classified in `exit_codes.py` → exit 11.
- **Test suite**: 511 → 642 tests (+131). New files: `test_halt.py`, `test_audit.py`, `test_status_time.py`, `test_watch.py`, `test_events.py`, `test_groups.py`, `test_reconcile.py`, `test_alerts.py`, `test_tail_ticks.py`.
- **Total subcommands**: 68 (up from 48 pre-Wave-2; includes all new state-ops + alerts).

### Unreleased — Wave 2: structured output + agent ergonomics

- **Envelope**: every non-streaming CLI command now wraps its output as `{ok, cmd, schema_version, request_id, data, warnings, meta}` — see `kite_algo/envelope.py`. Request IDs are ULID-compatible (26 base32 chars, time-sortable, 2^80 entropy). `meta.parent_request_id` propagates from `KITE_PARENT_REQUEST_ID` env so nested agent workflows are traceable.
- **JSON-by-default off-TTY**: `--format auto` (new default) resolves to `json` when stdout is not a TTY (pipes, subprocesses, agent runs) and `table` when it is. `KITE_NO_ENVELOPE=1` escape hatch disables wrapping; `KITE_JSON=1` forces JSON.
- **Exit code taxonomy** (`kite_algo/exit_codes.py`): `OK=0, GENERIC=1, USAGE=2, VALIDATION=3, HARD_REJECT=4, AUTH=5, PERMISSION=6, LEASE=10, HALTED=11, OUT_OF_WINDOW=12, MARKET_CLOSED=13, UNAVAILABLE=69, INTERNAL=70, TRANSIENT=75, TIMEOUT=124, SIGINT=130`. `classify_exception` maps Kite SDK exceptions, our own classes, `SystemExit`, `KeyboardInterrupt`, and transient message markers to codes deterministically.
- **Structured errors** (`kite_algo/errors.py`): `emit_error(exc, env=...)` converts any exception into a single stderr JSON object with `{code, class, message, retryable, kite_request_id, field_errors, suggested_action, exit_code_name}`. Canned `suggested_action` hints for every error code. `with_error_envelope` decorator for one-line wrapping of `cmd_*` handlers.
- **`--idempotency-key`** (`kite_algo/idempotency.py`): durable SQLite-backed cache at `data/idempotency.sqlite` with WAL + PRAGMA. Tracks every write command's attempt/completion. On retry with the same key, a completed prior attempt is replayed (`meta.replayed=true`) without touching Kite. Incomplete rows survive — they represent ghost orders needing reconciliation. Keys are deterministically hashed via BLAKE2b to 18-char Kite tags so orderbook lookups find the prior in-flight order across process restarts. Wired into `cmd_place`.
- **Auto-batching** (`_batched_quote_call`): `ltp`, `ohlc`, `quote`, and `chain --quote` transparently split ≥500-symbol requests into per-500 chunks, dedupe input, preserve order, and merge results. Agents can pass 1000+ symbols without knowing Kite's limit.
- **`--fields a,b,c`**: column projection on list-returning commands. Missing fields emit as `null` for CSV header stability.
- **`--summary`**: compact rollups for `orders`, `holdings`, `positions`, `chain` (including ATM/IV/put-call-ratio/max-pain for chain). Cuts agent context cost 60–90% on high-cardinality endpoints.
- **`--explain`** (`kite_algo/explain.py`): per-command description of `{action, side_effects, preconditions, reversibility, idempotency, rate_limit_bucket, notes}`. Purely local — no API call. Different from `--dry-run` which hits `order_margins()`.
- **`tools-describe`** (`kite_algo/tool_schema.py`): emits a JSONSchema array for every subcommand, derived by live argparse introspection — never drifts from the CLI. Ready to paste directly into Claude `tools` or GPT `function_call` specs.
- **Rate limit buckets extended**: `quote` bucket (1 req/s, Kite's documented ceiling, was 10/s via general); `orders_day` sliding window at 3000/24h. `RateLimitedKiteClient` routes `ltp`/`ohlc`/`quote` to the quote bucket.
- **Modification counter**: `record_modification()` caps at 20 per order_id to stay under Kite's ~25 lifetime limit. `cmd_modify` now refuses beyond cap.
- **Test suite**: 349 → 511 tests (+162). New files: `test_envelope.py`, `test_errors.py`, `test_exit_codes.py`, `test_idempotency.py`, `test_projection.py`, `test_quote_batching.py`, `test_tool_schema.py`, `test_explain.py`.

### Unreleased — Wave 1: correctness + safety hardening

- **Config**: `_env_bool` is now strict — unknown values raise `EnvParseError` instead of silently defaulting, so a typo on `TRADING_ALLOW_LIVE` can never flip a safety gate.
- **Atomic writes**: `data/session.json` and `data/instruments/*.json` now write via `tempfile + os.replace`, opened with `O_CREAT|O_EXCL` at mode 0o600. No TOCTOU window where a token is world-readable; no corrupt cache from a crash mid-write.
- **Resilience**:
  - `TokenBucket` clamps negative floating-point deficits and bounds the max wait to `capacity/rate` — no more unbounded sleeps from drift.
  - `find_order_by_tag` now raises `OrderbookLookupError` on API failure instead of returning `None` — a lookup failure is never confused with "order not placed". `IdempotentOrderPlacer` refuses to retry `place_order` if the orderbook is unreachable throughout the poll window.
  - Case-insensitive tag matching in `find_order_by_tag`.
  - `_wait_for_fill` parses `order_timestamp` to `datetime` before sorting — same-second events with differing zero-padding no longer reorder.
  - Per-order modification counter (`record_modification`) caps at 20 to stay under Kite's ~25 limit.
- **Rate limits** (Kite 2026 docs alignment):
  - New `quote` bucket at **1 req/s** (was implicit 10/s via general bucket) — applies to `/quote`, `/ohlc`, `/ltp`.
  - New `orders_day` sliding window at 3000 req/24h.
  - `RateLimitedKiteClient` routes `ltp`/`ohlc`/`quote` to the quote bucket.
- **market_protection (SEBI April 2026)**:
  - Mandatory for MARKET and SL-M orders — OMS rejects if absent.
  - CLI `--market-protection` flag, auto-defaults to `-1` (Kite auto) for MARKET/SL-M. Validator enforces non-zero for those order types.
  - Wired through `place`, `modify`, and `place --dry-run`.
- **Iceberg cap**: reduced max legs from 50 → 10 per SEBI April 2026 circular. Iceberg also rejected on CNC / MTF products.
- **Market data**: `MarketDataSnapshot` now has `bid: float | None`, `ask: float | None`, `market_closed: bool`. Zero-price depth from Kite is translated to `None` so callers cannot price orders against a fake zero spread.
- **Secret redaction**: new `kite_algo/redaction.py` with a global `logging.Filter` that scrubs env + session tokens and pattern-matches Authorization headers, access-token-KV pairs, Bearer tokens, and long tokeny strings. Installed at module import so `KITE_DEBUG=1` is safe.
- **Indian market rules**: new `kite_algo/market_rules.py` encoding per-exchange hours (NSE/BSE/NFO/BFO/MCX/CDS), MIS auto-squareoff cutoff (15:20 IST equity, 23:25 IST MCX), freeze-quantity table for major underlyings (post-SEBI April 2026), lot-size table, weekly-expiry-day (NIFTY Tue, SENSEX Thu), token-rotation window (06:45–07:30 IST). `cmd_place` runs these as pre-flight checks; `--skip-market-rules` available for intentional AMO / testing.
- **Historical chunking**: `cmd_history` auto-splits large ranges into per-interval-cap windows (minute=60d, day=2000d, etc.) so agents don't hit Kite's "date range too large" InputException.
- **Test suite**: 171 → 349 tests. New files: `test_broker_kite.py`, `test_historical_chunking.py`, `test_kite_tool_helpers.py`, `test_market_rules.py`, `test_redaction.py`. Existing files got new cases for strict env parsing, atomic writes, orderbook-lookup failure propagation, modification counter, quote bucket, iceberg cap change, market_protection, bid/ask-None semantics.

### Initial scaffold

- **2026-04-14** Initial scaffold. Package skeleton (`kite_algo/`) mirroring `trading-algo`: `config.py` with `KiteConfig` + `TradingConfig` safety rails, `broker/` with base interface and `KiteBroker` stub, `cli.py` trading-engine skeleton, `kite_tool.py` comprehensive Kite CLI with argparse-wired commands for auth (login / profile / session), account (margins / holdings / positions / orders / trades), quotes (ltp / quote / ohlc), historical bars, instruments dump + local search, options (chain / option-quote / expiries), order ops (place / modify / cancel / cancel-all), GTT, margin calc, and mutual funds. Most command bodies are stubbed pending Kite integration.
- **2026-04-14** Scaffolded docs (`docs/ARCHITECTURE.md`, `SAFETY.md`, `WORKFLOWS.md`), `.github/` PR + issue templates, `.gitignore` excluding session files and data caches, `.env.example` with Kite credentials + safety rails + optional LLM section, `requirements.txt` (kiteconnect, pandas, numpy, scipy, pytest), and `CLAUDE.md` documenting project rules (daily token rotation, rate limits, product types, market hours).
- **2026-04-14** Created public GitHub repo [`mahimn01/kite-algo`](https://github.com/mahimn01/kite-algo).
