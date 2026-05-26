import time
import asyncio
import logging
import math
from ib_insync import Option
from .trading_bot import TradingBot
from .market_data_fetcher import MarketDataFetcher
from utilities.utils import get_option_name, SAFEGUARD_MAX_CADENCE
from utilities.ib_utils import get_spy_option_name

logger = logging.getLogger(__name__)

class SpySubscriptionManager:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(SpySubscriptionManager, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if not self._initialized:
            self.trading_bot = TradingBot()
            self.market_data_fetcher = MarketDataFetcher()
            self.spx_to_spy_map = {} # conId of SPX -> Matching SPY Contract
            logger.info("SpySubscriptionManager initialized.")
            self._initialized = True

    async def run(self):
        """Main loop for managing SPY subscriptions based on SPX positions."""
        logger.info("SpySubscriptionManager: Starting background loop...")
        while True:
            try:
                from .option_safeguard import OptionSafeguard
                safeguard = OptionSafeguard()
                if time.time() - safeguard.last_run_end_time > SAFEGUARD_MAX_CADENCE:
                    await asyncio.sleep(0)
                    continue

                if self.trading_bot.ib.isConnected():
                    await self.manage_subscriptions()
            except Exception:
                logger.exception("Error in SpySubscriptionManager loop:")
            
            # Wait for 1 minute before the next check
            await asyncio.sleep(60)

    async def manage_subscriptions(self):
        """Check current SPX positions and update SPY subscriptions."""
        positions = await self.trading_bot.get_short_options()
        # SPX options can have symbol 'SPX' or 'SPXW' (weekly)
        spx_positions = [p for p in positions if p.contract.symbol in ('SPX', 'SPXW')]

        current_spx_con_ids = {p.contract.conId for p in spx_positions}

        # 1. Identify new matching SPY options to subscribe
        new_spy_contracts = []
        for position in spx_positions:
            spx_contract = position.contract
            if spx_contract.conId not in self.spx_to_spy_map:
                spy_contract = self.create_matching_spy_contract(spx_contract)
                new_spy_contracts.append((spx_contract, spy_contract))
        
        if new_spy_contracts:
            contracts_to_subscribe = [spy for spx, spy in new_spy_contracts]
            logger.info(f"Subscribing to {len(contracts_to_subscribe)} new matching SPY options in a batch.")
            
            # update_ticker_data qualifies and requests tickers for all contracts
            await self.market_data_fetcher.update_ticker_data(contracts_to_subscribe)
            
            for spx_contract, spy_contract in new_spy_contracts:
                if spy_contract.conId:
                    self.spx_to_spy_map[spx_contract.conId] = spy_contract
                    logger.info(f"Subscribed to {get_spy_option_name(spy_contract)} for SPX position {get_option_name(spx_contract)}")
                else:
                    logger.error(f"Failed to qualify matching SPY option for {get_option_name(spx_contract)}")
                
        # 2. Unsubscribe from SPY options for closed SPX positions
        closed_spx_con_ids = [con_id for con_id in self.spx_to_spy_map.keys() if con_id not in current_spx_con_ids]
        for con_id in closed_spx_con_ids:
            spy_contract = self.spx_to_spy_map.pop(con_id)
            logger.info(f"SPX position {con_id} closed. Unsubscribing from {get_spy_option_name(spy_contract)}.")
            self.market_data_fetcher.cancel_market_data(spy_contract)

    def create_matching_spy_contract(self, spx_contract):
        """Create a matching SPY option contract for a given SPX option contract."""
        # SPY strike is SPX strike / 10
        spy_strike_raw = spx_contract.strike / 10.0

        if spy_strike_raw == int(spy_strike_raw):
            spy_strike = spy_strike_raw  # clean mapping, e.g. 5000 → 500.0
        else:
            # Half-strike: pick the more expensive side
            if spx_contract.right == "P":
                spy_strike = math.ceil(spy_strike_raw)  # e.g. 500.5 → 501, 504.5 → 505
            else:  # "C"
                spy_strike = math.floor(spy_strike_raw)  # e.g. 500.5 → 500, 504.5 → 504
        
        # We use 'SMART' exchange for SPY options
        return Option(
            symbol='SPY',
            lastTradeDateOrContractMonth=spx_contract.lastTradeDateOrContractMonth,
            strike=spy_strike,
            right=spx_contract.right,
            exchange='SMART',
            currency='USD'
        )
