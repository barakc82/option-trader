import math

from .trading_bot import TradingBot


class PriceEstimator:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(PriceEstimator, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if not self._initialized:
            self.trading_bot = TradingBot()
            self._initialized = True

    def estimate_sell_price(self, option):
        if math.isnan(option.ticker.bid) or math.isnan(option.ticker.ask):
            return option.ticker.last
        return self.trading_bot.calculate_limit(option.ticker.bid, option.ticker.ask)
