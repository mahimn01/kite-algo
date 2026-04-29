"""Indian cost model — post-Oct 2024 SEBI rates.

Rates are intentionally hardcoded and not env-tunable: they're regulatory
inputs, not strategy knobs. Update them in one place when SEBI revises.

Per-leg API: pass `(price, qty_units, side, lot_size)`. For futures,
qty_units is contracts (lots) and notional = price * qty * lot_size. For
ETF, lot_size=1 and qty_units = shares.
"""

from __future__ import annotations

from kite_algo.backtest.models import CostBreakdown


_GST_RATE = 0.18


class IndianCostModel:
    def __init__(self, mode: str) -> None:
        if mode not in {"futures", "etf", "options", "none"}:
            raise ValueError(
                f"mode must be one of futures/etf/options/none, got {mode!r}"
            )
        self.mode = mode

    def compute_cost(
        self,
        price: float,
        qty_units: int,
        side: str,
        lot_size: int = 1,
    ) -> CostBreakdown:
        if side not in {"buy", "sell"}:
            raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")
        if price <= 0 or qty_units <= 0 or lot_size <= 0:
            raise ValueError(
                f"price/qty/lot_size must be positive: "
                f"price={price}, qty={qty_units}, lot_size={lot_size}"
            )

        if self.mode == "none":
            return CostBreakdown(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        if self.mode == "options":
            raise NotImplementedError(
                "options cost model not yet implemented "
                "(expected: STT 0.1% on premium sell, exchange 0.0353% on premium)"
            )

        notional = price * qty_units * lot_size
        is_buy = side == "buy"

        if self.mode == "futures":
            brokerage = 20.0
            stt = 0.0 if is_buy else 0.0002 * notional
            exchange = 0.0000173 * notional
            sebi = 0.000001 * notional
            stamp = 0.00002 * notional if is_buy else 0.0
            ipft = 0.000001 * notional
            dp_charge = 0.0
        elif self.mode == "etf":
            brokerage = 0.0
            stt = 0.001 * notional
            exchange = 0.0000297 * notional
            sebi = 0.000001 * notional
            stamp = 0.00015 * notional if is_buy else 0.0
            ipft = 0.000001 * notional
            dp_charge = 0.0 if is_buy else 15.93
        else:
            raise AssertionError(f"unreachable: mode={self.mode}")

        gst = _GST_RATE * (brokerage + exchange + sebi)
        total = brokerage + stt + exchange + sebi + stamp + ipft + dp_charge + gst

        return CostBreakdown(
            brokerage=brokerage,
            stt=stt,
            exchange=exchange,
            sebi=sebi,
            stamp=stamp,
            ipft=ipft,
            dp_charge=dp_charge,
            gst=gst,
            total=total,
        )

    def round_trip_cost(
        self,
        entry_price: float,
        exit_price: float,
        qty_units: int,
        lot_size: int = 1,
    ) -> CostBreakdown:
        buy = self.compute_cost(entry_price, qty_units, "buy", lot_size)
        sell = self.compute_cost(exit_price, qty_units, "sell", lot_size)
        return CostBreakdown(
            brokerage=buy.brokerage + sell.brokerage,
            stt=buy.stt + sell.stt,
            exchange=buy.exchange + sell.exchange,
            sebi=buy.sebi + sell.sebi,
            stamp=buy.stamp + sell.stamp,
            ipft=buy.ipft + sell.ipft,
            dp_charge=buy.dp_charge + sell.dp_charge,
            gst=buy.gst + sell.gst,
            total=buy.total + sell.total,
        )
