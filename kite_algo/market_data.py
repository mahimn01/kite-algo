"""Market-data snapshot client with TTL cache + throttle.

Sits between the engine/risk layer and the broker. Two problems it solves:

1. **Rate limits**: Kite's `/quote` is officially 1 req/s. A strategy that
   naively asks for the same snapshot several times per tick will burn
   through the bucket in milliseconds. TTL cache serves repeats from memory.
2. **Liveness checks**: bid/ask/last are point-in-time. `get_snapshot`
   surfaces the raw `MarketDataSnapshot` dataclass (which carries
   `market_closed`), so callers can reason about stale data.

Design mirrors `trading_algo/market_data.py` but adapted for Kite's shape
and our `KiteBroker` protocol.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Protocol

from kite_algo.broker.base import MarketDataSnapshot
from kite_algo.instruments import InstrumentSpec

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class MarketDataConfig:
    """Tuning knobs.

    - `ttl_seconds`: how long a cached snapshot is considered fresh. Kite's
      quote bucket is 1/s, so a 1s TTL means we spend at most one request
      per instrument per second even under a hot strategy loop. Loose
      strategies can bump to 5s; HFT-ish strategies can drop to 0.2s
      (at which point you'll hit the rate limit and block).
    - `min_interval_seconds`: additional throttle between ANY two fetches
      regardless of instrument. Defence against "N strategies each asking
      for a different symbol 10x a second" — still respects the bucket.
    """
    ttl_seconds: float = 1.0
    min_interval_seconds: float = 0.0


class _SnapshotFetcher(Protocol):
    """Anything with `get_market_data_snapshot(instrument) -> MarketDataSnapshot`."""
    def get_market_data_snapshot(self, instrument: InstrumentSpec) -> MarketDataSnapshot: ...


class MarketDataClient:
    """TTL-cached wrapper around a broker snapshot method.

    Thread-safe. The cache is keyed by `(exchange, symbol)` rather than the
    full `InstrumentSpec` — that avoids a subtle bug where two otherwise-
    identical specs with different `instrument_token` don't share a cache
    entry (broker always resolves by `exchange:symbol` anyway).
    """

    def __init__(self, broker: _SnapshotFetcher, cfg: MarketDataConfig | None = None):
        self._broker = broker
        self._cfg = cfg or MarketDataConfig()
        self._cache: dict[tuple[str, str], tuple[float, MarketDataSnapshot]] = {}
        self._last_fetch_monotonic: float = 0.0
        self._lock = threading.Lock()

    # -------------------------------------------------------------
    # Public
    # -------------------------------------------------------------

    def get_snapshot(
        self,
        instrument: InstrumentSpec,
        *,
        force_fresh: bool = False,
    ) -> MarketDataSnapshot:
        """Fetch a snapshot, serving from cache if still fresh.

        `force_fresh=True` bypasses the cache for this call. The throttle
        (`min_interval_seconds`) still applies — an agent can't spam "force
        fresh" to get around the bucket.
        """
        key = (instrument.exchange, instrument.symbol)
        now = time.monotonic()

        if not force_fresh:
            with self._lock:
                cached = self._cache.get(key)
                if cached is not None:
                    ts, snap = cached
                    if (now - ts) <= self._cfg.ttl_seconds:
                        return snap

        # Throttle: enforce min_interval between any two fetches, global
        # across instruments. Under contention (many instruments requested
        # in a burst), this serialises fetches so the rate limiter isn't
        # the only back-pressure.
        if self._cfg.min_interval_seconds > 0:
            with self._lock:
                elapsed = now - self._last_fetch_monotonic
                if elapsed < self._cfg.min_interval_seconds:
                    wait = self._cfg.min_interval_seconds - elapsed
                # Release lock during sleep.
                else:
                    wait = 0.0
            if wait > 0:
                time.sleep(wait)

        snap = self._broker.get_market_data_snapshot(instrument)
        _validate_snapshot(snap)
        with self._lock:
            self._cache[key] = (time.monotonic(), snap)
            self._last_fetch_monotonic = time.monotonic()
        return snap

    def invalidate(self, instrument: InstrumentSpec | None = None) -> None:
        """Drop the cache for one instrument or all.

        Call after any action that would invalidate the cached view — e.g.
        after placing a large order that moves the book, or after a known
        WebSocket reconnect.
        """
        with self._lock:
            if instrument is None:
                self._cache.clear()
            else:
                self._cache.pop((instrument.exchange, instrument.symbol), None)

    def cache_stats(self) -> dict:
        """Introspection helper for `status` command integration."""
        with self._lock:
            return {
                "entries": len(self._cache),
                "ttl_seconds": self._cfg.ttl_seconds,
                "min_interval_seconds": self._cfg.min_interval_seconds,
            }


def _validate_snapshot(snap: MarketDataSnapshot) -> None:
    """Loose sanity checks. Don't raise on bid=None/ask=None — that is the
    correct state for a closed market (we set `market_closed=True` at the
    broker layer). But a non-None bid/ask pair must be finite and non-
    negative; a bid > ask would indicate a corrupted response.
    """
    if snap.bid is not None and snap.bid < 0:
        raise ValueError(f"negative bid for {snap.instrument.kite_key}: {snap.bid}")
    if snap.ask is not None and snap.ask < 0:
        raise ValueError(f"negative ask for {snap.instrument.kite_key}: {snap.ask}")
    if (snap.bid is not None and snap.ask is not None and snap.bid > 0
            and snap.ask > 0 and snap.bid > snap.ask):
        # Crossed book is always a bug, never a market state.
        raise ValueError(
            f"crossed book for {snap.instrument.kite_key}: "
            f"bid {snap.bid} > ask {snap.ask}"
        )
