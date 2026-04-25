import logging.config
import asyncio
import sys
import faulthandler
from pathlib import Path

from utilities.utils import acquire_single_instance_lock
from .logging_setup import setup_logging
from .option_trader import OptionTrader
from .option_safeguard import OptionSafeguard

async def main():
    """Application entry point."""
    logger.info("Initializing Async Option Trader (Class-based Refactor)...")
    
    # Instantiate the components
    trader = OptionTrader()
    safeguard = OptionSafeguard()

    try:
        # Run tasks concurrently using class methods
        await asyncio.gather(
            trader.run(),
            safeguard.run()
        )
    except asyncio.CancelledError:
        logger.info("Tasks were cancelled during shutdown.")
    except Exception:
        logger.exception("Main Event Loop encountered a fatal error:")

if __name__ == "__main__":
    # Enable faulthandler to log tracebacks even on low-level crashes
    faulthandler.enable()

    # Setup single-instance lock and logging
    _lock = acquire_single_instance_lock(lock_path='/tmp/option_trader.lock', process_name='Option Trader')
    setup_logging()
    logger = logging.getLogger("main")

    # Log file cleanup logic
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    log_pattern = "*.log"
    keep_last_n = 5

    log_files = sorted(
        log_dir.glob(log_pattern),
        key=lambda f: f.stat().st_mtime,
        reverse=True
    )

    for old_log in log_files[keep_last_n:]:
        try:
            old_log.unlink()
            logger.info(f"Deleted old log file: {old_log.name}")
        except Exception as e:
            logger.error(f"Failed to delete {old_log.name}: {e}")

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutdown requested by user (Ctrl+C).")
    except Exception:
        logger.exception("System Crash - Unhandled exception in main execution:")
        sys.exit(1)
