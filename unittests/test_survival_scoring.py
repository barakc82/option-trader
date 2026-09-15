"""Tests for the reusable building blocks in
app/machine_learning/survival_scoring.py: the shared survival label,
probability clipping, logloss, and the two residual distributions. All fast
-- no exhaustive search happens here; that's covered by
test_best_subset_scoring.py.
"""
import unittest

import numpy as np
import pandas as pd

from app.machine_learning.survival_scoring import (
    EPS, STOP_COLUMN, TARGET_COLUMN,
    NormalResidualDistribution, EmpiricalResidualDistribution,
    clip_probabilities, compute_survival_label, logloss,
)


class TestSurvivalLabel(unittest.TestCase):
    def test_label_hand_worked_example(self):
        ctx = pd.DataFrame({TARGET_COLUMN: [1.0, 5.0, 3.0], STOP_COLUMN: [2.0, 2.0, 3.0]})
        label = compute_survival_label(ctx)
        # 1.0 < 2.0 -> survived; 5.0 < 2.0 -> not; 3.0 < 3.0 -> not (strict <)
        np.testing.assert_array_equal(label, np.array([1.0, 0.0, 0.0]))

    def test_label_matches_direct_comparison(self):
        ctx = pd.DataFrame({
            TARGET_COLUMN: [0.5, 1.5, 2.5, 3.5],
            STOP_COLUMN: [1.0, 1.0, 3.0, 3.0],
        })
        label = compute_survival_label(ctx)
        expected = (ctx[TARGET_COLUMN].to_numpy() < ctx[STOP_COLUMN].to_numpy()).astype(float)
        np.testing.assert_array_equal(label, expected)

    def test_repeated_calls_are_bit_identical(self):
        """The label is computed once, from one function -- confirmed here
        by checking two independent calls on the same ctx agree exactly,
        the property that lets every target transform / residual
        distribution combination be scored against the identical label."""
        ctx = pd.DataFrame({TARGET_COLUMN: [1.0, 2.0, 3.0], STOP_COLUMN: [2.0, 2.0, 2.0]})
        first = compute_survival_label(ctx)
        second = compute_survival_label(ctx)
        np.testing.assert_array_equal(first, second)


class TestClippingAndLogloss(unittest.TestCase):
    def test_clip_registers_and_logloss_is_finite_not_inf(self):
        p_clipped, clip_rate = clip_probabilities(np.array([0.0]))
        self.assertEqual(clip_rate, 1.0)
        self.assertEqual(p_clipped[0], EPS)

        ll = logloss(np.array([0.0]), np.array([1.0]))
        self.assertTrue(np.isfinite(ll))
        self.assertAlmostEqual(ll, -np.log(EPS))

    def test_clip_rate_zero_when_nothing_out_of_range(self):
        p_clipped, clip_rate = clip_probabilities(np.array([0.2, 0.5, 0.8]))
        self.assertEqual(clip_rate, 0.0)
        np.testing.assert_array_equal(p_clipped, np.array([0.2, 0.5, 0.8]))


class TestResidualDistributions(unittest.TestCase):
    def test_normal_survival_prob_at_median_is_half(self):
        nrd = NormalResidualDistribution()
        nrd.fit(np.array([-1.0, -0.5, 0.0, 0.5, 1.0]))
        p = nrd.survival_prob(np.array([0.0]))
        self.assertAlmostEqual(p[0], 0.5)

    def test_normal_survival_prob_monotonic_in_threshold(self):
        nrd = NormalResidualDistribution()
        nrd.fit(np.array([-2.0, -1.0, 0.0, 1.0, 2.0]))
        thresholds = np.linspace(-10, 10, 50)
        p = nrd.survival_prob(thresholds)
        self.assertTrue(np.all(np.diff(p) >= -1e-12))

    def test_empirical_laplace_smoothing_never_returns_exactly_zero_or_one(self):
        erd = EmpiricalResidualDistribution(smoothing="laplace")
        erd.fit(np.array([-1.0, -0.5, 0.0, 0.5, 1.0]))
        rng = np.random.default_rng(1)
        extreme = rng.uniform(-1000, 1000, 500)
        p = erd.survival_prob(extreme)
        self.assertTrue(np.all(p > 0.0))
        self.assertTrue(np.all(p < 1.0))

    def test_empirical_without_smoothing_can_hit_exact_bounds(self):
        """Sanity check for the contrast: without smoothing, the ECDF DOES
        return exact 0/1 beyond the residual pool -- this is the failure
        mode Laplace smoothing exists to avoid."""
        erd = EmpiricalResidualDistribution(smoothing="none")
        erd.fit(np.array([-1.0, -0.5, 0.0, 0.5, 1.0]))
        p_low = erd.survival_prob(np.array([-1000.0]))
        p_high = erd.survival_prob(np.array([1000.0]))
        self.assertEqual(p_low[0], 0.0)
        self.assertEqual(p_high[0], 1.0)

    def test_empirical_survival_prob_monotonic_in_threshold(self):
        erd = EmpiricalResidualDistribution(smoothing="laplace")
        erd.fit(np.array([-2.0, -1.0, 0.0, 1.0, 2.0]))
        thresholds = np.linspace(-10, 10, 50)
        p = erd.survival_prob(thresholds)
        self.assertTrue(np.all(np.diff(p) >= -1e-12))


if __name__ == "__main__":
    unittest.main()
