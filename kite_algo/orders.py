"""Strategy-level trade intents + conversion to broker OrderRequests.

A `TradeIntent` is what a strategy emits: "I want to BUY 1 RELIANCE at
limit 1340 for product CNC." It's richer than a plain tuple because the
risk layer needs to know product type, exchange, variety, etc., to
enforce rules correctly.

`to_order_request()` lowers a TradeIntent into the broker's
`OrderRequest` dataclass (which is what `KiteBroker.place_order` wants).
"""

from __future__ import annotations

from dataclasses import dataclass

from kite_algo.broker.base import OrderRequest, Side
from kite_algo.instruments import InstrumentSpec


@dataclass(frozen=True)
class TradeIntent:
    """High-level intent from a strategy.

    Required: `instrument`, `side`, `quantity`. Everything else has a safe
    default (LIMIT orders need `limit_price`; the validator enforces).
    """
    instrument: InstrumentSpec
    side: Side
    quantity: int
    order_type: str = "LIMIT"    # MARKET | LIMIT | SL | SL-M
    product: str = "CNC"         # CNC | MIS | NRML | MTF
    variety: str = "regular"     # regular | amo | co | iceberg | auction
    validity: str = "DAY"
    limit_price: float | None = None
    trigger_price: float | None = None
    disclosed_quantity: int | None = None
    iceberg_legs: int | None = None
    iceberg_quantity: int | None = None
    market_protection: float | None = None
    tag: str | None = None
    reason: str = ""
    strategy: str = ""
    group_id: str | None = None
    leg_name: str | None = None

    def to_order_request(self) -> OrderRequest:
        return OrderRequest(
            instrument=self.instrument,
            side=self.side,
            quantity=self.quantity,
            order_type=self.order_type,  # type: ignore[arg-type]
            product=self.product,         # type: ignore[arg-type]
            variety=self.variety,         # type: ignore[arg-type]
            validity=self.validity,       # type: ignore[arg-type]
            limit_price=self.limit_price,
            trigger_price=self.trigger_price,
            disclosed_quantity=self.disclosed_quantity,
            tag=self.tag,
        )


def validate_order_request(req: OrderRequest) -> None:
    if req.quantity <= 0:
        raise ValueError(f"Order quantity must be positive, got {req.quantity}")
    if req.order_type == "LIMIT" and req.limit_price is None:
        raise ValueError("LIMIT order requires limit_price")
    if req.order_type in ("SL", "SL-M") and req.trigger_price is None:
        raise ValueError(f"{req.order_type} order requires trigger_price")
    if req.side not in ("BUY", "SELL"):
        raise ValueError(f"Invalid side: {req.side}")
