"""Pre-flight order validation.

Validates order parameters before they hit the Kite API. Catches the most
common mistakes locally — saves an API round-trip and a rate-limit token,
and produces clearer error messages than the server's InputException.
"""

from __future__ import annotations

from dataclasses import dataclass

VALID_EXCHANGES = {"NSE", "BSE", "NFO", "BFO", "MCX", "CDS", "BCD"}
VALID_TRANSACTION_TYPES = {"BUY", "SELL"}
VALID_ORDER_TYPES = {"MARKET", "LIMIT", "SL", "SL-M"}
VALID_PRODUCTS = {"CNC", "NRML", "MIS", "MTF"}
VALID_VARIETIES = {"regular", "amo", "co", "iceberg", "auction"}
VALID_VALIDITIES = {"DAY", "IOC", "TTL"}

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
    if not isinstance(quantity, int) or quantity <= 0:
        errs.append(ValidationError("quantity", "must be a positive int"))

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
    if variety == "iceberg":
        if iceberg_legs is None or not (2 <= iceberg_legs <= 50):
            errs.append(ValidationError("iceberg_legs", "must be between 2 and 50"))
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

    # --- tag rules -------------------------------------------------------
    if tag is not None:
        if len(tag) > 20:
            errs.append(ValidationError("tag", "max 20 characters"))
        if not tag.replace("_", "").replace("-", "").isalnum():
            errs.append(ValidationError("tag", "must be alphanumeric (with optional _ or -)"))

    return errs


def format_errors(errors: list[ValidationError]) -> str:
    return "\n  ".join(["Order validation failed:"] + [str(e) for e in errors])
