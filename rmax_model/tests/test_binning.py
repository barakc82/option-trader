import numpy as np
import pandas as pd
import pytest

from rmax_model.binning import assign_bin, ensure_candidate_k_in_edges, survival

EDGES = (1.0, 1.2, 1.5, 2.0, 2.5, 3.0, 3.25, 3.5, 4.0, 4.5, 5.0, 7.0, 10.0, float("inf"))


def test_ensure_candidate_k_in_edges_all_present():
    edges = ensure_candidate_k_in_edges(EDGES, [1.5, 2.0, 3.0, 3.25, 3.5, 4.0, 5.0])
    assert edges == EDGES


def test_ensure_candidate_k_in_edges_inserts_missing():
    edges = ensure_candidate_k_in_edges(EDGES, [2.75])
    assert 2.75 in edges
    assert list(edges) == sorted(edges)


def test_bin_assignment_at_exact_edge_values():
    # Each edge value R must fall into the bin that STARTS at that edge
    # (bin i covers [edges[i], edges[i+1])), never the bin below it.
    R = pd.Series(list(EDGES[:-1]))  # every edge except the open-ended inf
    idx = assign_bin(R, EDGES)
    assert list(idx) == list(range(len(EDGES) - 1))


def test_bin_assignment_just_below_edge_falls_in_lower_bin():
    R = pd.Series([1.999999])
    idx = assign_bin(R, EDGES)
    assert idx[0] == EDGES.index(1.5)  # falls in [1.5, 2.0), not [2.0, 2.5)


def test_bin_assignment_rejects_below_first_edge():
    R = pd.Series([0.5])
    with pytest.raises(ValueError):
        assign_bin(R, EDGES)


def test_survival_monotone_non_increasing_in_k_random_pmf():
    rng = np.random.default_rng(0)
    n_bins = len(EDGES) - 1
    pmf = rng.dirichlet(np.ones(n_bins), size=50)
    candidate_k = [1.5, 2.0, 3.0, 3.25, 3.5, 4.0, 5.0]
    prev = np.ones(pmf.shape[0])
    for k in candidate_k:
        s = survival(pmf, EDGES, k)
        assert np.all(s <= prev + 1e-9)
        assert np.all((s >= -1e-9) & (s <= 1 + 1e-9))
        prev = s


def test_survival_at_first_edge_is_near_one():
    n_bins = len(EDGES) - 1
    pmf = np.ones((3, n_bins)) / n_bins
    s = survival(pmf, EDGES, EDGES[0])
    assert np.allclose(s, 1.0)


def test_survival_rejects_k_not_on_edge():
    n_bins = len(EDGES) - 1
    pmf = np.ones((3, n_bins)) / n_bins
    with pytest.raises(ValueError):
        survival(pmf, EDGES, 2.75)
