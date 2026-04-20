"""Deterministic simulation broker — for tests and offline runs."""

from __future__ import annotations

import itertools
import time

from kite_algo.broker.base import (
    AccountSnapshot,
    Bar,
    MarketDataSnapshot,
    OrderRequest,
    OrderResult,
    Position,
    Variety,
)
from kite_algo.instruments import InstrumentSpec


class SimBroker:
    """Fills everything at the requested price and pretends the account is rich.

    Useful for smoke-testing CLI plumbing without touching Kite.
    """

    def __init__(self, cash: float = 1_000_000.0):
        self._cash = cash
        self._positions: dict[str, Position] = {}
        self._connected = False
        self._order_ids = itertools.count(1)

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def get_account_snapshot(self) -> AccountSnapshot:
        return AccountSnapshot(
            user_id="SIM",
            net_liquidation=self._cash,
            available_cash=self._cash,
            margin_used=0.0,
            margin_available=self._cash,
            currency="INR",
        )

    def get_positions(self) -> list[Position]:
        return list(self._positions.values())

    def get_market_data_snapshot(self, instrument: InstrumentSpec) -> MarketDataSnapshot:
        price = 100.0
        return MarketDataSnapshot(
            instrument=instrument,
            last=price,
            bid=price - 0.05,
            ask=price + 0.05,
            volume=0,
            open=price,
            high=price,
            low=price,
            close=price,
            market_closed=False,
        )

    def get_historical_bars(
        self,
        instrument: InstrumentSpec,
        *,
        from_date: str,
        to_date: str,
        interval: str,
    ) -> list[Bar]:
        now = int(time.time())
        return [
            Bar(timestamp_epoch_s=now - 60 * i, open=100, high=101, low=99, close=100, volume=1000)
            for i in range(10)
        ]

    def place_order(self, req: OrderRequest) -> OrderResult:
        order_id = str(next(self._order_ids))
        price = req.limit_price or 100.0
        self._cash -= price * req.quantity * (1 if req.side == "BUY" else -1)
        key = req.instrument.kite_key
        self._positions[key] = Position(
            instrument=req.instrument,
            product=req.product,
            quantity=req.quantity * (1 if req.side == "BUY" else -1),
            avg_price=price,
            last_price=price,
        )
        return OrderResult(order_id=order_id, status="COMPLETE", avg_price=price, filled=req.quantity)

    def modify_order(self, order_id: str, new_req: OrderRequest) -> OrderResult:
        return OrderResult(order_id=order_id, status="COMPLETE", avg_price=new_req.limit_price or 0.0)

    def cancel_order(self, order_id: str, variety: Variety = "regular") -> None:
        return None

    def get_order_status(self, order_id: str) -> OrderResult:
        return OrderResult(order_id=order_id, status="COMPLETE")
