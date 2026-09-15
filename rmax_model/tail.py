"""Optional generalized Pareto tail splice for the open top bin. A running
maximum -- max(ask) from fill to expiry -- is exactly the object extreme
value theory addresses, so fitting a GPD to peaks-over-threshold excesses of
log(R_max) is principled rather than a patch on the binned pmf's crude
bin-midpoint estimate.

This module is deliberately independent of any specific backend: it is fit
once per side on training-fold log_R_max values and can be used by any
backend's expected_loss_multiple as a refinement for k values that fall at
or above the fitted threshold. Below that threshold, or if the tail sample
is too thin to identify the shape parameter, callers should fall back to the
binned empirical mean (see backends/binned_softmax.py::top_bin_empirical_mean).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy.stats import genpareto

from .logging_setup import get_logger

logger = get_logger(__name__)


@dataclass
class GpdTailFit:
    fitted: bool
    threshold_log_r: float
    shape: float | None
    scale: float | None
    shape_se: float | None
    n_exceedances: int
    fallback_reason: str | None


def fit_gpd_tail(log_r_max: np.ndarray, threshold_quantile: float, min_exceedances: int,
                  seed: int, n_bootstrap: int = 200) -> GpdTailFit:
    threshold = float(np.quantile(log_r_max, threshold_quantile))
    exceedances = log_r_max[log_r_max > threshold] - threshold
    n_exc = len(exceedances)
    if n_exc < min_exceedances:
        reason = (
            f"only {n_exc} exceedances above the {threshold_quantile:.0%} quantile threshold "
            f"(log R = {threshold:.3f}), below min_exceedances={min_exceedances}"
        )
        logger.info(f"fit_gpd_tail: not fitting -- {reason}")
        return GpdTailFit(False, threshold, None, None, None, n_exc, reason)

    shape, _loc, scale = genpareto.fit(exceedances, floc=0)

    rng = np.random.default_rng(seed)
    boot_shapes = []
    for _ in range(n_bootstrap):
        resample = rng.choice(exceedances, size=n_exc, replace=True)
        try:
            s, _, _ = genpareto.fit(resample, floc=0)
            boot_shapes.append(s)
        except Exception:
            continue
    shape_se = float(np.std(boot_shapes)) if boot_shapes else float("nan")

    logger.info(f"fit_gpd_tail: shape={shape:.4f} (SE={shape_se:.4f}), scale={scale:.4f}, n_exceedances={n_exc}")
    if shape >= 1:
        logger.warning(f"fit_gpd_tail: shape={shape:.4f} >= 1 implies an infinite-mean tail beyond the threshold")

    return GpdTailFit(True, threshold, float(shape), float(scale), shape_se, n_exc, None)


def expected_value_beyond(tail_fit: GpdTailFit, k_log: float, seed: int,
                           n_samples: int = 20_000, empirical_fallback: float | None = None) -> float:
    """E[R_max | log R_max >= k_log], via Monte Carlo from the fitted GPD
    re-parameterized at k (exceedance stability: given X > threshold, the
    excess of X over a higher level k is GPD(shape, scale + shape*(k -
    threshold))). Falls back to empirical_fallback (a plain R-space mean,
    not log) if the tail wasn't fit or k is below the fitted threshold."""
    if not tail_fit.fitted or k_log < tail_fit.threshold_log_r:
        return empirical_fallback if empirical_fallback is not None else float("nan")

    xi, sigma = tail_fit.shape, tail_fit.scale
    scale_at_k = sigma + xi * (k_log - tail_fit.threshold_log_r)
    if scale_at_k <= 0:
        return empirical_fallback if empirical_fallback is not None else float("nan")
    if xi >= 1:
        return float("inf")

    rng = np.random.default_rng(seed)
    samples = genpareto.rvs(xi, loc=0, scale=scale_at_k, size=n_samples, random_state=rng)
    r_samples = np.exp(k_log + samples)
    return float(r_samples.mean())
