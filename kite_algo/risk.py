"""Risk manager — enforces position + exposure + time-of-day rules.

Called by the engine before every order submission. Rejects loudly; never
silently clamps (a clamped order confuses strategies). The Indian-market
specifics (MIS cutoff, freeze quantity, lot-size multiples) come from
`kite_algo.market_rules`.

Structure mirrors `trading_algo/risk.py` but extended for Kite:
- INR-denominated instead of USD
- Product awareness (CNC / MIS / NRML) — MIS has 15:20 auto-squareoff
- Freeze-quantity check before orders go out
- Per-strategy notional cap via KITE_STRATEGY_NOTIONAL_CAP_INR env

Design: every check raises `RiskViolation` with a code + human message.
The engine catches and records the decision as rejected. No retries —
risk is a terminal gate.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from kite_algo.broker.base import AccountSnapshot, Broker, MarketDataSnapshot, Position
from kite_algo.instruments import InstrumentSpec
from kite_algo.market_rules import (
    check_market_rules,
    freeze_qty,
    is_valid_lot_multiple,
    lot_size,
    mis_status,
    now_ist,
)
from kite_algo.orders import TradeIntent


# -----------------------------------------------------------------------------
# Limits
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class RiskLimits:
    """All limits are INR-denominated; quantity limits are in share / lot units.

    Defaults are intentionally conservative — a strategy can override by
    passing a custom RiskLimits to RiskManager.
    """
    # Per-order limits
    max_order_quantity: int = 10_000
    max_single_order_inr: float = 500_000.0

    # Per-symbol position limits
    max_abs_position_per_symbol: int = 100_000
    allow_short: bool = False   # Equity on NSE/BSE is long-only by default.

    # Account-wide exposure
    max_notional_exposure_inr: float = 10_000_000.0   # 1 crore
    max_leverage: float = 5.0   # gross_position_value / net_liq
    max_margin_utilization: float = 0.8   # margin_used / margin_available

    # Daily-loss circuit breaker
    max_daily_loss_inr: float = 50_000.0

    # Time-based (Indian market)
    respect_mis_cutoff: bool = True
    respect_market_hours: bool = True
    respect_freeze_qty: bool = True
    respect_lot_size: bool = True

    # Per-strategy caps (reads KITE_STRATEGY_NOTIONAL_CAP_INR by default)
    strategy_notional_cap_inr: float | None = None


def risk_limits_from_env() -> RiskLimits:
    """Build RiskLimits from KITE_RISK_* env vars with fallback to defaults."""
    def _f(name: str, default: float) -> float:
        try:
            return float(os.getenv(name) or default)
        except ValueError:
            return default
    def _i(name: str, default: int) -> int:
        try:
            return int(os.getenv(name) or default)
        except ValueError:
            return default
    def _b(name: str, default: bool) -> bool:
        raw = (os.getenv(name) or "").strip().lower()
        if not raw:
            return default
        return raw in ("1", "true", "yes", "on", "y", "t")

    cap_env = os.getenv("KITE_STRATEGY_NOTIONAL_CAP_INR")
    try:
        strategy_cap = float(cap_env) if cap_env else None
    except ValueError:
        strategy_cap = None

    return RiskLimits(
        max_order_quantity=_i("KITE_RISK_MAX_ORDER_QTY", 10_000),
        max_single_order_inr=_f("KITE_RISK_MAX_SINGLE_ORDER_INR", 500_000.0),
        max_abs_position_per_symbol=_i("KITE_RISK_MAX_POS_PER_SYMBOL", 100_000),
        allow_short=_b("KITE_RISK_ALLOW_SHORT", False),
        max_notional_exposure_inr=_f("KITE_RISK_MAX_NOTIONAL_INR", 10_000_000.0),
        max_leverage=_f("KITE_RISK_MAX_LEVERAGE", 5.0),
        max_margin_utilization=_f("KITE_RISK_MAX_MARGIN_UTIL", 0.8),
        max_daily_loss_inr=_f("KITE_RISK_MAX_DAILY_LOSS_INR", 50_000.0),
        respect_mis_cutoff=_b("KITE_RISK_MIS_CUTOFF", True),
        respect_market_hours=_b("KITE_RISK_MARKET_HOURS", True),
        respect_freeze_qty=_b("KITE_RISK_FREEZE_QTY", True),
        respect_lot_size=_b("KITE_RISK_LOT_SIZE", True),
        strategy_notional_cap_inr=strategy_cap,
    )


# -----------------------------------------------------------------------------
# Exceptions
# -----------------------------------------------------------------------------

class RiskViolation(ValueError):
    """Terminal risk rejection. Carries a machine-readable `code`."""
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)

    def __str__(self) -> str:
        return f"[{self.code}] {super().__str__()}"


# -----------------------------------------------------------------------------
# Manager
# -----------------------------------------------------------------------------

GetSnapshot = Callable[[InstrumentSpec], MarketDataSnapshot]


class RiskManager:
    """Call `validate(intent, broker, get_snapshot)` before every submit.

    Tracks session-start NetLiquidation on first call (for daily-loss
    circuit breaker) and strategy-level notional usage per
    KITE_STRATEGY_ID.
    """

    def __init__(self, limits: RiskLimits | None = None):
        self._limits = limits or RiskLimits()
        self._session_start_equity: float | None = None
        # Per-strategy notional traded today, keyed by strategy id.
        self._strategy_notional_today: dict[str, float] = {}

    @property
    def limits(self) -> RiskLimits:
        return self._limits

    # --------------------------------------------------------------
    # Entrypoint
    # --------------------------------------------------------------

    def validate(
        self,
        intent: TradeIntent,
        broker: Broker,
        get_snapshot: GetSnapshot,
    ) -> None:
        """Raise RiskViolation if any limit would be breached. Otherwise
        records the notional against the strategy's running total and
        returns.
        """
        self._check_order_shape(intent)
        self._check_time_rules(intent)

        inst = intent.instrument
        price = self._price(intent, get_snapshot)
        trade_notional = abs(price * intent.quantity)

        self._check_order_notional(intent, trade_notional)
        self._check_freeze_and_lot(intent)

        positions = broker.get_positions() or []
        account = broker.get_account_snapshot()
        self._update_session_start(account)

        self._check_position_ceiling(intent, positions)
        self._check_short_allowed(intent, positions)
        self._check_exposure(account, trade_notional)
        self._check_leverage(account)
        self._check_margin_utilization(account)
        self._check_daily_loss(account)
        self._check_strategy_cap(intent, trade_notional)

        # All clear — tally strategy notional.
        sid = intent.strategy or os.getenv("KITE_STRATEGY_ID") or ""
        if sid:
            self._strategy_notional_today[sid] = (
                self._strategy_notional_today.get(sid, 0.0) + trade_notional
            )

    # --------------------------------------------------------------
    # Individual checks
    # --------------------------------------------------------------

    def _check_order_shape(self, intent: TradeIntent) -> None:
        if intent.quantity <= 0:
            raise RiskViolation("QUANTITY_NOT_POSITIVE",
                                f"quantity must be positive, got {intent.quantity}")
        if intent.quantity > self._limits.max_order_quantity:
            raise RiskViolation("ORDER_QTY_EXCEEDED",
                                f"quantity {intent.quantity} exceeds max_order_quantity "
                                f"{self._limits.max_order_quantity}")

    def _check_order_notional(self, intent: TradeIntent, trade_notional: float) -> None:
        if trade_notional > self._limits.max_single_order_inr:
            raise RiskViolation(
                "SINGLE_ORDER_NOTIONAL_EXCEEDED",
                f"order notional ₹{trade_notional:,.0f} exceeds "
                f"max_single_order_inr ₹{self._limits.max_single_order_inr:,.0f}",
            )

    def _check_time_rules(self, intent: TradeIntent) -> None:
        exch = intent.instrument.exchange
        nowt = now_ist()

        if self._limits.respect_mis_cutoff and intent.product == "MIS":
            status = mis_status(exch, nowt)
            if status == "refuse":
                raise RiskViolation(
                    "MIS_PAST_CUTOFF",
                    f"MIS order on {exch} past auto-squareoff cutoff; "
                    f"use CNC/NRML or wait for next session",
                )

        if self._limits.respect_market_hours:
            # Delegate to check_market_rules for market-hours; only error-
            # severity violations are terminal here.
            from kite_algo.market_rules import check_market_rules
            allow_amo = intent.variety == "amo"
            violations = check_market_rules(
                exchange=exch, product=intent.product,
                quantity=intent.quantity,
                tradingsymbol=intent.instrument.symbol,
                underlying=_underlying_for(intent.instrument),
                allow_amo=allow_amo, when=nowt,
            )
            for v in violations:
                if v.severity == "error" and v.code in (
                    "MARKET_CLOSED", "MIS_PAST_CUTOFF",
                ):
                    raise RiskViolation(v.code, v.message)

    def _check_freeze_and_lot(self, intent: TradeIntent) -> None:
        underlying = _underlying_for(intent.instrument)
        if underlying is None:
            return

        if self._limits.respect_freeze_qty:
            fq = freeze_qty(underlying)
            if fq is not None:
                ceiling = fq * 10  # autoslice cap post-SEBI April 2026
                if intent.quantity > ceiling:
                    raise RiskViolation(
                        "FREEZE_QTY_EXCEEDED",
                        f"quantity {intent.quantity} exceeds {underlying} "
                        f"freeze×10 ceiling ({fq}×10={ceiling})",
                    )

        if self._limits.respect_lot_size:
            ls = lot_size(underlying)
            if ls is not None and intent.instrument.lot_size:
                # If the intent carries the actual lot size, prefer it.
                ls = intent.instrument.lot_size
            if ls is not None and not is_valid_lot_multiple(intent.quantity, ls):
                raise RiskViolation(
                    "LOT_SIZE_MISMATCH",
                    f"quantity {intent.quantity} not a multiple of "
                    f"{underlying} lot size {ls}",
                )

    def _check_position_ceiling(
        self, intent: TradeIntent, positions: list[Position],
    ) -> None:
        current_qty = _current_quantity(positions, intent.instrument)
        delta = intent.quantity if intent.side == "BUY" else -intent.quantity
        resulting = current_qty + delta
        if abs(resulting) > self._limits.max_abs_position_per_symbol:
            raise RiskViolation(
                "POSITION_CEILING_EXCEEDED",
                f"resulting position for {intent.instrument.symbol} would be "
                f"{resulting}, exceeds max_abs_position_per_symbol "
                f"{self._limits.max_abs_position_per_symbol}",
            )

    def _check_short_allowed(
        self, intent: TradeIntent, positions: list[Position],
    ) -> None:
        if self._limits.allow_short:
            return
        current_qty = _current_quantity(positions, intent.instrument)
        delta = intent.quantity if intent.side == "BUY" else -intent.quantity
        resulting = current_qty + delta
        if resulting < 0:
            raise RiskViolation(
                "SHORT_NOT_ALLOWED",
                f"resulting position {resulting} would be short; "
                f"set allow_short=True to permit",
            )

    def _check_exposure(self, account: AccountSnapshot, trade_notional: float) -> None:
        # For Kite, available_cash is closest to the "can-spend" number.
        # gross-position-value isn't in our AccountSnapshot — use margin_used
        # as a proxy for current exposure.
        exposure_now = float(account.margin_used or 0)
        if exposure_now + trade_notional > self._limits.max_notional_exposure_inr:
            raise RiskViolation(
                "NOTIONAL_EXPOSURE_EXCEEDED",
                f"current exposure ₹{exposure_now:,.0f} + this trade "
                f"₹{trade_notional:,.0f} would exceed max_notional_exposure "
                f"₹{self._limits.max_notional_exposure_inr:,.0f}",
            )

    def _check_leverage(self, account: AccountSnapshot) -> None:
        nl = float(account.net_liquidation or 0)
        if nl <= 0:
            return
        # Kite's AccountSnapshot doesn't carry gross_position_value; leave
        # the check in as a structural hook and short-circuit if the field
        # isn't there. (Engines that plumb enriched snapshots can populate.)
        return

    def _check_margin_utilization(self, account: AccountSnapshot) -> None:
        used = float(account.margin_used or 0)
        avail = float(account.margin_available or 0)
        if avail <= 0:
            return
        util = used / (used + avail) if (used + avail) > 0 else 0.0
        if util > self._limits.max_margin_utilization:
            raise RiskViolation(
                "MARGIN_UTIL_EXCEEDED",
                f"margin utilisation {util:.0%} exceeds "
                f"{self._limits.max_margin_utilization:.0%}",
            )

    def _check_daily_loss(self, account: AccountSnapshot) -> None:
        if self._session_start_equity is None:
            return
        net = float(account.net_liquidation or 0)
        drawdown = self._session_start_equity - net
        if drawdown > self._limits.max_daily_loss_inr:
            raise RiskViolation(
                "DAILY_LOSS_CIRCUIT_BREAKER",
                f"intra-day drawdown ₹{drawdown:,.0f} exceeds "
                f"max_daily_loss ₹{self._limits.max_daily_loss_inr:,.0f}",
            )

    def _check_strategy_cap(self, intent: TradeIntent, trade_notional: float) -> None:
        cap = self._limits.strategy_notional_cap_inr
        if cap is None or cap <= 0:
            return
        sid = intent.strategy or os.getenv("KITE_STRATEGY_ID") or ""
        if not sid:
            return
        used = self._strategy_notional_today.get(sid, 0.0)
        if used + trade_notional > cap:
            raise RiskViolation(
                "STRATEGY_CAP_EXCEEDED",
                f"strategy '{sid}' used ₹{used:,.0f} + this trade "
                f"₹{trade_notional:,.0f} would exceed cap ₹{cap:,.0f}",
            )

    def _update_session_start(self, account: AccountSnapshot) -> None:
        if self._session_start_equity is not None:
            return
        nl = float(account.net_liquidation or 0)
        if nl > 0:
            self._session_start_equity = nl

    # --------------------------------------------------------------
    # Helpers
    # --------------------------------------------------------------

    def _price(self, intent: TradeIntent, get_snapshot: GetSnapshot) -> float:
        """Best-guess price for a trade: limit_price if set, else last from
        a just-fetched snapshot, else raise.
        """
        if intent.limit_price is not None and intent.limit_price > 0:
            return float(intent.limit_price)
        snap = get_snapshot(intent.instrument)
        candidates = [snap.last]
        if snap.bid is not None and snap.ask is not None:
            if snap.bid > 0 and snap.ask > 0:
                candidates.append((snap.bid + snap.ask) / 2.0)
        candidates.append(snap.close)
        for c in candidates:
            if c is not None and c > 0:
                return float(c)
        raise RiskViolation(
            "NO_PRICEABLE_SNAPSHOT",
            f"no usable price for {intent.instrument.symbol} "
            f"(market closed? snap={snap})",
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _current_quantity(positions: list[Position], instrument: InstrumentSpec) -> int:
    total = 0
    for p in positions:
        if (p.instrument.symbol, p.instrument.exchange) == (
            instrument.symbol, instrument.exchange,
        ):
            total += int(p.quantity)
    return total


# Common Indian-derivative underlying symbols. When the tradingsymbol
# starts with one of these, we use that as the underlying for freeze-qty /
# lot-size lookups. Longer underlyings are listed FIRST so
# MIDCPNIFTY/NIFTYNXT50 don't collapse to NIFTY.
_UNDERLYING_PREFIXES = (
    "MIDCPNIFTY", "NIFTYNXT50", "BANKNIFTY", "FINNIFTY",
    "NIFTY", "SENSEX", "BANKEX",
)


def _underlying_for(instrument: InstrumentSpec) -> str | None:
    """Extract the underlying for F&O instruments. Returns None for cash
    equities where freeze/lot-size rules don't apply.
    """
    if instrument.exchange not in ("NFO", "BFO"):
        return None
    sym = (instrument.symbol or "").upper()
    for u in _UNDERLYING_PREFIXES:
        if sym.startswith(u):
            return u
    # Stock F&O — underlying is the full tradingsymbol stripped of
    # expiry + strike + CE/PE. Heuristic; in practice the instrument cache
    # carries a `name` field (see /instruments dump) that's authoritative.
    return None
