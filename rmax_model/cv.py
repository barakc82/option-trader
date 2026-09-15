"""Cross-validation splitters, grouped by session date. Never KFold, never a
random split, never grouping by contract or trade -- every trade on a given
date shares one underlying path and positives arrive in clumps, so the date
is the unit of exchangeability, not the row.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.model_selection import GroupKFold

from .logging_setup import get_logger

logger = get_logger(__name__)


def group_kfold_splits(groups: pd.Series, n_splits: int, seed: int) -> list[tuple[np.ndarray, np.ndarray]]:
    """GroupKFold by session date. n_splits is clamped down to the number of
    distinct dates if there aren't enough of them, and a warning is logged
    so the caller can flag results as thin."""
    n_groups = groups.nunique()
    n_splits_eff = max(2, min(n_splits, n_groups))
    if n_splits_eff < n_splits:
        logger.warning(f"group_kfold_splits: requested n_splits={n_splits} but only {n_groups} distinct dates; using {n_splits_eff}")
    if n_groups < 2:
        raise ValueError(f"GroupKFold needs at least 2 distinct groups, got {n_groups}")
    gkf = GroupKFold(n_splits=n_splits_eff, shuffle=True, random_state=seed)
    return list(gkf.split(np.zeros(len(groups)), groups=groups))


def walk_forward_splits(groups: pd.Series, min_train_days: int) -> list[tuple[np.ndarray, np.ndarray]]:
    """Expanding-window walk-forward: train on all sessions strictly before
    date d, test on the single next date. Measures skill under regime drift,
    as opposed to GroupKFold's measure of skill under a stationary
    assumption. Returns [] (with a warning) if there aren't enough distinct
    dates to produce even one split."""
    unique_dates = np.sort(groups.unique())
    splits = []
    for i in range(min_train_days, len(unique_dates)):
        test_date = unique_dates[i]
        train_dates = unique_dates[:i]
        train_idx = np.flatnonzero(groups.isin(train_dates).to_numpy())
        test_idx = np.flatnonzero((groups == test_date).to_numpy())
        splits.append((train_idx, test_idx))
    if not splits:
        logger.warning(
            f"walk_forward_splits: only {len(unique_dates)} distinct dates and "
            f"min_train_days={min_train_days}; no walk-forward splits are possible"
        )
    return splits
