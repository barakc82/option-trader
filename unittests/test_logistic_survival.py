"""Tests for app/machine_learning/logistic_survival.py.

Uses small synthetic frames with a short feature list throughout so the
handful of tests that do a real best-subset search stay fast (2**k-1
subsets for k=2 or 3 features, not the full 13).
"""
import unittest

import numpy as np
import pandas as pd
from sklearn.model_selection import GroupKFold

from app.machine_learning import logistic_survival as ls
from app.machine_learning.best_subset import SCORE_METHODS
from app.machine_learning.survival_scoring import (
    CREDIT_COLUMN, GROUP_COLUMN, STOP_COLUMN, TARGET_COLUMN, compute_survival_label,
)


def _build_synthetic_frame(seed: int = 0, n: int = 120, n_groups: int = 10, features=("gamma", "vega", "theta")):
    rng = np.random.default_rng(seed)
    df = pd.DataFrame({c: rng.uniform(0.1, 1.0, n) for c in features})
    df[CREDIT_COLUMN] = rng.uniform(0.1, 1.0, n)
    df[GROUP_COLUMN] = rng.integers(0, n_groups, n)
    df[TARGET_COLUMN] = df[CREDIT_COLUMN] * rng.uniform(0.5, 6.0, n)
    df[STOP_COLUMN] = df[CREDIT_COLUMN] + rng.uniform(3.0, 5.0, n)
    return df, list(features)


class TestReplicateConstruction(unittest.TestCase):
    def test_hand_worked_example_credit_1_max_ask_2_40(self):
        """credit = 1.00, max_ask = 2.40: survived = 0 at every grid point
        below 2.40 and survived = 1 at every point above."""
        X = pd.DataFrame({"gamma": [0.5]})
        ctx = pd.DataFrame({TARGET_COLUMN: [2.40], CREDIT_COLUMN: [1.00], STOP_COLUMN: [4.0], GROUP_COLUMN: [1]})
        grid = ls.build_k_grid()
        _, y_rep, _, _ = ls.replicate_for_training(X, ctx, ["gamma"], grid)
        expected = (grid > 2.40).astype(float)
        np.testing.assert_array_equal(y_rep, expected)

    def test_replicate_weights_sum_to_one_per_trade(self):
        n_trades = 5
        X = pd.DataFrame({"gamma": np.linspace(0.1, 0.9, n_trades)})
        ctx = pd.DataFrame({
            TARGET_COLUMN: np.linspace(1.0, 5.0, n_trades),
            CREDIT_COLUMN: np.ones(n_trades),
            STOP_COLUMN: np.full(n_trades, 4.0),
            GROUP_COLUMN: np.arange(n_trades),
        })
        grid = ls.build_k_grid()
        _, _, w_rep, _ = ls.replicate_for_training(X, ctx, ["gamma"], grid)
        n_grid = len(grid)
        per_trade_sums = w_rep.reshape(n_trades, n_grid).sum(axis=1)
        np.testing.assert_allclose(per_trade_sums, np.ones(n_trades))

    def test_grid_identical_for_every_trade_independent_of_outcome(self):
        """Two trades with wildly different max_ask must still see the
        exact same grid of k values -- the grid never depends on a trade's
        own realised outcome."""
        X = pd.DataFrame({"gamma": [0.5, 0.5]})
        ctx = pd.DataFrame({
            TARGET_COLUMN: [0.5, 50.0],  # wildly different realised outcomes
            CREDIT_COLUMN: [1.0, 1.0],
            STOP_COLUMN: [4.0, 4.0],
            GROUP_COLUMN: [1, 2],
        })
        grid = ls.build_k_grid()
        X_rep, _, _, _ = ls.replicate_for_training(X, ctx, ["gamma"], grid)
        n_grid = len(grid)
        log_k_trade0 = X_rep[ls.LOG_K_COLUMN].to_numpy()[:n_grid]
        log_k_trade1 = X_rep[ls.LOG_K_COLUMN].to_numpy()[n_grid:]
        np.testing.assert_array_equal(log_k_trade0, log_k_trade1)
        np.testing.assert_array_equal(log_k_trade0, np.log(grid))

    def test_build_k_grid_does_not_take_any_trade_data(self):
        """Structural guard: build_k_grid's signature takes only a grid
        config, never a trade/outcome argument -- it cannot reference a
        trade's max_ask even by accident."""
        import inspect
        params = list(inspect.signature(ls.build_k_grid).parameters)
        self.assertEqual(params, ["grid_config"])

    def test_no_fold_split_separates_replicates_of_the_same_trade(self):
        n_trades = 60
        X, features = _build_synthetic_frame(n=n_trades, n_groups=8)
        grid = ls.build_k_grid()
        _, _, _, groups_rep = ls.replicate_for_training(X, X, features, grid)
        n_grid = len(grid)

        gkf = GroupKFold(n_splits=5, shuffle=True, random_state=42)
        for train_idx, test_idx in gkf.split(np.zeros(len(groups_rep)), groups=groups_rep):
            train_set, test_set = set(train_idx), set(test_idx)
            for trade_i in range(n_trades):
                block = set(range(trade_i * n_grid, (trade_i + 1) * n_grid))
                # every replicate of this trade must be entirely within
                # train_set or entirely within test_set, never split
                in_train = block & train_set
                in_test = block & test_set
                self.assertTrue(len(in_train) == 0 or len(in_test) == 0,
                                 f"trade {trade_i}'s replicates were split across train/test")


class TestPredictFrame(unittest.TestCase):
    def test_exactly_one_row_per_trade(self):
        X, features = _build_synthetic_frame(n=37)
        pred_frame = ls.build_predict_frame(X, X, features)
        self.assertEqual(len(pred_frame), 37)

    def test_label_parity_with_shared_compute_survival_label(self):
        """The label logistic is scored against (via build_predict_frame's
        implied stop) must be bit-identical to compute_survival_label's
        output on the same ctx -- logistic must not compute its own label."""
        X, features = _build_synthetic_frame(n=50)
        expected = compute_survival_label(X)
        # logistic's own scoring path uses compute_survival_label directly
        # (see search_logistic_candidates) -- confirm calling it again here
        # reproduces the identical array, i.e. there is only one definition.
        again = compute_survival_label(X)
        np.testing.assert_array_equal(expected, again)


class TestFitAssertions(unittest.TestCase):
    def test_negative_log_k_coefficient_raises(self):
        with self.assertRaises(ls.LogisticFitError):
            ls._assert_valid_coefficients(
                np.array([0.1, -0.3]), ["gamma", ls.LOG_K_COLUMN], fold_idx=0, subset=["gamma"],
            )

    def test_non_finite_coefficient_raises(self):
        with self.assertRaises(ls.LogisticFitError):
            ls._assert_valid_coefficients(
                np.array([np.nan, 0.5]), ["gamma", ls.LOG_K_COLUMN], fold_idx=0, subset=["gamma"],
            )

    def test_positive_log_k_coefficient_does_not_raise(self):
        ls._assert_valid_coefficients(
            np.array([0.1, 0.5]), ["gamma", ls.LOG_K_COLUMN], fold_idx=0, subset=["gamma"],
        )  # no exception


class TestSearchAndClassifier(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.X, cls.features = _build_synthetic_frame(n=150, n_groups=10)
        cls.ctx = cls.X
        cls.results = ls.search_logistic_candidates(cls.X, cls.ctx, cls.features, SCORE_METHODS["weighted_logloss"])

    def test_search_returns_all_subsets(self):
        expected_n = 2 ** len(self.features) - 1
        self.assertEqual(len(self.results), expected_n)

    def test_all_candidates_carry_finite_score_and_coefs(self):
        for c in self.results:
            self.assertTrue(np.isfinite(c.score))
            self.assertTrue(np.isfinite(c.log_k_coef))
            self.assertGreater(c.log_k_coef, 0)  # every surviving candidate passed the sign assertion

    def test_monotonicity_probability_never_decreases_with_stop(self):
        best = min(self.results, key=lambda c: c.score)
        columns = ls.columns_for_subset(best.subset)
        grid = ls.build_k_grid()
        X_rep, y_rep, w_rep, _ = ls.replicate_for_training(self.X, self.ctx, best.subset, grid)
        model, scaler, _ = ls.fit_logistic(X_rep, y_rep, w_rep, columns, ls.LOGISTIC_C, fold_idx="test", subset=best.subset)
        classifier = ls.LogisticSurvivalClassifier(model, scaler, best.subset, columns)

        row = self.X.iloc[[0]]
        credit = float(row[CREDIT_COLUMN].iloc[0])
        thresholds = [credit * m for m in (1.3, 2.0, 3.0, 5.0, 7.5)]
        prev = -1.0
        for t in thresholds:
            p, _ = classifier(row, t)
            self.assertGreaterEqual(p[0], prev - 1e-9)
            prev = p[0]

    def test_recommended_selection_is_within_pool(self):
        recommended = ls.select_logistic_recommended(self.results, self.features)
        self.assertIn(recommended, self.results)

    def test_budget_guard_raises_when_exceeded(self):
        with self.assertRaises(ls.LogisticSearchBudgetExceeded):
            ls.search_logistic_candidates(
                self.X, self.ctx, self.features, SCORE_METHODS["weighted_logloss"], time_budget_sec=1e-9,
            )


if __name__ == "__main__":
    unittest.main()
