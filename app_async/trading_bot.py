import logging
from ib_insync import IB

logger = logging.getLogger(__name__)

class TradingBot:
    def __init__(self, ib: IB):
        self.ib = ib
        logger.info("TradingBot initialized with shared IB connection.")
