import asyncio
import logging
import time
from .trading_bot import TradingBot
from .market_data_fetcher import MarketDataFetcher
from utilities.utils import get_option_name, SAFEGUARD_MAX_CADENCE

logger = logging.getLogger(__name__)

class TickerMaintenanceTask:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(TickerMaintenanceTask, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if not self._initialized:
            self.trading_bot = TradingBot()
            self.market_data_fetcher = MarketDataFetcher()
            logger.info("TickerMaintenanceTask singleton initialized.")
            self._initialized = True

    async def run(self):
        """Background task to ensure all current positions have attached tickers."""
        logger.info("TickerMaintenanceTask: Starting background maintenance loop...")
        while True:
            try:
                from .option_safeguard import OptionSafeguard
                safeguard = OptionSafeguard()
                if time.time() - safeguard.last_run_end_time > SAFEGUARD_MAX_CADENCE:
                    await asyncio.sleep(0)
                    continue

                if self.trading_bot.ib.isConnected():
                    await self.maintain_tickers()
            except Exception:
                logger.exception("Error in TickerMaintenanceTask loop:")
            
            # Sleep for a longer interval as this is a low-priority task
            await asyncio.sleep(60)

    async def maintain_tickers(self):
        """Transverse positions and ensure tickers are attached to contracts."""
        positions = self.trading_bot.get_short_options()
        
        contracts_missing_tickers = []
        for position in positions:
            contract = position.contract
            ticker = self.market_data_fetcher.get_ticker(contract)
            
            if ticker is not None:
                if getattr(contract, 'ticker', None) is None:
                    contract.ticker = ticker
            else:
                contracts_missing_tickers.append(contract)

        if contracts_missing_tickers:
            logger.info(f"Found {len(contracts_missing_tickers)} positions missing tickers. Updating...")
            # update_ticker_data will request tickers and attach them to the contracts
            await self.market_data_fetcher.update_ticker_data(contracts_missing_tickers)
            
            for contract in contracts_missing_tickers:
                if getattr(contract, 'ticker', None):
                    logger.debug(f"Ticker successfully attached to {get_option_name(contract)}")
                else:
                    logger.warning(f"Failed to attach ticker to {get_option_name(contract)}")
        else:
            logger.debug("All current positions have tickers attached.")
