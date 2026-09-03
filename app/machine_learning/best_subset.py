"""Exhaustive best-subset feature selection via GroupKFold CV, plus the
pluggable SCORE_FN family used to judge candidate subsets.

Three scoring methods are registered in SCORE_METHODS:
  - "rss": dollar-space (or log-space, or log-ratio-space) squared error of
    the point prediction against the fitting target. This is the ORIGINAL
    criterion (best_subset_for_target, unchanged below) and is still
    available, but is no longer the default -- least-squares against
    max_ask minimises dollar-space squared error by construction, so
    scoring candidates on it hands the "raw" target the metric it was
    fitted to, and different transforms' RSS values are not directly
    comparable (hence the old dollar-space rescoring step in
    select_probability_model.py, still used only for this method).
  - "logloss": logloss of P(max_ask < stop), the shared question every
    target transform answers once its point prediction is converted to a
    probability via a residual distribution (normal or empirical, see
    survival_scoring.py). Comparable across transforms directly, with no
    rescoring step needed.
  - "weighted_logloss" (DEFAULT): same as "logloss", but each row's
    contribution is weighted by stop_loss - estimated_sell_price (the
    dollar room between the stop and the credit received) rather than
    every row counting equally.

search_best_subset_with_distribution implements the logloss-family search:
for each candidate feature subset, ONE inner GroupKFold pass produces
per-fold training residuals and OOF point predictions (identical cost to
the RSS path's cross_val_predict); if the active score method needs a
residual distribution, BOTH "normal" and "empirical" variants are derived
cheaply from those same per-fold residuals (fitting a residual distribution
is O(n), not another regression fit) -- so the (subset x distribution) axis
costs only a little more than subset alone, not twice the regression
fitting. Every evaluated (subset, distribution) combination is returned,
not just the best, so callers can compute a standard error from the
winner's per-fold scores and count how many combinations fall within it
(the "one-standard-error rule" convention -- e.g. glmnet's lambda.1se --
for flagging when the apparent winner isn't distinguishable from its
runners-up).
"""
import itertools
from dataclasses import dataclass

import numpy as np
import pandas as pd
from sklearn.linear_model import LinearRegression
from sklearn.model_selection import GroupKFold, cross_val_predict

from .regress_max_ask import CV_FOLDS, CV_RANDOM_STATE
from .survival_scoring import (
    CREDIT_COLUMN, EPS, GROUP_COLUMN, RESIDUAL_DISTRIBUTIONS, STOP_COLUMN, TARGET_COLUMN,
    clip_probabilities, compute_survival_label, logloss,
)


# ---------------------------------------------------------------------------
# RSS path -- unchanged
# ---------------------------------------------------------------------------

def rss_score(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    """Lower is better."""
    return float(np.sum((y_true - y_pred) ** 2))


SCORE_FN = rss_score


def best_subset_for_target(X: pd.DataFrame, y: pd.Series, groups: pd.Series, feature_names: list[str],
                            score_fn=SCORE_FN, cv: int = CV_FOLDS) -> tuple[list[str], float, np.ndarray]:
    """Exhaustive best-subset selection via GroupKFold CV, scored by
    score_fn on y's own scale. Mirrors regress_max_ask.py's
    select_best_subset exactly, generalized to take y and score_fn as
    inputs (that function hardcodes RSS on the raw target and is left
    unmodified).

    Whatever (X, y, groups) this is called with is treated as the entire
    universe of fittable data -- callers that want a nested, nothing-leaks
    subset search need only restrict X/y/groups to a training fold before
    calling this; nothing in this function's own GroupKFold usage
    distinguishes an "outer" call from a "nested inner" one.

    Returns (best_subset, best_score, oof_predictions_for_best_subset).
    """
    n_splits = max(2, min(cv, groups.nunique()))
    print(f"    Number of trade days: {groups.nunique()}, number of samples: {X.shape[0]}")
    group_kfold = GroupKFold(n_splits=n_splits, shuffle=True, random_state=CV_RANDOM_STATE)

    best_subset, best_score, best_oof_pred = [], np.inf, None
    y_arr = y.to_numpy()
    for size in range(1, len(feature_names) + 1):
        print(f"    Working on size {size}")
        for subset in itertools.combinations(feature_names, size):
            subset = list(subset)
            y_pred = cross_val_predict(LinearRegression(), X[subset], y, cv=group_kfold, groups=groups)
            score = score_fn(y_arr, y_pred)
            if score < best_score:
                best_subset, best_score, best_oof_pred = subset, score, y_pred
                print(f"    Number of features: {len(subset)}, best score: {score:.4f}, features: {subset}")

    return best_subset, best_score, best_oof_pred


# ---------------------------------------------------------------------------
# Logloss-family scoring
# ---------------------------------------------------------------------------

def logloss_score(p: np.ndarray, y: np.ndarray, weight: np.ndarray | None = None) -> float:
    """Unweighted logloss. `weight` is accepted (and ignored) so this has
    the same call signature as weighted_logloss_score."""
    return logloss(p, y)


def weighted_logloss_score(p: np.ndarray, y: np.ndarray, weight: np.ndarray) -> float:
    """Logloss with each row weighted by `weight` (stop_loss -
    estimated_sell_price -- the dollar room between the stop and the credit
    received) instead of counting every row equally."""
    p = np.clip(p, EPS, 1 - EPS)
    per_row = -(y * np.log(p) + (1 - y) * np.log(1 - p))
    w = np.asarray(weight, dtype=float)
    total_w = np.sum(w)
    if total_w <= 0:
        return float("nan")
    return float(np.sum(w * per_row) / total_w)


@dataclass(frozen=True)
class ScoreMethod:
    name: str
    needs_distribution: bool  # requires a residual distribution to turn a point prediction into a probability
    needs_weight: bool
    compute: object  # Callable[[np.ndarray, np.ndarray, np.ndarray | None], float] or Callable[[np.ndarray, np.ndarray], float] for rss


SCORE_METHODS: dict[str, ScoreMethod] = {
    "rss": ScoreMethod("rss", needs_distribution=False, needs_weight=False, compute=rss_score),
    "logloss": ScoreMethod("logloss", needs_distribution=True, needs_weight=False, compute=logloss_score),
    "weighted_logloss": ScoreMethod("weighted_logloss", needs_distribution=True, needs_weight=True,
                                     compute=weighted_logloss_score),
}
DEFAULT_SCORE_METHOD_NAME = "weighted_logloss"


@dataclass
class CandidateResult:
    transform_name: str
    subset: list[str]
    distribution: str | None  # None for point-based (rss) candidates, and for methods with no distribution axis at all (e.g. "logistic")
    score: float
    fold_scores: list[float]
    # Optional, method-specific extras -- None for the six (transform x
    # distribution) candidates. Added so a method like "logistic" that
    # carries its own coefficient diagnostics can still be pooled through
    # this exact same dataclass/list, satisfying "the scorer, the CV loop
    # and the report must need no changes" for the code that only ever
    # reads .score/.fold_scores/.subset. Purely additive: existing
    # candidates never set these, so nothing about the six existing
    # methods' values or behavior changes.
    log_k_coef: float | None = None
    max_abs_coef: float | None = None


def search_best_subset_with_distribution(X: pd.DataFrame, ctx: pd.DataFrame, transform: dict, transform_name: str,
                                          feature_names: list[str], score_method: ScoreMethod,
                                          cv: int = CV_FOLDS) -> list[CandidateResult]:
    """See module docstring. Returns every evaluated (subset[, distribution])
    CandidateResult for this one transform; only meaningful for
    score_method.needs_distribution -- callers wanting the RSS path should
    use best_subset_for_target instead."""
    groups = ctx[GROUP_COLUMN]
    y_raw = ctx[TARGET_COLUMN].to_numpy()
    extra_col = transform["extra_column"]
    extra_arr = ctx[extra_col].to_numpy() if extra_col is not None else None
    y_transformed = transform["forward"](y_raw, extra_arr)
    stop_scale = transform["forward"](ctx[STOP_COLUMN].to_numpy(), extra_arr)
    y_survived = compute_survival_label(ctx)
    weight = (ctx[STOP_COLUMN] - ctx[CREDIT_COLUMN]).to_numpy()

    n_splits = max(2, min(cv, groups.nunique()))
    print(f"    Number of trade days: {groups.nunique()}, number of samples: {X.shape[0]}")
    gkf = GroupKFold(n_splits=n_splits, shuffle=True, random_state=CV_RANDOM_STATE)
    fold_splits = list(gkf.split(np.zeros(len(X)), groups=groups))

    distribution_kinds = list(RESIDUAL_DISTRIBUTIONS) if score_method.needs_distribution else [None]

    results: list[CandidateResult] = []
    for size in range(1, len(feature_names) + 1):
        print(f"    Working on size {size}")
        for subset in itertools.combinations(feature_names, size):
            subset = list(subset)
            X_subset = X[subset].to_numpy()

            oof_mu = np.full(len(X), np.nan)
            fold_train_residuals = []
            for train_idx, test_idx in fold_splits:
                lr = LinearRegression()
                lr.fit(X_subset[train_idx], y_transformed[train_idx])
                mu_train = lr.predict(X_subset[train_idx])
                oof_mu[test_idx] = lr.predict(X_subset[test_idx])
                fold_train_residuals.append(y_transformed[train_idx] - mu_train)

            if not score_method.needs_distribution:
                score = score_method.compute(y_transformed, oof_mu)
                fold_scores = [score_method.compute(y_transformed[test_idx], oof_mu[test_idx])
                               for _, test_idx in fold_splits]
                results.append(CandidateResult(transform_name, subset, None, score, fold_scores))
                continue

            for dist_kind in distribution_kinds:
                oof_p = np.full(len(X), np.nan)
                for (train_idx, test_idx), train_res in zip(fold_splits, fold_train_residuals):
                    dist = RESIDUAL_DISTRIBUTIONS[dist_kind]()
                    dist.fit(train_res)
                    t_minus_mu = stop_scale[test_idx] - oof_mu[test_idx]
                    p_raw = dist.survival_prob(t_minus_mu)
                    p_clipped, _ = clip_probabilities(p_raw)
                    oof_p[test_idx] = p_clipped

                w_full = weight if score_method.needs_weight else None
                score = score_method.compute(oof_p, y_survived, w_full)
                fold_scores = []
                for _, test_idx in fold_splits:
                    w_fold = weight[test_idx] if score_method.needs_weight else None
                    fold_scores.append(score_method.compute(oof_p[test_idx], y_survived[test_idx], w_fold))
                results.append(CandidateResult(transform_name, subset, dist_kind, score, fold_scores))

    return results


def standard_error_from_fold_scores(fold_scores: list[float]) -> float:
    """Standard error of the K-fold CV score estimate, treating each fold's
    held-out score as one quasi-independent replicate: std(fold_scores,
    ddof=1) / sqrt(K). This is the convention behind the well-known
    "one-standard-error rule" for model selection (e.g. glmnet's
    lambda.1se): a model within one SE of the apparent best is not
    statistically distinguishable from it on this sample."""
    arr = np.asarray(fold_scores, dtype=float)
    if len(arr) < 2:
        return float("nan")
    return float(np.std(arr, ddof=1) / np.sqrt(len(arr)))


def count_within_one_se(candidates: list[CandidateResult], best_score: float, se: float) -> int:
    """How many candidates (including the best itself) have score <=
    best_score + se. Returns 1 (only the best) if se isn't a usable number
    (e.g. fewer than 2 folds) -- we can't say anything about "within SE"
    without one."""
    if not np.isfinite(se):
        return 1
    return sum(1 for c in candidates if c.score <= best_score + se)
