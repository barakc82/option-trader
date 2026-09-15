import numpy as np
import pytest

from rmax_model.backtest import (
    block_bootstrap_total_and_mean, draw_stop_multiples, interpolate_survival_binned, realized_and_decision,
)

EDGES = (1.0, 1.2, 1.5, 2.0, 2.5, 3.0, 3.25, 3.5, 4.0, 4.5, 5.0, 7.0, 10.0, float("inf"))


def test_loss_arithmetic_is_buyback_minus_credit_not_k_times_credit():
    # C=1.00, k=3.0, stopped out -> -2.00 (not -3.00).
    p = np.array([0.0])  # forces taken=True so we can read `realized` directly
    R = np.array([10.0])  # R >= k, i.e. stopped out
    k = np.array([3.0])
    C = np.array([1.0])
    out = realized_and_decision(p, R, k, C, fees=0.0, ev_threshold=0.0, slip=np.ones(1))
    assert out["taken"][0]
    assert out["realized"][0] == pytest.approx(-2.00)
    assert out["loss"][0] == pytest.approx(2.00)


def test_ev_hat_positive_iff_p_below_one_over_k_when_fees_zero_slippage_one():
    rng = np.random.default_rng(0)
    n = 300
    p = rng.uniform(0, 1, n)
    k = rng.uniform(1.5, 6.0, n)
    R = rng.uniform(0.5, 8.0, n)
    C = rng.uniform(0.5, 3.0, n)
    out = realized_and_decision(p, R, k, C, fees=0.0, ev_threshold=0.0, slip=np.ones(n))
    assert np.array_equal(out["ev_hat"] > 0.0, p < 1.0 / k)
    assert np.array_equal(out["taken"], p < 1.0 / k)


def test_overconfidence_canary_zero_prediction_matches_take_all_exactly():
    rng = np.random.default_rng(1)
    n = 200
    R = rng.uniform(0.5, 8.0, n)
    k = rng.uniform(1.5, 6.0, n)
    C = rng.uniform(0.5, 3.0, n)
    slip = np.ones(n)

    zero_pred = realized_and_decision(np.zeros(n), R, k, C, fees=0.0, ev_threshold=0.0, slip=slip)
    # take-all: always taken, realized purely from truth (R, k, C) -- no model at all.
    gain = C
    loss = (k - 1) * C
    take_all_realized = np.where(R >= k, -loss, gain)

    assert np.all(zero_pred["taken"])
    assert np.allclose(zero_pred["realized"], take_all_realized)


def test_overconfidence_canary_one_prediction_matches_zero_exactly():
    rng = np.random.default_rng(2)
    n = 200
    R = rng.uniform(0.5, 8.0, n)
    k = rng.uniform(1.5, 6.0, n)
    C = rng.uniform(0.5, 3.0, n)
    slip = np.ones(n)

    one_pred = realized_and_decision(np.ones(n), R, k, C, fees=0.0, ev_threshold=0.0, slip=slip)

    assert not np.any(one_pred["taken"])
    assert np.allclose(one_pred["realized"], 0.0)


def test_neither_extreme_prediction_beats_a_realistically_skillful_model():
    """The reason this metric is shaped the way it is: if it ever rewards an
    always-0 or always-1 predictor over one with real skill, the evaluation
    has drifted back to probability-weighted payoffs."""
    rng = np.random.default_rng(3)
    n = 400
    C = rng.uniform(1.0, 3.0, n)
    k = rng.uniform(2.0, 6.0, n)
    quality = rng.uniform(0, 1, n)
    p_true = (1 - quality) * 0.5  # a genuinely informative predictor of stop-out risk
    stopped = rng.uniform(0, 1, n) < p_true
    R = np.where(stopped, k * rng.uniform(1.01, 1.5, n), k * rng.uniform(0.3, 0.99, n))
    slip = np.ones(n)

    skillful = realized_and_decision(p_true, R, k, C, fees=0.0, ev_threshold=0.0, slip=slip)
    zero_pred = realized_and_decision(np.zeros(n), R, k, C, fees=0.0, ev_threshold=0.0, slip=slip)
    one_pred = realized_and_decision(np.ones(n), R, k, C, fees=0.0, ev_threshold=0.0, slip=slip)

    skillful_per_taken = skillful["realized"].sum() / skillful["taken"].sum()
    zero_per_taken = zero_pred["realized"].sum() / zero_pred["taken"].sum()  # == take-all's overall mean
    one_total = one_pred["realized"].sum()  # == 0.0 exactly

    assert skillful_per_taken > zero_per_taken
    assert skillful["realized"].sum() > one_total


def test_realized_never_reads_prediction_shuffle_degrades_to_uninformative():
    """Shuffling predictions across records (breaking their row alignment
    with truth) must make the selection statistically indistinguishable
    from a random subset of the same size -- because `realized` is a pure
    function of (R, k, C, fees, slip), never of p_hit. If `realized` ever
    starts reading p_hit, this test stops holding."""
    rng = np.random.default_rng(7)
    n = 500
    C = rng.uniform(1.0, 3.0, n)
    k = rng.uniform(2.0, 6.0, n)
    quality = rng.uniform(0, 1, n)
    p_true = (1 - quality) * 0.5
    stopped = rng.uniform(0, 1, n) < p_true
    R = np.where(stopped, k * rng.uniform(1.01, 1.5, n), k * rng.uniform(0.3, 0.99, n))
    slip = np.ones(n)

    take_all = realized_and_decision(np.zeros(n), R, k, C, fees=0.0, ev_threshold=0.0, slip=slip)
    population = take_all["realized"]
    pop_mean, pop_std = population.mean(), population.std()

    shuffled_p = rng.permutation(p_true)
    shuffled = realized_and_decision(shuffled_p, R, k, C, fees=0.0, ev_threshold=0.0, slip=slip)
    n_taken = shuffled["taken"].sum()
    assert n_taken > 10  # sanity: the draw actually produced a nontrivial subset

    shuffled_mean_per_taken = shuffled["realized"].sum() / n_taken
    se = pop_std / np.sqrt(n_taken)
    assert abs(shuffled_mean_per_taken - pop_mean) < 5 * se, (
        f"shuffled selection's mean payout ({shuffled_mean_per_taken:.4f}) should look like a random "
        f"draw from the population (mean={pop_mean:.4f}, SE={se:.4f}) once predictions are decoupled from truth"
    )

    # Positive control: the *unshuffled* skillful predictor must look nothing
    # like a random draw -- otherwise this test would pass even if `realized`
    # were broken to ignore R/k entirely.
    skillful = realized_and_decision(p_true, R, k, C, fees=0.0, ev_threshold=0.0, slip=slip)
    skillful_mean_per_taken = skillful["realized"].sum() / skillful["taken"].sum()
    assert abs(skillful_mean_per_taken - pop_mean) > 4 * se


def test_block_bootstrap_total_contains_the_point_estimate():
    """Regression guard: an earlier implementation resampled dates via a
    membership mask (np.isin) instead of an explicit index concatenation,
    silently collapsing a repeatedly-drawn date back to a single copy. That
    understated the total systematically enough that the point estimate
    (computed on the full, undeduplicated sample) could fall outside its own
    95% bootstrap interval -- which should essentially never happen for a
    well-formed bootstrap CI."""
    rng = np.random.default_rng(0)
    n = 45
    dates = rng.choice(["d1", "d2", "d3", "d4", "d5"], n)
    realized = rng.normal(0.2, 1.5, n)
    point_total = realized.sum()
    point_mean = realized.mean()
    boot = block_bootstrap_total_and_mean(realized, dates, n_resamples=2000, ci=0.95, seed=0)
    assert boot["total_lo"] <= point_total <= boot["total_hi"]
    assert boot["mean_lo"] <= point_mean <= boot["mean_hi"]


def test_block_bootstrap_duplicates_repeated_dates():
    """A date drawn twice in a resample must contribute its rows twice --
    directly check the resampled total can exceed what a deduplicated
    (mask-based) resample could ever produce."""
    dates = np.array(["a"] * 3 + ["b"] * 3)
    realized = np.array([10.0, 10.0, 10.0, 1.0, 1.0, 1.0])
    # A membership-mask resample of dates=['a','a'] would still only count
    # 'a' once (mask-based total capped at 30.0); true block bootstrap must
    # be able to double-count 'a' and reach up to 60.0.
    rng = np.random.default_rng(1)
    max_seen = 0.0
    for _ in range(500):
        sampled = rng.choice(["a", "b"], size=2, replace=True)
        idx = np.concatenate([np.flatnonzero(dates == d) for d in sampled])
        max_seen = max(max_seen, realized[idx].sum())
    assert max_seen > 30.0


def test_drawn_k_reproduces_exactly_under_fixed_seed():
    rng1 = np.random.default_rng(123)
    rng2 = np.random.default_rng(123)
    d1 = draw_stop_multiples(rng1, 50, 2.0, 6.0)
    d2 = draw_stop_multiples(rng2, 50, 2.0, 6.0)
    assert np.array_equal(d1, d2)
    assert d1.min() >= 2.0
    assert d1.max() < 6.0


def test_interpolate_survival_binned_exact_at_edges():
    n_bins = len(EDGES) - 1
    rng = np.random.default_rng(4)
    pmf = rng.dirichlet(np.ones(n_bins), size=5)
    for edge in [1.5, 2.0, 3.0, 3.25, 5.0]:
        k = np.full(5, edge)
        s_interp, gap = interpolate_survival_binned(pmf, EDGES, k)
        from rmax_model.binning import survival as exact_survival
        s_exact = exact_survival(pmf, EDGES, edge)
        assert np.allclose(s_interp, s_exact, atol=1e-9)
        assert np.allclose(gap, 0.0, atol=1e-9)


def test_interpolate_survival_binned_monotone_within_bin():
    n_bins = len(EDGES) - 1
    rng = np.random.default_rng(5)
    pmf = rng.dirichlet(np.ones(n_bins), size=1)
    pmf = np.repeat(pmf, 5, axis=0)
    ks = np.array([2.0, 2.15, 2.3, 2.4, 2.5])  # within [2.0, 2.5)
    s_interp, _ = interpolate_survival_binned(pmf, EDGES, ks)
    assert np.all(np.diff(s_interp) <= 1e-9)  # non-increasing as k increases
