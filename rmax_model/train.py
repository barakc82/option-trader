"""CLI entry point: load -> feature-build -> bin -> evaluate (all methods,
both sides, OOF) -> fit shipped per-side models -> calibrate -> fit tail ->
save artifact -> write report.md.

    .venv/Scripts/python.exe -m rmax_model.train [--config path/to/config.yaml]
"""
from __future__ import annotations

import argparse
import random

import numpy as np
import pandas as pd

from . import backtest, binning, calibration, cv, tail
from .config import load_config
from .data import load_dataset
from .evaluate import (
    backend_collapsed_to_base_rate, binary_label, block_bootstrap_ci, brier, day_balanced_sample_weight,
    logloss, oof_predict_backend, oof_predict_base_rate, oof_predict_logistic,
    oof_predict_touch_prob_calibrated, summarize_table,
)
from .features import build_features, feature_list
from .logging_setup import get_logger
from .model import RmaxModel, SideArtifact, build_metadata, build_side_model

SIDES = ["C", "P"]


def set_seed(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)


def build_side_frame(full: pd.DataFrame, side: str, feature_names: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    df_side = full[full["option_type"] == side].reset_index(drop=True)
    feats_side = df_side[feature_names]
    return df_side, feats_side


def day_balance_report(df_side: pd.DataFrame, feats_side: pd.DataFrame, edges, feature_names, config, logger) -> pd.DataFrame:
    """Report OOF logloss/Brier for binned_softmax with vs without
    day_balanced_weights, so the flag's effect is visible regardless of
    whether it's enabled in config.yaml."""
    y = df_side["R_max"]
    groups = df_side["session_date"]
    candidate_k = list(config.candidate_k)
    splits = cv.group_kfold_splits(groups, config.cv.n_splits, config.seed)
    weights = day_balanced_sample_weight(groups)

    rows = []
    for label, sw in [("unweighted", None), ("day_balanced", weights)]:
        preds = oof_predict_backend("binned_softmax", df_side, y, groups, splits, candidate_k, edges,
                                     feature_names, config.lgb_params, sample_weight=sw)
        for k in candidate_k:
            labels = binary_label(y, k)
            probs = preds[k]
            rows.append({"weighting": label, "k": k, "logloss": logloss(probs, labels), "brier": brier(probs, labels)})
    return pd.DataFrame(rows)


def build_diagnostic_table(df_side: pd.DataFrame, feats_side: pd.DataFrame, gk_backtest: dict, config) -> pd.DataFrame:
    """OOF logloss/Brier/RSS table -- demoted from headline to diagnostic per
    the dollar-backtest change request. Reuses the linreg/binned_softmax OOF
    survival predictions already computed by backtest.run_side_backtest's
    GroupKFold pass (gk_backtest['survival_fixed']) instead of re-fitting
    those two backends a second time; only the three cheap baselines
    (base_rate, touch_prob_calibrated, logistic_l2) are computed fresh here.
    """
    y = df_side["R_max"]
    groups = df_side["session_date"]
    dates = groups.to_numpy()
    candidate_k = list(config.candidate_k)
    splits = cv.group_kfold_splits(groups, config.cv.n_splits, config.seed)

    preds_by_method = {
        "base_rate": oof_predict_base_rate(y, splits, candidate_k),
        "touch_prob_calibrated": oof_predict_touch_prob_calibrated(df_side, y, splits, candidate_k, config.seed),
        "logistic_l2": oof_predict_logistic(df_side, feats_side, y, splits, candidate_k, config.seed),
        "linreg": gk_backtest["survival_fixed"]["linreg"],
        "binned_softmax": gk_backtest["survival_fixed"]["binned_softmax"],
    }

    rows = []
    for method, preds_by_k in preds_by_method.items():
        rss = gk_backtest["mean_rss"].get(method) if method == "linreg" else None
        for k in candidate_k:
            labels = binary_label(y, k)
            probs = preds_by_k[k]
            n_finite = int(np.isfinite(probs).sum())
            n_pos = int(labels.sum())
            ll_point, ll_lo, ll_hi = block_bootstrap_ci(probs, labels, dates, logloss, config.bootstrap.n_resamples, config.bootstrap.ci, config.seed)
            br_point, br_lo, br_hi = block_bootstrap_ci(probs, labels, dates, brier, config.bootstrap.n_resamples, config.bootstrap.ci, config.seed)
            rows.append({
                "method": method, "k": k, "n": n_finite, "n_pos": n_pos, "rss": rss,
                "logloss": ll_point, "logloss_lo": ll_lo, "logloss_hi": ll_hi,
                "brier": br_point, "brier_lo": br_lo, "brier_hi": br_hi,
            })
    return pd.DataFrame(rows), preds_by_method


def dollar_recommendation(side: str, gk_methods: dict, configured: str) -> str:
    """'If the binned model does not beat both take-all and the incumbent on
    a given side, say so plainly and leave that side on linreg.' -- the
    dollar-total comparison is the one that governs this, not logloss."""
    if "binned_softmax" not in gk_methods or "linreg" not in gk_methods or "take_all" not in gk_methods:
        return "insufficient data to compare"
    bs, lr, ta = gk_methods["binned_softmax"], gk_methods["linreg"], gk_methods["take_all"]
    beats_take_all = bs.mean_per_available > ta.mean_per_available
    beats_linreg = bs.mean_per_available > lr.mean_per_available
    verdict = "beats" if (beats_take_all and beats_linreg) else "does NOT beat"
    return (
        f"binned_softmax {verdict} both take_all and linreg on realized $/available-trade "
        f"(binned_softmax={bs.mean_per_available:.4f} [{bs.mean_per_available_lo:.4f}, {bs.mean_per_available_hi:.4f}], "
        f"take_all={ta.mean_per_available:.4f}, linreg={lr.mean_per_available:.4f}). "
        f"config.yaml currently ships '{configured}' for this side."
    )


def render_backtest_section(side: str, side_backtest: dict, config, configured: str) -> list[str]:
    lines = []
    lines.append(f"### Realized-dollar backtest (headline)\n")
    lines.append(
        "p_hit only drives the take/skip decision; realized dollars come from what actually happened "
        "(R_max vs. the drawn k), never from the prediction -- see rmax_model/backtest.py module docstring. "
        f"fees={config.backtest.fees}, ev_threshold={config.backtest.ev_threshold} (fixed, not swept), "
        f"k drawn Uniform[{config.backtest.stop_draw_low}, {config.backtest.stop_draw_high}).\n"
    )
    for split_name in ["groupkfold", "walkforward"]:
        if split_name not in side_backtest:
            lines.append(f"*(no {split_name} splits available for this side)*\n")
            continue
        r = side_backtest[split_name]
        drawn_k = r["drawn_k"]
        q = np.quantile(drawn_k, [0, 0.25, 0.5, 0.75, 1.0])
        lines.append(f"**{split_name} OOF** -- {len(drawn_k)} covered rows. Drawn-k quantiles [min,25,50,75,max]: "
                      f"[{q[0]:.2f}, {q[1]:.2f}, {q[2]:.2f}, {q[3]:.2f}, {q[4]:.2f}]")
        gap = r["gap_drawn"].get("binned_softmax")
        if gap is not None and np.isfinite(gap).any():
            lines.append(f"binned_softmax log-R interpolation: mean |interpolated - nearest-edge| survival gap = {np.nanmean(gap):.4f}")
        df = backtest.methods_to_dataframe(r["methods"])
        lines.append(f"```\n{df.to_string(index=False)}\n```\n")

    if "groupkfold" in side_backtest:
        lines.append(f"**Recommendation (realized-dollar, GroupKFold OOF):** {dollar_recommendation(side, side_backtest['groupkfold']['methods'], configured)}\n")
    return lines


def render_report(config, data_report, edges, bin_counts, side_results: dict, side_backtest: dict, day_balance: dict,
                   reliability: dict, tail_fits: dict, recommendations: dict, backend_for_side: dict) -> str:
    lines = []
    lines.append("# rmax_model training report\n")
    lines.append(
        f"**PROVISIONAL RESULTS** -- trained on {data_report.distinct_session_dates} distinct trading "
        f"sessions ({data_report.final_row_count} rows after filtering). GroupKFold-by-date, walk-forward "
        f"CV, per-k isotonic calibration, and the GPD tail fit all assume a much larger sample of trading "
        f"days than this. Every number below should be treated as a pipeline shakedown, not a trading "
        f"decision, until more sessions accumulate.\n"
    )

    lines.append("## Data\n")
    lines.append(f"- Raw rows: {data_report.raw_row_count}")
    lines.append(f"- Rows with is_executed==1: {data_report.executed_row_count} (dropped {data_report.dropped_not_executed} unfilled)")
    lines.append(f"- Dropped for null required columns: {data_report.dropped_null_by_column}")
    lines.append(f"- Dropped for unreconstructable underlying: {data_report.dropped_underlying_reconstruction}")
    lines.append(f"- Dropped by validation gate: {data_report.dropped_validation_gate}")
    lines.append(f"- Final rows: {data_report.final_row_count} across {data_report.distinct_session_dates} session dates")
    lines.append(f"- Rows per side: {data_report.rows_per_side}")
    lines.append(f"- Rows per session date:\n\n```\n{data_report.rows_per_session_date}\n```\n")

    lines.append("## Bin edges and per-bin counts\n")
    lines.append(f"`R_EDGES` (after inserting any missing candidate_k): `{edges}`\n")
    lines.append(f"```\n{bin_counts}\n```\n")
    thin = bin_counts[bin_counts.sum(axis=1) < binning.MIN_BIN_COUNT_WARNING]
    if len(thin):
        lines.append(f"**Bins below {binning.MIN_BIN_COUNT_WARNING} observations (not merged):**\n```\n{thin}\n```\n")

    for side in SIDES:
        lines.append(f"## Side = {side}\n")
        configured = backend_for_side[side]

        lines.extend(render_backtest_section(side, side_backtest[side], config, configured))

        results = side_results[side]
        lines.append("### Diagnostic: OOF logloss/Brier/RSS (demoted from headline -- see realized-dollar backtest above)\n")
        lines.append(f"```\n{summarize_table(results)}\n```\n")
        lines.append("### Per-k detail (with 95% block-bootstrap CI over session dates)\n")
        lines.append(
            "`n_pos` is how many of this side's (unrestricted, not OOF-masked) rows actually have "
            "R_max >= k -- a near-zero logloss/Brier at a k where n_pos is 0 or tiny reflects the stop "
            "almost never being hit in this small sample, not a model that has learned something. `rss` "
            "is only populated for linreg (mean best-subset RSS across GroupKFold folds); it is not "
            "comparable across backend types and plays no role in model selection.\n"
        )
        detail_cols = ["method", "k", "n", "n_pos", "rss", "logloss", "logloss_lo", "logloss_hi", "brier", "brier_lo", "brier_hi"]
        lines.append(f"```\n{results[detail_cols].to_string(index=False)}\n```\n")

        rec = recommendations[side]
        if backend_collapsed_to_base_rate(results):
            rec += (
                " **Caveat: binned_softmax's OOF logloss/Brier are identical to the unconditional "
                "base_rate baseline at every k on this side.** The model has not learned any row-level "
                "signal -- it is just predicting the training marginal rate for every row (a conservative, "
                "well-regularized default given how little data this side has, but not evidence it "
                "understands anything about individual trades). Any apparent 'win' over linreg here is "
                "because linreg is worse than an uninformative baseline OOF, not because binned_softmax "
                "is skillful."
            )
        lines.append(f"**Diagnostic recommendation (OOF logloss/Brier, not the governing decision):** {rec}\n")

        lines.append("### day_balanced_weights effect on binned_softmax (reported both ways)\n")
        lines.append(f"```\n{day_balance[side].to_string(index=False)}\n```\n")

        lines.append(f"### Calibration reliability for the configured backend ({backend_for_side[side]})\n")
        rel = reliability[side]
        lines.append(f"- Pre-calibration Brier (mean across k): {rel['brier_pre']:.4f}, ECE: {rel['ece_pre']:.4f}")
        lines.append(f"- Post-calibration Brier (mean across k): {rel['brier_post']:.4f}, ECE: {rel['ece_post']:.4f}\n")

        tf = tail_fits[side]
        lines.append("### GPD tail fit\n")
        if tf.fitted:
            lines.append(f"- shape={tf.shape:.4f} (SE={tf.shape_se:.4f}), scale={tf.scale:.4f}, n_exceedances={tf.n_exceedances}")
            if tf.shape >= 1:
                lines.append("- **shape >= 1: infinite-mean tail implied beyond the threshold.**")
        else:
            lines.append(f"- Not fitted: {tf.fallback_reason}. Falling back to the binned model's empirical top-bin mean.")
        lines.append("")

    lines.append("## Known gaps vs. the original spec (see conversation this package was built from)\n")
    lines.append("- No `entry_ts`; `session_date` (expiry date) is used as the CV/bootstrap grouping key.")
    lines.append("- `credit := estimated_sell_price` (no separate fill-credit column exists).")
    lines.append("- `spread_ratio` is approximated by `spread_delta_width = ask_delta - bid_delta` (IB's own model-implied delta at bid/ask, not dollar NBBO -- no dollar bid/ask exists in this data).")
    lines.append("- `underlying_entry` is reconstructed exactly from `strike` and `distance_to_strike_pct`, not logged directly.")
    lines.append("- The spec's \"market context\" feature group (vix1d, realized_move_pct, minutes_since_open, open_range_pct) is not implemented -- no entry timestamp or intraday underlying path is logged anywhere in this repo yet.")
    lines.append("- No nested hyperparameter search was run: with 5 distinct session dates, a search validated on held-out folds of held-out folds has no statistical footing. lgb_params in config.yaml are the fixed, conservative hand-set values from the original plan.")
    lines.append("- tail.py's GPD splice is fit and reported per side, but is not yet wired into predict_expected_loss_multiple's live path -- both backends still use the plain bin-midpoint/empirical-mean estimator for that.")
    lines.append("- The random_subset baseline in the realized-dollar backtest is sized to match binned_softmax's taken-count specifically (the candidate being evaluated for promotion), not linreg's -- stated explicitly since the spec's 'same size' was ambiguous about which model's count to match.")
    lines.append("- fees=0.0 in config.yaml is a placeholder (no fee data exists in this repo) -- override with a real per-contract figure once available.")

    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=str, default=None)
    args = parser.parse_args(argv)

    config = load_config(args.config)
    set_seed(config.seed)
    logger = get_logger(__name__, config.paths)

    logger.info("=== rmax_model training run starting ===")
    df, data_report = load_dataset(config)

    edges = binning.ensure_candidate_k_in_edges(config.r_edges, config.candidate_k)
    feature_names = feature_list(config.physics_prior.reference_multiples)
    feats = build_features(df, config.physics_prior.reference_multiples)
    full = pd.concat([df, feats], axis=1)
    full["bin"] = binning.assign_bin(full["R_max"], edges)
    bin_counts = binning.report_bin_counts(full, "bin", edges)

    side_results, side_backtest, day_balance, reliability, tail_fits, recommendations = {}, {}, {}, {}, {}, {}
    side_artifacts = {}
    train_dates_all = []

    backend_for_side = {"C": config.backends.call, "P": config.backends.put}

    for side in SIDES:
        logger.info(f"--- Evaluating side {side} ---")
        df_side, feats_side = build_side_frame(full, side, feature_names)
        train_dates_all.extend(df_side["session_date"].tolist())

        logger.info(f"--- Realized-dollar backtest for side {side} (GroupKFold + walk-forward OOF) ---")
        bt_results = backtest.run_side_backtest(df_side, df_side, feats_side, config, edges)
        side_backtest[side] = bt_results

        if "groupkfold" not in bt_results:
            raise RuntimeError(f"side {side}: no GroupKFold splits available -- cannot build the diagnostic table")
        results, preds_by_method = build_diagnostic_table(df_side, feats_side, bt_results["groupkfold"], config)
        side_results[side] = results
        summary = summarize_table(results)

        configured = backend_for_side[side]
        if "binned_softmax" in summary.index and "linreg" in summary.index:
            bs_ll, lr_ll = summary.loc["binned_softmax", "logloss"], summary.loc["linreg", "logloss"]
            bs_br, lr_br = summary.loc["binned_softmax", "brier"], summary.loc["linreg", "brier"]
            binned_wins = (bs_ll < lr_ll) and (bs_br < lr_br)
            recommendations[side] = (
                f"binned_softmax {'beats' if binned_wins else 'does NOT beat'} linreg on OOF logloss+Brier "
                f"(binned_softmax logloss={bs_ll:.4f}/brier={bs_br:.4f} vs linreg logloss={lr_ll:.4f}/brier={lr_br:.4f}). "
                f"config.yaml currently ships '{configured}' for this side."
            )
        else:
            recommendations[side] = "insufficient data to compare binned_softmax vs linreg"

        day_balance[side] = day_balance_report(df_side, feats_side, edges, feature_names, config, logger)

        candidate_k = list(config.candidate_k)
        oof_raw_by_k = preds_by_method[configured]
        y_side = df_side["R_max"]
        oof_labels_by_k = {k: binary_label(y_side, k) for k in candidate_k}
        calibrators = calibration.fit_calibrators_for_side(oof_raw_by_k, oof_labels_by_k)
        calibrated = calibration.apply_calibrators_and_repair_monotonicity(calibrators, oof_raw_by_k, candidate_k)

        brier_pre = float(np.mean([brier(oof_raw_by_k[k], oof_labels_by_k[k]) for k in candidate_k]))
        brier_post = float(np.mean([brier(calibrated[k], oof_labels_by_k[k]) for k in candidate_k]))
        ece_pre = float(np.mean([calibration.expected_calibration_error(oof_raw_by_k[k], oof_labels_by_k[k]) for k in candidate_k]))
        ece_post = float(np.mean([calibration.expected_calibration_error(calibrated[k], oof_labels_by_k[k]) for k in candidate_k]))
        reliability[side] = {"brier_pre": brier_pre, "brier_post": brier_post, "ece_pre": ece_pre, "ece_post": ece_post}

        log_r = np.log(y_side.to_numpy())
        tail_fits[side] = tail.fit_gpd_tail(log_r, config.tail.threshold_quantile, config.tail.min_exceedances, config.seed) if config.tail.enabled else tail.GpdTailFit(False, float("nan"), None, None, None, 0, "tail.enabled=False in config")

        logger.info(f"--- Fitting shipped model for side {side} (backend={configured}) ---")
        sample_weight = day_balanced_sample_weight(df_side["session_date"]) if config.day_balanced_weights else None
        final_model = build_side_model(configured, edges, feature_names, config.lgb_params)
        final_model.fit(df_side, y_side, df_side["session_date"], sample_weight=sample_weight)
        side_artifacts[side] = SideArtifact(backend_tag=configured, model=final_model, calibrators=calibrators)

    metadata = build_metadata(
        train_date_range=(min(train_dates_all), max(train_dates_all)),
        extra={"n_distinct_sessions": data_report.distinct_session_dates, "config_backends": backend_for_side},
    )
    rmax_model = RmaxModel(r_edges=edges, candidate_k=tuple(config.candidate_k), feature_names=feature_names,
                            sides=side_artifacts, metadata=metadata)
    rmax_model.save(config.paths.resolved_artifact_path)

    report_text = render_report(config, data_report, edges, bin_counts, side_results, side_backtest, day_balance,
                                 reliability, tail_fits, recommendations, backend_for_side)
    config.paths.resolved_report_path.parent.mkdir(parents=True, exist_ok=True)
    config.paths.resolved_report_path.write_text(report_text, encoding="utf-8")

    logger.info(f"Wrote artifact to {config.paths.resolved_artifact_path}")
    logger.info(f"Wrote report to {config.paths.resolved_report_path}")
    print(f"Artifact: {config.paths.resolved_artifact_path}")
    print(f"Report:   {config.paths.resolved_report_path}")


if __name__ == "__main__":
    main()
