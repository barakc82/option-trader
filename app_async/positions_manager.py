import logging
from .trading_bot import TradingBot

logger = logging.getLogger(__name__)

class PositionsManager:
    def __init__(self, trading_bot: TradingBot):
        self.trading_bot = trading_bot
        logger.info("PositionsManager initialized (TradingBot injected).")
