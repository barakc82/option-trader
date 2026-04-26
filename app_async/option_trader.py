import asyncio

from ib_insync import IB

from utilities.utils import *

from .trading_bot import TradingBot
from .positions_manager import PositionsManager

logger = logging.getLogger(__name__)

class OptionTrader:
    def __init__(self, ib: IB, trading_bot: TradingBot, positions_manager: PositionsManager):
        self.ib = ib
        self.trading_bot = trading_bot
        self.positions_manager = positions_manager
        self.connection_failure_start_time = None
        self.config = {}
        self.should_write_options_overnight = True
        self.should_monitor_only = False

    def load_config(self):
        """Reads configuration from config/option_trader_config.json."""
        config_path = "config/option_trader_config.json"
        try:
            if os.path.exists(config_path):
                with open(config_path, "r") as f:
                    self.config = json.load(f)
                    self.should_write_options_overnight = self.config.get("should_write_options_overnight", True)
                    self.should_monitor_only = self.config.get("should_monitor_only", False)
                    logger.debug(f"Config loaded: monitor_only={self.should_monitor_only}")
        except Exception as e:
            logger.error(f"OptionTrader: Error reading config: {e}")

    async def run(self):
        logger.info("OptionTrader: Starting trading loop...")
        while True:
            try:
                # Refresh configuration
                self.load_config()

                write_heartbeat()
                
                if not self.ib.isConnected():
                    logger.warning("OptionTrader: Task is waiting for IB connection...")
                    await asyncio.sleep(30)
                    continue

                # Consistent status message
                logger.info(f"OptionTrader: Checking market status (Monitor Only: {self.should_monitor_only})...")
                self.trade()
                
                # Main trading cadence
                await self.sleep()
                
                if self.connection_failure_start_time is not None:
                    logger.info("OptionTrader: Connection error resolved.")
                    self.connection_failure_start_time = None
                
            except Exception:
                if self.connection_failure_start_time is None:
                    self.connection_failure_start_time = time.time()
                
                elapsed = time.time() - self.connection_failure_start_time
                if elapsed > 300:
                    logger.critical(f"OptionTrader: Persistent failure for {elapsed:.0f}s. Exiting.")
                    sys.exit(1)
                
                logger.exception(f"OptionTrader: Loop error ({elapsed:.0f}s):")
                await asyncio.sleep(5)


    async def sleep(self):
        write_heartbeat()
        sleep_time_in_seconds = 180
        if is_market_open() or is_buffer_time_around_trade_time():
            sleep_time_in_seconds = 90 if is_in_docker() else 180
        if is_early_closing_hours():
            sleep_time_in_seconds = 40
        logger.info(f"Sleeping for {sleep_time_in_seconds // 60} minutes")

        times = sleep_time_in_seconds // 10
        for _ in range(times):
            write_heartbeat()
            await asyncio.sleep(10)

    def trade(self):
        is_market_open_result = is_market_open()
        if is_market_open_result:
            self.positions_manager.manage_current_positions()