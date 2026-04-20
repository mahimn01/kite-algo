"""Polling engine — ties Strategy + Broker + RiskManager + OMS together.

Runtime model:
- `connect` the broker.
- Start a run in the persistence store (gets a run_id).
- Loop:
  - Build a StrategyContext (now_epoch_s + get_snapshot callable).
  - Ask strategy for intents.
  - For each intent: validate via risk, submit via OMS, persist outcome.
  - Sleep `poll_seconds`.
- On TokenException / KiteSessionError: raise a HALT via `halt.write_halt`
  and exit — a new day's login is needed. The engine never tries to self-
  reauth because Kite explicitly forbids automated login.
- On KeyboardInterrupt: gracefully stop, end_run, disconnect.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

from kite_algo.broker.base import Broker, MarketDataSnapshot
from kite_algo.config import TradingConfig
from kite_algo.envelope import new_request_id, parent_request_id
from kite_algo.instruments import InstrumentSpec
from kite_algo.market_data import MarketDataClient, MarketDataConfig
from kite_algo.oms import OrderManager
from kite_algo.orders import TradeIntent
from kite_algo.persistence import SqliteStore
from kite_algo.risk import RiskLimits, RiskManager, RiskViolation, risk_limits_from_env

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Strategy contract
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StrategyContext:
    """What a strategy sees on each tick."""
    now_epoch_s: float
    get_snapshot: Callable[[InstrumentSpec], MarketDataSnapshot]


class Strategy(Protocol):
    """Minimal strategy protocol. Richer strategies live outside this repo —
    we're only specifying the surface the engine needs.
    """
    name: str

    def on_tick(self, ctx: StrategyContext) -> list[TradeIntent]: ...


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

@dataclass
class Engine:
    broker: Broker
    config: TradingConfig
    strategy: Strategy
    risk: RiskManager
    confirm_token: str | None = None
    market_data_ttl: float = 1.0
    md_min_interval: float = 0.0
    _md: MarketDataClient | None = field(default=None, init=False)
    _store: SqliteStore | None = field(default=None, init=False)
    _oms: OrderManager | None = field(default=None, init=False)
    _run_id: int | None = field(default=None, init=False)
    _running: bool = field(default=False, init=False)

    # ----------------------------------------------------------------
    # Lifecycle
    # ----------------------------------------------------------------

    def run_forever(self) -> None:
        self._setup()
        self._running = True
        try:
            while self._running:
                self._tick_once()
                time.sleep(max(0.1, self.config.poll_seconds))
        except KeyboardInterrupt:
            log.info("engine: KeyboardInterrupt — stopping")
        finally:
            self._teardown()

    def run_once(self, ctx: StrategyContext | None = None) -> None:
        """Single-shot tick — connect, process, disconnect. For one-off
        scripts and for tests.
        """
        self._setup()
        try:
            self._tick_once(ctx=ctx)
        finally:
            self._teardown()

    def stop(self) -> None:
        self._running = False

    # ----------------------------------------------------------------
    # Internals
    # ----------------------------------------------------------------

    def _setup(self) -> None:
        self.broker.connect()
        self._md = MarketDataClient(
            self.broker,
            MarketDataConfig(
                ttl_seconds=self.market_data_ttl,
                min_interval_seconds=self.md_min_interval,
            ),
        )
        if self.config.db_path:
            self._store = SqliteStore(self.config.db_path)
            import os
            self._run_id = self._store.start_run(
                cfg={"broker": self.config.broker, "dry_run": self.config.dry_run,
                     "live_enabled": self.config.live_enabled,
                     "allow_live": self.config.allow_live,
                     "poll_seconds": self.config.poll_seconds},
                strategy=getattr(self.strategy, "name", "unknown"),
                strategy_id=os.getenv("KITE_STRATEGY_ID"),
                agent_id=os.getenv("KITE_AGENT_ID"),
                parent_request_id=parent_request_id(),
            )
        self._oms = OrderManager(
            self.broker, self.config,
            store=self._store, run_id=self._run_id,
        )

    def _teardown(self) -> None:
        if self._store is not None and self._run_id is not None:
            try:
                self._store.end_run(self._run_id)
            except Exception as exc:
                log.warning("end_run failed: %s", exc)
            self._store.close()
        self._store = None
        self._run_id = None
        self._md = None
        self._oms = None
        try:
            self.broker.disconnect()
        except Exception as exc:
            log.warning("broker.disconnect failed: %s", exc)

    def _tick_once(self, ctx: StrategyContext | None = None) -> None:
        if self._md is None or self._oms is None:
            raise RuntimeError("Engine._tick_once called before _setup")
        ctx = ctx or StrategyContext(
            now_epoch_s=time.time(),
            get_snapshot=self._md.get_snapshot,
        )
        try:
            intents = self.strategy.on_tick(ctx) or []
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            log.error("strategy.on_tick raised: %s", exc)
            if self._store is not None and self._run_id is not None:
                self._store.log_error(
                    self._run_id, where="strategy.on_tick",
                    message=str(exc),
                )
            return

        for intent in intents:
            self._handle_intent(intent, ctx)

    def _handle_intent(self, intent: TradeIntent, ctx: StrategyContext) -> None:
        strategy_name = intent.strategy or getattr(self.strategy, "name", "unknown")

        # 1. Risk validation.
        try:
            self.risk.validate(intent, self.broker, ctx.get_snapshot)
        except RiskViolation as exc:
            log.warning("risk reject %s: %s %s qty=%d",
                        exc.code, intent.side, intent.instrument.kite_key,
                        intent.quantity)
            if self._store is not None and self._run_id is not None:
                self._store.log_decision(
                    self._run_id, strategy=strategy_name, intent=intent,
                    accepted=False, reason=str(exc),
                )
            return
        except Exception as exc:
            log.error("risk.validate raised unexpectedly: %s", exc)
            if self._store is not None and self._run_id is not None:
                self._store.log_error(
                    self._run_id, where="risk.validate", message=str(exc),
                )
            return

        # 2. Submit via OMS (which handles persistence + gates).
        try:
            result = self._oms.submit(intent)  # type: ignore[union-attr]
            if not result.accepted:
                log.info("oms: %s — %s", result.action, result.reason or "blocked")
        except Exception as exc:
            # TokenException / session errors are terminal — raise halt + re-raise
            # so run_forever exits and the caller knows to re-login.
            name = type(exc).__name__
            if name in ("TokenException", "KiteSessionError"):
                from kite_algo.halt import write_halt
                write_halt(
                    reason=f"engine: {name}: {exc}",
                    by=getattr(self.strategy, "name", "engine"),
                )
                self._running = False
                raise
            log.error("oms.submit failed: %s", exc)
            if self._store is not None and self._run_id is not None:
                self._store.log_error(
                    self._run_id, where="oms.submit", message=str(exc),
                )


# ---------------------------------------------------------------------------
# Convenience factory
# ---------------------------------------------------------------------------

def default_risk_manager(limits: RiskLimits | None = None) -> RiskManager:
    return RiskManager(limits or risk_limits_from_env())
