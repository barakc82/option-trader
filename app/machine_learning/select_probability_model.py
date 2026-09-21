"""Selects the best probability model for P(option price stays below a
stop-loss threshold), separately per option right (C/P).

Reuses CSV_PATH, CANDIDATE_FEATURE_COLUMNS, TARGET_COLUMN, GROUP_COLUMN,
CV_FOLDS, CV_RANDOM_STATE and ProbabilityClassifier from regress_max_ask.py
rather than redefining them -- this script is a generalization of that one
(more target transforms, pluggable selection criterion), not a fork of it.

Three target-transform methods (see probability_transforms.py):
  - "raw": fit LinearRegression directly on TARGET_COLUMN (max_ask dollars).
  - "log": fit LinearRegression on log(TARGET_COLUMN).
  - "log_ratio": fit LinearRegression on log(TARGET_COLUMN / estimated_sell_price)
    -- the log is taken of the division result, not of the target alone.

What "best" means is a pluggable SCORE_FN, chosen via
best_subset.SCORE_METHODS (see that module's docstring):
  - "rss": dollar-space (or on-scale) squared error of the point
    prediction. Least-squares against max_ask minimises dollar-space
    squared error by construction, so scoring candidates on it hands the
    "raw" target the metric it was fitted to, and different transforms'
    RSS values live in different units -- hence the dollar-space rescoring
    step below, needed ONLY for this method.
  - "logloss": logloss of P(max_ask < stop), the same question every
    transform answers once its point prediction is converted to a
    probability via a residual distribution (normal or empirical). Directly
    comparable across transforms, no rescoring step needed.
  - "weighted_logloss" (DEFAULT): same as "logloss", but each row is
    weighted by stop_loss - estimated_sell_price (the dollar room between
    the stop and the credit received) instead of counting every row
    equally.

For "logloss"/"weighted_logloss", the search enumerates every (feature
subset x residual distribution) combination for every transform -- turning
a point prediction into a probability requires picking a residual
distribution, so that axis is part of what's searched, not a separate
step. A standard error is computed from the winning combination's per-fold
scores (the "one-standard-error rule" -- e.g. glmnet's lambda.1se), and the
report states how many of the OTHER (transform, feature subset, residual
distribution) combinations are statistically indistinguishable from the
winner (within that one SE) -- a small number means a real winner; a large
one means the apparent winner is noise.

Run directly (from the repo root, so CSV_PATH resolves):
    .venv/Scripts/python.exe -m app.machine_learning.select_probability_model
    .venv/Scripts/python.exe -m app.machine_learning.select_probability_model --score-method rss
"""
import argparse
import logging
import pickle
from pathlib import Path

import numpy as np
from sklearn.linear_model import LinearRegression

from . import logistic_survival, pre_processing, survival_scoring, xgboost_survival
from .best_subset import (
    SCORE_METHODS, DEFAULT_SCORE_METHOD_NAME, ScoreMethod,
    best_subset_for_target, count_within_one_se, search_best_subset_with_distribution,
    standard_error_from_fold_scores,
)
from .probability_transforms import TRANSFORMS, TransformedLinearModel
from .regress_max_ask import (
    CANDIDATE_FEATURE_COLUMNS, CSV_PATH, GROUP_COLUMN, TARGET_COLUMN, ProbabilityClassifier,
)

import pandas as pd

MODEL_PATH = "machine_learning/model/best_probability_model.pkl"


def _print_days_above_stop(subset: pd.DataFrame) -> None:
    """Count of distinct days (GROUP_COLUMN) with at least one row whose
    max_ask exceeded that row's own stop_loss -- i.e. would have been
    stopped out. Printed fresh before each method/transform below since
    they don't all share the exact same filtered subset."""
    stop_col = survival_scoring.STOP_COLUMN
    n_days = subset.loc[subset[TARGET_COLUMN] > subset[stop_col], GROUP_COLUMN].nunique()
    print(f"  {n_days} day(s) with {TARGET_COLUMN} > {stop_col}")


def _select_best_model_rss(df: pd.DataFrame, right: str, score_method: ScoreMethod) -> tuple[ProbabilityClassifier, dict]:
    """The original RSS-based selection, unchanged in behavior: best-subset
    search per transform, dollar-space rescoring to make the three
    transforms' scores comparable, refit the winner, wrap in
    ProbabilityClassifier."""
    subset = df[df["right"] == right].dropna(subset=CANDIDATE_FEATURE_COLUMNS + [TARGET_COLUMN])
    X = subset[CANDIDATE_FEATURE_COLUMNS]
    y_raw = subset[TARGET_COLUMN]
    groups = subset[GROUP_COLUMN]

    if (y_raw <= 0).any():
        n_bad = int((y_raw <= 0).sum())
        print(f"  Dropping {n_bad} rows with {TARGET_COLUMN} <= 0 (undefined for the log and log_ratio transforms)")
        keep = y_raw > 0
        X, y_raw, groups = X[keep], y_raw[keep], groups[keep]

    extra_columns_needed = {t["extra_column"] for t in TRANSFORMS.values() if t["extra_column"] is not None}
    for extra_col in extra_columns_needed:
        bad = X[extra_col] <= 0
        if bad.any():
            n_bad = int(bad.sum())
            print(f"  Dropping {n_bad} rows with {extra_col} <= 0 (required by a log_ratio-style transform)")
            keep = ~bad
            X, y_raw, groups = X[keep], y_raw[keep], groups[keep]

    candidates = {}
    for method_name, transform in TRANSFORMS.items():
        _print_days_above_stop(subset)
        print(f"  Method = {method_name}")
        extra_col = transform["extra_column"]
        extra_arr = X[extra_col].to_numpy() if extra_col is not None else None
        y_transformed = pd.Series(transform["forward"](y_raw.to_numpy(), extra_arr), index=y_raw.index)
        best_subset, within_method_score, oof_pred_transformed = best_subset_for_target(
            X, y_transformed, groups, CANDIDATE_FEATURE_COLUMNS, score_fn=score_method.compute,
        )
        oof_pred_raw = transform["inverse"](oof_pred_transformed, extra_arr)
        dollar_space_score = score_method.compute(y_raw.to_numpy(), oof_pred_raw)
        candidates[method_name] = {
            "best_subset": best_subset,
            "within_method_score": within_method_score,
            "dollar_space_score": dollar_space_score,
        }
        print(f"    Best subset: {best_subset}")
        print(f"    Within-method CV score ({method_name} space): {within_method_score:.4f}")
        print(f"    Dollar-space CV score (for cross-method comparison): {dollar_space_score:.4f}")

    winner_name = min(candidates, key=lambda name: candidates[name]["dollar_space_score"])
    winner = candidates[winner_name]
    print(f"  Winner: {winner_name} (dollar-space CV score {winner['dollar_space_score']:.4f})")

    transform = TRANSFORMS[winner_name]
    extra_col = transform["extra_column"]
    extra_arr = X[extra_col].to_numpy() if extra_col is not None else None
    best_subset = winner["best_subset"]
    y_transformed = pd.Series(transform["forward"](y_raw.to_numpy(), extra_arr), index=y_raw.index)
    linear_model = LinearRegression()
    linear_model.fit(X[best_subset], y_transformed)
    model = TransformedLinearModel(linear_model, best_subset, transform["inverse"], extra_col)

    feature_columns = list(best_subset)
    if extra_col is not None and extra_col not in feature_columns:
        feature_columns.append(extra_col)

    residuals = np.sort(y_raw.to_numpy() - model.predict(X[feature_columns]))

    print(f"  R^2 ({winner_name} space): {linear_model.score(X[best_subset], y_transformed):.4f}")
    for feature, coef in zip(best_subset, linear_model.coef_):
        print(f"    {feature}: {coef:.4f}")
    print(f"    intercept: {linear_model.intercept_:.4f}")

    classifier = ProbabilityClassifier(model, residuals, feature_columns)
    report = {"score_method": "rss", "winner": winner_name, "candidates": candidates}
    return classifier, report


def _select_best_model_distribution(df: pd.DataFrame, right: str,
                                     score_method: ScoreMethod) -> tuple[survival_scoring.TransformedResidualClassifier, dict]:
    """logloss / weighted_logloss selection: for every transform, search
    every (feature subset x residual distribution) combination, pool all
    transforms' results (directly comparable -- both target the same
    P(max_ask < stop) question), pick the global best, compute its standard
    error from per-fold scores, and count how many combinations are within
    one SE of it."""
    required_cols = CANDIDATE_FEATURE_COLUMNS + [TARGET_COLUMN, survival_scoring.STOP_COLUMN]
    subset = df[df["right"] == right].dropna(subset=required_cols)

    if (subset[TARGET_COLUMN] <= 0).any():
        n_bad = int((subset[TARGET_COLUMN] <= 0).sum())
        print(f"  Dropping {n_bad} rows with {TARGET_COLUMN} <= 0 (undefined for the log and log_ratio transforms)")
        subset = subset[subset[TARGET_COLUMN] > 0]

    credit_col = survival_scoring.CREDIT_COLUMN
    if (subset[credit_col] <= 0).any():
        n_bad = int((subset[credit_col] <= 0).sum())
        print(f"  Dropping {n_bad} rows with {credit_col} <= 0 (required by the log_ratio transform and the weight)")
        subset = subset[subset[credit_col] > 0]

    stop_report = survival_scoring.validate_stop_column(subset)
    print(f"  Stop/credit quantiles [0,25,50,75,100]: "
          f"{[round(v, 3) for v in stop_report.stop_over_credit_quantiles.values()]}")

    subset = subset.reset_index(drop=True)
    X = subset[CANDIDATE_FEATURE_COLUMNS]
    ctx = subset[survival_scoring.CTX_COLUMNS]

    all_candidates = []
    per_transform_best = {}
    for transform_name, transform in TRANSFORMS.items():
        _print_days_above_stop(ctx)
        print(f"  Transform = {transform_name}")
        results = search_best_subset_with_distribution(
            X, ctx, transform, transform_name, CANDIDATE_FEATURE_COLUMNS, score_method,
        )
        all_candidates.extend(results)
        best_for_transform = min(results, key=lambda c: c.score)
        per_transform_best[transform_name] = best_for_transform
        print(f"    Best for {transform_name}: subset={best_for_transform.subset}, "
              f"distribution={best_for_transform.distribution}, score={best_for_transform.score:.4f}")

    # Logistic: a seventh method that doesn't go through the (transform x
    # distribution) composition at all -- see logistic_survival.py. Its
    # candidates are just more CandidateResult objects pooled into the same
    # all_candidates list the min()/SE/count-within-1SE code below already
    # operates on, so that code needs no changes to accommodate it.
    logistic_extrapolation_fraction = None
    logistic_spread_comparison = None
    try:
        _print_days_above_stop(ctx)
        print(f"  Transform = logistic")
        logistic_results = logistic_survival.search_logistic_candidates(
            X, ctx, CANDIDATE_FEATURE_COLUMNS, score_method,
        )
        all_candidates.extend(logistic_results)
        logistic_recommended = logistic_survival.select_logistic_recommended(logistic_results, CANDIDATE_FEATURE_COLUMNS)
        per_transform_best["logistic"] = logistic_recommended
        print(f"    Best for logistic (1-SE + fewest-features rule): subset={logistic_recommended.subset}, "
              f"score={logistic_recommended.score:.4f}, log_k_coef={logistic_recommended.log_k_coef:.4f}, "
              f"max|coef|={logistic_recommended.max_abs_coef:.4f}")

        logistic_extrapolation_fraction = logistic_survival.extrapolation_fraction(ctx, logistic_survival.build_k_grid())
        print(f"    Out-of-grid extrapolation fraction (evaluated trades whose log(stop/credit) falls "
              f"outside the training grid): {logistic_extrapolation_fraction:.3f}")

        # Diagnostic: a linear regression on log(max_ask/credit) with normal
        # residuals is algebraically a probit with log_k as a feature, where
        # the log_k coefficient is 1/s. Both estimate the same spread by
        # different routes; if they disagree wildly, one of the two is
        # misspecified.
        log_ratio_normal_candidates = [c for c in all_candidates
                                        if c.transform_name == "log_ratio" and c.distribution == "normal"]
        if log_ratio_normal_candidates:
            best_lrn = min(log_ratio_normal_candidates, key=lambda c: c.score)
            lrn_transform = TRANSFORMS["log_ratio"]
            lrn_extra_col = lrn_transform["extra_column"]
            lrn_extra_arr = ctx[lrn_extra_col].to_numpy() if lrn_extra_col is not None else None
            lrn_y = pd.Series(lrn_transform["forward"](ctx[TARGET_COLUMN].to_numpy(), lrn_extra_arr), index=X.index)
            lrn_model = LinearRegression().fit(X[best_lrn.subset], lrn_y)
            lrn_residuals = lrn_y.to_numpy() - lrn_model.predict(X[best_lrn.subset])
            lrn_std = float(np.std(lrn_residuals, ddof=1)) if len(lrn_residuals) > 1 else float("nan")
            inv_log_k_coef = 1.0 / logistic_recommended.log_k_coef
            print(f"    Spread comparison: 1/coef(log_k) [logistic] = {inv_log_k_coef:.4f} vs "
                  f"residual std [log_ratio_normal, subset={best_lrn.subset}] = {lrn_std:.4f}")
            logistic_spread_comparison = {"inv_log_k_coef": inv_log_k_coef, "log_ratio_normal_residual_std": lrn_std}
    except logistic_survival.LogisticSearchBudgetExceeded as e:
        print(f"  WARNING: skipping logistic method for side {right}: {e}")

    # XGBoost: an eighth method, built on the exact same k-grid replication
    # scaffolding as logistic (see xgboost_survival.py) and searched over the
    # same CANDIDATE_FEATURE_COLUMNS -- "same features as logistic" by
    # construction, not by convention. Its candidates are more
    # CandidateResult objects pooled into the same all_candidates list.
    try:
        _print_days_above_stop(ctx)
        print(f"  Transform = xgboost")
        xgboost_results = xgboost_survival.search_xgboost_candidates(
            X, ctx, CANDIDATE_FEATURE_COLUMNS, score_method,
        )
        all_candidates.extend(xgboost_results)
        xgboost_recommended = xgboost_survival.select_xgboost_recommended(xgboost_results, CANDIDATE_FEATURE_COLUMNS)
        per_transform_best["xgboost"] = xgboost_recommended
        print(f"    Best for xgboost (1-SE + fewest-features rule): subset={xgboost_recommended.subset}, "
              f"score={xgboost_recommended.score:.4f}")
    except xgboost_survival.XgboostSearchBudgetExceeded as e:
        print(f"  WARNING: skipping xgboost method for side {right}: {e}")

    best = min(all_candidates, key=lambda c: c.score)
    se = standard_error_from_fold_scores(best.fold_scores)
    n_within = count_within_one_se(all_candidates, best.score, se)
    print(f"  Winner: transform={best.transform_name}, subset={best.subset}, distribution={best.distribution}, "
          f"score={best.score:.4f}")
    print(f"  Standard error (from winner's per-fold scores): {se:.4f}")
    print(f"  {n_within}/{len(all_candidates)} (transform, feature subset[, residual distribution]) "
          f"combinations fall within 1 SE of the winner")

    # Refit the winner on all available rows for the shipped artifact.
    if best.transform_name == "logistic":
        columns = logistic_survival.columns_for_subset(best.subset)
        k_grid = logistic_survival.build_k_grid()
        X_rep, y_rep, w_rep, _ = logistic_survival.replicate_for_training(X, ctx, best.subset, k_grid)
        model, scaler, coefs = logistic_survival.fit_logistic(
            X_rep, y_rep, w_rep, columns, logistic_survival.LOGISTIC_C, fold_idx="final", subset=best.subset,
        )
        print(f"  Final logistic fit: log_k_coef={coefs[columns.index(logistic_survival.LOG_K_COLUMN)]:.4f}, "
              f"max|coef|={float(np.max(np.abs(coefs))):.4f}")
        for feature, coef in zip(columns, coefs):
            print(f"    {feature}: {coef:.4f}")

        classifier = logistic_survival.LogisticSurvivalClassifier(model, scaler, best.subset, columns)
    elif best.transform_name == "xgboost":
        columns = logistic_survival.columns_for_subset(best.subset)
        k_grid = logistic_survival.build_k_grid()
        X_rep, y_rep, w_rep, _ = logistic_survival.replicate_for_training(X, ctx, best.subset, k_grid)
        model = xgboost_survival.fit_xgboost(X_rep, y_rep, w_rep, columns)
        print(f"  Final xgboost fit: {xgboost_survival.XGBOOST_PARAMS['n_estimators']} trees, "
              f"max_depth={xgboost_survival.XGBOOST_PARAMS['max_depth']}")

        classifier = xgboost_survival.XgboostSurvivalClassifier(model, best.subset, columns)
    else:
        transform = TRANSFORMS[best.transform_name]
        extra_col = transform["extra_column"]
        extra_arr = X[extra_col].to_numpy() if extra_col is not None else None
        y_raw = ctx[TARGET_COLUMN].to_numpy()
        y_transformed = pd.Series(transform["forward"](y_raw, extra_arr), index=X.index)
        linear_model = LinearRegression()
        linear_model.fit(X[best.subset], y_transformed)
        mu_hat_full = linear_model.predict(X[best.subset])
        residuals_full = y_transformed.to_numpy() - mu_hat_full

        residual_distribution = survival_scoring.RESIDUAL_DISTRIBUTIONS[best.distribution]()
        residual_distribution.fit(residuals_full)

        print(f"  R^2 ({best.transform_name} space): {linear_model.score(X[best.subset], y_transformed):.4f}")
        for feature, coef in zip(best.subset, linear_model.coef_):
            print(f"    {feature}: {coef:.4f}")
        print(f"    intercept: {linear_model.intercept_:.4f}")

        classifier = survival_scoring.TransformedResidualClassifier(
            linear_model, best.subset, transform, extra_col, residual_distribution,
        )

    report = {
        "score_method": score_method.name,
        "winner_transform": best.transform_name, "winner_subset": best.subset,
        "winner_distribution": best.distribution, "winner_score": best.score,
        "standard_error": se, "n_within_one_se": n_within, "n_candidates_total": len(all_candidates),
        "per_transform_best": per_transform_best,
        "logistic_extrapolation_fraction": logistic_extrapolation_fraction,
        "logistic_spread_comparison": logistic_spread_comparison,
    }
    return classifier, report


def select_best_model_for_side(df: pd.DataFrame, right: str,
                                score_method_name: str = DEFAULT_SCORE_METHOD_NAME):
    """Dispatches to the RSS or logloss-family selection path based on
    score_method_name (see best_subset.SCORE_METHODS). Returns (classifier,
    report); classifier is always callable as classifier(X_new, threshold)
    -> (probability, y_hat), regardless of which path produced it."""
    score_method = SCORE_METHODS[score_method_name]
    if score_method.needs_distribution:
        return _select_best_model_distribution(df, right, score_method)
    return _select_best_model_rss(df, right, score_method)


def print_selection_report(right: str, report: dict) -> None:
    if report["score_method"] == "rss":
        print(f"  {right}: RSS-lowest = {report['winner']}")
        for method_name, candidate in report["candidates"].items():
            marker = "*" if method_name == report["winner"] else " "
            print(f"    {marker} {method_name}: dollar-space score={candidate['dollar_space_score']:.4f}, "
                  f"subset={candidate['best_subset']}")
        return

    print(f"  {right}: score_method={report['score_method']}")
    print(f"    Winner: transform={report['winner_transform']}, distribution={report['winner_distribution']}, "
          f"subset={report['winner_subset']}, score={report['winner_score']:.4f}")
    print(f"    Standard error: {report['standard_error']:.4f} "
          f"({report['n_within_one_se']}/{report['n_candidates_total']} combinations within 1 SE)")
    print(f"    Best per transform:")
    for transform_name, candidate in report["per_transform_best"].items():
        if transform_name == "logistic":
            print(f"      logistic: subset={candidate.subset}, score={candidate.score:.4f}, "
                  f"log_k_coef={candidate.log_k_coef:.4f} (sign={'+' if candidate.log_k_coef > 0 else '-'}), "
                  f"1/coef(log_k)={1.0 / candidate.log_k_coef:.4f}, max|coef|={candidate.max_abs_coef:.4f}")
        elif transform_name == "xgboost":
            print(f"      xgboost: subset={candidate.subset}, score={candidate.score:.4f}")
        else:
            print(f"      {transform_name}: distribution={candidate.distribution}, subset={candidate.subset}, "
                  f"score={candidate.score:.4f}")

    if report.get("logistic_extrapolation_fraction") is not None:
        print(f"    Logistic out-of-grid extrapolation fraction: {report['logistic_extrapolation_fraction']:.3f}")
    if report.get("logistic_spread_comparison") is not None:
        cmp = report["logistic_spread_comparison"]
        print(f"    Spread comparison: 1/coef(log_k) [logistic] = {cmp['inv_log_k_coef']:.4f} vs "
              f"residual std [log_ratio_normal] = {cmp['log_ratio_normal_residual_std']:.4f}")


def main(argv=None):
    # Nothing in this package ever called logging.basicConfig, so the root
    # logger's default level (WARNING) silently dropped every logger.info
    # call (e.g. LogisticSurvivalClassifier/TransformedResidualClassifier's
    # __call__ diagnostics) before it reached any handler -- not even to
    # console. Configuring it once here, at the actual entry point, makes
    # every module's logger.info calls show up alongside the existing
    # print() output, regardless of which module's logger fires.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser()
    parser.add_argument("--score-method", choices=sorted(SCORE_METHODS), default=DEFAULT_SCORE_METHOD_NAME)
    args = parser.parse_args(argv)

    df = pre_processing.main()
    classifiers = {}
    reports = {}
    for right in ["C", "P"]:
        print(f"Right = {right}")
        classifiers[right], reports[right] = select_best_model_for_side(df, right, args.score_method)

    print("\nModel selection summary:")
    for right, report in reports.items():
        print_selection_report(right, report)

    Path(MODEL_PATH).parent.mkdir(parents=True, exist_ok=True)
    with open(MODEL_PATH, "wb") as f:
        pickle.dump(classifiers, f)
    print(f"\nSaved: {MODEL_PATH}")


if __name__ == "__main__":
    main()
