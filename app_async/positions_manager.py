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

    def is_recent_order_filled(self, position, action):
        for filled_trade in self.filled_trades:
            if filled_trade.action == action and filled_trade.conId == position.contract.conId and time.time() - filled_trade.fill_time < 60:
                # if filled_trade.conId == position.contract.conId and time.time() - filled_trade.fill_time < 60:
                logger.info(
                    f"Found a matching recent filled trade: {filled_trade.option_name}, contract id {filled_trade.conId}, action: {filled_trade.action}")
                return True
        return False

    def is_recent_buy_filled(self, position):
        return self.is_recent_order_filled(position, 'BUY')
