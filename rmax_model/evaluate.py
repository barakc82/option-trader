"""Evaluation harness. Runs every baseline plus both registered backends, for
both sides, on the same GroupKFold-by-date OOF splits, regardless of what
config.yaml has selected to ship -- the config picks what ships, this module
reports what every option would have scored.

Metrics are always reported per side, never pooled: it is a legitimate
outcome that one backend wins on calls and loses on puts.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

from .backends.base import get_backend_class
from .calibration import fit_isotonic_calibrator
from .cv import group_kfold_splits
from .logging_setup import get_logger
from .pricer import solve_barrier_underlying
from scipy.stats import norm

logger = get_logger(__name__)

LOGISTIC_BASELINE_FEATURES = [
    "spread_delta_width", "moneyness_sd", "gamma_n", "vega_n", "theta_n", "delta_n", "log_credit", "sigma",
]

METHOD_ORDER = ["base_rate", "touch_prob_calibrated", "logistic_l2", "linreg", "binned_softmax"]


def binary_label(R_max: pd.Series, k: float) -> np.ndarray:
    return (R_max.to_numpy() >= k).astype(float)


def logloss(probs: np.ndarray, labels: np.ndarray, eps: float = 1e-6) -> float:
    mask = np.isfinite(probs) & np.isfinite(labels)
    p = np.clip(probs[mask], eps, 1 - eps)
    y = labels[mask]
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def brier(probs: np.ndarray, labels: np.ndarray) -> float:
    mask = np.isfinite(probs) & np.isfinite(labels)
    return float(np.mean((probs[mask] - labels[mask]) ** 2))


def _raw_touch_prob_at_k(df: pd.DataFrame, k: float) -> np.ndarray:
    """Pure analytic P(touch) at an arbitrary k (not necessarily one of the
    fixed reference_multiples baked into FEATURES) -- baseline #2 needs this
    at every candidate_k, so it's computed fresh here rather than reused
    from the physics-prior feature columns."""
    out = np.full(len(df), np.nan)
    for i, row in enumerate(df.itertuples()):
        F = row.underlying_entry
        sigma = row.iv_entry
        T = row.minutes_to_expiry / (60 * 24 * 365)
        target_price = k * row.credit
        F_star = solve_barrier_underlying(row.option_type, row.strike, sigma, T, target_price, F)
        if not np.isfinite(F_star):
            continue
        barrier_sd = abs(F_star - F) / (F * sigma * np.sqrt(T))
        out[i] = 2.0 * norm.cdf(-barrier_sd)
    return out


def oof_predict_base_rate(y: pd.Series, splits: list, candidate_k: list[float]) -> dict[float, np.ndarray]:
    n = len(y)
    out = {k: np.full(n, np.nan) for k in candidate_k}
    for train_idx, test_idx in splits:
        for k in candidate_k:
            rate = binary_label(y.iloc[train_idx], k).mean()
            out[k][test_idx] = rate
    return out


def oof_predict_touch_prob_calibrated(df: pd.DataFrame, y: pd.Series, splits: list,
                                       candidate_k: list[float], seed: int) -> dict[float, np.ndarray]:
    n = len(df)
    out = {k: np.full(n, np.nan) for k in candidate_k}
    for k in candidate_k:
        raw = _raw_touch_prob_at_k(df, k)
        labels = binary_label(y, k)
        for train_idx, test_idx in splits:
            train_mask = np.isfinite(raw[train_idx])
            if train_mask.sum() < 5:
                continue
            calibrator = fit_isotonic_calibrator(raw[train_idx][train_mask], labels[train_idx][train_mask])
            test_mask = np.isfinite(raw[test_idx])
            idx = np.array(test_idx)[test_mask]
            out[k][idx] = calibrator.predict(raw[idx])
    return out


def oof_predict_logistic(df: pd.DataFrame, feats: pd.DataFrame, y: pd.Series, splits: list,
                          candidate_k: list[float], seed: int) -> dict[float, np.ndarray]:
    n = len(df)
    X = feats[LOGISTIC_BASELINE_FEATURES].to_numpy()
    out = {k: np.full(n, np.nan) for k in candidate_k}
    for k in candidate_k:
        labels = binary_label(y, k)
        for train_idx, test_idx in splits:
            y_train = labels[train_idx]
            if len(np.unique(y_train)) < 2:
                continue  # a fold with a single class can't fit a binary logistic model
            scaler = StandardScaler()
            X_train = scaler.fit_transform(X[train_idx])
            X_test = scaler.transform(X[test_idx])
            clf = LogisticRegression(penalty="l2", C=1.0, max_iter=1000, random_state=seed)
            clf.fit(X_train, y_train)
            out[k][test_idx] = clf.predict_proba(X_test)[:, 1]
    return out


def oof_predict_backend(backend_name: str, X_full: pd.DataFrame, y: pd.Series, groups: pd.Series, splits: list,
                         candidate_k: list[float], edges: tuple[float, ...], feature_names: list[str], lgb_params,
                         sample_weight: np.ndarray | None = None) -> dict[float, np.ndarray]:
    n = len(X_full)
    out = {k: np.full(n, np.nan) for k in candidate_k}
    cls = get_backend_class(backend_name)
    for fold_i, (train_idx, test_idx) in enumerate(splits):
        model = cls.create(edges, feature_names, lgb_params)
        fold_weight = sample_weight[train_idx] if sample_weight is not None else None
        model.fit(X_full.iloc[train_idx], y.iloc[train_idx], groups.iloc[train_idx], sample_weight=fold_weight)
        for k in candidate_k:
            out[k][test_idx] = model.predict_survival(X_full.iloc[test_idx], k)
        logger.info(f"oof_predict_backend[{backend_name}]: fold {fold_i+1}/{len(splits)} done")
    return out


def day_balanced_sample_weight(groups: pd.Series) -> np.ndarray:
    counts = groups.map(groups.value_counts())
    return (1.0 / counts).to_numpy()


def block_bootstrap_ci(probs: np.ndarray, labels: np.ndarray, dates: np.ndarray, metric_fn,
                        n_resamples: int, ci: float, seed: int) -> tuple[float, float, float]:
    """Block bootstrap over session dates (not rows): resample dates with
    replacement, pool all rows belonging to the resampled dates (with
    duplication), recompute the metric. Returns (point_estimate, lo, hi).

    A date drawn more than once in a resample must contribute its rows that
    many times -- using a plain membership mask (e.g. np.isin) instead of an
    explicit index concatenation silently collapses repeats back to a single
    copy, which understates sum-like statistics and is not a real block
    bootstrap at all. Build the resampled index array explicitly."""
    point = metric_fn(probs, labels)
    unique_dates = np.unique(dates)
    if len(unique_dates) < 2:
        return point, float("nan"), float("nan")
    date_to_indices = {d: np.flatnonzero(dates == d) for d in unique_dates}
    rng = np.random.default_rng(seed)
    boot_vals = []
    for _ in range(n_resamples):
        sampled_dates = rng.choice(unique_dates, size=len(unique_dates), replace=True)
        idx = np.concatenate([date_to_indices[d] for d in sampled_dates])
        boot_vals.append(metric_fn(probs[idx], labels[idx]))
    if not boot_vals:
        return point, float("nan"), float("nan")
    alpha = (1 - ci) / 2
    lo, hi = np.quantile(boot_vals, [alpha, 1 - alpha])
    return point, float(lo), float(hi)


def compute_oof_predictions(df_side: pd.DataFrame, X_full_side: pd.DataFrame, feats_side: pd.DataFrame,
                             config, edges: tuple[float, ...]) -> tuple[dict, list]:
    """OOF survival predictions for all 5 methods on one side, on GroupKFold-
    by-date splits. Returns (preds_by_method, splits) so callers (evaluate_side
    for metrics, train.py for fitting the shipped calibrators) share the same
    splits and don't need to recompute anything."""
    y = df_side["R_max"]
    groups = df_side["session_date"]
    candidate_k = list(config.candidate_k)
    splits = group_kfold_splits(groups, config.cv.n_splits, config.seed)

    preds_by_method = {
        "base_rate": oof_predict_base_rate(y, splits, candidate_k),
        "touch_prob_calibrated": oof_predict_touch_prob_calibrated(df_side, y, splits, candidate_k, config.seed),
        "logistic_l2": oof_predict_logistic(df_side, feats_side, y, splits, candidate_k, config.seed),
        "linreg": oof_predict_backend("linreg", X_full_side, y, groups, splits, candidate_k, edges,
                                       [], config.lgb_params),
        "binned_softmax": oof_predict_backend("binned_softmax", X_full_side, y, groups, splits, candidate_k, edges,
                                               feats_side.columns.tolist(), config.lgb_params),
    }
    return preds_by_method, splits


def evaluate_side(df_side: pd.DataFrame, X_full_side: pd.DataFrame, feats_side: pd.DataFrame,
                   config, edges: tuple[float, ...]) -> tuple[pd.DataFrame, dict]:
    """Runs all 5 methods for one side on GroupKFold-by-date OOF splits.
    Returns (results, preds_by_method); results is long-format: method, k,
    logloss, brier, n, logloss_lo/hi, brier_lo/hi."""
    y = df_side["R_max"]
    dates = df_side["session_date"].to_numpy()
    candidate_k = list(config.candidate_k)
    preds_by_method, _splits = compute_oof_predictions(df_side, X_full_side, feats_side, config, edges)

    rows = []
    for method, preds_by_k in preds_by_method.items():
        for k in candidate_k:
            labels = binary_label(y, k)
            probs = preds_by_k[k]
            n_finite = int(np.isfinite(probs).sum())
            n_pos = int(labels.sum())
            ll_point, ll_lo, ll_hi = block_bootstrap_ci(probs, labels, dates, logloss, config.bootstrap.n_resamples, config.bootstrap.ci, config.seed)
            br_point, br_lo, br_hi = block_bootstrap_ci(probs, labels, dates, brier, config.bootstrap.n_resamples, config.bootstrap.ci, config.seed)
            rows.append({
                "method": method, "k": k, "n": n_finite, "n_pos": n_pos,
                "logloss": ll_point, "logloss_lo": ll_lo, "logloss_hi": ll_hi,
                "brier": br_point, "brier_lo": br_lo, "brier_hi": br_hi,
            })
    return pd.DataFrame(rows), preds_by_method


def backend_collapsed_to_base_rate(results: pd.DataFrame, tol: float = 1e-4) -> bool:
    """True if binned_softmax's OOF logloss/Brier match the unconditional
    base_rate baseline at every k to within tol -- i.e. the model learned no
    row-level signal at all and is just predicting the training marginal
    rate for every row, which reads as a deceptively good-looking 'win' over
    a weaker baseline like linreg without reflecting real skill."""
    bs = results[results.method == "binned_softmax"].set_index("k")
    br = results[results.method == "base_rate"].set_index("k")
    common_k = bs.index.intersection(br.index)
    if len(common_k) == 0:
        return False
    return bool(np.allclose(bs.loc[common_k, "logloss"], br.loc[common_k, "logloss"], atol=tol) and
                np.allclose(bs.loc[common_k, "brier"], br.loc[common_k, "brier"], atol=tol))


def summarize_table(results: pd.DataFrame) -> pd.DataFrame:
    """Mean-across-k logloss and Brier per method -- the headline
    (side x backend) comparison table. Full per-k detail lives in `results`."""
    summary = results.groupby("method")[["logloss", "brier"]].mean().reindex(METHOD_ORDER)
    return summary
