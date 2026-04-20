"""OrderManager — thin layer between the engine and the broker.

Responsibilities:
- Convert TradeIntents to OrderRequests and submit them.
- Track outstanding order_ids in-process (so the engine can wait / cancel).
- Persist every submit / modify / cancel / status-update into SqliteStore.
- Reconcile the in-process view against Kite's live orderbook (detect orders
  terminal server-side that we haven't picked up yet).

State model: the OMS doesn't try to own the ground truth — Kite is the
source of truth for fills, and we persist into `order_status_events`. The
in-memory dict exists only for fast "is this order still active?" checks
during a live run.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from kite_algo.broker.base import Broker, OrderRequest, OrderResult
from kite_algo.config import TradingConfig
from kite_algo.orders import TradeIntent

log = logging.getLogger(__name__)

TERMINAL_STATUSES = frozenset({"COMPLETE", "CANCELLED", "REJECTED"})


@dataclass
class _Tracked:
    """In-process snapshot of a known order."""
    order_id: str
    intent: TradeIntent | None
    status: str = "SUBMITTED"
    tag: str | None = None
    group_id: str | None = None
    leg_name: str | None = None
    idempotency_key: str | None = None
    last_seen_ms: int = field(default_factory=lambda: int(time.time() * 1000))


@dataclass
class OMSResult:
    """What the OMS returns from submit / modify / cancel."""
    action: str                          # "place" | "modify" | "cancel" | "noop"
    order_id: str | None
    accepted: bool
    reason: str | None = None
    raw: OrderResult | None = None


class OrderManager:
    """Submit / modify / cancel orders through a Broker, with persistence.

    Usage:
        oms = OrderManager(broker, cfg, store=sqlite_store, run_id=42)
        result = oms.submit(intent)

    Thread-safe (mutex on the in-memory tracker).
    """

    def __init__(
        self,
        broker: Broker,
        cfg: TradingConfig,
        *,
        store: Any | None = None,
        run_id: int | None = None,
    ):
        self._broker = broker
        self._cfg = cfg
        self._store = store
        self._run_id = run_id
        self._tracked: dict[str, _Tracked] = {}
        self._lock = threading.Lock()

    # ----------------------------------------------------------------
    # Public — high-level
    # ----------------------------------------------------------------

    def submit(self, intent: TradeIntent) -> OMSResult:
        """Convert intent → OrderRequest → broker.place_order → persist."""
        if self._cfg.dry_run:
            self._log_decision("dry_run", intent, accepted=False, reason="dry_run")
            return OMSResult(action="noop", order_id=None, accepted=False,
                             reason="dry_run")

        req = intent.to_order_request()
        try:
            result = self._broker.place_order(req)
        except Exception as exc:
            self._log_error("submit", str(exc))
            self._log_decision(intent.strategy or "", intent,
                               accepted=False, reason=str(exc))
            raise

        order_id = str(result.order_id)
        with self._lock:
            self._tracked[order_id] = _Tracked(
                order_id=order_id,
                intent=intent,
                status=result.status,
                tag=intent.tag,
                group_id=intent.group_id,
                leg_name=intent.leg_name,
            )

        self._log_decision(intent.strategy or "", intent,
                           accepted=True, reason=None)
        if self._store is not None:
            self._store.log_order(
                self._run_id, broker=self._cfg.broker,
                order_id=order_id,
                request={
                    "exchange": intent.instrument.exchange,
                    "tradingsymbol": intent.instrument.symbol,
                    "transaction_type": intent.side,
                    "order_type": intent.order_type,
                    "product": intent.product,
                    "variety": intent.variety,
                    "quantity": intent.quantity,
                    "price": intent.limit_price,
                    "trigger_price": intent.trigger_price,
                    "validity": intent.validity,
                    "validity_ttl": None,
                    "disclosed_quantity": intent.disclosed_quantity,
                    "iceberg_legs": intent.iceberg_legs,
                    "iceberg_quantity": intent.iceberg_quantity,
                    "market_protection": intent.market_protection,
                    "tag": intent.tag,
                },
                status=result.status,
                tag=intent.tag,
                group_id=intent.group_id,
                leg_name=intent.leg_name,
            )
            try:
                st = self._broker.get_order_status(order_id)
                self._store.log_order_status_event(
                    self._run_id, self._cfg.broker,
                    {"order_id": order_id, "status": st.status,
                     "filled_quantity": st.filled, "pending_quantity": st.remaining,
                     "average_price": st.avg_price, "status_message": st.message},
                )
            except Exception as exc:
                self._log_error("submit.status", str(exc))

        return OMSResult(action="place", order_id=order_id, accepted=True,
                         raw=result)

    def modify(self, order_id: str, new_intent: TradeIntent) -> OMSResult:
        if self._cfg.dry_run:
            return OMSResult(action="noop", order_id=order_id, accepted=False,
                             reason="dry_run")
        try:
            result = self._broker.modify_order(order_id, new_intent.to_order_request())
        except Exception as exc:
            self._log_error("modify", f"{order_id}: {exc}")
            raise

        with self._lock:
            tracked = self._tracked.get(order_id)
            if tracked is not None:
                tracked.status = result.status
                tracked.last_seen_ms = int(time.time() * 1000)

        return OMSResult(action="modify", order_id=order_id, accepted=True,
                         raw=result)

    def cancel(self, order_id: str, variety: str = "regular") -> OMSResult:
        if self._cfg.dry_run:
            return OMSResult(action="noop", order_id=order_id, accepted=False,
                             reason="dry_run")
        try:
            self._broker.cancel_order(order_id, variety=variety)  # type: ignore[arg-type]
        except Exception as exc:
            self._log_error("cancel", f"{order_id}: {exc}")
            raise

        with self._lock:
            tracked = self._tracked.get(order_id)
            if tracked is not None:
                tracked.status = "CANCEL_SUBMITTED"
                tracked.last_seen_ms = int(time.time() * 1000)

        return OMSResult(action="cancel", order_id=order_id, accepted=True)

    def status(self, order_id: str) -> OrderResult:
        """Fetch latest status from the broker and record the event."""
        result = self._broker.get_order_status(order_id)
        with self._lock:
            tracked = self._tracked.get(order_id)
            if tracked is not None:
                tracked.status = result.status
                tracked.last_seen_ms = int(time.time() * 1000)
        if self._store is not None:
            try:
                self._store.log_order_status_event(
                    self._run_id, self._cfg.broker,
                    {"order_id": order_id, "status": result.status,
                     "filled_quantity": result.filled,
                     "pending_quantity": result.remaining,
                     "average_price": result.avg_price,
                     "status_message": result.message},
                )
            except Exception as exc:
                self._log_error("status", f"{order_id}: {exc}")
        return result

    # ----------------------------------------------------------------
    # Reconciliation
    # ----------------------------------------------------------------

    def tracked_order_ids(self) -> list[str]:
        with self._lock:
            return list(self._tracked.keys())

    def active_order_ids(self) -> list[str]:
        """Orders we believe are still live (non-terminal)."""
        with self._lock:
            return [
                oid for oid, t in self._tracked.items()
                if t.status not in TERMINAL_STATUSES
            ]

    def reconcile(self) -> dict:
        """Poll broker.get_order_status for every active order; update
        in-memory state + persist status events. Returns a summary dict.
        """
        active = self.active_order_ids()
        updates: dict[str, str] = {}
        failures: list[dict] = []
        for oid in active:
            try:
                res = self.status(oid)
                updates[oid] = res.status
            except Exception as exc:
                failures.append({"order_id": oid, "error": str(exc)})
        return {
            "checked": len(active),
            "terminal_now": sum(
                1 for s in updates.values() if s in TERMINAL_STATUSES
            ),
            "updates": updates,
            "failures": failures,
        }

    def track_open_orders(
        self,
        *,
        poll_seconds: float = 0.5,
        timeout_seconds: float = 30.0,
    ) -> dict:
        """Block until every tracked order is terminal, or `timeout_seconds`
        elapses. Returns a reconciliation summary.
        """
        deadline = time.monotonic() + timeout_seconds
        last: dict = {"checked": 0, "terminal_now": 0}
        while time.monotonic() < deadline:
            last = self.reconcile()
            if last["terminal_now"] == last["checked"]:
                break
            time.sleep(poll_seconds)
        return last

    # ----------------------------------------------------------------
    # Persistence helpers
    # ----------------------------------------------------------------

    def _log_decision(
        self, strategy: str, intent: TradeIntent, *, accepted: bool,
        reason: str | None,
    ) -> None:
        if self._store is None or self._run_id is None:
            return
        try:
            self._store.log_decision(
                self._run_id, strategy=strategy or "unknown",
                intent=intent, accepted=accepted, reason=reason,
            )
        except Exception as exc:
            log.warning("log_decision failed: %s", exc)

    def _log_error(self, where: str, message: str) -> None:
        if self._store is None or self._run_id is None:
            return
        try:
            self._store.log_error(
                self._run_id, where=where, message=message,
            )
        except Exception as exc:
            log.warning("log_error failed: %s", exc)
