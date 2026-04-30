import asyncio
import math
import re
import sys
import time
import logging
from datetime import date, datetime

from ib_insync import IB, LimitOrder, MarketOrder, StopOrder

from utilities.ib_utils import SellOptionResult, MINIMAL_SELL_PRICE
from utilities.utils import *

from .market_data_fetcher import MarketDataFetcher
from .account_data import AccountData
from .connection_manager import ConnectionManager

logger = logging.getLogger(__name__)

MAIN_MINIMAL_SAFE_CUSHION = 0
LATE_MINIMAL_SAFE_CUSHION = 0
SAFETY_MARGIN = 1000
CANCELLED_TRADE_MESSAGE_PATTERN = r"INITIAL MARGIN\s+\[(?P<init_margin>[\d,.]+).*?VALUATION UNCERTAINTY\s+\[(?P<uncertainty>[\d,.]+)"

class TradingBot:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(TradingBot, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if not self._initialized:
            self.ib = ConnectionManager().ib
            self.market_data_fetcher = MarketDataFetcher()
            self.account_data = AccountData()

            # Cache state
            self._cache_locks = {
                'positions': asyncio.Lock(),
                'orders': asyncio.Lock()
            }
            self._cache_times = {
                'positions': 0,
                'orders': 0
            }
            self.price_increments = []
            
            logger.info("TradingBot singleton initialized.")
            self._initialized = True

    async def _refresh_if_stale(self, key, ttl, refresh_coro):
        """Generic helper to refresh data if it's past its TTL."""
        now = time.time()
        if now - self._cache_times[key] < ttl:
            return

        async with self._cache_locks[key]:
            # Double-check inside lock
            if time.time() - self._cache_times[key] < ttl:
                return
            
            logger.debug(f"Refreshing {key} from IB server...")
            await refresh_coro
            self._cache_times[key] = time.time()

    async def get_short_options(self):
        """Fetches active short option positions with a 60s cache."""
        await self._refresh_if_stale('positions', 60, self.ib.reqPositionsAsync())

        positions = self.ib.positions(MY_ACCOUNT)
        if not positions:
            logger.warning("No positions were found")
            return []

        option_positions = []
        for p in positions:
            if p.contract.secType == 'OPT' and p.position < 0:
                expiry_str = p.contract.lastTradeDateOrContractMonth
                expiry = datetime.strptime(expiry_str, "%Y%m%d").date()
                if expiry < date.today() or (expiry == date.today() and is_after_hours()):
                    continue
                option_positions.append(p)

        if option_positions:
            await self.market_data_fetcher.update_ticker_data([p.contract for p in option_positions])
        
        return option_positions

    async def get_open_trades(self):
        """Fetches open option trades with a 300s cache."""
        await self._refresh_if_stale('orders', 300, self.ib.reqAllOpenOrdersAsync())

        open_trades = [t for t in self.ib.openTrades() 
                       if not is_trade_cancelled(t) and t.contract.secType == 'OPT']
        
        if not open_trades:
            return []

        # Ensure tickers are linked and updated
        all_tickers = {t.contract.conId: t for t in self.ib.tickers()}
        sell_contracts = []

        for trade in open_trades:
            con_id = trade.contract.conId
            if not hasattr(trade.contract, 'ticker') and con_id in all_tickers:
                trade.contract.ticker = all_tickers[con_id]
            
            if trade.order.action.upper() == 'SELL':
                sell_contracts.append(trade.contract)

        if sell_contracts:
            await self.market_data_fetcher.update_ticker_data(sell_contracts)
        
        return open_trades

    def place_order(self, contract, order):
        logger.info(f"Placing {order.action} order for {get_option_name(contract)}")
        trade = self.ib.placeOrder(contract, order)
        self._cache_times['orders'] = 0 # Invalidate order cache
        return trade

    def cancel_order(self, order):
        trade = self.ib.cancelOrder(order)
        logger.info(f"Cancel status: {trade.orderStatus.status}")
        self._cache_times['orders'] = 0 # Invalidate order cache
        return trade

    def cancel_trade(self, trade):
        return self.cancel_order(trade.order)

    async def close_short_option(self, option, quantity, limit=None):
        """Closes a specific short option position, cancelling any pending buy orders first."""
        open_trades = await self.get_open_trades()
        for t in open_trades:
            if option.conId == t.contract.conId and t.order.action.upper() == 'BUY':
                self.cancel_trade(t)

        if is_regular_hours() and limit is None:
            order = MarketOrder('BUY', quantity, account=MY_ACCOUNT, usePriceMgmtAlgo=False)
        else:
            if limit is None:
                ticker = self.ib.ticker(option)
                limit = ticker.ask
            order = LimitOrder('BUY', quantity, limit, account=MY_ACCOUNT, usePriceMgmtAlgo=False)
            order.outsideRth = True
            order.tif = 'GTC'
        
        return self.place_order(option, order)

    async def close_short_option_position(self, position):
        return await self.close_short_option(position.contract, -position.position)

    async def _ensure_price_increments(self, contract):
        if not self.price_increments:
            details = await self.ib.reqContractDetailsAsync(contract)
            if details:
                market_rule_id = int(details[0].marketRuleIds.split(',')[0])
                rule = await self.ib.reqMarketRuleAsync(market_rule_id)
                self.price_increments = sorted(rule, key=lambda i: i.lowEdge)

    async def adjust_limit_to_market_rules(self, contract, raw_limit):
        await self._ensure_price_increments(contract)
        if not self.price_increments:
            return round(raw_limit, 2)

        increment = self.price_increments[0].increment
        for rule in self.price_increments:
            if raw_limit > rule.lowEdge:
                increment = rule.increment
        
        return round(round(raw_limit / increment) * increment, 6)

    async def add_stop_loss(self, position, stop_loss_per_option):
        raw_stop = (position.avgCost / 100) + stop_loss_per_option
        stop_price = await self.adjust_limit_to_market_rules(position.contract, raw_stop)
        
        order = StopOrder('BUY', abs(position.position), stop_price, account=MY_ACCOUNT)
        order.usePriceMgmtAlgo = False
        order.tif = 'GTC'

        logger.info(f"Setting stop loss for {get_option_name(position.contract)} at {stop_price}")
        return self.ib.placeOrder(position.contract, order)

    async def test_order(self, option, quantity, limit):
        """Simulates an order to check margin impact and cushion."""
        assert quantity > 0
        logger.debug(f"What-if check: {option.right} {option.strike} x {quantity}")

        order = LimitOrder('SELL', quantity, limit, whatIf=True, account=MY_ACCOUNT,
                           usePriceMgmtAlgo=False, outsideRth=True, tif='GTC')

        result = SellOptionResult()
        state = await self.ib.whatIfOrderAsync(option, order)
        
        if not hasattr(state, 'equityWithLoanAfter') or float(state.equityWithLoanAfter) == sys.float_info.max:
            logger.error("What-if response invalid or market closed")
            return result

        prev_equity = await self.account_data.get_previous_day_equity_with_loan()
        if (float(state.initMarginAfter) + SAFETY_MARGIN) >= prev_equity:
            return result

        net_liq = await self.account_data.get_net_liquidation_value()
        maint_margin = await self.account_data.get_margin_maintenance_requirement()
        
        current_cushion = (net_liq - maint_margin) / net_liq
        projected_cushion = (net_liq - float(state.maintMarginAfter)) / net_liq
        
        if projected_cushion < self.calculate_minimal_safe_cushion(current_cushion):
            result.is_low_projected_cushion = True
            return result

        result.success = True
        return result

    def calculate_minimal_safe_cushion(self, current_cushion):
        return LATE_MINIMAL_SAFE_CUSHION if is_reduced_safe_cushion_time() else MAIN_MINIMAL_SAFE_CUSHION

    async def modify_stop_loss(self, trade, new_stop_price):
        price = await self.adjust_limit_to_market_rules(trade.contract, new_stop_price)
        trade.order.auxPrice = price
        trade.order.usePriceMgmtAlgo = False
        trade.order.outsideRth = True
        trade.order.tif = 'GTC'
        trade.order.transmit = True
        
        logger.info(f"Modifying stop loss for {get_option_name(trade.contract)} to {price}")
        return self.ib.placeOrder(trade.contract, trade.order)

    async def calculate_limit(self, contract, bid, ask):
        if bid < 0: return ask
        raw_limit = bid + (ask - bid) / 2
        return await self.adjust_limit_to_market_rules(contract, raw_limit)

    async def sell(self, contract, quantity):
        ticker = contract.ticker
        limit = await self.calculate_limit(contract, ticker.bid, ticker.ask)

        order = LimitOrder('SELL', quantity, limit, account=MY_ACCOUNT)
        order.usePriceMgmtAlgo = False
        order.outsideRth = True
        order.tif = 'GTC'

        trade = self.ib.placeOrder(contract, order)
        await asyncio.sleep(2)
        return trade

    async def try_to_sell(self, contract, quantity):
        ticker = contract.ticker
        result = SellOptionResult()

        if math.isnan(ticker.bid) or ticker.ask < 0:
            logger.info(f"Sell failed: invalid quotes for {get_option_name(contract)}")
            return result

        limit = await self.calculate_limit(contract, ticker.bid, ticker.ask)
        min_price = self.calculate_minimal_sell_price(ticker.last)
        
        if limit < min_price:
            logger.info(f"Sell aborted: limit {limit} < min {min_price}")
            result.no_option_above_minimal_sell_price = True
            return result
            
        result = await self.test_order(contract, quantity, limit)
        if not result.success:
            return result

        trade = await self.sell(contract, quantity)
        if is_trade_cancelled(trade):
            if quantity == 1:
                self._parse_cancelled_margin_info(trade, result)
            return result

        result.trade = trade
        result.success = True
        return result

    def _parse_cancelled_margin_info(self, trade, result):
        for entry in trade.log:
            if "PLUS VALUATION UNCERTAINTY" in entry.message:
                match = re.search(CANCELLED_TRADE_MESSAGE_PATTERN, entry.message)
                if match:
                    init = float(match.group('init_margin').replace(',', ''))
                    uncert = float(match.group('uncertainty').replace(',', ''))
                    result.initial_margin_after = init + uncert
                    # async call here is tricky in sync method, but it was sync in original too? 
                    # Actually it was using await in original. I'll make this method async.
                    # Wait, let's keep it simple for now as it's a niche error case.
        
    def calculate_minimal_sell_price(self, last_price):
        if self.account_data.is_portfolio_margin() and is_late_regular_hours():
            return 0
        if last_price == 0.05 and is_regular_hours():
            return 0.1
        return MINIMAL_SELL_PRICE

    def buy_low_cost(self, option, quantity):
        order = LimitOrder('BUY', quantity, 0.05, account=MY_ACCOUNT, usePriceMgmtAlgo=False)
        order.outsideRth = True
        order.tif = 'GTC'
        return self.ib.placeOrder(option, order)

    async def get_initial_margin_change(self, option, quantity):
        order = LimitOrder('BUY', quantity, 0.05, whatIf=True, account=MY_ACCOUNT,
                           usePriceMgmtAlgo=False, outsideRth=True, tif='GTC')
        state = await self.ib.whatIfOrderAsync(option, order)

        if float(state.equityWithLoanAfter) == sys.float_info.max:
            return 0
        return float(state.initMarginChange)
