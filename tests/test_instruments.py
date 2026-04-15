"""Tests for InstrumentSpec."""

from __future__ import annotations

import pytest

from kite_algo.instruments import InstrumentSpec, validate_instrument


def test_instrument_key():
    spec = InstrumentSpec(symbol="RELIANCE", exchange="NSE")
    assert spec.kite_key == "NSE:RELIANCE"


def test_from_kite_key():
    spec = InstrumentSpec.from_kite_key("NSE:RELIANCE")
    assert spec.symbol == "RELIANCE"
    assert spec.exchange == "NSE"


def test_from_kite_key_bad():
    with pytest.raises(ValueError):
        InstrumentSpec.from_kite_key("RELIANCE")


def test_validate_requires_symbol():
    with pytest.raises(ValueError):
        validate_instrument(InstrumentSpec(symbol="", exchange="NSE"))


def test_validate_rejects_bad_exchange():
    with pytest.raises(ValueError):
        validate_instrument(InstrumentSpec(symbol="X", exchange="LSE"))  # type: ignore[arg-type]


def test_option_requires_expiry_and_strike():
    with pytest.raises(ValueError):
        validate_instrument(InstrumentSpec(symbol="NIFTY", exchange="NFO", segment="CE"))

    with pytest.raises(ValueError):
        validate_instrument(
            InstrumentSpec(symbol="NIFTY", exchange="NFO", segment="CE", expiry="2026-05-29")
        )


def test_with_token():
    spec = InstrumentSpec(symbol="RELIANCE", exchange="NSE")
    new = spec.with_token(12345)
    assert new.instrument_token == 12345
    assert new.symbol == spec.symbol
