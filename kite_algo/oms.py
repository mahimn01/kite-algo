"""Order manager (scaffold — persistence + state machine coming)."""

from __future__ import annotations

from dataclasses import dataclass

from kite_algo.broker.base import Broker, OrderRequest, OrderResult


@dataclass
class OrderManager:
    broker: Broker

    def submit(self, req: OrderRequest) -> OrderResult:
        return self.broker.place_order(req)

    def cancel(self, order_id: str) -> None:
        self.broker.cancel_order(order_id)
