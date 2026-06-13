import asyncio
import logging
import time
import math
from ib_insync import Option
from .connection_manager import ConnectionManager
from .trading_bot import TradingBot
from .market_data_fetcher import MarketDataFetcher
from utilities.utils import get_option_name, SAFEGUARD_MAX_CADENCE
from utilities.ib_utils import get_spy_option_name

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
            self.ib = ConnectionManager().ib
            self.trading_bot = TradingBot()
            self.market_data_fetcher = MarketDataFetcher()
            
            # Tracks SPX conId -> Matching SPY Contract for hedge subscriptions
            self.spx_to_spy_map = {}
            self.spy_strikes = None
            self.spy_strikes_update_time = 0
            
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

                if self.ib.isConnected():
                    logger.info("Running subscription maintenance")
                    # 1. Maintain tickers for positions and open trades
                    await self.maintain_tickers()
                    
                    # 2. Maintain matching SPY subscriptions for SPX positions
                    await self.manage_spy_subscriptions()
                    
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

        active_indices = [ticker.contract for ticker in active_tickers if ticker.contract.secType in ['IND', 'STK']]
        for required_index in [self.market_data_fetcher.spx, self.market_data_fetcher.spy]:
            if required_index not in active_indices:
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


    async def manage_spy_subscriptions(self):
        now = time.time()
        if not self.spy_strikes or now - self.spy_strikes_update_time > 24 * 3600:
            chains = await self.market_data_fetcher.get_chains(self.market_data_fetcher.spy)
            chain = next(c for c in chains if len(c.strikes) > 1)
            self.spy_strikes = sorted(chain.strikes)
            self.spy_strikes_update_time = now
            logger.info(f"SubscriptionManager: Successfully updated {len(self.spy_strikes)} SPY strikes.")

        """Check current SPX positions and update SPY subscriptions."""
        positions = self.trading_bot.get_short_options()
        # SPX options can have symbol 'SPX' or 'SPXW' (weekly)
        spx_positions = [p for p in positions if p.contract.symbol in ('SPX', 'SPXW')]
        current_spx_con_ids = {p.contract.conId for p in spx_positions}

        # 1. Identify new matching SPY options to subscribe
        new_spy_contracts_batch = []
        for position in spx_positions:
            spx_contract = position.contract
            if spx_contract.conId not in self.spx_to_spy_map:
                logger.info(f"No SPY contracts found for {get_option_name(spx_contract)}")
                spy_contracts = self.create_matching_spy_contracts(spx_contract)
                new_spy_contracts_batch.append((spx_contract, spy_contracts))

        if new_spy_contracts_batch:
            active_tickers = self.ib.wrapper.ticker2ReqId['mktData'].keys()
            active_spy_options = [ticker.contract for ticker in active_tickers if ticker.contract.symbol == 'SPY' and ticker.contract.secType == 'OPT']

            # Deduplicate by (strike, right) to create a set of unique contracts to subscribe
            unique_new_spy = {}
            for _, spy_pair in new_spy_contracts_batch:
                for spy in spy_pair:
                    key = (spy.strike, spy.right)
                    if key not in unique_new_spy:
                        unique_new_spy[key] = spy
            
            contracts_to_subscribe = []
            for required_spy_contract in unique_new_spy.values():
                is_contract_subscribed = False
                for active_contract in active_spy_options:
                    if active_contract is required_spy_contract:
                        is_contract_subscribed = True
                        break
                if not is_contract_subscribed:
                    logger.info(f"Option {get_spy_option_name(required_spy_contract)} is missing a ticker")
                    contracts_to_subscribe.append(required_spy_contract)

            if contracts_to_subscribe:
                logger.info(f"Subscribing to {len(contracts_to_subscribe)} unique new matching SPY options.")
                await self.market_data_fetcher.request_subscriptions(contracts_to_subscribe)
            
            for spx_contract, spy_pair in new_spy_contracts_batch:
                # Use the qualified instances from the unique_new_spy map
                qualified_pair = []
                for spy in spy_pair:
                    qualified_spy = unique_new_spy[(spy.strike, spy.right)]
                    if qualified_spy.conId and self.market_data_fetcher.get_ticker(qualified_spy):
                        qualified_pair.append(qualified_spy)
                
                if len(qualified_pair) == 2:
                    self.spx_to_spy_map[spx_contract.conId] = qualified_pair
                    logger.info(f"Subscribed to SPY hedge pair for SPX position {get_option_name(spx_contract)}")
                else:
                    logger.error(f"Failed to subscribe complete matching SPY pair for {get_option_name(spx_contract)}")

        # 2. Unsubscribe from SPY options for closed SPX positions
        closed_spx_con_ids = [con_id for con_id in list(self.spx_to_spy_map.keys()) if con_id not in current_spx_con_ids]
        for con_id in closed_spx_con_ids:
            spy_pair = self.spx_to_spy_map.pop(con_id)
            logger.info(f"SPX position {con_id} closed. Unsubscribing from SPY hedge pair.")
            for spy_contract in spy_pair:
                # Only cancel if no other SPX position is using this SPY contract
                is_in_use = any(spy_contract in pair for pair in self.spx_to_spy_map.values())
                if not is_in_use:
                    self.market_data_fetcher.cancel_market_data(spy_contract)

    def create_matching_spy_contracts(self, spx_contract):
        """Create a pair of matching SPY option contracts for a given SPX option contract."""
        target_strike = spx_contract.strike / 10.0
        
        # Find exact match or surrounding strikes
        if target_strike in self.spy_strikes:
            strikes = [target_strike, target_strike]
        else:
            # Find the closest strike below and above
            lower_strikes = [s for s in self.spy_strikes if s < target_strike]
            upper_strikes = [s for s in self.spy_strikes if s > target_strike]
            
            strikes = [
                max(lower_strikes) if lower_strikes else self.spy_strikes[0],
                min(upper_strikes) if upper_strikes else self.spy_strikes[-1]
            ]

        return [
            Option(
                symbol='SPY',
                lastTradeDateOrContractMonth=spx_contract.lastTradeDateOrContractMonth,
                strike=s,
                right=spx_contract.right,
                exchange='SMART',
                currency='USD'
            ) for s in strikes
        ]
