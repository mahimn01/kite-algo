"""Statistical-validation suite for the kite-algo backtest framework.

Public surface:
    - PBO (CSCV)
    - Deflated / Probabilistic Sharpe Ratio
    - Stationary bootstrap CIs on Sharpe and max drawdown
    - Monte Carlo trade-resampling
    - Walk-forward validation
"""

from __future__ import annotations

from kite_algo.backtest.validation.bootstrap import (
    bootstrap_max_dd_ci,
    bootstrap_sharpe_ci,
    stationary_bootstrap_indices,
    stationary_bootstrap_returns,
)
from kite_algo.backtest.validation.deflated_sharpe import (
    annualized_dsr,
    annualized_psr,
    deflated_sharpe_ratio,
    probabilistic_sharpe_ratio,
)
from kite_algo.backtest.validation.monte_carlo import (
    MonteCarloResult,
    monte_carlo_trade_paths,
)
from kite_algo.backtest.validation.pbo import PBOCalculator, PBOResult
from kite_algo.backtest.validation.walk_forward import (
    WalkForwardResult,
    WalkForwardValidator,
    WalkForwardWindow,
)

__all__ = [
    "PBOCalculator",
    "PBOResult",
    "probabilistic_sharpe_ratio",
    "deflated_sharpe_ratio",
    "annualized_psr",
    "annualized_dsr",
    "stationary_bootstrap_indices",
    "stationary_bootstrap_returns",
    "bootstrap_sharpe_ci",
    "bootstrap_max_dd_ci",
    "MonteCarloResult",
    "monte_carlo_trade_paths",
    "WalkForwardValidator",
    "WalkForwardWindow",
    "WalkForwardResult",
]
