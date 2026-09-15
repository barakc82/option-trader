"""LightGBM multiclass backend: predicts the full pmf over R_max bins, reads
survival probabilities off it as suffix sums (binning.survival), and expected
loss as a pmf-weighted average of bin midpoints (empirical mean for the open
top bin).

Do NOT set monotone_constraints. LightGBM applies a monotone constraint
identically across every class column in multiclass mode, which is not the
per-class-boundary semantics an ordered-bin softmax needs -- see
config.py::LgbParams.as_lgb_dict for where this would otherwise go.
"""
from __future__ import annotations

import lightgbm as lgb
import numpy as np
import pandas as pd

from .base import register_backend
from ..binning import assign_bin, survival as survival_readout
from ..config import LgbParams
from ..logging_setup import get_logger

logger = get_logger(__name__)


@register_backend("binned_softmax")
class BinnedSoftmaxBackend:
    name = "binned_softmax"

    def __init__(self, r_edges: tuple[float, ...], feature_names: list[str], lgb_params: LgbParams):
        self.r_edges = tuple(r_edges)
        self.feature_names = list(feature_names)
        self.lgb_params = lgb_params
        self.booster: lgb.Booster | None = None
        self.top_bin_empirical_mean: float | None = None

    @classmethod
    def create(cls, r_edges, feature_names, lgb_params) -> "BinnedSoftmaxBackend":
        return cls(r_edges=r_edges, feature_names=feature_names, lgb_params=lgb_params)

    @property
    def n_bins(self) -> int:
        return len(self.r_edges) - 1

    def fit(self, X: pd.DataFrame, y: pd.Series, groups: pd.Series,
            sample_weight: np.ndarray | None = None) -> None:
        bin_idx = assign_bin(y, self.r_edges)
        X_feat = X[self.feature_names]
        params = self.lgb_params.as_lgb_dict(num_class=self.n_bins)
        num_boost_round = self.lgb_params.num_boost_round

        unique_groups = groups.unique()
        train_mask = np.ones(len(X), dtype=bool)
        valid_mask = None
        if len(unique_groups) >= 2:
            valid_group = sorted(unique_groups)[-1]
            candidate_train_mask = (groups != valid_group).to_numpy()
            if candidate_train_mask.sum() > 0 and (~candidate_train_mask).sum() > 0:
                train_mask = candidate_train_mask
                valid_mask = ~train_mask

        train_weight = sample_weight[train_mask] if sample_weight is not None else None
        train_set = lgb.Dataset(X_feat[train_mask], label=bin_idx[train_mask], weight=train_weight)

        best_iteration = num_boost_round
        if valid_mask is not None:
            valid_weight = sample_weight[valid_mask] if sample_weight is not None else None
            valid_set = lgb.Dataset(X_feat[valid_mask], label=bin_idx[valid_mask], weight=valid_weight, reference=train_set)
            booster = lgb.train(
                params, train_set, num_boost_round=num_boost_round, valid_sets=[valid_set],
                callbacks=[lgb.early_stopping(self.lgb_params.early_stopping_rounds, verbose=False)],
            )
            best_iteration = booster.best_iteration or num_boost_round
        else:
            logger.warning(
                "binned_softmax: fewer than 2 distinct groups in this training call, "
                "cannot carve an internal early-stopping split; training fixed rounds"
            )

        # best_iteration is decided on the held-back date; refit on every
        # available row at that iteration count so the shipped model isn't
        # permanently missing one date's worth of signal.
        full_set = lgb.Dataset(X_feat, label=bin_idx, weight=sample_weight)
        self.booster = lgb.train(params, full_set, num_boost_round=best_iteration)

        top_bin_mask = bin_idx == (self.n_bins - 1)
        if top_bin_mask.any():
            self.top_bin_empirical_mean = float(y.to_numpy()[top_bin_mask].mean())
        else:
            self.top_bin_empirical_mean = self.r_edges[-2] * 1.5
            logger.warning(
                "binned_softmax: no training rows fell in the open top bin; falling back to "
                "1.5x the top bin's lower edge as its expected-value midpoint"
            )

    def predict_pmf(self, X: pd.DataFrame) -> np.ndarray:
        assert self.booster is not None, "fit() must be called before predict"
        return self.booster.predict(X[self.feature_names])

    def predict_survival(self, X: pd.DataFrame, k: float) -> np.ndarray:
        pmf = self.predict_pmf(X)
        return survival_readout(pmf, self.r_edges, k)

    def predict_expected_loss_multiple(self, X: pd.DataFrame, k: float) -> np.ndarray:
        pmf = self.predict_pmf(X)
        edges = list(self.r_edges)
        midpoints = np.array([
            self.top_bin_empirical_mean if j == self.n_bins - 1 else (edges[j] + edges[j + 1]) / 2.0
            for j in range(self.n_bins)
        ])
        j_k = edges.index(k)
        surv = pmf[:, j_k:].sum(axis=1)
        numer = (pmf[:, j_k:] * midpoints[j_k:]).sum(axis=1)
        with np.errstate(invalid="ignore", divide="ignore"):
            out = numer / surv
        out = np.where(surv <= 0, np.nan, out)
        return out
