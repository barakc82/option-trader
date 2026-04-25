import asyncio
import logging

from utilities.utils import *

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

class OptionTrader:
    def __init__(self):
        pass

    async def run(self):
        """Continuous task: Main trading logic."""
        logger.info("OptionTrader: Starting trading loop...")

        while True:
            try:
                logger.info("OptionTrader: Checking market opportunities...")
                write_heartbeat()

                await asyncio.sleep(5)

            except asyncio.CancelledError:
                logger.info("OptionTrader: Shutting down...")
                break
            except Exception:
                # This line captures and prints the full traceback automatically
                logger.exception("OptionTrader: Fatal error in loop logic:")
                # We sleep before retrying to prevent "rapid-fire" error logs
                await asyncio.sleep(10)
