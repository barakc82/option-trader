from pathlib import Path

import numpy as np
import pandas as pd

CSV_PATH = "machine_learning/data/options_data.csv"

DELTA_COLUMNS = ["bid_delta", "ask_delta", "last_delta", "model_delta"]

# Range for the simulated per-row stop_loss backfill below. Seeded so the
# draw is reproducible and, combined with the "only if column missing"
# check, stable across repeated preprocessing runs -- existing rows keep
# whatever stop_loss they were first assigned.
STOP_LOSS_LOW = 3.0
STOP_LOSS_HIGH = 5.0
STOP_LOSS_SEED = 42


def main():
    df = pd.read_csv(CSV_PATH)
    changed = False

    if "max_delta" not in df.columns:
        df["max_delta"] = df[DELTA_COLUMNS].max(axis=1)
        changed = True

    if "stop_loss" not in df.columns:
        rng = np.random.default_rng(STOP_LOSS_SEED)
        df["stop_loss"] = rng.uniform(STOP_LOSS_LOW, STOP_LOSS_HIGH, len(df))
        changed = True

    if changed:
        df.to_csv(CSV_PATH, index=False)

    return df


if __name__ == "__main__":
    main()
