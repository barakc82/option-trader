import numpy as np
import pandas as pd
import pytest

import rmax_model.backends  # noqa: F401  (registers linreg + binned_softmax)
from rmax_model.backends.base import SideModel, registered_backend_names, get_backend_class
from rmax_model.config import LgbParams
from rmax_model.features import FEATURES

R_EDGES = (1.0, 1.2, 1.5, 2.0, 2.5, 3.0, 3.25, 3.5, 4.0, 4.5, 5.0, 7.0, 10.0, float("inf"))
CANDIDATE_K = [1.5, 2.0, 3.0]

# Raw columns backends/linreg.py needs (via its rename map back to
# CANDIDATE_FEATURE_COLUMNS in app/machine_learning/regress_max_ask.py).
RAW_COLUMNS = [
    "credit", "target_delta", "bid_delta", "ask_delta", "last_delta", "model_delta",
    "max_delta", "gamma", "vega", "theta", "minutes_to_expiry", "iv_entry", "distance_to_strike_pct",
]


def make_synthetic_frame(seed: int = 0, n_per_date: int = 12, n_dates: int = 6) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    n = n_per_date * n_dates
    session_date = np.repeat(np.arange(20260101, 20260101 + n_dates), n_per_date)

    data = {"session_date": session_date}
    for col in RAW_COLUMNS:
        data[col] = rng.uniform(0.05, 1.0, n)
    data["credit"] = rng.uniform(1.0, 3.0, n)
    data["minutes_to_expiry"] = rng.uniform(10, 300, n)
    data["distance_to_strike_pct"] = rng.uniform(1.0, 10.0, n)
    for col in FEATURES:
        data[col] = rng.normal(0, 1, n)

    df = pd.DataFrame(data)
    df["R_max"] = rng.uniform(1.0, 6.0, n)
    return df


@pytest.fixture(scope="module")
def synthetic_frame() -> pd.DataFrame:
    return make_synthetic_frame()


@pytest.mark.parametrize("backend_name", registered_backend_names())
def test_backend_satisfies_side_model_protocol(backend_name, synthetic_frame):
    cls = get_backend_class(backend_name)
    lgb_params = LgbParams(num_boost_round=20, early_stopping_rounds=5, min_child_samples=3, num_leaves=7)
    model = cls.create(R_EDGES, FEATURES, lgb_params)
    assert isinstance(model, SideModel)

    X = synthetic_frame
    y = X["R_max"]
    groups = X["session_date"]
    model.fit(X, y, groups)

    prev = np.ones(len(X))
    for k in CANDIDATE_K:
        surv = model.predict_survival(X, k)
        assert surv.shape == (len(X),)
        finite = np.isfinite(surv)
        assert np.all((surv[finite] >= -1e-9) & (surv[finite] <= 1 + 1e-9)), f"{backend_name}: survival out of [0,1] at k={k}"
        assert np.all(surv[finite] <= prev[finite] + 1e-6), f"{backend_name}: survival not non-increasing in k at k={k}"
        prev = surv

    for k in CANDIDATE_K:
        el = model.predict_expected_loss_multiple(X, k)
        assert el.shape == (len(X),)
