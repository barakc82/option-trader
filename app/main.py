from datetime import datetime
import os
import sys
import logging.config
import traceback
from pathlib import Path

from logging_setup import setup_logging
from option_trader import OptionTrader


if __name__ == "__main__":

    setup_logging()
    logger = logging.getLogger("main")

    log_dir = Path("../logs")
    log_dir.mkdir(exist_ok=True)
    log_pattern = "*.log"
    keep_last_n = 5

    # Clean up old log files (keep only most recent 5)
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
        option_trader = OptionTrader()
        option_trader.trade_continuously()
    except Exception:
        traceback.print_exc()
        logger.error("Unhandled exception:\n%s", traceback.format_exc())
        sys.exit(1)
