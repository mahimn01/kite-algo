"""Kite Connect broker adapter.

Implements the full Broker protocol on top of Kite Connect. Writes go
through `IdempotentOrderPlacer` so they get rate-limited, tag-based
orderbook dedup, and a conservative retry posture out of the box.

The write path asserts the halt sentinel + TradingConfig safety rails
before any network call, so a stray place_order can't accidentally
transmit when the broader system is halted or dry-run.
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
        """Fetch a point-in-time quote + OHLC + depth for `instrument`.

        Kite's `/quote` returns `0` (not `null`) for bid/ask when the market
        is closed or depth is unavailable — we translate to `None` so the
        caller cannot misprice against a fake zero spread. `market_closed`
        is True when *both* bid and ask come back as 0/missing (a reliable
        proxy: an open book has at least one side with a non-zero price).
        """
        client = self._require_client()
        key = instrument.kite_key
        data = client.quote([key]).get(key, {}) or {}
        ohlc = data.get("ohlc", {}) or {}
        depth = data.get("depth", {}) or {}

        def _opt_float(v: Any) -> float | None:
            """Kite returns 0 for missing prices; treat 0 and None identically
            as 'no value' for bid/ask. For OHLC/last, 0 is also never a valid
            live price."""
            if v is None:
                return None
            try:
                f = float(v)
            except (TypeError, ValueError):
                return None
            return f if f > 0 else None

        bid_raw = (depth.get("buy") or [{}])[0].get("price")
        ask_raw = (depth.get("sell") or [{}])[0].get("price")
        bid = _opt_float(bid_raw)
        ask = _opt_float(ask_raw)

        return MarketDataSnapshot(
            instrument=instrument,
            last=_opt_float(data.get("last_price")),
            bid=bid,
            ask=ask,
            volume=int(data.get("volume") or 0),
            open=_opt_float(ohlc.get("open")),
            high=_opt_float(ohlc.get("high")),
            low=_opt_float(ohlc.get("low")),
            close=_opt_float(ohlc.get("close")),
            ohlc=ohlc,
            depth=depth,
            oi=data.get("oi"),
            # Both sides missing/zero → market effectively closed (or extremely
            # illiquid). Safer to flag than to silently surface bid=None,
            # ask=None and hope callers notice.
            market_closed=bid is None and ask is None,
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
    # Write path
    # ------------------------------------------------------------------

    def _require_live(self, action: str) -> None:
        """Assert the two-layer safety gate before any outbound write.

        Also refuses while the HALTED sentinel is set so the engine can't
        drive orders through during a circuit-breaker. Dry-run and
        live-enabled flags are the same as the CLI's gates — a duplicated
        guard means the broker can't be called around the CLI layer.
        """
        from kite_algo.halt import read_halt
        halt = read_halt()
        if halt is not None:
            raise RuntimeError(
                f"{action}: refusing — trading is HALTED "
                f"(reason: {halt.reason!r} by {halt.by})"
            )
        if self._cfg.dry_run:
            raise RuntimeError(
                f"{action}: refusing in dry_run mode. Set TRADING_DRY_RUN=false."
            )
        if not self._cfg.live_enabled or not self._cfg.allow_live:
            raise RuntimeError(
                f"{action}: refusing — TRADING_ALLOW_LIVE and TRADING_LIVE_ENABLED "
                f"must both be true."
            )

    def _default_market_protection(self, order_type: str) -> int | None:
        """Post-SEBI April 2026: MARKET + SL-M must carry `market_protection`
        or the OMS rejects. -1 means Kite auto (sane default for an engine).
        """
        if order_type in ("MARKET", "SL-M"):
            return -1
        return None

    def place_order(self, req: OrderRequest) -> OrderResult:
        """Place an order via the rate-limited, idempotent placer.

        Returns an `OrderResult` with the Kite order_id. `status` reflects
        only whether Kite *accepted* the request — the OMS may later reject
        asynchronously (engine subscribes to order updates or polls).
        """
        self._require_live("place_order")
        client = self._require_client()

        from kite_algo.resilience import IdempotentOrderPlacer, new_order_tag

        tag = req.tag or new_order_tag()
        extras: dict[str, Any] = {}
        if req.limit_price is not None:
            extras["price"] = req.limit_price
        if req.trigger_price is not None:
            extras["trigger_price"] = req.trigger_price
        if req.disclosed_quantity is not None:
            extras["disclosed_quantity"] = req.disclosed_quantity
        if req.validity:
            extras["validity"] = req.validity
        mp = self._default_market_protection(req.order_type)
        if mp is not None:
            extras["market_protection"] = mp

        placer = IdempotentOrderPlacer(client)
        order_id = placer.place(
            variety=req.variety,
            exchange=req.instrument.exchange,
            tradingsymbol=req.instrument.symbol,
            transaction_type=req.side,
            quantity=req.quantity,
            product=req.product,
            order_type=req.order_type,
            tag=tag,
            **extras,
        )
        log.info("placed: id=%s tag=%s %s %s %d %s",
                 order_id, tag, req.side, req.instrument.kite_key,
                 req.quantity, req.order_type)
        return OrderResult(
            order_id=str(order_id),
            status="SUBMITTED",
            avg_price=0.0,
            filled=0,
            remaining=req.quantity,
            message=f"tag={tag}",
        )

    def modify_order(self, order_id: str, new_req: OrderRequest) -> OrderResult:
        """Modify an existing order. Tracks per-order mod count to stay
        under Kite's ~25-modification lifetime limit.
        """
        self._require_live("modify_order")
        client = self._require_client()

        from kite_algo.resilience import record_modification
        record_modification(order_id)

        kwargs: dict[str, Any] = {
            "variety": new_req.variety,
            "order_id": order_id,
            "order_type": new_req.order_type,
            "quantity": new_req.quantity,
            "validity": new_req.validity,
        }
        if new_req.limit_price is not None:
            kwargs["price"] = new_req.limit_price
        if new_req.trigger_price is not None:
            kwargs["trigger_price"] = new_req.trigger_price
        if new_req.disclosed_quantity is not None:
            kwargs["disclosed_quantity"] = new_req.disclosed_quantity
        mp = self._default_market_protection(new_req.order_type)
        if mp is not None:
            kwargs["market_protection"] = mp

        returned = client.modify_order(**kwargs)
        log.info("modified: id=%s → %s", order_id, returned)
        return OrderResult(
            order_id=str(order_id),
            status="MODIFY_SUBMITTED",
            avg_price=0.0,
            filled=0,
            remaining=new_req.quantity,
        )

    def cancel_order(self, order_id: str, variety: Variety = "regular") -> None:
        self._require_live("cancel_order")
        client = self._require_client()
        client.cancel_order(variety=variety, order_id=order_id)
        log.info("cancelled: id=%s variety=%s", order_id, variety)

    def get_order_status(self, order_id: str) -> OrderResult:
        """Return the latest state from Kite's order history.

        Sorts history entries by parsed timestamp to handle same-second
        transitions correctly — just like the CLI's `_wait_for_fill`.
        """
        client = self._require_client()
        from kite_algo.kite_tool import _parse_order_timestamp
        history = client.order_history(order_id) or []
        if not history:
            return OrderResult(
                order_id=str(order_id), status="UNKNOWN",
                message="no history returned",
            )
        history = sorted(
            history, key=lambda h: _parse_order_timestamp(h.get("order_timestamp")),
        )
        last = history[-1]
        return OrderResult(
            order_id=str(order_id),
            status=str(last.get("status") or "UNKNOWN"),
            avg_price=float(last.get("average_price") or 0),
            filled=int(last.get("filled_quantity") or 0),
            remaining=int(last.get("pending_quantity") or 0),
            message=str(last.get("status_message") or ""),
        )
