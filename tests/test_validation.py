"""Pre-flight order validation."""

from __future__ import annotations

import pytest

from kite_algo.validation import validate_order


def _base(**overrides):
    """Default valid order: 1 sh RELIANCE LIMIT BUY @ 1340 CNC."""
    params = dict(
        exchange="NSE",
        tradingsymbol="RELIANCE",
        transaction_type="BUY",
        order_type="LIMIT",
        quantity=1,
        product="CNC",
        price=1340.0,
    )
    params.update(overrides)
    return params


class TestHappyPath:
    def test_valid_equity_limit(self) -> None:
        assert validate_order(**_base()) == []

    def test_valid_equity_market(self) -> None:
        assert validate_order(**_base(order_type="MARKET", price=None)) == []

    def test_valid_sl_order(self) -> None:
        errs = validate_order(**_base(order_type="SL", price=1340.0, trigger_price=1335.0))
        assert errs == []

    def test_valid_sl_m_order(self) -> None:
        errs = validate_order(**_base(order_type="SL-M", price=None, trigger_price=1335.0))
        assert errs == []

    def test_valid_fno_nrml(self) -> None:
        errs = validate_order(**_base(
            exchange="NFO", tradingsymbol="NIFTY26APR24400CE",
            product="NRML", quantity=65,
        ))
        assert errs == []

    def test_valid_mtf(self) -> None:
        errs = validate_order(**_base(product="MTF"))
        assert errs == []

    def test_valid_iceberg(self) -> None:
        errs = validate_order(**_base(
            variety="iceberg", quantity=1000,
            iceberg_legs=5, iceberg_quantity=200,
        ))
        assert errs == []


class TestEnumValidation:
    def test_invalid_exchange(self) -> None:
        errs = validate_order(**_base(exchange="INVALID"))
        assert any(e.field == "exchange" for e in errs)

    def test_bcd_accepted(self) -> None:
        errs = validate_order(**_base(
            exchange="BCD", tradingsymbol="USDINR26APRFUT",
            product="NRML", quantity=1,
        ))
        assert errs == []

    def test_invalid_transaction_type(self) -> None:
        errs = validate_order(**_base(transaction_type="HOLD"))
        assert any(e.field == "transaction_type" for e in errs)

    def test_invalid_product(self) -> None:
        errs = validate_order(**_base(product="FOO"))
        assert any(e.field == "product" for e in errs)

    def test_auction_variety_accepted(self) -> None:
        errs = validate_order(**_base(variety="auction"))
        assert errs == []


class TestPriceTriggerRules:
    def test_limit_requires_price(self) -> None:
        errs = validate_order(**_base(price=None))
        assert any(e.field == "price" for e in errs)

    def test_market_rejects_price(self) -> None:
        errs = validate_order(**_base(order_type="MARKET", price=1340.0))
        assert any(e.field == "price" for e in errs)

    def test_market_rejects_trigger(self) -> None:
        errs = validate_order(**_base(
            order_type="MARKET", price=None, trigger_price=1335.0,
        ))
        assert any(e.field == "trigger_price" for e in errs)

    def test_sl_requires_trigger(self) -> None:
        errs = validate_order(**_base(
            order_type="SL", price=1340.0, trigger_price=None,
        ))
        assert any(e.field == "trigger_price" for e in errs)

    def test_sl_m_requires_trigger(self) -> None:
        errs = validate_order(**_base(
            order_type="SL-M", price=None, trigger_price=None,
        ))
        assert any(e.field == "trigger_price" for e in errs)

    def test_sl_requires_price(self) -> None:
        errs = validate_order(**_base(
            order_type="SL", price=None, trigger_price=1335.0,
        ))
        assert any(e.field == "price" and "SL" in e.message for e in errs)

    def test_limit_rejects_trigger(self) -> None:
        errs = validate_order(**_base(trigger_price=1335.0))
        assert any(e.field == "trigger_price" for e in errs)


class TestProductExchangeCompatibility:
    def test_cnc_on_nse_ok(self) -> None:
        assert validate_order(**_base(product="CNC", exchange="NSE")) == []

    def test_cnc_on_nfo_rejected(self) -> None:
        errs = validate_order(**_base(
            product="CNC", exchange="NFO", tradingsymbol="NIFTY26APR24400CE",
        ))
        assert any(e.field == "product" for e in errs)

    def test_nrml_on_equity_rejected(self) -> None:
        errs = validate_order(**_base(product="NRML", exchange="NSE"))
        assert any(e.field == "product" for e in errs)

    def test_mtf_on_nfo_rejected(self) -> None:
        errs = validate_order(**_base(
            product="MTF", exchange="NFO", tradingsymbol="NIFTY26APR24400CE",
        ))
        assert any(e.field == "product" for e in errs)


class TestValidityRules:
    def test_ttl_requires_minutes(self) -> None:
        errs = validate_order(**_base(validity="TTL", validity_ttl=None))
        assert any(e.field == "validity_ttl" for e in errs)

    def test_ttl_accepts_minutes(self) -> None:
        errs = validate_order(**_base(validity="TTL", validity_ttl=10))
        assert errs == []

    def test_day_rejects_ttl_minutes(self) -> None:
        errs = validate_order(**_base(validity="DAY", validity_ttl=10))
        assert any(e.field == "validity_ttl" for e in errs)


class TestIcebergRules:
    def test_iceberg_requires_legs(self) -> None:
        errs = validate_order(**_base(variety="iceberg", quantity=1000))
        assert any(e.field == "iceberg_legs" for e in errs)

    def test_iceberg_legs_bounds(self) -> None:
        errs = validate_order(**_base(
            variety="iceberg", quantity=1000,
            iceberg_legs=1, iceberg_quantity=1000,
        ))
        assert any(e.field == "iceberg_legs" for e in errs)

        errs2 = validate_order(**_base(
            variety="iceberg", quantity=5000,
            iceberg_legs=100, iceberg_quantity=50,
        ))
        assert any(e.field == "iceberg_legs" for e in errs2)

    def test_iceberg_legs_x_quantity_must_equal_total(self) -> None:
        errs = validate_order(**_base(
            variety="iceberg", quantity=1000,
            iceberg_legs=5, iceberg_quantity=100,  # 5×100=500 ≠ 1000
        ))
        assert any(e.field == "iceberg_quantity" for e in errs)

    def test_non_iceberg_rejects_iceberg_params(self) -> None:
        errs = validate_order(**_base(iceberg_legs=5))
        assert any("iceberg" in e.field for e in errs)


class TestDisclosedQuantity:
    def test_disclosed_within_quantity(self) -> None:
        errs = validate_order(**_base(quantity=100, disclosed_quantity=10))
        assert errs == []

    def test_disclosed_exceeds_quantity(self) -> None:
        errs = validate_order(**_base(quantity=100, disclosed_quantity=200))
        assert any(e.field == "disclosed_quantity" for e in errs)


class TestTagRules:
    def test_valid_tag(self) -> None:
        errs = validate_order(**_base(tag="STRATEGY_1"))
        assert errs == []

    def test_tag_too_long(self) -> None:
        errs = validate_order(**_base(tag="A" * 21))
        assert any(e.field == "tag" for e in errs)

    def test_tag_non_alphanumeric(self) -> None:
        errs = validate_order(**_base(tag="foo@bar"))
        assert any(e.field == "tag" for e in errs)


class TestQuantityRules:
    def test_zero_quantity_rejected(self) -> None:
        errs = validate_order(**_base(quantity=0))
        assert any(e.field == "quantity" for e in errs)

    def test_negative_quantity_rejected(self) -> None:
        errs = validate_order(**_base(quantity=-5))
        assert any(e.field == "quantity" for e in errs)
