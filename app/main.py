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
from .subscription_manager import SubscriptionManager
from .state_updater import StateUpdater

OPTION_TRADER_CLIENT_ID = 1

async def supervisor(task_coro, name):
    """Supervise a task and restart it if it crashes."""
    while True:
        try:
            logger.info(f"Supervisor: Starting {name}...")
            await task_coro
            logger.warning(f"Supervisor: {name} finished unexpectedly. Restarting in 10s...")
        except asyncio.CancelledError:
            logger.info(f"Supervisor: {name} was cancelled.")
            raise
        except Exception:
            logger.exception(f"Supervisor: {name} crashed with a fatal error. Restarting in 10s...")
        
        await asyncio.sleep(10)

async def main():
    """Application entry point."""
    logger.info("Initializing Async Option Trader...")
    
    # 1. Initialize the Connection singleton
    connection_manager = ConnectionManager()
    
    # 2. Start Task Classes
    trader = OptionTrader()
    safeguard = OptionSafeguard()
    subscription_manager = SubscriptionManager()
    state_updater = StateUpdater()

    try:
        # Run everything concurrently under supervision
        await asyncio.gather(
            supervisor(connection_manager.connect(client_id=OPTION_TRADER_CLIENT_ID), "ConnectionManager"),
            supervisor(trader.run(), "OptionTrader"),
            supervisor(safeguard.run(), "OptionSafeguard"),
            supervisor(subscription_manager.run(), "SubscriptionManager"),
            supervisor(state_updater.run(), "StateUpdater")
        )
    except asyncio.CancelledError:
        logger.info("Tasks were cancelled during shutdown.")
    except Exception:
        logger.exception("Main Event Loop encountered a fatal error:")
    finally:
        connection_manager.disconnect()

if __name__ == "__main__":
    faulthandler.enable()

    _lock = acquire_single_instance_lock(lock_path='/tmp/option_trader_async.lock', process_name='Option Trader Async')
    setup_logging()
    logger = logging.getLogger("main")

    # Cleanup logs
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    for old_log in sorted(log_dir.glob("*.log"), key=lambda f: f.stat().st_mtime, reverse=True)[5:]:
        try:
            old_log.unlink()
        except Exception:
            pass

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutdown requested by user.")
    except Exception:
        logger.exception("System Crash:")
        sys.exit(1)
