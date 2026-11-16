import csv
import logging
import math
import os
import pickle
from datetime import datetime

import pandas as pd

from utilities.utils import get_option_name, is_early_closing_time, is_first_five_minutes_of_closing_time, \
    new_york_timezone

from .limit_adjuster import LimitAdjuster
from .predictions_evaluator import EARLY_TRADE_PROBABILITY_BUCKETS_CSV_PATH, PROBABILITY_BUCKETS_CSV_PATH, \
    VERY_EARLY_TRADE_PROBABILITY_BUCKETS_CSV_PATH

logger = logging.getLogger(__name__)

# Produced by app.machine_learning.select_probability_model (best-subset
# search over raw/log/log_ratio/logistic scored by weighted OOF logloss),
# not by the older regress_max_ask.py. Same {"C": classifier, "P": classifier}
# shape and the same classifier(X_new, threshold) -> (probability, y_hat)
# calling convention, so this is a drop-in swap for feature_values/X_new
# construction below -- nothing else in this file needs to change.
#
# Matches the deployed container layout installed by
# docker/install_probability_classifier.ps1 (${APP_DIR}/machine_learning/model/).
# cwd at runtime is ${APP_DIR} (docker/start.sh cd's there before launching
# the supervisor), so this relative path resolves there, not against the repo.
PROBABILITY_CLASSIFIER_PATH = "./machine_learning/model/best_probability_model.pkl"

ALL_TRADES_WINDOW = "all"
EARLY_TRADE_WINDOW = "early_trade"
VERY_EARLY_TRADE_WINDOW = "very_early_trade"


def _aggregated_path(buckets_csv_path):
    """PredictionsEvaluator._log_aggregated_buckets writes aggregated_<name> next to each
    buckets file: one row per results date, with each bucket's pooled not-hit-stop-loss
    rate under a "<lower>-<upper>%" header."""
    return os.path.join(os.path.dirname(buckets_csv_path), f"aggregated_{os.path.basename(buckets_csv_path)}")


AGGREGATED_PROBABILITY_BUCKETS_CSV_PATHS = {
    ALL_TRADES_WINDOW: _aggregated_path(PROBABILITY_BUCKETS_CSV_PATH),
    EARLY_TRADE_WINDOW: _aggregated_path(EARLY_TRADE_PROBABILITY_BUCKETS_CSV_PATH),
    VERY_EARLY_TRADE_WINDOW: _aggregated_path(VERY_EARLY_TRADE_PROBABILITY_BUCKETS_CSV_PATH),
}


class Predictor:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(Predictor, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if not self._initialized:
            self.probability_classifier = None
            self.probability_classifier_mtime = None
            self.limit_adjuster = LimitAdjuster()
            # {analysis-time window: [(lower, upper, correction)] per probability bucket},
            # read at start and reloaded after PredictionsEvaluator updates the files. Lists
            # rather than dicts keyed by bucket so buckets of unequal width work too.
            self.calibration_corrections = {}
            self._load_probability_classifier()
            self.load_calibration_corrections()
            self._initialized = True

    def _load_probability_classifier(self):
        try:
            mtime = os.path.getmtime(PROBABILITY_CLASSIFIER_PATH)
        except OSError as e:
            logger.warning(f"Could not stat probability classifier file: {e}")
            return

        if self.probability_classifier_mtime is not None and mtime <= self.probability_classifier_mtime:
            return

        try:
            with open(PROBABILITY_CLASSIFIER_PATH, 'rb') as f:
                self.probability_classifier = pickle.load(f)
            self.probability_classifier_mtime = mtime
            logger.info("Loaded probability classifier")
        except Exception as e:
            logger.warning(f"Could not load probability classifier: {e}")

    def load_calibration_corrections(self):
        """Loads the corrections of every analysis-time window: all trades, early trades
        and very early trades. Each comes from the last row of that window's aggregated
        buckets file, as written by PredictionsEvaluator. Called at start, and again by
        PredictionsEvaluator after it appends new rows to those files. The new set
        replaces the old one in a single assignment, so _calibrate never sees a mix."""
        calibration_corrections = {}
        for window, path in AGGREGATED_PROBABILITY_BUCKETS_CSV_PATHS.items():
            corrections = self._read_calibration_corrections(path)
            if corrections is not None:
                calibration_corrections[window] = corrections
        self.calibration_corrections = calibration_corrections

    @staticmethod
    def _read_calibration_corrections(path):
        """[(lower, upper, correction)] per bucket from the last row of path, or None if
        the file can't be read or has no data rows. For every bucket whose observed
        survival rate fell below the bucket's lower edge, the correction is how far it
        fell below the bucket's centre; buckets at or above their lower edge, or still
        empty, get 0."""
        try:
            with open(path, 'r', newline='') as f:
                rows = list(csv.DictReader(f, delimiter='\t'))
        except OSError as e:
            logger.warning(f"No probability calibration from {path} (could not read it: {e})")
            return None
        if not rows:
            logger.warning(f"No probability calibration from {path} (no data rows)")
            return None

        last_row = rows[-1]
        corrections = []
        for column, value in last_row.items():
            if not column or not column.endswith('%'):
                continue
            lower_pct, upper_pct = column[:-1].split('-')
            lower, upper = float(lower_pct) / 100, float(upper_pct) / 100
            correction = 0.0
            if value not in (None, ''):
                observed = float(value)
                if observed < lower:
                    correction = (lower + upper) / 2 - observed
            corrections.append((lower, upper, correction))
            logger.info(f"Calibration bucket {column} ({path}): observed {value or 'n/a'}, "
                        f"correction {correction:.3f}")
        logger.info(f"Loaded probability calibration from the {last_row.get('results_date')} row of {path}")
        return corrections

    @staticmethod
    def _current_calibration_window():
        """The analysis-time window of now: very early trade (first five minutes of the
        closing time) is checked first, since it lies inside the early closing time."""
        now_in_nyc = datetime.now(new_york_timezone)
        if is_first_five_minutes_of_closing_time(now_in_nyc):
            return VERY_EARLY_TRADE_WINDOW
        if is_early_closing_time(now_in_nyc):
            return EARLY_TRADE_WINDOW
        return ALL_TRADES_WINDOW

    def _calibrate(self, probability):
        """probability minus the correction of the bucket it falls in (0 outside every
        bucket), using the corrections of the current analysis-time window -- or of all
        trades when that window's file couldn't be loaded."""
        window = self._current_calibration_window()
        corrections = self.calibration_corrections.get(window)
        if corrections is None:
            corrections = self.calibration_corrections.get(ALL_TRADES_WINDOW, [])
        for index, (lower, upper, correction) in enumerate(corrections):
            is_last_bucket = index == len(corrections) - 1
            if lower <= probability < upper or (is_last_bucket and probability == upper):
                return max(0.0, probability - correction)
        return probability

    def predict_out_of_the_money_probability(self, option, right, estimated_sell_price, stop_loss_per_option,
                                             bid_delta, ask_delta, last_delta, model_delta, gamma, vega, theta,
                                             minutes_to_expiration, atm_iv, contract_iv, distance_to_strike_pct, should_use_calibration=False):
        self._load_probability_classifier()

        classifier = self.probability_classifier.get(right) if self.probability_classifier else None
        if not classifier:
            return None

        delta_values = [d for d in (bid_delta, ask_delta, last_delta, model_delta) if d is not None]
        max_delta = max(delta_values) if delta_values else None

        feature_values = {
            "estimated_sell_price": estimated_sell_price,
            "bid_delta": bid_delta,
            "ask_delta": ask_delta,
            "last_delta": last_delta,
            "model_delta": model_delta,
            "max_delta": max_delta,
            "gamma": gamma,
            "vega": vega,
            "theta": theta,
            "minutes_to_expiration": minutes_to_expiration,
            "atm_iv": atm_iv,
            "contract_iv": contract_iv,
            "distance_to_strike_pct": distance_to_strike_pct,
        }
        for key in classifier.get_feature_subset():
            value = feature_values.get(key)
            if value is None or (isinstance(value, float) and math.isnan(value)):
                logger.warning(f"Cannot compute max ask probability for {get_option_name(option)}: {key} is NaN")
                return None

        X_new = pd.DataFrame([feature_values])
        stop_loss = estimated_sell_price + stop_loss_per_option
        if self.limit_adjuster.is_ready():
            stop_loss = self.limit_adjuster.adjust_limit_to_market_rules(stop_loss)
        try:
            probabilities, predicted_max_asks = classifier(X_new, stop_loss)
            probability, predicted_max_ask = probabilities[0], predicted_max_asks[0]
        except Exception as e:
            logger.warning(f"Classifier call failed for {get_option_name(option)}: {e}")
            return math.nan
        if should_use_calibration:
            calibrated_probability = self._calibrate(probability)
            logger.info(f"Probability that max ask stays below {stop_loss:.2f} for {get_option_name(option)}: {calibrated_probability:.3f} "
                        f"(classifier: {probability:.3f}), "
                        f"Predicted max ask: {predicted_max_ask} (estimated sell price: {estimated_sell_price:.2f}, stop loss per option: {stop_loss_per_option:.2f})")
            return calibrated_probability

        logger.info(f"Probability that max ask stays below {stop_loss:.2f} for {get_option_name(option)}: {probability:.3f}, "
                    f"Predicted max ask: {predicted_max_ask} (estimated sell price: {estimated_sell_price:.2f}, stop loss per option: {stop_loss_per_option:.2f})")
        return probability

