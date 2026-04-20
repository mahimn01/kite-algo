"""Secret redaction for logs, error messages, and any stderr output.

Threats this module addresses:

1. Access tokens and API secrets appearing in error strings that Kite's SDK
   occasionally echoes back (request-body parroting on 4xx validation fails).
2. OAuth request_token leaking into a log if `cmd_login` crashes after the
   token was read but before it was exchanged.
3. HMAC checksums (`sha256(api_key+request_token+api_secret)`) — harmless on
   their own, but a paired checksum + request_token leak reveals the secret.
4. Generic "Authorization: token ..." headers if they end up stringified.
5. Session JSON blobs printed in full (debug logs).

Usage:
    from kite_algo.redaction import redact_text, install_logging_filter

    # One-shot redaction of any string about to reach a user-visible surface:
    print(redact_text(str(exc)), file=sys.stderr)

    # At process start, attach a filter to every logger so KITE_DEBUG=1 is safe:
    install_logging_filter()

The filter is idempotent — multiple installs do nothing.  It uses only
static patterns plus any known secrets from the current process environment
/ session file, so it cannot exfiltrate secrets through its own operation.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Iterable

REDACTED = "***REDACTED***"


# Regex patterns for secret-shaped tokens. These cast a wide net and may
# over-redact — that is the desired failure mode. Never the other way.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Authorization header forms. The payload can contain any non-space bytes
    # after `token `, including underscores and colons.
    ("auth-header", re.compile(r"(?i)(authorization:\s*token\s+)\S+")),
    # Any "access_token": "...", 'access_token': '...', or access_token=...
    # Key may be wrapped in single quotes, double quotes, or bare. Value may
    # be double-quoted, single-quoted, or a bare non-space token.
    ("access-token-kv", re.compile(
        r"""(?ix)
        (                                                     # group 1: key + separator (kept)
          ["']?
          (?:access_token|refresh_token|public_token|request_token|api_secret|api_key|order_token)
          ["']?
          \s*[:=]\s*
        )
        ( "[^"]+" | '[^']+' | \S+ )                           # group 2: value (replaced)
        """
    )),
    # Long hex / base64 strings that look tokeny (>=32 chars, mostly alnum).
    # Heuristic — may over-redact. Acceptable trade.
    ("long-token", re.compile(r"\b[A-Za-z0-9_\-]{32,}\b")),
    # Bearer tokens.
    ("bearer", re.compile(r"(?i)(bearer\s+)\S+")),
)


def _patterns_sub(text: str) -> str:
    out = text
    for name, rx in _PATTERNS:
        if name == "access-token-kv":
            out = rx.sub(lambda m: f"{m.group(1)}{REDACTED}", out)
        elif name == "auth-header":
            out = rx.sub(lambda m: f"{m.group(1)}{REDACTED}", out)
        elif name == "bearer":
            out = rx.sub(lambda m: f"{m.group(1)}{REDACTED}", out)
        else:
            out = rx.sub(REDACTED, out)
    return out


def known_secrets() -> list[str]:
    """Collect any secrets we already know from the current process.

    Reads from env vars (KITE_API_SECRET, KITE_ACCESS_TOKEN, TRADING_ORDER_TOKEN)
    and the session file. Returns a list of candidate secret strings. Empty /
    short strings (< 8 chars) are dropped — too risky to use as a literal
    replacement target (false-positive rate goes up, real value drops).
    """
    candidates: list[str] = []

    for var in (
        "KITE_API_SECRET",
        "KITE_ACCESS_TOKEN",
        "KITE_API_KEY",  # not secret but often paired with one
        "TRADING_ORDER_TOKEN",
        "GEMINI_API_KEY",
    ):
        v = os.getenv(var)
        if v and len(v) >= 8:
            candidates.append(v)

    # Session file can also contain access_token, public_token, request_token.
    try:
        import json
        sess_path = Path(os.getenv("KITE_SESSION_PATH") or "data/session.json")
        if sess_path.exists():
            sess = json.loads(sess_path.read_text(encoding="utf-8") or "{}")
            for k in ("access_token", "public_token", "refresh_token", "request_token"):
                v = sess.get(k)
                if v and isinstance(v, str) and len(v) >= 8:
                    candidates.append(v)
    except Exception:
        # If we can't read session, just rely on env and patterns.
        pass

    return candidates


def redact_text(text: str, *, extra_secrets: Iterable[str] = ()) -> str:
    """Redact secrets from `text`.

    Two layers:
      1. Known secrets (from env + session file + `extra_secrets`) are
         replaced literally — most reliable.
      2. Regex patterns catch anything that *looks* like a token, even if we
         don't know it yet.

    Empty/non-string inputs are returned unchanged.
    """
    if not isinstance(text, str) or not text:
        return text if isinstance(text, str) else str(text)

    out = text
    all_secrets = list(known_secrets()) + [s for s in extra_secrets if s]
    # Sort by length desc so longer secrets redact first (avoids e.g.
    # replacing a token prefix that's shared with a shorter secret).
    for s in sorted({s for s in all_secrets if s and len(s) >= 8}, key=len, reverse=True):
        out = out.replace(s, REDACTED)
    out = _patterns_sub(out)
    return out


# ---------------------------------------------------------------------------
# Global logging filter
# ---------------------------------------------------------------------------

class _SecretRedactingFilter(logging.Filter):
    """Rewrite LogRecord.msg / args so emitted log lines carry no secrets.

    Applied at the root logger so it covers every module. `kite_tool.log`,
    `kite_algo.broker.kite.log`, and third-party loggers (e.g. `kiteconnect`,
    `urllib3`, `requests`) all pass through.

    Performance: the expensive work (env + session reads) is cached for the
    lifetime of the filter. If secrets rotate mid-process (e.g. after
    `cmd_login`), call `install_logging_filter(reset=True)` to refresh.
    """

    def __init__(self) -> None:
        super().__init__()
        self._secrets_cache: list[str] | None = None

    def _secrets(self) -> list[str]:
        if self._secrets_cache is None:
            self._secrets_cache = known_secrets()
        return self._secrets_cache

    def filter(self, record: logging.LogRecord) -> bool:
        # Rewrite msg + args. If args are present, the record gets formatted
        # at emit time by logger.handle → we need to redact BOTH pre-format
        # (the message template may contain the secret) and the args.
        try:
            secrets = self._secrets()
            if isinstance(record.msg, str):
                redacted = _sub_many(record.msg, secrets)
                redacted = _patterns_sub(redacted)
                record.msg = redacted
            # Args can be a tuple (positional) or dict (named).
            if record.args:
                if isinstance(record.args, tuple):
                    record.args = tuple(
                        _sub_many_any(a, secrets) for a in record.args
                    )
                elif isinstance(record.args, dict):
                    record.args = {
                        k: _sub_many_any(v, secrets)
                        for k, v in record.args.items()
                    }
        except Exception:
            # Never let the filter itself crash log emission.
            pass
        return True


def _sub_many(text: str, secrets: list[str]) -> str:
    out = text
    for s in sorted({s for s in secrets if s and len(s) >= 8}, key=len, reverse=True):
        out = out.replace(s, REDACTED)
    return out


def _sub_many_any(value: Any, secrets: list[str]) -> Any:
    if isinstance(value, str):
        return _patterns_sub(_sub_many(value, secrets))
    return value


_FILTER_INSTALLED: _SecretRedactingFilter | None = None


def install_logging_filter(reset: bool = False) -> None:
    """Attach the redacting filter to the root logger (idempotent).

    Use `reset=True` after a login/token rotation to force the cache to
    re-read secrets. Otherwise the filter is installed once and reused.
    """
    global _FILTER_INSTALLED
    root = logging.getLogger()
    if _FILTER_INSTALLED is not None and not reset:
        return
    if _FILTER_INSTALLED is not None:
        try:
            root.removeFilter(_FILTER_INSTALLED)
        except Exception:
            pass
    _FILTER_INSTALLED = _SecretRedactingFilter()
    root.addFilter(_FILTER_INSTALLED)
    # Also attach to each handler so it applies before any formatter:
    for h in root.handlers:
        h.addFilter(_FILTER_INSTALLED)
