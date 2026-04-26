import asyncio
import logging
from datetime import date

from ib_insync import IB

from utilities.utils import *
from .market_data_fetcher import MarketDataFetcher

logger = logging.getLogger(__name__)

class TradingBot:
    def __init__(self, ib: IB, market_data_fetcher: MarketDataFetcher):
        self.ib = ib
        self.market_data_fetcher = market_data_fetcher
        logger.info("TradingBot initialized (MarketDataFetcher injected).")

    async def get_short_options(self, should_use_cache=True):

        if not should_use_cache:
            original_request_timeout = self.ib.RequestTimeout
            self.ib.RequestTimeout = 10.0
            try:
                await self.ib.reqPositionsAsync()
            except TimeoutError:
                logger.warning("reqPositions timed out")
            finally:
                await asyncio.sleep(2)
                self.ib.RequestTimeout = original_request_timeout

        logger.debug("Requesting positions from cache")
        positions = self.ib.positions(MY_ACCOUNT)
        if not positions and should_use_cache:
            logger.info("No positions, retrying using should_use_cache=False")
            return self.get_short_options(should_use_cache=False)

        option_positions = []
        for position in positions:
            if position.contract.secType == 'OPT' and position.position < 0:
                last_trade_date = datetime.strptime(position.contract.lastTradeDateOrContractMonth, "%Y%m%d").date()
                if last_trade_date < date.today() or (last_trade_date == date.today() and is_after_hours()):
                    continue
                option_positions.append(position)

        options = [position.contract for position in option_positions]
        if options:
            logger.debug(f"Updating {len(options)} tickers of existing option positions")
            await self.market_data_fetcher.update_ticker_data(options)
        return option_positions
