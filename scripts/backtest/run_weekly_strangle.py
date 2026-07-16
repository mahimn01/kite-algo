"""Weekly Delta-Strangle Harvest — the mechanical form of the live playbook.

Each week, the first session after the Tuesday weekly expiry, at the close of
the 09:30-09:45 bar: SELL next-expiry NIFTY PE and CE at the delta-target
strikes (default 0.10 — the proxy for "at/behind the 2nd OI wall"; expired
strikes have no fetchable OI, so live execution adds the wall check on top).
Hold to expiry, or manage per --exit. Priced BSM-on-real-ticks (prior-day
India VIX as IV) via `kite_algo.backtest.options_synth`; options cost model
+ 1pt/side slippage.

AUDITED FINDINGS (Jul 2026, two adversarial agents + live-chain validation):
  * Jan-Jun 2026, 25 cycles, 2 lots/side: HOLD +110,697 (96% win, t=+5.01).
    Strike-touch stops DESTROY the edge (8/12 stops were whipsaws, -99k vs
    hold) — the opposite of directional systems, where stops are mandatory.
  * 17-year extension (892 cycles, 2009-2026, incl. COVID): +873,664, 92% win,
    t=+4.24 — the volatility-risk-premium edge is real. Long-run pace is
    ~Rs 979/cycle; H1-2026's 4,428/cycle was a rich regime. Plan on the former.
  * Live validation: BSM(prior-day VIX) OVERSTATES the strangle credit ~6%
    (skew: real puts richer, real calls cheaper). Never price protective wings
    off BSM — it underprices deep-OTM protection 2-3x; use market quotes.
  * TAIL (2 lots, real prices): -5% week = -81k (1-in-23), -8% = -174k
    (1-in-92), -12% = -299k (1-in-350). True intraweek MTM drawdown in-sample
    was -54k. Deployment: max 2 lots TOTAL including discretionary shorts
    (worst-week <= 10% NetLiq); prefer ~400-pt wings (~27% of credit at real
    prices) while credits are rich; naked = consciously owning the 1-in-90 hit.

Needs a live Kite session (data/session.json). Reads only; places no orders.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import time
from collections import defaultdict
from statistics import mean, pstdev

import pandas as pd

from kite_algo.backtest.costs import IndianCostModel
from kite_algo.backtest.options_synth import price_at, strike_at_delta, years_to_expiry
from kite_algo.config import KiteConfig
from kite_algo.kite_tool import _import_kiteconnect

LOT = 65
SLIP = 1.0
R = 0.065


def _client():
    cfg = KiteConfig.from_env()
    kc = _import_kiteconnect()
    sess = json.load(open("data/session.json"))
    client = kc(api_key=cfg.api_key)
    client.set_access_token(sess["access_token"])
    return client


def _fetch(client, args: argparse.Namespace) -> tuple[dict, dict]:
    nf = [i for i in client.instruments("NSE") if i["tradingsymbol"] == "NIFTY 50"][0]["instrument_token"]
    vix = [i for i in client.instruments("NSE") if i["tradingsymbol"] == "INDIA VIX"][0]["instrument_token"]
    start = dt.datetime.fromisoformat(args.start)
    end = dt.datetime.fromisoformat(args.end)
    vixd = {str(c["date"].date()): c["close"] for c in client.historical_data(vix, start - dt.timedelta(days=10), end, "day")}
    daily = client.historical_data(nf, start, end, "day")
    m15: dict[str, list] = {}
    for c in daily:
        d = c["date"].date()
        for _ in range(4):
            try:
                bars = client.historical_data(
                    nf, dt.datetime(d.year, d.month, d.day, 9, 15),
                    dt.datetime(d.year, d.month, d.day, 15, 30), "15minute",
                )
                if bars:
                    m15[str(d)] = [dict(t=str(b["date"])[:19], h=b["high"], l=b["low"], c=b["close"]) for b in bars]
                break
            except Exception:
                time.sleep(1.2)
        time.sleep(0.3)
    if len(m15) < len(daily) - 2:
        raise RuntimeError(f"data loss: {len(m15)}/{len(daily)} sessions fetched")
    return m15, vixd


def run(m15: dict, vixd: dict, tgt: float, lots: int, use_stop: bool, take50: bool) -> list[dict]:
    cost_model = IndianCostModel("options")
    qty = LOT * lots
    sessions = sorted(m15)
    sess_set = set(sessions)
    first = dt.date.fromisoformat(sessions[0])

    def expiry_session(t: dt.date) -> str | None:
        dd = t
        while str(dd) not in sess_set:
            dd -= dt.timedelta(days=1)
            if dd < first:
                return None
        return str(dd)

    tues = [first + dt.timedelta(days=i) for i in range((dt.date.fromisoformat(sessions[-1]) - first).days + 1)]
    expiries = sorted({e for e in (expiry_session(t) for t in tues if t.weekday() == 1) if e})

    cycles = []
    for ei in range(1, len(expiries)):
        prev_exp, exp = expiries[ei - 1], expiries[ei]
        entries = [s for s in sessions if prev_exp < s <= exp]
        if len(entries) < 2 or len(m15[entries[0]]) < 2:
            continue
        eday = entries[0]
        eb = m15[eday][1]
        s0 = eb["c"]
        ets = pd.Timestamp(dt.datetime.fromisoformat(eb["t"]) + dt.timedelta(minutes=15))
        exp_close = pd.Timestamp(dt.datetime.fromisoformat(m15[exp][-1]["t"]) + dt.timedelta(minutes=15))
        pv = sessions[sessions.index(eday) - 1] if sessions.index(eday) > 0 else None
        iv = vixd.get(pv, 15.0) / 100.0

        legs = {}
        for right in ("PE", "CE"):
            k = strike_at_delta(s0, exp_close, ets, iv, right, tgt)
            if k is None:
                continue
            px = price_at(s0, k, exp_close, ets, iv, right)
            legs[right] = dict(K=k, entry=px, open=True, exit=0.0,
                               ecost=cost_model.compute_cost(max(px, 0.05), lots, "sell", LOT).total, xcost=0.0)
        if not legs:
            continue

        for s in [x for x in sessions if eday <= x <= exp]:
            for b in m15[s]:
                ts = pd.Timestamp(dt.datetime.fromisoformat(b["t"]) + dt.timedelta(minutes=15))
                if ts <= ets:
                    continue
                trem = years_to_expiry(ts, exp_close)
                for right, leg in legs.items():
                    if not leg["open"] or trem <= 1e-9:
                        continue
                    touched = (b["l"] <= leg["K"]) if right == "PE" else (b["h"] >= leg["K"])
                    if use_stop and touched:
                        bb = price_at(b["c"], leg["K"], exp_close, ts, iv, right) + SLIP
                        leg.update(exit=bb, open=False, xcost=cost_model.compute_cost(bb, lots, "buy", LOT).total)
                        continue
                    if take50 and price_at(b["c"], leg["K"], exp_close, ts, iv, right) <= 0.5 * leg["entry"]:
                        bb = price_at(b["c"], leg["K"], exp_close, ts, iv, right) + SLIP
                        leg.update(exit=bb, open=False, xcost=cost_model.compute_cost(bb, lots, "buy", LOT).total)

        st = m15[exp][-1]["c"]
        pnl = 0.0
        for right, leg in legs.items():
            if leg["open"]:
                intr = max(0.0, (st - leg["K"]) if right == "CE" else (leg["K"] - st))
                leg["exit"] = intr
                leg["xcost"] = 0.00125 * intr * qty if intr > 0 else 0.0
            pnl += ((leg["entry"] - SLIP) - leg["exit"]) * qty - leg["ecost"] - leg["xcost"]
        cycles.append(dict(exp=exp, pnl=round(pnl)))
    return cycles


def _summary(cycles: list[dict], label: str) -> None:
    if not cycles:
        print(f"{label}: no cycles")
        return
    wins = [c for c in cycles if c["pnl"] > 0]
    tot = sum(c["pnl"] for c in cycles)
    eq = pk = mdd = 0
    for c in cycles:
        eq += c["pnl"]; pk = max(pk, eq); mdd = min(mdd, eq - pk)
    p = [c["pnl"] for c in cycles]
    sd = pstdev(p) if len(p) > 1 else 1.0
    mo: dict[str, float] = defaultdict(float)
    for c in cycles:
        mo[c["exp"][:7]] += c["pnl"]
    print(f"{label}: cycles={len(cycles)} win%={len(wins) / len(cycles) * 100:.0f} "
          f"NET=Rs{tot:+,.0f} exp/cycle=Rs{tot / len(cycles):+,.0f} maxDD=Rs{mdd:,.0f} "
          f"t={mean(p) / (sd / len(p) ** 0.5):+.2f}")
    print(f"  months: {dict((k, round(v)) for k, v in sorted(mo.items()))}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--start", default="2026-01-01")
    p.add_argument("--end", default="2026-06-30")
    p.add_argument("--delta", type=float, default=0.10)
    p.add_argument("--lots", type=int, default=2)
    p.add_argument("--exit", choices=["hold", "stop", "take50", "managed"], default="hold")
    args = p.parse_args()
    m15, vixd = _fetch(_client(), args)
    print(f"sessions: {len(m15)}")
    use_stop = args.exit in ("stop", "managed")
    take50 = args.exit in ("take50", "managed")
    _summary(run(m15, vixd, args.delta, args.lots, use_stop, take50),
             f"delta={args.delta} lots={args.lots} exit={args.exit}")


if __name__ == "__main__":
    main()
