"""Broker interface + common dataclasses.

Intentionally shaped to match `trading_algo.broker.base` so the engine, risk
manager, and OMS can target both repos with minimal glue.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

from kite_algo.instruments import InstrumentSpec, Product

Side = Literal["BUY", "SELL"]
OrderType = Literal["MARKET", "LIMIT", "SL", "SL-M"]
Variety = Literal["regular", "amo", "co", "iceberg", "auction"]
Validity = Literal["DAY", "IOC", "TTL"]


# -----------------------------------------------------------------------------
# Dataclasses
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class OrderRequest:
    instrument: InstrumentSpec
    side: Side
    quantity: int
    order_type: OrderType = "LIMIT"
    product: Product = "CNC"
    variety: Variety = "regular"
    validity: Validity = "DAY"
    limit_price: float | None = None
    trigger_price: float | None = None
    disclosed_quantity: int | None = None
    tag: str | None = None


@dataclass(frozen=True)
class OrderResult:
    order_id: str
    status: str
    avg_price: float = 0.0
    filled: int = 0
    remaining: int = 0
    message: str = ""


@dataclass(frozen=True)
class Position:
    instrument: InstrumentSpec
    product: Product
    quantity: int
    avg_price: float
    last_price: float
    day_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0


@dataclass(frozen=True)
class Bar:
    timestamp_epoch_s: int
    open: float
    high: float
    low: float
    close: float
    volume: int
    oi: int | None = None


@dataclass(frozen=True)
class MarketDataSnapshot:
    """Snapshot of live market state for one instrument.

    Fields that may be absent are typed `float | None`.  Kite returns 0 for
    `bid`/`ask` when the market is closed or depth is unavailable — we
    explicitly translate that to `None` so callers cannot accidentally price
    trades against a zero spread.

    Use `market_closed` to reason about why bid/ask may be None: Kite
    returns a partial snapshot even when the market is shut (LTP = prior
    close, no depth).  An agent reading this should never place a LIMIT
    order priced against `bid=None`.
    """
    instrument: InstrumentSpec
    last: float | None
    bid: float | None
    ask: float | None
    volume: int
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    ohlc: dict = field(default_factory=dict)
    depth: dict = field(default_factory=dict)
    oi: int | None = None
    market_closed: bool = False


@dataclass(frozen=True)
class AccountSnapshot:
    user_id: str
    net_liquidation: float
    available_cash: float
    margin_used: float
    margin_available: float
    currency: str = "INR"


# -----------------------------------------------------------------------------
# Protocol the engine and OMS target
# -----------------------------------------------------------------------------

class Broker(Protocol):
    def connect(self) -> None: ...
    def disconnect(self) -> None: ...
    def is_connected(self) -> bool: ...

    def get_account_snapshot(self) -> AccountSnapshot: ...
    def get_positions(self) -> list[Position]: ...

    def get_market_data_snapshot(self, instrument: InstrumentSpec) -> MarketDataSnapshot: ...
    def get_historical_bars(
        self,
        instrument: InstrumentSpec,
        *,
        from_date: str,
        to_date: str,
        interval: str,
    ) -> list[Bar]: ...

    def place_order(self, req: OrderRequest) -> OrderResult: ...
    def modify_order(self, order_id: str, new_req: OrderRequest) -> OrderResult: ...
    def cancel_order(self, order_id: str, variety: Variety = "regular") -> None: ...
    def get_order_status(self, order_id: str) -> OrderResult: ...
