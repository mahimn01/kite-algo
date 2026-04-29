"""End-to-end evaluation of the Pine 'Jaimin — ST Nifty + EMA200' strategy.

Runs:
1. Single-run baseline on 2015-2026 1H Nifty (ST 10/3, EMA200 filter on)
2. Comparison vs Buy-and-Hold and SMA200-cross benchmarks
3. Parameter sensitivity grid (5 periods × 5 mults) → 25 variants
4. PBO across the variant grid (Bailey/LdP CSCV)
5. Deflated Sharpe with N_trials=25
6. PSR(0) and PSR(BAH-Sharpe)
7. Stationary-bootstrap CIs on Sharpe and Max-DD
8. Monte Carlo on trade returns (10k paths)
9. Walk-forward (2y train / 6mo test, rolling, with purge+embargo)
10. Regime-stratified P&L (vol bucket × trend bucket × time-of-day)
11. Robustness: vary slippage 0.5/1.5/5/15 bps; futures vs ETF cost model

Outputs: text summary + matplotlib figures to /tmp/jaimin_backtest_out/.
"""

from __future__ import annotations

import json
import time
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from kite_algo.backtest.data import DataLoader
from kite_algo.backtest.engine import run_backtest
from kite_algo.backtest.models import BacktestConfig, BacktestResults
from kite_algo.backtest.regime import RegimeTagger
from kite_algo.backtest.reports import generate_report
from kite_algo.backtest.validation.bootstrap import bootstrap_max_dd_ci, bootstrap_sharpe_ci
from kite_algo.backtest.validation.deflated_sharpe import (
    annualized_dsr,
    annualized_psr,
)
from kite_algo.backtest.validation.monte_carlo import monte_carlo_trade_paths
from kite_algo.backtest.validation.pbo import PBOCalculator
from kite_algo.backtest.validation.walk_forward import WalkForwardValidator
from kite_algo.strategies.st_ema_trend import JaiminSTEMAStrategy

OUT = Path("/tmp/jaimin_backtest_out")
OUT.mkdir(parents=True, exist_ok=True)
PERIODS_PER_YEAR = 252.0


def banner(s: str) -> None:
    print()
    print("=" * 80)
    print(s)
    print("=" * 80)


def fmt_metrics(label: str, m) -> str:
    return (
        f"{label:<22s} ret={m.total_return*100:7.2f}%  cagr={m.cagr*100:6.2f}%  "
        f"sharpe={m.sharpe:5.2f}  sortino={m.sortino:5.2f}  calmar={m.calmar:5.2f}  "
        f"mdd={m.max_dd_pct*100:7.2f}%  ulcer={m.ulcer_index:5.2f}  "
        f"trades={m.n_trades:4d}  win={m.win_rate*100:4.1f}%  "
        f"pf={m.profit_factor:5.2f}  exp={m.expectancy_pct*100:5.2f}%  "
        f"costs=₹{m.total_costs_inr:>10,.0f}"
    )


def build_strategy(df: pd.DataFrame, st_period: int = 10, st_mult: float = 3.0,
                   use_ema200: bool = True, use_ema50: bool = False) -> JaiminSTEMAStrategy:
    return JaiminSTEMAStrategy(df, st_period=st_period, st_mult=st_mult,
                               use_ema200=use_ema200, use_ema50=use_ema50)


def main() -> None:
    t0 = time.time()
    print("loading 1H Nifty data ...")
    dl = DataLoader()
    df = dl.load_nifty_1h()
    df_daily = dl.load_nifty_daily()
    df_vix = dl.load_india_vix_daily()
    print(f"  1H bars: {len(df):,}  range: {df.index.min()}  →  {df.index.max()}")
    print(f"  daily bars: {len(df_daily):,}")
    print(f"  vix daily: {len(df_vix):,}")

    regime_tagger = RegimeTagger(vix_daily_df=df_vix, daily_df=df_daily)

    base_cfg = BacktestConfig(
        initial_capital=1_000_000.0,
        instrument="NIFTY_FUT",
        lot_size=75,
        fixed_lots=1,
        sizing_mode="fixed_lots",
        cost_model="futures",
        slippage_bps_per_side=1.5,
        warmup_bars=250,
        risk_free_rate_annual=0.065,
        bars_per_year=1750.0,
    )

    # ── 1. Baseline ───────────────────────────────────────────────────────
    banner("1. BASELINE — Pine 1:1 (ST 10/3 + EMA200, 1H Nifty futures, slippage 1.5 bps)")
    strat = build_strategy(df)
    res = run_backtest(df, strat, base_cfg, regime_tagger=regime_tagger, benchmark="buy_and_hold")
    print(fmt_metrics("STRATEGY", res.metrics))
    if res.benchmark_metrics:
        print(fmt_metrics("BAH (futures, 1x)", res.benchmark_metrics))

    # SMA200 cross benchmark
    sma_res = run_backtest(df, build_strategy(df), base_cfg, regime_tagger=regime_tagger, benchmark="sma200_cross")
    if sma_res.benchmark_metrics:
        print(fmt_metrics("SMA200-cross", sma_res.benchmark_metrics))

    # ── 2. Slippage sensitivity ──────────────────────────────────────────
    banner("2. SLIPPAGE SENSITIVITY (futures, ST 10/3 + EMA200)")
    for bps in (0.5, 1.5, 5.0, 15.0):
        cfg = BacktestConfig(**{**base_cfg.__dict__, "slippage_bps_per_side": bps})
        s = build_strategy(df)
        r = run_backtest(df, s, cfg, regime_tagger=regime_tagger, benchmark=None)
        print(fmt_metrics(f"slippage={bps:>4.1f} bps", r.metrics))

    # ── 3. Cost-model sensitivity (futures vs ETF) ───────────────────────
    banner("3. COST-MODEL SENSITIVITY (slippage=1.5 bps)")
    for mode in ("futures", "etf", "none"):
        cfg = BacktestConfig(**{**base_cfg.__dict__, "cost_model": mode})
        s = build_strategy(df)
        r = run_backtest(df, s, cfg, regime_tagger=regime_tagger, benchmark=None)
        print(fmt_metrics(f"cost={mode}", r.metrics))

    # ── 4. Parameter heatmap ─────────────────────────────────────────────
    banner("4. PARAMETER HEATMAP (5 periods × 5 mults = 25 variants)")
    periods = [7, 10, 14, 20, 30]
    mults = [1.5, 2.0, 3.0, 4.0, 5.0]
    grid_metrics: list[tuple[int, float, object]] = []
    grid_returns: list[np.ndarray] = []  # per-variant daily-return series for PBO
    for p in periods:
        for k in mults:
            s = build_strategy(df, st_period=p, st_mult=k)
            r = run_backtest(df, s, base_cfg, regime_tagger=regime_tagger, benchmark=None)
            grid_metrics.append((p, k, r.metrics))
            grid_returns.append(r.daily_returns)
    print(f"{'period':>8} {'mult':>6} {'sharpe':>7} {'cagr%':>7} {'mdd%':>7} {'trades':>7} {'win%':>6}")
    for p, k, m in grid_metrics:
        print(f"{p:>8d} {k:>6.1f} {m.sharpe:>7.2f} {m.cagr*100:>7.2f} {m.max_dd_pct*100:>7.2f} {m.n_trades:>7d} {m.win_rate*100:>6.1f}")

    # Align all variant return series to common length (truncate to shortest)
    L = min(len(r) for r in grid_returns)
    returns_matrix = np.vstack([r[:L] for r in grid_returns])  # shape (25, L)

    # ── 5. PBO ───────────────────────────────────────────────────────────
    banner("5. PROBABILITY OF BACKTEST OVERFITTING (CSCV, N_groups=16)")
    pbo_calc = PBOCalculator(metric="sharpe", n_groups=16, annualization_factor=PERIODS_PER_YEAR)
    pbo = pbo_calc.calculate(returns_matrix)
    print(f"  PBO            = {pbo.pbo:.3f}  (interpretation: <0.3 healthy, >0.5 overfit)")
    print(f"  IS-OOS rank ρ  = {pbo.rank_correlation_is_oos:.3f}  (positive = consistent)")
    print(f"  variants={pbo.n_variants}, groups={pbo.n_groups}, combos={pbo.n_combinations}")

    # ── 6. Deflated Sharpe + PSR ─────────────────────────────────────────
    banner("6. DEFLATED SHARPE + PSR")
    base_dr = res.daily_returns
    psr_zero = annualized_psr(base_dr, benchmark_annual_sr=0.0, periods_per_year=PERIODS_PER_YEAR)
    bah_sharpe = res.benchmark_metrics.sharpe if res.benchmark_metrics else 0.0
    psr_bah = annualized_psr(base_dr, benchmark_annual_sr=bah_sharpe, periods_per_year=PERIODS_PER_YEAR)
    sr_var_annual = float(np.var(np.array([m.sharpe for _, _, m in grid_metrics], dtype=np.float64), ddof=1))
    dsr, sr0 = annualized_dsr(
        base_dr, n_trials=len(grid_metrics),
        periods_per_year=PERIODS_PER_YEAR, sr_variance_annual=sr_var_annual,
    )
    print(f"  PSR(0)             = {psr_zero:.3f}   (P[true Sharpe > 0])")
    print(f"  PSR(BAH={bah_sharpe:.2f})   = {psr_bah:.3f}   (P[true Sharpe > BAH-Sharpe])")
    print(f"  DSR(N_trials=25)   = {dsr:.3f}   threshold SR0={sr0:.3f}")

    # ── 7. Bootstrap CIs ─────────────────────────────────────────────────
    banner("7. STATIONARY BOOTSTRAP 95% CIs (1000 resamples, mean block 10)")
    sh_lo, sh_hi, sh_pt = bootstrap_sharpe_ci(
        base_dr, periods_per_year=PERIODS_PER_YEAR,
        n_resamples=1000, ci=0.95, mean_block_length=10, seed=42,
    )
    eq_array = res.equity_array
    dd_lo, dd_hi, dd_pt = bootstrap_max_dd_ci(
        eq_array, n_resamples=1000, ci=0.95, mean_block_length=10, seed=42,
    )
    print(f"  Sharpe: point={sh_pt:.3f}  95% CI=[{sh_lo:.3f}, {sh_hi:.3f}]")
    print(f"  MaxDD:  point={dd_pt*100:.2f}%  95% CI=[{dd_lo*100:.2f}%, {dd_hi*100:.2f}%]")

    # ── 8. Monte Carlo ───────────────────────────────────────────────────
    banner("8. MONTE CARLO (trade-return resampling, 10k paths)")
    mc = monte_carlo_trade_paths(
        trade_returns=res.trade_returns,
        initial_capital=base_cfg.initial_capital,
        n_simulations=10_000,
        ruin_floor_pct=0.5,
        seed=42,
    )
    fe = mc.final_equity_quantiles
    md = mc.max_dd_quantiles
    print(f"  Final equity quantiles: P5=₹{fe[0.05]:>12,.0f}  P50=₹{fe[0.50]:>12,.0f}  P95=₹{fe[0.95]:>12,.0f}")
    print(f"  Max DD quantiles:       P5={md[0.05]*100:7.2f}%  P50={md[0.50]*100:7.2f}%  P95={md[0.95]*100:7.2f}%")
    print(f"  Ruin probability (eq < 50% initial): {mc.ruin_probability:.3f}")

    # ── 9. Walk-Forward ──────────────────────────────────────────────────
    banner("9. WALK-FORWARD (rolling, 2y train / 6mo test, step 6mo, purge+embargo 5d)")
    wf = WalkForwardValidator(
        train_window_days=730, test_window_days=180,
        step_days=180, mode="rolling",
        purge_days=5, embargo_days=5,
    )

    def factory():
        # Strategy is precomputed on the full df at construction; window-specific df is what's passed at run time.
        return None  # placeholder — we override the run loop via run_backtest_fn below

    def run_bt(window_df: pd.DataFrame, _strategy, cfg: BacktestConfig) -> BacktestResults:
        s = build_strategy(window_df)
        return run_backtest(window_df, s, cfg, regime_tagger=regime_tagger, benchmark=None)

    wf_res = wf.run(df=df, strategy_factory=factory, config=base_cfg, run_backtest_fn=run_bt)
    print(f"  Windows: {len(wf_res.windows)}")
    print(f"  IS-OOS Spearman ρ:    {wf_res.is_oos_correlation:>6.3f}")
    print(f"  Aggregate OOS Sharpe: {wf_res.aggregate_test_sharpe:>6.3f}")
    print(f"  Aggregate OOS CAGR:   {wf_res.aggregate_test_cagr*100:>6.2f}%")
    print(f"  Aggregate OOS Max DD: {wf_res.aggregate_test_max_dd_pct*100:>6.2f}%")
    print(f"  Decay ratio (test/train Sharpe mean): {wf_res.decay_ratio:.3f}")
    print(f"  Per-window:")
    print(f"    {'k':>3} {'train_sh':>9} {'test_sh':>8} {'test_cagr%':>11} {'test_mdd%':>10} {'test_trades':>11}")
    for w in wf_res.windows:
        print(f"    {w.window_index:>3d} {w.train_metrics.sharpe:>9.2f} {w.test_metrics.sharpe:>8.2f} "
              f"{w.test_metrics.cagr*100:>11.2f} {w.test_metrics.max_dd_pct*100:>10.2f} "
              f"{w.test_metrics.n_trades:>11d}")

    # ── 10. Regime-stratified P&L ────────────────────────────────────────
    banner("10. REGIME-STRATIFIED P&L (composite tag = vol×trend×time)")
    if res.regime_pnl:
        items = sorted(res.regime_pnl.items(), key=lambda x: -abs(x[1]))
        print(f"  {'regime tag':<40} {'P&L (₹)':>14} {'P&L %':>9}")
        total = sum(res.regime_pnl.values())
        for tag, pnl in items[:25]:
            pct = (pnl / total * 100) if total != 0 else 0.0
            print(f"  {tag:<40} {pnl:>14,.0f} {pct:>8.1f}%")
        print(f"  {'TOTAL':<40} {total:>14,.0f}")

    # ── 11. Final report (text + figures) ────────────────────────────────
    banner("11. WRITING REPORT")
    pbo_for_report = pbo
    report_path = generate_report(
        results=res,
        out_dir=OUT,
        pbo_result=pbo_for_report,
        dsr=(dsr, sr0),
        psr=psr_zero,
        bootstrap_ci={
            "sharpe": (sh_lo, sh_hi, sh_pt),
            "max_dd": (dd_lo, dd_hi, dd_pt),
        },
        walk_forward=wf_res,
        monte_carlo=mc,
    )
    print(f"  text summary → {report_path}")
    figures = list(OUT.glob("*.png"))
    print(f"  figures      → {len(figures)} PNGs in {OUT}")

    # Dump structured JSON for downstream
    summary = {
        "baseline": {
            "total_return": res.metrics.total_return,
            "cagr": res.metrics.cagr,
            "sharpe": res.metrics.sharpe,
            "sortino": res.metrics.sortino,
            "calmar": res.metrics.calmar,
            "max_dd_pct": res.metrics.max_dd_pct,
            "max_dd_duration_days": res.metrics.max_dd_duration_days,
            "n_trades": res.metrics.n_trades,
            "win_rate": res.metrics.win_rate,
            "profit_factor": res.metrics.profit_factor,
            "expectancy_pct": res.metrics.expectancy_pct,
            "avg_r_multiple": res.metrics.avg_r_multiple,
            "trades_per_year": res.metrics.trades_per_year,
            "time_in_market_pct": res.metrics.time_in_market_pct,
            "total_costs_inr": res.metrics.total_costs_inr,
            "cost_drag_pct": res.metrics.cost_drag_pct,
            "ulcer_index": res.metrics.ulcer_index,
        },
        "bah": {
            "total_return": res.benchmark_metrics.total_return if res.benchmark_metrics else None,
            "cagr": res.benchmark_metrics.cagr if res.benchmark_metrics else None,
            "sharpe": res.benchmark_metrics.sharpe if res.benchmark_metrics else None,
            "max_dd_pct": res.benchmark_metrics.max_dd_pct if res.benchmark_metrics else None,
        },
        "validation": {
            "psr_zero": psr_zero,
            "psr_vs_bah": psr_bah,
            "dsr_25_trials": dsr,
            "dsr_threshold_sr0": sr0,
            "pbo": pbo.pbo,
            "pbo_rank_corr": pbo.rank_correlation_is_oos,
            "bootstrap_sharpe_ci": [sh_lo, sh_hi, sh_pt],
            "bootstrap_max_dd_ci": [dd_lo, dd_hi, dd_pt],
            "monte_carlo_p5_eq": fe[0.05],
            "monte_carlo_p50_eq": fe[0.50],
            "monte_carlo_p95_eq": fe[0.95],
            "monte_carlo_ruin": mc.ruin_probability,
            "wf_oos_sharpe": wf_res.aggregate_test_sharpe,
            "wf_oos_cagr": wf_res.aggregate_test_cagr,
            "wf_oos_max_dd": wf_res.aggregate_test_max_dd_pct,
            "wf_decay_ratio": wf_res.decay_ratio,
            "wf_is_oos_corr": wf_res.is_oos_correlation,
        },
        "param_grid": [
            {"period": p, "mult": k, "sharpe": m.sharpe, "cagr": m.cagr, "mdd": m.max_dd_pct, "trades": m.n_trades}
            for p, k, m in grid_metrics
        ],
    }
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2))
    print(f"  json summary → {OUT / 'summary.json'}")

    print(f"\nDone in {time.time() - t0:.1f}s.")


if __name__ == "__main__":
    main()
