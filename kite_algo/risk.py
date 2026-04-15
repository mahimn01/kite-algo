"""Risk manager (scaffold)."""

from __future__ import annotations

from dataclasses import dataclass

from kite_algo.broker.base import OrderRequest, Position


@dataclass(frozen=True)
class RiskLimits:
    max_gross_exposure: float = 1.5
    max_position_size_rupees: float = 500_000.0
    max_orders_per_tick: int = 5
    max_single_order_rupees: float = 100_000.0


class RiskManager:
    def __init__(self, limits: RiskLimits = RiskLimits()):
        self._limits = limits

    def check(self, req: OrderRequest, positions: list[Position], cash: float) -> None:
        """Raise if the order would violate any limit. Scaffold only."""
        if req.limit_price and req.limit_price * req.quantity > self._limits.max_single_order_rupees:
            raise ValueError(
                f"Order value exceeds max_single_order_rupees "
                f"({self._limits.max_single_order_rupees})"
            )
