import pickle
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import LinearRegression

CSV_PATH = "machine_learning/data/options_data.csv"
MODEL_PATH =  "machine_learning/model/probability_classifier.pkl"

TARGET_COLUMN = "max_ask"
FEATURE_COLUMNS = [
    "estimated_sell_price",
    #"target_delta", "bid_delta", "ask_delta", "last_delta", "model_delta",
    "max_delta",
    "gamma", "vega", "theta",
    "minutes_to_expiration", "atm_iv", "distance_to_strike_pct",
]


class ProbabilityClassifier:
    """Wraps a fitted regression model with its sorted training residuals, so calling the
    instance with new feature rows and a threshold gives the empirical probability that the
    true max_ask falls below that threshold."""

    def __init__(self, model, sorted_residuals):
        self.model = model
        self.sorted_residuals = sorted_residuals

    def __call__(self, X_new, threshold):
        """Empirical probability that the true max_ask is below threshold for each row of
        X_new, estimated from how often training residuals would have kept the actual value
        (y_hat + residual) under threshold. A binary search locates each cutoff in O(log n)
        instead of comparing against every residual."""
        y_hat = self.model.predict(X_new)
        idx = np.searchsorted(self.sorted_residuals, threshold - y_hat, side='left')
        return idx / len(self.sorted_residuals)


def run_regression(df, right):
    subset = df[df["right"] == right].dropna(subset=FEATURE_COLUMNS + [TARGET_COLUMN])
    X = subset[FEATURE_COLUMNS]
    y = subset[TARGET_COLUMN]

    model = LinearRegression()
    model.fit(X, y)
    residuals = np.sort(y.to_numpy() - model.predict(X))

    print(f"Right = {right} ({len(subset)} records)")
    print(f"  R^2: {model.score(X, y):.4f}")
    for feature, coef in zip(FEATURE_COLUMNS, model.coef_):
        print(f"  {feature}: {coef:.4f}")
    print(f"  intercept: {model.intercept_:.4f}")

    return ProbabilityClassifier(model, residuals)


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
