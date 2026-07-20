import asyncio
import logging
import time
import math
import json
import os
from ib_insync import Option, FuturesOption

from utilities.ib_utils import get_es_option_name
from .connection_manager import ConnectionManager
from .trading_bot import TradingBot
from .market_data_fetcher import MarketDataFetcher
from utilities.utils import get_option_name, SAFEGUARD_MAX_CADENCE

logger = logging.getLogger(__name__)

class SubscriptionManager:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(SubscriptionManager, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if not self._initialized:
            self.connection_manager = ConnectionManager()
            self.ib = self.connection_manager.ib
            self.trading_bot = TradingBot()
            self.market_data_fetcher = MarketDataFetcher()
            
            # Tracks SPX conId -> Matching ES Contract for hedge subscriptions
            self.spx_to_es_map = {}

            logger.info("SubscriptionManager initialized.")
            self._initialized = True


    async def run(self):
        """Unified background loop for all market data subscriptions."""
        logger.info("SubscriptionManager: Starting background maintenance loop...")
        while True:
            try:
                from .option_safeguard import OptionSafeguard
                safeguard = OptionSafeguard()
                if time.time() - safeguard.last_run_end_time > SAFEGUARD_MAX_CADENCE:
                    await asyncio.sleep(0)
                    continue

                if self.connection_manager.is_connected():
                    logger.info("Running subscription maintenance")
                    # 1. Maintain tickers for positions and open trades
                    await self.maintain_tickers()
                    await self.manage_es_subscriptions()
                    
            except Exception:
                logger.exception("Error in SubscriptionManager loop:")
            
            # Sleep for 1 minute before the next cycle
            await asyncio.sleep(60)

    async def maintain_tickers(self):

        """Transverse positions and open trades to ensure tickers are attached to contracts."""
        positions = self.trading_bot.get_short_options()
        open_trades = self.trading_bot.get_open_trades()

        # Combine unique contracts from positions and open trades
        required_contracts = {p.contract.conId: p.contract for p in positions}
        for trade in open_trades:
            if trade.contract.conId not in required_contracts:
                required_contracts[trade.contract.conId] = trade.contract

        contracts_missing_tickers = []
        active_tickers = self.ib.wrapper.ticker2ReqId['mktData'].keys()
        if time.time() % 100000:
            for active_ticker in active_tickers:
                contract = active_ticker.contract
                logger.info(f"Subscribed to {contract.symbol} {contract.secType} {contract.right} {contract.strike}")

        active_indices = [ticker.contract for ticker in active_tickers if ticker.contract.secType in ['IND', 'STK', 'FUT']]
        es_future = await self.market_data_fetcher.fetch_es_future()

        for required_index in [self.market_data_fetcher.spx, es_future]:
            if required_index and required_index not in active_indices:
                logger.info(f"Going to subscribe to Index {required_index.symbol}")
                contracts_missing_tickers.append(required_index)

        active_spx_options = [ticker.contract for ticker in active_tickers if ticker.contract.symbol == 'SPX' and ticker.contract.secType == 'OPT']

        for required_contract in required_contracts.values():
            is_contract_subscribed = False
            for active_contract in active_spx_options:
                if active_contract is required_contract:
                    is_contract_subscribed = True
            if not is_contract_subscribed:
                logger.info(f"Option {get_option_name(required_contract)} is missing a ticker")
                contracts_missing_tickers.append(required_contract)

        if contracts_missing_tickers:
            logger.info(f"Found {len(contracts_missing_tickers)} contracts missing tickers. Updating...")
            # update_ticker_data will request tickers and attach them to the contracts
            await self.market_data_fetcher.request_subscriptions(contracts_missing_tickers)
            
            for contract in contracts_missing_tickers:
                ticker = getattr(contract, 'ticker', None)
                if ticker:
                    self.market_data_fetcher.register_ticker(ticker)
                    logger.info(f"Ticker successfully attached to {get_option_name(contract)}")
                else:
                    logger.warning(f"Failed to attach ticker to {get_option_name(contract)}")
        else:
            logger.debug("All current positions and open trades have tickers attached.")

        # Cleanup stale subscriptions
        for active_contract in active_spx_options:
            required_contract = required_contracts.get(active_contract.conId, None)
            if active_contract is not required_contract:
                if required_contract is None:
                    logger.info(f"Unsubscribing option {get_option_name(active_contract)} since it is no longer in use")
                else:
                    logger.info(f"Unsubscribing option {get_option_name(active_contract)} since the required contract changed")
                self.market_data_fetcher.cancel_market_data(active_contract)


    async def manage_es_subscriptions(self):
        """Check current SPX positions and update ES subscriptions."""
        positions = self.trading_bot.get_short_options()
        # SPX options can have symbol 'SPX' or 'SPXW' (weekly)
        spx_positions = [p for p in positions if p.contract.symbol in ('SPX', 'SPXW')]
        current_spx_con_ids = {p.contract.conId for p in spx_positions}

        # 1. Identify new matching ES options to subscribe
        new_es_contracts_batch = []
        for position in spx_positions:
            spx_contract = position.contract
            if spx_contract.conId not in self.spx_to_es_map:
                logger.info(f"No ES contract found for {get_option_name(spx_contract)}")
                es_contracts = await self.create_matching_es_contracts(spx_contract)
                new_es_contracts_batch.append((spx_contract, es_contracts))

        if new_es_contracts_batch:
            active_tickers = self.ib.wrapper.ticker2ReqId['mktData'].keys()
            active_es_options = [ticker.contract for ticker in active_tickers if ticker.contract.symbol == 'ES' and ticker.contract.secType == 'FOP']

            # Deduplicate all ES contracts by (strike, right, lastTradeDateOrContractMonth)
            unique_new_es = {}
            for _, es_contracts in new_es_contracts_batch:
                for es in es_contracts:
                    key = (es.strike, es.right, es.lastTradeDateOrContractMonth)
                    if key not in unique_new_es:
                        unique_new_es[key] = es

            contracts_to_subscribe = []
            for required_es_contract in unique_new_es.values():
                is_contract_subscribed = False
                for active_es_contract in active_es_options:
                    if (active_es_contract.symbol == required_es_contract.symbol and
                        active_es_contract.strike == required_es_contract.strike and
                        active_es_contract.right == required_es_contract.right and
                        active_es_contract.lastTradeDateOrContractMonth == required_es_contract.lastTradeDateOrContractMonth):
                        is_contract_subscribed = True
                        key = (required_es_contract.strike, required_es_contract.right, required_es_contract.lastTradeDateOrContractMonth)
                        unique_new_es[key] = active_es_contract
                        break
                if not is_contract_subscribed:
                    logger.info(f"Option ES {required_es_contract.right} {required_es_contract.strike} is missing a ticker")
                    contracts_to_subscribe.append(required_es_contract)

            if contracts_to_subscribe:
                logger.info(f"Subscribing to {len(contracts_to_subscribe)} unique new matching ES options")
                await self.market_data_fetcher.request_subscriptions(contracts_to_subscribe)

            for spx_contract, es_contracts in new_es_contracts_batch:
                qualified_es_list = []
                for es_contract in es_contracts:
                    key = (es_contract.strike, es_contract.right, es_contract.lastTradeDateOrContractMonth)
                    qualified_es = unique_new_es[key]
                    if qualified_es.conId and self.market_data_fetcher.get_ticker(qualified_es):
                        qualified_es_list.append(qualified_es)
                        logger.info(
                            f"Subscribed to {get_es_option_name(qualified_es)} for SPX {get_option_name(spx_contract)}")
                    else:
                        logger.error(f"Failed to subscribe matching ES option {qualified_es.strike} for {get_option_name(spx_contract)}")
                if qualified_es_list:
                    self.spx_to_es_map[spx_contract.conId] = qualified_es_list

        # 2. Unsubscribe from ES options for closed SPX positions
        closed_spx_con_ids = [con_id for con_id in list(self.spx_to_es_map.keys()) if con_id not in current_spx_con_ids]
        for con_id in closed_spx_con_ids:
            es_contracts = self.spx_to_es_map.pop(con_id)
            logger.info(f"SPX position {con_id} closed. Unsubscribing from ES hedges.")
            for es_contract in es_contracts:
                is_in_use = any(es_contract in contracts for contracts in self.spx_to_es_map.values())
                if not is_in_use:
                    self.market_data_fetcher.cancel_market_data(es_contract)

        # 3. Warn about stale ES tickers
        active_tickers = self.ib.wrapper.ticker2ReqId['mktData'].keys()
        active_es_con_ids = {ticker.contract.conId for ticker in active_tickers
                             if ticker.contract.symbol == 'ES' and ticker.contract.secType == 'FOP'}
        for es_contracts in self.spx_to_es_map.values():
            for es_contract in es_contracts:
                if es_contract.conId not in active_es_con_ids:
                    logger.warning(f"ES ticker {get_es_option_name(es_contract)} is not in active subscriptions")
                    continue
                ticker = self.market_data_fetcher.get_ticker(es_contract)
                if ticker and ticker.time:
                    age_minutes = (time.time() - ticker.time.timestamp()) / 60
                    if age_minutes > 20:
                        logger.warning(f"ES ticker {get_es_option_name(es_contract)} last updated {age_minutes:.1f} minutes ago (at {ticker.time})")
                elif ticker and time.time() % 1000000:
                    logger.error(f"ES ticker {get_es_option_name(es_contract)} has no time field")

    def invalidate_key(self, option):
        logger.info(f"Invalidating ES subscription key for {get_option_name(option)}")
        self.spx_to_es_map.pop(option.conId, None)

    async def create_matching_es_contracts(self, spx_contract):
        """Return two ES option contracts whose strikes bracket the SPX strike in ES terms."""
        difference = self.market_data_fetcher.calculate_spx_es_difference()
        equivalent_es_strike = spx_contract.strike - difference

        strike_increment = 5.0
        lower_strike = math.floor(equivalent_es_strike / strike_increment) * strike_increment
        upper_strike = math.ceil(equivalent_es_strike / strike_increment) * strike_increment

        logger.info(
            f"SPX strike {spx_contract.strike} maps to ES equivalent {equivalent_es_strike:.2f} "
            f"(difference={difference:.2f}); bracketing with ES strikes {lower_strike} / {upper_strike}"
        )

        async def fetch_es_contract(strike):
            future_option = FuturesOption(
                symbol='ES',
                lastTradeDateOrContractMonth=spx_contract.lastTradeDateOrContractMonth,
                strike=strike,
                right=spx_contract.right,
                exchange='CME',
                currency='USD'
            )
            details = await self.ib.reqContractDetailsAsync(future_option)
            return next(
                detail.contract
                for detail in details
                if detail.contract.tradingClass != "ES"
            )

        return await asyncio.gather(
            fetch_es_contract(lower_strike),
            fetch_es_contract(upper_strike)
        )
