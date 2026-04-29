"""Text + matplotlib report generator for backtest validation outputs.

Sober, publication-ready figures. Text report produces a single .txt file
with all sections; figures are saved as PNGs alongside.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

from kite_algo.backtest.models import BacktestMetrics, BacktestResults
from kite_algo.backtest.validation.monte_carlo import MonteCarloResult
from kite_algo.backtest.validation.pbo import PBOResult
from kite_algo.backtest.validation.walk_forward import WalkForwardResult

_IST = "Asia/Kolkata"


def _fmt_pct(x: float, digits: int = 2) -> str:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "n/a"
    return f"{x * 100:.{digits}f}%"


def _fmt_num(x: float, digits: int = 3) -> str:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "n/a"
    return f"{x:.{digits}f}"


def _fmt_inr(x: float) -> str:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "n/a"
    return f"INR {x:,.2f}"


def _to_ist(ts: pd.Timestamp) -> str:
    if ts is None:
        return "n/a"
    if ts.tzinfo is None:
        return ts.strftime("%Y-%m-%d %H:%M")
    return ts.tz_convert(_IST).strftime("%Y-%m-%d %H:%M IST")


def _section(title: str) -> str:
    bar = "=" * 72
    return f"\n{bar}\n{title}\n{bar}\n"


def generate_text_report(
    results: BacktestResults,
    pbo_result: PBOResult | None = None,
    dsr: tuple[float, float] | None = None,
    psr: float | None = None,
    bootstrap_ci: Mapping[str, tuple[float, float, float]] | None = None,
    walk_forward: WalkForwardResult | None = None,
    monte_carlo: MonteCarloResult | None = None,
) -> str:
    lines: list[str] = []
    cfg = results.config
    m: BacktestMetrics = results.metrics

    lines.append(_section("STRATEGY + CONFIG"))
    lines.append(f"Instrument: {cfg.instrument}")
    lines.append(f"Initial capital: {_fmt_inr(cfg.initial_capital)}")
    lines.append(f"Lot size: {cfg.lot_size}    Multiplier: {cfg.multiplier}")
    lines.append(f"Sizing mode: {cfg.sizing_mode}    Fixed lots: {cfg.fixed_lots}")
    lines.append(f"Risk-free (annual): {_fmt_pct(cfg.risk_free_rate_annual)}    "
                 f"Bars/yr: {cfg.bars_per_year:.0f}")
    lines.append(f"Slippage/side (bps): {cfg.slippage_bps_per_side}    "
                 f"Cost model: {cfg.cost_model}    Fill: {cfg.fill_at}")
    if results.equity_curve:
        lines.append(f"Period: {_to_ist(results.equity_curve[0].ts)} -> "
                     f"{_to_ist(results.equity_curve[-1].ts)}")

    lines.append(_section("RETURNS"))
    lines.append(f"Total return: {_fmt_pct(m.total_return)}")
    lines.append(f"CAGR ({m.n_years:.2f} yrs): {_fmt_pct(m.cagr)}")
    lines.append(f"Sharpe: {_fmt_num(m.sharpe)}    "
                 f"Sortino: {_fmt_num(m.sortino)}    "
                 f"Calmar: {_fmt_num(m.calmar)}    "
                 f"Omega(1%): {_fmt_num(m.omega_1pct)}")
    if bootstrap_ci:
        for name, (lo, hi, pt) in bootstrap_ci.items():
            lines.append(f"  {name} bootstrap CI: point={_fmt_num(pt)}  "
                         f"[{_fmt_num(lo)}, {_fmt_num(hi)}]")

    lines.append(_section("DRAWDOWN"))
    lines.append(f"Max DD: {_fmt_pct(m.max_dd_pct)}    "
                 f"Avg DD: {_fmt_pct(m.avg_dd_pct)}    "
                 f"Ulcer: {_fmt_num(m.ulcer_index)}    "
                 f"Max DD duration: {m.max_dd_duration_days} days")

    lines.append(_section("TRADE STATS"))
    lines.append(f"N trades: {m.n_trades}  Wins: {m.n_wins}  Losses: {m.n_losses}  "
                 f"Win rate: {_fmt_pct(m.win_rate)}")
    lines.append(f"Profit factor: {_fmt_num(m.profit_factor)}    "
                 f"Expectancy: {_fmt_pct(m.expectancy_pct)}    "
                 f"Avg R: {_fmt_num(m.avg_r_multiple)}")
    lines.append(f"Avg win: {_fmt_pct(m.avg_win_pct)}    "
                 f"Avg loss: {_fmt_pct(m.avg_loss_pct)}    "
                 f"Win/loss ratio: {_fmt_num(m.win_loss_ratio)}")
    lines.append(f"Avg bars held: {_fmt_num(m.avg_bars_held, 1)}    "
                 f"Avg MAE: {_fmt_pct(m.avg_mae_pct)}    "
                 f"Avg MFE: {_fmt_pct(m.avg_mfe_pct)}")
    lines.append(f"Max consec wins: {m.max_consec_wins}    "
                 f"Max consec losses: {m.max_consec_losses}    "
                 f"Trades/yr: {_fmt_num(m.trades_per_year, 1)}    "
                 f"Time in mkt: {_fmt_pct(m.time_in_market_pct)}")

    lines.append(_section("DISTRIBUTION"))
    lines.append(f"Skew: {_fmt_num(m.skew)}    Kurtosis: {_fmt_num(m.kurtosis)}    "
                 f"Daily win rate: {_fmt_pct(m.daily_win_rate)}")
    lines.append(f"Best day: {_fmt_pct(m.best_day_pct)}    "
                 f"Worst day: {_fmt_pct(m.worst_day_pct)}    "
                 f"VaR(95): {_fmt_pct(m.var_95)}    "
                 f"CVaR(95): {_fmt_pct(m.cvar_95)}")

    lines.append(_section("COSTS"))
    lines.append(f"Total: {_fmt_inr(m.total_costs_inr)}    "
                 f"Drag: {_fmt_pct(m.cost_drag_pct)}    "
                 f"Per trade: {_fmt_inr(m.cost_per_trade_inr)}")

    lines.append(_section("VALIDATION"))
    if psr is not None:
        lines.append(f"PSR(0): {_fmt_num(psr, 4)}")
    if dsr is not None:
        dsr_val, sr0 = dsr
        lines.append(f"DSR: {_fmt_num(dsr_val, 4)}    Implied SR0 threshold: {_fmt_num(sr0, 4)}")
    if pbo_result is not None:
        lines.append(
            f"PBO: {_fmt_num(pbo_result.pbo, 4)} "
            f"(std {_fmt_num(pbo_result.pbo_std, 4)}, "
            f"{pbo_result.n_combinations} combos, "
            f"{pbo_result.n_variants} variants, metric={pbo_result.metric_name})"
        )
        lines.append(f"IS-OOS rank correlation (mean over combos): "
                     f"{_fmt_num(pbo_result.rank_correlation_is_oos, 3)}")
    if walk_forward is not None:
        lines.append("")
        lines.append("Walk-forward windows:")
        lines.append(f"  N windows: {len(walk_forward.windows)}")
        lines.append(f"  IS-OOS Sharpe rank correlation: "
                     f"{_fmt_num(walk_forward.is_oos_correlation, 3)}")
        lines.append(f"  Aggregate OOS Sharpe: {_fmt_num(walk_forward.aggregate_test_sharpe)}")
        lines.append(f"  Aggregate OOS CAGR:   {_fmt_pct(walk_forward.aggregate_test_cagr)}")
        lines.append(f"  Aggregate OOS max DD: {_fmt_pct(walk_forward.aggregate_test_max_dd_pct)}")
        lines.append(f"  Decay ratio (test/train mean Sharpe): "
                     f"{_fmt_num(walk_forward.decay_ratio, 3)}")
        lines.append("  -- per window --")
        lines.append(f"  {'k':>3}  {'train':<35}  {'test':<35}  {'IS SR':>7}  {'OOS SR':>7}")
        for w in walk_forward.windows:
            train_str = f"{w.train_start.date()}->{w.train_end.date()}"
            test_str = f"{w.test_start.date()}->{w.test_end.date()}"
            lines.append(
                f"  {w.window_index:>3}  {train_str:<35}  {test_str:<35}  "
                f"{w.train_metrics.sharpe:>7.3f}  {w.test_metrics.sharpe:>7.3f}"
            )
    if monte_carlo is not None:
        mc = monte_carlo
        lines.append("")
        lines.append("Monte Carlo (trade-return resampling):")
        lines.append(f"  Simulations: {mc.n_simulations}")
        lines.append(f"  Final equity P5/P50/P95: "
                     f"{_fmt_inr(mc.final_equity_quantiles[0.05])} / "
                     f"{_fmt_inr(mc.final_equity_quantiles[0.5])} / "
                     f"{_fmt_inr(mc.final_equity_quantiles[0.95])}")
        lines.append(f"  Max DD P5/P50/P95: "
                     f"{_fmt_pct(mc.max_dd_quantiles[0.05])} / "
                     f"{_fmt_pct(mc.max_dd_quantiles[0.5])} / "
                     f"{_fmt_pct(mc.max_dd_quantiles[0.95])}")
        lines.append(f"  Ruin probability: {_fmt_pct(mc.ruin_probability)}")

    lines.append(_section("REGIME P&L"))
    if results.regime_pnl:
        for k_, v_ in sorted(results.regime_pnl.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {k_:<32} {_fmt_inr(v_)}")
    else:
        lines.append("  (no regime tags recorded)")

    lines.append(_section("VERDICT"))
    verdict = _verdict(m, pbo_result, dsr, results.benchmark_metrics)
    lines.append(verdict)

    return "\n".join(lines) + "\n"


def _verdict(
    m: BacktestMetrics,
    pbo_result: PBOResult | None,
    dsr: tuple[float, float] | None,
    benchmark: BacktestMetrics | None,
) -> str:
    sharpe_ok = m.sharpe > 1.0
    dsr_ok = (dsr is not None and dsr[0] > 0.95) or dsr is None
    pbo_ok = (pbo_result is not None and pbo_result.pbo < 0.3) or pbo_result is None
    bah_ok = benchmark is None or m.cagr > benchmark.cagr

    flags = {
        "Sharpe > 1": sharpe_ok,
        "DSR > 0.95": dsr_ok,
        "PBO < 0.3": pbo_ok,
        "Beats benchmark": bah_ok,
    }
    n_pass = sum(1 for v in flags.values() if v)
    if n_pass == 4:
        light = "GREEN"
    elif n_pass >= 2:
        light = "YELLOW"
    else:
        light = "RED"
    rows = "  " + "    ".join(f"{k}: {'PASS' if v else 'FAIL'}" for k, v in flags.items())
    return f"  Traffic light: {light}\n{rows}"


# ---------------------------- figures ----------------------------------- #


def _setup_axes(ax: plt.Axes) -> None:
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(True, linestyle="--", alpha=0.3)


def _fig_equity_curve(results: BacktestResults, out_path: Path) -> Path:
    fig, ax = plt.subplots(figsize=(10, 5))
    ts = [p.ts for p in results.equity_curve]
    eq = [p.equity for p in results.equity_curve]
    ax.plot(ts, eq, color="#1f4e79", linewidth=1.4, label="Strategy")
    if results.benchmark_equity_curve:
        b_ts = [p.ts for p in results.benchmark_equity_curve]
        b_eq = [p.equity for p in results.benchmark_equity_curve]
        ax.plot(b_ts, b_eq, color="#888888", linewidth=1.0, linestyle="--", label="Benchmark")
        ax.legend(frameon=False)
    ax.set_title("Equity Curve")
    ax.set_ylabel("Equity (INR)")
    _setup_axes(ax)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    return out_path


def _fig_drawdown(results: BacktestResults, out_path: Path) -> Path:
    fig, ax = plt.subplots(figsize=(10, 4))
    ts = [p.ts for p in results.equity_curve]
    dd = [p.drawdown_pct for p in results.equity_curve]
    ax.fill_between(ts, dd, 0, color="#a83232", alpha=0.4)
    ax.plot(ts, dd, color="#a83232", linewidth=0.8)
    ax.set_title("Drawdown")
    ax.set_ylabel("Drawdown")
    _setup_axes(ax)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    return out_path


def _fig_monthly_heatmap(results: BacktestResults, out_path: Path) -> Path:
    fig, ax = plt.subplots(figsize=(10, 5))
    if not results.daily_results:
        ax.text(0.5, 0.5, "No daily returns", ha="center", va="center")
        ax.axis("off")
        fig.savefig(out_path, dpi=120)
        plt.close(fig)
        return out_path

    df = pd.DataFrame(
        {"d": [d.d for d in results.daily_results],
         "r": [d.daily_return for d in results.daily_results]}
    )
    df["ym"] = pd.to_datetime(df["d"])
    df["year"] = df["ym"].dt.year
    df["month"] = df["ym"].dt.month
    monthly = df.groupby(["year", "month"])["r"].apply(
        lambda x: float(np.prod(1.0 + x.to_numpy()) - 1.0)
    ).unstack(fill_value=np.nan)

    im = ax.imshow(monthly.values, aspect="auto", cmap="RdYlGn",
                   vmin=-0.10, vmax=0.10)
    ax.set_xticks(range(monthly.shape[1]))
    ax.set_xticklabels([f"{m:02d}" for m in monthly.columns])
    ax.set_yticks(range(monthly.shape[0]))
    ax.set_yticklabels([str(y) for y in monthly.index])
    ax.set_title("Monthly Returns (compounded)")
    fig.colorbar(im, ax=ax, fraction=0.025, pad=0.02, label="return")
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    return out_path


def _fig_trade_distribution(results: BacktestResults, out_path: Path) -> Path:
    fig, ax = plt.subplots(figsize=(8, 4.5))
    rets = results.trade_returns
    if rets.size == 0:
        ax.text(0.5, 0.5, "No trades", ha="center", va="center")
        ax.axis("off")
    else:
        ax.hist(rets, bins=40, color="#1f4e79", alpha=0.85, edgecolor="white")
        ax.axvline(0.0, color="black", linewidth=0.8)
        ax.set_title("Trade Return Distribution")
        ax.set_xlabel("Return")
        ax.set_ylabel("Count")
        _setup_axes(ax)
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    return out_path


def _fig_mae_mfe(results: BacktestResults, out_path: Path) -> Path:
    fig, ax = plt.subplots(figsize=(7, 6))
    if not results.trades:
        ax.text(0.5, 0.5, "No trades", ha="center", va="center")
        ax.axis("off")
    else:
        mae = np.array([t.mae for t in results.trades])
        mfe = np.array([t.mfe for t in results.trades])
        win = np.array([t.net_pnl > 0 for t in results.trades])
        ax.scatter(mae[win], mfe[win], s=14, color="#2e7d32", alpha=0.7, label="Win")
        ax.scatter(mae[~win], mfe[~win], s=14, color="#a83232", alpha=0.7, label="Loss")
        ax.set_xlabel("MAE")
        ax.set_ylabel("MFE")
        ax.set_title("MAE vs MFE")
        ax.legend(frameon=False)
        _setup_axes(ax)
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    return out_path


def _fig_regime_pnl(results: BacktestResults, out_path: Path) -> Path:
    fig, ax = plt.subplots(figsize=(9, 4.5))
    if not results.regime_pnl:
        ax.text(0.5, 0.5, "No regime tags", ha="center", va="center")
        ax.axis("off")
    else:
        items = sorted(results.regime_pnl.items(), key=lambda kv: kv[1])
        labels = [k for k, _ in items]
        vals = [v for _, v in items]
        colors = ["#a83232" if v < 0 else "#2e7d32" for v in vals]
        ax.barh(labels, vals, color=colors)
        ax.set_title("P&L by Regime")
        ax.set_xlabel("Net P&L (INR)")
        _setup_axes(ax)
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    return out_path


def generate_figures(results: BacktestResults, out_dir: Path) -> list[Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    paths.append(_fig_equity_curve(results, out_dir / "equity_curve.png"))
    paths.append(_fig_drawdown(results, out_dir / "drawdown.png"))
    paths.append(_fig_monthly_heatmap(results, out_dir / "monthly_returns_heatmap.png"))
    paths.append(_fig_trade_distribution(results, out_dir / "trade_return_distribution.png"))
    paths.append(_fig_mae_mfe(results, out_dir / "mae_mfe_scatter.png"))
    paths.append(_fig_regime_pnl(results, out_dir / "regime_pnl.png"))
    return paths


def generate_report(
    results: BacktestResults,
    out_dir: Path,
    pbo_result: PBOResult | None = None,
    dsr: tuple[float, float] | None = None,
    psr: float | None = None,
    bootstrap_ci: Mapping[str, tuple[float, float, float]] | None = None,
    walk_forward: WalkForwardResult | None = None,
    monte_carlo: MonteCarloResult | None = None,
) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    text = generate_text_report(
        results,
        pbo_result=pbo_result,
        dsr=dsr,
        psr=psr,
        bootstrap_ci=bootstrap_ci,
        walk_forward=walk_forward,
        monte_carlo=monte_carlo,
    )
    txt_path = out_dir / "summary.txt"
    txt_path.write_text(text, encoding="utf-8")
    generate_figures(results, out_dir)
    return txt_path
