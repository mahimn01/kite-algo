"""Enumerated exit codes for agent-driven control flow.

A CLI consumer — especially an AI agent — needs to branch on *why* a command
failed before it reads any output. Collapsing every failure into `1` forces
the agent to string-match stderr, which is slow, error-prone, and gives the
model yet another free-text parsing problem.

Each code below has a specific meaning; agents can build decision tables on
them.

References:
- sysexits.h (BSD): https://www.man7.org/linux/man-pages/man3/sysexits.h.3head.html
- kubectl conventions: 0 success, 1 generic, 2 usage, N per-resource
- git: 0 success, 1 logic, 2 usage, 128 internal
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------

OK = 0
"""Success. Side effects (if any) completed as described."""

GENERIC = 1
"""Non-specific failure. Only used when we cannot classify more precisely;
prefer a specific code below whenever possible.

Also used for *partial* successes: e.g. `cancel-all` where some cancels
succeeded and others failed — exit 1 lets the agent know to inspect, even
though nothing catastrophic happened.
"""

USAGE = 2
"""Bad invocation: missing `--yes`, unknown flag, wrong enum value. The
command itself is recognisable but the arguments can't be used. argparse
already exits 2 for unknown flags — we align with that."""

VALIDATION = 3
"""Pre-flight client-side validation rejected the request. No API call was
made. Agent should inspect `error.field_errors` and retry with fixed params.
Examples: iceberg_legs out of range, negative quantity, LIMIT without price,
MARKET without market_protection."""

HARD_REJECT = 4
"""Kite's OMS rejected the request. Never retry. Examples: insufficient
margin, price outside circuit band, freeze quantity exceeded, contract
expired. Corresponds to Kite exceptions: InputException, OrderException,
MarginException, HoldingException."""

AUTH = 5
"""Token invalid or expired. Corresponds to Kite's TokenException and
HTTP 401/403. Agent must re-run `login` (tokens rotate daily between 06:45
and 07:30 IST, plus on-demand if user signed in elsewhere)."""

PERMISSION = 6
"""Feature not available to this account. Examples: historical data without
subscription, MF endpoints without the MF add-on. Corresponds to Kite's
PermissionException."""

LEASE = 10
"""Another agent/process holds the trading lease. Retry after backoff.
Used by the multi-agent coordination primitives (§Wave 3)."""

HALTED = 11
"""Trading is administratively halted via the `halt` command. Resume with
`kite-algo resume --confirm-resume`. Writes are refused while halted."""

OUT_OF_WINDOW = 12
"""Order attempted outside the configured live-trade window
(e.g. KITE_LIVE_WINDOW_START/END). Also used for MIS past 15:20 cutoff."""

MARKET_CLOSED = 13
"""Regular session is closed for the target exchange. Retry at next market
open or pass --variety amo."""

UNAVAILABLE = 69
"""Upstream service unavailable. Corresponds to HTTP 502/503 and Kite's
NetworkException/DataException. Retryable with backoff."""

INTERNAL = 70
"""Uncaught exception inside kite-algo (our bug). Corresponds to Kite's
GeneralException catch-all — we tag as INTERNAL because we cannot safely
classify it as transient. Investigate; do NOT silently retry."""

TRANSIENT = 75
"""Transient failure we've classified (429, timeout, connection reset).
Retryable with backoff."""

TIMEOUT = 124
"""A wait-for-state call hit its deadline without reaching the expected
terminal state. Matches coreutils `timeout(1)`. The operation may still be
in progress — poll separately."""

SIGINT = 130
"""User interrupt (Ctrl+C). Standard Unix 128 + SIGINT(2)."""


# All codes we emit; useful for tests.
ALL_CODES = frozenset({
    OK, GENERIC, USAGE, VALIDATION, HARD_REJECT, AUTH, PERMISSION,
    LEASE, HALTED, OUT_OF_WINDOW, MARKET_CLOSED,
    UNAVAILABLE, INTERNAL, TRANSIENT, TIMEOUT, SIGINT,
})


@dataclass(frozen=True)
class ClassifiedError:
    """Result of classifying an exception for agent consumption."""
    exit_code: int
    error_code: str       # stable string identifier, e.g. "AUTH", "HARD_REJECT"
    retryable: bool


# Map Kite SDK exception class names (we match on __name__ to avoid an import
# dependency on kiteconnect) → our code categories.
_EXCEPTION_MAP: dict[str, ClassifiedError] = {
    "TokenException":      ClassifiedError(AUTH, "AUTH", retryable=False),
    "InputException":      ClassifiedError(HARD_REJECT, "HARD_REJECT", retryable=False),
    "OrderException":      ClassifiedError(HARD_REJECT, "HARD_REJECT", retryable=False),
    "MarginException":     ClassifiedError(HARD_REJECT, "HARD_REJECT", retryable=False),
    "HoldingException":    ClassifiedError(HARD_REJECT, "HARD_REJECT", retryable=False),
    "UserException":       ClassifiedError(HARD_REJECT, "HARD_REJECT", retryable=False),
    "PermissionException": ClassifiedError(PERMISSION, "PERMISSION", retryable=False),
    "NetworkException":    ClassifiedError(UNAVAILABLE, "UNAVAILABLE", retryable=True),
    "DataException":       ClassifiedError(UNAVAILABLE, "UNAVAILABLE", retryable=True),
    # GeneralException is the SDK's catch-all — we do NOT mark it retryable.
    # Kite's own guidance: "account blocks arrive as GeneralException; do not
    # silently replay." Retrying guarantees double-charges if the first
    # attempt partially succeeded.
    "GeneralException":    ClassifiedError(INTERNAL, "INTERNAL", retryable=False),
    # Our own classes.
    "OrderbookLookupError": ClassifiedError(UNAVAILABLE, "UNAVAILABLE", retryable=True),
    "ModificationLimitExceeded": ClassifiedError(HARD_REJECT, "HARD_REJECT", retryable=False),
    "KiteSessionError":    ClassifiedError(AUTH, "AUTH", retryable=False),
    "KiteDependencyError": ClassifiedError(INTERNAL, "INTERNAL", retryable=False),
    "EnvParseError":       ClassifiedError(USAGE, "USAGE", retryable=False),
    "HaltActive":          ClassifiedError(HALTED, "HALTED", retryable=False),
}


def classify_exception(exc: BaseException) -> ClassifiedError:
    """Return the exit-code + error-code + retryable classification for `exc`.

    Priority order:
      1. Non-Exception BaseException: KeyboardInterrupt, SystemExit.
      2. Class-name lookup in `_EXCEPTION_MAP` — our own classes and Kite SDK
         class-name strings. This takes precedence over `isinstance` builtins
         because some of our classes (e.g. `EnvParseError`) inherit from
         `ValueError` but have semantics distinct from "general value error".
      3. `ValueError` / `TypeError` → VALIDATION.
      4. Message marker scan for transient HTTP / timeout strings.
      5. Default: INTERNAL (safe — do not retry).
    """
    # 1. Non-Exception BaseException.
    if isinstance(exc, KeyboardInterrupt):
        return ClassifiedError(SIGINT, "SIGINT", retryable=False)
    if isinstance(exc, SystemExit):
        code = int(exc.code) if isinstance(exc.code, int) else GENERIC
        # String-message SystemExits (our `_require_yes` gates) are user errors.
        if isinstance(exc.code, str):
            return ClassifiedError(USAGE, "USAGE", retryable=False)
        return ClassifiedError(code, "GENERIC", retryable=False)

    # 2. Class-name lookup — takes precedence over isinstance builtins.
    name = type(exc).__name__
    if name in _EXCEPTION_MAP:
        return _EXCEPTION_MAP[name]

    # 3. Generic value / type errors.
    if isinstance(exc, (ValueError, TypeError)):
        return ClassifiedError(VALIDATION, "VALIDATION", retryable=False)

    # 4. Message markers for transient conditions.
    msg = str(exc).lower()
    transient_markers = (
        "timeout", "timed out", "connection reset",
        "429", "500", "502", "503", "504",
        "rate limit", "too many requests",
    )
    if any(m in msg for m in transient_markers):
        return ClassifiedError(TRANSIENT, "TRANSIENT", retryable=True)

    # 5. Fallback.
    return ClassifiedError(INTERNAL, "INTERNAL", retryable=False)


def exit_code_name(code: int) -> str:
    """Reverse lookup: int → constant name. For error JSON suggested_action."""
    for name, value in globals().items():
        if name.isupper() and value == code and name != "ALL_CODES":
            return name
    return f"UNKNOWN_{code}"
