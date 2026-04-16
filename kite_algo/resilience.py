"""Resilience layer: rate limiting, retry with backoff, idempotent orders.

Kite Connect has no server-side idempotency and strict rate limits. This
module adds:

* Token-bucket rate limiter (three buckets: general 10/s, historical 3/s,
  orders 10/s with a 200/min sliding window).
* Retry with exponential backoff for transient exceptions
  (NetworkException, DataException, 429, 502, 503, 504).
* Tag-based idempotent order placement: every order gets a unique tag
  (16-char alphanumeric, within Kite's 20-char limit). On retry after
  timeout, we search the orderbook by tag before re-submitting — this
  prevents double-fills when place_order() times out but the order
  actually made it through to the OMS.

Official Kite guidance: "Never retry POST orders without checking the
orderbook first."
"""

from __future__ import annotations

import collections
import functools
import logging
import random
import string
import threading
import time
import uuid
from typing import Any, Callable, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Rate limiter — token bucket
# ---------------------------------------------------------------------------

class TokenBucket:
    """Thread-safe token bucket."""

    def __init__(self, rate_per_sec: float, capacity: float | None = None):
        self.rate = rate_per_sec
        self.capacity = capacity if capacity is not None else max(1.0, rate_per_sec)
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, tokens: float = 1.0, block: bool = True) -> bool:
        """Remove `tokens` from the bucket, waiting if needed. Returns True."""
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last
                self._last = now
                self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return True
                deficit = tokens - self._tokens
                wait = deficit / self.rate
            if not block:
                return False
            time.sleep(wait + 0.001)


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
# Retry with exponential backoff
# ---------------------------------------------------------------------------

def _is_transient_error(exc: BaseException) -> bool:
    """Kite transient errors: NetworkException, 429, 5xx server errors."""
    name = type(exc).__name__
    if name in ("NetworkException", "DataException", "GeneralException"):
        return True
    msg = str(exc).lower()
    transient_markers = (
        "timeout", "timed out", "connection reset", "temporarily unavailable",
        "429", "502", "503", "504", "rate limit", "too many requests",
    )
    return any(m in msg for m in transient_markers)


def retry_with_backoff(
    max_attempts: int = 3,
    initial_delay: float = 0.5,
    max_delay: float = 10.0,
    jitter: bool = True,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Decorator: retry transient exceptions with exponential backoff."""

    def decorator(fn: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            delay = initial_delay
            last_exc: BaseException | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except BaseException as exc:
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

_TAG_ALPHABET = string.ascii_uppercase + string.digits


def new_order_tag(prefix: str = "KA") -> str:
    """Generate a unique order tag within Kite's 20-char alphanumeric limit.

    Format: {prefix}{13-char random base36-ish}.  PREFIX=2 + body=13 = 15 chars.
    Room is left for potential future suffixes.
    """
    body = "".join(random.choices(_TAG_ALPHABET, k=13))
    tag = f"{prefix}{body}"
    assert len(tag) <= 20 and tag.isalnum(), f"bad tag: {tag}"
    return tag


def find_order_by_tag(client: Any, tag: str) -> dict | None:
    """Search the day's orderbook for an order with the given tag.

    Used for idempotent retry: if place_order() times out we don't know
    whether it actually reached the OMS. Instead of blindly retrying
    (which could double-fill), we search the orderbook first.
    """
    try:
        orders = client.orders() or []
    except Exception as exc:
        log.error("orderbook lookup failed: %s", exc)
        return None
    for o in orders:
        if o.get("tag") == tag:
            return o
    return None


class IdempotentOrderPlacer:
    """Wrap client.place_order() with tag-based idempotency + retry.

    The tag is always round-tripped back in the orderbook, so:
        1. Generate a tag if one isn't provided.
        2. Try place_order().
        3. On transient failure: search the orderbook by tag.
           - If the order is there: return its order_id (already placed).
           - If not: safe to retry.
        4. Hard failures (InputException, MarginException, HoldingException)
           never retry — those are user errors, not network blips.
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
        last_exc: BaseException | None = None

        for attempt in range(1, max_attempts + 1):
            self.rate_limiter.wait_order()
            try:
                order_id = self.client.place_order(**payload)
                log.info("order placed: id=%s tag=%s attempt=%d", order_id, tag, attempt)
                return order_id
            except BaseException as exc:
                last_exc = exc
                exc_name = type(exc).__name__

                # Hard errors: never retry — surface immediately.
                # Kite SDK exceptions: InputException (bad params), OrderException
                # (margin/holding/OMS reject), TokenException (session expired),
                # PermissionException (insufficient privileges).
                if exc_name in ("InputException", "OrderException", "TokenException", "PermissionException"):
                    log.error("order rejected (%s): %s", exc_name, exc)
                    raise

                # Transient: check orderbook first before retrying.
                if not _is_transient_error(exc):
                    raise

                log.warning(
                    "transient error on place_order attempt %d/%d: %s",
                    attempt, max_attempts, exc,
                )

                # Check if the order was actually placed despite the error.
                time.sleep(1.0)  # give OMS a moment to settle
                found = find_order_by_tag(self.client, tag)
                if found is not None:
                    oid = found.get("order_id", "")
                    log.info(
                        "order found by tag after transient error — tag=%s id=%s status=%s",
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
