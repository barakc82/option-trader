"""XGBoost as an eighth survival method, alongside logistic_survival.py's
seventh.

Same reason logistic needs the k-grid replication trick applies here
unchanged: a tree ensemble has no notion of "point prediction plus residual
distribution" to threshold at predict time, so the stop has to be a feature
the model was trained to condition on (log_k = log(stop/credit)), exactly as
in logistic_survival.py. Rather than reimplementing the replicate/predict
scaffolding, this module reuses logistic_survival.build_k_grid,
replicate_for_training, build_predict_frame and columns_for_subset
byte-for-byte -- this is also how "same features as logistic regression" is
guaranteed: both methods search the exact same CANDIDATE_FEATURE_COLUMNS
list, replicated across the exact same k-grid, with log_k always included
and never a candidate for removal.

Where this diverges from logistic: a boosted tree has no coefficient to
sanity-check the sign of (_assert_valid_coefficients has no XGBoost
equivalent), so the "further stop => never-lower survival probability"
invariant is instead enforced structurally via monotone_constraints on
log_k -- XGBoost refuses to fit a tree split that would violate it, rather
than fitting freely and then checking after the fact.

Produces the same best_subset.CandidateResult objects logistic_survival.py
does, meant to be pooled into the same all_candidates list in
select_probability_model.py -- the scorer/CV-loop/report code needs no
changes to accept them.
"""
from __future__ import annotations

import itertools
import logging
import time

import numpy as np
import pandas as pd
from sklearn.model_selection import GroupKFold
from xgboost import XGBClassifier

from .best_subset import MAX_STALE_SIZES, CandidateResult, ScoreMethod
from .logistic_survival import (
    LOG_K_COLUMN, build_k_grid, build_predict_frame, columns_for_subset,
    replicate_for_training, select_logistic_recommended, trade_weight,
)
from .regress_max_ask import CV_FOLDS, CV_RANDOM_STATE, GROUP_COLUMN
from .survival_scoring import clip_probabilities, compute_survival_label

logger = logging.getLogger(__name__)

TRANSFORM_NAME = "xgboost"

# --- config -----------------------------------------------------------------
# Deliberately small trees/few rounds: this is fit 2**p - 1 times per fold
# (best-subset search), so per-fit cost matters far more here than it would
# for a single production fit. Raise these only after checking the search
# still finishes inside XGBOOST_SEARCH_TIME_BUDGET_SEC.
XGBOOST_PARAMS = dict(
    n_estimators=100,
    max_depth=3,
    learning_rate=0.1,
    subsample=0.8,
    colsample_bytree=0.8,
    reg_lambda=1.0,
    n_jobs=1,
    random_state=CV_RANDOM_STATE,
    verbosity=0,
)

# Best-subset search over p non-log_k features is 2**p - 1 subsets, each
# fitted n_folds times on n_train_trades * n_grid rows -- same shape of cost
# as logistic_survival's search, just a heavier per-fit cost (boosted trees
# vs. a single closed-form-ish logistic fit). Timed via one calibration fit
# before the real search starts; if the projected total exceeds this, the
# search raises instead of silently running for hours.
XGBOOST_SEARCH_TIME_BUDGET_SEC = 14400.0


class XgboostSearchBudgetExceeded(RuntimeError):
    """Raised by estimate_and_check_cost when the projected search runtime
    exceeds XGBOOST_SEARCH_TIME_BUDGET_SEC. Callers should catch this
    specifically and skip the xgboost method for this side rather than
    aborting the whole run -- the other methods' results are still valid."""


# ---------------------------------------------------------------------------
# Fitting
# ---------------------------------------------------------------------------

def _monotone_constraints(columns: list[str]) -> tuple[int, ...]:
    """+1 on log_k (a further-away stop must never make the fitted survival
    probability go down), 0 (unconstrained) on every other column. This is
    the structural equivalent of logistic_survival's
    _assert_valid_coefficients log_k-sign check: here it is enforced by
    XGBoost's tree-building itself, not verified after the fact."""
    return tuple(1 if c == LOG_K_COLUMN else 0 for c in columns)


def fit_xgboost(X_rep: pd.DataFrame, y_rep: np.ndarray, weight_rep: np.ndarray, columns: list[str],
                params: dict = XGBOOST_PARAMS) -> XGBClassifier:
    model = XGBClassifier(**params, monotone_constraints=_monotone_constraints(columns))
    model.fit(X_rep[columns], y_rep, sample_weight=weight_rep)
    return model


# ---------------------------------------------------------------------------
# Cost guard
# ---------------------------------------------------------------------------

def estimate_and_check_cost(X: pd.DataFrame, ctx: pd.DataFrame, feature_names: list[str], k_grid: np.ndarray,
                             fold_splits: list, params: dict = XGBOOST_PARAMS,
                             time_budget_sec: float = XGBOOST_SEARCH_TIME_BUDGET_SEC) -> float:
    """Times ONE calibration fit (full feature set, fold 0's training split)
    and projects the total search time. Prints the estimate either way;
    raises XgboostSearchBudgetExceeded (not a silent proceed) if it exceeds
    the budget."""
    n_subsets = 2 ** len(feature_names) - 1
    n_total_fits = n_subsets * len(fold_splits)

    train_idx0, _ = fold_splits[0]
    X_train0, ctx_train0 = X.iloc[train_idx0], ctx.iloc[train_idx0]
    columns = columns_for_subset(feature_names)

    X_rep0, y_rep0, w_rep0, _ = replicate_for_training(X_train0, ctx_train0, feature_names, k_grid)

    t0 = time.time()
    fit_xgboost(X_rep0, y_rep0, w_rep0, columns, params)
    calibration_fit_seconds = time.time() - t0

    estimated_total_sec = calibration_fit_seconds * n_total_fits
    print(f"    xgboost best-subset search: {n_subsets} subsets x {len(fold_splits)} folds = "
          f"{n_total_fits} fits; calibration fit took {calibration_fit_seconds:.3f}s -> "
          f"estimated total {estimated_total_sec / 60:.1f} min (budget {time_budget_sec / 60:.1f} min)")

    if estimated_total_sec > time_budget_sec:
        raise XgboostSearchBudgetExceeded(
            f"xgboost best-subset search estimated at {estimated_total_sec / 60:.1f} min, exceeding the "
            f"{time_budget_sec / 60:.1f} min budget (XGBOOST_SEARCH_TIME_BUDGET_SEC). Not running silently "
            f"for hours. Options: reduce XGBOOST_PARAMS['n_estimators']/['max_depth'], cap the max subset "
            f"size searched (e.g. only sizes 1..6), or switch to forward stepwise selection instead of "
            f"exhaustive best-subset. Raise the budget explicitly if you actually want the full search to run."
        )
    return estimated_total_sec


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def search_xgboost_candidates(X: pd.DataFrame, ctx: pd.DataFrame, feature_names: list[str],
                               score_method: ScoreMethod, cv: int = CV_FOLDS,
                               params: dict = XGBOOST_PARAMS,
                               time_budget_sec: float = XGBOOST_SEARCH_TIME_BUDGET_SEC) -> list[CandidateResult]:
    """Exhaustive best-subset search over feature_names (log_k is always
    included and never a candidate for removal -- see module docstring).
    Fits on the replicated grid, scores on the one-row-per-trade prediction
    representation (see logistic_survival.build_predict_frame) using
    score_method -- the exact representation every other registered method
    is scored on. Returns every evaluated CandidateResult
    (transform_name="xgboost", distribution=None), meant to be pooled into
    the same list the other methods' candidates go into."""
    groups = ctx[GROUP_COLUMN]
    n_splits = max(2, min(cv, groups.nunique()))
    print(f"    Number of trade days: {groups.nunique()}, number of samples: {X.shape[0]}")
    gkf = GroupKFold(n_splits=n_splits, shuffle=True, random_state=CV_RANDOM_STATE)
    fold_splits = list(gkf.split(np.zeros(len(X)), groups=groups))

    k_grid = build_k_grid()

    estimate_and_check_cost(X, ctx, feature_names, k_grid, fold_splits, params, time_budget_sec)

    y_survived_trades = compute_survival_label(ctx)
    weight_trades = trade_weight(ctx)

    results: list[CandidateResult] = []
    best_score = np.inf
    stale_sizes = 0
    for size in range(1, len(feature_names) + 1):
        print(f"    Working on size {size}")
        improved_this_size = False
        for subset in itertools.combinations(feature_names, size):
            subset = list(subset)
            columns = columns_for_subset(subset)

            oof_p = np.full(len(X), np.nan)
            fold_scores = []
            for fold_idx, (train_idx, test_idx) in enumerate(fold_splits):
                X_train, ctx_train = X.iloc[train_idx], ctx.iloc[train_idx]
                X_rep, y_rep, w_rep, _ = replicate_for_training(X_train, ctx_train, subset, k_grid)

                model = fit_xgboost(X_rep, y_rep, w_rep, columns, params)

                X_test_pred = build_predict_frame(X.iloc[test_idx], ctx.iloc[test_idx], subset)
                p_raw = model.predict_proba(X_test_pred[columns])[:, 1]
                p_clipped, _ = clip_probabilities(p_raw)
                oof_p[test_idx] = p_clipped

                w_fold = weight_trades[test_idx] if score_method.needs_weight else None
                fold_scores.append(score_method.compute(p_clipped, y_survived_trades[test_idx], w_fold))

            w_full = weight_trades if score_method.needs_weight else None
            score = score_method.compute(oof_p, y_survived_trades, w_full)
            results.append(CandidateResult(TRANSFORM_NAME, subset, None, score, fold_scores))
            if score < best_score:
                best_score = score
                improved_this_size = True
                print(f"    Size {size} improves best score to {score:.4f}, features: {subset}")

        if improved_this_size:
            stale_sizes = 0
        else:
            stale_sizes += 1
            if stale_sizes >= MAX_STALE_SIZES:
                print(f"    No improvement for {MAX_STALE_SIZES} consecutive sizes, stopping at size {size}")
                break

    return results


# ---------------------------------------------------------------------------
# Recommended-subset tie-break (display/diagnostic only)
# ---------------------------------------------------------------------------

# select_logistic_recommended is fully generic over CandidateResult.score/
# .subset/.fold_scores -- nothing in it is logistic-specific -- so it is
# reused here as-is rather than duplicated.
select_xgboost_recommended = select_logistic_recommended


# ---------------------------------------------------------------------------
# Shipped-artifact wrapper
# ---------------------------------------------------------------------------

class XgboostSurvivalClassifier:
    """Same (X_new, threshold) -> (probability, y_hat) calling convention as
    ProbabilityClassifier/TransformedResidualClassifier/
    LogisticSurvivalClassifier, so downstream code doesn't need to branch on
    which method won. There is no dollar point-estimate for this model
    either, so y_hat is NaN, same as LogisticSurvivalClassifier."""

    def __init__(self, model: XGBClassifier, subset: list[str], columns: list[str]):
        self.model = model
        self.subset = subset
        self.columns = columns

    def __call__(self, X_new: pd.DataFrame, threshold):
        from .survival_scoring import CREDIT_COLUMN  # local import: avoids a module-level cycle risk

        credit = X_new[CREDIT_COLUMN].to_numpy()
        log_k = np.log(np.asarray(threshold, dtype=float) / credit)
        X_pred = X_new[self.subset].reset_index(drop=True).copy()
        X_pred[LOG_K_COLUMN] = log_k
        X_pred = X_pred[self.columns]

        nan_mask = X_pred.isna().any()
        if nan_mask.any():
            raise ValueError(f"XgboostSurvivalClassifier: NaN input in column(s) "
                              f"{nan_mask.index[nan_mask].tolist()}; refusing to call predict_proba")

        p_raw = self.model.predict_proba(X_pred)[:, 1]
        p_clipped, clip_rate = clip_probabilities(p_raw)
        logger.info(f"XgboostSurvivalClassifier: p = model.predict_proba(X[{self.columns}])[:, 1] "
                    f"from the persisted fit on subset={self.subset}; log_k=log(threshold/credit) ranged "
                    f"[{log_k.min():.3f}, {log_k.max():.3f}] over {len(p_raw)} row(s); "
                    f"{clip_rate:.1%} of raw probabilities fell outside [eps, 1-eps] and were clipped.")
        y_hat = np.full(len(X_new), np.nan)
        return p_clipped, y_hat
