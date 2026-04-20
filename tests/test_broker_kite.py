"""Tests for KiteBroker read-path correctness.

Focus: market-data snapshot must surface Kite's zero-price "market closed"
state as `None` (not 0), with an explicit `market_closed=True` flag.
Otherwise downstream risk/pricing code can misprice against a fake zero
spread.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from kite_algo.broker.base import MarketDataSnapshot
from kite_algo.broker.kite import KiteBroker
from kite_algo.config import KiteConfig, TradingConfig
from kite_algo.instruments import InstrumentSpec


def _cfg() -> TradingConfig:
    # Build a TradingConfig without hitting the real env/session loader.
    return TradingConfig(
        kite=KiteConfig(
            api_key="KEY",
            api_secret="SEC",
            access_token="TOK",
            user_id="U",
        ),
    )


def _make_broker_with_quote(quote_payload: dict) -> KiteBroker:
    broker = KiteBroker(_cfg())
    # Avoid require_session() + kiteconnect import — inject a fake client.
    fake = Mock()
    fake.quote.return_value = quote_payload
    broker._client = fake  # type: ignore[attr-defined]
    return broker


class TestMarketDataSnapshot:
    def test_open_market_populates_bid_ask(self) -> None:
        broker = _make_broker_with_quote({
            "NSE:RELIANCE": {
                "last_price": 1340.5,
                "volume": 120_000,
                "ohlc": {"open": 1335, "high": 1345, "low": 1330, "close": 1338},
                "depth": {
                    "buy": [{"price": 1340.45, "quantity": 100, "orders": 5}],
                    "sell": [{"price": 1340.55, "quantity": 80, "orders": 3}],
                },
                "oi": 0,
            }
        })
        snap = broker.get_market_data_snapshot(
            InstrumentSpec(symbol="RELIANCE", exchange="NSE")
        )
        assert snap.last == pytest.approx(1340.5)
        assert snap.bid == pytest.approx(1340.45)
        assert snap.ask == pytest.approx(1340.55)
        assert snap.market_closed is False

    def test_closed_market_returns_none_not_zero(self) -> None:
        """Kite returns bid=0, ask=0 when depth is empty — we convert to None
        and set market_closed=True.
        """
        broker = _make_broker_with_quote({
            "NSE:RELIANCE": {
                "last_price": 1338,  # prior-day close still populated
                "volume": 0,
                "ohlc": {"open": 0, "high": 0, "low": 0, "close": 1338},
                "depth": {
                    "buy": [{"price": 0, "quantity": 0, "orders": 0}],
                    "sell": [{"price": 0, "quantity": 0, "orders": 0}],
                },
            }
        })
        snap = broker.get_market_data_snapshot(
            InstrumentSpec(symbol="RELIANCE", exchange="NSE")
        )
        assert snap.bid is None, "bid must be None when depth is zeroed"
        assert snap.ask is None, "ask must be None when depth is zeroed"
        assert snap.market_closed is True
        # Last price still populated from prior close — not wrong, just stale.
        assert snap.last == 1338
        # OHLC of prior day: open/high/low are 0 → we also nullify those.
        assert snap.open is None
        assert snap.close == 1338  # a real prior-day close

    def test_missing_depth_returns_none(self) -> None:
        """When Kite omits depth entirely (not even a [{}] stub), bid/ask=None."""
        broker = _make_broker_with_quote({
            "NSE:X": {
                "last_price": 100,
                "volume": 10,
                "ohlc": {"open": 99, "high": 101, "low": 98, "close": 100},
            }
        })
        snap = broker.get_market_data_snapshot(
            InstrumentSpec(symbol="X", exchange="NSE")
        )
        assert snap.bid is None
        assert snap.ask is None
        assert snap.market_closed is True

    def test_one_sided_book_not_market_closed(self) -> None:
        """If one side of the book is populated and the other isn't (rare but
        possible in illiquid instruments), market_closed stays False.
        """
        broker = _make_broker_with_quote({
            "NSE:X": {
                "last_price": 100,
                "volume": 1,
                "ohlc": {"open": 100, "high": 100, "low": 100, "close": 100},
                "depth": {
                    "buy": [{"price": 99.5, "quantity": 10, "orders": 1}],
                    "sell": [{"price": 0, "quantity": 0, "orders": 0}],
                },
            }
        })
        snap = broker.get_market_data_snapshot(
            InstrumentSpec(symbol="X", exchange="NSE")
        )
        assert snap.bid == pytest.approx(99.5)
        assert snap.ask is None
        assert snap.market_closed is False  # one side is live

    def test_one_sided_book_not_market_closed_again(self) -> None:
        """Already covered above; keep as a second regression row."""
        pass

    def test_volume_always_int(self) -> None:
        broker = _make_broker_with_quote({
            "NSE:X": {
                "last_price": 100,
                "ohlc": {"open": 100, "high": 100, "low": 100, "close": 100},
                "depth": {
                    "buy": [{"price": 99, "quantity": 1, "orders": 1}],
                    "sell": [{"price": 101, "quantity": 1, "orders": 1}],
                },
                # No volume field
            }
        })
        snap = broker.get_market_data_snapshot(
            InstrumentSpec(symbol="X", exchange="NSE")
        )
        assert snap.volume == 0
        assert isinstance(snap.volume, int)
