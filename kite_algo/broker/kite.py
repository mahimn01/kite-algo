"""Kite Connect broker adapter.

Scaffold only — the full implementation will land incrementally. For now this
wires up a KiteConnect client from config and exposes minimal read-only
methods that the CLI / engine can already target.

The write path (place/modify/cancel) is behind `require_live()` so it raises
loudly until safety rails are opted into explicitly.
"""

from __future__ import annotations

import logging
from typing import Any

from kite_algo.broker.base import (
    AccountSnapshot,
    Bar,
    MarketDataSnapshot,
    OrderRequest,
    OrderResult,
    Position,
    Variety,
)
from kite_algo.config import KiteConfig, TradingConfig
from kite_algo.instruments import InstrumentSpec, PRODUCT_LABELS

log = logging.getLogger(__name__)


class KiteDependencyError(RuntimeError):
    pass


class KiteSessionError(RuntimeError):
    pass


def _import_kiteconnect() -> Any:
    try:
        from kiteconnect import KiteConnect  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise KiteDependencyError(
            "kiteconnect is not installed. Run: pip install kiteconnect"
        ) from exc
    return KiteConnect


class KiteBroker:
    """Kite Connect broker adapter.

    Matches the `kite_algo.broker.base.Broker` protocol (which mirrors the
    shape of `trading_algo.broker.base.Broker`) so the engine/OMS layers can
    target it interchangeably with IBKR.
    """

    def __init__(self, cfg: TradingConfig):
        self._cfg = cfg
        self._kite_cfg: KiteConfig = cfg.kite
        self._client: Any = None

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self) -> None:
        self._kite_cfg.require_session()
        KiteConnect = _import_kiteconnect()
        self._client = KiteConnect(api_key=self._kite_cfg.api_key)
        self._client.set_access_token(self._kite_cfg.access_token)
        # Ping to verify session
        try:
            self._client.profile()
        except Exception as exc:
            raise KiteSessionError(
                f"Kite session invalid (token may have expired at 6am IST). "
                f"Run `kite_tool login` to re-auth. Underlying error: {exc}"
            ) from exc
        log.info("KiteBroker connected as user=%s", self._kite_cfg.user_id or "(unset)")

    def disconnect(self) -> None:
        self._client = None

    def is_connected(self) -> bool:
        return self._client is not None

    def _require_client(self) -> Any:
        if self._client is None:
            raise RuntimeError("KiteBroker is not connected — call connect() first.")
        return self._client

    # ------------------------------------------------------------------
    # Read-only (implemented)
    # ------------------------------------------------------------------

    def get_account_snapshot(self) -> AccountSnapshot:
        client = self._require_client()
        margins = client.margins()
        equity = margins.get("equity", {}) if isinstance(margins, dict) else {}
        cash = float(equity.get("available", {}).get("cash") or 0)
        used = float(equity.get("utilised", {}).get("debits") or 0)
        available = float(equity.get("available", {}).get("live_balance") or cash)
        net = float(equity.get("net") or (cash - used))
        return AccountSnapshot(
            user_id=self._kite_cfg.user_id or "",
            net_liquidation=net,
            available_cash=cash,
            margin_used=used,
            margin_available=available,
            currency="INR",
        )

    def get_positions(self) -> list[Position]:
        client = self._require_client()
        payload = client.positions() or {}
        rows = payload.get("net", []) if isinstance(payload, dict) else []
        out: list[Position] = []
        for r in rows:
            exch = r.get("exchange", "NSE")
            inst = InstrumentSpec(symbol=r.get("tradingsymbol", ""), exchange=exch)  # type: ignore[arg-type]
            out.append(
                Position(
                    instrument=inst,
                    product=r.get("product", "CNC"),  # type: ignore[arg-type]
                    quantity=int(r.get("quantity") or 0),
                    avg_price=float(r.get("average_price") or 0),
                    last_price=float(r.get("last_price") or 0),
                    day_pnl=float(r.get("day_pnl") or 0),
                    unrealized_pnl=float(r.get("unrealised") or 0),
                    realized_pnl=float(r.get("realised") or 0),
                )
            )
        return out

    def get_market_data_snapshot(self, instrument: InstrumentSpec) -> MarketDataSnapshot:
        client = self._require_client()
        key = instrument.kite_key
        data = client.quote([key]).get(key, {})
        ohlc = data.get("ohlc", {}) or {}
        depth = data.get("depth", {}) or {}
        return MarketDataSnapshot(
            instrument=instrument,
            last=float(data.get("last_price") or 0),
            bid=float((depth.get("buy") or [{}])[0].get("price") or 0),
            ask=float((depth.get("sell") or [{}])[0].get("price") or 0),
            volume=int(data.get("volume") or 0),
            open=float(ohlc.get("open") or 0),
            high=float(ohlc.get("high") or 0),
            low=float(ohlc.get("low") or 0),
            close=float(ohlc.get("close") or 0),
            ohlc=ohlc,
            depth=depth,
            oi=data.get("oi"),
        )

    def get_historical_bars(
        self,
        instrument: InstrumentSpec,
        *,
        from_date: str,
        to_date: str,
        interval: str,
    ) -> list[Bar]:
        client = self._require_client()
        if instrument.instrument_token is None:
            raise ValueError(
                "get_historical_bars requires instrument.instrument_token. "
                "Look it up via the instruments dump first."
            )
        bars = client.historical_data(
            instrument_token=instrument.instrument_token,
            from_date=from_date,
            to_date=to_date,
            interval=interval,
        ) or []
        return [
            Bar(
                timestamp_epoch_s=int(b["date"].timestamp()),
                open=float(b["open"]),
                high=float(b["high"]),
                low=float(b["low"]),
                close=float(b["close"]),
                volume=int(b.get("volume") or 0),
                oi=b.get("oi"),
            )
            for b in bars
        ]

    # ------------------------------------------------------------------
    # Write path (stubbed — gated on safety rails)
    # ------------------------------------------------------------------

    def _require_live(self, action: str) -> None:
        if self._cfg.dry_run:
            raise RuntimeError(
                f"{action}: refusing in dry_run mode. Set TRADING_DRY_RUN=false."
            )
        if not self._cfg.live_enabled or not self._cfg.allow_live:
            raise RuntimeError(
                f"{action}: refusing — TRADING_ALLOW_LIVE and TRADING_LIVE_ENABLED "
                f"must both be true."
            )

    def place_order(self, req: OrderRequest) -> OrderResult:
        self._require_live("place_order")
        raise NotImplementedError("KiteBroker.place_order — pending implementation")

    def modify_order(self, order_id: str, new_req: OrderRequest) -> OrderResult:
        self._require_live("modify_order")
        raise NotImplementedError("KiteBroker.modify_order — pending implementation")

    def cancel_order(self, order_id: str, variety: Variety = "regular") -> None:
        self._require_live("cancel_order")
        raise NotImplementedError("KiteBroker.cancel_order — pending implementation")

    def get_order_status(self, order_id: str) -> OrderResult:
        raise NotImplementedError("KiteBroker.get_order_status — pending implementation")
