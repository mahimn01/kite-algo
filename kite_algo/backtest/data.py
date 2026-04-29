"""Parquet loaders for Nifty 1H/1D and India VIX. UTC tz-aware index, IST view column."""

from __future__ import annotations

import logging
from datetime import timedelta, timezone
from pathlib import Path

import pandas as pd


log = logging.getLogger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))
_DEFAULT_DATA_DIR = Path(__file__).resolve().parents[2] / "data"

# 9-15 IST covers regular cash session 09:15-15:30; bars labeled by their start
# hour. Drops Diwali Muhurat (18:00-19:30 IST) and any one-off special sessions.
_RTH_HOURS_IST = {9, 10, 11, 12, 13, 14, 15}

_REQUIRED_OHLCV = ["open", "high", "low", "close", "volume"]


def _validate(df: pd.DataFrame, label: str) -> None:
    if df.empty:
        raise ValueError(f"{label}: empty DataFrame")
    missing = [c for c in _REQUIRED_OHLCV if c not in df.columns]
    if missing:
        raise ValueError(f"{label}: missing columns {missing}")
    if df.index.has_duplicates:
        raise ValueError(f"{label}: duplicate timestamps in index")
    if not df.index.is_monotonic_increasing:
        raise ValueError(f"{label}: index not monotonic increasing")
    nan_cols = [c for c in _REQUIRED_OHLCV if df[c].isna().any()]
    if nan_cols:
        raise ValueError(f"{label}: NaN values in columns {nan_cols}")


def _read_ohlcv(path: Path, label: str) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"{label}: file not found at {path}")
    df = pd.read_parquet(path)

    # Find timestamp column (handle index already set, or 'timestamp'/'date' column).
    if not isinstance(df.index, pd.DatetimeIndex):
        ts_col = next((c for c in ("timestamp", "ts", "date", "datetime") if c in df.columns), None)
        if ts_col is None:
            raise ValueError(f"{label}: no timestamp column or DatetimeIndex")
        df = df.set_index(ts_col)
    df.index = pd.to_datetime(df.index, utc=True)
    df = df.sort_index()

    df.columns = [c.lower() for c in df.columns]
    return df


class DataLoader:
    @staticmethod
    def load_nifty_1h(
        path: Path | None = None,
        skip_offhours: bool = True,
    ) -> pd.DataFrame:
        path = path or (_DEFAULT_DATA_DIR / "nifty_1h.parquet")
        df = _read_ohlcv(path, "nifty_1h")

        df["ts_ist"] = df.index.tz_convert(_IST)
        if skip_offhours:
            mask = df["ts_ist"].dt.hour.isin(_RTH_HOURS_IST)
            n_dropped = (~mask).sum()
            if n_dropped:
                log.info("nifty_1h: dropped %d off-hours bars", int(n_dropped))
            df = df.loc[mask]

        if "volume" not in df.columns:
            df["volume"] = 0
        df["volume"] = df["volume"].fillna(0).astype("int64")

        _validate(df[_REQUIRED_OHLCV], "nifty_1h")
        return df[_REQUIRED_OHLCV + ["ts_ist"]]

    @staticmethod
    def load_nifty_daily(path: Path | None = None) -> pd.DataFrame:
        path = path or (_DEFAULT_DATA_DIR / "nifty_1d.parquet")
        df = _read_ohlcv(path, "nifty_1d")
        if "volume" not in df.columns:
            df["volume"] = 0
        df["volume"] = df["volume"].fillna(0).astype("int64")
        _validate(df[_REQUIRED_OHLCV], "nifty_1d")
        return df[_REQUIRED_OHLCV]

    @staticmethod
    def load_india_vix_daily(path: Path | None = None) -> pd.DataFrame:
        path = path or (_DEFAULT_DATA_DIR / "india_vix_1d.parquet")
        df = _read_ohlcv(path, "india_vix_1d")
        if "volume" not in df.columns:
            df["volume"] = 0
        df["volume"] = df["volume"].fillna(0).astype("int64")
        _validate(df[_REQUIRED_OHLCV], "india_vix_1d")
        return df[_REQUIRED_OHLCV]
