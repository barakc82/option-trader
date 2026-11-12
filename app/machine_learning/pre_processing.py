from pathlib import Path

import numpy as np
import pandas as pd

CSV_PATH = "machine_learning/data/options_data.csv"

DELTA_COLUMNS = ["bid_delta", "ask_delta", "last_delta", "model_delta"]

STOP_LOSS_SEED = 42

TRADING_DAY_COLUMN = "expiration"
STOP_LOSS_HIT_ON_PREVIOUS_DAY_COLUMN = "stop_loss_hit_on_previous_day"


def stop_loss_hit_on_previous_day(df: pd.DataFrame) -> pd.Series:
    """1 for every row whose previous trading day (the previous expiration date present in
    the data, across both rights) had at least one row with max_ask > stop_loss, else 0.
    Rows on the earliest trading day have no previous day and get 0."""
    hit_by_day = (df["max_ask"] > df["stop_loss"]).groupby(df[TRADING_DAY_COLUMN]).any().sort_index()
    hit_on_previous_day = hit_by_day.shift(1, fill_value=False).astype(int)
    return df[TRADING_DAY_COLUMN].map(hit_on_previous_day)


def main():
    df = pd.read_csv(CSV_PATH)
    changed = False

    if "max_delta" not in df.columns:
        df["max_delta"] = df[DELTA_COLUMNS].max(axis=1)
        changed = True

    # Recomputed every run, since rows appended for a new day change the next day's value.
    previous_day_hits = stop_loss_hit_on_previous_day(df)
    if (STOP_LOSS_HIT_ON_PREVIOUS_DAY_COLUMN not in df.columns
            or not df[STOP_LOSS_HIT_ON_PREVIOUS_DAY_COLUMN].equals(previous_day_hits)):
        df[STOP_LOSS_HIT_ON_PREVIOUS_DAY_COLUMN] = previous_day_hits
        changed = True

    if changed:
        df.to_csv(CSV_PATH, index=False)

    return df


if __name__ == "__main__":
    main()
