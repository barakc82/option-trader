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
import math
from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.stats import norm
from sklearn.linear_model import LinearRegression

from .regress_max_ask import GROUP_COLUMN, TARGET_COLUMN

logger = logging.getLogger(__name__)

STOP_COLUMN = "stop_loss"
CREDIT_COLUMN = "estimated_sell_price"
MINUTES_TO_EXPIRATION_COLUMN = "minutes_to_expiration"

# ctx (the non-feature columns the scoring machinery needs alongside X)
# always carries at least these -- max_ask/credit/stop for label and weight
# computation, minutes_to_expiration for the weight's time-range multiplier,
# plus GROUP_COLUMN for the nested best-subset search's own GroupKFold.
CTX_COLUMNS = [TARGET_COLUMN, CREDIT_COLUMN, STOP_COLUMN, MINUTES_TO_EXPIRATION_COLUMN, GROUP_COLUMN]

# Extra weight for trades sampled within certain minutes-to-expiration ranges, applied on
# top of the stop_loss - estimated_sell_price weight (see trade_weight). Each entry is
# (min_minutes inclusive, max_minutes exclusive, multiplier); a trade outside every range
# gets a multiplier of 1, and if ranges overlap the first matching one wins.
# Left empty (all multipliers 1) until filled in, e.g.:
#     (0, 60, 3.0),       # last hour before expiration counts 3x
#     (60, 390, 1.5),     # rest of the expiration day counts 1.5x
MINUTES_TO_EXPIRATION_WEIGHT_RANGES: list[tuple[float, float, float]] = [
    # TODO: fill in (min_minutes, max_minutes, multiplier) ranges
    (4400, 4315, 3),
    (4315, 4305, 2),
    (1441, 1435, 3),
    (1435, 1425, 2),
    (1185, 1170, 3),
    (1170, 1050, 2)
]

EPS = 1e-6
EMPIRICAL_SMOOTHING = "laplace"  # "laplace" | "none"


# ---------------------------------------------------------------------------
# Weight
# ---------------------------------------------------------------------------

def minutes_to_expiration_multiplier(minutes_to_expiration: np.ndarray,
                                     ranges=MINUTES_TO_EXPIRATION_WEIGHT_RANGES) -> np.ndarray:
    """Per-row multiplier from MINUTES_TO_EXPIRATION_WEIGHT_RANGES (1 outside every range)."""
    minutes = np.asarray(minutes_to_expiration, dtype=float)
    multiplier = np.ones(len(minutes))
    matched = np.zeros(len(minutes), dtype=bool)
    for min_minutes, max_minutes, range_multiplier in ranges:
        in_range = ~matched & (minutes >= min_minutes) & (minutes < max_minutes)
        multiplier[in_range] = range_multiplier
        matched |= in_range
    return multiplier


def trade_weight(ctx: pd.DataFrame) -> np.ndarray:
    """The single per-trade weight every weighted_logloss score uses -- the
    regression+distribution search, logistic and xgboost alike, so their pooled
    scores stay directly comparable: the dollar room between the stop and the credit
    received (stop_loss - estimated_sell_price), times the trade's
    minutes-to-expiration range multiplier."""
    room = (ctx[STOP_COLUMN] - ctx[CREDIT_COLUMN]).to_numpy()
    return room * minutes_to_expiration_multiplier(ctx[MINUTES_TO_EXPIRATION_COLUMN].to_numpy())


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
# Feature-count cap
# ---------------------------------------------------------------------------

# A feature subset may have at most one feature per this many trading days with a
# stop-loss hit (an "events per variable" rule of thumb, counting days rather than
# trades since hits on the same day are strongly correlated): with few such days,
# large subsets just overfit them.
STOP_LOSS_HIT_DAYS_PER_FEATURE = 10


def max_feature_count(ctx: pd.DataFrame, n_candidate_features: int) -> int:
    """Largest feature-subset size the best-subset searches may try:
    ceil(trading days with a stop-loss hit / STOP_LOSS_HIT_DAYS_PER_FEATURE). A hit is a
    trade whose max_ask reached the stop (the complement of compute_survival_label), and
    a trading day is a distinct GROUP_COLUMN value -- the same grouping the CV folds use.
    At least 1, so a search always has something to evaluate, and at most
    n_candidate_features."""
    is_hit = compute_survival_label(ctx) == 0
    hit_days = ctx.loc[is_hit, GROUP_COLUMN].nunique()
    cap = min(max(1, math.ceil(hit_days / STOP_LOSS_HIT_DAYS_PER_FEATURE)), n_candidate_features)
    print(f"    Feature count capped at {cap} ({hit_days} trading day(s) with a stop-loss hit / "
          f"{STOP_LOSS_HIT_DAYS_PER_FEATURE}, {n_candidate_features} candidate feature(s))")
    return cap


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

class InterceptOnlyRegression:
    """The zero-feature baseline for the linear families: predicts the mean of the
    training target for every row. sklearn's LinearRegression refuses zero columns,
    so this stands in for it with the same fit/predict/score/coef_/intercept_ surface,
    and is picklable for the shipped artifact."""

    def fit(self, X, y, sample_weight=None):
        y = np.asarray(y, dtype=float)
        self.intercept_ = float(np.average(y, weights=sample_weight))
        self.coef_ = np.zeros(0)
        return self

    def predict(self, X):
        return np.full(len(X), self.intercept_)

    def score(self, X, y):
        """R^2, which is 0 on the training data by construction."""
        y = np.asarray(y, dtype=float)
        total = float(np.sum((y - y.mean()) ** 2))
        residual = float(np.sum((y - self.predict(X)) ** 2))
        return 1.0 - residual / total if total > 0 else 0.0


def make_linear_regression(n_features: int):
    """LinearRegression for a non-empty feature subset, InterceptOnlyRegression for the
    zero-feature baseline."""
    return LinearRegression() if n_features else InterceptOnlyRegression()


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

    def get_feature_subset(self) -> list[str]:
        """The X_new columns __call__ actually reads: best_subset plus the
        transform's extra_column (e.g. log_ratio's denominator), if any."""
        features = list(self.best_subset)
        if self.extra_column is not None and self.extra_column not in features:
            features.append(self.extra_column)
        return features
