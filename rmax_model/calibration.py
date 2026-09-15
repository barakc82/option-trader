"""Isotonic calibration of P(R_max >= k) at each candidate k, fit on
out-of-fold predictions. The decision-relevant object is this derived binary
probability at real stop levels, not the raw multiclass logloss the backend
was trained on.

Isotonic calibration is fit independently per (option_type, k), which can
break monotonicity in k (a later, larger k could calibrate to a higher
probability than an earlier, smaller k for the same row). A pooled-adjacent-
violators pass across k restores it afterwards.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.isotonic import IsotonicRegression, isotonic_regression

from .logging_setup import get_logger

logger = get_logger(__name__)


def fit_isotonic_calibrator(raw_probs: np.ndarray, labels: np.ndarray) -> IsotonicRegression:
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    iso.fit(raw_probs, labels)
    return iso


def fit_calibrators_for_side(oof_raw_by_k: dict[float, np.ndarray], oof_labels_by_k: dict[float, np.ndarray]) -> dict[float, IsotonicRegression]:
    calibrators = {}
    for k, raw in oof_raw_by_k.items():
        labels = oof_labels_by_k[k]
        mask = np.isfinite(raw) & np.isfinite(labels)
        n_dropped = int((~mask).sum())
        if n_dropped:
            logger.warning(f"fit_calibrators_for_side: k={k}, dropping {n_dropped} non-finite OOF rows before isotonic fit")
        calibrators[k] = fit_isotonic_calibrator(raw[mask], labels[mask])
    return calibrators


def apply_calibrators_and_repair_monotonicity(
    calibrators: dict[float, IsotonicRegression], raw_by_k: dict[float, np.ndarray], candidate_k_sorted: list[float],
) -> dict[float, np.ndarray]:
    """Apply each k's isotonic calibrator, then re-assert P(R>=k) is
    non-increasing in k per row via a PAVA pass across the k axis. Returns a
    dict keyed the same way as raw_by_k."""
    calibrated_cols = []
    for k in candidate_k_sorted:
        raw = raw_by_k[k]
        out = np.full(len(raw), np.nan)
        mask = np.isfinite(raw)
        out[mask] = calibrators[k].predict(raw[mask])
        calibrated_cols.append(out)
    mat = np.column_stack(calibrated_cols)

    violations = 0
    fixed = np.empty_like(mat)
    for i in range(mat.shape[0]):
        row = mat[i]
        finite_mask = np.isfinite(row)
        if not finite_mask.any():
            fixed[i] = row
            continue
        row_filled = np.where(finite_mask, row, 0.0)
        repaired = isotonic_regression(row_filled, increasing=False)
        if finite_mask.all() and not np.allclose(repaired, row):
            violations += 1
        fixed[i] = np.where(finite_mask, repaired, np.nan)
    if violations:
        logger.warning(
            f"apply_calibrators_and_repair_monotonicity: {violations}/{mat.shape[0]} rows needed a "
            f"pooled-adjacent-violators repair after per-k isotonic calibration broke monotonicity in k"
        )

    return {k: fixed[:, i] for i, k in enumerate(candidate_k_sorted)}


def brier_score(probs: np.ndarray, labels: np.ndarray) -> float:
    mask = np.isfinite(probs) & np.isfinite(labels)
    return float(np.mean((probs[mask] - labels[mask]) ** 2))


def expected_calibration_error(probs: np.ndarray, labels: np.ndarray, n_bins: int = 10) -> float:
    mask = np.isfinite(probs) & np.isfinite(labels)
    probs, labels = probs[mask], labels[mask]
    if len(probs) == 0:
        return float("nan")
    bin_edges = np.linspace(0.0, 1.0, n_bins + 1)
    bin_idx = np.clip(np.digitize(probs, bin_edges[1:-1]), 0, n_bins - 1)
    total = len(probs)
    ece = 0.0
    for b in range(n_bins):
        mask_b = bin_idx == b
        if not mask_b.any():
            continue
        conf = probs[mask_b].mean()
        acc = labels[mask_b].mean()
        ece += (mask_b.sum() / total) * abs(acc - conf)
    return float(ece)


def reliability_curve(probs: np.ndarray, labels: np.ndarray, n_bins: int = 10) -> pd.DataFrame:
    mask = np.isfinite(probs) & np.isfinite(labels)
    probs, labels = probs[mask], labels[mask]
    bin_edges = np.linspace(0.0, 1.0, n_bins + 1)
    bin_idx = np.clip(np.digitize(probs, bin_edges[1:-1]), 0, n_bins - 1)
    rows = []
    for b in range(n_bins):
        mask_b = bin_idx == b
        rows.append({
            "bin": f"[{bin_edges[b]:.1f}, {bin_edges[b+1]:.1f})",
            "mean_predicted": probs[mask_b].mean() if mask_b.any() else np.nan,
            "mean_actual": labels[mask_b].mean() if mask_b.any() else np.nan,
            "count": int(mask_b.sum()),
        })
    return pd.DataFrame(rows)
