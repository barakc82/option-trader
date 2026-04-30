import asyncio
import math
import re
import sys
import time
import logging
from datetime import date, datetime
from typing import Optional, List, Union, Dict, Any

from ib_insync import IB, LimitOrder, MarketOrder, StopOrder, Contract, Position, Trade, Ticker

from utilities.ib_utils import SellOptionResult, MINIMAL_SELL_PRICE
from utilities.utils import *

from .market_data_fetcher import MarketDataFetcher
from .account_data import AccountData
from .connection_manager import ConnectionManager

logger = logging.getLogger(__name__)

MAIN_MINIMAL_SAFE_CUSHION = 0.0
LATE_MINIMAL_SAFE_CUSHION = 0.0
SAFETY_MARGIN = 1000.0
CANCELLED_TRADE_MESSAGE_PATTERN = r"INITIAL MARGIN\s+\[(?P<init_margin>[\d,.]+).*?VALUATION UNCERTAINTY\s+\[(?P<uncertainty>[\d,.]+)"

class TradingBot:
    """
    Singleton manager for high-level trading operations.
    Handles position management, order placement, and margin checks.
    """
    _instance: Optional['TradingBot'] = None

    def __new__(cls) -> 'TradingBot':
        if cls._instance is None:
            cls._instance = super(TradingBot, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
            
        self.ib = ConnectionManager().ib
        self.market_data_fetcher = MarketDataFetcher()
        self.account_data = AccountData()

        self.req_all_open_orders_lock = asyncio.Lock()
        self.req_positions_lock = asyncio.Lock()
        self.last_request_positions_time = 0.0
        self.last_request_all_open_trades_time = 0.0
        self.price_increments: List[Any] = []
        
        logger.info("TradingBot singleton initialized.")
        self._initialized = True

    async def get_short_options(self) -> List[Position]:
        """Fetches active short option positions with a 60s cache."""
        should_use_cache = time.time() - self.last_request_positions_time < 60
        if not should_use_cache and not self.req_positions_lock.locked():
            async with self.req_positions_lock:
                logger.debug("Requesting fresh positions from IB server...")
                await self.ib.reqPositionsAsync()
                self.last_request_positions_time = time.time()

        positions = self.ib.positions(MY_ACCOUNT)
        if not positions:
            logger.warning("No positions were found")
            return []

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

    async def get_open_trades(self) -> List[Trade]:
        """Fetches open option trades with a 300s cache."""
        should_use_cache = time.time() - self.last_request_all_open_trades_time < 300
        if not should_use_cache:
            async with self.req_all_open_orders_lock:
                await self.ib.reqAllOpenOrdersAsync()
            self.last_request_all_open_trades_time = time.time()

        open_trades = [t for t in self.ib.openTrades() if not is_trade_cancelled(t) and t.contract.secType == 'OPT']
        
        # Link tickers
        tickers = {t.contract.conId: t for t in self.ib.tickers()}
        for trade in open_trades:
            if not hasattr(trade.contract, 'ticker'):
                con_id = trade.contract.conId
                if con_id in tickers:
                    trade.contract.ticker = tickers[con_id]

        # Update sell trade tickers
        sell_trades = [t for t in open_trades if t.order.action.upper() == 'SELL']
        if sell_trades:
            await self.market_data_fetcher.update_ticker_data([t.contract for t in sell_trades])
        
        return open_trades

    def place_order(self, contract: Contract, order: Union[LimitOrder, MarketOrder, StopOrder]) -> Trade:
        """Places an order and invalidates the open trades cache."""
        logger.info(f"Placing {order.action} order for {get_option_name(contract)}")
        trade = self.ib.placeOrder(contract, order)
        self.last_request_all_open_trades_time = 0.0
        return trade

    def cancel_order(self, order: Union[LimitOrder, MarketOrder, StopOrder]) -> Trade:
        """Cancels an order and invalidates the open trades cache."""
        trade = self.ib.cancelOrder(order)
        logger.info(f"Status of cancel: {trade.orderStatus.status}")
        self.last_request_all_open_trades_time = 0.0
        return trade

    def cancel_trade(self, trade: Trade) -> Trade:
        """Helper to cancel a Trade's underlying order."""
        return self.cancel_order(trade.order)

    async def close_short_option(self, option: Contract, quantity: float, limit: Optional[float] = None) -> Trade:
        """Closes a short option position, cancelling existing BUY orders first."""
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

    async def close_short_option_position(self, position: Position) -> Trade:
        """Helper to close an entire short position."""
        return await self.close_short_option(position.contract, -position.position)

    async def verify_price_increments_exist(self, contract: Contract) -> None:
        """Ensures the price_increments cache is populated for the given contract."""
        if not self.price_increments:
            details = await self.ib.reqContractDetailsAsync(contract)
            if details:
                market_rule_id = int(details[0].marketRuleIds.split(',')[0])
                rule = await self.ib.reqMarketRuleAsync(market_rule_id)
                self.price_increments = sorted(rule, key=lambda i: i.lowEdge)

    async def adjust_limit_to_market_rules(self, contract: Contract, raw_limit: float) -> float:
        """Adjusts a raw limit price to comply with exchange tick size rules."""
        await self.verify_price_increments_exist(contract)
        if not self.price_increments:
            return round(raw_limit, 2)
            
        current_increment = self.price_increments[0].increment
        for i in self.price_increments:
            if raw_limit > i.lowEdge:
                current_increment = i.increment
        return round(round(raw_limit / current_increment) * current_increment, 6)

    async def add_stop_loss(self, position: Position, stop_loss_per_option: float) -> Trade:
        """Adds a GTC stop loss order for a position."""
        raw_stop = position.avgCost / 100 + stop_loss_per_option
        stop_price = await self.adjust_limit_to_market_rules(position.contract, raw_stop)
        
        order = StopOrder('BUY', abs(position.position), stop_price, account=MY_ACCOUNT)
        order.usePriceMgmtAlgo = False
        order.tif = 'GTC'

        logger.info(f"Adding stop loss for {get_option_name(position.contract)} at {stop_price}")
        return self.ib.placeOrder(position.contract, order)

    async def test_order(self, option: Contract, number_of_options: float, limit: float) -> SellOptionResult:
        """Performs a what-if order check to validate margin impact."""
        assert number_of_options > 0
        logger.debug(f"Checking {option.right} {option.strike} for {number_of_options} options")

        order = LimitOrder('SELL', number_of_options, limit, whatIf=True, account=MY_ACCOUNT,
                           usePriceMgmtAlgo=False, outsideRth=True, tif='GTC')

        result = SellOptionResult()
        order_state = await self.ib.whatIfOrderAsync(option, order)
        
        if not hasattr(order_state, 'equityWithLoanAfter') or float(order_state.equityWithLoanAfter) == sys.float_info.max:
            logger.error("What-if check failed: invalid data or market closed.")
            return result

        init_margin_after = float(order_state.initMarginAfter)
        previous_day_equity = await self.account_data.get_previous_day_equity_with_loan()
        
        if (init_margin_after + SAFETY_MARGIN) >= previous_day_equity:
            return result

        maint_margin_after = float(order_state.maintMarginAfter)
        net_liq = await self.account_data.get_net_liquidation_value()
        maint_req = await self.account_data.get_margin_maintenance_requirement()
        
        current_cushion = (net_liq - maint_req) / net_liq
        projected_cushion = (net_liq - maint_margin_after) / net_liq
        
        if projected_cushion < self.calculate_minimal_safe_cushion(current_cushion):
            result.is_low_projected_cushion = True
            return result

        result.success = True
        return result

    def calculate_minimal_safe_cushion(self, current_cushion: float) -> float:
        """Returns the minimal safe cushion based on the time of day."""
        return LATE_MINIMAL_SAFE_CUSHION if is_reduced_safe_cushion_time() else MAIN_MINIMAL_SAFE_CUSHION

    async def modify_stop_loss(self, stop_loss_trade: Trade, new_stop_loss: float) -> Trade:
        """Updates an existing stop loss order."""
        stop_loss_price = await self.adjust_limit_to_market_rules(stop_loss_trade.contract, new_stop_loss)
        stop_loss_trade.order.auxPrice = stop_loss_price
        stop_loss_trade.order.usePriceMgmtAlgo = False
        stop_loss_trade.order.outsideRth = True
        stop_loss_trade.order.tif = 'GTC'
        stop_loss_trade.order.transmit = True
        logger.info(f"Modifying a stop loss order for {get_option_name(stop_loss_trade.contract)}")
        return self.ib.placeOrder(stop_loss_trade.contract, stop_loss_trade.order)

    async def calculate_limit(self, contract: Contract, bid: float, ask: float) -> float:
        """Calculates a mid-point limit price adjusted for market rules."""
        if bid < 0:
            return ask
        raw_limit = bid + (ask - bid) / 2
        return await self.adjust_limit_to_market_rules(contract, raw_limit)

    async def sell(self, contract: Contract, quantity: float) -> Trade:
        """Executes a sell order."""
        ticker = contract.ticker
        assert ticker
        limit = await self.calculate_limit(contract, ticker.bid, ticker.ask)

        order = LimitOrder('SELL', quantity, limit, account=MY_ACCOUNT)
        order.usePriceMgmtAlgo = False
        order.outsideRth = True
        order.tif = 'GTC'

        trade = self.ib.placeOrder(contract, order)
        await asyncio.sleep(2)
        return trade

    async def try_to_sell(self, contract: Contract, quantity: float) -> SellOptionResult:
        """High-level attempt to sell an option, including validation and what-if checks."""
        ticker = contract.ticker
        assert ticker

        result = SellOptionResult()
        if math.isnan(ticker.bid) or ticker.ask < 0:
            logger.info(f"Sell of {get_option_name(contract)} failed: bid={ticker.bid}, ask={ticker.ask}")
            return result

        limit = await self.calculate_limit(contract, ticker.bid, ticker.ask)
        minimal_sell_price = self.calculate_minimal_sell_price(ticker.last)
        if limit < minimal_sell_price:
            logger.info(f"Sell of {get_option_name(contract)} limit ({limit}) < min price ({minimal_sell_price})")
            result.no_option_above_minimal_sell_price = True
            return result
            
        result = await self.test_order(contract, quantity, limit)
        if not result.success:
            return result

        trade = await self.sell(contract, quantity)
        if is_trade_cancelled(trade):
            if quantity == 1:
                self._handle_cancelled_trade_info(trade, result)
            return result

        result.trade = trade
        result.success = True
        return result

    def _handle_cancelled_trade_info(self, trade: Trade, result: SellOptionResult) -> None:
        """Extracts margin info from a cancelled trade's log."""
        for entry in trade.log:
            if "PLUS VALUATION UNCERTAINTY" in entry.message:
                match = re.search(CANCELLED_TRADE_MESSAGE_PATTERN, entry.message)
                if match:
                    init = float(match.group('init_margin').replace(',', ''))
                    uncert = float(match.group('uncertainty').replace(',', ''))
                    result.initial_margin_after = init + uncert
                    break

    def calculate_minimal_sell_price(self, last_price: float) -> float:
        """Determines the minimum price for selling based on account type and time."""
        if self.account_data.is_portfolio_margin() and is_late_regular_hours():
            return 0.0
        if last_price == 0.05 and is_regular_hours():
            return 0.1
        return MINIMAL_SELL_PRICE

    def buy_low_cost(self, option: Contract, quantity: float) -> Trade:
        """Places a low-cost buy order (e.g., for margin relaxation)."""
        order = LimitOrder('BUY', quantity, 0.05, account=MY_ACCOUNT, usePriceMgmtAlgo=False)
        order.outsideRth = True
        order.tif = 'GTC'
        return self.ib.placeOrder(option, order)

    async def get_initial_margin_change(self, option: Contract, quantity: float) -> float:
        """Calculates the projected initial margin change for a potential buy order."""
        order = LimitOrder('BUY', quantity, 0.05, whatIf=True, account=MY_ACCOUNT,
                           usePriceMgmtAlgo=False, outsideRth=True, tif='GTC')
        order_state = await self.ib.whatIfOrderAsync(option, order)

        if float(order_state.equityWithLoanAfter) == sys.float_info.max:
            logger.error("What-if check for margin change failed.")
            return 0.0

        return float(order_state.initMarginChange)
