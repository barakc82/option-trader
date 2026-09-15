"""Feature construction. All features here are computable at decision time
from information available at trade entry, and none of them may reference
the stop level `k` -- that is enforced by a test that greps FEATURES for
`stop|k_|barrier_actual`.

`FEATURES` is the single authoritative ordering; it is persisted in the
model artifact and every backend consumes columns in exactly this order.

v1 scope note: the spec's "market context" feature group (vix1d,
realized_move_pct, minutes_since_open, open_range_pct) is not implemented --
none of those are computable from this repo's current data (no entry
timestamp, no intraday underlying path is logged). See rmax_model/README-ish
plan notes in the conversation this package was built from.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .pricer import bsm_price, solve_barrier_underlying
from .logging_setup import get_logger

logger = get_logger(__name__)

MINUTES_PER_YEAR = 60 * 24 * 365


def time_to_expiry_years(minutes_to_expiry: pd.Series) -> pd.Series:
    return minutes_to_expiry / MINUTES_PER_YEAR


def build_scale_free_features(df: pd.DataFrame) -> pd.DataFrame:
    """Scale-free option state at entry. `df` must already have
    underlying_entry, credit, iv_entry (sigma), minutes_to_expiry,
    distance_to_strike_pct, gamma, vega, theta, model_delta, bid_delta,
    ask_delta.
    """
    F = df["underlying_entry"]
    credit = df["credit"]
    sigma = df["iv_entry"]
    T = time_to_expiry_years(df["minutes_to_expiry"])

    out = pd.DataFrame(index=df.index)

    # Substitute for the spec's spread_ratio = (ask_entry - bid_entry) /
    # credit: this repo does not log dollar NBBO at entry, only IB's own
    # model-implied delta at the bid and ask ticks (bidGreeks.delta /
    # askGreeks.delta, see utilities/ib_utils.py:119-124), which we cannot
    # invert to a dollar price ourselves (opaque IB model). This is a
    # scale-free proxy for quote uncertainty in delta units, not the literal
    # spec formula.
    out["spread_delta_width"] = df["ask_delta"] - df["bid_delta"]

    # distance_to_strike_pct is already signed OTM-positive for both call
    # and put (utilities/ib_utils.py:260-266), matching the spec's intended
    # sign convention for moneyness_sd's numerator exactly.
    out["moneyness_sd"] = (df["distance_to_strike_pct"] / 100.0) / (sigma * np.sqrt(T))

    out["gamma_n"] = 0.5 * df["gamma"] * (0.01 * F) ** 2 / credit
    out["vega_n"] = df["vega"] * 0.01 / credit
    out["theta_n"] = df["theta"] * (1.0 / 1440.0) / credit
    out["delta_n"] = df["model_delta"] * (0.01 * F) / credit

    out["log_credit"] = np.log(credit)
    out["log_minutes_to_expiry"] = np.log(df["minutes_to_expiry"])
    out["sigma"] = sigma

    return out


def _barrier_row(row, r: float) -> tuple[float, float]:
    F = row["underlying_entry"]
    sigma = row["iv_entry"]
    T = row["_T"]
    target_price = r * row["credit"]
    F_star = solve_barrier_underlying(
        right=row["option_type"], strike=row["strike"], sigma=sigma, T=T,
        target_price=target_price, current_F=F,
    )
    if not math.isfinite(F_star):
        return math.nan, math.nan
    barrier_sd = abs(F_star - F) / (F * sigma * math.sqrt(T))
    from scipy.stats import norm
    touch_prob = 2.0 * norm.cdf(-barrier_sd)
    return barrier_sd, touch_prob


def build_physics_prior_features(df: pd.DataFrame, reference_multiples: tuple[float, ...]) -> pd.DataFrame:
    """Inverted-pricer features: for each fixed reference multiple r, the
    number of underlying standard deviations to the level F*_r at which the
    option's theoretical BSM value equals r * credit, and the reflection-
    principle touch probability implied by that distance. r is a fixed
    constant from config, never the stop -- these features stay k-free.
    """
    work = df.copy()
    work["_T"] = time_to_expiry_years(work["minutes_to_expiry"])

    out = pd.DataFrame(index=df.index)
    for r in reference_multiples:
        suffix = f"{r:g}".replace(".", "p")
        results = work.apply(lambda row, r=r: _barrier_row(row, r), axis=1)
        barrier_col = f"barrier_sd_{suffix}"
        touch_col = f"touch_prob_{suffix}"
        out[barrier_col] = [t[0] for t in results]
        out[touch_col] = [t[1] for t in results]
        n_nan = out[barrier_col].isna().sum()
        if n_nan:
            logger.info(f"barrier_sd_{suffix}/touch_prob_{suffix}: {n_nan}/{len(out)} rows unsolvable (target price unreachable)")
    return out


def build_features(df: pd.DataFrame, reference_multiples: tuple[float, ...]) -> pd.DataFrame:
    scale_free = build_scale_free_features(df)
    physics = build_physics_prior_features(df, reference_multiples)
    return pd.concat([scale_free, physics], axis=1)


def feature_list(reference_multiples: tuple[float, ...]) -> list[str]:
    """The authoritative feature ordering. Must match the column order
    build_features() actually produces."""
    names = [
        "spread_delta_width", "moneyness_sd", "gamma_n", "vega_n", "theta_n",
        "delta_n", "log_credit", "log_minutes_to_expiry", "sigma",
    ]
    for r in reference_multiples:
        suffix = f"{r:g}".replace(".", "p")
        names.append(f"barrier_sd_{suffix}")
        names.append(f"touch_prob_{suffix}")
    return names


FEATURES = feature_list((1.5, 2.0, 3.0, 5.0))
