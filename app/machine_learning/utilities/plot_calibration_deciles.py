"""Calibration plot of the shipped probability model: predicted P(survive) (x) versus
the actual empirical survival rate (y), one point per decile of predicted probability,
one panel per right.

The predictions come from the pickled classifiers in MODEL_PATH (written by
select_probability_model.py), evaluated on the same options_data.csv rows they were
fit on -- so this is an in-sample check: it shows systematic miscalibration, but a
flexible model can look better here than it will on new days.

Survival uses the selection's own label (survival_scoring.compute_survival_label):
max_ask strictly below the stop loss; a tie is a hit.

Deciles are equal-count bins of predicted probability. When many rows share the
same prediction (e.g. a zero-feature base-rate model predicts one value for every
trade) the duplicate edges merge, so a panel can show fewer than 10 points.

Run from the repo root:
    .venv/Scripts/python.exe -m app.machine_learning.utilities.plot_calibration_deciles
"""
import pickle
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from app.machine_learning.regress_max_ask import CSV_PATH
from app.machine_learning.select_probability_model import MODEL_PATH
from app.machine_learning.survival_scoring import CREDIT_COLUMN, STOP_COLUMN, TARGET_COLUMN, compute_survival_label

N_BINS = 10
RIGHTS = ["C", "P"]
OUTPUT_PATH = "machine_learning/data/calibration_deciles.png"

# Single series per panel -- reference palette's categorical slot 1 (blue) on light
# chart chrome (see dataviz skill references/palette.md); the perfect-calibration
# diagonal is a recessive reference line in muted ink.
MARK_COLOR = "#2a78d6"
CHART_SURFACE = "#fcfcfb"
PRIMARY_INK = "#0b0b0b"
MUTED_INK = "#898781"
GRIDLINE = "#e1e0d9"
BASELINE = "#c3c2b7"


def decile_table(p: np.ndarray, survived: np.ndarray) -> pd.DataFrame:
    """One row per equal-count bin of p: mean predicted, empirical survival rate,
    count, and a 95% Wilson interval for the survival rate."""
    bins = pd.qcut(pd.Series(p).rank(method="average"), N_BINS, labels=False, duplicates="drop") \
        if np.unique(p).size >= N_BINS else pd.Series(pd.factorize(np.round(p, 6), sort=True)[0])
    frame = pd.DataFrame({"p": p, "survived": survived, "bin": bins.to_numpy()})
    table = frame.groupby("bin").agg(mean_predicted=("p", "mean"), min_predicted=("p", "min"),
                                     max_predicted=("p", "max"), survival_rate=("survived", "mean"),
                                     survivals=("survived", "sum"), samples=("survived", "size"))
    z = 1.96
    n = table["samples"]
    rate = table["survival_rate"]
    centre = (rate + z ** 2 / (2 * n)) / (1 + z ** 2 / n)
    half_width = z * np.sqrt(rate * (1 - rate) / n + z ** 2 / (4 * n ** 2)) / (1 + z ** 2 / n)
    table["ci_low"] = centre - half_width
    table["ci_high"] = centre + half_width
    return table.reset_index(drop=True)


def predictions_for_right(df: pd.DataFrame, classifier, right: str) -> tuple[np.ndarray, np.ndarray]:
    features = classifier.get_feature_subset()
    required = list(dict.fromkeys(features + [TARGET_COLUMN, STOP_COLUMN, CREDIT_COLUMN]))
    rows = df[df["right"] == right].dropna(subset=required)
    rows = rows[(rows[TARGET_COLUMN] > 0) & (rows[CREDIT_COLUMN] > 0)].reset_index(drop=True)
    p, _ = classifier(rows[features], rows[STOP_COLUMN].to_numpy(dtype=float))
    return np.asarray(p, dtype=float), compute_survival_label(rows)


def draw_panel(ax, table: pd.DataFrame, right: str, n_rows: int, classifier_name: str):
    ax.set_facecolor(CHART_SURFACE)
    low = min(table["ci_low"].min(), table["mean_predicted"].min())
    lo = max(0.0, np.floor(low * 20) / 20 - 0.05)
    ax.plot([lo, 1], [lo, 1], color=MUTED_INK, linewidth=1, linestyle="--", label="Perfect calibration")

    yerr = [table["survival_rate"] - table["ci_low"], table["ci_high"] - table["survival_rate"]]
    ax.errorbar(table["mean_predicted"], table["survival_rate"], yerr=yerr, fmt="none",
                ecolor=MARK_COLOR, elinewidth=1, alpha=0.5, capsize=3)
    ax.plot(table["mean_predicted"], table["survival_rate"], color=MARK_COLOR, linewidth=2,
            marker="o", markersize=8, markeredgecolor=CHART_SURFACE, markeredgewidth=2,
            label="Decile (95% CI)")

    ax.set_xlim(lo, 1)
    ax.set_ylim(lo, 1.01)
    ax.set_aspect("equal")
    ax.set_title(f"{right}: {classifier_name}, {n_rows} trades, {len(table)} bin(s)",
                 color=PRIMARY_INK, fontsize=12, pad=10)
    ax.set_xlabel("Predicted probability of survival", color=MUTED_INK)
    ax.set_ylabel("Actual survival rate", color=MUTED_INK)
    ax.grid(True, color=GRIDLINE, linewidth=0.8)
    ax.set_axisbelow(True)
    for spine_name, spine in ax.spines.items():
        spine.set_color(BASELINE if spine_name in ("bottom", "left") else "none")
    ax.tick_params(colors=MUTED_INK)
    legend = ax.legend(frameon=False, loc="upper left")
    for text in legend.get_texts():
        text.set_color(PRIMARY_INK)


def main():
    df = pd.read_csv(CSV_PATH)
    with open(MODEL_PATH, "rb") as f:
        classifiers = pickle.load(f)

    fig, axes = plt.subplots(1, len(RIGHTS), figsize=(7 * len(RIGHTS), 7), facecolor=CHART_SURFACE)
    for ax, right in zip(np.atleast_1d(axes), RIGHTS):
        classifier = classifiers[right]
        p, survived = predictions_for_right(df, classifier, right)
        table = decile_table(p, survived)
        name = type(classifier).__name__
        print(f"\nRight = {right} ({name}, {len(p)} trades, overall survival rate {survived.mean():.4f}, "
              f"mean predicted {p.mean():.4f})")
        print(table.to_string(float_format=lambda v: f"{v:.4f}"))
        draw_panel(ax, table, right, len(p), name)

    fig.tight_layout()
    Path(OUTPUT_PATH).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUTPUT_PATH, dpi=150, facecolor=CHART_SURFACE)
    print(f"\nSaved to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
