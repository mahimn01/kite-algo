"""Order validation + TradeIntent (scaffold)."""

from __future__ import annotations

from dataclasses import dataclass

from kite_algo.broker.base import OrderRequest, Side


@dataclass(frozen=True)
class TradeIntent:
    """High-level intent from a strategy before it becomes an `OrderRequest`."""
    symbol: str
    side: Side
    quantity: int
    reason: str = ""
    target_price: float | None = None


def validate_order_request(req: OrderRequest) -> None:
    if req.quantity <= 0:
        raise ValueError(f"Order quantity must be positive, got {req.quantity}")
    if req.order_type == "LIMIT" and req.limit_price is None:
        raise ValueError("LIMIT order requires limit_price")
    if req.order_type in ("SL", "SL-M") and req.trigger_price is None:
        raise ValueError(f"{req.order_type} order requires trigger_price")
    if req.side not in ("BUY", "SELL"):
        raise ValueError(f"Invalid side: {req.side}")
