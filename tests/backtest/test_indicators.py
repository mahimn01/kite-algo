"""Hand-computed Supertrend / ATR / EMA reference values."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from kite_algo.backtest.indicators import ema, supertrend, wilder_atr


def _make_df(highs: list[float], lows: list[float], closes: list[float]) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=len(highs), freq="h", tz="UTC")
    return pd.DataFrame(
        {"open": closes, "high": highs, "low": lows, "close": closes, "volume": [0] * len(highs)},
        index=idx,
    )


def test_wilder_atr_5bar_period_2() -> None:
    df = _make_df(
        highs=[100, 102, 103, 105, 103],
        lows=[99, 100, 101, 102, 98],
        closes=[99.5, 101.5, 102.5, 104, 99],
    )
    atr = wilder_atr(df, period=2).to_numpy()
    # Hand computed:
    # TR=[1, 2.5, 2, 3, 6]; seed ATR[1]=avg(1,2.5)=1.75; Wilder smoothing thereafter.
    expected = [math.nan, 1.75, 1.875, 2.4375, 4.21875]
    assert math.isnan(atr[0])
    for i in range(1, 5):
        assert atr[i] == pytest.approx(expected[i], abs=1e-9)


def test_supertrend_5bar_reference() -> None:
    df = _make_df(
        highs=[100, 102, 103, 105, 103],
        lows=[99, 100, 101, 102, 98],
        closes=[99.5, 101.5, 102.5, 104, 99],
    )
    st = supertrend(df, period=2, mult=2.0)

    # Hand-computed (see analysis): ST stays on upper band 104.5 from t=1 onward;
    # direction stays +1 (downtrend / red) for the whole series.
    st_value = st["st_value"].to_numpy()
    direction = st["direction"].to_numpy()

    assert math.isnan(st_value[0])
    expected_st = [None, 104.5, 104.5, 104.5, 104.5]
    expected_dir = [0, 1, 1, 1, 1]
    for i in range(1, 5):
        assert st_value[i] == pytest.approx(expected_st[i], abs=1e-9)
        assert int(direction[i]) == expected_dir[i]


def test_ema_seed_and_smoothing() -> None:
    s = pd.Series([10.0, 12.0, 14.0, 16.0, 18.0])
    e = ema(s, period=3).to_numpy()
    # Seed = SMA of first 3 = 12.
    # alpha = 2/4 = 0.5.
    # e[3] = 0.5*16 + 0.5*12 = 14
    # e[4] = 0.5*18 + 0.5*14 = 16
    assert math.isnan(e[0]) and math.isnan(e[1])
    assert e[2] == pytest.approx(12.0, abs=1e-12)
    assert e[3] == pytest.approx(14.0, abs=1e-12)
    assert e[4] == pytest.approx(16.0, abs=1e-12)


def test_supertrend_flips_to_green_on_break() -> None:
    # Construct a clear flip: stable-ish then a strong upmove crossing the upper band.
    highs = [100, 100, 100, 100, 100, 110, 115]
    lows = [98, 98, 98, 98, 98, 108, 113]
    closes = [99, 99, 99, 99, 99, 109, 114]
    df = _make_df(highs, lows, closes)
    st = supertrend(df, period=2, mult=1.5)
    # Somewhere in last 2 bars we expect a flip_to_green when close pierces the upper band.
    assert st["flip_to_green"].any()


def test_wilder_atr_invalid_period() -> None:
    df = _make_df([1, 2], [0, 1], [0.5, 1.5])
    with pytest.raises(ValueError):
        wilder_atr(df, period=0)


def test_supertrend_empty_input() -> None:
    df = pd.DataFrame({"open": [], "high": [], "low": [], "close": [], "volume": []})
    out = supertrend(df, period=10, mult=3.0)
    assert len(out) == 0
    assert {"st_value", "direction", "is_green", "is_red", "flip_to_green", "flip_to_red"}.issubset(out.columns)
