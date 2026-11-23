import logging
import math

logger = logging.getLogger(__name__)

# Above this price level, a non-ML-derived jump from below it is slowed down to it.
PRICE_LEVEL_JUMP_CAP = 0.15


class PriceLevelsManager:
    """The current price level of each right (the price an option of that right can
    currently be sold for), as found by OpportunityExplorer, and whether it came from
    the ML (expected-profit) path. All three fields are keyed by right ('C' / 'P')."""
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(PriceLevelsManager, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if not self._initialized:
            self.last_option_price = {'C': 0, 'P': 0}
            self.last_ml_derived_option_price = {'C': 0, 'P': 0}
            self.is_last_option_ml_derived = {'C': False, 'P': False}
            self._initialized = True

    def get_last_option_price(self, right):
        return self.last_ml_derived_option_price[right] if self.is_last_option_ml_derived[right] else self.last_option_price[right]

    def update_last_option_price(self, price, right, is_ml_derived):
        if math.isnan(price):
            return

        is_ml_derived_text = "" if is_ml_derived else "non-"
        is_change = False

        right_text = "call" if right == 'C' else "put"
        if self.get_last_option_price(right) != price:
            logger.info(
                f"The current price level for {right_text} options changed from {self.get_last_option_price(right)} to {price}")
            if self.get_last_option_price(right) < PRICE_LEVEL_JUMP_CAP < price and not is_ml_derived:
                logger.info(
                    f"However, this is a high jump from the previous level of {self.get_last_option_price(right)} to {price}, slowing the pace of increase so that the price level for {right_text} options is {PRICE_LEVEL_JUMP_CAP}")
                price = PRICE_LEVEL_JUMP_CAP
            is_change = True

        if self.is_last_option_ml_derived[right] != is_ml_derived:
            logger.info(
                f"The current price level for {right_text} options changed to {is_ml_derived_text}ml-derived with a price of {price}")
            is_change = True

        if is_change:
            self.is_last_option_ml_derived[right] = is_ml_derived
            if is_ml_derived:
                self.last_ml_derived_option_price[right] = price
            else:
                self.last_option_price[right] = price
