"""Pre-flight order validation.

Validates order parameters before they hit the Kite API. Catches the most
common mistakes locally — saves an API round-trip and a rate-limit token,
and produces clearer error messages than the server's InputException.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

VALID_EXCHANGES = frozenset({"NSE", "BSE", "NFO", "BFO", "MCX", "CDS", "BCD"})
VALID_TRANSACTION_TYPES = frozenset({"BUY", "SELL"})
VALID_ORDER_TYPES = frozenset({"MARKET", "LIMIT", "SL", "SL-M"})
VALID_PRODUCTS = frozenset({"CNC", "NRML", "MIS", "MTF"})
VALID_VARIETIES = frozenset({"regular", "amo", "co", "iceberg", "auction"})
VALID_VALIDITIES = frozenset({"DAY", "IOC", "TTL"})

# Kite tradingsymbol limits (per exchange docs).
MAX_TRADINGSYMBOL_LENGTH = 50
# Kite tag limit.
MAX_TAG_LENGTH = 20
# Conservative default cap to catch accidental `--quantity 100000` typos.
# Override via env KITE_MAX_QUANTITY if you genuinely need larger clip sizes.
DEFAULT_MAX_QUANTITY = 100_000

# Iceberg cap post-SEBI April 2026 circular: legs dropped from 50 → 10.
# autoslice (server-side variety) also capped at 10.
ICEBERG_MIN_LEGS = 2
ICEBERG_MAX_LEGS = 10

# Max order modifications per order_id lifetime. Kite support says ~20–25;
# we use 20 as a conservative ceiling to avoid the "Maximum allowed order
# modifications exceeded" InputException.
MAX_MODIFICATIONS_PER_ORDER = 20


def _max_quantity() -> int:
    try:
        return int(os.getenv("KITE_MAX_QUANTITY", str(DEFAULT_MAX_QUANTITY)))
    except ValueError:
        return DEFAULT_MAX_QUANTITY

# Exchanges × product compatibility (per Kite docs).
# - CNC: equity only (NSE, BSE)
# - MIS: intraday; supported on NSE/BSE equity + F&O + commodities
# - NRML: normal; F&O (NFO, BFO), commodity (MCX), currency (CDS, BCD)
# - MTF: margin trading; NSE/BSE equity only
EQUITY_EXCHANGES = {"NSE", "BSE"}
FNO_EXCHANGES = {"NFO", "BFO"}
COMMODITY_EXCHANGES = {"MCX"}
CURRENCY_EXCHANGES = {"CDS", "BCD"}


@dataclass(frozen=True)
class ValidationError:
    field: str
    message: str

    def __str__(self) -> str:
        return f"{self.field}: {self.message}"


def validate_order(
    *,
    exchange: str,
    tradingsymbol: str,
    transaction_type: str,
    order_type: str,
    quantity: int,
    product: str,
    variety: str = "regular",
    price: float | None = None,
    trigger_price: float | None = None,
    validity: str = "DAY",
    validity_ttl: int | None = None,
    disclosed_quantity: int | None = None,
    iceberg_legs: int | None = None,
    iceberg_quantity: int | None = None,
    tag: str | None = None,
    market_protection: float | None = None,
) -> list[ValidationError]:
    """Return a list of validation errors. Empty list = valid."""
    errs: list[ValidationError] = []

    # --- enum fields ------------------------------------------------------
    if exchange not in VALID_EXCHANGES:
        errs.append(ValidationError("exchange", f"must be one of {sorted(VALID_EXCHANGES)}"))
    if transaction_type not in VALID_TRANSACTION_TYPES:
        errs.append(ValidationError("transaction_type", "must be BUY or SELL"))
    if order_type not in VALID_ORDER_TYPES:
        errs.append(ValidationError("order_type", f"must be one of {sorted(VALID_ORDER_TYPES)}"))
    if product not in VALID_PRODUCTS:
        errs.append(ValidationError("product", f"must be one of {sorted(VALID_PRODUCTS)}"))
    if variety not in VALID_VARIETIES:
        errs.append(ValidationError("variety", f"must be one of {sorted(VALID_VARIETIES)}"))
    if validity not in VALID_VALIDITIES:
        errs.append(ValidationError("validity", f"must be one of {sorted(VALID_VALIDITIES)}"))

    # --- trivial fields ---------------------------------------------------
    if not tradingsymbol or not isinstance(tradingsymbol, str):
        errs.append(ValidationError("tradingsymbol", "required non-empty string"))
    elif len(tradingsymbol) > MAX_TRADINGSYMBOL_LENGTH:
        errs.append(ValidationError("tradingsymbol", f"max {MAX_TRADINGSYMBOL_LENGTH} chars"))
    elif " " in tradingsymbol or not all(c.isalnum() or c in "-_&" for c in tradingsymbol):
        errs.append(ValidationError("tradingsymbol", "must be alphanumeric (with optional - _ &)"))

    if not isinstance(quantity, int) or quantity <= 0:
        errs.append(ValidationError("quantity", "must be a positive int"))
    elif quantity > _max_quantity():
        errs.append(ValidationError(
            "quantity",
            f"exceeds guardrail {_max_quantity()} (override via env KITE_MAX_QUANTITY)",
        ))

    # --- price/trigger_price rules ---------------------------------------
    if order_type == "LIMIT" and (price is None or price <= 0):
        errs.append(ValidationError("price", "required and must be > 0 for LIMIT orders"))
    if order_type in ("SL", "SL-M") and (trigger_price is None or trigger_price <= 0):
        errs.append(ValidationError("trigger_price", f"required for {order_type} orders"))
    if order_type == "SL" and (price is None or price <= 0):
        errs.append(ValidationError("price", "required for SL orders (limit price)"))
    if order_type == "MARKET" and price is not None:
        errs.append(ValidationError("price", "must not be set for MARKET orders"))
    if order_type in ("MARKET", "LIMIT") and trigger_price is not None:
        errs.append(ValidationError("trigger_price", f"must not be set for {order_type} orders"))

    # --- product × exchange compatibility --------------------------------
    if product == "CNC" and exchange not in EQUITY_EXCHANGES:
        errs.append(ValidationError("product", f"CNC only valid on equity exchanges ({sorted(EQUITY_EXCHANGES)})"))
    if product == "MTF" and exchange not in EQUITY_EXCHANGES:
        errs.append(ValidationError("product", "MTF only valid on NSE/BSE equity"))
    if product == "NRML" and exchange in EQUITY_EXCHANGES:
        errs.append(ValidationError("product", "NRML not valid for equity (use CNC or MIS)"))

    # --- validity rules --------------------------------------------------
    if validity == "TTL":
        if validity_ttl is None or validity_ttl <= 0:
            errs.append(ValidationError("validity_ttl", "required (in minutes) when validity=TTL"))
    else:
        if validity_ttl is not None:
            errs.append(ValidationError("validity_ttl", f"must not be set when validity={validity}"))

    # --- iceberg rules ---------------------------------------------------
    # Post-SEBI April 2026: max 10 legs (was 50). Iceberg is also nonsensical
    # for delivery/MTF products where whole quantity must settle together.
    if variety == "iceberg":
        if product in ("CNC", "MTF"):
            errs.append(ValidationError(
                "product",
                f"iceberg variety is not supported for {product} — use MIS or NRML",
            ))
        if iceberg_legs is None or not (ICEBERG_MIN_LEGS <= iceberg_legs <= ICEBERG_MAX_LEGS):
            errs.append(ValidationError(
                "iceberg_legs",
                f"must be between {ICEBERG_MIN_LEGS} and {ICEBERG_MAX_LEGS} "
                f"(post-SEBI April 2026)",
            ))
        if iceberg_quantity is None or iceberg_quantity <= 0:
            errs.append(ValidationError("iceberg_quantity", "required and > 0 for iceberg"))
        if iceberg_legs and iceberg_quantity and iceberg_legs * iceberg_quantity != quantity:
            errs.append(ValidationError(
                "iceberg_quantity",
                f"iceberg_legs × iceberg_quantity ({iceberg_legs}×{iceberg_quantity}) must equal quantity ({quantity})",
            ))
    else:
        if iceberg_legs is not None or iceberg_quantity is not None:
            errs.append(ValidationError("iceberg_*", f"iceberg params not valid for variety={variety}"))

    # --- disclosed quantity ----------------------------------------------
    if disclosed_quantity is not None:
        if disclosed_quantity <= 0 or disclosed_quantity > quantity:
            errs.append(ValidationError("disclosed_quantity", "must be > 0 and ≤ quantity"))

    # --- market_protection (MANDATORY for MARKET/SL-M per SEBI Apr 2026) -
    # `market_protection` bounds how far MARKET / SL-M orders can slip against
    # the LTP. Post-SEBI April 2026, OMS REJECTS market orders without it
    # (market_protection=0 → reject). Use -1 (auto), or a positive percent
    # (e.g. 1.0 means +/- 1% of LTP).
    if order_type in ("MARKET", "SL-M"):
        if market_protection is None:
            errs.append(ValidationError(
                "market_protection",
                f"{order_type} orders MUST carry market_protection (SEBI April "
                f"2026). Use -1 for Kite auto, or a positive percent like 1.0.",
            ))
        elif market_protection != -1 and market_protection <= 0:
            errs.append(ValidationError(
                "market_protection",
                f"must be -1 (auto) or > 0, got {market_protection}",
            ))
    else:
        if market_protection is not None and market_protection != -1:
            # LIMIT / SL carry an explicit price — market_protection is
            # meaningless there. Allow -1 as a harmless default passthrough.
            errs.append(ValidationError(
                "market_protection",
                f"only applies to MARKET and SL-M orders, not {order_type}",
            ))

    # --- tag rules -------------------------------------------------------
    # Per Kite docs: alphanumeric, max 20 chars. We accept _ and - as common
    # developer separators; Kite accepts them in practice.
    if tag is not None:
        if not tag:
            errs.append(ValidationError("tag", "must not be empty if provided"))
        elif len(tag) > MAX_TAG_LENGTH:
            errs.append(ValidationError("tag", f"max {MAX_TAG_LENGTH} characters"))
        elif not all(c.isalnum() or c in "_-" for c in tag):
            errs.append(ValidationError("tag", "must be alphanumeric (with optional _ or -)"))

    return errs


def format_errors(errors: list[ValidationError]) -> str:
    return "\n  ".join(["Order validation failed:"] + [str(e) for e in errors])
