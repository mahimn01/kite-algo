"""Fill model — slippage in bps, configurable bar-relative timing.

Slippage adds on buys, subtracts on sells (worst-case execution). For
NIFTY futures, 1.5 bps default ≈ ₹3 on a ₹22k contract — realistic for
liquid front-month at TBT depth.
"""

from __future__ import annotations

import pandas as pd

from kite_algo.backtest.models import Bar


_VALID_FILL_AT = {"next_bar_open", "this_bar_close"}


class FillModel:
    def __init__(self, slippage_bps_per_side: float, fill_at: str) -> None:
        if slippage_bps_per_side < 0:
            raise ValueError(f"slippage_bps must be >= 0, got {slippage_bps_per_side}")
        if fill_at not in _VALID_FILL_AT:
            raise ValueError(f"fill_at must be one of {_VALID_FILL_AT}, got {fill_at!r}")
        self.slippage = slippage_bps_per_side / 10_000.0
        self.fill_at = fill_at

    def fill_buy(self, bar_t: Bar, bar_t1: Bar | None) -> tuple[float, pd.Timestamp]:
        if self.fill_at == "next_bar_open":
            if bar_t1 is None:
                # No next bar — fall back to close (used at end-of-data forced exits).
                return bar_t.close * (1.0 + self.slippage), bar_t.ts
            return bar_t1.open * (1.0 + self.slippage), bar_t1.ts
        return bar_t.close * (1.0 + self.slippage), bar_t.ts

    def fill_sell(self, bar_t: Bar, bar_t1: Bar | None) -> tuple[float, pd.Timestamp]:
        if self.fill_at == "next_bar_open":
            if bar_t1 is None:
                return bar_t.close * (1.0 - self.slippage), bar_t.ts
            return bar_t1.open * (1.0 - self.slippage), bar_t1.ts
        return bar_t.close * (1.0 - self.slippage), bar_t.ts
