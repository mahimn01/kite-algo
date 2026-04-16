"""Resilience layer: rate limiting, retry with backoff, idempotent orders.

Kite Connect has no server-side idempotency and strict rate limits. This
module adds:

* Token-bucket rate limiter (three buckets: general 10/s, historical 3/s,
  orders 10/s with a 200/min sliding window). Uses Condition variables for
  correct multi-threaded fairness.
* Retry with exponential backoff for transient exceptions
  (NetworkException, DataException, 429, 5xx, timeouts). GeneralException
  is explicitly NOT retried — it is the catch-all for account blocks and
  risk-management rejections that must not be silently replayed.
* Tag-based idempotent order placement: every order gets a unique
  tag (secrets-derived, 14-char alphanumeric within Kite's 20-char limit).
  On transient failure, we poll the orderbook by tag with increasing delays
  (the OMS has observable propagation latency > 1s under load). We only
  retry if the orderbook does NOT show our tag after the full poll window.

Official Kite guidance: "Never retry POST orders without checking the
orderbook first."
"""

from __future__ import annotations

import collections
import functools
import logging
import random
import secrets
import threading
import time
from typing import Any, Callable, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Rate limiter — token bucket with Condition variable
# ---------------------------------------------------------------------------

class TokenBucket:
    """Thread-safe token bucket using a Condition variable.

    Consumers wait on the condition until enough tokens are available or
    the bucket is refilled. Avoids the starvation pattern of naive
    sleep-outside-lock implementations.
    """

    def __init__(self, rate_per_sec: float, capacity: float | None = None):
        self.rate = rate_per_sec
        self.capacity = capacity if capacity is not None else max(1.0, rate_per_sec)
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._cond = threading.Condition()

    def _refill(self) -> None:
        """Caller must hold self._cond."""
        now = time.monotonic()
        elapsed = now - self._last
        self._last = now
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)

    def acquire(self, tokens: float = 1.0, block: bool = True) -> bool:
        """Remove `tokens`; wait on the Condition if needed."""
        with self._cond:
            while True:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    self._cond.notify_all()
                    return True
                if not block:
                    return False
                deficit = tokens - self._tokens
                # +1ms floor, small jitter to break synchronisation storms
                wait = max(0.001, deficit / self.rate) + random.random() * 0.005
                self._cond.wait(timeout=wait)


class SlidingWindowLimiter:
    """Sliding-window limiter (for 'N requests per M seconds' rules)."""

    def __init__(self, max_requests: int, window_seconds: float):
        self.max = max_requests
        self.window = window_seconds
        self._events: collections.deque[float] = collections.deque()
        self._lock = threading.Lock()

    def acquire(self, block: bool = True) -> bool:
        while True:
            with self._lock:
                now = time.monotonic()
                cutoff = now - self.window
                while self._events and self._events[0] < cutoff:
                    self._events.popleft()
                if len(self._events) < self.max:
                    self._events.append(now)
                    return True
                wait = self._events[0] + self.window - now
            if not block:
                return False
            time.sleep(max(0.01, wait) + 0.001)


class KiteRateLimiter:
    """Kite-specific rate limits per the 2026 official docs.

    * 10 GET requests/second (combined across all endpoints per API key).
    * 3 requests/second for /historical.
    * 10 orders/second, 200 orders/minute.
    """

    def __init__(self) -> None:
        self.general = TokenBucket(rate_per_sec=10.0, capacity=10)
        self.historical = TokenBucket(rate_per_sec=3.0, capacity=3)
        self.orders_sec = TokenBucket(rate_per_sec=10.0, capacity=10)
        self.orders_min = SlidingWindowLimiter(max_requests=200, window_seconds=60.0)

    def wait_general(self) -> None:
        self.general.acquire()

    def wait_historical(self) -> None:
        self.historical.acquire()
        self.general.acquire()  # counts against general too

    def wait_order(self) -> None:
        self.orders_sec.acquire()
        self.orders_min.acquire()
        self.general.acquire()


# ---------------------------------------------------------------------------
# Rate-limited client wrapper
# ---------------------------------------------------------------------------

# Method-name classification for automatic bucket selection.
_ORDER_METHODS = frozenset({
    "place_order", "modify_order", "cancel_order",
    "place_gtt", "modify_gtt", "delete_gtt",
    "place_mf_order", "cancel_mf_order",
    "place_mf_sip", "modify_mf_sip", "cancel_mf_sip",
    "convert_position",
})
_HISTORICAL_METHODS = frozenset({"historical_data"})


class RateLimitedKiteClient:
    """Proxy that rate-limits every call to the wrapped KiteConnect client.

    Automatically picks the right bucket based on method name:
    * order-write methods → wait_order()
    * historical_data    → wait_historical()
    * everything else    → wait_general()

    Class attributes (e.g. KiteConnect.GTT_TYPE_SINGLE) are delegated
    transparently via __getattr__.
    """

    def __init__(self, client: Any, rate_limiter: KiteRateLimiter):
        # Use object.__setattr__ to bypass our own __setattr__ (if any).
        object.__setattr__(self, "_client", client)
        object.__setattr__(self, "_rate_limiter", rate_limiter)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._client, name)
        if not callable(attr):
            return attr
        if name in _ORDER_METHODS:
            waiter = self._rate_limiter.wait_order
        elif name in _HISTORICAL_METHODS:
            waiter = self._rate_limiter.wait_historical
        else:
            waiter = self._rate_limiter.wait_general

        @functools.wraps(attr)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            waiter()
            return attr(*args, **kwargs)

        return wrapped


# ---------------------------------------------------------------------------
# Retry with exponential backoff
# ---------------------------------------------------------------------------

# Hard-error exception class names — never retry.
_HARD_ERROR_NAMES = frozenset({
    "InputException", "OrderException", "TokenException", "PermissionException",
    "KeyboardInterrupt", "SystemExit",
})

# Transient-by-class names.
_TRANSIENT_EXCEPTION_NAMES = frozenset({
    "NetworkException", "DataException",
})


def _is_transient_error(exc: BaseException) -> bool:
    """Classify error as transient (retryable) or permanent.

    Kite guidance:
    - NetworkException, DataException: transient.
    - InputException, OrderException, TokenException, PermissionException: NEVER retry.
    - GeneralException: NOT transient. It is the catch-all for unclassified
      errors including account-level blocks — retrying can silently replay
      an already-rejected action.
    """
    name = type(exc).__name__
    if name in _HARD_ERROR_NAMES:
        return False
    if name in _TRANSIENT_EXCEPTION_NAMES:
        return True
    msg = str(exc).lower()
    transient_markers = (
        "timeout", "timed out", "connection reset", "temporarily unavailable",
        "429", "500", "502", "503", "504",
        "rate limit", "too many requests",
    )
    return any(m in msg for m in transient_markers)


def retry_with_backoff(
    max_attempts: int = 3,
    initial_delay: float = 0.5,
    max_delay: float = 10.0,
    jitter: bool = True,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Decorator: retry transient exceptions with exponential backoff.

    Catches `Exception`, NOT `BaseException` — so KeyboardInterrupt and
    SystemExit propagate immediately.
    """

    def decorator(fn: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            delay = initial_delay
            last_exc: Exception | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except Exception as exc:
                    last_exc = exc
                    if not _is_transient_error(exc) or attempt == max_attempts:
                        raise
                    sleep_for = min(max_delay, delay)
                    if jitter:
                        sleep_for *= 0.5 + random.random()
                    log.warning(
                        "transient error on %s attempt %d/%d: %s (sleeping %.2fs)",
                        fn.__name__, attempt, max_attempts, exc, sleep_for,
                    )
                    time.sleep(sleep_for)
                    delay *= 2
            assert last_exc is not None
            raise last_exc

        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Idempotent order placement
# ---------------------------------------------------------------------------

# 16-char alphanumeric alphabet (uppercase + digits). Kite allows 20 chars.
_TAG_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def new_order_tag(prefix: str = "KA") -> str:
    """Generate a unique order tag within Kite's 20-char alphanumeric limit.

    Uses the `secrets` module (cryptographically strong PRNG, OS entropy)
    rather than `random` — matters when multiple processes start at the
    same millisecond and could otherwise seed identically.

    Format: {prefix}{12 random chars from [A-Z0-9]}. Default length: 14.
    """
    body = "".join(secrets.choice(_TAG_ALPHABET) for _ in range(12))
    tag = f"{prefix}{body}"
    assert len(tag) <= 20 and tag.isalnum(), f"bad tag: {tag!r}"
    return tag


def find_order_by_tag(client: Any, tag: str) -> dict | None:
    """Search the day's orderbook for an order with the given tag."""
    try:
        orders = client.orders() or []
    except Exception as exc:
        log.error("orderbook lookup failed: %s", exc)
        return None
    for o in orders:
        if o.get("tag") == tag:
            return o
    return None


# OMS propagation delays: Kite's OMS can take several seconds under load
# to reflect a newly-placed order in /orders. Poll delays in seconds.
# Total window ≈ 7.5s before we assume the order was actually lost.
_ORDERBOOK_POLL_DELAYS = (0.5, 1.0, 1.5, 2.0, 2.5)


class IdempotentOrderPlacer:
    """Wrap client.place_order() with tag-based idempotency + retry.

    The tag is always round-tripped back in the orderbook, so:
        1. Generate a tag if one isn't provided.
        2. Try place_order().
        3. On transient failure: POLL the orderbook by tag (with increasing
           delays over ~7.5s to accommodate OMS propagation).
           - If the order is there: return its order_id (already placed).
           - If still absent after the full window: retry place_order().
        4. Hard failures (InputException, OrderException, TokenException,
           PermissionException) never retry — those are user/server
           rejections, not network blips.
    """

    def __init__(self, client: Any, rate_limiter: KiteRateLimiter | None = None):
        self.client = client
        self.rate_limiter = rate_limiter or KiteRateLimiter()

    def place(
        self,
        *,
        variety: str,
        exchange: str,
        tradingsymbol: str,
        transaction_type: str,
        quantity: int,
        product: str,
        order_type: str,
        tag: str | None = None,
        max_attempts: int = 3,
        **extra: Any,
    ) -> str:
        """Place an order with idempotent retry. Returns order_id."""
        if tag is None:
            tag = new_order_tag()

        payload: dict[str, Any] = {
            "variety": variety,
            "exchange": exchange,
            "tradingsymbol": tradingsymbol,
            "transaction_type": transaction_type,
            "quantity": quantity,
            "product": product,
            "order_type": order_type,
            "tag": tag,
            **{k: v for k, v in extra.items() if v is not None},
        }

        delay = 0.5
        last_exc: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            # Note: rate-limited via the RateLimitedKiteClient wrapper that
            # the CLI passes in. If a plain client is used, call wait_order
            # directly as a belt-and-braces check.
            if not isinstance(self.client, RateLimitedKiteClient):
                self.rate_limiter.wait_order()

            try:
                order_id = self.client.place_order(**payload)
                log.info("order placed: id=%s tag=%s attempt=%d", order_id, tag, attempt)
                return order_id
            except Exception as exc:
                last_exc = exc
                exc_name = type(exc).__name__

                # Hard errors: never retry — surface immediately.
                if exc_name in _HARD_ERROR_NAMES:
                    log.error("order rejected (%s): %s", exc_name, exc)
                    raise

                # Transient: poll orderbook before retrying.
                if not _is_transient_error(exc):
                    raise

                log.warning(
                    "transient error on place_order attempt %d/%d: %s — polling orderbook by tag %s",
                    attempt, max_attempts, exc, tag,
                )

                found = self._poll_orderbook_for_tag(tag)
                if found is not None:
                    oid = found.get("order_id", "")
                    log.info(
                        "order found via orderbook after transient error — tag=%s id=%s status=%s",
                        tag, oid, found.get("status"),
                    )
                    return oid

                if attempt == max_attempts:
                    break

                sleep_for = min(10.0, delay) * (0.5 + random.random())
                log.info("retrying place_order in %.2fs", sleep_for)
                time.sleep(sleep_for)
                delay *= 2

        assert last_exc is not None
        raise last_exc

    def _poll_orderbook_for_tag(self, tag: str) -> dict | None:
        """Poll the orderbook with increasing delays to catch propagation lag."""
        for step_delay in _ORDERBOOK_POLL_DELAYS:
            time.sleep(step_delay)
            found = find_order_by_tag(self.client, tag)
            if found is not None:
                return found
        return None
