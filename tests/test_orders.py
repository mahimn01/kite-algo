"""Tests for order validation."""

from __future__ import annotations

import pytest

from kite_algo.broker.base import OrderRequest
from kite_algo.instruments import InstrumentSpec
from kite_algo.orders import validate_order_request


def _req(**kw) -> OrderRequest:
    return OrderRequest(
        instrument=InstrumentSpec(symbol="RELIANCE", exchange="NSE"),
        side=kw.pop("side", "BUY"),
        quantity=kw.pop("quantity", 1),
        order_type=kw.pop("order_type", "LIMIT"),
        limit_price=kw.pop("limit_price", 1250.0),
        **kw,
    )


def test_valid_limit_order():
    validate_order_request(_req())


def test_rejects_zero_qty():
    with pytest.raises(ValueError):
        validate_order_request(_req(quantity=0))


def test_limit_without_price_fails():
    with pytest.raises(ValueError):
        validate_order_request(_req(limit_price=None))


def test_sl_requires_trigger():
    with pytest.raises(ValueError):
        validate_order_request(_req(order_type="SL", trigger_price=None))


def test_sl_m_requires_trigger():
    with pytest.raises(ValueError):
        validate_order_request(_req(order_type="SL-M", trigger_price=None, limit_price=None))


def test_market_without_price_ok():
    validate_order_request(_req(order_type="MARKET", limit_price=None))
