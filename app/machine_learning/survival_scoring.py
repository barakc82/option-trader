"""Building blocks for P(max_ask stays below stop): the shared survival
label, stop-column validation, probability clipping, logloss/brier, and the
two residual-distribution implementations (normal, empirical) that turn a
regression's point prediction into a probability.

These are reused by best_subset.py's logloss/weighted_logloss SCORE_FN
implementations, which run the actual best-subset search on the shared
question every candidate model answers -- P(max_ask < stop) -- rather than
on dollar-space squared error, which only ever measured the "raw" target
fairly since that's what least-squares was fitted to.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.stats import norm

from .regress_max_ask import GROUP_COLUMN, TARGET_COLUMN

logger = logging.getLogger(__name__)

STOP_COLUMN = "stop_loss"
CREDIT_COLUMN = "estimated_sell_price"

# ctx (the non-feature columns the scoring machinery needs alongside X)
# always carries at least these -- max_ask/credit/stop for label and weight
# computation, plus GROUP_COLUMN for the nested best-subset search's own
# GroupKFold.
CTX_COLUMNS = [TARGET_COLUMN, CREDIT_COLUMN, STOP_COLUMN, GROUP_COLUMN]

EPS = 1e-6
EMPIRICAL_SMOOTHING = "laplace"  # "laplace" | "none"


# ---------------------------------------------------------------------------
# Label
# ---------------------------------------------------------------------------

def compute_survival_label(ctx: pd.DataFrame, max_ask_col: str = TARGET_COLUMN, stop_col: str = STOP_COLUMN) -> np.ndarray:
    """The single, shared definition of the survival label. survived=1 means
    the ask never crossed the stop for the life of the trade. Computed once
    here and nowhere else -- every logloss-family score is computed against
    exactly this output, regardless of target transform or residual
    distribution."""
    return (ctx[max_ask_col].to_numpy() < ctx[stop_col].to_numpy()).astype(float)


# ---------------------------------------------------------------------------
# Stop-column validation
# ---------------------------------------------------------------------------

@dataclass
class StopValidationReport:
    n_rows: int
    stop_over_credit_quantiles: dict


def validate_stop_column(df: pd.DataFrame, stop_col: str = STOP_COLUMN, credit_col: str = CREDIT_COLUMN) -> StopValidationReport:
    """Hard requirements (raise, do not repair): stop present and finite,
    stop > credit."""
    if stop_col not in df.columns:
        raise ValueError(f"Required column '{stop_col}' not present")

    finite = np.isfinite(df[stop_col].to_numpy())
    n_missing = int((~finite).sum())
    if n_missing:
        raise ValueError(f"{n_missing} rows have missing/non-finite '{stop_col}' -- fix data, not repaired silently")

    bad = df[stop_col] <= df[credit_col]
    n_bad = int(bad.sum())
    if n_bad:
        raise ValueError(f"{n_bad} rows have '{stop_col}' <= '{credit_col}'; stop must exceed credit")

    ratio = df[stop_col] / df[credit_col]
    quantiles = {q: float(v) for q, v in ratio.quantile([0.0, 0.25, 0.5, 0.75, 1.0]).items()}

    return StopValidationReport(n_rows=len(df), stop_over_credit_quantiles=quantiles)


# ---------------------------------------------------------------------------
# Clipping and metrics
# ---------------------------------------------------------------------------

def clip_probabilities(p: np.ndarray, eps: float = EPS) -> tuple[np.ndarray, float]:
    """Clips p into [eps, 1-eps] so a single confidently-wrong prediction
    can't make logloss infinite. Returns (clipped, clip_rate); clip_rate is
    the fraction of predictions that fell outside [eps, 1-eps] before
    clipping."""
    p = np.asarray(p, dtype=float)
    clipped_mask = (p <= eps) | (p >= 1 - eps)
    clip_rate = float(clipped_mask.mean()) if len(p) else float("nan")
    return np.clip(p, eps, 1 - eps), clip_rate


def oof_clip_rate(p: np.ndarray, eps: float = EPS) -> float:
    """Detects, from an already-clipped array, what fraction landed exactly
    on a clip bound. np.clip sets out-of-range values to EXACTLY eps or
    1-eps (not an approximation), and no well-behaved model's raw output
    lands on that precise float by chance, so exact equality is a reliable
    detector without needing the pre-clip array."""
    return float(np.mean((p == eps) | (p == 1 - eps)))


def logloss(p: np.ndarray, y: np.ndarray, eps: float = EPS) -> float:
    p = np.clip(p, eps, 1 - eps)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def brier(p: np.ndarray, y: np.ndarray) -> float:
    return float(np.mean((p - y) ** 2))


# ---------------------------------------------------------------------------
# Residual distributions
# ---------------------------------------------------------------------------

class NormalResidualDistribution:
    name = "normal"

    def __init__(self):
        self.std: float | None = None

    def fit(self, residuals: np.ndarray) -> None:
        self.std = float(np.std(residuals, ddof=1)) if len(residuals) > 1 else float("nan")

    def survival_prob(self, t_minus_mu: np.ndarray) -> np.ndarray:
        assert self.std is not None, "fit() must be called before survival_prob()"
        if not np.isfinite(self.std) or self.std <= 0:
            # Degenerate training residuals (no spread). Fall back to a step
            # function at 0 rather than dividing by zero.
            return (t_minus_mu > 0).astype(float)
        return norm.cdf(t_minus_mu / self.std)


class EmpiricalResidualDistribution:
    name = "empirical"

    def __init__(self, smoothing: str = EMPIRICAL_SMOOTHING):
        if smoothing not in ("laplace", "none"):
            raise ValueError(f"unknown smoothing {smoothing!r}, expected 'laplace' or 'none'")
        self.smoothing = smoothing
        self.sorted_residuals: np.ndarray | None = None

    def fit(self, residuals: np.ndarray) -> None:
        self.sorted_residuals = np.sort(residuals)

    def survival_prob(self, t_minus_mu: np.ndarray) -> np.ndarray:
        """Fraction of training residuals e with e < t_minus_mu, i.e. the
        empirical CDF of the training residual pool evaluated at each row's
        own (stop - mu_hat). Laplace smoothing keeps this strictly inside
        (0, 1) even when the threshold sits beyond every training residual --
        without it the ECDF returns exact 0 or 1 (not "unlikely", but
        "impossible"), which is exactly the failure mode clip_probabilities
        exists to catch."""
        assert self.sorted_residuals is not None, "fit() must be called before survival_prob()"
        n = len(self.sorted_residuals)
        n_below = np.searchsorted(self.sorted_residuals, t_minus_mu, side="left")
        if self.smoothing == "laplace":
            return (n_below + 0.5) / (n + 1)
        return n_below / n


RESIDUAL_DISTRIBUTIONS = {"normal": NormalResidualDistribution, "empirical": EmpiricalResidualDistribution}


# ---------------------------------------------------------------------------
# Shipped-artifact wrapper for a (transform, residual-distribution) winner
# ---------------------------------------------------------------------------

class TransformedResidualClassifier:
    """Ships a (target transform, feature subset, residual distribution)
    winner from best_subset.search_best_subset_with_distribution, with the
    same calling convention as regress_max_ask.ProbabilityClassifier --
    __call__(X_new, threshold) -> (probability, y_hat) -- so downstream code
    doesn't need to know which transform or residual distribution won.

    threshold is a dollar stop level; it is mapped onto the fitted scale via
    the SAME transform['forward'] used for the fitting target itself before
    the residual distribution is asked for a probability -- exactly what
    happened for every row scored during the search this artifact is the
    outcome of. y_hat is returned in dollar space (transform['inverse']
    applied to the on-scale point prediction) for interpretability, even
    though the probability itself is computed on-scale.
    """

    def __init__(self, linear_model, best_subset: list[str], transform: dict, extra_column: str | None,
                 residual_distribution):
        self.linear_model = linear_model
        self.best_subset = best_subset
        self.transform = transform
        self.extra_column = extra_column
        self.residual_distribution = residual_distribution

    def __call__(self, X_new: pd.DataFrame, threshold):
        mu_hat = self.linear_model.predict(X_new[self.best_subset])
        extra_arr = X_new[self.extra_column].to_numpy() if self.extra_column is not None else None
        t = self.transform["forward"](np.asarray(threshold, dtype=float), extra_arr)
        p_raw = self.residual_distribution.survival_prob(t - mu_hat)
        p_clipped, clip_rate = clip_probabilities(p_raw)
        logger.info(f"TransformedResidualClassifier: p = "
                    f"{getattr(self.residual_distribution, 'name', '?')}_residual_distribution.survival_prob(t - mu_hat), "
                    f"mu_hat=linear_model.predict(X[{self.best_subset}]), t=transform['forward'](threshold, extra) "
                    f"ranged [{np.min(t):.3f}, {np.max(t):.3f}] over {len(p_raw)} row(s); "
                    f"{clip_rate:.1%} of raw probabilities fell outside [eps, 1-eps] and were clipped.")
        y_hat_dollar = self.transform["inverse"](mu_hat, extra_arr)
        return p_clipped, y_hat_dollar
