"""Realized probability of not hitting the stop loss, per bucket of predicted probability,
for the trades in options_data.csv whose target_delta is exactly zero.

Each such trade is run through the shipped probability model (the pickled per-right
classifiers in MODEL_PATH, written by select_probability_model.py), with the trade's own
stop_loss as the threshold. The raw classifier output is used: the live Predictor's
calibration corrections are not applied. The trades are then grouped into fixed ranges of
predicted probability (PROBABILITY_BUCKET_EDGES) and, for each range, the plot shows the
share that did not hit the stop loss (with a 95% Wilson interval) next to the mean
predicted probability in that range. One panel per right, plus one for both rights
pooled.

Not hitting the stop loss uses the shared survival label
(survival_scoring.compute_survival_label): max_ask strictly below stop_loss, so a tie is
a hit.

Note: the model was fit on options_data.csv, which includes these trades, so this is an
in-sample check.

Run from the repo root:
    .venv/Scripts/python.exe -m app.machine_learning.plot_zero_target_delta_calibration
"""
import pickle
from datetime import date
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # file output only, also on machines without a display

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.lines import Line2D
from matplotlib.patches import Patch

from app.machine_learning.regress_max_ask import CSV_PATH
from app.machine_learning.select_probability_model import MODEL_PATH
from app.machine_learning.survival_scoring import STOP_COLUMN, compute_survival_label

TARGET_DELTA_COLUMN = "target_delta"
DATETIME_COLUMN = "datetime"
RIGHTS = ["C", "P"]
RIGHT_NAMES = {"C": "Calls", "P": "Puts"}

# Bucket edges of predicted probability. The lower part mirrors the live evaluation's
# buckets (predictions_evaluator.BUCKET_LOWER_BOUNDS = [0, 0.7, 0.8, 0.9]); the top
# bucket is split further since most predictions land above 0.9. Each bucket is
# [lower, upper), except the last, which includes 1.
PROBABILITY_BUCKET_EDGES = [0.0, 0.7, 0.8, 0.9, 0.95, 1.0]

OUTPUT_PATH = "machine_learning/data/zero_target_delta_calibration.png"

# Reference palette (dataviz skill references/palette.md), light chart chrome: the
# realized rate is the one data series (categorical slot 1, blue); the mean predicted
# probability is a reference tick in primary ink.
MARK_COLOR = "#2a78d6"
CHART_SURFACE = "#fcfcfb"
PRIMARY_INK = "#0b0b0b"
SECONDARY_INK = "#52514e"
MUTED_INK = "#898781"
GRIDLINE = "#e1e0d9"
BASELINE = "#c3c2b7"


def load_zero_target_delta_predictions(starting_from: date | None = None) -> pd.DataFrame:
    """One row per zero-target-delta trade the model can score: its right, predicted
    probability of not hitting the stop loss, and whether it actually did not hit it.
    When starting_from is given, trades recorded before that date (by the date of their
    DATETIME_COLUMN, as written in the file) are ignored."""
    df = pd.read_csv(CSV_PATH)
    df = df[df[TARGET_DELTA_COLUMN] == 0]
    if starting_from is not None:
        trade_dates = pd.to_datetime(df[DATETIME_COLUMN]).dt.date
        ignored = int((trade_dates < starting_from).sum())
        df = df[trade_dates >= starting_from]
        print(f"Considering trades from {starting_from} onwards ({ignored} earlier trade(s) ignored)")
    with open(MODEL_PATH, "rb") as f:
        classifiers = pickle.load(f)

    frames = []
    for right in RIGHTS:
        classifier = classifiers[right]
        features = classifier.get_feature_subset()
        rows = df[df["right"] == right]
        scorable = rows.dropna(subset=list(dict.fromkeys(features + [STOP_COLUMN, "max_ask"])))
        dropped = len(rows) - len(scorable)
        print(f"{right}: {type(classifier).__name__} on {features}, {len(scorable)} trade(s)"
              + (f" ({dropped} dropped for missing values)" if dropped else ""))
        if scorable.empty:
            continue
        probabilities, _ = classifier(scorable[features], scorable[STOP_COLUMN].to_numpy(dtype=float))
        frames.append(pd.DataFrame({
            "right": right,
            "predicted": np.asarray(probabilities, dtype=float),
            "not_hit": compute_survival_label(scorable),
        }))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=["right", "predicted", "not_hit"])


def bucket_table(predictions: pd.DataFrame) -> pd.DataFrame:
    """One row per PROBABILITY_BUCKET_EDGES bucket (empty buckets included): trade count,
    mean predicted probability, realized not-hit rate and its 95% Wilson interval."""
    edges = PROBABILITY_BUCKET_EDGES
    index = np.clip(np.searchsorted(edges, predictions["predicted"].to_numpy(), side="right") - 1,
                    0, len(edges) - 2)
    rows = []
    for i in range(len(edges) - 1):
        in_bucket = predictions[index == i]
        n = len(in_bucket)
        rate = in_bucket["not_hit"].mean() if n else np.nan
        ci_low, ci_high = wilson_interval(rate, n)
        rows.append({
            "bucket": f"{edges[i]:.0%}-{edges[i + 1]:.0%}",
            "trades": n,
            "not_hit": int(in_bucket["not_hit"].sum()),
            "mean_predicted": in_bucket["predicted"].mean() if n else np.nan,
            "realized_rate": rate,
            "ci_low": ci_low,
            "ci_high": ci_high,
        })
    return pd.DataFrame(rows)


def wilson_interval(rate: float, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return np.nan, np.nan
    centre = (rate + z ** 2 / (2 * n)) / (1 + z ** 2 / n)
    half_width = z * np.sqrt(rate * (1 - rate) / n + z ** 2 / (4 * n ** 2)) / (1 + z ** 2 / n)
    # The interval always contains rate, but at rate 0 or 1 rounding can put an edge ~1e-16
    # on the wrong side of it, which would make draw_panel's yerr negative (matplotlib
    # rejects that), so clamp the edges around rate and into [0, 1].
    return max(0.0, min(centre - half_width, rate)), min(1.0, max(centre + half_width, rate))


def draw_panel(ax, table: pd.DataFrame, title: str):
    ax.set_facecolor(CHART_SURFACE)
    x = np.arange(len(table))
    filled = table["trades"] > 0

    ax.bar(x[filled], table.loc[filled, "realized_rate"], width=0.6, color=MARK_COLOR, zorder=2)
    yerr = [table.loc[filled, "realized_rate"] - table.loc[filled, "ci_low"],
            table.loc[filled, "ci_high"] - table.loc[filled, "realized_rate"]]
    ax.errorbar(x[filled], table.loc[filled, "realized_rate"], yerr=yerr, fmt="none",
                ecolor=SECONDARY_INK, elinewidth=1, capsize=3, zorder=3)
    ax.hlines(table.loc[filled, "mean_predicted"], x[filled] - 0.3, x[filled] + 0.3,
              colors=PRIMARY_INK, linewidth=2, linestyles="--", zorder=4)

    for xi, row in zip(x, table.itertuples()):
        label = f"{row.realized_rate:.0%}\nn={row.trades}" if row.trades else "n=0"
        ax.text(xi, 1.02, label, ha="center", va="bottom", fontsize=9, color=SECONDARY_INK)

    ax.set_xticks(x, table["bucket"])
    ax.set_xlim(-0.6, len(table) - 0.4)
    ax.set_ylim(0, 1.15)
    ax.set_yticks(np.linspace(0, 1, 6))
    ax.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0))
    ax.set_title(f"{title}: {int(table['trades'].sum())} trades", color=PRIMARY_INK, fontsize=12, pad=24)
    ax.set_xlabel("Predicted probability of not hitting the stop loss", color=MUTED_INK)
    ax.set_ylabel("Realized rate of not hitting the stop loss", color=MUTED_INK)
    ax.grid(True, axis="y", color=GRIDLINE, linewidth=0.8)
    ax.set_axisbelow(True)
    for spine_name, spine in ax.spines.items():
        spine.set_color(BASELINE if spine_name in ("bottom", "left") else "none")
    ax.tick_params(colors=MUTED_INK)


def main():
    # Only trades recorded on this date or later are considered; earlier ones are ignored.
    # None considers every trade. E.g. starting_from = date(2026, 11, 1)
    # starting_from = None
    starting_from = date(2026, 11, 17)

    predictions = load_zero_target_delta_predictions(starting_from)
    if predictions.empty:
        print("No trades with a target delta of exactly zero could be scored")
        return

    panels = [(RIGHT_NAMES[right], predictions[predictions["right"] == right]) for right in RIGHTS]
    panels.append(("Both rights", predictions))

    fig, axes = plt.subplots(1, len(panels), figsize=(6.5 * len(panels), 6), facecolor=CHART_SURFACE)
    for ax, (title, panel_predictions) in zip(np.atleast_1d(axes), panels):
        table = bucket_table(panel_predictions)
        print(f"\n{title} ({len(panel_predictions)} trades, realized rate "
              f"{panel_predictions['not_hit'].mean():.4f}, mean predicted {panel_predictions['predicted'].mean():.4f})")
        print(table.to_string(index=False, float_format=lambda v: f"{v:.4f}"))
        draw_panel(ax, table, title)

    legend_handles = [Patch(color=MARK_COLOR, label="Realized rate (95% CI)"),
                      Line2D([], [], color=PRIMARY_INK, linewidth=2, linestyle="--", label="Mean predicted probability")]
    legend = fig.legend(handles=legend_handles, loc="upper center", ncol=2, frameon=False,
                        bbox_to_anchor=(0.5, 1.0))
    for text in legend.get_texts():
        text.set_color(PRIMARY_INK)
    since = f" since {starting_from}" if starting_from is not None else ""
    fig.suptitle(f"Trades with a target delta of zero{since}: predicted vs. realized probability of not hitting "
                 f"the stop loss", color=PRIMARY_INK, fontsize=13, y=1.06)

    fig.tight_layout()
    Path(OUTPUT_PATH).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUTPUT_PATH, dpi=150, facecolor=CHART_SURFACE, bbox_inches="tight")
    print(f"\nSaved to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
