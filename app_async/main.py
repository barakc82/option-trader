import logging.config
import asyncio
import sys
import faulthandler
from pathlib import Path

from utilities.utils import acquire_single_instance_lock
from .logging_setup import setup_logging
from .connection_manager import ConnectionManager
from .option_trader import OptionTrader
from .option_safeguard import OptionSafeguard

OPTION_TRADER_CLIENT_ID = 1

async def main():
    """Application entry point."""
    logger.info("Initializing Async Option Trader (Shared Connection refactor)...")
    
    # 1. Initialize Connection Manager (Shared IB instance)
    connection_manager = ConnectionManager()
    
    trader = OptionTrader()
    safeguard = OptionSafeguard()

    try:
        # Run everything concurrently:
        # - The connection manager loop (connects and maintains connection)
        # - The trading loop
        # - The safeguard loop
        await asyncio.gather(
            connection_manager.connect(client_id=OPTION_TRADER_CLIENT_ID),
            trader.run(),
            safeguard.run()
        )
    except asyncio.CancelledError:
        logger.info("Tasks were cancelled during shutdown.")
    except Exception:
        logger.exception("Main Event Loop encountered a fatal error:")
    finally:
        connection_manager.disconnect()

if __name__ == "__main__":
    faulthandler.enable()

    _lock = acquire_single_instance_lock(lock_path='/tmp/option_trader.lock', process_name='Option Trader')
    setup_logging()
    logger = logging.getLogger("main")

    # Cleanup logs
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    for old_log in sorted(log_dir.glob("*.log"), key=lambda f: f.stat().st_mtime, reverse=True)[5:]:
        try:
            old_log.unlink()
            logger.info(f"Deleted old log file: {old_log.name}")
        except Exception:
            pass

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutdown requested by user.")
    except Exception:
        logger.exception("System Crash:")
        sys.exit(1)
