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

async def supervisor(task_factory, name, register=True):
    """Supervise a task and restart it on crash or reconnect-triggered cancellation."""
    cm = ConnectionManager()
    while True:
        logger.info(f"Supervisor: Starting {name}...")
        inner_task = asyncio.create_task(task_factory(), name=name)
        if register:
            cm.register_task(inner_task)
        try:
            await inner_task
            logger.warning(f"Supervisor: {name} finished unexpectedly. Restarting in 10s...")
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            if inner_task.cancelled():
                # Inner task was cancelled by ConnectionManager for restart — loop back immediately.
                    logger.info(f"Supervisor: {name} restarting after reconnect...")
            else:
                # The supervisor itself is being shut down — propagate.
                inner_task.cancel()
                await asyncio.gather(inner_task, return_exceptions=True)
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
            supervisor(lambda: connection_manager.connect(client_id=OPTION_TRADER_CLIENT_ID), "ConnectionManager", register=False),
            supervisor(trader.run, "OptionTrader"),
            supervisor(safeguard.run, "OptionSafeguard"),
            supervisor(subscription_manager.run, "SubscriptionManager"),
            supervisor(state_updater.run, "StateUpdater")
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
