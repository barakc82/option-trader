"""Dot plot of the CV scores of every candidate model evaluated for one right.

One column per model method (transform / residual distribution, logistic, xgboost,
knn). Every candidate is a dot in its method's column, jittered sideways so the dots
don't hide each other, and coloured by its number of features, so the same feature
count has the same colour in every method. Up to eight feature
counts get the categorical palette's distinct hues, in fixed order; more than eight
step through a single-hue ordinal ramp, light (few features) to dark (many). The selected
model is ringed, and a dashed line marks the best score + 1 standard error, the
threshold of the one-standard-error rule. Lower scores are better.

Called by select_probability_model.main for each right; it can't be run on its own.
"""
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # file output only, also on machines without a display

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.lines import Line2D

# Reference palette (dataviz skill references/palette.md), light chart chrome.
# Categorical slots in their fixed order, one per feature count while there are at
# most eight; past that, a ninth hue is never generated -- the counts fall back to
# the sequential blue ramp used as an ordinal ramp, from step 250 (lightest that
# still clears 2:1 on the surface) to step 700.
CATEGORICAL = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
ORDINAL_RAMP = ["#86b6ef", "#6da7ec", "#5598e7", "#3987e5", "#2a78d6", "#256abf", "#1c5cab",
                "#184f95", "#104281", "#0d366b"]
CHART_SURFACE = "#fcfcfb"
PRIMARY_INK = "#0b0b0b"
SECONDARY_INK = "#52514e"
MUTED_INK = "#898781"
GRIDLINE = "#e1e0d9"
BASELINE = "#c3c2b7"

# Scores above the third quartile + Y_CLIP_IQRS interquartile ranges of all
# candidates' scores are left off the axis, so a few badly-fitting candidates don't
# squash every other dot into a flat line. Their count is noted.
Y_CLIP_IQRS = 3


def method_label(candidate) -> str:
    if candidate.distribution:
        return f"{candidate.transform_name}\n{candidate.distribution}"
    return candidate.transform_name


def plot_model_scores(right: str, candidates: list, winner, standard_error: float, output_path: str):
    """Writes the dot plot of candidates' scores for right to output_path."""
    methods = list(dict.fromkeys(method_label(c) for c in candidates))
    scores_by_method = {m: np.array([c.score for c in candidates if method_label(c) == m]) for m in methods}
    feature_counts = sorted({len(c.subset) for c in candidates})
    if len(feature_counts) <= len(CATEGORICAL):
        color_of_count = dict(zip(feature_counts, CATEGORICAL))
    else:
        ramp = LinearSegmentedColormap.from_list("ordinal", ORDINAL_RAMP)
        color_of_count = {count: ramp(i / (len(feature_counts) - 1)) for i, count in enumerate(feature_counts)}

    all_scores = np.array([c.score for c in candidates])
    best_score = all_scores.min()
    q1, q3 = np.percentile(all_scores, [25, 75])
    y_top = min(all_scores.max(), q3 + Y_CLIP_IQRS * (q3 - q1))
    y_top = max(y_top, best_score + standard_error, winner.score)
    margin = (y_top - best_score) * 0.05 or 0.01
    n_clipped = int((all_scores > y_top).sum())

    fig, ax = plt.subplots(figsize=(max(7.0, 1.3 * len(methods) + 2.5), 5.5), facecolor=CHART_SURFACE)
    ax.set_facecolor(CHART_SURFACE)

    positions = np.arange(1, len(methods) + 1)
    rng = np.random.default_rng(0)
    x_of_candidate = {}
    for position, method in zip(positions, methods):
        method_candidates = [c for c in candidates if method_label(c) == method and c.score <= y_top]
        # Fewer features drawn last, so they sit on top of the (more numerous) larger subsets.
        method_candidates.sort(key=lambda c: -len(c.subset))
        x = position + rng.uniform(-0.3, 0.3, size=len(method_candidates))
        x_of_candidate.update({id(c): x_value for c, x_value in zip(method_candidates, x)})
        ax.scatter(x, [c.score for c in method_candidates],
                   c=[color_of_count[len(c.subset)] for c in method_candidates],
                   s=14, linewidths=0.4, edgecolors=CHART_SURFACE, zorder=3)

    # The ring goes on the winner's own (jittered) dot; the column centre if it is off the axis.
    winner_position = x_of_candidate.get(id(winner), positions[methods.index(method_label(winner))])
    ax.scatter([winner_position], [winner.score], s=120, facecolors="none", edgecolors=PRIMARY_INK,
               linewidths=1.6, zorder=4)
    ax.annotate(f"selected ({len(winner.subset)} features)", (winner_position, winner.score),
                xytext=(10, -14), textcoords="offset points", fontsize=9, color=PRIMARY_INK)

    one_se_threshold = best_score + standard_error
    ax.axhline(one_se_threshold, color=MUTED_INK, linewidth=1, linestyle=(0, (4, 3)), zorder=2)
    ax.annotate("best + 1 SE", (0, one_se_threshold), xycoords=("axes fraction", "data"),
                xytext=(4, 4), textcoords="offset points", ha="left", fontsize=9, color=SECONDARY_INK)

    ax.set_xticks(positions)
    ax.set_xticklabels(methods, fontsize=9, color=SECONDARY_INK)
    ax.set_ylim(best_score - margin, y_top + margin)
    ax.set_ylabel("CV score (lower is better)", color=SECONDARY_INK)
    side = "Calls" if right == "C" else "Puts"
    title = f"{side}: CV scores of {len(candidates)} candidate models"
    if n_clipped:
        title += f" ({n_clipped} outliers above {y_top:.3f} not shown)"
    ax.set_title(title, color=PRIMARY_INK, fontsize=11, loc="left")
    ax.grid(axis="y", color=GRIDLINE, linewidth=0.8)
    ax.set_axisbelow(True)
    for spine in ("top", "right", "left"):
        ax.spines[spine].set_visible(False)
    ax.spines["bottom"].set_color(BASELINE)
    ax.tick_params(colors=MUTED_INK, length=0)

    legend_handles = [Line2D([], [], marker="o", linestyle="", markersize=7, markerfacecolor=color_of_count[count],
                             markeredgecolor=CHART_SURFACE, label=str(count)) for count in feature_counts]
    legend = ax.legend(handles=legend_handles, title="Features", loc="upper left", bbox_to_anchor=(1.01, 1),
                       frameon=False, fontsize=9, title_fontsize=9)
    plt.setp(legend.get_texts(), color=SECONDARY_INK)
    legend.get_title().set_color(SECONDARY_INK)

    fig.tight_layout()
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=150, facecolor=CHART_SURFACE)
    plt.close(fig)
