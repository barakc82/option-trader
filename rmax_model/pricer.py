"""Black-76 (forward-based) pricer for 0-DTE SPX options, plus a Brent solve
that inverts the pricer for the underlying level at which a given option's
theoretical value equals a target multiple of its entry credit.

Black-76 rather than spot-based Black-Scholes because the underlying proxy
this repo uses is an ES futures price (`F`), which is already a forward
price. Interest is dropped (discount factor = 1): with minutes-to-expiry
measured in the tens to low hundreds, r*T is negligible for 0-DTE SPX.

Nothing here is fit from data -- these are closed-form / root-finding
functions of (F, K, sigma, T, right) only.
"""
from __future__ import annotations

import math

import numpy as np
from scipy.optimize import brentq
from scipy.stats import norm


def _d1_d2(F: float, K: float, sigma: float, T: float) -> tuple[float, float]:
    vol_sqrt_t = sigma * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * T) / vol_sqrt_t
    d2 = d1 - vol_sqrt_t
    return d1, d2


def bsm_price(F: float, K: float, sigma: float, T: float, right: str) -> float:
    """Undiscounted Black-76 theoretical price of a European option on a
    futures-like underlying F, struck at K, with IV sigma and time-to-expiry
    T in years. `right` is 'C' or 'P'."""
    if F <= 0 or K <= 0 or sigma <= 0 or T <= 0:
        return math.nan
    d1, d2 = _d1_d2(F, K, sigma, T)
    if right == "C":
        return F * norm.cdf(d1) - K * norm.cdf(d2)
    elif right == "P":
        return K * norm.cdf(-d2) - F * norm.cdf(-d1)
    raise ValueError(f"right must be 'C' or 'P', got {right!r}")


def bsm_delta(F: float, K: float, sigma: float, T: float, right: str) -> float:
    if F <= 0 or K <= 0 or sigma <= 0 or T <= 0:
        return math.nan
    d1, _ = _d1_d2(F, K, sigma, T)
    if right == "C":
        return norm.cdf(d1)
    elif right == "P":
        return norm.cdf(d1) - 1.0
    raise ValueError(f"right must be 'C' or 'P', got {right!r}")


def solve_barrier_underlying(
    right: str, strike: float, sigma: float, T: float, target_price: float, current_F: float
) -> float:
    """Solve for the underlying level F* at which bsm_price(F*, strike, sigma,
    T, right) == target_price, holding strike/sigma/T fixed at their entry
    values. Returns nan if the target is unreachable (e.g. target_price >=
    strike for a put, whose price is bounded above by strike) or if inputs
    are degenerate.

    Price is monotonic in F for a fixed right (increasing for calls,
    decreasing for puts), so a single bracket expansion around current_F is
    sufficient for brentq.
    """
    if strike <= 0 or sigma <= 0 or T <= 0 or current_F <= 0 or not math.isfinite(target_price):
        return math.nan
    if target_price <= 0:
        return math.nan
    if right == "P" and target_price >= strike:
        # A put's price is bounded above by its strike (undiscounted); a
        # target at or beyond that is unreachable at any positive F.
        return math.nan

    def objective(F: float) -> float:
        return bsm_price(F, strike, sigma, T, right) - target_price

    lo, hi = current_F * 0.5, current_F * 1.5
    f_lo, f_hi = objective(lo), objective(hi)
    expansions = 0
    while f_lo * f_hi > 0 and expansions < 40:
        lo *= 0.5
        hi *= 1.5
        if lo < 1e-6:
            lo = 1e-6
        f_lo, f_hi = objective(lo), objective(hi)
        expansions += 1
        if hi > current_F * 1e6:
            break
    if f_lo * f_hi > 0:
        return math.nan
    try:
        return brentq(objective, lo, hi, xtol=1e-6, rtol=1e-10, maxiter=200)
    except (ValueError, RuntimeError):
        return math.nan
