"""Fixed, domain-driven bin edges in R = max_ask / credit space, and the
suffix-sum survival read-out. Edges are never fitted from data -- there is
no leakage path through the bin definition.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .logging_setup import get_logger

logger = get_logger(__name__)

MIN_BIN_COUNT_WARNING = 20


def ensure_candidate_k_in_edges(r_edges: tuple[float, ...], candidate_k: tuple[float, ...]) -> tuple[float, ...]:
    """Every candidate stop multiple must land exactly on an edge so
    P(R >= k) is an exact suffix sum, never an interpolation. Assert at
    build time; insert and re-sort if a candidate is missing."""
    edges = list(r_edges)
    missing = [k for k in candidate_k if k not in edges]
    if missing:
        logger.warning(f"candidate_k values missing from r_edges, inserting: {missing}")
        edges = sorted(set(edges) | set(missing))
    for k in candidate_k:
        assert k in edges, f"candidate k={k} still not in edges after insertion -- should be impossible"
    return tuple(edges)


def assign_bin(R_max: pd.Series, r_edges: tuple[float, ...]) -> np.ndarray:
    """Bin index = np.searchsorted(r_edges, R_max, side='right') - 1, i.e.
    bin i covers [edges[i], edges[i+1])."""
    values = R_max.to_numpy()
    if np.any(values < r_edges[0]):
        n_bad = int(np.sum(values < r_edges[0]))
        raise ValueError(
            f"{n_bad} rows have R_max < r_edges[0]={r_edges[0]}, which the fixed bin "
            f"definition does not cover. Not repairing silently -- inspect these rows."
        )
    edges_arr = np.array(r_edges)
    idx = np.searchsorted(edges_arr, values, side="right") - 1
    idx = np.clip(idx, 0, len(r_edges) - 2)
    return idx


def report_bin_counts(df: pd.DataFrame, bin_col: str, r_edges: tuple[float, ...], side_col: str = "option_type") -> pd.DataFrame:
    """Per-bin count, split by option_type (call/put). Warns (does not
    merge) on any bin with fewer than MIN_BIN_COUNT_WARNING observations."""
    n_bins = len(r_edges) - 1
    counts = pd.DataFrame(0, index=range(n_bins), columns=sorted(df[side_col].unique()))
    grouped = df.groupby([bin_col, side_col]).size()
    for (bin_idx, side), n in grouped.items():
        counts.loc[bin_idx, side] = n

    labels = []
    for i in range(n_bins):
        lo, hi = r_edges[i], r_edges[i + 1]
        labels.append(f"[{lo:g}, {hi:g})")
    counts.index = labels

    logger.info(f"Per-bin counts by side:\n{counts}")

    thin_region = [f"[{r_edges[i]:g}, {r_edges[i+1]:g})" for i in range(n_bins) if 3.0 <= r_edges[i] <= 5.0]
    if thin_region:
        logger.info(f"3-5 region bins (stop-relevant resolution): {thin_region}")
        logger.info(f"Counts in 3-5 region:\n{counts.loc[[l for l in thin_region]]}")

    total_by_bin = counts.sum(axis=1)
    thin = total_by_bin[total_by_bin < MIN_BIN_COUNT_WARNING]
    if len(thin):
        logger.warning(
            f"Bins with fewer than {MIN_BIN_COUNT_WARNING} total observations "
            f"(not auto-merged, review manually):\n{thin}"
        )
    return counts


def survival(pmf: np.ndarray, r_edges: tuple[float, ...], k: float) -> np.ndarray:
    """P(R_max >= k) for each row of pmf (shape [n_rows, n_bins]). k must be
    an element of r_edges -- this is an exact suffix sum, no interpolation.
    Monotone non-increasing in k by construction."""
    edges = list(r_edges)
    if k not in edges:
        raise ValueError(f"k={k} is not in r_edges; ensure_candidate_k_in_edges should have guaranteed this")
    j = edges.index(k)
    return pmf[:, j:].sum(axis=1)
