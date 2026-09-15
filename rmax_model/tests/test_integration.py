"""Wiring smoke test: data.py -> features.py -> binning.py on a tiny
hand-built CSV matching the real schema, to catch cross-module regressions
that unit tests of each module in isolation would miss."""
import pandas as pd

from rmax_model.binning import assign_bin, ensure_candidate_k_in_edges
from rmax_model.config import DataConfig, load_config
from rmax_model.data import load_dataset
from rmax_model.features import build_features, feature_list


def _tiny_csv_frame(n=12) -> pd.DataFrame:
    rows = []
    for i in range(n):
        right = "C" if i % 2 == 0 else "P"
        strike = 100.0 + i
        distance_to_strike_pct = 3.0 + (i % 4)
        rows.append({
            "datetime": f"2026-08-{5 + i % 5:02d}T15:00:00",
            "is_executed": 1,
            "right": right,
            "strike": strike,
            "expiration": 20260805 + (i % 5),
            "estimated_sell_price": 2.0 + 0.1 * i,
            "target_delta": 0.15,
            "bid_delta": 0.14,
            "ask_delta": 0.16,
            "last_delta": 0.15,
            "model_delta": 0.15,
            "gamma": 0.01,
            "vega": 0.02,
            "theta": -0.001,
            "minutes_to_expiration": 60 + i,
            "atm_iv": 0.2,
            "distance_to_strike_pct": distance_to_strike_pct,
            "max_ask": (2.0 + 0.1 * i) * (1.2 + 0.05 * i),
            "max_delta": 0.2,
        })
    return pd.DataFrame(rows)


def test_data_features_binning_wiring(tmp_path):
    csv_path = tmp_path / "options_data.csv"
    _tiny_csv_frame().to_csv(csv_path, index=False)

    config = load_config()
    # DataConfig.resolved_csv_path does REPO_ROOT / csv_path; pathlib's `/`
    # returns the right-hand side unchanged when it's already absolute, so
    # pointing csv_path at the tmp_path CSV directly works regardless of
    # where REPO_ROOT resolves to.
    config = config.__class__(**{**config.__dict__, "data": DataConfig(csv_path=str(csv_path))})

    df, report = load_dataset(config)
    assert report.final_row_count == 12
    assert report.distinct_session_dates == 5

    feats = build_features(df, config.physics_prior.reference_multiples)
    names = feature_list(config.physics_prior.reference_multiples)
    assert list(feats.columns) == names
    assert not feats.isna().any().any()

    edges = ensure_candidate_k_in_edges(config.r_edges, config.candidate_k)
    bin_idx = assign_bin(df["R_max"], edges)
    assert (bin_idx >= 0).all()
    assert (bin_idx < len(edges) - 1).all()
