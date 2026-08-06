from pathlib import Path

import pandas as pd

CSV_PATH = Path(__file__).parent / "data" / "options_data.csv"

DELTA_COLUMNS = ["bid_delta", "ask_delta", "last_delta", "model_delta"]


def main():
    df = pd.read_csv(CSV_PATH)
    df["max_delta"] = df[DELTA_COLUMNS].max(axis=1)
    df.to_csv(CSV_PATH, index=False)


if __name__ == "__main__":
    main()
