"""Polling engine (scaffold — to be built out)."""

from __future__ import annotations

from kite_algo.broker.base import Broker


class Engine:
    """Minimal polling skeleton. Real implementation pending."""

    def __init__(self, broker: Broker, poll_seconds: int = 5):
        self._broker = broker
        self._poll_seconds = poll_seconds
        self._running = False

    def run_once(self) -> None:
        raise NotImplementedError

    def run_forever(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        self._running = False
