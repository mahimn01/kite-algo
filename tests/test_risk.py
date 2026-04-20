"""Tests for RiskManager + RiskLimits + RiskViolation taxonomy."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import Mock

import pytest

from kite_algo.broker.base import AccountSnapshot, MarketDataSnapshot, Position
from kite_algo.instruments import InstrumentSpec
from kite_algo.market_rules import IST
from kite_algo.orders import TradeIntent
from kite_algo.risk import (
    RiskLimits,
    RiskManager,
    RiskViolation,
    _underlying_for,
    risk_limits_from_env,
)


def _spec(symbol="RELIANCE", exchange="NSE") -> InstrumentSpec:
    return InstrumentSpec(symbol=symbol, exchange=exchange)


def _account(*, net=1_000_000.0, used=0.0, avail=1_000_000.0) -> AccountSnapshot:
    return AccountSnapshot(
        user_id="U", net_liquidation=net, available_cash=avail,
        margin_used=used, margin_available=avail, currency="INR",
    )


def _snap(spec, last=1340.0) -> MarketDataSnapshot:
    return MarketDataSnapshot(
        instrument=spec, last=last, bid=last - 0.5, ask=last + 0.5,
        volume=100, open=last, high=last, low=last, close=last,
    )


def _make_broker(positions=None, account=None) -> Mock:
    b = Mock()
    b.get_positions.return_value = positions or []
    b.get_account_snapshot.return_value = account or _account()
    return b


def _intent(**overrides) -> TradeIntent:
    defaults = dict(
        instrument=_spec(),
        side="BUY", quantity=1, order_type="LIMIT",
        product="CNC", limit_price=1340.0,
    )
    defaults.update(overrides)
    return TradeIntent(**defaults)


@pytest.fixture
def rm() -> RiskManager:
    return RiskManager(RiskLimits(
        max_order_quantity=100,
        max_single_order_inr=500_000.0,
        max_abs_position_per_symbol=500,
        max_notional_exposure_inr=10_000_000.0,
        max_margin_utilization=0.8,
        max_daily_loss_inr=50_000.0,
        allow_short=False,
        respect_mis_cutoff=False,  # tests don't run at market hours
        respect_market_hours=False,
        respect_freeze_qty=True,
        respect_lot_size=True,
    ))


class TestOrderShape:
    def test_zero_quantity_rejected(self, rm) -> None:
        with pytest.raises(RiskViolation, match="QUANTITY_NOT_POSITIVE"):
            rm.validate(_intent(quantity=0), _make_broker(), _snap_fn())

    def test_negative_quantity_rejected(self, rm) -> None:
        with pytest.raises(RiskViolation):
            rm.validate(_intent(quantity=-1), _make_broker(), _snap_fn())

    def test_over_max_order_qty(self, rm) -> None:
        with pytest.raises(RiskViolation, match="ORDER_QTY_EXCEEDED"):
            rm.validate(_intent(quantity=1000), _make_broker(), _snap_fn())


class TestSingleOrderNotional:
    def test_large_notional_rejected(self, rm) -> None:
        with pytest.raises(RiskViolation, match="SINGLE_ORDER_NOTIONAL_EXCEEDED"):
            # 50 * 50_000 = 2.5M > 500k cap
            rm.validate(
                _intent(quantity=50, limit_price=50_000),
                _make_broker(), _snap_fn(50_000),
            )

    def test_within_cap_allowed(self, rm) -> None:
        # 10 * 1340 = 13400, well under 500k
        rm.validate(_intent(quantity=10), _make_broker(), _snap_fn())


class TestPositionCeiling:
    def test_resulting_over_ceiling_rejected(self, rm) -> None:
        pos = [Position(instrument=_spec(), product="CNC",
                        quantity=495, avg_price=1300, last_price=1340)]
        with pytest.raises(RiskViolation, match="POSITION_CEILING"):
            rm.validate(_intent(quantity=10), _make_broker(positions=pos),
                        _snap_fn())


class TestShortAllowed:
    def test_short_rejected_by_default(self, rm) -> None:
        with pytest.raises(RiskViolation, match="SHORT_NOT_ALLOWED"):
            rm.validate(_intent(side="SELL", quantity=1), _make_broker(),
                        _snap_fn())

    def test_short_allowed_with_flag(self) -> None:
        rm = RiskManager(RiskLimits(
            allow_short=True, max_order_quantity=10,
            max_single_order_inr=1e9, max_abs_position_per_symbol=10_000,
            respect_mis_cutoff=False, respect_market_hours=False,
            respect_freeze_qty=False, respect_lot_size=False,
        ))
        rm.validate(_intent(side="SELL", quantity=1), _make_broker(),
                    _snap_fn())


class TestExposure:
    def test_total_exposure_rejected(self) -> None:
        rm = RiskManager(RiskLimits(
            max_order_quantity=1_000_000,
            max_single_order_inr=1e12,
            max_abs_position_per_symbol=1_000_000,
            max_notional_exposure_inr=100_000.0,
            respect_mis_cutoff=False, respect_market_hours=False,
            respect_freeze_qty=False, respect_lot_size=False,
        ))
        acct = _account(used=50_000, avail=1e9)
        with pytest.raises(RiskViolation, match="NOTIONAL_EXPOSURE_EXCEEDED"):
            # used 50k + 75*1340=100.5k > 100k cap
            rm.validate(_intent(quantity=75), _make_broker(account=acct),
                        _snap_fn())


class TestMarginUtilization:
    def test_util_rejected(self) -> None:
        rm = RiskManager(RiskLimits(
            max_order_quantity=1_000_000,
            max_single_order_inr=1e12, max_notional_exposure_inr=1e12,
            max_abs_position_per_symbol=1_000_000,
            max_margin_utilization=0.5,
            respect_mis_cutoff=False, respect_market_hours=False,
            respect_freeze_qty=False, respect_lot_size=False,
        ))
        acct = _account(used=800_000, avail=200_000)  # 80% util
        with pytest.raises(RiskViolation, match="MARGIN_UTIL_EXCEEDED"):
            rm.validate(_intent(), _make_broker(account=acct), _snap_fn())


class TestDailyLoss:
    def test_circuit_breaker_fires(self) -> None:
        rm = RiskManager(RiskLimits(
            max_order_quantity=1_000_000,
            max_single_order_inr=1e12, max_notional_exposure_inr=1e12,
            max_abs_position_per_symbol=1_000_000,
            max_daily_loss_inr=10_000.0,
            respect_mis_cutoff=False, respect_market_hours=False,
            respect_freeze_qty=False, respect_lot_size=False,
        ))
        # First call establishes session-start NL.
        broker = _make_broker(account=_account(net=1_000_000))
        rm.validate(_intent(), broker, _snap_fn())
        # Now simulate a 20k drawdown.
        broker2 = _make_broker(account=_account(net=980_000))
        with pytest.raises(RiskViolation, match="DAILY_LOSS"):
            rm.validate(_intent(), broker2, _snap_fn())


class TestFreezeAndLot:
    def test_nifty_freeze_blocked_above_ceiling(self, rm) -> None:
        """NIFTY freeze × 10 = 18_000. 20_000 → blocked."""
        # Use a fresh RM with big single-order-INR cap so freeze-qty is
        # the thing that fires.
        rm = RiskManager(RiskLimits(
            max_order_quantity=1_000_000,
            max_single_order_inr=1e12,
            max_notional_exposure_inr=1e12,
            max_abs_position_per_symbol=1_000_000,
            respect_mis_cutoff=False, respect_market_hours=False,
            respect_freeze_qty=True, respect_lot_size=False,
        ))
        with pytest.raises(RiskViolation, match="FREEZE_QTY_EXCEEDED"):
            rm.validate(
                _intent(
                    instrument=_spec("NIFTY26APR24000CE", "NFO"),
                    product="NRML",
                    quantity=20_000, limit_price=50,
                ),
                _make_broker(), _snap_fn(),
            )

    def test_lot_size_mismatch_blocks(self) -> None:
        """NIFTY lot = 75. 100 is not a multiple → block."""
        rm = RiskManager(RiskLimits(
            max_order_quantity=1_000_000,
            max_single_order_inr=1e12,
            max_notional_exposure_inr=1e12,
            max_abs_position_per_symbol=1_000_000,
            respect_mis_cutoff=False, respect_market_hours=False,
            respect_freeze_qty=True, respect_lot_size=True,
        ))
        with pytest.raises(RiskViolation, match="LOT_SIZE_MISMATCH"):
            rm.validate(
                _intent(
                    instrument=_spec("NIFTY26APR24000CE", "NFO"),
                    product="NRML",
                    quantity=100, limit_price=50,  # not a multiple of 75
                ),
                _make_broker(), _snap_fn(),
            )

    def test_valid_lot_multiple_allowed(self) -> None:
        rm = RiskManager(RiskLimits(
            max_order_quantity=1_000_000,
            max_single_order_inr=1e12,
            max_notional_exposure_inr=1e12,
            max_abs_position_per_symbol=1_000_000,
            respect_mis_cutoff=False, respect_market_hours=False,
            respect_freeze_qty=True, respect_lot_size=True,
        ))
        rm.validate(
            _intent(
                instrument=_spec("NIFTY26APR24000CE", "NFO"),
                product="NRML",
                quantity=150, limit_price=50,  # 2 * 75
            ),
            _make_broker(), _snap_fn(),
        )


class TestStrategyCap:
    def test_cap_fires(self) -> None:
        rm = RiskManager(RiskLimits(
            max_order_quantity=1_000_000,
            max_single_order_inr=1e12,
            max_notional_exposure_inr=1e12,
            max_abs_position_per_symbol=1_000_000,
            strategy_notional_cap_inr=20_000.0,
            respect_mis_cutoff=False, respect_market_hours=False,
            respect_freeze_qty=False, respect_lot_size=False,
        ))
        rm.validate(_intent(quantity=10, strategy="s1"), _make_broker(), _snap_fn())
        # First 10 × 1340 = 13400; a second 10 would push to 26800 > 20000.
        with pytest.raises(RiskViolation, match="STRATEGY_CAP"):
            rm.validate(_intent(quantity=10, strategy="s1"),
                        _make_broker(), _snap_fn())


class TestPriceResolution:
    def test_limit_price_used_when_set(self, rm) -> None:
        """When limit_price is present, we don't need a snapshot at all."""
        snap_fn = Mock()  # would raise if called
        rm.validate(_intent(limit_price=1500), _make_broker(), snap_fn)
        snap_fn.assert_not_called()

    def test_snapshot_last_used_for_market_order(self, rm) -> None:
        rm.validate(
            _intent(order_type="MARKET", limit_price=None, product="MIS"),
            _make_broker(), _snap_fn(),
        )

    def test_no_priceable_snapshot_raises(self, rm) -> None:
        spec = _spec()
        def snap_fn(s):
            return MarketDataSnapshot(
                instrument=spec, last=None, bid=None, ask=None,
                volume=0, open=None, high=None, low=None, close=None,
                market_closed=True,
            )
        with pytest.raises(RiskViolation, match="NO_PRICEABLE_SNAPSHOT"):
            rm.validate(
                _intent(order_type="MARKET", limit_price=None, product="MIS"),
                _make_broker(), snap_fn,
            )


class TestEnvConfig:
    def test_env_overrides(self, monkeypatch) -> None:
        monkeypatch.setenv("KITE_RISK_MAX_ORDER_QTY", "42")
        monkeypatch.setenv("KITE_RISK_ALLOW_SHORT", "true")
        monkeypatch.setenv("KITE_STRATEGY_NOTIONAL_CAP_INR", "123456.7")
        lim = risk_limits_from_env()
        assert lim.max_order_quantity == 42
        assert lim.allow_short is True
        assert lim.strategy_notional_cap_inr == pytest.approx(123456.7)


class TestUnderlyingFor:
    def test_nfo_nifty(self) -> None:
        assert _underlying_for(_spec("NIFTY26APR24000CE", "NFO")) == "NIFTY"

    def test_bfo_sensex(self) -> None:
        assert _underlying_for(_spec("SENSEX26APR80000CE", "BFO")) == "SENSEX"

    def test_nfo_midcpnifty_not_confused_with_nifty(self) -> None:
        """Prefix 'MIDCPNIFTY' must match before 'NIFTY'."""
        assert _underlying_for(_spec("MIDCPNIFTY26APR11000CE", "NFO")) == "MIDCPNIFTY"

    def test_cash_equity_returns_none(self) -> None:
        assert _underlying_for(_spec("RELIANCE", "NSE")) is None

    def test_mcx_returns_none(self) -> None:
        assert _underlying_for(_spec("CRUDEOIL26APRFUT", "MCX")) is None


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _snap_fn(last: float = 1340.0):
    def f(spec):
        return _snap(spec, last)
    return f
