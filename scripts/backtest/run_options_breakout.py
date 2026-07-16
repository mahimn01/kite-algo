"""Prev-day breakout intraday-options study, priced BSM-on-real-ticks.

Demonstrates `kite_algo.backtest.options_synth` end-to-end on a real strategy:

  Signal : on the first 1-min CLOSE beyond the previous session's high/low,
           between 09:45 and 14:00 IST.
  Long   : buy the ATM option in the break direction (PE on a low-break,
           CE on a high-break).
  Fade   : SELL that ATM option (or a defined-risk credit spread) — bet the
           break fails.
  Exit   : +target / -stop option-points, else square-off at the close.

Option prices are reconstructed with Black-Scholes on the REAL NIFTY 1-min
ticks (India VIX as IV, time-to-expiry decaying intraday) because expired
weekly contracts are de-listed — see `options_synth` for the method and its
~18% under-pricing caveat. Costs use `IndianCostModel("options")` plus a
per-side slippage assumption.

FINDING (78 sessions, Mar-Jun 2026): neither side is a regime-AGNOSTIC edge.
Each is a bet on the day's regime — the long profits when breakouts trend, the
fade when they fail — so the net sign flips with the window's regime mix, which
you cannot predict in advance:
  • Fade: net-NEGATIVE after costs over the full sample (gross ~zero). Its
    "+Rs20k / 76% win" on a recent 30-session window was range-bound bias.
  • Long: net-POSITIVE over this trend-heavy window, but it LOSES in range
    regimes (e.g. the range-bound May-Jun sub-window: ~-Rs21k) and runs an
    uncapped tail with no stop. Trend-luck, not a durable edge.
Conclusion: do not trade either mechanically — there is no edge that survives
across regimes. Run this before trusting any "it worked yesterday" intraday idea.

Needs a live Kite session (data/session.json) — minute history is pulled live,
not from Parquet. Reads only; places no orders.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from collections import defaultdict
from statistics import mean

import pandas as pd

from kite_algo.backtest.costs import IndianCostModel
from kite_algo.backtest.options_synth import atm_strike, price_at, years_to_expiry
from kite_algo.config import KiteConfig
from kite_algo.greeks import bs_price
from kite_algo.kite_tool import _import_kiteconnect

_ENTRY_START = dt.time(9, 45)
_ENTRY_END = dt.time(14, 0)
_R = 0.065


def _client():
    cfg = KiteConfig.from_env()
    kc = _import_kiteconnect()
    sess = json.load(open("data/session.json"))
    client = kc(api_key=cfg.api_key)
    client.set_access_token(sess["access_token"])
    return client


def _naive(ts: dt.datetime) -> dt.datetime:
    return ts.replace(tzinfo=None) if ts.tzinfo else ts


def _next_tuesday(d: dt.date) -> dt.date:
    return d + dt.timedelta(days=(1 - d.weekday()) % 7)


def _regime(bar: dict) -> str:
    rng = bar["high"] - bar["low"]
    return "TREND" if rng > 0 and abs(bar["close"] - bar["open"]) / rng >= 0.55 else "RANGE"


def run(args: argparse.Namespace) -> None:
    client = _client()
    nf = [i for i in client.instruments("NSE") if i["tradingsymbol"] == "NIFTY 50"][0]["instrument_token"]
    vix = [i for i in client.instruments("NSE") if i["tradingsymbol"] == "INDIA VIX"][0]["instrument_token"]
    start = dt.datetime.fromisoformat(args.start)
    end = dt.datetime.fromisoformat(args.end)

    daily = client.historical_data(nf, start, end, "day")
    vixd = {str(c["date"].date()): c["close"] for c in client.historical_data(vix, start, end, "day")}
    cost_model = IndianCostModel("options")
    lot = args.lot

    rows: list[dict] = []
    for i in range(1, len(daily)):
        prev, day = daily[i - 1], daily[i]
        d = day["date"].date()
        try:
            mins = client.historical_data(
                nf, dt.datetime(d.year, d.month, d.day, 9, 15),
                dt.datetime(d.year, d.month, d.day, 15, 30), "minute",
            )
        except Exception:
            continue
        if not mins:
            continue
        iv = vixd.get(str(prev["date"].date()), 15.0) / 100.0
        exp = _next_tuesday(d)
        exp_ts = pd.Timestamp(dt.datetime(exp.year, exp.month, exp.day, 15, 30))

        trade = None
        for c in mins:
            t = _naive(c["date"]).time()
            if t < _ENTRY_START:
                continue
            if t > _ENTRY_END:
                break
            if c["close"] < prev["low"]:
                trade = ("PE", c)
                break
            if c["close"] > prev["high"]:
                trade = ("CE", c)
                break
        if trade is None:
            rows.append({"side": None, "pnl": 0.0, "reg": _regime(day)})
            continue

        right, bc = trade
        strike = atm_strike(bc["close"])
        t0 = pd.Timestamp(_naive(bc["date"]))
        entry = bs_price(bc["close"], strike, years_to_expiry(t0, exp_ts), _R, iv, right)
        long = args.strategy == "long"

        exit_px = None
        is_stop = False
        for c in [c for c in mins if c["date"] > bc["date"]]:
            now = pd.Timestamp(_naive(c["date"]))
            ttx = years_to_expiry(now, exp_ts)
            # option value at the bar's favourable / adverse spot extreme for THIS position
            if long:
                fav_s, adv_s = (c["low"], c["high"]) if right == "PE" else (c["high"], c["low"])
            else:
                fav_s, adv_s = (c["high"], c["low"]) if right == "PE" else (c["low"], c["high"])
            fav = bs_price(fav_s, strike, ttx, _R, iv, right)
            adv = bs_price(adv_s, strike, ttx, _R, iv, right)
            gain = (fav - entry) if long else (entry - fav)
            loss = (entry - adv) if long else (adv - entry)
            if loss >= args.stop:
                exit_px = (entry - args.stop) if long else (entry + args.stop)
                is_stop = True
                break
            if gain >= args.target:
                exit_px = (entry + args.target) if long else (entry - args.target)
                break
        if exit_px is None:
            last = [c for c in mins if c["date"] > bc["date"]][-1]
            exit_px = bs_price(last["close"], strike, years_to_expiry(pd.Timestamp(_naive(last["date"])), exp_ts), _R, iv, right)

        gross = (exit_px - entry) * lot if long else (entry - exit_px) * lot
        charges = cost_model.round_trip_cost(max(entry, 0.05), max(exit_px, 0.05), 1, lot).total
        slip = args.slippage_pts * lot * (1.5 if is_stop else 1.0)
        rows.append({"side": right, "pnl": round(gross - charges - slip), "gross": round(gross), "reg": _regime(day)})

    _summary(rows, args)


def _summary(rows: list[dict], args: argparse.Namespace) -> None:
    tr = [r for r in rows if r["side"]]
    if not tr:
        print("no trades in range")
        return
    w = [r for r in tr if r["pnl"] > 0]
    tot = sum(r["pnl"] for r in tr)
    reg: dict[str, float] = defaultdict(float)
    mo: dict[str, float] = defaultdict(float)
    for r in tr:
        reg[r["reg"]] += r["pnl"]
    print(f"\n{args.strategy.upper()}  target={args.target} stop={args.stop} lot={args.lot} slip={args.slippage_pts}pt")
    print(f"  trades={len(tr)}  win%={len(w) / len(tr) * 100:.0f}  gross=Rs{sum(r['gross'] for r in tr):+,.0f}")
    print(f"  NET total=Rs{tot:+,.0f}  exp/trade=Rs{tot / len(tr):+,.0f}")
    print(f"  by regime: RANGE Rs{reg['RANGE']:+,.0f} | TREND Rs{reg['TREND']:+,.0f}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--start", default="2026-03-01")
    p.add_argument("--end", default="2026-06-29")
    p.add_argument("--strategy", choices=["long", "fade"], default="fade")
    p.add_argument("--target", type=float, default=30.0)
    p.add_argument("--stop", type=float, default=40.0)
    p.add_argument("--lot", type=int, default=75)
    p.add_argument("--slippage-pts", type=float, default=2.0)
    run(p.parse_args())


if __name__ == "__main__":
    main()
