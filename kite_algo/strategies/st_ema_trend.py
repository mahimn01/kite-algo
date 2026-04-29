"""Pine 1:1 port of "Jaimin — ST Nifty + EMA200 Filter".

Pine semantics (Pine v5 strategy, `process_orders_on_close=false` default):
    turns_green = st_green and not st_green[1]
    turns_red   = st_red   and not st_red[1]
    f200 = use_e200 ? close > ema200 : true
    f50  = use_e50  ? close > ema50  : true

    if turns_green and f200 and f50 and position_size == 0:
        entry("Buy", long)              # fills at next bar open
    if position_size > 0 and turns_red:
        close("Buy")                    # fills at next bar open

Match this exactly. Indicators are computed once on the full series at
construction time so on_bar lookup is O(1) (engine's history slice is the
same DataFrame; we just key by timestamp).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from kite_algo.backtest.indicators import ema, supertrend
from kite_algo.backtest.models import Bar, Signal


class JaiminSTEMAStrategy:
    name: str = "jaimin_st_ema200"

    def __init__(
        self,
        full_df: pd.DataFrame,
        st_period: int = 10,
        st_mult: float = 3.0,
        use_ema200: bool = True,
        use_ema50: bool = False,
    ) -> None:
        if not isinstance(full_df.index, pd.DatetimeIndex):
            raise ValueError("full_df must have a DatetimeIndex")
        if full_df.index.tz is None:
            raise ValueError("full_df index must be tz-aware")
        if not full_df.index.is_monotonic_increasing:
            raise ValueError("full_df index must be monotonic increasing")

        self.st_period = st_period
        self.st_mult = st_mult
        self.use_ema200 = use_ema200
        self.use_ema50 = use_ema50

        st = supertrend(full_df, period=st_period, mult=st_mult)
        is_green = st["is_green"].to_numpy(dtype=bool)
        # turns_green / turns_red on this bar (Pine: st_green and not st_green[1])
        turns_green = np.zeros_like(is_green)
        turns_red = np.zeros_like(is_green)
        if len(is_green) > 1:
            turns_green[1:] = is_green[1:] & (~is_green[:-1])
            turns_red[1:] = (~is_green[1:]) & is_green[:-1]

        ema200 = ema(full_df["close"], 200).to_numpy(dtype=np.float64)
        ema50 = ema(full_df["close"], 50).to_numpy(dtype=np.float64)
        close = full_df["close"].to_numpy(dtype=np.float64)

        f200 = (close > ema200) if use_ema200 else np.ones_like(close, dtype=bool)
        f50 = (close > ema50) if use_ema50 else np.ones_like(close, dtype=bool)
        # Pine: filters use the *current* bar's close vs the *current* bar's EMA.
        # During EMA warmup (NaN), filter passes only if explicitly disabled.
        if use_ema200:
            f200 = f200 & ~np.isnan(ema200)
        if use_ema50:
            f50 = f50 & ~np.isnan(ema50)

        # Pre-baked decision arrays, indexed by row position.
        self._entry_trigger = turns_green & f200 & f50
        self._exit_trigger = turns_red

        # Index for O(1) timestamp → row lookup.
        self._index = full_df.index
        # We rely on the engine passing exactly this df (same row positions).
        # Verify alignment lazily on first on_bar call.
        self._aligned: bool | None = None

    def on_bar(self, bar: Bar, history: pd.DataFrame) -> list[Signal]:
        # `history` is df.iloc[: i+1] — the last row's index is bar.ts.
        i = len(history) - 1
        if self._aligned is None:
            # One-time sanity check: history must be a prefix of our precomputed series.
            if i >= len(self._index) or self._index[i] != bar.ts:
                raise ValueError(
                    f"strategy was constructed on a different df than the engine is iterating "
                    f"(idx mismatch at i={i})"
                )
            self._aligned = True

        signals: list[Signal] = []
        if self._exit_trigger[i]:
            signals.append(Signal(action="sell", reason="st_red"))
        if self._entry_trigger[i]:
            signals.append(Signal(action="buy", reason="st_green_ema200"))
        return signals
