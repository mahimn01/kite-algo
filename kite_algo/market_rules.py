"""Indian-market-specific trading rules.

This module encodes the rules an agent must obey when trading on NSE/BSE/NFO
/BFO/MCX/CDS through Kite Connect. Kite's OMS will eventually reject orders
that violate these rules, but the round-trip wastes API quota and produces
opaque `InputException` / `OrderException` messages. Checking locally means
we fail fast with a clear reason.

What's here:

* `is_market_open(exchange, now_ist)` — per-exchange trading hours.
* `mis_cutoff_warning(now_ist)` — MIS positions are auto-squared at 15:20
  IST; agents should stop placing new MIS orders well before that.
* `freeze_qty(underlying)` — freeze-quantity table (NSE publishes a daily
  circular; not exposed via Kite API). Constants as of April 2026 — refresh
  quarterly from NSE's `NSE_FO_contract_ddmmyyyy` circulars.
* `lot_size_multiple_of(quantity, lot_size)` — F&O quantities must be an
  integral multiple of the contract lot size.
* `weekly_expiry_day(exchange)` — current weekly-expiry day of week (NSE:
  Tuesday, BSE: Thursday post-Sep 2025).
* `session_rotation_window_ist()` — daily access-token rotation happens
  between 06:45 and 07:30 IST; code should re-auth after 07:30.

Everything here is pure data + pure functions — no network, no side effects,
no state. Easy to test and easy to update for rule changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal

# -----------------------------------------------------------------------------
# Timezone
# -----------------------------------------------------------------------------

IST = timezone(timedelta(hours=5, minutes=30), name="IST")


def now_ist() -> datetime:
    """Current time in IST (Asia/Kolkata). Always timezone-aware."""
    return datetime.now(tz=IST)


def ensure_ist(dt: datetime) -> datetime:
    """Coerce a naive or UTC `datetime` to IST-aware."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


# -----------------------------------------------------------------------------
# Market hours
# -----------------------------------------------------------------------------

Exchange = Literal["NSE", "BSE", "NFO", "BFO", "MCX", "CDS", "BCD"]


@dataclass(frozen=True)
class Session:
    """A single contiguous trading window (start inclusive, end inclusive).
    All times are IST."""
    start: time
    end: time

    def contains(self, t: time) -> bool:
        # `start <= t <= end` — but has to wrap correctly if end < start
        # (none of the Indian segments cross midnight in practice).
        if self.start <= self.end:
            return self.start <= t <= self.end
        return t >= self.start or t <= self.end


# Regular trading sessions per segment (2026).
#
# NSE / BSE equity:       09:15–15:30
# NFO / BFO derivatives:  09:15–15:30
# MCX commodities:        09:00–23:30 (winter: 09:00–23:55 for energy; we use
#                                      23:30 as the conservative bound.)
# CDS / BCD currencies:   09:00–17:00
#
# Pre-open on equity (09:00–09:15): order entry allowed as AMO / pre-open
# variety. We do not treat pre-open as "market open" for MARKET/LIMIT
# regular orders — that has to go through `variety=amo`.
_REGULAR_SESSIONS: dict[str, tuple[Session, ...]] = {
    "NSE": (Session(time(9, 15), time(15, 30)),),
    "BSE": (Session(time(9, 15), time(15, 30)),),
    "NFO": (Session(time(9, 15), time(15, 30)),),
    "BFO": (Session(time(9, 15), time(15, 30)),),
    "MCX": (Session(time(9, 0), time(23, 30)),),
    "CDS": (Session(time(9, 0), time(17, 0)),),
    "BCD": (Session(time(9, 0), time(17, 0)),),
}


def _is_weekend(d: date) -> bool:
    return d.weekday() >= 5  # Sat=5, Sun=6


def is_market_open(exchange: str, when: datetime | None = None) -> bool:
    """Is `exchange` in its regular trading session at `when` (IST)?

    `when` defaults to now (IST). Weekends always return False. Indian
    national holidays are NOT encoded here — holiday calendars change
    annually and we don't want stale data silently greenlighting an order
    on a holiday. Callers that need holiday-aware checks should layer them
    in via the orchestrator/persistence layer.
    """
    if exchange not in _REGULAR_SESSIONS:
        raise ValueError(f"unknown exchange: {exchange}")
    when = ensure_ist(when) if when is not None else now_ist()
    if _is_weekend(when.date()):
        return False
    t = when.time()
    return any(s.contains(t) for s in _REGULAR_SESSIONS[exchange])


def market_close_time(exchange: str) -> time:
    """Regular-session close time for `exchange`, IST. Raises on unknown."""
    sessions = _REGULAR_SESSIONS.get(exchange)
    if not sessions:
        raise ValueError(f"unknown exchange: {exchange}")
    return sessions[-1].end


def market_open_time(exchange: str) -> time:
    sessions = _REGULAR_SESSIONS.get(exchange)
    if not sessions:
        raise ValueError(f"unknown exchange: {exchange}")
    return sessions[0].start


# -----------------------------------------------------------------------------
# MIS auto-squareoff
# -----------------------------------------------------------------------------

# MIS positions are auto-squared off by Zerodha's RMS at 15:20 IST on the
# same trading day (earlier for commodity: 23:25 IST for MCX).
#
# We use two thresholds:
# - WARN at 15:05  — agent should start winding down MIS exposure.
# - CUTOFF at 15:20 — refuse new MIS orders; they would be auto-squared
#                    before getting a chance to work.
MIS_SQUAREOFF_CUTOFF_EQUITY = time(15, 20)
MIS_SQUAREOFF_WARN_EQUITY = time(15, 5)

# For MCX commodity, MIS auto-squareoff is 23:25 IST (or slightly earlier
# for short-DTE contracts). Conservative warn at 23:10.
MIS_SQUAREOFF_CUTOFF_MCX = time(23, 25)
MIS_SQUAREOFF_WARN_MCX = time(23, 10)


def mis_cutoff_for(exchange: str) -> time:
    if exchange == "MCX":
        return MIS_SQUAREOFF_CUTOFF_MCX
    return MIS_SQUAREOFF_CUTOFF_EQUITY


def mis_warn_for(exchange: str) -> time:
    if exchange == "MCX":
        return MIS_SQUAREOFF_WARN_MCX
    return MIS_SQUAREOFF_WARN_EQUITY


def mis_status(exchange: str, when: datetime | None = None) -> str:
    """Return one of 'ok', 'warn', 'refuse' for placing a MIS order now."""
    when = ensure_ist(when) if when is not None else now_ist()
    t = when.time()
    if t >= mis_cutoff_for(exchange):
        return "refuse"
    if t >= mis_warn_for(exchange):
        return "warn"
    return "ok"


# -----------------------------------------------------------------------------
# Freeze quantity
# -----------------------------------------------------------------------------

# Exchange freeze quantity per underlying — the max quantity that can go
# through in a single order. Kite does NOT expose this via API. These values
# are from NSE's Feb 2026 freeze circular — update quarterly.
#
# Source: https://www1.nseindia.com/content/fo/fo_contract_mkt_wise_pos_limits.xls
# (NSE File: "Market-wise position limits & quantity freeze" circular).
#
# An agent placing F&O orders should compare against `freeze_qty(underlying)`
# — Kite splits via `autoslice`, but autoslice is capped at 10 slices
# post-SEBI, so ceiling = 10 × freeze_qty.
_FREEZE_QTY_2026Q2: dict[str, int] = {
    # Indices (NFO)
    "NIFTY": 1800,
    "BANKNIFTY": 900,
    "FINNIFTY": 1800,
    "MIDCPNIFTY": 4200,
    "NIFTYNXT50": 1800,
    # BFO
    "SENSEX": 1000,
    "BANKEX": 900,
    # Stock F&O top-10 — abbreviated sample; in practice agents should refresh
    # from NSE circular. Listed for the most-traded names only.
    "RELIANCE": 20_000,
    "HDFCBANK": 22_400,
    "TCS": 9_500,
    "INFY": 21_000,
    "ICICIBANK": 18_000,
    "ITC": 72_000,
    "SBIN": 57_000,
    "AXISBANK": 17_500,
    "KOTAKBANK": 10_400,
    "BAJFINANCE": 4_100,
}


def freeze_qty(underlying: str) -> int | None:
    """Return the known freeze quantity for `underlying`, or None if unknown.

    Returning None explicitly signals "I don't know this underlying's freeze
    limit" — caller should allow the order through and let Kite's server
    enforce. Returning a conservative estimate would create false-positive
    rejects on unlisted names.
    """
    return _FREEZE_QTY_2026Q2.get((underlying or "").upper())


def max_slicable_qty(underlying: str) -> int | None:
    """Maximum quantity a single `place` can handle via autoslice.

    Post-SEBI April 2026: autoslice splits into up to 10 legs, each at most
    `freeze_qty`. Returns None when the underlying isn't known.
    """
    fq = freeze_qty(underlying)
    if fq is None:
        return None
    return fq * 10


# -----------------------------------------------------------------------------
# Lot sizes + multiples
# -----------------------------------------------------------------------------

# Current SEBI-mandated lot sizes. Refresh each quarter from NSE's
# `NSE_FO_contract_specifications` circular. These are the post-Nov-2024
# SEBI revision values.
_LOT_SIZES_2026Q2: dict[str, int] = {
    "NIFTY": 75,
    "BANKNIFTY": 30,
    "FINNIFTY": 65,
    "MIDCPNIFTY": 120,
    "NIFTYNXT50": 120,
    "SENSEX": 20,
    "BANKEX": 30,
}


def lot_size(underlying: str) -> int | None:
    """Known lot size for `underlying`. None if unknown — defer to the
    /instruments dump's `lot_size` column in that case."""
    return _LOT_SIZES_2026Q2.get((underlying or "").upper())


def is_valid_lot_multiple(quantity: int, lot_size_value: int) -> bool:
    """F&O quantities must be integer multiples of the lot size. Lot size
    sourced from /instruments dump per-symbol, not hardcoded."""
    if lot_size_value <= 0:
        raise ValueError(f"lot_size must be positive, got {lot_size_value}")
    return quantity > 0 and quantity % lot_size_value == 0


# -----------------------------------------------------------------------------
# Weekly expiry day
# -----------------------------------------------------------------------------

# Post September 1, 2025 SEBI circular:
#   NSE benchmark weekly (NIFTY): Tuesday
#   BSE benchmark weekly (SENSEX): Thursday
# All other weeklies (BANKNIFTY, FINNIFTY, MIDCPNIFTY, BANKEX) have been
# discontinued — only monthly / quarterly remain on those.

_WEEKLY_EXPIRY_WEEKDAY: dict[str, int] = {
    "NSE": 1,  # Tuesday = 1
    "NFO": 1,
    "BSE": 3,  # Thursday = 3
    "BFO": 3,
}


def weekly_expiry_weekday(exchange: str) -> int | None:
    """Monday=0 … Sunday=6. Returns None for non-equity-derivatives exchanges."""
    return _WEEKLY_EXPIRY_WEEKDAY.get(exchange)


def next_weekly_expiry(exchange: str, from_date: date | None = None) -> date | None:
    """Next weekly expiry date for `exchange`, strictly after `from_date`.

    Does NOT account for holidays (an expiry falling on a trading holiday
    shifts to the previous trading day per SEBI). Callers that need
    holiday-aware expiry dates should layer a holiday calendar on top.
    """
    wd = weekly_expiry_weekday(exchange)
    if wd is None:
        return None
    d = from_date or now_ist().date()
    # Days until next weekly weekday; if today IS the weekday we return next
    # week's, not today — "next" is strictly future.
    days_ahead = (wd - d.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return d + timedelta(days=days_ahead)


# -----------------------------------------------------------------------------
# Session rotation window
# -----------------------------------------------------------------------------

# Kite rotates access tokens daily between 06:45 and 07:30 IST (per Zerodha
# support, forum 7610). The safe "session is definitely valid today" signal
# is a successful /profile call after 07:30 IST.
SESSION_ROTATION_START_IST = time(6, 45)
SESSION_ROTATION_END_IST = time(7, 30)


def in_token_rotation_window(when: datetime | None = None) -> bool:
    """Is `when` within the daily access-token rotation window?

    If True, a fresh `login` is recommended before trusting the session.
    Returns True even on weekends — tokens rotate every day, including
    Saturday/Sunday.
    """
    when = ensure_ist(when) if when is not None else now_ist()
    t = when.time()
    return SESSION_ROTATION_START_IST <= t <= SESSION_ROTATION_END_IST


def safe_login_time_today(when: datetime | None = None) -> datetime:
    """Earliest safe login time today (07:30 IST). For scheduling automated
    re-auth."""
    when = ensure_ist(when) if when is not None else now_ist()
    return datetime.combine(when.date(), SESSION_ROTATION_END_IST, tzinfo=IST)


# -----------------------------------------------------------------------------
# Combined pre-flight check
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class MarketRuleViolation:
    code: str         # machine-readable code e.g. "MARKET_CLOSED"
    message: str      # human-readable
    severity: str     # "error" (block) | "warn" (log but allow)


def check_market_rules(
    *,
    exchange: str,
    product: str,
    quantity: int,
    tradingsymbol: str | None = None,
    underlying: str | None = None,
    allow_amo: bool = False,
    when: datetime | None = None,
) -> list[MarketRuleViolation]:
    """Run all market-rule checks for an order about to be placed.

    Returns a list of violations. Empty list = all clear. Callers decide
    whether to block (any `severity="error"`) or merely surface warnings.
    """
    when = ensure_ist(when) if when is not None else now_ist()
    out: list[MarketRuleViolation] = []

    if exchange in _REGULAR_SESSIONS:
        if not is_market_open(exchange, when):
            if not allow_amo:
                out.append(MarketRuleViolation(
                    "MARKET_CLOSED",
                    f"{exchange} regular session is closed at {when.time().isoformat(timespec='minutes')} IST. "
                    f"Use --variety amo for after-market orders.",
                    "error",
                ))

    if product == "MIS":
        status = mis_status(exchange, when)
        if status == "refuse":
            out.append(MarketRuleViolation(
                "MIS_PAST_CUTOFF",
                f"MIS orders on {exchange} after {mis_cutoff_for(exchange).isoformat()} IST "
                f"will be auto-squared immediately. Use CNC/NRML or wait for next session.",
                "error",
            ))
        elif status == "warn":
            out.append(MarketRuleViolation(
                "MIS_APPROACHING_CUTOFF",
                f"MIS auto-squareoff at {mis_cutoff_for(exchange).isoformat()} IST is close. "
                f"Remaining intraday window is short.",
                "warn",
            ))

    if underlying:
        fq = freeze_qty(underlying)
        if fq is not None and quantity > fq:
            max_with_slice = max_slicable_qty(underlying)
            if max_with_slice is not None and quantity > max_with_slice:
                out.append(MarketRuleViolation(
                    "FREEZE_QTY_EXCEEDED",
                    f"quantity {quantity} exceeds {underlying} freeze×10 ceiling "
                    f"({fq} × 10 = {max_with_slice}). Kite autoslice cannot split "
                    f"beyond 10 legs post-SEBI April 2026.",
                    "error",
                ))
            else:
                out.append(MarketRuleViolation(
                    "FREEZE_QTY_AUTOSLICE",
                    f"quantity {quantity} exceeds single-order freeze "
                    f"({underlying} = {fq}); Kite will autoslice into "
                    f"{-(-quantity // fq)} legs.",
                    "warn",
                ))

    if underlying:
        ls = lot_size(underlying)
        if ls is not None and not is_valid_lot_multiple(quantity, ls):
            out.append(MarketRuleViolation(
                "LOT_SIZE_MISMATCH",
                f"quantity {quantity} is not a multiple of {underlying} lot size {ls}.",
                "error",
            ))

    return out
