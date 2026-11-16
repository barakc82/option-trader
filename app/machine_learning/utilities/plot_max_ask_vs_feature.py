"""Plots how max_ask varies with a chosen feature column, from options_data.csv.

To plot against a different column, change X_COLUMN below.

Run directly from the repo root (so CSV_PATH resolves), same convention as
regress_max_ask.py / select_probability_model.py:
    .venv/Scripts/python.exe -m app.machine_learning.utilities.plot_max_ask_vs_feature
"""
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.ticker import ScalarFormatter
import pandas as pd

from app.machine_learning.survival_scoring import STOP_COLUMN, TARGET_COLUMN, is_stop_loss_hit

CSV_PATH = "machine_learning/data/options_data.csv"
X_COLUMN = "log_k"
Y_COLUMN = "stop_loss"
LOG_SCALE_Y = False  # plot the y axis on a log scale (needs Y_COLUMN > 0)
OUTPUT_PATH = f"machine_learning/data/{Y_COLUMN}_vs_{X_COLUMN}.png"

# Stop-loss hits (max_ask >= stop_loss) in red, misses in blue, on light chart
# chrome (see dataviz skill references/palette.md).
HIT_COLOR = "#d6342a"
MISS_COLOR = "#2a78d6"
CHART_SURFACE = "#fcfcfb"
PRIMARY_INK = "#0b0b0b"
MUTED_INK = "#898781"
GRIDLINE = "#e1e0d9"
BASELINE = "#c3c2b7"


def main():
    df = pd.read_csv(CSV_PATH)
    # Hits are always judged on max_ask vs. the stop, whatever columns are plotted.
    required_columns = list(dict.fromkeys([X_COLUMN, Y_COLUMN, TARGET_COLUMN, STOP_COLUMN]))
    subset = df.dropna(subset=required_columns)
    dropped = len(df) - len(subset)
    print(f"Plotting {len(subset)} row(s) ({dropped} dropped for missing {'/'.join(required_columns)})")

    fig, ax = plt.subplots(figsize=(9, 6), facecolor=CHART_SURFACE)
    ax.set_facecolor(CHART_SURFACE)

    is_hit = is_stop_loss_hit(subset[TARGET_COLUMN], subset[STOP_COLUMN])
    misses, hits = subset[~is_hit], subset[is_hit]
    print(f"Stop-loss hits: {len(hits)}, misses: {len(misses)}")
    # Misses first so the rarer hits are drawn on top.
    ax.scatter(misses[X_COLUMN], misses[Y_COLUMN], s=28, c=MISS_COLOR, alpha=0.55, linewidths=0.5,
               edgecolors=CHART_SURFACE, label=f"Stop loss not hit ({len(misses)})")
    ax.scatter(hits[X_COLUMN], hits[Y_COLUMN], s=28, c=HIT_COLOR, alpha=0.75, linewidths=0.5,
               edgecolors=CHART_SURFACE, label=f"Stop loss hit ({len(hits)})")
    legend = ax.legend(frameon=False, loc="upper right")
    for text in legend.get_texts():
        text.set_color(PRIMARY_INK)

    ax.set_title(f"{Y_COLUMN} vs. {X_COLUMN}", color=PRIMARY_INK, fontsize=14, pad=12)
    ax.set_xlabel(X_COLUMN, color=MUTED_INK)
    if LOG_SCALE_Y:
        ax.set_ylabel(f"{Y_COLUMN} (log scale)", color=MUTED_INK)
        ax.set_yscale("log")
        ax.yaxis.set_major_formatter(ScalarFormatter())
    else:
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
