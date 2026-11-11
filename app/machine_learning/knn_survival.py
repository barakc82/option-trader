"""k-nearest-neighbours as a ninth survival method, alongside logistic_survival.py
and xgboost_survival.py.

For every past trade keep R = max_ask / estimated_sell_price (how many times the
credit the ask reached). For a candidate trade with stop multiple
k = stop_loss / estimated_sell_price, P(survive) is the share of its n nearest past
trades whose R stayed below k -- R < k is exactly max_ask < stop, the same survival
label every other method is scored on (survival_scoring.compute_survival_label).

Similarity is Euclidean distance on the candidate's feature subset, each feature
standardised with the training rows' mean/std first (otherwise a feature measured in
minutes would drown out one measured in deltas). The subset is searched exactly like
the other methods (same CANDIDATE_FEATURE_COLUMNS, same feature-count cap, same
stale-size stop), crossed with N_NEIGHBORS_GRID the way xgboost crosses each subset
with its reg_lambda grid. Size 0 (no features) makes every past trade an equal
neighbour, so it is the unconditional base rate over all training trades and is
evaluated once rather than per n_neighbors.

The share is Laplace-smoothed, (n_below + 0.5) / (n + 1), the same smoothing
survival_scoring.EmpiricalResidualDistribution uses: with only 15-50 neighbours an
unsmoothed share is often exactly 0 or 1, which clip_probabilities would turn into a
near-infinite logloss for a single surprise.
"""
from __future__ import annotations

import itertools
import logging
import time

import numpy as np
import pandas as pd
from sklearn.model_selection import GroupKFold
from sklearn.neighbors import NearestNeighbors

from .best_subset import MAX_STALE_SIZES, SEARCH_TIME_BUDGET_SEC, CandidateResult, ScoreMethod
from .logistic_survival import select_logistic_recommended
from .regress_max_ask import CV_FOLDS, CV_RANDOM_STATE, GROUP_COLUMN, TARGET_COLUMN
from .survival_scoring import (
    CREDIT_COLUMN, STOP_COLUMN, clip_probabilities, compute_survival_label, max_feature_count, trade_weight,
)

logger = logging.getLogger(__name__)

TRANSFORM_NAME = "knn"

N_NEIGHBORS_GRID = [15, 25, 50]

KNN_SEARCH_TIME_BUDGET_SEC = SEARCH_TIME_BUDGET_SEC


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

def max_ask_ratio(ctx: pd.DataFrame) -> np.ndarray:
    """R = max_ask / estimated_sell_price for every trade."""
    return ctx[TARGET_COLUMN].to_numpy() / ctx[CREDIT_COLUMN].to_numpy()


def stop_multiple(stop, credit) -> np.ndarray:
    """k = stop_loss / estimated_sell_price."""
    return np.asarray(stop, dtype=float) / np.asarray(credit, dtype=float)


def _share_below(neighbor_ratios: np.ndarray, k: np.ndarray) -> np.ndarray:
    """Laplace-smoothed share of each row's neighbour R values that stayed below its k.
    neighbor_ratios is (n_rows, n_neighbors), k is (n_rows,)."""
    n = neighbor_ratios.shape[1]
    n_below = (neighbor_ratios < k[:, None]).sum(axis=1)
    return (n_below + 0.5) / (n + 1)


class KnnModel:
    """Standardiser + neighbour index over the training rows' features, plus their R values."""

    def __init__(self, X_train: np.ndarray, ratios_train: np.ndarray):
        self.ratios_train = np.asarray(ratios_train, dtype=float)
        self.n_features = X_train.shape[1]
        if self.n_features:
            self.mean = X_train.mean(axis=0)
            std = X_train.std(axis=0)
            # A constant feature carries no similarity information; leave it unscaled
            # (all zeros after centring) rather than dividing by zero.
            self.std = np.where(std > 0, std, 1.0)
            self.index = NearestNeighbors().fit((X_train - self.mean) / self.std)

    def survival_prob(self, X: np.ndarray, k: np.ndarray, n_neighbors: int) -> np.ndarray:
        k = np.asarray(k, dtype=float)
        if not self.n_features:
            # No features: every training trade is an equal neighbour.
            return _share_below(np.broadcast_to(self.ratios_train, (len(k), len(self.ratios_train))), k)
        n_neighbors = min(n_neighbors, len(self.ratios_train))
        _, neighbor_idx = self.index.kneighbors((X - self.mean) / self.std, n_neighbors=n_neighbors)
        return _share_below(self.ratios_train[neighbor_idx], k)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def search_knn_candidates(X: pd.DataFrame, ctx: pd.DataFrame, feature_names: list[str],
                          score_method: ScoreMethod, cv: int = CV_FOLDS,
                          n_neighbors_grid: list[int] = N_NEIGHBORS_GRID,
                          time_budget_sec: float = KNN_SEARCH_TIME_BUDGET_SEC) -> list[CandidateResult]:
    """Best-subset search over feature_names crossed with n_neighbors_grid, on the same
    GroupKFold split and score_method as the other methods. Returns every evaluated
    CandidateResult (transform_name="knn", distribution=None, n_neighbors set; None
    for the size-0 base-rate candidate)."""
    groups = ctx[GROUP_COLUMN]
    n_splits = max(2, min(cv, groups.nunique()))
    gkf = GroupKFold(n_splits=n_splits, shuffle=True, random_state=CV_RANDOM_STATE)
    fold_splits = list(gkf.split(np.zeros(len(X)), groups=groups))

    ratios = max_ask_ratio(ctx)
    k_all = stop_multiple(ctx[STOP_COLUMN], ctx[CREDIT_COLUMN])
    y_survived = compute_survival_label(ctx)
    weight = trade_weight(ctx)

    deadline = time.time() + time_budget_sec
    results: list[CandidateResult] = []
    best_score = np.inf
    best_subset = []
    best_n_neighbors = None
    stale_sizes = 0
    stop_search = False
    for size in range(0, max_feature_count(ctx, len(feature_names)) + 1):
        print(f"    Working on size {size}")
        improved_this_size = False
        for subset in itertools.combinations(feature_names, size):
            subset = list(subset)
            X_subset = X[subset].to_numpy(dtype=float)
            fold_models = [KnnModel(X_subset[train_idx], ratios[train_idx]) for train_idx, _ in fold_splits]

            for n_neighbors in (n_neighbors_grid if subset else [None]):
                oof_p = np.full(len(X), np.nan)
                fold_scores = []
                for model, (_, test_idx) in zip(fold_models, fold_splits):
                    p_raw = model.survival_prob(X_subset[test_idx], k_all[test_idx], n_neighbors)
                    p_clipped, _ = clip_probabilities(p_raw)
                    oof_p[test_idx] = p_clipped
                    w_fold = weight[test_idx] if score_method.needs_weight else None
                    fold_scores.append(score_method.compute(p_clipped, y_survived[test_idx], w_fold))

                w_full = weight if score_method.needs_weight else None
                score = score_method.compute(oof_p, y_survived, w_full)
                results.append(CandidateResult(TRANSFORM_NAME, subset, None, score, fold_scores,
                                               n_neighbors=n_neighbors))
                if score < best_score:
                    best_score = score
                    best_subset = subset
                    best_n_neighbors = n_neighbors
                    improved_this_size = True

                if time.time() >= deadline:
                    stop_search = True
                    break
            if stop_search:
                break
        if stop_search:
            print(f"    Time budget of {time_budget_sec / 60:.1f} min reached after {len(results)} "
                  f"candidate(s); stopping the search early and using the results collected so far.")
            break

        if improved_this_size:
            stale_sizes = 0
            print(f"    Size {size} improves best score to {best_score:.4f}, features: {best_subset}, "
                  f"n_neighbors: {best_n_neighbors}")
        else:
            stale_sizes += 1
            if stale_sizes >= MAX_STALE_SIZES:
                print(f"    No improvement for {MAX_STALE_SIZES} consecutive sizes, stopping at size {size}")
                break

    return results


# Generic over CandidateResult.score/.subset/.fold_scores, as for xgboost.
select_knn_recommended = select_logistic_recommended


# ---------------------------------------------------------------------------
# Shipped-artifact wrapper
# ---------------------------------------------------------------------------

class KnnSurvivalClassifier:
    """Same (X_new, threshold) -> (probability, y_hat) calling convention as the other
    shipped classifiers. y_hat is the median neighbour R times the credit -- a dollar
    max_ask estimate from the same neighbours the probability comes from (NaN for the
    zero-feature base-rate model, which has no neighbours of its own)."""

    def __init__(self, model: KnnModel, subset: list[str], n_neighbors: int | None):
        self.model = model
        self.subset = subset
        self.n_neighbors = n_neighbors

    def __call__(self, X_new: pd.DataFrame, threshold):
        credit = X_new[CREDIT_COLUMN].to_numpy(dtype=float)
        k = stop_multiple(threshold, credit)
        X_pred = X_new[self.subset].to_numpy(dtype=float)
        if np.isnan(X_pred).any():
            raise ValueError(f"KnnSurvivalClassifier: NaN input in subset {self.subset}; refusing to predict")

        p_raw = self.model.survival_prob(X_pred, k, self.n_neighbors)
        p_clipped, clip_rate = clip_probabilities(p_raw)

        y_hat = np.full(len(X_new), np.nan)
        if self.subset:
            n_neighbors = min(self.n_neighbors, len(self.model.ratios_train))
            _, idx = self.model.index.kneighbors((X_pred - self.model.mean) / self.model.std, n_neighbors=n_neighbors)
            y_hat = np.median(self.model.ratios_train[idx], axis=1) * credit

        logger.info(f"KnnSurvivalClassifier: p = share of {self.n_neighbors} nearest neighbours on {self.subset} "
                    f"with max_ask/credit < k, k=threshold/credit ranged [{k.min():.3f}, {k.max():.3f}] over "
                    f"{len(p_raw)} row(s); {clip_rate:.1%} of probabilities were clipped.")
        return p_clipped, y_hat

    def get_feature_subset(self) -> list[str]:
        """The X_new columns __call__ reads: subset plus CREDIT_COLUMN (used for k)."""
        features = list(self.subset)
        if CREDIT_COLUMN not in features:
            features.append(CREDIT_COLUMN)
        return features
