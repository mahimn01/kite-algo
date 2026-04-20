"""Tests for the Engine polling loop."""

from __future__ import annotations

from unittest.mock import MagicMock, Mock

import pytest

from kite_algo.broker.base import OrderResult
from kite_algo.broker.sim import SimBroker
from kite_algo.config import KiteConfig, TradingConfig
from kite_algo.engine import Engine, StrategyContext
from kite_algo.instruments import InstrumentSpec
from kite_algo.orders import TradeIntent
from kite_algo.persistence import SqliteStore
from kite_algo.risk import RiskLimits, RiskManager


def _cfg(*, dry_run: bool = True, db_path: str | None = None) -> TradingConfig:
    return TradingConfig(
        broker="sim", dry_run=dry_run,
        db_path=db_path or "",
        poll_seconds=0,
        kite=KiteConfig(api_key="K", api_secret="S", access_token="T", user_id="U"),
    )


def _intent(**overrides) -> TradeIntent:
    d = dict(
        instrument=InstrumentSpec(symbol="RELIANCE", exchange="NSE"),
        side="BUY", quantity=1, order_type="LIMIT",
        product="CNC", limit_price=100.0,
    )
    d.update(overrides)
    return TradeIntent(**d)


def _lax_risk() -> RiskManager:
    """RiskManager with permissive limits so test intents pass."""
    return RiskManager(RiskLimits(
        max_order_quantity=1_000_000,
        max_single_order_inr=1e12,
        max_abs_position_per_symbol=1_000_000,
        max_notional_exposure_inr=1e12,
        max_leverage=100.0,
        max_margin_utilization=1.0,
        max_daily_loss_inr=1e12,
        respect_mis_cutoff=False,
        respect_market_hours=False,
        respect_freeze_qty=False,
        respect_lot_size=False,
    ))


class _Strat:
    name = "test"
    def __init__(self, intents_per_tick: list[list[TradeIntent]]):
        self._plan = iter(intents_per_tick)

    def on_tick(self, ctx: StrategyContext) -> list[TradeIntent]:
        try:
            return next(self._plan)
        except StopIteration:
            return []


# -----------------------------------------------------------------------------
# Run-once
# -----------------------------------------------------------------------------

class TestRunOnce:
    def test_dry_run_no_orders(self) -> None:
        broker = SimBroker()
        strategy = _Strat([[_intent()]])
        engine = Engine(
            broker=broker, config=_cfg(dry_run=True),
            strategy=strategy, risk=_lax_risk(),
        )
        engine.run_once()
        assert broker.get_positions() == []

    def test_live_run_produces_order(self) -> None:
        broker = SimBroker()
        strategy = _Strat([[_intent()]])
        engine = Engine(
            broker=broker, config=_cfg(dry_run=False),
            strategy=strategy, risk=_lax_risk(),
        )
        engine.run_once()
        # SimBroker fills; positions should reflect.
        pos = broker.get_positions()
        assert len(pos) == 1
        assert pos[0].quantity == 1

    def test_persistence_logs_decision_and_order(self, tmp_path) -> None:
        db = tmp_path / "trading.sqlite"
        broker = SimBroker()
        strategy = _Strat([[_intent(strategy="testing")]])
        engine = Engine(
            broker=broker,
            config=_cfg(dry_run=False, db_path=str(db)),
            strategy=strategy, risk=_lax_risk(),
        )
        engine.run_once()

        store = SqliteStore(db)
        # At least one decision.
        decisions = store.query("SELECT * FROM decisions")
        assert len(decisions) >= 1
        # At least one order row.
        orders = store.query("SELECT * FROM orders")
        assert len(orders) >= 1

    def test_risk_rejection_persisted(self, tmp_path) -> None:
        """When risk vetos, we persist accepted=0 and don't call broker."""
        db = tmp_path / "trading.sqlite"
        broker = SimBroker()
        broker.place_order = MagicMock()  # ensure not called
        strategy = _Strat([[_intent(quantity=10_000_000)]])
        strict = RiskManager(RiskLimits(
            max_order_quantity=1,  # reject quantity > 1
            max_single_order_inr=1e12,
            max_abs_position_per_symbol=1_000_000,
            respect_mis_cutoff=False, respect_market_hours=False,
            respect_freeze_qty=False, respect_lot_size=False,
        ))
        engine = Engine(
            broker=broker, config=_cfg(dry_run=False, db_path=str(db)),
            strategy=strategy, risk=strict,
        )
        engine.run_once()

        broker.place_order.assert_not_called()
        store = SqliteStore(db)
        decisions = store.query("SELECT * FROM decisions WHERE accepted=0")
        assert len(decisions) == 1

    def test_empty_intents_no_error(self) -> None:
        broker = SimBroker()
        strategy = _Strat([[]])
        engine = Engine(
            broker=broker, config=_cfg(dry_run=False),
            strategy=strategy, risk=_lax_risk(),
        )
        engine.run_once()  # no errors


# -----------------------------------------------------------------------------
# Strategy exception handling
# -----------------------------------------------------------------------------

class TestStrategyExceptions:
    def test_strategy_exception_logged_not_raised(self, tmp_path) -> None:
        class Bad:
            name = "bad"
            def on_tick(self, ctx):
                raise RuntimeError("strategy bug")

        broker = SimBroker()
        db = tmp_path / "t.sqlite"
        engine = Engine(
            broker=broker, config=_cfg(dry_run=False, db_path=str(db)),
            strategy=Bad(), risk=_lax_risk(),
        )
        engine.run_once()  # no exception

        store = SqliteStore(db)
        errors = store.query("SELECT * FROM errors")
        assert any("strategy bug" in e["message"] for e in errors)

    def test_token_exception_halts_and_reraises(self, tmp_path, monkeypatch) -> None:
        """TokenException on order placement should trigger the halt
        sentinel + propagate (so the caller exits and re-runs login).
        """
        halt_file = tmp_path / "HALTED"
        monkeypatch.setenv("KITE_HALT_PATH", str(halt_file))

        TokenException = type("TokenException", (Exception,), {})
        broker = MagicMock()
        broker.place_order.side_effect = TokenException("expired")
        broker.get_positions.return_value = []
        broker.get_account_snapshot.return_value = Mock(
            user_id="U", net_liquidation=100_000,
            available_cash=100_000, margin_used=0, margin_available=100_000,
            currency="INR",
        )

        strategy = _Strat([[_intent()]])
        engine = Engine(
            broker=broker, config=_cfg(dry_run=False),
            strategy=strategy, risk=_lax_risk(),
        )
        with pytest.raises(Exception):
            engine.run_once()

        from kite_algo.halt import read_halt
        halt = read_halt()
        assert halt is not None
        assert "TokenException" in halt.reason


# -----------------------------------------------------------------------------
# run_forever termination
# -----------------------------------------------------------------------------

class TestRunForever:
    def test_stop_exits_loop(self) -> None:
        broker = SimBroker()
        strategy = _Strat([[_intent()], [_intent()], [_intent()]])

        engine = Engine(
            broker=broker, config=_cfg(dry_run=True),
            strategy=strategy, risk=_lax_risk(),
        )

        # Call stop() in a separate thread after first tick.
        import threading, time as _t
        def killer():
            _t.sleep(0.05)
            engine.stop()
        threading.Thread(target=killer, daemon=True).start()

        engine.run_forever()  # should exit within ~poll interval after stop
        # Broker should have disconnected; calling connect again should work.
        broker.connect()
