import time
import logging
from .trading_bot import TradingBot

logger = logging.getLogger(__name__)

class PositionsManager:
    def __init__(self, trading_bot: TradingBot):
        self.trading_bot = trading_bot
        self.filled_trades = []
        logger.info("PositionsManager initialized (TradingBot injected).")


    def get_recent_trades(self):
        return [filled_trade for filled_trade in self.filled_trades if time.time() - filled_trade.fill_time < 300]
