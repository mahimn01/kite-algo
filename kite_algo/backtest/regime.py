"""Regime tagger — vol bucket × trend bucket × time-of-day, plus event flags.

Hardcoded event windows for major Indian-market regime markers. Tags are
applied per-bar by `tag_for(ts)`; the engine attributes each closed trade
to the regime composite of its entry bar.
"""

from __future__ import annotations

from datetime import date, timedelta, timezone

import pandas as pd

from kite_algo.backtest.indicators import ema
from kite_algo.backtest.models import RegimeTag


_IST = timezone(timedelta(hours=5, minutes=30))


_EVENT_WINDOWS: tuple[tuple[str, date, date], ...] = (
    ("covid_crash", date(2020, 3, 9), date(2020, 4, 30)),
    ("demonetization", date(2016, 11, 8), date(2016, 11, 18)),
    ("adani_hindenburg", date(2023, 1, 24), date(2023, 2, 10)),
    ("loksabha_2024", date(2024, 6, 4), date(2024, 6, 4)),
    ("yen_carry_unwind", date(2024, 8, 5), date(2024, 8, 5)),
)


def _vol_bucket(vix: float) -> str:
    if vix < 14.0:
        return "vix_low"
    if vix <= 22.0:
        return "vix_mid"
    return "vix_high"


def _time_of_day(hour_ist: int) -> str:
    if hour_ist <= 9:
        return "open_hour"
    if hour_ist >= 14:
        return "close_hour"
    return "mid"


class RegimeTagger:
    def __init__(self, vix_daily_df: pd.DataFrame, daily_df: pd.DataFrame) -> None:
        if "close" not in vix_daily_df.columns or "close" not in daily_df.columns:
            raise ValueError("vix_daily_df and daily_df must both have a 'close' column")

        # Use prior-day close so the tag at time t doesn't peek at intraday data.
        self._vix_by_date: dict[date, float] = {
            ts.date(): float(c) for ts, c in vix_daily_df["close"].items()
        }

        ema200 = ema(daily_df["close"], 200)
        self._trend_by_date: dict[date, str] = {}
        for ts, close in daily_df["close"].items():
            ema_v = ema200.loc[ts]
            if pd.isna(ema_v):
                self._trend_by_date[ts.date()] = "bull"  # default before EMA seeds
            else:
                self._trend_by_date[ts.date()] = "bull" if float(close) > float(ema_v) else "bear"

        # Pre-build a sorted list of (date, vix) for prior-day lookup.
        self._sorted_vix_dates: list[date] = sorted(self._vix_by_date.keys())

    def _prior_vix(self, d: date) -> float | None:
        # Binary search via bisect would be cleaner but list is short; linear is fine.
        prior: float | None = None
        for vd in self._sorted_vix_dates:
            if vd >= d:
                break
            prior = self._vix_by_date[vd]
        return prior

    def _prior_trend(self, d: date) -> str:
        # Walk back up to 7 calendar days to find the last available trading day.
        for delta in range(0, 8):
            probe = d - timedelta(days=delta)
            if probe in self._trend_by_date:
                return self._trend_by_date[probe]
        return "bull"

    def _event_flag(self, d: date) -> str:
        for name, start, end in _EVENT_WINDOWS:
            if start <= d <= end:
                return name
        return "none"

    def tag_for(self, ts: pd.Timestamp) -> RegimeTag:
        if ts.tzinfo is None:
            raise ValueError(f"ts must be tz-aware, got naive {ts}")
        ts_ist = ts.tz_convert(_IST)
        d = ts_ist.date()
        hour = ts_ist.hour

        vix = self._prior_vix(d)
        vol_bucket = _vol_bucket(vix) if vix is not None else "vix_mid"
        trend_bucket = self._prior_trend(d)
        tod = _time_of_day(hour)
        event = self._event_flag(d)
        composite = f"{vol_bucket}/{trend_bucket}/{tod}"

        return RegimeTag(
            vol_bucket=vol_bucket,
            trend_bucket=trend_bucket,
            time_of_day=tod,
            event_flag=event,
            composite_key=composite,
        )
