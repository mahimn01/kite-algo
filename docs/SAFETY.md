# Safety Model

Kite is live-money from day one — there is no paper account. Use `SimBroker`
for offline testing.

## Layers

1. **Env var gates**
   - `TRADING_BROKER=kite` — routes to live Kite
   - `TRADING_BROKER=sim` — routes to SimBroker (always safe)
   - `TRADING_ALLOW_LIVE=true` — required to enable live writes
   - `TRADING_LIVE_ENABLED=true` — second gate, must also be true
   - `TRADING_DRY_RUN=true` — short-circuits before any network write

2. **CLI gates**
   - Every order-placing command in `kite_tool` requires `--yes`
   - `TRADING_CONFIRM_TOKEN_REQUIRED=true` forces `--confirm-token` to match
     `TRADING_ORDER_TOKEN` on every call

3. **Broker gate** (`KiteBroker._require_live`)
   - Raises if `dry_run` is true
   - Raises unless both `allow_live` and `live_enabled` are set

4. **Audit**
   - Optional sqlite audit trail at `TRADING_DB_PATH` logs every order
     request, response, and state change (implementation pending)

## Defaults

Everything defaults to safe:
- `TRADING_DRY_RUN=true`
- `TRADING_ALLOW_LIVE=false`
- `TRADING_LIVE_ENABLED=false`
- `TRADING_BROKER=kite` (but writes are blocked by the above)

To actually place a live order you must:
1. Set three env vars to their explicit "yes" values
2. Run the command with `--yes`
3. Respond to any interactive confirmation prompt

## Rate limits

Kite's published limits:
- 3 req/s on most endpoints
- 10 req/s on `/quote` and `/ltp`
- 200k calls/day cap

The CLI doesn't currently enforce these (beyond what `kiteconnect` does
internally). When the broker adapter grows a write path, we'll add a token
bucket mirror of `trading_algo.broker.ibkr`'s `RateLimiter`.

## Daily token expiry

This is a **hard** safety property: if you haven't logged in today,
everything fails closed. No cached token will bypass the 6am IST rotation.
This is a feature, not a bug — it ensures at least one human action per day
before any live order can go through.
