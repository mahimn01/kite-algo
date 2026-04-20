"""Row projection (--fields) + summary rollups (--summary).

Agent-driven calls frequently care about a small subset of a command's
response:

- `orders --summary` → just `{total, by_status, oldest_open_timestamp}`.
- `chain --summary` → `{atm_strike, atm_iv_ce, atm_iv_pe, put_call_oi_ratio,
  max_pain, total_oi}`.
- `holdings --summary` → `{count, total_invested_inr, total_pnl_inr,
  worst_performer, best_performer}`.
- `positions --summary` → `{open_count, day_m2m, net_pnl}`.

Without `--summary` the list endpoints emit 100+ rows × 15 fields each,
which is 50 KB of JSON that the agent then has to read into its context
window at cost. With `--summary` the same call fits in 500 bytes.

`--fields a,b,c` is complementary: list mode with only the named columns
kept. Used when the agent knows exactly which fields it needs (e.g.
`orders --fields order_id,status,average_price`).
"""

from __future__ import annotations

from collections import Counter
from typing import Any


# -----------------------------------------------------------------------------
# Field projection
# -----------------------------------------------------------------------------

def project_rows(rows: list[dict], fields: list[str] | None) -> list[dict]:
    """Return a new list where each row keeps only the named keys.

    Missing fields are included as `None` so the output shape stays stable
    across rows (important for CSV header consistency).  Pass `None` or an
    empty list to get the rows unchanged.
    """
    if not fields:
        return rows
    return [{f: r.get(f) for f in fields} for r in rows]


def parse_fields(raw: str | None) -> list[str] | None:
    """`"a,b,c"` → `["a","b","c"]`; `""` / `None` → None."""
    if not raw:
        return None
    out = [s.strip() for s in raw.split(",") if s.strip()]
    return out or None


# -----------------------------------------------------------------------------
# Summary rollups
# -----------------------------------------------------------------------------

def summarize_orders(orders: list[dict]) -> dict:
    """Compact rollup of today's orders list.

    {
      total: int,
      by_status: {OPEN: N, COMPLETE: N, ...},
      open_count: int,
      oldest_open_timestamp: str | null,
      total_buy_value: float,
      total_sell_value: float,
    }
    """
    if not orders:
        return {
            "total": 0, "by_status": {}, "open_count": 0,
            "oldest_open_timestamp": None,
            "total_buy_value": 0.0, "total_sell_value": 0.0,
        }
    by_status = Counter(o.get("status") or "UNKNOWN" for o in orders)
    active_states = {"OPEN", "TRIGGER PENDING"}
    open_orders = [o for o in orders if o.get("status") in active_states]
    oldest = None
    if open_orders:
        oldest = min(
            (o.get("order_timestamp") for o in open_orders if o.get("order_timestamp")),
            default=None,
        )

    def _val(o: dict) -> float:
        # Rough value = quantity × price (LIMIT) or average_price (filled).
        qty = float(o.get("quantity") or 0)
        px = float(o.get("price") or o.get("average_price") or 0)
        return qty * px

    buy_v = sum(_val(o) for o in orders if o.get("transaction_type") == "BUY")
    sell_v = sum(_val(o) for o in orders if o.get("transaction_type") == "SELL")
    return {
        "total": len(orders),
        "by_status": dict(by_status),
        "open_count": len(open_orders),
        "oldest_open_timestamp": str(oldest) if oldest else None,
        "total_buy_value": round(buy_v, 2),
        "total_sell_value": round(sell_v, 2),
    }


def summarize_holdings(holdings: list[dict]) -> dict:
    """{count, total_invested_inr, total_value_inr, total_pnl_inr,
       day_pnl_inr, best_performer, worst_performer}."""
    if not holdings:
        return {
            "count": 0, "total_invested_inr": 0.0,
            "total_value_inr": 0.0, "total_pnl_inr": 0.0, "day_pnl_inr": 0.0,
            "best_performer": None, "worst_performer": None,
        }
    total_invested = 0.0
    total_value = 0.0
    total_pnl = 0.0
    total_day_pnl = 0.0
    best = (None, float("-inf"))
    worst = (None, float("inf"))
    for h in holdings:
        qty = float(h.get("quantity") or 0)
        avg = float(h.get("average_price") or 0)
        last = float(h.get("last_price") or 0)
        invested = qty * avg
        value = qty * last
        pnl = float(h.get("pnl") or (value - invested))
        day_change = float(h.get("day_change") or 0) * qty
        total_invested += invested
        total_value += value
        total_pnl += pnl
        total_day_pnl += day_change
        pnl_pct = ((last - avg) / avg * 100) if avg > 0 else 0.0
        sym = h.get("tradingsymbol") or "?"
        if pnl_pct > best[1]:
            best = (sym, pnl_pct)
        if pnl_pct < worst[1]:
            worst = (sym, pnl_pct)
    return {
        "count": len(holdings),
        "total_invested_inr": round(total_invested, 2),
        "total_value_inr": round(total_value, 2),
        "total_pnl_inr": round(total_pnl, 2),
        "day_pnl_inr": round(total_day_pnl, 2),
        "best_performer": {"symbol": best[0], "pnl_pct": round(best[1], 2)} if best[0] else None,
        "worst_performer": {"symbol": worst[0], "pnl_pct": round(worst[1], 2)} if worst[0] else None,
    }


def summarize_positions(payload: dict) -> dict:
    """Positions payload is `{net: [...], day: [...]}`; summarize both."""
    net = payload.get("net", []) if isinstance(payload, dict) else []
    day = payload.get("day", []) if isinstance(payload, dict) else []
    open_net = [p for p in net if int(p.get("quantity") or 0) != 0]

    day_m2m = sum(float(p.get("m2m") or 0) for p in day)
    net_pnl = sum(float(p.get("pnl") or 0) for p in net)
    realised = sum(float(p.get("realised") or 0) for p in net)
    unrealised = sum(float(p.get("unrealised") or 0) for p in net)
    return {
        "open_count": len(open_net),
        "net_count": len(net),
        "day_count": len(day),
        "day_m2m_inr": round(day_m2m, 2),
        "net_pnl_inr": round(net_pnl, 2),
        "realised_inr": round(realised, 2),
        "unrealised_inr": round(unrealised, 2),
    }


def summarize_option_chain(chain: list[dict], spot: float | None = None) -> dict:
    """{atm_strike, atm_ce_iv, atm_pe_iv, put_call_oi_ratio, max_pain,
        total_ce_oi, total_pe_oi, strike_count}.

    All based on the chain rows as emitted by `cmd_chain --quote --greeks`.
    spot = last price of the underlying (if available) — used to identify
    the ATM strike.
    """
    if not chain:
        return {
            "strike_count": 0, "atm_strike": None,
            "total_ce_oi": 0, "total_pe_oi": 0,
            "put_call_oi_ratio": None, "max_pain": None,
            "atm_ce_iv": None, "atm_pe_iv": None,
        }
    ce_rows = [r for r in chain if r.get("right") == "CE"]
    pe_rows = [r for r in chain if r.get("right") == "PE"]
    total_ce_oi = sum(int(r.get("oi") or 0) for r in ce_rows)
    total_pe_oi = sum(int(r.get("oi") or 0) for r in pe_rows)
    put_call_oi_ratio = (total_pe_oi / total_ce_oi) if total_ce_oi else None

    strikes = sorted({float(r.get("strike") or 0) for r in chain if r.get("strike")})

    # ATM: nearest strike to spot (if spot known), else middle of range.
    atm_strike = None
    if strikes:
        if spot:
            atm_strike = min(strikes, key=lambda s: abs(s - spot))
        else:
            atm_strike = strikes[len(strikes) // 2]

    atm_ce_iv = None
    atm_pe_iv = None
    if atm_strike is not None:
        for r in chain:
            if float(r.get("strike") or 0) == atm_strike:
                if r.get("right") == "CE":
                    atm_ce_iv = r.get("iv")
                elif r.get("right") == "PE":
                    atm_pe_iv = r.get("iv")

    # Max pain: strike where total intrinsic loss to option writers is
    # minimised. Standard formula: sum over all strikes K of
    #   put_oi(K) × max(0, K_expiry - S) + call_oi(K) × max(0, S - K_expiry)
    # evaluated at each candidate S; the min is max pain.
    max_pain = None
    if strikes and total_ce_oi + total_pe_oi > 0:
        def pain_at(s: float) -> float:
            p = 0.0
            for r in chain:
                strike = float(r.get("strike") or 0)
                oi = int(r.get("oi") or 0)
                if r.get("right") == "CE" and s > strike:
                    p += oi * (s - strike)
                elif r.get("right") == "PE" and s < strike:
                    p += oi * (strike - s)
            return p
        max_pain = min(strikes, key=pain_at)

    return {
        "strike_count": len(strikes),
        "atm_strike": atm_strike,
        "total_ce_oi": total_ce_oi,
        "total_pe_oi": total_pe_oi,
        "put_call_oi_ratio": round(put_call_oi_ratio, 3) if put_call_oi_ratio is not None else None,
        "max_pain": max_pain,
        "atm_ce_iv": atm_ce_iv,
        "atm_pe_iv": atm_pe_iv,
    }
