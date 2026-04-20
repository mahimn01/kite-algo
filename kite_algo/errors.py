"""Structured error emission for agent-driven error recovery.

Every top-level `cmd_*` that can fail wraps its body in
`with_error_handling(cmd, env)` so that any uncaught exception is converted
to a structured stderr JSON blob and the correct exit code. Agents never
have to regex free-text error messages.

Error JSON shape:

    {
      "ok": false,
      "cmd": "place",
      "schema_version": "1",
      "request_id": "...",
      "data": null,
      "warnings": [],
      "meta": {...},
      "error": {
        "code": "HARD_REJECT",
        "class": "OrderException",
        "message": "Insufficient funds",
        "retryable": false,
        "kite_request_id": "abc123",     // populated when available
        "field_errors": [                 // per-field for validation errors
          {"field": "quantity", "message": "exceeds freeze limit"}
        ],
        "suggested_action": "..."
      }
    }

stdout carries the success envelope; stderr carries the error envelope on
failure. An agent reads stderr's final JSON blob to understand what happened.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, is_dataclass
from typing import Any

from kite_algo.envelope import (
    Envelope,
    envelope_to_json,
    finalize_envelope,
)
from kite_algo.exit_codes import (
    ClassifiedError,
    classify_exception,
    exit_code_name,
)
from kite_algo.redaction import redact_text


# ---------------------------------------------------------------------------
# Suggested-action hints
# ---------------------------------------------------------------------------

# Canned remediation text per error code — the agent gets a hint without
# needing a second API call.
_SUGGESTED_ACTIONS: dict[str, str] = {
    "AUTH": (
        "Re-authenticate with `python -m kite_algo.kite_tool login`. "
        "Kite access tokens rotate daily between 06:45–07:30 IST."
    ),
    "HARD_REJECT": (
        "Inspect `error.message` and fix the order parameters. This is NOT "
        "retryable — the OMS explicitly rejected the request."
    ),
    "PERMISSION": (
        "Your account lacks access to this endpoint. Historical data, MF, "
        "and some order types require separate Kite Connect subscriptions."
    ),
    "VALIDATION": (
        "Inspect `error.field_errors`; each entry names the field and the "
        "rule that was violated. Fix and retry — no API call was made."
    ),
    "USAGE": (
        "Re-run with `--help` to see the expected flags. Every destructive "
        "command requires `--yes`."
    ),
    "UNAVAILABLE": (
        "Kite OMS or network appears flaky. Retry with exponential backoff. "
        "For order placement, first GET /orders and match on `tag` to avoid "
        "double-fills."
    ),
    "TRANSIENT": (
        "Transient error (timeout / 429 / 5xx). Retry with backoff; for "
        "orders, confirm via orderbook before retrying."
    ),
    "INTERNAL": (
        "Unexpected error inside kite-algo. Capture `request_id`, `message`, "
        "and file a bug. Do NOT silently retry."
    ),
    "SIGINT": (
        "User interrupt. No retry."
    ),
    "LEASE": (
        "Another agent holds the trading lease. Retry after the lease TTL "
        "expires or have the holder release it."
    ),
    "HALTED": (
        "Trading is administratively halted via `halt`. Resume with "
        "`kite-algo resume --confirm-resume`."
    ),
    "MARKET_CLOSED": (
        "Regular session is closed for this exchange. Use `--variety amo` "
        "for after-market orders, or wait for next open."
    ),
    "OUT_OF_WINDOW": (
        "Order falls outside the configured live-trade window "
        "(KITE_LIVE_WINDOW_*). Adjust window or retry later."
    ),
    "TIMEOUT": (
        "Deadline elapsed while waiting for a terminal state. The operation "
        "may still be in progress — poll `order-history` separately."
    ),
    "GENERIC": (
        "Partial or non-specific failure. Inspect `error.message` for detail."
    ),
}


def suggested_action(error_code: str) -> str:
    return _SUGGESTED_ACTIONS.get(error_code, "See `error.message`.")


# ---------------------------------------------------------------------------
# Error extraction
# ---------------------------------------------------------------------------

def _kite_request_id(exc: BaseException) -> str | None:
    """kiteconnect attaches a `request_id` to most exception instances (the
    value Kite's OMS used in its response headers). Extract if present.
    """
    rid = getattr(exc, "request_id", None) or getattr(exc, "kite_request_id", None)
    if isinstance(rid, str) and rid.strip():
        return rid.strip()
    return None


def _field_errors(exc: BaseException) -> list[dict]:
    """Extract per-field validation errors from known exception types."""
    # Our own ValidationError list (from kite_algo.validation) — if the
    # exception carries one.
    errs = getattr(exc, "field_errors", None)
    if isinstance(errs, list):
        out = []
        for e in errs:
            if is_dataclass(e):
                out.append(asdict(e))
            elif isinstance(e, dict):
                out.append(e)
        return out
    return []


def build_error_payload(
    exc: BaseException,
    *,
    classified: ClassifiedError | None = None,
) -> dict:
    """Build the `error` sub-object for the envelope.

    Every string passes through `redact_text` so secrets can't leak via an
    error echo.
    """
    if classified is None:
        classified = classify_exception(exc)
    message = redact_text(str(exc) or type(exc).__name__)
    payload: dict[str, Any] = {
        "code": classified.error_code,
        "exit_code": classified.exit_code,
        "exit_code_name": exit_code_name(classified.exit_code),
        "class": type(exc).__name__,
        "message": message,
        "retryable": classified.retryable,
        "suggested_action": suggested_action(classified.error_code),
    }
    rid = _kite_request_id(exc)
    if rid:
        payload["kite_request_id"] = rid
    fe = _field_errors(exc)
    if fe:
        payload["field_errors"] = fe
    return payload


# ---------------------------------------------------------------------------
# Emission entry point
# ---------------------------------------------------------------------------

def emit_error(
    exc: BaseException,
    *,
    env: Envelope,
    stream=None,
) -> int:
    """Populate `env` with a structured error, emit JSON to stderr, return
    the exit code. Call site: every top-level `cmd_*` catch-all.

    The envelope is finalised in-place (elapsed_ms, etc.) and the full
    envelope is emitted to stderr. Returns the classified exit code so the
    caller can use `return emit_error(exc, env=env)`.
    """
    classified = classify_exception(exc)
    env.ok = False
    env.data = None
    env.error = build_error_payload(exc, classified=classified)
    finalize_envelope(env)

    out = stream if stream is not None else sys.stderr
    try:
        out.write(envelope_to_json(env))
        out.write("\n")
        out.flush()
    except BrokenPipeError:
        pass

    return classified.exit_code


def with_error_envelope(cmd: str):
    """Decorator for `cmd_*` functions: catches every exception, emits the
    structured error, and returns the right exit code.

    Usage:

        @with_error_envelope("place")
        def cmd_place(args, *, env):
            ...
            env.data = result
            return 0

    The decorator injects `env` as a kwarg so commands can attach warnings
    or custom meta fields during execution.
    """
    def decorator(fn):
        import functools

        @functools.wraps(fn)
        def wrapper(args):
            from kite_algo.envelope import new_envelope

            env = new_envelope(cmd)
            try:
                return fn(args, env=env)
            except SystemExit:
                # argparse / _require_yes — let it propagate.
                raise
            except BaseException as exc:
                return emit_error(exc, env=env)

        return wrapper

    return decorator
