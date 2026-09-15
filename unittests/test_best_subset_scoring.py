"""Tests for the pluggable SCORE_FN machinery in
app/machine_learning/best_subset.py: weighted logloss, the standard-error /
within-1-SE reporting, and the (feature subset x residual distribution)
search itself.

The end-to-end search test uses a tiny 3-feature list (not the full
13-feature CANDIDATE_FEATURE_COLUMNS) so it runs in well under a second --
2**3-1 = 7 subsets x 2 distributions = 14 candidates -- while still
exercising the real code path.
"""
import unittest

import numpy as np
import pandas as pd
from sklearn.linear_model import LinearRegression

from app.machine_learning.best_subset import (
    CandidateResult, SCORE_METHODS, DEFAULT_SCORE_METHOD_NAME,
    count_within_one_se, logloss_score, search_best_subset_with_distribution,
    standard_error_from_fold_scores, weighted_logloss_score,
)
from app.machine_learning.probability_transforms import TRANSFORMS
from app.machine_learning.survival_scoring import (
    CREDIT_COLUMN, CTX_COLUMNS, STOP_COLUMN, TARGET_COLUMN,
    NormalResidualDistribution, TransformedResidualClassifier,
)


class TestScoreMethods(unittest.TestCase):
    def test_default_is_weighted_logloss(self):
        self.assertEqual(DEFAULT_SCORE_METHOD_NAME, "weighted_logloss")

    def test_needs_distribution_flags(self):
        self.assertFalse(SCORE_METHODS["rss"].needs_distribution)
        self.assertTrue(SCORE_METHODS["logloss"].needs_distribution)
        self.assertTrue(SCORE_METHODS["weighted_logloss"].needs_distribution)

    def test_weighted_logloss_hand_worked(self):
        p = np.array([0.9, 0.1])
        y = np.array([1.0, 0.0])
        weight = np.array([1.0, 3.0])
        # Both predictions are "correct" (p close to y), so per-row loss:
        # row0: -log(0.9) ~= 0.10536; row1: -log(1-0.1) = -log(0.9) ~= 0.10536
        per_row = -np.log(0.9)
        expected = (weight[0] * per_row + weight[1] * per_row) / (weight[0] + weight[1])
        got = weighted_logloss_score(p, y, weight)
        self.assertAlmostEqual(got, expected, places=6)
        self.assertAlmostEqual(got, per_row, places=6)  # weights cancel out when both rows agree equally well

    def test_weighted_logloss_weights_bigger_rows_more(self):
        """A row with a much larger weight should dominate the score --
        confirm by making that row's prediction bad and the other's good,
        and checking the weighted score is much closer to the bad row's
        loss than an unweighted average would be."""
        p = np.array([0.99, 0.01])  # row0 good, row1 (survived) very bad
        y = np.array([1.0, 1.0])
        weight = np.array([1.0, 99.0])
        weighted = weighted_logloss_score(p, y, weight)
        unweighted = logloss_score(p, y)
        bad_row_loss = -np.log(0.01)
        self.assertGreater(weighted, unweighted)
        self.assertAlmostEqual(weighted, bad_row_loss, places=1)

    def test_weight_is_stop_minus_credit(self):
        ctx = pd.DataFrame({STOP_COLUMN: [4.0, 5.5, 3.2], CREDIT_COLUMN: [1.0, 0.5, 0.2]})
        weight = (ctx[STOP_COLUMN] - ctx[CREDIT_COLUMN]).to_numpy()
        np.testing.assert_allclose(weight, np.array([3.0, 5.0, 3.0]))


class TestStandardErrorAndCount(unittest.TestCase):
    def test_standard_error_hand_worked(self):
        fold_scores = [1.0, 2.0, 3.0, 4.0, 5.0]
        se = standard_error_from_fold_scores(fold_scores)
        expected = float(np.std(fold_scores, ddof=1) / np.sqrt(5))
        self.assertAlmostEqual(se, expected, places=10)

    def test_standard_error_nan_for_single_fold(self):
        se = standard_error_from_fold_scores([1.0])
        self.assertTrue(np.isnan(se))

    def test_count_within_one_se_hand_worked(self):
        candidates = [
            CandidateResult("raw", ["a"], "normal", 1.0, [1.0, 1.0]),
            CandidateResult("raw", ["b"], "normal", 1.05, [1.05, 1.05]),
            CandidateResult("raw", ["c"], "normal", 1.5, [1.5, 1.5]),
        ]
        best_score = 1.0
        se = 0.1
        count = count_within_one_se(candidates, best_score, se)
        # 1.0 <= 1.1 (yes), 1.05 <= 1.1 (yes), 1.5 <= 1.1 (no)
        self.assertEqual(count, 2)

    def test_count_within_one_se_returns_one_when_se_not_finite(self):
        candidates = [
            CandidateResult("raw", ["a"], "normal", 1.0, [1.0]),
            CandidateResult("raw", ["b"], "normal", 1.01, [1.01]),
        ]
        count = count_within_one_se(candidates, 1.0, float("nan"))
        self.assertEqual(count, 1)


def _build_synthetic_frame(seed: int = 0, n: int = 120, n_groups: int = 10) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    small_features = ["gamma", "vega", "theta"]
    df = pd.DataFrame({c: rng.uniform(0.1, 1.0, n) for c in small_features})
    df[CREDIT_COLUMN] = rng.uniform(0.1, 1.0, n)
    df["expiration"] = rng.integers(0, n_groups, n)
    df[TARGET_COLUMN] = df[CREDIT_COLUMN] * rng.uniform(0.5, 6.0, n)
    df[STOP_COLUMN] = df[CREDIT_COLUMN] + rng.uniform(3.0, 5.0, n)
    return df, small_features


class TestSearchBestSubsetWithDistribution(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.df, cls.small_features = _build_synthetic_frame()
        cls.X = cls.df[cls.small_features]
        cls.ctx = cls.df[CTX_COLUMNS]
        cls.results = search_best_subset_with_distribution(
            cls.X, cls.ctx, TRANSFORMS["raw"], "raw", cls.small_features, SCORE_METHODS["weighted_logloss"],
        )

    def test_enumerates_every_subset_and_distribution_combo(self):
        n_subsets = 2 ** len(self.small_features) - 1
        self.assertEqual(len(self.results), n_subsets * 2)
        distributions_seen = {c.distribution for c in self.results}
        self.assertEqual(distributions_seen, {"normal", "empirical"})

    def test_all_scores_finite_and_fold_scores_populated(self):
        for c in self.results:
            with self.subTest(subset=c.subset, distribution=c.distribution):
                self.assertTrue(np.isfinite(c.score))
                self.assertGreaterEqual(len(c.fold_scores), 2)
                self.assertTrue(all(np.isfinite(s) for s in c.fold_scores))

    def test_rss_method_produces_single_none_distribution_per_subset(self):
        results = search_best_subset_with_distribution(
            self.X, self.ctx, TRANSFORMS["raw"], "raw", self.small_features, SCORE_METHODS["rss"],
        )
        n_subsets = 2 ** len(self.small_features) - 1
        self.assertEqual(len(results), n_subsets)
        self.assertTrue(all(c.distribution is None for c in results))


class TestTransformedResidualClassifierMonotonicity(unittest.TestCase):
    """Fits a classifier directly (skipping the exhaustive search, which
    isn't needed to test this property) and checks that raising the dollar
    threshold never decreases the predicted survival probability, for both
    a transform with no extra column (raw) and one that needs it
    (log_ratio)."""

    def _fit_classifier(self, transform_name, seed=0):
        df, features = _build_synthetic_frame(seed=seed)
        X = df[features]
        transform = TRANSFORMS[transform_name]
        extra_col = transform["extra_column"]
        extra_arr = df[extra_col].to_numpy() if extra_col else None
        y_transformed = pd.Series(transform["forward"](df[TARGET_COLUMN].to_numpy(), extra_arr))
        model = LinearRegression().fit(X, y_transformed)
        residuals = y_transformed.to_numpy() - model.predict(X)
        dist = NormalResidualDistribution()
        dist.fit(residuals)
        classifier = TransformedResidualClassifier(model, features, transform, extra_col, dist)
        return classifier, df

    def test_monotonic_for_raw(self):
        classifier, df = self._fit_classifier("raw")
        row = df.iloc[[0]]
        credit = float(row[CREDIT_COLUMN].iloc[0])
        thresholds = [credit * m for m in (1.5, 3.0, 6.0, 15.0, 60.0)]
        prev = -np.inf
        for t in thresholds:
            p, _ = classifier(row, t)
            self.assertGreaterEqual(p[0], prev - 1e-9)
            prev = p[0]

    def test_monotonic_for_log_ratio(self):
        classifier, df = self._fit_classifier("log_ratio")
        row = df.iloc[[0]]
        credit = float(row[CREDIT_COLUMN].iloc[0])
        thresholds = [credit * m for m in (1.5, 3.0, 6.0, 15.0, 60.0)]
        prev = -np.inf
        for t in thresholds:
            p, _ = classifier(row, t)
            self.assertGreaterEqual(p[0], prev - 1e-9)
            prev = p[0]


if __name__ == "__main__":
    unittest.main()
