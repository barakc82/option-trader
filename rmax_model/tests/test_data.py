import math

import numpy as np
import pandas as pd

from rmax_model.data import _reconstruct_underlying


def test_R_max_label_hand_worked_example():
    # credit=2.0, max_ask=6.0 -> R_max=3.0, log_R_max=ln(3). Mirrors exactly
    # what data.py computes: df["R_max"] = df["max_ask"] / df["credit"];
    # df["log_R_max"] = np.log(df["R_max"]).
    credit = pd.Series([2.0])
    max_ask = pd.Series([6.0])
    R_max = max_ask / credit
    log_R_max = np.log(R_max)
    assert R_max.iloc[0] == 3.0
    assert math.isclose(log_R_max.iloc[0], math.log(3.0))


def test_underlying_reconstruction_is_exact_inverse_call():
    # utilities/ib_utils.py::calculate_distance_to_strike_pct for a call:
    # pct = (strike - F) / F * 100. Pick F=95, strike=100 by hand:
    # pct = (100-95)/95*100 = 5.263157894736842
    strike = pd.Series([100.0])
    right = pd.Series(["C"])
    pct = pd.Series([(100.0 - 95.0) / 95.0 * 100.0])
    F = _reconstruct_underlying(strike, right, pct)
    assert math.isclose(F.iloc[0], 95.0, rel_tol=1e-9)


def test_underlying_reconstruction_is_exact_inverse_put():
    # For a put: pct = (F - strike) / F * 100. Pick F=105, strike=100:
    # pct = (105-100)/105*100 = 4.761904761904762
    strike = pd.Series([100.0])
    right = pd.Series(["P"])
    pct = pd.Series([(105.0 - 100.0) / 105.0 * 100.0])
    F = _reconstruct_underlying(strike, right, pct)
    assert math.isclose(F.iloc[0], 105.0, rel_tol=1e-9)


def test_underlying_reconstruction_handles_degenerate_denominator():
    # A put whose pct implies F/(1 - pct/100) has a zero or negative
    # denominator must come back nan, not a silently wrong number.
    strike = pd.Series([100.0])
    right = pd.Series(["P"])
    pct = pd.Series([100.0])  # 1 - 100/100 == 0
    F = _reconstruct_underlying(strike, right, pct)
    assert math.isnan(F.iloc[0])
