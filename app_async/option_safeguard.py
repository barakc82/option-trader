import asyncio
import logging
import time
import sys
from ib_insync import IB
from .trading_bot import TradingBot
from .positions_manager import PositionsManager
from utilities.utils import is_market_open, is_regular_hours_with_after_hours

logger = logging.getLogger(__name__)

class OptionSafeguard:
    def __init__(self, ib: IB, trading_bot: TradingBot, positions_manager: PositionsManager):
        self.ib = ib
        self.trading_bot = trading_bot
        self.positions_manager = positions_manager
        self.connection_failure_start_time = None
        self.last_alive_log_time = 0

    async def run(self):
        logger.info("OptionSafeguard: Starting safeguard loop...")
        while True:
            try:
                if not self.ib.isConnected():
                    logger.warning("OptionSafeguard: Task is waiting for IB connection...")
                    await asyncio.sleep(30)
                    continue

                if time.time() - self.last_alive_log_time > 300:
                    logger.info("Option safeguard is still running")
                    self.last_alive_log_time = time.time()

                logger.info("OptionSafeguard: Monitoring position risk...")

                
                if self.connection_failure_start_time is not None:
                    logger.info("OptionSafeguard: Connection error resolved.")
                    self.connection_failure_start_time = None

                # Adaptive sleep logic
                sleep_time = 180 if is_regular_hours_with_after_hours() or not is_market_open() else 1
                await asyncio.sleep(sleep_time)

            except Exception:
                if self.connection_failure_start_time is None:
                    self.connection_failure_start_time = time.time()
                
                elapsed = time.time() - self.connection_failure_start_time
                if elapsed > 300:
                    logger.critical(f"OptionSafeguard: Persistent failure for {elapsed:.0f}s. Exiting.")
                    sys.exit(1)
                
                logger.exception(f"OptionSafeguard: Safeguard error ({elapsed:.0f}s):")
                await asyncio.sleep(10)
