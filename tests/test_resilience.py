"""Rate limiter, retry, and idempotent order placer."""

from __future__ import annotations

import threading
import time
from unittest.mock import Mock

import pytest

from kite_algo.resilience import (
    IdempotentOrderPlacer,
    KiteRateLimiter,
    RateLimitedKiteClient,
    SlidingWindowLimiter,
    TokenBucket,
    _is_transient_error,
    find_order_by_tag,
    new_order_tag,
    retry_with_backoff,
)


# ---------------------------------------------------------------------------
# TokenBucket
# ---------------------------------------------------------------------------

class TestTokenBucket:
    def test_initial_capacity(self) -> None:
        b = TokenBucket(rate_per_sec=10.0)
        # Should be able to acquire up to capacity without waiting
        t0 = time.monotonic()
        for _ in range(10):
            assert b.acquire(block=False)
        assert time.monotonic() - t0 < 0.05

    def test_blocks_when_empty(self) -> None:
        b = TokenBucket(rate_per_sec=20.0, capacity=2)
        b.acquire()
        b.acquire()
        # Third acquire should block ~50ms (1/20s = 0.05)
        t0 = time.monotonic()
        b.acquire()
        elapsed = time.monotonic() - t0
        assert 0.03 < elapsed < 0.15

    def test_refills_over_time(self) -> None:
        b = TokenBucket(rate_per_sec=50.0, capacity=1)
        b.acquire()
        assert not b.acquire(block=False)  # empty
        time.sleep(0.05)
        assert b.acquire(block=False)  # refilled


# ---------------------------------------------------------------------------
# SlidingWindowLimiter
# ---------------------------------------------------------------------------

class TestSlidingWindowLimiter:
    def test_allows_up_to_max(self) -> None:
        lim = SlidingWindowLimiter(max_requests=5, window_seconds=1.0)
        for _ in range(5):
            assert lim.acquire(block=False)

    def test_blocks_beyond_max(self) -> None:
        lim = SlidingWindowLimiter(max_requests=3, window_seconds=1.0)
        for _ in range(3):
            lim.acquire(block=False)
        assert not lim.acquire(block=False)

    def test_slides_after_window(self) -> None:
        lim = SlidingWindowLimiter(max_requests=2, window_seconds=0.1)
        lim.acquire()
        lim.acquire()
        assert not lim.acquire(block=False)
        time.sleep(0.12)
        assert lim.acquire(block=False)


# ---------------------------------------------------------------------------
# KiteRateLimiter — smoke test
# ---------------------------------------------------------------------------

class TestKiteRateLimiter:
    def test_three_buckets_exist(self) -> None:
        lim = KiteRateLimiter()
        assert lim.general.rate == 10.0
        assert lim.historical.rate == 3.0
        assert lim.orders_sec.rate == 10.0
        assert lim.orders_min.max == 200

    def test_wait_methods_dont_raise(self) -> None:
        lim = KiteRateLimiter()
        lim.wait_general()
        lim.wait_historical()
        lim.wait_order()


# ---------------------------------------------------------------------------
# Transient error detection
# ---------------------------------------------------------------------------

class TestTransientErrorDetection:
    def test_network_exception_transient(self) -> None:
        exc = type("NetworkException", (Exception,), {})("timeout")
        assert _is_transient_error(exc)

    def test_data_exception_transient(self) -> None:
        exc = type("DataException", (Exception,), {})("parse error")
        assert _is_transient_error(exc)

    def test_rate_limit_message_transient(self) -> None:
        assert _is_transient_error(Exception("429 too many requests"))
        assert _is_transient_error(Exception("rate limit exceeded"))

    def test_5xx_transient(self) -> None:
        assert _is_transient_error(Exception("502 bad gateway"))
        assert _is_transient_error(Exception("503 service unavailable"))
        assert _is_transient_error(Exception("504 timeout"))

    def test_input_exception_not_transient(self) -> None:
        exc = type("InputException", (Exception,), {})("bad quantity")
        assert not _is_transient_error(exc)

    def test_token_exception_not_transient(self) -> None:
        exc = type("TokenException", (Exception,), {})("session expired")
        assert not _is_transient_error(exc)


# ---------------------------------------------------------------------------
# Retry decorator
# ---------------------------------------------------------------------------

class TestRetryWithBackoff:
    def test_success_no_retry(self) -> None:
        calls = []

        @retry_with_backoff(max_attempts=3, initial_delay=0.01)
        def fn() -> str:
            calls.append(1)
            return "ok"

        assert fn() == "ok"
        assert len(calls) == 1

    def test_retries_on_transient(self) -> None:
        calls = []

        @retry_with_backoff(max_attempts=3, initial_delay=0.01, jitter=False)
        def fn() -> str:
            calls.append(1)
            if len(calls) < 3:
                raise Exception("503 try again")
            return "ok"

        assert fn() == "ok"
        assert len(calls) == 3

    def test_no_retry_on_permanent(self) -> None:
        calls = []
        InputException = type("InputException", (Exception,), {})

        @retry_with_backoff(max_attempts=3, initial_delay=0.01)
        def fn() -> str:
            calls.append(1)
            raise InputException("bad input")

        with pytest.raises(Exception):
            fn()
        assert len(calls) == 1

    def test_gives_up_after_max(self) -> None:
        calls = []

        @retry_with_backoff(max_attempts=3, initial_delay=0.01, jitter=False)
        def fn() -> str:
            calls.append(1)
            raise Exception("503")

        with pytest.raises(Exception):
            fn()
        assert len(calls) == 3


# ---------------------------------------------------------------------------
# Idempotent tag generation + orderbook search
# ---------------------------------------------------------------------------

class TestOrderTag:
    def test_tag_format(self) -> None:
        tag = new_order_tag()
        assert len(tag) <= 20
        assert tag.isalnum()
        assert tag.startswith("KA")

    def test_custom_prefix(self) -> None:
        tag = new_order_tag(prefix="T1")
        assert tag.startswith("T1")

    def test_uniqueness(self) -> None:
        tags = {new_order_tag() for _ in range(500)}
        assert len(tags) == 500


class TestFindOrderByTag:
    def test_finds_matching_tag(self) -> None:
        client = Mock()
        client.orders.return_value = [
            {"order_id": "ORD1", "tag": "OTHER"},
            {"order_id": "ORD2", "tag": "KAABC123"},
            {"order_id": "ORD3", "tag": "MORE"},
        ]
        found = find_order_by_tag(client, "KAABC123")
        assert found is not None
        assert found["order_id"] == "ORD2"

    def test_returns_none_when_absent(self) -> None:
        client = Mock()
        client.orders.return_value = [{"order_id": "O1", "tag": "DIFF"}]
        assert find_order_by_tag(client, "MISSING") is None

    def test_handles_orderbook_failure(self) -> None:
        client = Mock()
        client.orders.side_effect = Exception("API down")
        assert find_order_by_tag(client, "ANY") is None


# ---------------------------------------------------------------------------
# IdempotentOrderPlacer
# ---------------------------------------------------------------------------

class TestIdempotentOrderPlacer:
    def _base_args(self):
        return {
            "variety": "regular",
            "exchange": "NSE",
            "tradingsymbol": "RELIANCE",
            "transaction_type": "BUY",
            "quantity": 1,
            "product": "CNC",
            "order_type": "LIMIT",
            "price": 1340.0,
        }

    def test_happy_path_places_once(self) -> None:
        client = Mock()
        client.place_order.return_value = "ORD_123"
        placer = IdempotentOrderPlacer(client)

        order_id = placer.place(**self._base_args())

        assert order_id == "ORD_123"
        assert client.place_order.call_count == 1

    def test_auto_generates_tag(self) -> None:
        client = Mock()
        client.place_order.return_value = "ORD_1"
        placer = IdempotentOrderPlacer(client)

        placer.place(**self._base_args())

        _, kwargs = client.place_order.call_args
        assert "tag" in kwargs
        assert kwargs["tag"].startswith("KA")

    def test_respects_custom_tag(self) -> None:
        client = Mock()
        client.place_order.return_value = "ORD_1"
        placer = IdempotentOrderPlacer(client)

        placer.place(**self._base_args(), tag="MY_TAG")

        _, kwargs = client.place_order.call_args
        assert kwargs["tag"] == "MY_TAG"

    def test_no_retry_on_input_exception(self) -> None:
        client = Mock()
        InputException = type("InputException", (Exception,), {})
        client.place_order.side_effect = InputException("bad quantity")
        placer = IdempotentOrderPlacer(client)

        with pytest.raises(Exception):
            placer.place(**self._base_args())
        assert client.place_order.call_count == 1
        client.orders.assert_not_called()  # No orderbook check on hard errors

    def test_no_retry_on_order_exception(self) -> None:
        client = Mock()
        OrderException = type("OrderException", (Exception,), {})
        client.place_order.side_effect = OrderException("insufficient margin")
        placer = IdempotentOrderPlacer(client)

        with pytest.raises(Exception):
            placer.place(**self._base_args())
        assert client.place_order.call_count == 1

    def test_transient_error_checks_orderbook_first(self) -> None:
        """Simulates: place_order times out, but order actually reached OMS."""
        client = Mock()
        NetworkException = type("NetworkException", (Exception,), {})
        client.place_order.side_effect = NetworkException("timeout")

        # Orderbook has our order (by tag) — so we return its ID, don't retry.
        client.orders.return_value = [
            {"order_id": "ORD_FOUND", "tag": "PLACEHOLDER_WILL_REPLACE", "status": "OPEN"},
        ]
        placer = IdempotentOrderPlacer(client)

        # Patch the orderbook return to use the auto-generated tag after it's created.
        # We do this by intercepting the call.
        real_place = client.place_order
        observed_tag: list[str] = []

        def capture_then_fail(*args, **kwargs):
            observed_tag.append(kwargs["tag"])
            # Also update the orderbook mock to match the captured tag
            client.orders.return_value = [
                {"order_id": "ORD_FOUND", "tag": kwargs["tag"], "status": "OPEN"},
            ]
            raise NetworkException("timeout")

        client.place_order.side_effect = capture_then_fail

        order_id = placer.place(**self._base_args())
        assert order_id == "ORD_FOUND"  # Returned from orderbook, not retried

    def test_transient_error_retries_when_orderbook_empty(self) -> None:
        """Simulates: first attempt fails, orderbook empty, second attempt succeeds."""
        client = Mock()
        NetworkException = type("NetworkException", (Exception,), {})

        # First call: fail with transient. Second call: succeed.
        client.place_order.side_effect = [
            NetworkException("timeout"),
            "ORD_SUCCESS",
        ]
        # Orderbook empty — safe to retry.
        client.orders.return_value = []

        placer = IdempotentOrderPlacer(client)
        order_id = placer.place(**self._base_args())

        assert order_id == "ORD_SUCCESS"
        assert client.place_order.call_count == 2

    def test_orderbook_propagation_delay(self, monkeypatch) -> None:
        """Simulates: OMS lag — order isn't in orderbook immediately but
        appears after a few polls. The placer must poll patiently and NOT
        retry once it finds the order.
        """
        # Speed up the poll delays so the test doesn't take 7s.
        import kite_algo.resilience as R
        monkeypatch.setattr(R, "_ORDERBOOK_POLL_DELAYS", (0.01, 0.01, 0.01, 0.01, 0.01))

        client = Mock()
        NetworkException = type("NetworkException", (Exception,), {})

        captured_tag: list[str] = []

        def place_side_effect(*a, **kw):
            captured_tag.append(kw["tag"])
            raise NetworkException("timeout")

        # orders() returns empty for the first 3 calls, then has the order.
        call_count = {"n": 0}
        def orders_side_effect():
            call_count["n"] += 1
            if call_count["n"] < 3:
                return []
            return [{"order_id": "ORD_LATE", "tag": captured_tag[0], "status": "OPEN"}]

        client.place_order.side_effect = place_side_effect
        client.orders.side_effect = orders_side_effect

        placer = IdempotentOrderPlacer(client)
        order_id = placer.place(**self._base_args())

        assert order_id == "ORD_LATE"
        # Must NOT have retried place_order since we found it via polling
        assert client.place_order.call_count == 1
        # Must have polled orders() multiple times
        assert client.orders.call_count >= 3

    def test_general_exception_is_not_transient(self) -> None:
        """GeneralException must NOT trigger retry (reviewer finding #9)."""
        client = Mock()
        GeneralException = type("GeneralException", (Exception,), {})
        client.place_order.side_effect = GeneralException("account blocked")
        placer = IdempotentOrderPlacer(client)

        with pytest.raises(Exception):
            placer.place(**self._base_args())
        assert client.place_order.call_count == 1
        client.orders.assert_not_called()

    def test_keyboard_interrupt_propagates_immediately(self) -> None:
        """KeyboardInterrupt must not be treated as transient."""
        client = Mock()
        client.place_order.side_effect = KeyboardInterrupt()
        placer = IdempotentOrderPlacer(client)

        with pytest.raises(KeyboardInterrupt):
            placer.place(**self._base_args())
        assert client.place_order.call_count == 1


# ---------------------------------------------------------------------------
# TokenBucket thread-safety
# ---------------------------------------------------------------------------

class TestTokenBucketThreading:
    def test_aggregate_rate_under_contention(self) -> None:
        """10 threads hammering a 50/s bucket should NOT exceed the rate
        meaningfully over a wall-clock window.
        """
        b = TokenBucket(rate_per_sec=50.0, capacity=5)
        acquisitions: list[float] = []
        lock = threading.Lock()
        stop = threading.Event()

        def worker() -> None:
            while not stop.is_set():
                b.acquire()
                with lock:
                    acquisitions.append(time.monotonic())

        threads = [threading.Thread(target=worker, daemon=True) for _ in range(10)]
        t0 = time.monotonic()
        for t in threads:
            t.start()
        time.sleep(1.0)  # 1 second test window
        stop.set()
        for t in threads:
            t.join(timeout=2.0)
        elapsed = time.monotonic() - t0

        # Over 1s at 50/s we should see ~50 + capacity (5) ≈ 55 acquisitions.
        # Allow generous tolerance for CI variance.
        count = len(acquisitions)
        expected_max = int(elapsed * 50 + 10)
        assert count <= expected_max, f"rate limit violated: {count} in {elapsed:.2f}s (max {expected_max})"
        assert count >= 30, f"suspiciously low acquisitions: {count} (expected ~50)"

    def test_no_deadlock_on_notify(self) -> None:
        """If one thread notifies another, no deadlock."""
        b = TokenBucket(rate_per_sec=100.0, capacity=1)

        # Drain the bucket
        b.acquire()

        # Start a waiter
        got = threading.Event()
        def waiter() -> None:
            b.acquire()
            got.set()

        t = threading.Thread(target=waiter, daemon=True)
        t.start()

        # Wait up to 1s for the refill; at 100/s, refill takes 10ms.
        assert got.wait(timeout=1.0), "waiter did not progress — possible deadlock"


# ---------------------------------------------------------------------------
# RateLimitedKiteClient
# ---------------------------------------------------------------------------

class TestRateLimitedKiteClient:
    def test_delegates_methods(self) -> None:
        raw = Mock()
        raw.profile.return_value = {"user_id": "X"}
        rl = KiteRateLimiter()
        client = RateLimitedKiteClient(raw, rl)
        assert client.profile() == {"user_id": "X"}
        raw.profile.assert_called_once()

    def test_delegates_class_attributes(self) -> None:
        raw = Mock()
        raw.GTT_TYPE_SINGLE = "single"
        raw.GTT_TYPE_OCO = "two-leg"
        rl = KiteRateLimiter()
        client = RateLimitedKiteClient(raw, rl)
        assert client.GTT_TYPE_SINGLE == "single"
        assert client.GTT_TYPE_OCO == "two-leg"

    def test_order_methods_use_orders_bucket(self) -> None:
        """place_order should wait on the orders bucket before call."""
        raw = Mock()
        raw.place_order.return_value = "ORD_1"
        rl = KiteRateLimiter()

        bucket_hits = {"order": 0, "general": 0, "historical": 0}
        def track(name):
            orig = getattr(rl, name)
            def wrapper():
                bucket_hits[name.replace("wait_", "")] += 1
                return orig()
            return wrapper
        rl.wait_order = track("wait_order")
        rl.wait_general = track("wait_general")
        rl.wait_historical = track("wait_historical")

        client = RateLimitedKiteClient(raw, rl)
        client.place_order(variety="regular", tradingsymbol="RELIANCE")
        client.profile()
        # historical_data is the only historical-bucket method
        raw.historical_data.return_value = []
        client.historical_data(instrument_token=1, from_date=1, to_date=2, interval="day")

        # wait_order is called for place_order, wait_general for profile,
        # wait_historical for historical_data.
        assert bucket_hits["order"] == 1
        assert bucket_hits["historical"] == 1
        assert bucket_hits["general"] >= 1
