# Login workflows

Kite access tokens rotate every day between **06:45–07:30 IST**. There's no refresh token — every trading day needs a fresh human login. `kite-algo login` supports three ways to complete that login, so you never have to be at your trading machine.

## What NOT to do

- **Do not Selenium-automate the Kite login.** The script would have to store your Kite password + TOTP secret, and it would break on every Kite UI change. Zerodha has banned API keys for this (forum thread 1876). Your repo's `CLAUDE.md` enforces this rule.
- **Do not paste `--request-token` as a CLI flag.** It would leak into `ps`, shell history, `/proc/<pid>/cmdline`. `login` only accepts the token via a listener callback or `getpass` (no echo, no history).

## Mode 1 — Local listener (default)

When you're at the machine that runs the trading stack:

```bash
kite-algo login
```

This binds a one-shot HTTP server on `http://127.0.0.1:5000/`, opens the Kite login URL in your browser, waits for Kite's 302 redirect to hit the listener, then exchanges the captured `request_token` for an access token.

**Prereq:** Your Kite app profile at [developers.kite.trade/apps](https://developers.kite.trade/apps) must register `http://127.0.0.1:5000/` as its **Redirect URI**. Kite matches ports literally — no wildcards — so if you use `--listen-port 49732`, the profile must say `http://127.0.0.1:49732/` too.

## Mode 2 — Remote login over SSH (the "not at my computer" case)

You're at your laptop (or phone with an SSH client like Termius / Blink). The trading box is somewhere else. You want to log in without getting up.

The trick: **SSH port-forwarding maps `localhost:5000` on your laptop to `localhost:5000` on the trading box.** Kite's 302 redirect hits your laptop's `127.0.0.1:5000`, SSH forwards it over the encrypted tunnel to the trading box's listener, which catches it and saves `data/session.json` on the box.

### Recipe

From your laptop (or phone terminal):

```bash
# 1. SSH with port forwarding. -L local_port:remote_host:remote_port
ssh -L 5000:127.0.0.1:5000 user@trading-box

# 2. On the trading box:
kite-algo login
```

The `login` command prints a URL. Open it in your laptop's browser (or your phone's). Sign in with your Zerodha credentials + TOTP.

Kite redirects to `http://127.0.0.1:5000/?action=login&status=success&request_token=XXX&state=YYY`. On your laptop, `127.0.0.1:5000` goes through the SSH tunnel to the trading box's listener. The listener catches the callback, verifies the CSRF nonce, exchanges the token, and writes `data/session.json` on the trading box.

Your laptop's browser shows a little "✓ Login captured" page — close the tab and you're done.

**What your credentials/TOTP see:** your laptop's browser + Zerodha's servers. They never touch the trading box or any script. This is legitimate OAuth — it's what `gh auth login`, `stripe login`, and `gcloud auth login` do.

## Mode 3 — Paste fallback (`--paste`)

For exotic setups — agent runners in a sandbox, servers without SSH tunneling permitted, or when you need to sign in on a device (phone) with no easy way to reach the trading box:

```bash
kite-algo login --paste
```

This is the original copy/paste flow. You open the URL, sign in, and when Kite redirects to `http://127.0.0.1/?...&request_token=XXX&...`, your browser will show a "site can't be reached" error. That's fine — the URL in the address bar still contains `request_token=XXX`. Copy just the token value, paste it at the `request_token:` prompt (input is hidden via `getpass`).

## Flags

| Flag | Default | Purpose |
|------|---------|---------|
| `--listen-port PORT` | 5000 | Listener port. Must match the port in your app's registered redirect URI. |
| `--timeout SECS` | 300 | Max seconds to wait for callback. Kite's `request_token` expires in ~5 min anyway. |
| `--no-browser` | off | Don't auto-open; just print the URL. Useful over SSH. |
| `--paste` | off | Mode 3 fallback. Skip listener entirely. |

## Security properties

- **Bind is 127.0.0.1 only.** The CallbackServer class raises `LocalBindOnlyError` if asked to bind anywhere else — a LAN-exposed listener would let anyone on the network race to intercept your `request_token`.
- **CSRF state nonce.** `login` generates a 256-bit random nonce per invocation, passes it through Kite's `redirect_params`, and rejects any callback whose `state` doesn't match. A stale redirect from a previous attempt or a cross-origin prank can't complete someone else's flow.
- **Session file at mode 0o600, atomic write.** The daily-rotating `access_token` lands in `data/session.json` via `os.open(O_CREAT|O_EXCL|O_WRONLY, mode=0o600)` + atomic `rename`. No TOCTOU window where another local user could read a world-readable version.
- **Redaction filter refreshes post-login.** Every log line in the same process is secret-scrubbed against the new `access_token` before it hits stdout/stderr.
- **No `--request-token` CLI flag.** The paste flow uses `getpass` so the token doesn't appear in `ps`/history.

## Troubleshooting

**Callback timeout, no error body.** Your app profile's redirect URI doesn't match `http://127.0.0.1:<port>/` exactly. Check developers.kite.trade/apps — even a trailing-slash difference blocks the redirect.

**`csrf_mismatch` in the listener page.** You're using a stale login URL from a previous `login` attempt. Re-run `login`.

**`bad_status:error` or `bad_status:missing`.** Kite redirected but the status wasn't `success`. Common causes:
- The user is not enabled on the app (check developer console).
- `api_key` is wrong.
- The account needs to re-accept the app's permissions.

**`port is already in use`.** Either another `kite-algo login` is running, or a previous listener didn't clean up (rare — `CallbackServer.stop()` is idempotent). Use `--listen-port` with a different number (and update the registered redirect URI to match), or wait ~30s for the kernel to release the port.

**I signed in on my phone and the listener never caught it.** Phones resolve `127.0.0.1` to themselves, not to your trading box. You need an SSH tunnel from your phone (Termius does `-L`), OR sign in on a machine that IS the listener, OR use `--paste` and paste the token back via SSH.
