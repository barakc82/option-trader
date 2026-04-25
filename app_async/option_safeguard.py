import asyncio
import logging

logger = logging.getLogger(__name__)

class OptionSafeguard:
    def __init__(self):
        pass

    async def run(self):
        """Continuous task: Risk management logic."""
        logger.info("OptionSafeguard: Starting safeguard loop...")
        while True:
            try:
                # Placeholder for risk management logic
                logger.info("OptionSafeguard: Monitoring position risk...")
                await asyncio.sleep(2)
                
            except asyncio.CancelledError:
                logger.info("OptionSafeguard: Shutting down...")
                break
            except Exception:
                # This line captures and prints the full traceback automatically
                logger.exception("OptionSafeguard: Fatal error in safeguard logic:")
                await asyncio.sleep(10)
