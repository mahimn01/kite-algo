"""Frozen dataclasses + Protocols defining the backtest ABI.

Agent B (validation) builds against these contracts. Do not change field
names or types without versioning. All money is INR; all timestamps are
tz-aware UTC unless explicitly noted as `_ist`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class Bar:
    ts: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass(frozen=True)
class Signal:
    action: str
    qty: int = 0
    reason: str = ""
    stop_loss_price: float | None = None


@dataclass(frozen=True)
class Trade:
    entry_ts: pd.Timestamp
    entry_price: float
    exit_ts: pd.Timestamp
    exit_price: float
    qty: int
    side: str
    gross_pnl: float
    costs: float
    net_pnl: float
    return_pct: float
    bars_held: int
    mae: float
    mfe: float
    exit_reason: str
    entry_atr: float
    entry_st_band: float
    regime_tag: str = ""


@dataclass(frozen=True)
class EquityPoint:
    ts: pd.Timestamp
    equity: float
    cash: float
    position_value: float
    drawdown_pct: float


@dataclass(frozen=True)
class DailyResult:
    d: date
    daily_return: float
    daily_pnl: float
    end_equity: float
    n_trades_closed: int


@dataclass(frozen=True)
class BacktestConfig:
    initial_capital: float = 1_000_000.0
    instrument: str = "NIFTY_FUT"
    lot_size: int = 75
    multiplier: float = 1.0
    sizing_mode: str = "fixed_lots"
    fixed_lots: int = 1
    fixed_notional_inr: float = 1_500_000.0
    target_daily_vol_pct: float = 0.01
    slippage_bps_per_side: float = 1.5
    cost_model: str = "futures"
    fill_at: str = "next_bar_open"
    warmup_bars: int = 250
    risk_free_rate_annual: float = 0.065
    bars_per_year: float = 1750.0
    skip_offhours_bars: bool = True


@dataclass(frozen=True)
class BacktestMetrics:
    # Returns
    total_return: float
    cagr: float
    n_years: float
    # Risk-adjusted
    sharpe: float
    sortino: float
    calmar: float
    omega_1pct: float
    # Drawdown
    max_dd_pct: float
    max_dd_duration_days: int
    avg_dd_pct: float
    ulcer_index: float
    # Trade-level
    n_trades: int
    n_wins: int
    n_losses: int
    win_rate: float
    profit_factor: float
    expectancy_pct: float
    avg_win_pct: float
    avg_loss_pct: float
    win_loss_ratio: float
    avg_r_multiple: float
    avg_bars_held: float
    avg_mae_pct: float
    avg_mfe_pct: float
    payoff_capture: float
    max_consec_wins: int
    max_consec_losses: int
    trades_per_year: float
    time_in_market_pct: float
    # Distribution
    skew: float
    kurtosis: float
    daily_win_rate: float
    best_day_pct: float
    worst_day_pct: float
    var_95: float
    cvar_95: float
    # Costs
    total_costs_inr: float
    cost_drag_pct: float
    cost_per_trade_inr: float


@dataclass(frozen=True)
class BacktestResults:
    config: BacktestConfig
    trades: list[Trade]
    equity_curve: list[EquityPoint]
    daily_results: list[DailyResult]
    metrics: BacktestMetrics
    benchmark_equity_curve: list[EquityPoint] | None = None
    benchmark_metrics: BacktestMetrics | None = None
    regime_pnl: dict[str, float] = field(default_factory=dict)

    @property
    def daily_returns(self) -> np.ndarray:
        return np.array([d.daily_return for d in self.daily_results], dtype=np.float64)

    @property
    def trade_returns(self) -> np.ndarray:
        return np.array([t.return_pct for t in self.trades], dtype=np.float64)

    @property
    def equity_array(self) -> np.ndarray:
        return np.array([p.equity for p in self.equity_curve], dtype=np.float64)


class Strategy(Protocol):
    name: str

    def on_bar(self, bar: Bar, history: pd.DataFrame) -> list[Signal]: ...


@dataclass(frozen=True)
class CostBreakdown:
    brokerage: float
    stt: float
    exchange: float
    sebi: float
    stamp: float
    ipft: float
    dp_charge: float
    gst: float
    total: float


@dataclass(frozen=True)
class RegimeTag:
    vol_bucket: str
    trend_bucket: str
    time_of_day: str
    event_flag: str
    composite_key: str
