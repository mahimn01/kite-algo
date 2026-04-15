"""Instrument specs for Indian market segments."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Exchange = Literal["NSE", "BSE", "NFO", "BFO", "MCX", "CDS"]
Segment = Literal["EQ", "FUT", "CE", "PE", "IND"]
Product = Literal["CNC", "MIS", "NRML"]
ProductNormalized = Literal["equity_delivery", "equity_intraday", "fno_carry"]

PRODUCT_LABELS: dict[Product, ProductNormalized] = {
    "CNC": "equity_delivery",
    "MIS": "equity_intraday",
    "NRML": "fno_carry",
}


@dataclass(frozen=True)
class InstrumentSpec:
    """Symbolic identifier for a tradable Kite instrument.

    The Kite Connect API resolves instruments by `exchange:tradingsymbol`
    (e.g. `NSE:RELIANCE`, `NFO:NIFTY26MAY24000CE`). We also carry the optional
    `instrument_token` so callers can skip lookup if they already have it.
    """

    symbol: str
    exchange: Exchange = "NSE"
    segment: Segment = "EQ"
    instrument_token: int | None = None
    expiry: str | None = None          # YYYY-MM-DD
    strike: float | None = None
    lot_size: int | None = None
    tick_size: float | None = None

    @property
    def kite_key(self) -> str:
        """The `exchange:tradingsymbol` key used by the Kite REST API."""
        return f"{self.exchange}:{self.symbol}"

    @classmethod
    def from_kite_key(cls, key: str) -> "InstrumentSpec":
        if ":" not in key:
            raise ValueError(f"Expected 'EXCHANGE:SYMBOL', got '{key}'")
        exch, sym = key.split(":", 1)
        return cls(symbol=sym, exchange=exch.upper())  # type: ignore[arg-type]

    def with_token(self, token: int) -> "InstrumentSpec":
        return InstrumentSpec(
            symbol=self.symbol,
            exchange=self.exchange,
            segment=self.segment,
            instrument_token=token,
            expiry=self.expiry,
            strike=self.strike,
            lot_size=self.lot_size,
            tick_size=self.tick_size,
        )


def validate_instrument(spec: InstrumentSpec) -> None:
    if not spec.symbol:
        raise ValueError("InstrumentSpec.symbol is required")
    if spec.exchange not in ("NSE", "BSE", "NFO", "BFO", "MCX", "CDS"):
        raise ValueError(f"Unsupported exchange: {spec.exchange}")
    if spec.segment in ("CE", "PE", "FUT"):
        if not spec.expiry:
            raise ValueError(f"{spec.segment} requires an expiry")
        if spec.segment in ("CE", "PE") and spec.strike is None:
            raise ValueError("Option requires a strike")
