"""Tests for MarketDataClient — TTL cache + throttle."""

from __future__ import annotations

import threading
import time
from unittest.mock import Mock

import pytest

from kite_algo.broker.base import MarketDataSnapshot
from kite_algo.instruments import InstrumentSpec
from kite_algo.market_data import MarketDataClient, MarketDataConfig


def _snap(spec: InstrumentSpec, last: float, *, closed: bool = False) -> MarketDataSnapshot:
    return MarketDataSnapshot(
        instrument=spec, last=last, bid=last - 0.5, ask=last + 0.5,
        volume=100, open=last, high=last, low=last, close=last,
        market_closed=closed,
    )


@pytest.fixture
def spec() -> InstrumentSpec:
    return InstrumentSpec(symbol="RELIANCE", exchange="NSE")


class TestTTLCache:
    def test_cache_hit_within_ttl(self, spec) -> None:
        broker = Mock()
        broker.get_market_data_snapshot.return_value = _snap(spec, 1340)
        client = MarketDataClient(broker, MarketDataConfig(ttl_seconds=1.0))

        a = client.get_snapshot(spec)
        b = client.get_snapshot(spec)
        assert a is b
        assert broker.get_market_data_snapshot.call_count == 1

    def test_cache_miss_after_ttl(self, spec) -> None:
        broker = Mock()
        broker.get_market_data_snapshot.return_value = _snap(spec, 1340)
        client = MarketDataClient(broker, MarketDataConfig(ttl_seconds=0.05))

        client.get_snapshot(spec)
        time.sleep(0.1)
        client.get_snapshot(spec)
        assert broker.get_market_data_snapshot.call_count == 2

    def test_force_fresh_bypasses_cache(self, spec) -> None:
        broker = Mock()
        broker.get_market_data_snapshot.return_value = _snap(spec, 1340)
        client = MarketDataClient(broker, MarketDataConfig(ttl_seconds=10.0))

        client.get_snapshot(spec)
        client.get_snapshot(spec, force_fresh=True)
        assert broker.get_market_data_snapshot.call_count == 2

    def test_different_instruments_cached_separately(self) -> None:
        broker = Mock()
        s1 = InstrumentSpec(symbol="A", exchange="NSE")
        s2 = InstrumentSpec(symbol="B", exchange="NSE")
        broker.get_market_data_snapshot.side_effect = [_snap(s1, 100), _snap(s2, 200)]
        client = MarketDataClient(broker, MarketDataConfig(ttl_seconds=10.0))

        client.get_snapshot(s1)
        client.get_snapshot(s2)
        assert broker.get_market_data_snapshot.call_count == 2


class TestInvalidate:
    def test_invalidate_one(self, spec) -> None:
        broker = Mock()
        broker.get_market_data_snapshot.return_value = _snap(spec, 1340)
        client = MarketDataClient(broker, MarketDataConfig(ttl_seconds=10.0))
        client.get_snapshot(spec)
        client.invalidate(spec)
        client.get_snapshot(spec)
        assert broker.get_market_data_snapshot.call_count == 2

    def test_invalidate_all(self, spec) -> None:
        broker = Mock()
        broker.get_market_data_snapshot.return_value = _snap(spec, 1340)
        client = MarketDataClient(broker, MarketDataConfig(ttl_seconds=10.0))
        client.get_snapshot(spec)
        client.invalidate()
        client.get_snapshot(spec)
        assert broker.get_market_data_snapshot.call_count == 2


class TestThrottle:
    def test_min_interval_delays(self, spec) -> None:
        broker = Mock()
        broker.get_market_data_snapshot.return_value = _snap(spec, 1340)
        client = MarketDataClient(
            broker, MarketDataConfig(ttl_seconds=0.0, min_interval_seconds=0.1),
        )
        t0 = time.monotonic()
        client.get_snapshot(spec)
        client.get_snapshot(spec)
        elapsed = time.monotonic() - t0
        assert elapsed >= 0.08, f"no throttle enforced (elapsed {elapsed:.3f}s)"


class TestValidate:
    def test_rejects_crossed_book(self, spec) -> None:
        broker = Mock()
        broker.get_market_data_snapshot.return_value = MarketDataSnapshot(
            instrument=spec, last=100, bid=110, ask=90,
            volume=0, open=100, high=100, low=100, close=100,
        )
        client = MarketDataClient(broker, MarketDataConfig(ttl_seconds=1.0))
        with pytest.raises(ValueError, match="crossed"):
            client.get_snapshot(spec)

    def test_allows_none_bid_ask(self, spec) -> None:
        """Market-closed snapshots (bid=None, ask=None) are legitimate."""
        broker = Mock()
        broker.get_market_data_snapshot.return_value = MarketDataSnapshot(
            instrument=spec, last=100, bid=None, ask=None,
            volume=0, open=None, high=None, low=None, close=100,
            market_closed=True,
        )
        client = MarketDataClient(broker)
        snap = client.get_snapshot(spec)
        assert snap.market_closed is True


class TestStats:
    def test_cache_stats(self, spec) -> None:
        broker = Mock()
        broker.get_market_data_snapshot.return_value = _snap(spec, 1340)
        client = MarketDataClient(
            broker, MarketDataConfig(ttl_seconds=1.5, min_interval_seconds=0.2),
        )
        client.get_snapshot(spec)
        stats = client.cache_stats()
        assert stats["entries"] == 1
        assert stats["ttl_seconds"] == 1.5
        assert stats["min_interval_seconds"] == 0.2


class TestConcurrency:
    def test_concurrent_cache_access(self, spec) -> None:
        broker = Mock()
        broker.get_market_data_snapshot.return_value = _snap(spec, 1340)
        client = MarketDataClient(broker, MarketDataConfig(ttl_seconds=10.0))

        def w() -> None:
            for _ in range(20):
                client.get_snapshot(spec)

        threads = [threading.Thread(target=w) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Cache hit rate makes actual broker calls far fewer than 200.
        assert broker.get_market_data_snapshot.call_count <= 20
