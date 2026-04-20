"""Tests for auto-batched quote calls.

Kite's `/quote`, `/ohlc`, `/ltp` endpoints cap at 500 symbols per call.
`_batched_quote_call` transparently splits and merges so agents don't have
to know the limit.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from kite_algo.kite_tool import QUOTE_BATCH_SIZE, _batched_quote_call


class TestBatchedQuoteCall:
    def test_batch_size_is_500(self) -> None:
        assert QUOTE_BATCH_SIZE == 500

    def test_single_batch_under_cap(self) -> None:
        client = Mock()
        client.quote.return_value = {"NSE:RELIANCE": {"last_price": 1340}}
        out = _batched_quote_call(client, "quote", ["NSE:RELIANCE"])
        assert out == {"NSE:RELIANCE": {"last_price": 1340}}
        assert client.quote.call_count == 1

    def test_empty_symbols_no_call(self) -> None:
        client = Mock()
        out = _batched_quote_call(client, "quote", [])
        assert out == {}
        client.quote.assert_not_called()

    def test_splits_above_cap(self) -> None:
        """501 symbols → 2 calls (500 + 1)."""
        client = Mock()
        # Return distinct data per batch so merge isn't a no-op.
        call_index = {"n": 0}
        def sfx(symbols):
            call_index["n"] += 1
            return {s: {"last_price": float(call_index["n"])} for s in symbols}
        client.quote.side_effect = sfx

        syms = [f"NSE:SYM{i}" for i in range(501)]
        out = _batched_quote_call(client, "quote", syms)
        assert client.quote.call_count == 2
        assert len(out) == 501

    def test_deduplicates_inputs(self) -> None:
        """Duplicates aren't sent twice; second occurrence skipped."""
        client = Mock()
        client.quote.return_value = {}
        _batched_quote_call(client, "quote", ["A", "B", "A", "C", "B"])
        args, _ = client.quote.call_args
        assert args[0] == ["A", "B", "C"]

    def test_preserves_input_order(self) -> None:
        """Output dict iteration order reflects the first-seen order."""
        client = Mock()
        def sfx(symbols):
            return {s: {"v": i} for i, s in enumerate(symbols)}
        client.quote.side_effect = sfx

        out = _batched_quote_call(client, "quote", ["Z", "A", "M"])
        assert list(out.keys()) == ["Z", "A", "M"]

    def test_different_method_ltp(self) -> None:
        client = Mock()
        client.ltp.return_value = {"NSE:X": {"last_price": 100}}
        out = _batched_quote_call(client, "ltp", ["NSE:X"])
        assert out == {"NSE:X": {"last_price": 100}}
        assert client.ltp.call_count == 1
        client.quote.assert_not_called()

    def test_exact_batch_boundary(self) -> None:
        """Exactly 500 symbols → 1 call, not 2 empty ones."""
        client = Mock()
        client.quote.return_value = {}
        syms = [f"S{i}" for i in range(500)]
        _batched_quote_call(client, "quote", syms)
        assert client.quote.call_count == 1

    def test_1000_symbols_two_batches(self) -> None:
        client = Mock()
        client.quote.return_value = {}
        syms = [f"S{i}" for i in range(1000)]
        _batched_quote_call(client, "quote", syms)
        assert client.quote.call_count == 2

    def test_none_response_handled(self) -> None:
        """Kite returning None (rare) shouldn't crash; just skip merge."""
        client = Mock()
        client.quote.return_value = None
        out = _batched_quote_call(client, "quote", ["A"])
        assert out == {}

    def test_custom_batch_size(self) -> None:
        client = Mock()
        client.quote.return_value = {}
        syms = [f"S{i}" for i in range(10)]
        _batched_quote_call(client, "quote", syms, batch_size=3)
        # 10 items / batch 3 = 4 calls (3+3+3+1)
        assert client.quote.call_count == 4
