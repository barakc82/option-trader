"""Leakage canary: a per-row split that lets the same session date appear in
both train and test must look detectably better than the correct
GroupKFold-by-date split, on data where the true signal lives entirely at
the date level. If this test ever fails, something in cv.py has stopped
grouping by date correctly.
"""
import numpy as np
import pandas as pd
from sklearn.model_selection import KFold

from rmax_model.cv import group_kfold_splits
from rmax_model.evaluate import brier


def _date_effect_dataset(seed: int = 0, n_dates: int = 10, n_per_date: int = 15):
    rng = np.random.default_rng(seed)
    date_rate = rng.uniform(0.05, 0.95, n_dates)  # each date has its own true hit rate
    session_date = np.repeat(np.arange(n_dates), n_per_date)
    labels = rng.uniform(0, 1, n_dates * n_per_date) < np.repeat(date_rate, n_per_date)
    return pd.Series(session_date), labels.astype(float), date_rate


def _date_lookup_predict(train_dates, train_labels, test_dates, global_fallback):
    """A deliberately naive 'cheat' predictor: for each test row, predict the
    training-set mean label for rows sharing its exact session_date, falling
    back to the global training mean if that date wasn't in training. This
    is exactly the shape of leak a row-level (non-grouped) split allows and
    a date-grouped split forbids."""
    per_date_mean = pd.Series(train_labels).groupby(train_dates.reset_index(drop=True)).mean()
    return np.array([per_date_mean.get(d, global_fallback) for d in test_dates])


def test_row_level_split_leaks_relative_to_grouped_split():
    groups, labels, _ = _date_effect_dataset()
    seed = 42

    # Correct: GroupKFold by date -- a test date's rows are never in that
    # fold's training set, so the lookup predictor can't use the test date's
    # own label mean and must fall back to the global training mean.
    grouped_splits = group_kfold_splits(groups, n_splits=5, seed=seed)
    grouped_preds = np.full(len(labels), np.nan)
    for train_idx, test_idx in grouped_splits:
        fallback = labels[train_idx].mean()
        grouped_preds[test_idx] = _date_lookup_predict(
            groups.iloc[train_idx], labels[train_idx], groups.iloc[test_idx].to_numpy(), fallback,
        )
    grouped_brier = brier(grouped_preds, labels)

    # Leaky: plain row-level KFold ignores groups entirely, so a date's rows
    # are split between train and test within the same fold -- the lookup
    # predictor sees that exact date's own (partial) label mean at test time.
    kf = KFold(n_splits=5, shuffle=True, random_state=seed)
    leaky_preds = np.full(len(labels), np.nan)
    for train_idx, test_idx in kf.split(np.zeros(len(labels))):
        fallback = labels[train_idx].mean()
        leaky_preds[test_idx] = _date_lookup_predict(
            groups.iloc[train_idx], labels[train_idx], groups.iloc[test_idx].to_numpy(), fallback,
        )
    leaky_brier = brier(leaky_preds, labels)

    assert leaky_brier < grouped_brier, (
        f"expected the leaky row-level split to look better (lower Brier) than the correct "
        f"grouped split -- got leaky={leaky_brier:.4f}, grouped={grouped_brier:.4f}. "
        f"If these are no longer detectably different, the leakage canary itself is broken."
    )
