import pickle
from pathlib import Path

import pandas as pd
from sklearn.linear_model import LinearRegression

CSV_PATH = Path(__file__).parent / "data" / "options_data.csv"
MODEL_PATH = Path(__file__).parent / "model" / "max_ask_models.pkl"

TARGET_COLUMN = "max_ask"
FEATURE_COLUMNS = [
    "estimated_sell_price", "target_delta",
    "bid_delta", "ask_delta", "last_delta", "model_delta", "max_delta",
    "gamma", "vega", "theta",
    "minutes_to_expiration", "implied_volatility", "distance_to_strike_pct",
]


def run_regression(df, right):
    subset = df[df["right"] == right].dropna(subset=FEATURE_COLUMNS + [TARGET_COLUMN])
    X = subset[FEATURE_COLUMNS]
    y = subset[TARGET_COLUMN]

    model = LinearRegression()
    model.fit(X, y)

    print(f"Right = {right} ({len(subset)} records)")
    print(f"  R^2: {model.score(X, y):.4f}")
    for feature, coef in zip(FEATURE_COLUMNS, model.coef_):
        print(f"  {feature}: {coef:.4f}")
    print(f"  intercept: {model.intercept_:.4f}")

    return model


def main():
    df = pd.read_csv(CSV_PATH)
    models = {
        "C": run_regression(df, "C"),
        "P": run_regression(df, "P"),
    }

    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(MODEL_PATH, "wb") as f:
        pickle.dump(models, f)


if __name__ == "__main__":
    main()
