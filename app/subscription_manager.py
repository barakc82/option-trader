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

        active_indices = [ticker.contract for ticker in active_tickers if ticker.contract.secType == 'IND']
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
        """Check current SPX positions and update SPY subscriptions."""
        positions = self.trading_bot.get_short_options()
        # SPX options can have symbol 'SPX' or 'SPXW' (weekly)
        spx_positions = [p for p in positions if p.contract.symbol in ('SPX', 'SPXW')]
        current_spx_con_ids = {p.contract.conId for p in spx_positions}

        # 1. Identify new matching SPY options to subscribe
        new_spy_contracts = []
        for position in spx_positions:
            spx_contract = position.contract
            if spx_contract.conId not in self.spx_to_spy_map:
                logger.info(f"No SPY contract found for {get_option_name(spx_contract)}")
                spy_contract = self.create_matching_spy_contract(spx_contract)
                new_spy_contracts.append((spx_contract, spy_contract))
        
        if new_spy_contracts:
            contracts_to_subscribe = [spy for spx, spy in new_spy_contracts]
            logger.info(f"Subscribing to {len(contracts_to_subscribe)} new matching SPY options.")
            
            await self.market_data_fetcher.request_subscriptions(contracts_to_subscribe)
            
            for spx_contract, spy_contract in new_spy_contracts:
                if spy_contract.conId and self.market_data_fetcher.get_ticker(spy_contract):
                    self.spx_to_spy_map[spx_contract.conId] = spy_contract
                    logger.info(f"Subscribed to SPY hedge {get_spy_option_name(spy_contract)} for SPX position {get_option_name(spx_contract)}")
                else:
                    logger.error(f"Failed to subscribe matching SPY option for {get_option_name(spx_contract)}")

        # 2. Unsubscribe from SPY options for closed SPX positions
        closed_spx_con_ids = [con_id for con_id in list(self.spx_to_spy_map.keys()) if con_id not in current_spx_con_ids]
        for con_id in closed_spx_con_ids:
            spy_contract = self.spx_to_spy_map.pop(con_id)
            logger.info(f"SPX position {con_id} closed. Unsubscribing from SPY hedge {get_spy_option_name(spy_contract)}.")
            self.market_data_fetcher.cancel_market_data(spy_contract)

    def create_matching_spy_contract(self, spx_contract):
        """Create a matching SPY option contract for a given SPX option contract."""
        spy_strike_raw = spx_contract.strike / 10.0

        if spy_strike_raw == int(spy_strike_raw):
            spy_strike = spy_strike_raw
        else:
            if spx_contract.right == "P":
                spy_strike = math.ceil(spy_strike_raw)
            else:  # "C"
                spy_strike = math.floor(spy_strike_raw)
        
        return Option(
            symbol='SPY',
            lastTradeDateOrContractMonth=spx_contract.lastTradeDateOrContractMonth,
            strike=spy_strike,
            right=spx_contract.right,
            exchange='SMART',
            currency='USD'
        )
