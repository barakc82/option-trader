import itertools
import pickle
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import LinearRegression
from sklearn.model_selection import GroupKFold, cross_val_predict

CSV_PATH = "machine_learning/data/options_data.csv"
MODEL_PATH =  "machine_learning/model/probability_classifier.pkl"

TARGET_COLUMN = "max_ask"
GROUP_COLUMN = "expiration"
CANDIDATE_FEATURE_COLUMNS = [
    "estimated_sell_price", "target_delta", "bid_delta", "ask_delta", "last_delta", "model_delta",
    "max_delta", "gamma", "vega", "theta", "minutes_to_expiration", "atm_iv", "distance_to_strike_pct",
]
CV_FOLDS = 5
CV_RANDOM_STATE = 42


class ProbabilityClassifier:
    """Wraps a fitted regression model with its sorted training residuals, so calling the
    instance with new feature rows and a threshold gives the empirical probability that the
    true max_ask falls below that threshold."""

    def __init__(self, model, sorted_residuals, feature_columns):
        self.model = model
        self.sorted_residuals = sorted_residuals
        self.feature_columns = feature_columns

    def __call__(self, X_new, threshold):
        """Empirical probability that the true max_ask is below threshold for each row of
        X_new, estimated from how often training residuals would have kept the actual value
        (y_hat + residual) under threshold. A binary search locates each cutoff in O(log n)
        instead of comparing against every residual. X_new may carry extra columns; only
        feature_columns (the subset the model was actually fit on) are used."""
        y_hat = self.model.predict(X_new[self.feature_columns])
        idx = np.searchsorted(self.sorted_residuals, threshold - y_hat, side='left')
        return idx / len(self.sorted_residuals), y_hat


def select_best_subset(X, y, feature_names, groups, cv=CV_FOLDS):
    """Best-subset feature selection: try every non-empty subset of feature_names, score each
    by cross-validated RSS (residual sum of squares computed from out-of-fold predictions),
    and return the subset with the lowest RSS along with that RSS. Folds are grouped by
    `groups` (the option's expiration) so records sharing an expiry never straddle the
    train/test split, which would otherwise leak information between them."""
    n_splits = max(2, min(cv, groups.nunique()))
    print(f"Number of trade days: {groups.nunique()}, number of samples: {X.shape[0]}")
    group_kfold = GroupKFold(n_splits=n_splits, shuffle=True, random_state=CV_RANDOM_STATE)

    best_subset, best_rss = [], np.inf
    for size in range(1, len(feature_names) + 1):
        print(f"Working on size {size}")
        for subset in itertools.combinations(feature_names, size):
            subset = list(subset)
            y_pred = cross_val_predict(LinearRegression(), X[subset], y, cv=group_kfold, groups=groups)
            rss = np.sum((y.to_numpy() - y_pred) ** 2)
            if rss < best_rss:
                best_subset, best_rss = subset, rss
                print(
                    f"Number of features: {len(subset)}, best RSS: {rss}, features: {subset}")

    return best_subset, best_rss


def run_regression(df, right):
    subset = df[df["right"] == right].dropna(subset=CANDIDATE_FEATURE_COLUMNS + [TARGET_COLUMN])
    X = subset[CANDIDATE_FEATURE_COLUMNS]
    y = subset[TARGET_COLUMN]
    groups = subset[GROUP_COLUMN]

    best_features, best_rss = select_best_subset(X, y, CANDIDATE_FEATURE_COLUMNS, groups)

    model = LinearRegression()
    model.fit(X[best_features], y)
    residuals = np.sort(y.to_numpy() - model.predict(X[best_features]))

    print(f"Right = {right} ({len(subset)} records)")
    print(f"  Best subset (CV RSS={best_rss:.4f}): {best_features}")
    print(f"  R^2: {model.score(X[best_features], y):.4f}")
    for feature, coef in zip(best_features, model.coef_):
        print(f"  {feature}: {coef:.4f}")
    print(f"  intercept: {model.intercept_:.4f}")

    quantiles = np.quantile(residuals, np.linspace(0.1, 1.0, 10))
    print("  Residual deciles:")
    for pct, value in zip(range(10, 101, 10), quantiles):
        print(f"    {pct:3d}%: {value:.4f}")

    return ProbabilityClassifier(model, residuals, best_features)


def main():
    df = pd.read_csv(CSV_PATH)
    classifiers = {
        "C": run_regression(df, "C"),
        "P": run_regression(df, "P"),
    }

    Path(MODEL_PATH).parent.mkdir(parents=True, exist_ok=True)
    with open(MODEL_PATH, "wb") as f:
        pickle.dump(classifiers, f)


if __name__ == "__main__":
    main()
