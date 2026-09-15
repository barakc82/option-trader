"""Realized-dollar backtest -- the headline evaluation metric, replacing RSS.

Why this is shaped the way it is: a dollar metric computed as
`probability * payoff` is maximized by a model that always predicts 0 or 1,
because the score is linear in the predicted probability and a linear
function is maximized at its edges. Fitting to (or selecting on) such a
metric trains/picks an overconfident model.

This module avoids that trap by construction: the predicted probability
(`p_hit`) only ever drives a binary take/skip decision through `ev_hat`.
Once a trade is taken, its `realized` payoff comes entirely from what
actually happened in the data (R_max vs k) -- never from p_hit. See
`realized_and_decision` below; `realized` must never read `p_hit`, and
`test_backtest.py::test_realized_never_reads_prediction` (via random
prediction shuffling) is the regression guard for that invariant.

Do NOT:
  - add this metric to any hyperparameter search, early-stopping criterion,
    or automated model-selection path (training objectives and model
    selection stay on multiclass logloss / OOF logloss+Brier, unchanged).
  - sweep `ev_threshold` to maximize the total and report the maximum. It is
    a fixed config value. A sensitivity curve across thresholds is fine to
    show, but must be labeled a sensitivity analysis, not a result.
  - "simplify" this back into probability-weighted expected payoffs.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .backends.base import get_backend_class
from .cv import group_kfold_splits, walk_forward_splits
from .logging_setup import get_logger

logger = get_logger(__name__)


def draw_stop_multiples(rng: np.random.Generator, n: int, low: float, high: float) -> np.ndarray:
    """One k per record, drawn uniformly on [low, high). Never draw more
    than one k per record -- rows share one underlying path and inflating
    the row count would fabricate error-bar precision."""
    return rng.uniform(low, high, n)


def slippage_model(X: pd.DataFrame, k) -> np.ndarray:
    """Hook: a stop on a far-OTM 0-DTE option does not fill at the stop
    price. Returns a multiplicative factor of 1.0 (no slippage) for every
    row until real fill-quality data justifies something else. Must accept
    either a scalar k or a per-row array of k, and always returns an array
    aligned to X's rows."""
    return np.ones(len(X))


def realized_and_decision(p_hit: np.ndarray, R: np.ndarray, k: np.ndarray, C: np.ndarray,
                           fees: float, ev_threshold: float, slip: np.ndarray) -> dict:
    """The core EV / decision / realized-payoff arithmetic.

    `realized` is a pure function of (R, k, C, fees, slip) -- it must NEVER
    read p_hit. p_hit enters only through `ev_hat`, which drives the binary
    `taken` decision. If a future change makes `realized` depend on p_hit,
    this function -- and the metric -- is broken.

    loss = (k - 1) * C * slip + fees, i.e. the buyback minus the credit, not
    k * C. Getting this wrong overstates every losing trade by one credit.
    """
    gain = C - fees
    loss = (k - 1) * C * slip + fees
    ev_hat = (1 - p_hit) * gain - p_hit * loss
    taken = ev_hat > ev_threshold
    stopped_out = R >= k
    realized_if_taken = np.where(stopped_out, -loss, gain)
    realized = np.where(taken, realized_if_taken, 0.0)
    return {"taken": taken, "realized": realized, "ev_hat": ev_hat, "gain": gain, "loss": loss, "stopped_out": stopped_out}


def interpolate_survival_binned(pmf: np.ndarray, edges: tuple[float, ...], k: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Linear interpolation of the survival curve in log(R), within the bin
    containing each row's own (generally off-edge) k. Returns
    (interpolated_survival, abs_gap_vs_nearest_edge) -- the gap quantifies
    how much the interpolation is doing versus the naive alternative of
    snapping k to its nearest bin edge.

    Only meaningful for the binned_softmax backend; linreg's survival is
    already continuous in k and needs no interpolation (see
    survival_at_row_k below).
    """
    edges_arr = np.array(edges)
    n_bins = len(edges) - 1
    j = np.searchsorted(edges_arr, k, side="right") - 1
    j = np.clip(j, 0, n_bins - 1)

    # suffix_sums[:, b] = P(R >= edges[b]) = sum(pmf[:, b:], axis=1)
    suffix_sums = np.cumsum(pmf[:, ::-1], axis=1)[:, ::-1]

    row_idx = np.arange(len(k))
    lo_edge = edges_arr[j]
    hi_edge = edges_arr[np.minimum(j + 1, n_bins)]
    s_lo = suffix_sums[row_idx, j]
    j_hi = j + 1
    s_hi = np.where(j_hi < n_bins, suffix_sums[row_idx, np.minimum(j_hi, n_bins - 1)], 0.0)

    with np.errstate(divide="ignore", invalid="ignore"):
        log_k, log_lo, log_hi = np.log(k), np.log(lo_edge), np.log(hi_edge)
        frac = np.where(np.isfinite(log_hi), (log_k - log_lo) / (log_hi - log_lo), 0.0)
    frac = np.clip(frac, 0.0, 1.0)
    s_interp = s_lo + frac * (s_hi - s_lo)

    nearest_is_lo = np.abs(k - lo_edge) <= np.abs(hi_edge - k)
    s_nearest = np.where(nearest_is_lo, s_lo, s_hi)
    gap = np.abs(s_interp - s_nearest)
    return s_interp, gap


def survival_at_row_k(backend_tag: str, model, X: pd.DataFrame, k: np.ndarray, edges: tuple[float, ...]) -> tuple[np.ndarray, np.ndarray | None]:
    """Dispatch to the right per-row-k survival computation. Returns
    (survival, interpolation_gap_or_None). linreg is natively continuous in
    k (its ProbabilityClassifier does a residual-ECDF lookup at an arbitrary
    dollar threshold) so it needs no interpolation and returns gap=None."""
    if backend_tag == "linreg":
        return model.predict_survival(X, k), None
    if backend_tag == "binned_softmax":
        pmf = model.predict_pmf(X)
        return interpolate_survival_binned(pmf, edges, k)
    raise KeyError(f"unknown backend_tag {backend_tag!r}")


def oof_backtest_fit(backend_name: str, X_full: pd.DataFrame, y: pd.Series, groups: pd.Series, splits: list,
                      drawn_k: np.ndarray, candidate_k: list[float], edges: tuple[float, ...],
                      feature_names: list[str], lgb_params) -> tuple[np.ndarray, np.ndarray, dict[float, np.ndarray], float | None]:
    """Fits the backend once per fold and derives, from that single fit,
    both the OOF survival at each row's own drawn k AND the OOF survival at
    every fixed candidate_k -- so this one pass serves both the dollar
    backtest and the diagnostic logloss/Brier/RSS table, instead of fitting
    the (expensive) linreg backend twice for the same fold.

    Returns (survival_drawn, gap_drawn, survival_fixed_by_k, mean_rss).
    gap_drawn is all-nan for linreg; mean_rss is None for binned_softmax
    (RSS is a linreg-only diagnostic, not a comparable quantity across
    backend types -- retained per the change request, never used for
    selection).
    """
    n = len(X_full)
    survival_drawn = np.full(n, np.nan)
    gap_drawn = np.full(n, np.nan)
    survival_fixed = {k: np.full(n, np.nan) for k in candidate_k}
    fold_rss = []
    cls = get_backend_class(backend_name)
    for fold_i, (train_idx, test_idx) in enumerate(splits):
        model = cls.create(edges, feature_names, lgb_params)
        model.fit(X_full.iloc[train_idx], y.iloc[train_idx], groups.iloc[train_idx])
        X_test = X_full.iloc[test_idx]

        s_drawn, gap = survival_at_row_k(backend_name, model, X_test, drawn_k[test_idx], edges)
        survival_drawn[test_idx] = s_drawn
        if gap is not None:
            gap_drawn[test_idx] = gap

        for k in candidate_k:
            survival_fixed[k][test_idx] = model.predict_survival(X_test, k)

        if getattr(model, "rss", None) is not None:
            fold_rss.append(model.rss)

        logger.info(f"oof_backtest_fit[{backend_name}]: fold {fold_i+1}/{len(splits)} done")
    mean_rss = float(np.mean(fold_rss)) if fold_rss else None
    return survival_drawn, gap_drawn, survival_fixed, mean_rss


@dataclass
class MethodResult:
    method: str
    n_available: int
    n_taken: int
    total_realized: float
    mean_per_available: float
    mean_per_available_lo: float = float("nan")
    mean_per_available_hi: float = float("nan")
    total_lo: float = float("nan")
    total_hi: float = float("nan")
    realized_hit_rate: float = float("nan")
    mean_predicted_p_hit: float = float("nan")
    by_date: pd.Series = None
    worst_day_removed_total: float = float("nan")


def block_bootstrap_total_and_mean(realized: np.ndarray, dates: np.ndarray, n_resamples: int, ci: float, seed: int) -> dict:
    """A date drawn more than once in a resample must contribute its rows
    that many times -- a membership mask (np.isin) instead of an explicit
    index concatenation silently collapses repeats to one copy, which
    understates the total (a sum-like statistic is exactly what that bug
    breaks) and isn't a real block bootstrap. Build the index array
    explicitly, with duplication."""
    unique_dates = np.unique(dates)
    if len(unique_dates) < 2:
        return {"total_lo": float("nan"), "total_hi": float("nan"), "mean_lo": float("nan"), "mean_hi": float("nan")}
    date_to_indices = {d: np.flatnonzero(dates == d) for d in unique_dates}
    rng = np.random.default_rng(seed)
    totals, means = [], []
    for _ in range(n_resamples):
        sampled_dates = rng.choice(unique_dates, size=len(unique_dates), replace=True)
        idx = np.concatenate([date_to_indices[d] for d in sampled_dates])
        totals.append(realized[idx].sum())
        means.append(realized[idx].mean())
    alpha = (1 - ci) / 2
    total_lo, total_hi = np.quantile(totals, [alpha, 1 - alpha])
    mean_lo, mean_hi = np.quantile(means, [alpha, 1 - alpha])
    return {"total_lo": float(total_lo), "total_hi": float(total_hi), "mean_lo": float(mean_lo), "mean_hi": float(mean_hi)}


def summarize_method(method: str, realized: np.ndarray, taken: np.ndarray, p_hit: np.ndarray | None,
                      stopped_out: np.ndarray, R: np.ndarray, dates: np.ndarray,
                      n_resamples: int, ci: float, seed: int) -> MethodResult:
    n_available = len(realized)
    n_taken = int(taken.sum())
    total = float(np.sum(realized))
    mean_per_avail = float(np.mean(realized)) if n_available else float("nan")

    boot = block_bootstrap_total_and_mean(realized, dates, n_resamples, ci, seed)

    realized_hit_rate = float(stopped_out[taken].mean()) if n_taken else float("nan")
    mean_pred = float(np.nanmean(p_hit[taken])) if (p_hit is not None and n_taken) else float("nan")

    by_date = pd.Series(realized, index=dates).groupby(level=0).sum()
    worst_day_removed = total - by_date.max() if len(by_date) else total

    return MethodResult(
        method=method, n_available=n_available, n_taken=n_taken, total_realized=total,
        mean_per_available=mean_per_avail,
        mean_per_available_lo=boot["mean_lo"], mean_per_available_hi=boot["mean_hi"],
        total_lo=boot["total_lo"], total_hi=boot["total_hi"],
        realized_hit_rate=realized_hit_rate, mean_predicted_p_hit=mean_pred,
        by_date=by_date, worst_day_removed_total=worst_day_removed,
    )


def run_side_backtest(df_side: pd.DataFrame, X_full_side: pd.DataFrame, feats_side: pd.DataFrame,
                       config, edges: tuple[float, ...]) -> dict:
    """Runs the full realized-dollar backtest for one side, on both
    GroupKFold-by-date and walk-forward OOF splits. Returns
    {split_kind: {"drawn_k": ..., "methods": {method_name: MethodResult},
    "gap_drawn": {backend: array}, "survival_fixed": {backend: {k: array}}}}.
    """
    y = df_side["R_max"]
    R = df_side["R_max"].to_numpy()
    C = df_side["credit"].to_numpy()
    groups = df_side["session_date"]
    candidate_k = list(config.candidate_k)
    feature_names = feats_side.columns.tolist()

    seed = config.seed
    rng = np.random.default_rng(seed)
    n = len(df_side)
    drawn_k = draw_stop_multiples(rng, n, config.backtest.stop_draw_low, config.backtest.stop_draw_high)
    slip = slippage_model(df_side, drawn_k)
    fees = config.backtest.fees
    ev_threshold = config.backtest.ev_threshold

    split_kinds = {
        "groupkfold": group_kfold_splits(groups, config.cv.n_splits, seed),
        "walkforward": walk_forward_splits(groups, config.cv.walk_forward_min_train_days),
    }

    results = {}
    for split_name, splits in split_kinds.items():
        if not splits:
            logger.warning(f"run_side_backtest: no {split_name} splits available, skipping")
            continue

        covered = np.zeros(n, dtype=bool)
        for _, test_idx in splits:
            covered[test_idx] = True
        cov_idx = np.flatnonzero(covered)

        backend_out = {}
        survival_fixed_by_backend = {}
        gap_drawn_by_backend = {}
        mean_rss_by_backend = {}
        for backend_name in ["linreg", "binned_softmax"]:
            fnames = feature_names if backend_name == "binned_softmax" else []
            survival_drawn, gap_drawn, survival_fixed, mean_rss = oof_backtest_fit(
                backend_name, X_full_side, y, groups, splits, drawn_k, candidate_k, edges, fnames, config.lgb_params,
            )
            survival_fixed_by_backend[backend_name] = survival_fixed
            gap_drawn_by_backend[backend_name] = gap_drawn
            mean_rss_by_backend[backend_name] = mean_rss
            decision = realized_and_decision(survival_drawn[covered], R[covered], drawn_k[covered], C[covered], fees, ev_threshold, slip[covered])
            backend_out[backend_name] = summarize_method(
                backend_name, decision["realized"], decision["taken"], survival_drawn[covered],
                decision["stopped_out"], R[covered], groups.to_numpy()[covered],
                config.bootstrap.n_resamples, config.bootstrap.ci, seed,
            )

        # take_all / take_nothing need no model at all.
        gain_all = C[covered] - fees
        loss_all = (drawn_k[covered] - 1) * C[covered] * slip[covered] + fees
        stopped_out_all = R[covered] >= drawn_k[covered]
        realized_take_all = np.where(stopped_out_all, -loss_all, gain_all)
        take_all_result = summarize_method(
            "take_all", realized_take_all, np.ones(len(cov_idx), dtype=bool), None, stopped_out_all,
            R[covered], groups.to_numpy()[covered], config.bootstrap.n_resamples, config.bootstrap.ci, seed,
        )
        take_nothing_result = summarize_method(
            "take_nothing", np.zeros(len(cov_idx)), np.zeros(len(cov_idx), dtype=bool), None, stopped_out_all,
            R[covered], groups.to_numpy()[covered], config.bootstrap.n_resamples, config.bootstrap.ci, seed,
        )

        # Random subset baseline, matched to binned_softmax's taken count
        # (the candidate being evaluated for promotion), averaged over
        # n_random_draws seeded draws.
        n_take = backend_out["binned_softmax"].n_taken
        rng_random = np.random.default_rng(seed + 1)
        draws = []
        for _ in range(config.backtest.n_random_draws):
            chosen = rng_random.choice(len(cov_idx), size=min(n_take, len(cov_idx)), replace=False)
            realized_random = np.zeros(len(cov_idx))
            realized_random[chosen] = realized_take_all[chosen]
            draws.append(realized_random)
        realized_random_mean = np.mean(draws, axis=0) if draws else np.zeros(len(cov_idx))
        taken_random = realized_random_mean != 0  # approx indicator for hit-rate reporting only
        random_result = summarize_method(
            "random_subset", realized_random_mean, taken_random, None, stopped_out_all,
            R[covered], groups.to_numpy()[covered], config.bootstrap.n_resamples, config.bootstrap.ci, seed,
        )

        results[split_name] = {
            "drawn_k": drawn_k[covered],
            "methods": {
                "take_all": take_all_result, "random_subset": random_result, "take_nothing": take_nothing_result,
                "linreg": backend_out["linreg"], "binned_softmax": backend_out["binned_softmax"],
            },
            "survival_fixed": survival_fixed_by_backend,
            "gap_drawn": gap_drawn_by_backend,
            "mean_rss": mean_rss_by_backend,
            "covered": covered,
        }

    return results


METHOD_DISPLAY_ORDER = ["take_all", "random_subset", "take_nothing", "linreg", "binned_softmax"]


def methods_to_dataframe(methods: dict[str, "MethodResult"]) -> pd.DataFrame:
    rows = []
    for name in METHOD_DISPLAY_ORDER:
        if name not in methods:
            continue
        m = methods[name]
        rows.append({
            "method": m.method,
            "n_avail": m.n_available,
            "n_taken": m.n_taken,
            "frac_taken": m.n_taken / m.n_available if m.n_available else float("nan"),
            "total": m.total_realized,
            "total_lo": m.total_lo,
            "total_hi": m.total_hi,
            "mean_per_avail": m.mean_per_available,
            "mean_lo": m.mean_per_available_lo,
            "mean_hi": m.mean_per_available_hi,
            "realized_hit_rate": m.realized_hit_rate,
            "mean_pred_p_hit": m.mean_predicted_p_hit,
            "worst_day_removed_total": m.worst_day_removed_total,
        })
    return pd.DataFrame(rows)
