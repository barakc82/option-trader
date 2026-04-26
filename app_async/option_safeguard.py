import asyncio
import logging
import time
import sys

from ib_insync import IB

from utilities.utils import *

from .trading_bot import TradingBot
from .positions_manager import PositionsManager
from .market_data_fetcher import MarketDataFetcher

logger = logging.getLogger(__name__)

class OptionSafeguard:
    def __init__(self, ib: IB, trading_bot: TradingBot, positions_manager: PositionsManager, market_data_fetcher: MarketDataFetcher):
        self.ib = ib
        self.trading_bot = trading_bot
        self.positions_manager = positions_manager
        self.market_data_fetcher = market_data_fetcher
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

                logger.debug("OptionSafeguard: Monitoring position risk...")
                if is_market_open():
                    await self.guard_current_positions()
                else:
                    logger.debug(f"Market is closed")
                
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


    async def guard_current_positions(self):
        recent_trades = self.positions_manager.get_recent_trades()
        for recent_trade in recent_trades:
            logger.info(
                f"Recent filled trade: {recent_trade.option_name}, contract id {recent_trade.conId}, order type: {recent_trade.action}")

        logger.debug("Checking current positions")
        positions = await self.trading_bot.get_short_options(should_use_cache=True)
        for position in positions:
            await self.handle_current_risk(position)


    async def handle_current_risk(self, position):
        option = position.contract
        if not hasattr(option, 'ticker') or option.ticker is None:
            ticker = self.market_data_fetcher.get_ticker(option)
            if ticker is None:
                logger.error(f"The ticker of {get_option_name(option)} is missing")
                ticker = await self.market_data_fetcher.req_mkt_data(option, is_snapshot=False)
                option.ticker = ticker
            else:
                logger.debug(f"The ticker of {get_option_name(option)} was found in search, attaching it to the contract")
                option.ticker = ticker
            return

        if datetime.now().astimezone() - option.ticker.time > timedelta(seconds=4):
            logger.debug(f"The ticker of {get_option_name(option)} is invalid, updating it")
            ticker = await self.market_data_fetcher.req_mkt_data(option, is_snapshot=False)
            option.ticker = ticker

        last_price = option.ticker.last