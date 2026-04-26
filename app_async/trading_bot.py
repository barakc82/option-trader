import asyncio
import time
import logging
from datetime import date, datetime

from ib_insync import IB, LimitOrder, MarketOrder, StopOrder

from utilities.utils import *
from .market_data_fetcher import MarketDataFetcher
from .connection_manager import ConnectionManager

logger = logging.getLogger(__name__)

class TradingBot:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(TradingBot, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if not self._initialized:
            # Accessing shared singleton dependencies
            self.ib = ConnectionManager().ib
            self.market_data_fetcher = MarketDataFetcher()
            
            self.last_request_all_open_trades_time = 0
            self.price_increments = []
            logger.info("TradingBot singleton initialized.")
            self._initialized = True

    async def get_short_options(self, should_use_cache=True):
        for attempt in range(2):
            if not should_use_cache or attempt == 1:
                original_timeout = self.ib.RequestTimeout
                self.ib.RequestTimeout = 10.0
                try:
                    await self.ib.reqPositionsAsync()
                except TimeoutError:
                    logger.warning("reqPositions timed out")
                finally:
                    await asyncio.sleep(2)
                    self.ib.RequestTimeout = original_timeout

            positions = self.ib.positions(MY_ACCOUNT)
            if positions or not should_use_cache:
                break
            logger.info("No positions in cache, retrying...")

        option_positions = []
        for position in positions:
            if position.contract.secType == 'OPT' and position.position < 0:
                expiry = datetime.strptime(position.contract.lastTradeDateOrContractMonth, "%Y%m%d").date()
                if expiry < date.today() or (expiry == date.today() and is_after_hours()):
                    continue
                option_positions.append(position)

        options = [p.contract for p in option_positions]
        if options:
            await self.market_data_fetcher.update_ticker_data(options)
        return option_positions

    async def get_open_trades(self):
        should_use_cache = time.time() - self.last_request_all_open_trades_time < 300
        if not should_use_cache:
            await self.ib.reqAllOpenOrdersAsync()
            self.last_request_all_open_trades_time = time.time()

        open_trades = [t for t in self.ib.openTrades() if not is_trade_cancelled(t) and t.contract.secType == 'OPT']
        
        # Link tickers
        tickers = self.ib.tickers()
        for trade in open_trades:
            if not hasattr(trade.contract, 'ticker'):
                for t in tickers:
                    if t.contract.conId == trade.contract.conId:
                        trade.contract.ticker = t
                        break

        # Update sell trade tickers
        sell_trades = [t for t in open_trades if t.order.action.upper() == 'SELL']
        if sell_trades:
            await self.market_data_fetcher.update_ticker_data([t.contract for t in sell_trades])
        
        return open_trades

    def place_order(self, contract, order):
        logger.info(f"Placing {order.action} order for {get_option_name(contract)}")
        trade = self.ib.placeOrder(contract, order)
        self.last_request_all_open_trades_time = 0
        return trade

    def cancel_order(self, order):
        trade = self.ib.cancelOrder(order)
        self.last_request_all_open_trades_time = 0
        return trade

    def cancel_trade(self, trade):
        return self.cancel_order(trade.order)

    async def close_short_option(self, option, quantity):
        open_trades = await self.get_open_trades()
        for t in open_trades:
            if option.conId == t.contract.conId and t.order.action.upper() == 'BUY':
                self.cancel_trade(t)

        ticker = self.ib.ticker(option)
        if is_regular_hours():
            order = MarketOrder('BUY', quantity, account=MY_ACCOUNT, usePriceMgmtAlgo=False)
        else:
            order = LimitOrder('BUY', quantity, ticker.ask, account=MY_ACCOUNT, usePriceMgmtAlgo=False)
            order.outsideRth = True
            order.tif = 'GTC'
        
        return self.place_order(option, order)

    async def close_short_option_position(self, position):
        return await self.close_short_option(position.contract, -position.position)

    def verify_price_increments_exist(self, contract):
        if not self.price_increments:
            details = self.ib.reqContractDetails(contract)
            market_rule_id = int(details[0].marketRuleIds.split(',')[0])
            rule = self.ib.reqMarketRule(market_rule_id)
            self.price_increments = sorted(rule, key=lambda i: i.lowEdge)

    def adjust_limit_to_market_rules(self, raw_limit):
        current_increment = self.price_increments[0].increment
        for i in self.price_increments:
            if raw_limit > i.lowEdge:
                current_increment = i.increment
        return round(round(raw_limit / current_increment) * current_increment, 6)

    def add_stop_loss(self, position, stop_loss_per_option):
        self.verify_price_increments_exist(position.contract)
        raw_stop = position.avgCost / 100 + stop_loss_per_option
        stop_price = self.adjust_limit_to_market_rules(raw_stop)
        
        order = StopOrder('BUY', abs(position.position), stop_price, account=MY_ACCOUNT)
        order.usePriceMgmtAlgo = False
        order.tif = 'GTC'

        logger.info(f"Adding stop loss for {get_option_name(position.contract)} at {stop_price}")
        return self.ib.placeOrder(position.contract, order)
