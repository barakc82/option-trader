import logging
import math
import os
import pickle

import pandas as pd

from utilities.utils import get_option_name

logger = logging.getLogger(__name__)

PROBABILITY_CLASSIFIER_PATH = "./machine_learning/probability_classifier.pkl"


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
            self._load_probability_classifier()
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

    def predict_max_ask_probability(self, option, right, target_delta, estimated_sell_price, stop_loss_per_option,
                                     bid_delta, ask_delta, last_delta, model_delta, gamma, vega, theta,
                                     minutes_to_expiration, atm_iv, distance_to_strike_pct):
        self._load_probability_classifier()

        classifier = self.probability_classifier.get(right) if self.probability_classifier else None
        if not classifier:
            return None

        delta_values = [d for d in (bid_delta, ask_delta, last_delta, model_delta) if d is not None]
        max_delta = max(delta_values) if delta_values else None

        feature_values = {
            "estimated_sell_price": estimated_sell_price,
            "target_delta": target_delta,
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
            "distance_to_strike_pct": distance_to_strike_pct,
        }
        for key, value in feature_values.items():
            if value is None or (isinstance(value, float) and math.isnan(value)):
                logger.warning(f"Cannot compute max ask probability for {get_option_name(option)}: {key} is NaN")
                return None

        X_new = pd.DataFrame([feature_values])
        stop_loss = estimated_sell_price + stop_loss_per_option
        probabilities, predicted_max_asks = classifier(X_new, stop_loss)
        probability, predicted_max_ask = probabilities[0], predicted_max_asks[0]
        logger.info(f"Probability that max ask stays below {stop_loss:.2f} for {get_option_name(option)}: {probability:.3f}, "
                    f"Predicted max ask: {predicted_max_ask} (estimated sell price: {estimated_sell_price:.2f}, stop loss per option: {stop_loss_per_option:.2f})")
        return probability
