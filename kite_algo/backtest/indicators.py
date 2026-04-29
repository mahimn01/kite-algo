"""Pine v5-faithful indicators (Wilder ATR, EMA, Supertrend).

Vectorized where it's clean (TR, hl2). Supertrend's ratchet has cross-bar
state dependencies, so we run a single tight Python loop over numpy arrays
— still O(n) and fast enough for a 100k-bar backtest.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def wilder_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    if period < 1:
        raise ValueError(f"period must be >= 1, got {period}")
    if not {"high", "low", "close"}.issubset(df.columns):
        raise ValueError("df must have columns high, low, close")
    if len(df) == 0:
        return pd.Series([], index=df.index, dtype=np.float64, name="atr")

    high = df["high"].to_numpy(dtype=np.float64)
    low = df["low"].to_numpy(dtype=np.float64)
    close = df["close"].to_numpy(dtype=np.float64)
    n = len(df)

    prev_close = np.empty(n, dtype=np.float64)
    prev_close[0] = np.nan
    prev_close[1:] = close[:-1]

    tr = np.empty(n, dtype=np.float64)
    tr[0] = high[0] - low[0]
    if n > 1:
        a = high[1:] - low[1:]
        b = np.abs(high[1:] - prev_close[1:])
        c = np.abs(low[1:] - prev_close[1:])
        tr[1:] = np.maximum(np.maximum(a, b), c)

    atr = np.full(n, np.nan, dtype=np.float64)
    if n >= period:
        atr[period - 1] = tr[:period].mean()
        for i in range(period, n):
            atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period

    return pd.Series(atr, index=df.index, name="atr")


def ema(series: pd.Series, period: int) -> pd.Series:
    if period < 1:
        raise ValueError(f"period must be >= 1, got {period}")
    n = len(series)
    if n == 0:
        return pd.Series([], index=series.index, dtype=np.float64, name="ema")

    arr = series.to_numpy(dtype=np.float64)
    out = np.full(n, np.nan, dtype=np.float64)
    if n >= period:
        out[period - 1] = arr[:period].mean()
        alpha = 2.0 / (period + 1)
        for i in range(period, n):
            out[i] = alpha * arr[i] + (1.0 - alpha) * out[i - 1]
    return pd.Series(out, index=series.index, name="ema")


def supertrend(df: pd.DataFrame, period: int = 10, mult: float = 3.0) -> pd.DataFrame:
    if period < 1:
        raise ValueError(f"period must be >= 1, got {period}")
    if mult <= 0:
        raise ValueError(f"mult must be > 0, got {mult}")
    if not {"high", "low", "close"}.issubset(df.columns):
        raise ValueError("df must have columns high, low, close")

    n = len(df)
    if n == 0:
        return pd.DataFrame(
            {
                "st_value": pd.Series([], dtype=np.float64),
                "direction": pd.Series([], dtype=np.int64),
                "is_green": pd.Series([], dtype=bool),
                "is_red": pd.Series([], dtype=bool),
                "flip_to_green": pd.Series([], dtype=bool),
                "flip_to_red": pd.Series([], dtype=bool),
            },
            index=df.index,
        )

    high = df["high"].to_numpy(dtype=np.float64)
    low = df["low"].to_numpy(dtype=np.float64)
    close = df["close"].to_numpy(dtype=np.float64)
    hl2 = (high + low) / 2.0

    atr = wilder_atr(df, period).to_numpy(dtype=np.float64)
    basic_upper = hl2 + mult * atr
    basic_lower = hl2 - mult * atr

    final_upper = np.full(n, np.nan, dtype=np.float64)
    final_lower = np.full(n, np.nan, dtype=np.float64)
    direction = np.zeros(n, dtype=np.int64)
    st_value = np.full(n, np.nan, dtype=np.float64)

    started = False
    for i in range(n):
        if np.isnan(atr[i]):
            continue
        if not started:
            # First bar with valid ATR: seed bands; default to downtrend (+1) per Pine convention.
            final_upper[i] = basic_upper[i]
            final_lower[i] = basic_lower[i]
            direction[i] = 1
            st_value[i] = final_upper[i]
            started = True
            continue

        prev_fu = final_upper[i - 1]
        prev_fl = final_lower[i - 1]
        prev_close = close[i - 1]
        prev_st = st_value[i - 1]

        # Ratchet: upper only descends if last close was below it.
        if basic_upper[i] < prev_fu or prev_close > prev_fu:
            final_upper[i] = basic_upper[i]
        else:
            final_upper[i] = prev_fu

        if basic_lower[i] > prev_fl or prev_close < prev_fl:
            final_lower[i] = basic_lower[i]
        else:
            final_lower[i] = prev_fl

        # Direction flip logic — based on which band was active previously.
        if prev_fu == prev_st:
            direction[i] = -1 if close[i] > final_upper[i] else 1
        else:
            direction[i] = 1 if close[i] < final_lower[i] else -1

        st_value[i] = final_lower[i] if direction[i] < 0 else final_upper[i]

    is_green = direction < 0
    is_red = direction > 0

    flip_to_green = np.zeros(n, dtype=bool)
    flip_to_red = np.zeros(n, dtype=bool)
    if n > 1:
        flip_to_green[1:] = (direction[1:] < 0) & (direction[:-1] >= 0) & (direction[:-1] != 0)
        flip_to_red[1:] = (direction[1:] > 0) & (direction[:-1] <= 0) & (direction[:-1] != 0)

    return pd.DataFrame(
        {
            "st_value": st_value,
            "direction": direction,
            "is_green": is_green,
            "is_red": is_red,
            "flip_to_green": flip_to_green,
            "flip_to_red": flip_to_red,
        },
        index=df.index,
    )
