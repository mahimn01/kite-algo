"""Tests for Indian market rules — hours, MIS cutoff, freeze qty, lot size,
weekly expiry, session rotation window.

These are pure functions over data; tests just drive `when` explicitly so
they're deterministic across timezones and DST (India has no DST but the
test host might be anywhere).
"""

from __future__ import annotations

from datetime import date, datetime, time

import pytest

from kite_algo.market_rules import (
    IST,
    MIS_SQUAREOFF_CUTOFF_EQUITY,
    Session,
    check_market_rules,
    ensure_ist,
    freeze_qty,
    in_token_rotation_window,
    is_market_open,
    is_valid_lot_multiple,
    lot_size,
    market_close_time,
    market_open_time,
    max_slicable_qty,
    mis_cutoff_for,
    mis_status,
    next_weekly_expiry,
    safe_login_time_today,
    weekly_expiry_weekday,
)


def _ist(y, m, d, h=10, mi=0) -> datetime:
    return datetime(y, m, d, h, mi, tzinfo=IST)


# -----------------------------------------------------------------------------
# is_market_open
# -----------------------------------------------------------------------------

class TestIsMarketOpen:
    def test_nse_equity_open_midday_tuesday(self) -> None:
        # 2026-04-21 was a Tuesday.
        assert is_market_open("NSE", _ist(2026, 4, 21, 12, 0)) is True

    def test_nse_closed_before_open(self) -> None:
        assert is_market_open("NSE", _ist(2026, 4, 21, 9, 0)) is False

    def test_nse_open_at_exact_open_time(self) -> None:
        assert is_market_open("NSE", _ist(2026, 4, 21, 9, 15)) is True

    def test_nse_open_at_exact_close_time(self) -> None:
        assert is_market_open("NSE", _ist(2026, 4, 21, 15, 30)) is True

    def test_nse_closed_after_close(self) -> None:
        assert is_market_open("NSE", _ist(2026, 4, 21, 15, 31)) is False

    def test_nse_closed_weekend(self) -> None:
        # 2026-04-18 Sat, 2026-04-19 Sun
        assert is_market_open("NSE", _ist(2026, 4, 18, 12, 0)) is False
        assert is_market_open("NSE", _ist(2026, 4, 19, 12, 0)) is False

    def test_mcx_open_late_evening(self) -> None:
        # MCX runs until 23:30
        assert is_market_open("MCX", _ist(2026, 4, 21, 22, 0)) is True

    def test_mcx_closed_after_2330(self) -> None:
        assert is_market_open("MCX", _ist(2026, 4, 21, 23, 45)) is False

    def test_cds_closed_at_1800(self) -> None:
        assert is_market_open("CDS", _ist(2026, 4, 21, 18, 0)) is False

    def test_naive_datetime_treated_as_ist(self) -> None:
        dt = datetime(2026, 4, 21, 12, 0)  # naive
        assert is_market_open("NSE", dt) is True

    def test_unknown_exchange_raises(self) -> None:
        with pytest.raises(ValueError):
            is_market_open("FAKE", _ist(2026, 4, 21, 12, 0))


class TestMarketTimes:
    def test_nse_open_close(self) -> None:
        assert market_open_time("NSE") == time(9, 15)
        assert market_close_time("NSE") == time(15, 30)

    def test_mcx_times(self) -> None:
        assert market_open_time("MCX") == time(9, 0)
        assert market_close_time("MCX") == time(23, 30)


# -----------------------------------------------------------------------------
# MIS cutoff
# -----------------------------------------------------------------------------

class TestMISCutoff:
    def test_ok_early_morning(self) -> None:
        assert mis_status("NSE", _ist(2026, 4, 21, 10, 0)) == "ok"

    def test_warn_at_1505(self) -> None:
        assert mis_status("NSE", _ist(2026, 4, 21, 15, 5)) == "warn"

    def test_refuse_at_1520(self) -> None:
        assert mis_status("NSE", _ist(2026, 4, 21, 15, 20)) == "refuse"

    def test_refuse_at_1525(self) -> None:
        assert mis_status("NSE", _ist(2026, 4, 21, 15, 25)) == "refuse"

    def test_mcx_has_later_cutoff(self) -> None:
        assert mis_status("MCX", _ist(2026, 4, 21, 15, 25)) == "ok"
        assert mis_status("MCX", _ist(2026, 4, 21, 23, 10)) == "warn"
        assert mis_status("MCX", _ist(2026, 4, 21, 23, 26)) == "refuse"

    def test_mis_cutoff_for_equity(self) -> None:
        assert mis_cutoff_for("NSE") == MIS_SQUAREOFF_CUTOFF_EQUITY


# -----------------------------------------------------------------------------
# Freeze quantity
# -----------------------------------------------------------------------------

class TestFreezeQty:
    def test_nifty_freeze_known(self) -> None:
        assert freeze_qty("NIFTY") == 1800

    def test_case_insensitive(self) -> None:
        assert freeze_qty("nifty") == 1800
        assert freeze_qty("Nifty") == 1800

    def test_unknown_underlying_returns_none(self) -> None:
        """Unknown = defer to server. Conservative default would false-positive."""
        assert freeze_qty("SOMESTRANGESYMBOL") is None

    def test_banknifty_freeze(self) -> None:
        assert freeze_qty("BANKNIFTY") == 900

    def test_sensex_freeze(self) -> None:
        assert freeze_qty("SENSEX") == 1000

    def test_max_slicable_ten_times_freeze(self) -> None:
        """Post-SEBI April 2026: autoslice caps at 10 legs."""
        assert max_slicable_qty("NIFTY") == 18_000
        assert max_slicable_qty("BANKNIFTY") == 9_000

    def test_max_slicable_unknown(self) -> None:
        assert max_slicable_qty("UNKNOWN") is None


# -----------------------------------------------------------------------------
# Lot size
# -----------------------------------------------------------------------------

class TestLotSize:
    def test_nifty_lot_75(self) -> None:
        assert lot_size("NIFTY") == 75

    def test_banknifty_lot_30(self) -> None:
        assert lot_size("BANKNIFTY") == 30

    def test_unknown(self) -> None:
        assert lot_size("FOO") is None

    def test_is_valid_multiple(self) -> None:
        assert is_valid_lot_multiple(75, 75) is True
        assert is_valid_lot_multiple(150, 75) is True
        assert is_valid_lot_multiple(225, 75) is True

    def test_is_invalid_multiple(self) -> None:
        assert is_valid_lot_multiple(74, 75) is False
        assert is_valid_lot_multiple(100, 75) is False

    def test_zero_invalid(self) -> None:
        assert is_valid_lot_multiple(0, 75) is False

    def test_negative_invalid(self) -> None:
        assert is_valid_lot_multiple(-75, 75) is False

    def test_invalid_lot_size_raises(self) -> None:
        with pytest.raises(ValueError):
            is_valid_lot_multiple(75, 0)


# -----------------------------------------------------------------------------
# Weekly expiry
# -----------------------------------------------------------------------------

class TestWeeklyExpiry:
    def test_nse_tuesday(self) -> None:
        """NIFTY weekly is Tuesday (post Sep 2025)."""
        assert weekly_expiry_weekday("NSE") == 1
        assert weekly_expiry_weekday("NFO") == 1

    def test_bse_thursday(self) -> None:
        """SENSEX weekly is Thursday."""
        assert weekly_expiry_weekday("BSE") == 3
        assert weekly_expiry_weekday("BFO") == 3

    def test_non_derivatives_none(self) -> None:
        assert weekly_expiry_weekday("MCX") is None
        assert weekly_expiry_weekday("CDS") is None

    def test_next_weekly_from_monday(self) -> None:
        # 2026-04-20 Mon → next Tue is 2026-04-21
        assert next_weekly_expiry("NSE", date(2026, 4, 20)) == date(2026, 4, 21)

    def test_next_weekly_from_tuesday_skips_to_next_week(self) -> None:
        """On the expiry day itself, 'next' means next week (strict future)."""
        assert next_weekly_expiry("NSE", date(2026, 4, 21)) == date(2026, 4, 28)

    def test_next_weekly_from_wed_wraps(self) -> None:
        # Wed 2026-04-22 → next Tue is 2026-04-28
        assert next_weekly_expiry("NSE", date(2026, 4, 22)) == date(2026, 4, 28)

    def test_next_weekly_for_bse_thursday(self) -> None:
        # Tue 2026-04-21 → next Thu is 2026-04-23
        assert next_weekly_expiry("BSE", date(2026, 4, 21)) == date(2026, 4, 23)


# -----------------------------------------------------------------------------
# Token rotation window
# -----------------------------------------------------------------------------

class TestTokenRotationWindow:
    def test_in_window(self) -> None:
        assert in_token_rotation_window(_ist(2026, 4, 21, 7, 0)) is True

    def test_before_window(self) -> None:
        assert in_token_rotation_window(_ist(2026, 4, 21, 6, 30)) is False

    def test_after_window(self) -> None:
        assert in_token_rotation_window(_ist(2026, 4, 21, 7, 45)) is False

    def test_at_window_boundaries(self) -> None:
        assert in_token_rotation_window(_ist(2026, 4, 21, 6, 45)) is True
        assert in_token_rotation_window(_ist(2026, 4, 21, 7, 30)) is True

    def test_safe_login_time_is_0730(self) -> None:
        safe = safe_login_time_today(_ist(2026, 4, 21, 5, 0))
        assert safe.time() == time(7, 30)
        assert safe.date() == date(2026, 4, 21)


# -----------------------------------------------------------------------------
# check_market_rules — integration
# -----------------------------------------------------------------------------

class TestCheckMarketRules:
    def test_clean_nse_order_mid_session(self) -> None:
        v = check_market_rules(
            exchange="NSE", product="CNC", quantity=1,
            tradingsymbol="RELIANCE",
            when=_ist(2026, 4, 21, 12, 0),
        )
        assert v == []

    def test_nse_order_weekend_blocked(self) -> None:
        v = check_market_rules(
            exchange="NSE", product="CNC", quantity=1,
            when=_ist(2026, 4, 18, 12, 0),  # Sat
        )
        codes = [x.code for x in v]
        assert "MARKET_CLOSED" in codes

    def test_nse_order_weekend_amo_allowed(self) -> None:
        """With allow_amo, weekend should not trigger MARKET_CLOSED."""
        v = check_market_rules(
            exchange="NSE", product="CNC", quantity=1,
            allow_amo=True,
            when=_ist(2026, 4, 18, 12, 0),
        )
        codes = [x.code for x in v]
        assert "MARKET_CLOSED" not in codes

    def test_mis_after_cutoff_blocked(self) -> None:
        v = check_market_rules(
            exchange="NSE", product="MIS", quantity=1,
            when=_ist(2026, 4, 21, 15, 25),
        )
        codes = [x.code for x in v]
        assert "MIS_PAST_CUTOFF" in codes
        assert any(x.severity == "error" for x in v)

    def test_mis_near_cutoff_warns_only(self) -> None:
        v = check_market_rules(
            exchange="NSE", product="MIS", quantity=1,
            when=_ist(2026, 4, 21, 15, 10),
        )
        codes = [x.code for x in v]
        assert "MIS_APPROACHING_CUTOFF" in codes
        assert all(x.severity != "error" for x in v)

    def test_freeze_qty_exceeds_single_order_warns(self) -> None:
        v = check_market_rules(
            exchange="NFO", product="NRML", quantity=2000,
            underlying="NIFTY",
            when=_ist(2026, 4, 21, 12, 0),
        )
        # 2000 > 1800 (single-order freeze), but < 18000 (autoslice ceiling)
        codes = [x.code for x in v]
        assert "FREEZE_QTY_AUTOSLICE" in codes
        assert all(x.severity != "error" for x in v if x.code == "FREEZE_QTY_AUTOSLICE")

    def test_freeze_qty_exceeds_autoslice_blocks(self) -> None:
        v = check_market_rules(
            exchange="NFO", product="NRML", quantity=20_000,
            underlying="NIFTY",
            when=_ist(2026, 4, 21, 12, 0),
        )
        # 20_000 > 18_000 autoslice ceiling
        codes = [x.code for x in v]
        assert "FREEZE_QTY_EXCEEDED" in codes
        assert any(x.severity == "error" for x in v if x.code == "FREEZE_QTY_EXCEEDED")

    def test_lot_size_mismatch_blocks(self) -> None:
        v = check_market_rules(
            exchange="NFO", product="NRML", quantity=100,  # NIFTY lot=75
            underlying="NIFTY",
            when=_ist(2026, 4, 21, 12, 0),
        )
        codes = [x.code for x in v]
        assert "LOT_SIZE_MISMATCH" in codes

    def test_valid_nifty_lot_multiple_passes(self) -> None:
        v = check_market_rules(
            exchange="NFO", product="NRML", quantity=150,  # 2 × 75
            underlying="NIFTY",
            when=_ist(2026, 4, 21, 12, 0),
        )
        assert all(x.code != "LOT_SIZE_MISMATCH" for x in v)


# -----------------------------------------------------------------------------
# Session unit
# -----------------------------------------------------------------------------

class TestSession:
    def test_contains_within(self) -> None:
        s = Session(time(9, 15), time(15, 30))
        assert s.contains(time(12, 0))

    def test_contains_boundaries_inclusive(self) -> None:
        s = Session(time(9, 15), time(15, 30))
        assert s.contains(time(9, 15))
        assert s.contains(time(15, 30))

    def test_outside_window(self) -> None:
        s = Session(time(9, 15), time(15, 30))
        assert not s.contains(time(15, 31))


class TestEnsureIst:
    def test_naive_becomes_ist(self) -> None:
        dt = datetime(2026, 4, 21, 12, 0)
        out = ensure_ist(dt)
        assert out.tzinfo == IST

    def test_utc_shifts_to_ist(self) -> None:
        from datetime import timezone
        dt = datetime(2026, 4, 21, 6, 30, tzinfo=timezone.utc)  # 12:00 IST
        out = ensure_ist(dt)
        assert out.hour == 12
        assert out.minute == 0
