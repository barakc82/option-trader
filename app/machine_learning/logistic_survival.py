"""Logistic regression as a seventh survival method.

Predicts P(max_ask < stop) directly from the trade's entry features plus
log(stop/credit) -- no point estimate, no residual distribution. This is
exactly the case best_subset.py's (target transform x residual distribution)
composition was never meant to cover: there is no "point prediction" here to
convert into a probability, the model IS the probability. Rather than
bending that composition to fit, this module produces its own
best_subset.CandidateResult objects and hands them to the same pooling,
standard-error, and reporting code in select_probability_model.py --
unchanged, per the non-goal that the scorer/CV-loop/report must not change
to accommodate this.

Why replicate each trade across a k-grid: logistic regression has no
distribution to threshold at prediction time, so the stop has to be a
feature the model was trained to condition on (log_k = log(stop/credit)).
One row per trade at its own actual stop would give the model exactly one
bit of information per trade (survived or not, at that one k) and no way to
learn how survival probability falls off as k varies -- the whole point of
including log_k as a predictor. So each trade contributes one row per point
on a FIXED grid, weighted 1/n_grid so the effective sample size stays one
trade, not one-per-replicate.

Non-negotiable, repeated from the change request because it is easy to get
wrong in a way that looks reasonable: the grid must never be derived from
the trade's own outcome (e.g. the two points bracketing its realised R).
That construction forces every trade to contribute exactly one hit and one
miss, pins the apparent hit rate to 50% at every stop level regardless of
the true rate, and attenuates every feature's coefficient. If a change to
this module starts computing grid points per-trade, stop and reconsider --
it almost certainly reintroduces exactly this bug.
"""
from __future__ import annotations

import itertools
import logging
import random
import time
from dataclasses import dataclass

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import GroupKFold
from sklearn.preprocessing import StandardScaler

from .best_subset import CandidateResult, ScoreMethod
from .regress_max_ask import CV_FOLDS, CV_RANDOM_STATE, GROUP_COLUMN
from .survival_scoring import CREDIT_COLUMN, STOP_COLUMN, TARGET_COLUMN, clip_probabilities, compute_survival_label

logger = logging.getLogger(__name__)

LOG_K_COLUMN = "log_k"
TRANSFORM_NAME = "logistic"

# --- config -----------------------------------------------------------------
LOGISTIC_K_GRID = {"n": 15, "lo": 1.25, "hi": 8.0, "spacing": "log"}
LOGISTIC_C = 1.0
LOGISTIC_MAX_ITER = 1000

# Best-subset search over p non-log_k features is 2**p - 1 subsets, each
# fitted n_folds times on n_train_trades * n_grid rows. Timed via one
# calibration fit before the real search starts; if the projected total
# exceeds this, the search raises instead of silently running for hours.
LOGISTIC_SEARCH_TIME_BUDGET_SEC = 3600.0

# Optional, off by default -- see module docstring section in the change
# request. Both are deliberately simplified from the literal ask (a real
# natural-spline basis, or a proper canonical-signal PCA/median collapse)
# to keep this addition scoped; documented at each use site.
DELTA_COLLAPSE_ENABLED = False
DELTA_COLUMNS_TO_COLLAPSE = ["bid_delta", "ask_delta", "last_delta", "model_delta"]
LOG_K_SPLINE_ENABLED = False


class LogisticFitError(ValueError):
    """Raised when a fold/subset's fitted coefficients can't be trusted --
    non-finite, or a non-positive log_k coefficient. Never caught to
    silently fall back to returning probabilities anyway; see
    _assert_valid_coefficients."""


class LogisticSearchBudgetExceeded(RuntimeError):
    """Raised by estimate_and_check_cost when the projected search runtime
    exceeds LOGISTIC_SEARCH_TIME_BUDGET_SEC. Callers should catch this
    specifically and skip the logistic method for this side rather than
    aborting the whole run -- the other methods' results are still valid."""


# ---------------------------------------------------------------------------
# Grid
# ---------------------------------------------------------------------------

def build_k_grid(grid_config: dict = LOGISTIC_K_GRID) -> np.ndarray:
    """Fixed grid, identical for every trade, independent of any trade's own
    outcome -- see module docstring."""
    n, lo, hi, spacing = grid_config["n"], grid_config["lo"], grid_config["hi"], grid_config["spacing"]
    if spacing == "log":
        return np.exp(np.linspace(np.log(lo), np.log(hi), n))
    elif spacing == "linear":
        return np.linspace(lo, hi, n)
    raise ValueError(f"unknown spacing {spacing!r}, expected 'log' or 'linear'")


def extrapolation_fraction(ctx: pd.DataFrame, k_grid: np.ndarray) -> float:
    """Fraction of trades whose actual log(stop/credit) falls outside the
    training grid's [log(min), log(max)] range -- those trades are
    evaluated by extrapolating the fitted logistic curve beyond where it
    was ever trained."""
    log_k_actual = np.log(ctx[STOP_COLUMN].to_numpy() / ctx[CREDIT_COLUMN].to_numpy())
    lo, hi = np.log(k_grid.min()), np.log(k_grid.max())
    outside = (log_k_actual < lo) | (log_k_actual > hi)
    return float(np.mean(outside))


def trade_weight(ctx: pd.DataFrame) -> np.ndarray:
    """Same per-trade weight convention as best_subset.py's
    search_best_subset_with_distribution (stop_loss - estimated_sell_price)
    -- kept identical so logistic's trade-level scoring is directly
    comparable to the other methods' under the same weighted_logloss."""
    return (ctx[STOP_COLUMN] - ctx[CREDIT_COLUMN]).to_numpy()


def _feature_columns(feature_names: list[str]) -> list[str]:
    """Applies DELTA_COLLAPSE_ENABLED (if on) to a candidate feature list --
    replaces the four IB delta streams with their row-wise median, keeping
    max_delta/target_delta separate. Simplified stand-in for the fuller
    "canonical signal" idea in the change request; only touches which
    *names* are searched over, applied consistently at both replicate and
    predict time via _apply_delta_collapse below."""
    if not DELTA_COLLAPSE_ENABLED:
        return list(feature_names)
    collapsed_present = [c for c in DELTA_COLUMNS_TO_COLLAPSE if c in feature_names]
    if not collapsed_present:
        return list(feature_names)
    kept = [c for c in feature_names if c not in collapsed_present]
    return kept + ["delta_collapsed"]


def _apply_feature_engineering(frame: pd.DataFrame) -> pd.DataFrame:
    """Adds delta_collapsed (if DELTA_COLLAPSE_ENABLED) and the log_k_spline
    proxy columns (if LOG_K_SPLINE_ENABLED) to an already-built
    replicate/predict frame that has LOG_K_COLUMN present. Must be called
    identically at both training and prediction time."""
    frame = frame.copy()
    if DELTA_COLLAPSE_ENABLED:
        present = [c for c in DELTA_COLUMNS_TO_COLLAPSE if c in frame.columns]
        if present:
            frame["delta_collapsed"] = frame[present].median(axis=1)
    if LOG_K_SPLINE_ENABLED:
        # Simplified stand-in for a natural spline basis on log_k: a
        # quadratic term plus a log_k x atm_iv interaction, letting the
        # falloff shape vary with implied vol rather than being one fixed
        # slope. Try LOG_K_SPLINE_ENABLED=False first; only turn this on if
        # it improves OOF logloss (see change request section 5).
        frame["log_k_sq"] = frame[LOG_K_COLUMN] ** 2
        if "atm_iv" in frame.columns:
            frame["log_k_x_atm_iv"] = frame[LOG_K_COLUMN] * frame["atm_iv"]
    return frame


def _always_included_columns() -> list[str]:
    cols = [LOG_K_COLUMN]
    if LOG_K_SPLINE_ENABLED:
        cols += ["log_k_sq"]  # log_k_x_atm_iv added conditionally below at call sites if atm_iv present
    return cols


def columns_for_subset(subset: list[str]) -> list[str]:
    """The full column list a fitted model for `subset` actually needs:
    subset + log_k (always) + any optional feature-engineering additions.
    Used consistently by the search, the full-data refit in
    select_probability_model.py, and prediction (LogisticSurvivalClassifier)
    so all three never drift apart on what a given subset's model was
    actually trained on."""
    columns = list(subset) + _always_included_columns()
    if LOG_K_SPLINE_ENABLED and "atm_iv" in subset:
        columns = columns + ["log_k_x_atm_iv"]
    return columns


# ---------------------------------------------------------------------------
# Replicate construction (training) / one-row-per-trade (prediction)
# ---------------------------------------------------------------------------

def replicate_for_training(X: pd.DataFrame, ctx: pd.DataFrame, feature_names: list[str],
                            k_grid: np.ndarray) -> tuple[pd.DataFrame, np.ndarray, np.ndarray, np.ndarray]:
    """One row per (trade, grid point). Returns (X_rep, y_survived_rep,
    weight_rep, groups_rep). X_rep carries feature_names plus LOG_K_COLUMN
    (and any _apply_feature_engineering additions). groups_rep repeats each
    trade's session date across its replicates, so a GroupKFold split by
    session date -- never by replicate index -- keeps every replicate of a
    trade, and every trade from a session date, on the same side."""
    n_trades = len(X)
    n_grid = len(k_grid)
    max_ask = ctx[TARGET_COLUMN].to_numpy()
    credit = ctx[CREDIT_COLUMN].to_numpy()
    groups = ctx[GROUP_COLUMN].to_numpy()
    log_k_grid = np.log(k_grid)

    rep_block = X[feature_names].to_numpy()
    X_rep = np.repeat(rep_block, n_grid, axis=0)
    log_k_rep = np.tile(log_k_grid, n_trades)
    k_rep = np.tile(k_grid, n_trades)
    max_ask_rep = np.repeat(max_ask, n_grid)
    credit_rep = np.repeat(credit, n_grid)
    groups_rep = np.repeat(groups, n_grid)

    y_survived_rep = (max_ask_rep < k_rep * credit_rep).astype(float)
    weight_rep = np.full(len(y_survived_rep), 1.0 / n_grid)

    X_rep_df = pd.DataFrame(X_rep, columns=feature_names)
    X_rep_df[LOG_K_COLUMN] = log_k_rep
    if "atm_iv" in feature_names or LOG_K_SPLINE_ENABLED:
        pass  # atm_iv, if selected, already carried through rep_block above
    X_rep_df = _apply_feature_engineering(X_rep_df)

    return X_rep_df, y_survived_rep, weight_rep, groups_rep


def build_predict_frame(X: pd.DataFrame, ctx: pd.DataFrame, feature_names: list[str]) -> pd.DataFrame:
    """Exactly one row per trade, at that trade's own actual stop. This is
    the ONLY representation ever used for evaluation/scoring, for every
    method including this one -- the grid must never leak into scoring."""
    log_k = np.log(ctx[STOP_COLUMN].to_numpy() / ctx[CREDIT_COLUMN].to_numpy())
    X_pred = X[feature_names].reset_index(drop=True).copy()
    X_pred[LOG_K_COLUMN] = log_k
    X_pred = _apply_feature_engineering(X_pred)
    return X_pred


# ---------------------------------------------------------------------------
# Fitting
# ---------------------------------------------------------------------------

def _assert_valid_coefficients(coefs: np.ndarray, columns: list[str], fold_idx, subset) -> None:
    """Split out from fit_logistic so it's directly unit-testable without
    needing to coerce an actual fit into misbehaving. Raises
    LogisticFitError -- never silently repaired -- on non-finite
    coefficients, or a non-positive log_k coefficient (a further-away stop
    must make survival more likely; a non-positive sign means the fit is
    confused, usually by collinearity or too few hits in this fold/subset,
    and returning probabilities from a fit this confused is the worst
    outcome, not a graceful degradation)."""
    if not np.all(np.isfinite(coefs)):
        raise LogisticFitError(
            f"[fold {fold_idx}, subset {subset}] non-finite coefficients: {coefs}. "
            f"L2 should prevent this -- something is wrong with the input data for this fold/subset."
        )
    log_k_idx = columns.index(LOG_K_COLUMN)
    if coefs[log_k_idx] <= 0:
        raise LogisticFitError(
            f"[fold {fold_idx}, subset {subset}] log_k coefficient must be positive (a further-away "
            f"stop must make survival more likely); got {coefs[log_k_idx]:.6f}. This usually means "
            f"collinearity or too few hits in this fold/subset -- refusing to return probabilities "
            f"from a fit this confused rather than silently producing nonsense."
        )


def fit_logistic(X_rep: pd.DataFrame, y_rep: np.ndarray, weight_rep: np.ndarray, columns: list[str],
                  C: float, fold_idx, subset) -> tuple[LogisticRegression, StandardScaler, np.ndarray]:
    """Standardizes features inside this fold (fit_transform here, transform
    only at predict time -- the scaler is part of what gets persisted/
    returned), fits L2-penalized logistic regression, and asserts the fit
    is trustworthy before returning it.

    class_weight is deliberately NOT set (no 'balanced', no manual weights
    beyond the 1/n_grid replicate weight already in weight_rep).
    class_weight='balanced' distorts predicted probabilities toward an even
    split to fix a class-imbalance metric that was never the goal here --
    calibration (getting P(survive) right, not just ranking trades) is the
    entire point of this model. Do not add it back as an "imbalance fix".
    """
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X_rep[columns])
    # L2 is sklearn's default penalty; passing penalty="l2" explicitly is
    # deprecated as of sklearn 1.8 (superseded by l1_ratio), so L2 is
    # enabled here simply by leaving `penalty` unset rather than by naming
    # it -- still L2, not a behavior change.
    model = LogisticRegression(C=C, max_iter=LOGISTIC_MAX_ITER)
    model.fit(X_scaled, y_rep, sample_weight=weight_rep)

    coefs = model.coef_[0]
    _assert_valid_coefficients(coefs, columns, fold_idx, subset)
    return model, scaler, coefs


# ---------------------------------------------------------------------------
# Cost guard
# ---------------------------------------------------------------------------

def estimate_and_check_cost(X: pd.DataFrame, ctx: pd.DataFrame, feature_names: list[str], k_grid: np.ndarray,
                             fold_splits: list, C: float,
                             time_budget_sec: float = LOGISTIC_SEARCH_TIME_BUDGET_SEC) -> float:
    """Times ONE calibration fit (full feature set, fold 0's training
    split) and projects the total search time. Prints the estimate either
    way; raises LogisticSearchBudgetExceeded (not a silent proceed) if it
    exceeds the budget, naming concrete alternatives rather than just
    failing."""
    n_subsets = 2 ** len(feature_names) - 1
    n_total_fits = n_subsets * len(fold_splits)

    train_idx0, _ = fold_splits[0]
    X_train0, ctx_train0 = X.iloc[train_idx0], ctx.iloc[train_idx0]
    searchable = _feature_columns(feature_names)
    columns = columns_for_subset(searchable)

    X_rep0, y_rep0, w_rep0, _ = replicate_for_training(X_train0, ctx_train0, searchable, k_grid)

    t0 = time.time()
    fit_logistic(X_rep0, y_rep0, w_rep0, columns, C, fold_idx=0, subset=feature_names)
    calibration_fit_seconds = time.time() - t0

    estimated_total_sec = calibration_fit_seconds * n_total_fits
    print(f"    logistic best-subset search: {n_subsets} subsets x {len(fold_splits)} folds = "
          f"{n_total_fits} fits; calibration fit took {calibration_fit_seconds:.3f}s -> "
          f"estimated total {estimated_total_sec / 60:.1f} min (budget {time_budget_sec / 60:.1f} min)")

    if estimated_total_sec > time_budget_sec:
        raise LogisticSearchBudgetExceeded(
            f"logistic best-subset search estimated at {estimated_total_sec / 60:.1f} min, exceeding the "
            f"{time_budget_sec / 60:.1f} min budget (LOGISTIC_SEARCH_TIME_BUDGET_SEC). Not running silently "
            f"for hours. Options: cap the max subset size searched (e.g. only sizes 1..6), or switch to "
            f"forward stepwise selection instead of exhaustive best-subset. Raise the budget explicitly if "
            f"you actually want the full search to run."
        )
    return estimated_total_sec


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def search_logistic_candidates(X: pd.DataFrame, ctx: pd.DataFrame, feature_names: list[str],
                                score_method: ScoreMethod, cv: int = CV_FOLDS,
                                k_grid_config: dict = LOGISTIC_K_GRID, C: float = LOGISTIC_C,
                                time_budget_sec: float = LOGISTIC_SEARCH_TIME_BUDGET_SEC) -> list[CandidateResult]:
    """Exhaustive best-subset search over feature_names (log_k is always
    included and never a candidate for removal -- see module docstring).
    Fits on the replicated grid, scores on the one-row-per-trade prediction
    representation (see build_predict_frame) using score_method, exactly
    the representation every other registered method is scored on. Returns
    every evaluated CandidateResult (transform_name="logistic",
    distribution=None), meant to be pooled into the same list the other
    methods' candidates go into -- the caller's pooling/SE/report code
    needs no changes to accept them.
    """
    groups = ctx[GROUP_COLUMN]
    n_splits = max(2, min(cv, groups.nunique()))
    print(f"    Number of trade days: {groups.nunique()}, number of samples: {X.shape[0]}")
    gkf = GroupKFold(n_splits=n_splits, shuffle=True, random_state=CV_RANDOM_STATE)
    fold_splits = list(gkf.split(np.zeros(len(X)), groups=groups))

    k_grid = build_k_grid(k_grid_config)
    searchable_features = _feature_columns(feature_names)

    estimate_and_check_cost(X, ctx, searchable_features, k_grid, fold_splits, C, time_budget_sec)

    y_survived_trades = compute_survival_label(ctx)
    weight_trades = trade_weight(ctx)

    results: list[CandidateResult] = []
    for size in range(1, len(searchable_features) + 1):
        print(f"    Working on size {size}")
        for subset in itertools.combinations(searchable_features, size):
            subset = list(subset)
            columns = columns_for_subset(subset)

            oof_p = np.full(len(X), np.nan)
            fold_scores = []
            fold_max_abs_coef = []
            fold_log_k_coef = []
            for fold_idx, (train_idx, test_idx) in enumerate(fold_splits):
                X_train, ctx_train = X.iloc[train_idx], ctx.iloc[train_idx]
                X_rep, y_rep, w_rep, _ = replicate_for_training(X_train, ctx_train, subset, k_grid)

                model, scaler, coefs = fit_logistic(X_rep, y_rep, w_rep, columns, C, fold_idx, subset)
                fold_max_abs_coef.append(float(np.max(np.abs(coefs))))
                fold_log_k_coef.append(float(coefs[columns.index(LOG_K_COLUMN)]))

                X_test_pred = build_predict_frame(X.iloc[test_idx], ctx.iloc[test_idx], subset)
                X_test_scaled = scaler.transform(X_test_pred[columns])
                p_raw = model.predict_proba(X_test_scaled)[:, 1]
                p_clipped, _ = clip_probabilities(p_raw)
                oof_p[test_idx] = p_clipped

                w_fold = weight_trades[test_idx] if score_method.needs_weight else None
                fold_scores.append(score_method.compute(p_clipped, y_survived_trades[test_idx], w_fold))

            w_full = weight_trades if score_method.needs_weight else None
            score = score_method.compute(oof_p, y_survived_trades, w_full)
            candidate = CandidateResult(
                TRANSFORM_NAME, subset, None, score, fold_scores,
                log_k_coef=float(np.mean(fold_log_k_coef)), max_abs_coef=float(np.mean(fold_max_abs_coef)),
            )
            results.append(candidate)

    return results


# ---------------------------------------------------------------------------
# Recommended-subset tie-break (display/diagnostic only -- does not change
# how the global cross-method winner is picked; see select_probability_model.py)
# ---------------------------------------------------------------------------

def select_logistic_recommended(candidates: list[CandidateResult], feature_order: list[str]) -> CandidateResult:
    """Among logistic's own candidates: lowest score first; then the
    "one-standard-error rule" (any candidate within one SE of the raw best
    is considered statistically indistinguishable from it); among those,
    fewest features; final tie-break by a fixed sorted order (each
    candidate's subset compared as a tuple of indices into feature_order)
    for full reproducibility. This selection is purely for this method's
    own reported "recommended subset" -- the actual cross-method winner
    picked by select_probability_model.py still uses a plain minimum over
    the full pooled candidate list, unchanged, per the requirement that the
    existing scorer/selection machinery not change."""
    from .best_subset import standard_error_from_fold_scores  # local import: avoids a module-level cycle risk

    best = min(candidates, key=lambda c: c.score)
    se = standard_error_from_fold_scores(best.fold_scores)
    if not np.isfinite(se):
        within_se = [best]
    else:
        within_se = [c for c in candidates if c.score <= best.score + se]

    min_size = min(len(c.subset) for c in within_se)
    smallest = [c for c in within_se if len(c.subset) == min_size]

    order_index = {name: i for i, name in enumerate(feature_order)}
    smallest.sort(key=lambda c: tuple(order_index[f] for f in c.subset))
    return smallest[0]


# ---------------------------------------------------------------------------
# Shipped-artifact wrapper
# ---------------------------------------------------------------------------

class LogisticSurvivalClassifier:
    """Same (X_new, threshold) -> (probability, y_hat) calling convention as
    ProbabilityClassifier/TransformedResidualClassifier, so downstream code
    doesn't need to branch on whether the winning method was a
    (transform, residual distribution) combination or this one. There is
    no dollar point-estimate for a logistic model, so y_hat is NaN rather
    than silently repurposing the probability as if it meant something
    else."""

    def __init__(self, model: LogisticRegression, scaler: StandardScaler, subset: list[str], columns: list[str]):
        self.model = model
        self.scaler = scaler
        self.subset = subset
        self.columns = columns

    def __call__(self, X_new: pd.DataFrame, threshold):
        credit = X_new[CREDIT_COLUMN].to_numpy()
        log_k = np.log(np.asarray(threshold, dtype=float) / credit)
        X_pred = X_new[self.subset].reset_index(drop=True).copy()
        X_pred[LOG_K_COLUMN] = log_k
        X_pred = _apply_feature_engineering(X_pred)
        X_scaled = self.scaler.transform(X_pred[self.columns])
        p_raw = self.model.predict_proba(X_scaled)[:, 1]
        p_clipped, clip_rate = clip_probabilities(p_raw)
        logger.info(f"LogisticSurvivalClassifier: p = sigmoid(scaler.transform(X[{self.columns}]) @ coef) "
                    f"from the persisted fit on subset={self.subset}; log_k=log(threshold/credit) ranged "
                    f"[{log_k.min():.3f}, {log_k.max():.3f}] over {len(p_raw)} row(s); "
                    f"{clip_rate:.1%} of raw probabilities fell outside [eps, 1-eps] and were clipped.")
        y_hat = np.full(len(X_new), np.nan)
        return p_clipped, y_hat
