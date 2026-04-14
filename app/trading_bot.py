import sys
import math
import re

from datetime import date

from ib_insync import LimitOrder, MarketOrder, StopOrder, StopLimitOrder

from account_data import AccountData
from ib_utils import SellOptionResult, MINIMAL_SELL_PRICE
from profit_monitor import on_sell_fill
from utils import *

MAIN_MINIMAL_SAFE_CUSHION = 0
# MAIN_MINIMAL_SAFE_CUSHION = 0.2
LATE_MINIMAL_SAFE_CUSHION = 0
# LATE_MINIMAL_SAFE_CUSHION = 0.15
SAFETY_MARGIN = 2000

CANCELLED_TRADE_MESSAGE_PATTERN = r"INITIAL MARGIN\s+\[(?P<init_margin>[\d,.]+).*?VALUATION UNCERTAINTY\s+\[(?P<uncertainty>[\d,.]+)"

logger = logging.getLogger(__name__)


def calculate_minimal_safe_cushion(current_cushion):
    if is_reduced_safe_cushion_time():
        return LATE_MINIMAL_SAFE_CUSHION

    if MAIN_MINIMAL_SAFE_CUSHION - 0.01 < current_cushion < MAIN_MINIMAL_SAFE_CUSHION:
        return current_cushion

    return MAIN_MINIMAL_SAFE_CUSHION


class TradingBot:
    def __init__(self):
        self.initial_margin_requirement = None
        self.equity_with_loan = None
        self.ib = current_thread.ib
        self.account_data = AccountData()
        self.market_data_fetcher = current_thread.market_data_fetcher
        self.price_increments = []
        self.last_request_all_open_trades_time = 0

    def test_order(self, option, number_of_options, limit):

        assert number_of_options > 0
        logger.debug(f"Checking {option.right} {option.strike} for {number_of_options} options")

        order = LimitOrder('SELL', number_of_options, limit, whatIf=True, account=MY_ACCOUNT,
                           usePriceMgmtAlgo=False, outsideRth=True, tif='GTC')

        result = SellOptionResult()
        order_state = self.ib.whatIfOrder(option, order)
        if not hasattr(order_state, 'equityWithLoanAfter'):
            logger.error(f"ib.whatIfOrder returned an Order state with no 'equityWithLoanAfter' field. "
                         f"Option {get_option_name(option)}, Contract ID: {option.conId}, "
                         f"number of options: {number_of_options}, limit: {limit}")
            return result

        if float(order_state.equityWithLoanAfter) == sys.float_info.max:
            logger.error(f"Response has no real data, the market is probably closed")
            return result

        valuation_uncertainty = 0
        if order_state.warningText:
            logger.warning(f"  Warning Text:           {order_state.warningText}")

        init_margin_after = float(order_state.initMarginAfter)
        previous_day_equity_with_loan = self.account_data.get_previous_day_equity_with_loan()
        safe_init_margin_after = init_margin_after + SAFETY_MARGIN + valuation_uncertainty
        is_order_possible = safe_init_margin_after < previous_day_equity_with_loan

        maintenance_margin_after = float(order_state.maintMarginAfter)
        net_liquidation_value = self.account_data.get_net_liquidation_value()
        margin_maintenance_requirement = self.account_data.get_margin_maintenance_requirement()
        logger.info(f"Margin maintenance requirement: {margin_maintenance_requirement:.2f}")
        logger.info(f"Margin maintenance after: {maintenance_margin_after:.2f}")
        current_cushion = (net_liquidation_value - margin_maintenance_requirement) / net_liquidation_value
        projected_cushion = (net_liquidation_value - maintenance_margin_after) / net_liquidation_value
        logger.info(f"Current calculated cushion: {current_cushion:.2f}, projected Cushion: {projected_cushion:.2f}")

        minimal_safe_cushion = calculate_minimal_safe_cushion(current_cushion)
        if projected_cushion < minimal_safe_cushion:
            logger.info(
                f"The projected cushion ({projected_cushion:.2f}) is less than the minimal safe cushion requirement ({minimal_safe_cushion})")
            result.is_low_projected_cushion = True
            return result

        if is_order_possible:
            logger.debug(f"Can transmit {get_option_name(option)} for {number_of_options} options")
            if number_of_options == 1:
                logger.info(
                    f"previous_day_equity_with_loan: {previous_day_equity_with_loan}, init_margin_after: {init_margin_after}")
        else:
            logger.info(
                f"Cannot transmit {get_option_name(option)} for {number_of_options} options since the previous day equity with load ({previous_day_equity_with_loan}) "
                f"is lower than the safe initial margin after the trade ({safe_init_margin_after})")
            result.required_initial_margin = previous_day_equity_with_loan
            result.initial_margin_after = init_margin_after

        result.success = is_order_possible
        return result

    def try_to_sell(self, contract, quantity):

        ticker = contract.ticker
        assert ticker

        result = SellOptionResult()
        result.success = False
        result.no_option_above_minimal_sell_price = False

        if math.isnan(ticker.bid) or ticker.ask < 0:
            logger.info(
                f" of {get_option_name(contract)} not executed due to lack of data, bid: {ticker.bid}, ask: {ticker.ask})")
            return result

        limit = self.calculate_limit(contract, ticker.bid, ticker.ask)
        logger.info(f"Calculated limit for {get_option_name(contract)}: {limit}, bid: {ticker.bid}, ask: {ticker.ask}")
        minimal_sell_price = self.calculate_minimal_sell_price(ticker.last)
        if limit < self.calculate_minimal_sell_price(ticker.last):
            logger.info(
                f"Sell of {get_option_name(contract)} not executed since calculated limit ({limit}) is lower than the minimal sell price ({minimal_sell_price})")
            result.no_option_above_minimal_sell_price = True
            return result
        result = self.test_order(contract, quantity, limit)
        if not result.success:
            return result

        trade = self.sell(contract, quantity)
        is_cancelled = is_trade_cancelled(trade)
        if is_cancelled and quantity == 1:
            for trade_log_entry in trade.log:
                if "PLUS VALUATION UNCERTAINTY" in trade_log_entry.message:
                    match = re.search(CANCELLED_TRADE_MESSAGE_PATTERN, trade_log_entry.message)
                    init_margin_after = float(match.group('init_margin').replace(',', ''))
                    valuation_uncertainty = float(match.group('uncertainty').replace(',', ''))
                    logger.info(f"Initial margin: {init_margin_after}, valuation uncertainty: {valuation_uncertainty}")
                    result.required_initial_margin = self.account_data.get_previous_day_equity_with_loan()
                    result.initial_margin_after = init_margin_after + valuation_uncertainty
                    break

        if not is_cancelled:
            trade.fillEvent += on_sell_fill
            result.trade = trade

        result.success = not is_cancelled
        return result

    def close_short_option_position(self, position):
        return self.close_short_option(position.contract, -position.position)

    def close_short_option(self, option, quantity):

        open_trades = self.get_open_trades()
        for open_trade in open_trades:
            if option.conId == open_trade.contract.conId and open_trade.order.action.upper() == 'BUY':
                logger.info(f"Cancelling buy trade for {get_option_name(option)}")
                self.cancel_trade(open_trade)

        ticker = self.ib.ticker(option)
        if is_regular_hours():
            order = MarketOrder('BUY', quantity, account=MY_ACCOUNT, usePriceMgmtAlgo=False)
        else:
            limit = ticker.ask
            order = LimitOrder('BUY', quantity, limit, account=MY_ACCOUNT, usePriceMgmtAlgo=False)
            order.outsideRth = True
            order.tif = 'GTC'
        trade = self.ib.placeOrder(option, order)
        return trade

    def close_position_at_limit(self, position, limit):
        assert position.position < 0
        order = LimitOrder('BUY', -position.position, limit, account=MY_ACCOUNT)
        order.usePriceMgmtAlgo = False
        order.outsideRth = True
        order.tif = 'GTC'
        return self.ib.placeOrder(position.contract, order)

    def sell(self, contract, quantity):

        ticker = contract.ticker
        assert ticker
        limit = self.calculate_limit(contract, ticker.bid, ticker.ask)

        order = LimitOrder('SELL', quantity, limit, account=MY_ACCOUNT)
        order.usePriceMgmtAlgo = False
        order.outsideRth = True
        order.tif = 'GTC'
        # order.transmit = False
        # order.orderId = self.ib.client.getReqId()  # Assign a unique ID

        trade = self.ib.placeOrder(contract, order)
        self.ib.sleep(2)
        return trade

    def get_open_trades(self):

        should_use_cache = time.time() - self.last_request_all_open_trades_time < 300
        if not should_use_cache:
            non_cache_open_trades = self.ib.reqAllOpenOrders()
            self.last_request_all_open_trades_time = time.time()
            for non_cache_open_trade in non_cache_open_trades:
                client_id = non_cache_open_trade.order.clientId
                order_id = non_cache_open_trade.order.orderId
                perm_id = non_cache_open_trade.order.permId
                logger.debug(
                    f"Non-cache trade: {client_id}, {order_id}, {perm_id}, stop loss: {non_cache_open_trade.order.auxPrice}")
                order_key = self.ib.wrapper.orderKey(client_id, order_id, perm_id)
                # self.ib.wrapper.trades[order_key] = non_cache_open_trade
                # self.ib.wrapper.permId2Trade[perm_id] = non_cache_open_trade

        open_trades = self.ib.openTrades()
        open_trades = [trade for trade in open_trades if
                       not is_trade_cancelled(trade) and trade.contract.secType == 'OPT']

        if not should_use_cache:
            for open_trade in open_trades:
                client_id = open_trade.order.clientId
                order_id = open_trade.order.orderId
                perm_id = open_trade.order.permId
                logger.debug(
                    f"Open trade: {client_id}, {order_id}, {perm_id}, stop loss: {open_trade.order.auxPrice}")

        tickers = self.ib.tickers()
        for open_trade in open_trades:
            if not hasattr(open_trade.contract, 'ticker'):
                for ticker in tickers:
                    if ticker.contract.conId == open_trade.contract.conId:
                        open_trade.contract.ticker = ticker
                        break

        open_sell_trades = [trade for trade in open_trades if trade.order.action.upper() == 'SELL']
        if open_sell_trades:
            contracts = [trade.contract for trade in open_sell_trades]
            logger.debug(f"Updating {len(contracts)} options for pending sell trades")
            self.market_data_fetcher.update_ticker_data(contracts)
        return open_trades

    def cancel_order(self, order):
        trade = self.ib.cancelOrder(order)
        logger.info(f"Status of cancel: {trade.orderStatus.status}")
        return trade

    def cancel_trade(self, trade):
        return self.cancel_order(trade.order)

    def modify_stop_loss(self, stop_loss_trade, new_stop_loss):
        self.verify_price_increments_exist(stop_loss_trade.contract)
        stop_loss_price = self.adjust_limit_to_market_rules(new_stop_loss)
        stop_loss_trade.order.auxPrice = stop_loss_price
        stop_loss_trade.order.usePriceMgmtAlgo = False
        stop_loss_trade.order.outsideRth = True
        stop_loss_trade.order.tif = 'GTC'
        stop_loss_trade.order.transmit = True
        logger.info(f"Modifying a stop loss order for {get_option_name(stop_loss_trade.contract)}")
        trade = self.ib.placeOrder(stop_loss_trade.contract, stop_loss_trade.order)
        return trade

    def calculate_limit(self, contract, bid, ask):
        assert not math.isnan(bid)
        assert not math.isnan(ask)

        self.verify_price_increments_exist(contract)
        if bid < 0:
            return ask
        spread = ask - bid
        raw_limit = bid + spread / 2
        if raw_limit > 2.5:
            logger.info(f"{get_option_name(contract)}, Ask: {ask}, Bid: {bid}, Spread: {spread}")
        return self.adjust_limit_to_market_rules(raw_limit)

    def verify_price_increments_exist(self, contract):
        if not self.price_increments:
            contract_details = self.ib.reqContractDetails(contract)
            market_rule_ids_str = contract_details[0].marketRuleIds
            market_rule_id_str = market_rule_ids_str.split(',')[0]
            market_rule_id = int(market_rule_id_str)
            market_rule = self.ib.reqMarketRule(int(market_rule_id))
            assert market_rule
            for tier in market_rule:
                self.price_increments.append(tier)
            self.price_increments = sorted(self.price_increments, key=lambda increment_tier: increment_tier.lowEdge)

    def adjust_limit_to_market_rules(self, raw_limit):
        assert self.price_increments
        assert raw_limit
        assert not math.isnan(raw_limit)
        current_increment = self.price_increments[0].increment
        for price_increment in self.price_increments:
            if raw_limit > price_increment.lowEdge:
                current_increment = price_increment.increment
        assert not math.isnan(current_increment)
        limit = round(round(raw_limit / current_increment) * current_increment, 6)
        return limit

    def add_stop_loss(self, position, stop_loss_per_option):
        self.verify_price_increments_exist(position.contract)
        raw_stop_loss_price = position.avgCost / 100 + stop_loss_per_option
        stop_loss_price = self.adjust_limit_to_market_rules(raw_stop_loss_price)
        stop_loss_order = StopOrder('BUY', abs(position.position), stop_loss_price, account=MY_ACCOUNT)
        # limit_price = stop_loss_price + 0.15
        # stop_loss_order = StopLimitOrder('BUY', abs(position.position), lmtPrice=limit_price, stopPrice=stop_loss_price)
        stop_loss_order.usePriceMgmtAlgo = False
        stop_loss_order.tif = 'GTC'

        logger.info(f"Placing a stop loss order for {get_option_name(position.contract)}")
        trade = self.ib.placeOrder(position.contract, stop_loss_order)

        # is this really required?
        # self.ib.sleep(2)
        return trade

    def get_short_options(self, should_use_cache=True):

        if not should_use_cache:
            original_request_timeout = self.ib.RequestTimeout
            self.ib.RequestTimeout = 10.0
            try:
                self.ib.reqPositions()
            except TimeoutError:
                logger.warning("reqPositions timed out")
            finally:
                self.ib.sleep(2)
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
            self.market_data_fetcher.update_ticker_data(options)
        return option_positions

    def get_fills(self):
        return self.ib.fills()

    def preview_order_status(self, option):
        order = MarketOrder('BUY', 1, whatIf=True, account=MY_ACCOUNT,
                            usePriceMgmtAlgo=False, outsideRth=True, tif='GTC')
        order_state = self.ib.whatIfOrder(option, order)
        self.ib.sleep(2)
        return order_state

    def calculate_minimal_sell_price(self, last_price):
        if self.account_data.is_portfolio_margin() and is_late_regular_hours():
            return 0
        if last_price == 0.05 and is_regular_hours():
            return 0.1
        return MINIMAL_SELL_PRICE

    def get_initial_margin_change(self, option, quantity):
        order = LimitOrder('BUY', quantity, 0.05, whatIf=True, account=MY_ACCOUNT,
                           usePriceMgmtAlgo=False, outsideRth=True, tif='GTC')
        order_state = self.ib.whatIfOrder(option, order)

        if float(order_state.equityWithLoanAfter) == sys.float_info.max:
            logger.error(f"Response has no real data, the market is probably closed")
            return 0

        init_margin_change = float(order_state.initMarginChange)
        return init_margin_change

    def buy_low_cost(self, option, quantity):
        order = LimitOrder('BUY', quantity, 0.05, account=MY_ACCOUNT, usePriceMgmtAlgo=False)
        order.outsideRth = True
        order.tif = 'GTC'
        trade = self.ib.placeOrder(option, order)
        return trade
