import asyncio
import logging
import time
import sys
from ib_insync import IB

logger = logging.getLogger(__name__)

class OptionTrader:
    def __init__(self):
        self.connection_failure_start_time = None

    async def run(self):
        logger.info("OptionTrader: Starting trading loop...")
        while True:
            try:
                # Now you can use self.ib directly!
                # positions = self.ib.positions()
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
