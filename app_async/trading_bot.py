import asyncio
import math
import re
import sys
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
            # Accessing shared singleton dependencies
            self.ib = ConnectionManager().ib
            self.market_data_fetcher = MarketDataFetcher()
            self.account_data = AccountData()

            self.req_all_open_orders_lock = asyncio.Lock()
            self.req_positions_lock = asyncio.Lock()
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
                    async with self.req_positions_lock:
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
            async with self.req_all_open_orders_lock:
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
        logger.info(f"Status of cancel: {trade.orderStatus.status}")
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

    async def verify_price_increments_exist(self, contract):
        if not self.price_increments:
            details = await self.ib.reqContractDetailsAsync(contract)
            market_rule_id = int(details[0].marketRuleIds.split(',')[0])
            rule = await self.ib.reqMarketRuleAsync(market_rule_id)
            self.price_increments = sorted(rule, key=lambda i: i.lowEdge)

    async def adjust_limit_to_market_rules(self, contract, raw_limit):
        await self.verify_price_increments_exist(contract)
        current_increment = self.price_increments[0].increment
        for i in self.price_increments:
            if raw_limit > i.lowEdge:
                current_increment = i.increment
        return round(round(raw_limit / current_increment) * current_increment, 6)

    async def add_stop_loss(self, position, stop_loss_per_option):
        raw_stop = position.avgCost / 100 + stop_loss_per_option
        stop_price = await self.adjust_limit_to_market_rules(position.contract, raw_stop)
        
        order = StopOrder('BUY', abs(position.position), stop_price, account=MY_ACCOUNT)
        order.usePriceMgmtAlgo = False
        order.tif = 'GTC'

        logger.info(f"Adding stop loss for {get_option_name(position.contract)} at {stop_price}")
        return self.ib.placeOrder(position.contract, order)

    async def test_order(self, option, number_of_options, limit):
        assert number_of_options > 0
        logger.debug(f"Checking {option.right} {option.strike} for {number_of_options} options")

        order = LimitOrder('SELL', number_of_options, limit, whatIf=True, account=MY_ACCOUNT,
                           usePriceMgmtAlgo=False, outsideRth=True, tif='GTC')

        result = SellOptionResult()
        order_state = await self.ib.whatIfOrderAsync(option, order)
        
        if not hasattr(order_state, 'equityWithLoanAfter'):
            logger.error(f"ib.whatIfOrderAsync returned an Order state with no 'equityWithLoanAfter' field.")
            return result

        if float(order_state.equityWithLoanAfter) == sys.float_info.max:
            logger.error(f"Response has no real data, the market is probably closed")
            return result

        init_margin_after = float(order_state.initMarginAfter)
        previous_day_equity_with_loan = await self.account_data.get_previous_day_equity_with_loan()
        safe_init_margin_after = init_margin_after + SAFETY_MARGIN
        is_order_possible = safe_init_margin_after < previous_day_equity_with_loan

        maintenance_margin_after = float(order_state.maintMarginAfter)
        net_liquidation_value = await self.account_data.get_net_liquidation_value()
        margin_maintenance_requirement = await self.account_data.get_margin_maintenance_requirement()
        
        current_cushion = (net_liquidation_value - margin_maintenance_requirement) / net_liquidation_value
        projected_cushion = (net_liquidation_value - maintenance_margin_after) / net_liquidation_value
        
        minimal_safe_cushion = self.calculate_minimal_safe_cushion(current_cushion)
        if projected_cushion < minimal_safe_cushion:
            result.is_low_projected_cushion = True
            return result

        result.success = is_order_possible
        return result

    def calculate_minimal_safe_cushion(self, current_cushion):
        if is_reduced_safe_cushion_time():
            return LATE_MINIMAL_SAFE_CUSHION
        return MAIN_MINIMAL_SAFE_CUSHION

    async def modify_stop_loss(self, stop_loss_trade, new_stop_loss):
        stop_loss_price = await self.adjust_limit_to_market_rules(stop_loss_trade.contract, new_stop_loss)
        stop_loss_trade.order.auxPrice = stop_loss_price
        stop_loss_trade.order.usePriceMgmtAlgo = False
        stop_loss_trade.order.outsideRth = True
        stop_loss_trade.order.tif = 'GTC'
        stop_loss_trade.order.transmit = True
        logger.info(f"Modifying a stop loss order for {get_option_name(stop_loss_trade.contract)}")
        trade = self.ib.placeOrder(stop_loss_trade.contract, stop_loss_trade.order)
        return trade

    async def calculate_limit(self, contract, bid, ask):
        assert not math.isnan(bid)
        assert not math.isnan(ask)

        if bid < 0:
            return ask
        spread = ask - bid
        raw_limit = bid + spread / 2
        return await self.adjust_limit_to_market_rules(contract, raw_limit)

    async def sell(self, contract, quantity):
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

    async def try_to_sell(self, contract, quantity):
        ticker = contract.ticker
        assert ticker

        result = SellOptionResult()
        result.success = False
        result.no_option_above_minimal_sell_price = False

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
        is_cancelled = is_trade_cancelled(trade)
        if is_cancelled and quantity == 1:
            for trade_log_entry in trade.log:
                if "PLUS VALUATION UNCERTAINTY" in trade_log_entry.message:
                    match = re.search(CANCELLED_TRADE_MESSAGE_PATTERN, trade_log_entry.message)
                    init_margin_after = float(match.group('init_margin').replace(',', ''))
                    valuation_uncertainty = float(match.group('uncertainty').replace(',', ''))
                    logger.info(f"Initial margin: {init_margin_after}, valuation uncertainty: {valuation_uncertainty}")
                    result.required_initial_margin = await self.account_data.get_previous_day_equity_with_loan()
                    result.initial_margin_after = init_margin_after + valuation_uncertainty
                    break

        if not is_cancelled:
            result.trade = trade

        result.success = not is_cancelled
        return result

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
        trade = self.ib.placeOrder(option, order)
        return trade

    async def get_initial_margin_change(self, option, quantity):
        order = LimitOrder('BUY', quantity, 0.05, whatIf=True, account=MY_ACCOUNT,
                           usePriceMgmtAlgo=False, outsideRth=True, tif='GTC')
        order_state = await self.ib.whatIfOrderAsync(option, order)

        if float(order_state.equityWithLoanAfter) == sys.float_info.max:
            logger.error(f"Response has no real data, the market is probably closed")
            return 0

        return float(order_state.initMarginChange)
