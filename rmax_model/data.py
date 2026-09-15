"""Load, validate, and label the rmax_model training frame.

Source of truth for the derivation of every field below is documented in the
module docstring of each function -- this is a thin, auditable layer over the
raw CSV, not a place to silently repair or impute anything.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .config import Config
from .logging_setup import get_logger

logger = get_logger(__name__)

# Columns required (non-null) for the v1 feature set. Rows missing any of
# these are dropped with a per-column count logged -- never imputed.
REQUIRED_RAW_COLUMNS = [
    "right", "strike", "expiration", "estimated_sell_price", "max_ask",
    "gamma", "vega", "theta", "minutes_to_expiration", "atm_iv",
    "distance_to_strike_pct", "bid_delta", "ask_delta", "model_delta",
]


@dataclass
class DataReport:
    raw_row_count: int
    executed_row_count: int
    dropped_not_executed: int
    dropped_underlying_reconstruction: int
    final_row_count: int
    dropped_null_by_column: dict = field(default_factory=dict)
    dropped_validation_gate: dict = field(default_factory=dict)
    rows_per_session_date: pd.Series = None
    distinct_session_dates: int = 0
    rows_per_side: dict = field(default_factory=dict)


def _reconstruct_underlying(strike: pd.Series, right: pd.Series, distance_to_strike_pct: pd.Series) -> pd.Series:
    """Invert utilities/ib_utils.py::calculate_distance_to_strike_pct, which
    stores, per row:
        distance_to_strike_pct = (strike - F) / F * 100   for calls
        distance_to_strike_pct = (F - strike) / F * 100   for puts
    Solving for F:
        F = strike / (1 + pct/100)   for calls
        F = strike / (1 - pct/100)   for puts
    This is an exact closed-form inversion (to float precision) of an
    existing repo column -- no new data capture required.
    """
    pct = distance_to_strike_pct / 100.0
    denom = np.where(right.to_numpy() == "C", 1.0 + pct.to_numpy(), 1.0 - pct.to_numpy())
    with np.errstate(divide="ignore", invalid="ignore"):
        F = strike.to_numpy() / denom
    F = np.where((denom == 0) | ~np.isfinite(F) | (F <= 0), np.nan, F)
    return pd.Series(F, index=strike.index)


def load_dataset(config: Config) -> tuple[pd.DataFrame, DataReport]:
    csv_path = config.data.resolved_csv_path
    logger.info(f"Loading trades dataset from {csv_path}")
    raw = pd.read_csv(csv_path)
    raw_row_count = len(raw)
    logger.info(f"Resolved path: {csv_path}, row count: {raw_row_count}")

    missing_cols = [c for c in REQUIRED_RAW_COLUMNS + ["is_executed"] if c not in raw.columns]
    if missing_cols:
        raise ValueError(
            f"Input CSV is missing expected columns: {missing_cols}. "
            f"Columns actually present: {list(raw.columns)}"
        )

    df = raw.copy()
    dropped_not_executed = 0
    if config.data.require_executed:
        executed_mask = df["is_executed"] == 1
        dropped_not_executed = int((~executed_mask).sum())
        df = df[executed_mask].copy()
    executed_row_count = len(df)
    logger.info(f"Rows with is_executed==1: {executed_row_count} (dropped {dropped_not_executed})")

    dropped_null_by_column = {}
    for col in REQUIRED_RAW_COLUMNS:
        null_mask = df[col].isna()
        n_null = int(null_mask.sum())
        if n_null:
            dropped_null_by_column[col] = n_null
            logger.warning(f"Dropping {n_null} rows with null '{col}'")
            df = df[~null_mask].copy()

    df = df.rename(columns={"right": "option_type", "estimated_sell_price": "credit",
                             "minutes_to_expiration": "minutes_to_expiry", "atm_iv": "iv_entry",
                             "expiration": "session_date"})
    df["trade_id"] = np.arange(len(df))

    df["underlying_entry"] = _reconstruct_underlying(df["strike"], df["option_type"], df["distance_to_strike_pct"])
    dropped_underlying_reconstruction = int(df["underlying_entry"].isna().sum())
    if dropped_underlying_reconstruction:
        logger.warning(
            f"Dropping {dropped_underlying_reconstruction} rows where underlying_entry "
            f"could not be reconstructed from strike/distance_to_strike_pct"
        )
        df = df[df["underlying_entry"].notna()].copy()

    df["R_max"] = df["max_ask"] / df["credit"]
    df["log_R_max"] = np.log(df["R_max"])

    dropped_validation_gate = {}

    def _drop(mask_bad: pd.Series, reason: str):
        nonlocal df
        n_bad = int(mask_bad.sum())
        if n_bad:
            dropped_validation_gate[reason] = n_bad
            logger.warning(f"Validation gate: dropping {n_bad} rows failing '{reason}'")
            df = df[~mask_bad].copy()

    _drop(~(df["credit"] > 0), "credit > 0")
    _drop(~(df["max_ask"] >= 0), "max_ask >= 0")
    _drop(~np.isfinite(df["R_max"]), "R_max finite")

    if df["trade_id"].duplicated().any():
        raise AssertionError("Duplicate trade_id after synthesis -- should be impossible.")

    logger.warning(
        "No ask_entry column exists in the source data (only bid_delta/ask_delta, IB's own "
        "model-implied deltas at bid/ask, not dollar NBBO), so the spec's "
        "'max_ask >= ask_entry' and 'R_max >= ask_entry/credit' checks cannot be run. "
        "This is a data-capture gap, not a silent skip."
    )

    final_row_count = len(df)
    rows_per_session_date = df.groupby("session_date").size()
    rows_per_side = df.groupby("option_type").size().to_dict()

    report = DataReport(
        raw_row_count=raw_row_count,
        executed_row_count=executed_row_count,
        dropped_not_executed=dropped_not_executed,
        dropped_null_by_column=dropped_null_by_column,
        dropped_underlying_reconstruction=dropped_underlying_reconstruction,
        dropped_validation_gate=dropped_validation_gate,
        final_row_count=final_row_count,
        rows_per_session_date=rows_per_session_date,
        distinct_session_dates=int(df["session_date"].nunique()),
        rows_per_side=rows_per_side,
    )

    logger.info(f"Final row count: {final_row_count} across {report.distinct_session_dates} session dates")
    logger.info(f"Rows per session date:\n{rows_per_session_date}")
    logger.info(f"Rows per side: {rows_per_side}")

    if report.distinct_session_dates < 30:
        logger.warning(
            f"Only {report.distinct_session_dates} distinct session dates in the training set. "
            f"GroupKFold-by-date, walk-forward CV, and per-k isotonic calibration all assume a "
            f"much larger sample of trading days than this. Treat every downstream metric as "
            f"provisional until more sessions accumulate."
        )

    return df.reset_index(drop=True), report
