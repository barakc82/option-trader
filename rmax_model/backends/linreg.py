"""Adapter around the existing per-side linear regression
(app/machine_learning/regress_max_ask.py), so it can be A/B'd against the
binned_softmax backend behind the same SideModel interface.

This is a compatibility shim, not an endorsement or a retune. It reuses the
*exact* fitting procedure already in the repo -- select_best_subset (GroupKFold
best-subset LinearRegression selection) and ProbabilityClassifier (empirical
training-residual ECDF against an arbitrary threshold) -- unmodified. The only
new code here is: (a) renaming our processed-frame columns back to the raw
CSV names that CANDIDATE_FEATURE_COLUMNS expects, (b) converting between our
shared R-space (y = max_ask/credit) and the existing model's native dollar
target (max_ask = R * credit), and (c) deriving predict_survival /
predict_expected_loss_multiple from ProbabilityClassifier's output.

Confirmed in Step 0 (see conversation this package was built from):
ProbabilityClassifier does NOT assume Gaussian residuals -- it is already a
nonparametric lookup against sorted training residuals
(np.searchsorted(sorted_residuals, threshold - y_hat)), so no Gaussian
homoskedasticity assumption is introduced here. It is also not fit against a
stop-specific binary label; its threshold is supplied per-call, exactly the
role k plays here.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.machine_learning.regress_max_ask import (
    CANDIDATE_FEATURE_COLUMNS, CV_FOLDS, ProbabilityClassifier, select_best_subset,
)
from sklearn.linear_model import LinearRegression

from .base import SideModel, register_backend
from ..logging_setup import get_logger

logger = get_logger(__name__)

# Maps our processed-frame column names back to the raw CSV names
# CANDIDATE_FEATURE_COLUMNS was written against (see data.py's rename in
# load_dataset). Columns not renamed by data.py (target_delta, bid_delta,
# ask_delta, last_delta, model_delta, max_delta, gamma, vega, theta,
# distance_to_strike_pct) pass through unchanged.
_RENAME_TO_RAW = {
    "credit": "estimated_sell_price",
    "minutes_to_expiry": "minutes_to_expiration",
    "iv_entry": "atm_iv",
}


def _candidate_frame(X: pd.DataFrame) -> pd.DataFrame:
    cols = {}
    for raw_name in CANDIDATE_FEATURE_COLUMNS:
        processed_name = next((k for k, v in _RENAME_TO_RAW.items() if v == raw_name), raw_name)
        cols[raw_name] = X[processed_name]
    return pd.DataFrame(cols, index=X.index)


@register_backend("linreg")
class LinregBackend:
    name = "linreg"

    def __init__(self):
        self.classifier: ProbabilityClassifier | None = None
        self.rss: float | None = None  # diagnostic only -- never used for backend selection

    @classmethod
    def create(cls, r_edges, feature_names, lgb_params) -> "LinregBackend":
        """Uniform factory signature shared with every registered backend
        (see model.py::build_side_model), even though this backend ignores
        all three arguments -- it works entirely off the raw CSV columns."""
        return cls()

    def fit(self, X: pd.DataFrame, y: pd.Series, groups: pd.Series,
            sample_weight: np.ndarray | None = None) -> None:
        candidate_df = _candidate_frame(X)
        target_dollar = y * X["credit"]

        mask = candidate_df.notna().all(axis=1) & target_dollar.notna()
        n_dropped = int((~mask).sum())
        if n_dropped:
            logger.warning(f"linreg backend: dropping {n_dropped}/{len(X)} training rows with NaN candidate features")

        best_features, best_rss = select_best_subset(
            candidate_df[mask], target_dollar[mask], CANDIDATE_FEATURE_COLUMNS, groups[mask], cv=CV_FOLDS,
        )
        model = LinearRegression()
        model.fit(candidate_df[mask][best_features], target_dollar[mask])
        residuals = np.sort(target_dollar[mask].to_numpy() - model.predict(candidate_df[mask][best_features]))
        self.classifier = ProbabilityClassifier(model, residuals, best_features)
        self.rss = best_rss
        logger.info(f"linreg backend fit: best_features={best_features}, RSS={best_rss:.4f}")

    def _prob_below(self, X: pd.DataFrame, target_dollar: pd.Series) -> tuple[np.ndarray, np.ndarray]:
        assert self.classifier is not None, "fit() must be called before predict"
        candidate_df = _candidate_frame(X)
        mask = candidate_df.notna().all(axis=1).to_numpy()
        prob_below = np.full(len(X), np.nan)
        y_hat = np.full(len(X), np.nan)
        if mask.any():
            p, yh = self.classifier(candidate_df[mask], target_dollar[mask].to_numpy())
            prob_below[mask] = p
            y_hat[mask] = yh
        n_dropped = int((~mask).sum())
        if n_dropped:
            logger.warning(f"linreg backend: {n_dropped}/{len(X)} rows have NaN candidate features, returning NaN")
        return prob_below, y_hat

    def predict_survival(self, X: pd.DataFrame, k: float) -> np.ndarray:
        target_dollar = k * X["credit"]
        prob_below, _ = self._prob_below(X, target_dollar)
        return 1.0 - prob_below

    def predict_expected_loss_multiple(self, X: pd.DataFrame, k: float) -> np.ndarray:
        """E[R_max | R_max >= k], derived from the same empirical residual
        distribution used for predict_survival. Rows for which no training
        residual satisfies the >= k condition (empirical tail support is
        empty) fall back to k itself -- a conservative floor, not an
        extrapolation -- and are counted in a logged warning."""
        assert self.classifier is not None, "fit() must be called before predict"
        candidate_df = _candidate_frame(X)
        credit = X["credit"].to_numpy()
        target_dollar = k * credit
        mask = candidate_df.notna().all(axis=1).to_numpy()

        out = np.full(len(X), np.nan)
        n_fallback = 0
        if mask.any():
            y_hat = self.classifier.model.predict(candidate_df[mask][self.classifier.feature_columns])
            residuals = self.classifier.sorted_residuals
            for local_i, (yh, td, cr) in enumerate(zip(y_hat, target_dollar[mask], credit[mask])):
                cutoff = td - yh
                j = np.searchsorted(residuals, cutoff, side="left")
                if j == len(residuals):
                    n_fallback += 1
                    row_val = td / cr
                else:
                    row_val = (yh + residuals[j:].mean()) / cr
                out[np.flatnonzero(mask)[local_i]] = row_val
        if n_fallback:
            logger.warning(
                f"linreg backend: {n_fallback} rows had no training residual >= k threshold, "
                f"falling back to E[R|R>=k]=k (empirical tail support empty)"
            )
        return out
