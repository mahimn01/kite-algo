"""Tests for historical-data auto-chunking.

Kite limits each `/instruments/historical/` call by interval:
  minute=60d, 3m/5m/10m=100d, 15m/30m=200d, 60m=400d, day=2000d.

Our `_fetch_historical_chunked` transparently splits large windows into
per-interval-cap-sized chunks. Tests verify:
  - Small windows (one chunk) still work.
  - Large windows get split correctly.
  - Boundary bars are not duplicated (windows are +1s apart).
  - Unknown intervals fall through to a single call.
  - Chunks are merged in order.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import Mock

import pytest

from kite_algo.kite_tool import (
    HISTORICAL_MAX_LOOKBACK_DAYS,
    _fetch_historical_chunked,
)


def _make_bar(d: datetime, price: float = 100.0) -> dict:
    return {"date": d, "open": price, "high": price, "low": price, "close": price, "volume": 1}


class TestFetchHistoricalChunked:
    def test_single_chunk_within_cap(self) -> None:
        """30 days of minute bars → one call (cap is 60)."""
        client = Mock()
        bar = _make_bar(datetime(2026, 4, 1))
        client.historical_data.return_value = [bar]

        result = _fetch_historical_chunked(
            client,
            token=123,
            from_d=datetime(2026, 4, 1),
            to_d=datetime(2026, 4, 30),
            interval="minute",
            continuous=False, oi=False,
        )

        assert client.historical_data.call_count == 1
        assert len(result) == 1

    def test_multi_chunk_minute(self) -> None:
        """180 days of minute bars → 3 chunks of 60 days each."""
        client = Mock()
        # Each call returns one bar, distinct token-per-call.
        call_count = {"n": 0}
        def sfx(**kwargs):
            call_count["n"] += 1
            return [_make_bar(kwargs["from_date"], float(call_count["n"]))]
        client.historical_data.side_effect = sfx

        from_d = datetime(2026, 1, 1)
        to_d = from_d + timedelta(days=180)
        result = _fetch_historical_chunked(
            client,
            token=1, from_d=from_d, to_d=to_d,
            interval="minute", continuous=False, oi=False,
        )

        assert client.historical_data.call_count == 3
        assert len(result) == 3
        # Bars come back in order
        assert [b["open"] for b in result] == [1.0, 2.0, 3.0]

    def test_day_interval_large_range(self) -> None:
        """Daily bars with a 1-year range fit in a single chunk (cap is 2000 days)."""
        client = Mock()
        client.historical_data.return_value = [_make_bar(datetime(2025, 1, 1))]

        _fetch_historical_chunked(
            client,
            token=1,
            from_d=datetime(2025, 1, 1),
            to_d=datetime(2026, 1, 1),
            interval="day", continuous=False, oi=False,
        )
        assert client.historical_data.call_count == 1

    def test_unknown_interval_single_call(self) -> None:
        """Unknown intervals → pass through (let Kite decide)."""
        client = Mock()
        client.historical_data.return_value = []
        _fetch_historical_chunked(
            client, token=1,
            from_d=datetime(2026, 1, 1), to_d=datetime(2026, 2, 1),
            interval="week", continuous=False, oi=False,
        )
        assert client.historical_data.call_count == 1

    def test_chunks_are_non_overlapping(self) -> None:
        """Consecutive chunk windows are at least 1s apart — no duplicate bar
        at the boundary.
        """
        client = Mock()
        seen_windows: list[tuple[datetime, datetime]] = []
        def sfx(**kwargs):
            seen_windows.append((kwargs["from_date"], kwargs["to_date"]))
            return []
        client.historical_data.side_effect = sfx

        _fetch_historical_chunked(
            client,
            token=1,
            from_d=datetime(2026, 1, 1),
            to_d=datetime(2026, 7, 1),  # ~180 days
            interval="minute", continuous=False, oi=False,
        )

        # Each chunk's from_date >= previous to_date + 1s.
        for (a_from, a_to), (b_from, b_to) in zip(seen_windows, seen_windows[1:]):
            assert b_from >= a_to + timedelta(seconds=1), (
                f"overlapping windows: prev=({a_from}, {a_to}), next=({b_from}, {b_to})"
            )

    def test_exact_chunk_boundary(self) -> None:
        """from_d + exactly max_days interval → one chunk, not two empty ones."""
        client = Mock()
        client.historical_data.return_value = []
        max_days = HISTORICAL_MAX_LOOKBACK_DAYS["minute"]
        _fetch_historical_chunked(
            client, token=1,
            from_d=datetime(2026, 1, 1),
            to_d=datetime(2026, 1, 1) + timedelta(days=max_days),
            interval="minute", continuous=False, oi=False,
        )
        assert client.historical_data.call_count == 1

    def test_flags_propagated(self) -> None:
        """continuous and oi flags reach every chunk."""
        client = Mock()
        client.historical_data.return_value = []
        _fetch_historical_chunked(
            client, token=1,
            from_d=datetime(2026, 1, 1),
            to_d=datetime(2026, 4, 1),  # 90 days
            interval="minute", continuous=True, oi=True,
        )
        for call in client.historical_data.call_args_list:
            assert call.kwargs["continuous"] is True
            assert call.kwargs["oi"] is True


class TestHistoricalLookbackTable:
    def test_day_is_largest(self) -> None:
        assert HISTORICAL_MAX_LOOKBACK_DAYS["day"] == 2000

    def test_minute_is_smallest_of_sub_day(self) -> None:
        assert HISTORICAL_MAX_LOOKBACK_DAYS["minute"] == 60

    def test_all_intervals_present(self) -> None:
        """Every interval the CLI exposes is in the table."""
        supported = {"minute", "3minute", "5minute", "10minute", "15minute",
                     "30minute", "60minute", "day"}
        assert supported == set(HISTORICAL_MAX_LOOKBACK_DAYS)
