"""Plots how max_ask varies with a chosen feature column, from options_data.csv.

To plot against a different column, change X_COLUMN below.

Run directly from the repo root (so CSV_PATH resolves), same convention as
regress_max_ask.py / select_probability_model.py:
    .venv/Scripts/python.exe -m app.machine_learning.utilities.plot_max_ask_vs_feature
"""
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd

CSV_PATH = "machine_learning/data/options_data.csv"
X_COLUMN = "contract_iv"
Y_COLUMN = "max_ask"
OUTPUT_PATH = f"machine_learning/data/{Y_COLUMN}_vs_{X_COLUMN}.png"

# Single-series scatter -- reference palette's categorical slot 1 (blue) and
# light chart chrome (see dataviz skill references/palette.md).
MARK_COLOR = "#2a78d6"
CHART_SURFACE = "#fcfcfb"
PRIMARY_INK = "#0b0b0b"
MUTED_INK = "#898781"
GRIDLINE = "#e1e0d9"
BASELINE = "#c3c2b7"


def main():
    df = pd.read_csv(CSV_PATH)
    subset = df.dropna(subset=[X_COLUMN, Y_COLUMN])
    dropped = len(df) - len(subset)
    print(f"Plotting {len(subset)} row(s) ({dropped} dropped for missing {X_COLUMN}/{Y_COLUMN})")

    fig, ax = plt.subplots(figsize=(9, 6), facecolor=CHART_SURFACE)
    ax.set_facecolor(CHART_SURFACE)

    ax.scatter(subset[X_COLUMN], subset[Y_COLUMN], s=28, c=MARK_COLOR,
               alpha=0.55, linewidths=0.5, edgecolors=CHART_SURFACE)

    ax.set_title(f"{Y_COLUMN} vs. {X_COLUMN}", color=PRIMARY_INK, fontsize=14, pad=12)
    ax.set_xlabel(X_COLUMN, color=MUTED_INK)
    ax.set_ylabel(Y_COLUMN, color=MUTED_INK)

    ax.grid(True, axis="y", color=GRIDLINE, linewidth=0.8)
    ax.set_axisbelow(True)
    for spine_name, spine in ax.spines.items():
        spine.set_color(BASELINE if spine_name in ("bottom", "left") else "none")
    ax.tick_params(colors=MUTED_INK)

    fig.tight_layout()
    Path(OUTPUT_PATH).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUTPUT_PATH, dpi=150, facecolor=CHART_SURFACE)
    print(f"Saved to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
