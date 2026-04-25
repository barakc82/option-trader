import asyncio
import logging
import time
import sys
from ib_insync import IB

from utilities.utils import write_heartbeat
from .logging_setup import setup_logging
from .trading_bot import TradingBot
from .positions_manager import PositionsManager

logger = logging.getLogger(__name__)

class OptionTrader:
    def __init__(self, ib: IB, trading_bot: TradingBot, positions_manager: PositionsManager):
        self.ib = ib
        self.trading_bot = trading_bot
        self.positions_manager = positions_manager
        self.connection_failure_start_time = None

    async def run(self):
        logger.info("OptionTrader: Starting trading loop...")
        while True:
            try:
                write_heartbeat()
                setup_logging()
                if not self.ib.isConnected():
                    logger.warning("OptionTrader: Task is waiting for IB connection...")
                    await asyncio.sleep(30) # Longer wait while disconnected
                    continue

                logger.info("OptionTrader: Checking market opportunities...")
                await asyncio.sleep(5)
                
                if self.connection_failure_start_time is not None:
                    self.connection_failure_start_time = None
                
            except Exception:
                if self.connection_failure_start_time is None:
                    self.connection_failure_start_time = time.time()
                
                elapsed = time.time() - self.connection_failure_start_time
                if elapsed > 300:
                    logger.critical(f"OptionTrader: Persistent failure for {elapsed:.0f}s. Exiting.")
                    sys.exit(1)
                
                logger.exception(f"OptionTrader: Loop error ({elapsed:.0f}s):")
                await asyncio.sleep(10)
