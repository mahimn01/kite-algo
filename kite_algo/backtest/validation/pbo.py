"""Probability of Backtest Overfitting (PBO) via CSCV.

Reference: Bailey, Borwein, López de Prado, Zhu (2014),
"The Probability of Backtest Overfitting", J. Computational Finance 17(4).
https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2326253

CSCV (Combinatorially Symmetric Cross-Validation):
1. Take an N x T return matrix (N variants, T periods).
2. Partition columns into S equal groups (S even).
3. For each of the C(S, S/2) ways to choose S/2 groups as IS:
   - Compute per-variant Sharpe on IS and OOS.
   - n* = argmax IS Sharpe; record OOS rank of n*.
   - logit_c = log(w / (1 - w)) where w is the relative OOS rank in (0, 1).
4. PBO = fraction of combinations with logit_c < 0.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from math import comb

import numpy as np
from scipy import stats


@dataclass(frozen=True)
class PBOResult:
    pbo: float
    pbo_std: float
    n_combinations: int
    n_variants: int
    n_groups: int
    rank_correlation_is_oos: float
    is_performance_of_oos_best: np.ndarray
    oos_performance_of_is_best: np.ndarray
    logits: np.ndarray
    metric_name: str


class PBOCalculator:
    def __init__(
        self,
        metric: str = "sharpe",
        n_groups: int = 16,
        annualization_factor: float = 252.0,
    ) -> None:
        if n_groups < 2 or n_groups % 2 != 0:
            raise ValueError(f"n_groups must be even and >= 2, got {n_groups}")
        if metric not in ("sharpe", "mean", "sortino"):
            raise ValueError(f"metric must be one of sharpe|mean|sortino, got {metric}")
        self.metric = metric
        self.n_groups = n_groups
        self.annualization_factor = annualization_factor

    def calculate(self, returns_matrix: np.ndarray) -> PBOResult:
        if returns_matrix.ndim != 2:
            raise ValueError(f"returns_matrix must be 2D (n_variants, n_periods), got shape {returns_matrix.shape}")
        n_variants, n_periods = returns_matrix.shape
        if n_variants < 2:
            raise ValueError(f"need >= 2 variants, got {n_variants}")
        if n_periods < self.n_groups * 2:
            raise ValueError(
                f"n_periods ({n_periods}) too small for n_groups ({self.n_groups}); "
                f"need >= {self.n_groups * 2}"
            )

        # Trim trailing periods so T is divisible by n_groups.
        usable = (n_periods // self.n_groups) * self.n_groups
        M = np.ascontiguousarray(returns_matrix[:, :usable], dtype=np.float64)
        group_size = usable // self.n_groups
        # Shape: (n_groups, n_variants, group_size) for fast slicing.
        groups = M.reshape(n_variants, self.n_groups, group_size).transpose(1, 0, 2)

        n_is = self.n_groups // 2
        n_combos = comb(self.n_groups, n_is)

        logits = np.empty(n_combos, dtype=np.float64)
        is_perf_of_oos_best = np.empty(n_combos, dtype=np.float64)
        oos_perf_of_is_best = np.empty(n_combos, dtype=np.float64)
        rank_corrs: list[float] = []

        all_indices = np.arange(self.n_groups)
        for c_idx, is_idx in enumerate(combinations(range(self.n_groups), n_is)):
            is_idx_arr = np.asarray(is_idx, dtype=np.int64)
            mask = np.ones(self.n_groups, dtype=bool)
            mask[is_idx_arr] = False
            oos_idx_arr = all_indices[mask]

            # (n_is * group_size, n_variants)? We want per-variant series.
            # groups shape (n_groups, n_variants, group_size). Select IS groups -> (n_is, n_variants, group_size)
            is_block = groups[is_idx_arr].transpose(1, 0, 2).reshape(n_variants, -1)
            oos_block = groups[oos_idx_arr].transpose(1, 0, 2).reshape(n_variants, -1)

            is_perf = self._metric_vec(is_block)
            oos_perf = self._metric_vec(oos_block)

            best_is = int(np.argmax(is_perf))
            # Relative OOS rank of best_is in (0, 1) using mid-rank for ties, then
            # divide by (N+1) so values stay strictly in (0, 1).
            ranks = stats.rankdata(oos_perf, method="average")
            w = ranks[best_is] / (n_variants + 1.0)
            # Numerical safety; with the (N+1) denominator w in (0, 1) strictly.
            w = float(np.clip(w, 1e-12, 1.0 - 1e-12))
            logits[c_idx] = float(np.log(w / (1.0 - w)))

            best_oos = int(np.argmax(oos_perf))
            is_perf_of_oos_best[c_idx] = is_perf[best_oos]
            oos_perf_of_is_best[c_idx] = oos_perf[best_is]

            if n_variants >= 3:
                rho, _ = stats.spearmanr(is_perf, oos_perf)
                if not np.isnan(rho):
                    rank_corrs.append(float(rho))

        pbo = float(np.mean(logits < 0.0))
        # Std of the indicator var across combinations.
        pbo_std = float(np.std((logits < 0.0).astype(np.float64), ddof=0))
        rank_corr = float(np.mean(rank_corrs)) if rank_corrs else 0.0

        return PBOResult(
            pbo=pbo,
            pbo_std=pbo_std,
            n_combinations=n_combos,
            n_variants=n_variants,
            n_groups=self.n_groups,
            rank_correlation_is_oos=rank_corr,
            is_performance_of_oos_best=is_perf_of_oos_best,
            oos_performance_of_is_best=oos_perf_of_is_best,
            logits=logits,
            metric_name=self.metric,
        )

    def _metric_vec(self, block: np.ndarray) -> np.ndarray:
        """Compute the metric per row (variant) on a (n_variants, T) block."""
        if self.metric == "mean":
            return block.mean(axis=1) * self.annualization_factor

        if self.metric == "sharpe":
            mean = block.mean(axis=1)
            std = block.std(axis=1, ddof=1)
            std = np.where(std < 1e-12, np.nan, std)
            sr = (mean / std) * np.sqrt(self.annualization_factor)
            # Replace nan (zero-std rows) with 0 so argmax stays well-defined.
            return np.nan_to_num(sr, nan=0.0)

        # sortino
        mean = block.mean(axis=1)
        downside = np.where(block < 0.0, block, 0.0)
        # Population-style: sqrt(mean(downside^2)).
        dd = np.sqrt(np.mean(downside ** 2, axis=1))
        dd = np.where(dd < 1e-12, np.nan, dd)
        s = (mean / dd) * np.sqrt(self.annualization_factor)
        return np.nan_to_num(s, nan=0.0)
