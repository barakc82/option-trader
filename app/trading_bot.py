import asyncio
import math
import re
from datetime import date

from ib_insync import LimitOrder, Trade

from utilities.ib_utils import SellOptionResult, MINIMAL_SELL_PRICE
from utilities.utils import *

from .account_data import AccountData
from .connection_manager import ConnectionManager


logger = logging.getLogger(__name__)
SAFETY_MARGIN = 1000
STOP_LIMIT_FACTOR = 1.1
CANCELLED_TRADE_MESSAGE_PATTERN = r"INITIAL MARGIN\s+\[(?P<init_margin>[\d,.]+).*?VALUATION UNCERTAINTY\s+\[(?P<uncertainty>[\d,.]+)"
INSUFFICIENT_FUNDS_MESSAGE_PATTERN = r"Loan Value\s+\[(?P<loan_value>[\d,.]+).*?Initial Margin of\s+\[(?P<init_margin>[\d,.]+)"

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
            self.account_data = AccountData()

            self.price_increments = []
            self.req_id_to_order_metadata = {}

            logger.info("TradingBot singleton initialized.")
            self._initialized = True

    def get_short_options(self):
        """Fetches active short option positions from the internal cache."""
        positions = self.ib.positions(MY_ACCOUNT)
        if not positions:
            logger.warning("No position were found")

        option_positions = []
        now_in_nyc = datetime.now(new_york_timezone)
        for position in positions:
            if position.contract.secType == 'OPT' and position.position < 0:
                expiry = datetime.strptime(position.contract.lastTradeDateOrContractMonth, "%Y%m%d").date()
                if expiry < now_in_nyc.date() or (expiry == now_in_nyc.date() and REGULAR_HOURS_END_TIME < now_in_nyc.time()):
                    continue
                option_positions.append(position)

        return option_positions

    def get_open_trades(self) -> list[Trade]:
        open_trades = [t for t in self.ib.openTrades() if not is_trade_cancelled(t) and t.contract.secType == 'OPT']

        # Link tickers
        tickers = self.ib.tickers()
        for trade in open_trades:
            if not hasattr(trade.contract, 'ticker'):
                for t in tickers:
                    if t.contract.conId == trade.contract.conId:
                        trade.contract.ticker = t
                        break

        return open_trades

    def cancel_order(self, order):
        trade = self.ib.cancelOrder(order)
        logger.info(f"Status of cancel: {trade.orderStatus.status}")
        return trade

    def cancel_trade(self, trade):
        return self.cancel_order(trade.order)

    async def close_short_option(self, option, quantity, limit):
        limit = self.adjust_limit_to_market_rules(option, limit)
        open_trades = self.get_open_trades()

        for t in open_trades:
            if option.conId == t.contract.conId and t.order.action.upper() == 'BUY':
                logger.info(f"Cancelling the buy of {get_option_name(t.contract)} in order to place a new order that will close the positon")
                self.cancel_trade(t)

        order = LimitOrder('BUY', quantity, limit, account=MY_ACCOUNT, usePriceMgmtAlgo=False)
        order.outsideRth = True
        order.tif = 'GTC'

        logger.info(f"Placing {order.action} order for {get_option_name(option)}")
        trade = self.ib.placeOrder(option, order)

        # Wait for TWS to acknowledge the order
        for _ in range(50):
            if trade.orderStatus.status not in ('PendingSubmit', ''):
                break
            await asyncio.sleep(0.1)

        return trade

    async def close_short_option_position(self, position, limit=None):
        return await self.close_short_option(position.contract, abs(position.position), limit)

    async def fetch_price_increments(self, contract):
        while not self.price_increments:
            try:
                details = await asyncio.wait_for(self.ib.reqContractDetailsAsync(contract), timeout=10)
                if not details:
                    logger.error(f"No contract details found for {contract}. Retrying...")
                    await asyncio.sleep(5)
                    continue

                market_rule_id = int(details[0].marketRuleIds.split(',')[0])
                rule = await asyncio.wait_for(self.ib.reqMarketRuleAsync(market_rule_id), timeout=10)
                if rule:
                    self.price_increments = sorted(rule, key=lambda i: i.lowEdge)
                    logger.info(f"Successfully fetched {len(self.price_increments)} price increments")
                else:
                    logger.error(f"Market rule {market_rule_id} returned no increments. Retrying...")
                    await asyncio.sleep(5)
            except asyncio.TimeoutError:
                logger.error(f"Timeout while fetching price increments for {contract}. Retrying...")
                await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"Error while fetching price increments for {contract}: {e}. Retrying...")
                await asyncio.sleep(5)

    def adjust_limit_to_market_rules(self, contract, raw_limit):
        current_increment = self.price_increments[0].increment
        for i in self.price_increments:
            if raw_limit > i.lowEdge:
                current_increment = i.increment
        return round(round(raw_limit / current_increment) * current_increment, 6)


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
        safe_init_margin_after = init_margin_after + SAFETY_MARGIN

        previous_day_equity_with_loan = await self.account_data.get_previous_day_equity_with_loan()
        is_previous_day_equity_with_loan_check_passed = safe_init_margin_after < previous_day_equity_with_loan
        if not is_previous_day_equity_with_loan_check_passed:
            logger.info(f"Testing the sell of {get_option_name(option)} failed because the required initial margin ({init_margin_after:,.0f} + {SAFETY_MARGIN:,.0f} safety) "
                        f"exceeds the previous day equity with loan ({previous_day_equity_with_loan:,.0f})")
            result.required_initial_margin = previous_day_equity_with_loan
            result.initial_margin_after = safe_init_margin_after
            return result

        equity_with_loan = await self.account_data.get_equity_with_loan()
        is_equity_with_loan_check_passed = safe_init_margin_after < equity_with_loan
        if not is_equity_with_loan_check_passed:
            logger.info(
                f"Testing the sell of {get_option_name(option)} failed because the required initial margin ({init_margin_after:,.0f} + {SAFETY_MARGIN:,.0f} safety) "
                f"exceeds the equity with loan ({equity_with_loan:,.0f})")
            result.required_initial_margin = equity_with_loan
            result.initial_margin_after = safe_init_margin_after
            return result

        result.success = True
        return result


    def calculate_limit(self, contract, bid, ask):
        assert not math.isnan(bid)
        assert not math.isnan(ask)

        if bid < 0:
            return ask
        spread = ask - bid
        raw_limit = bid + spread / 2
        return self.adjust_limit_to_market_rules(contract, raw_limit)

    async def sell(self, contract, quantity, order_metadata):
        ticker = contract.ticker
        assert ticker
        limit = self.calculate_limit(contract, ticker.bid, ticker.ask)

        order = LimitOrder('SELL', quantity, limit, account=MY_ACCOUNT)
        order.usePriceMgmtAlgo = False
        order.outsideRth = True
        order.tif = 'GTC'

        trade = self.ib.placeOrder(contract, order)
        self.req_id_to_order_metadata[trade.order.orderId] = order_metadata

        for _ in range(20):
            if trade.orderStatus.status not in ('PendingSubmit', 'PreSubmitted'):
                break
            await asyncio.sleep(0.1)

        if trade.orderStatus.status in ('Cancelled', 'Inactive'):
            option_name = get_option_name(contract)
            logger.warning(f"Sell order for {option_name} was rejected with status: {trade.orderStatus.status}")
            for entry in trade.log:
                if entry.message:
                    logger.warning(f"Trade log for {option_name}: {entry.message}")

        return trade

    async def try_to_sell(self, contract, quantity, order_metadata):
        ticker = contract.ticker
        assert ticker

        result = SellOptionResult()
        result.success = False
        result.no_option_above_minimal_sell_price = False

        if math.isnan(ticker.bid) or ticker.ask < 0:
            logger.info(f"Sell of {get_option_name(contract)} failed: bid={ticker.bid}, ask={ticker.ask}")
            return result

        limit = self.calculate_limit(contract, ticker.bid, ticker.ask)
        minimal_sell_price = self.calculate_minimal_sell_price(ticker.last, contract.lastTradeDateOrContractMonth)
        if limit < minimal_sell_price:
            logger.info(f"Sell of {get_option_name(contract)} limit ({limit}) < min price ({minimal_sell_price})")
            result.no_option_above_minimal_sell_price = True
            return result
            
        result = await self.test_order(contract, quantity, limit)
        if not result.success:
            return result

        trade = await self.sell(contract, quantity, order_metadata=order_metadata)
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
                if "Your Available Funds are in sufficient" in trade_log_entry.message:
                    match = re.search(INSUFFICIENT_FUNDS_MESSAGE_PATTERN, trade_log_entry.message)
                    loan_value = float(match.group('loan_value').replace(',', ''))
                    init_margin_after = float(match.group('init_margin').replace(',', ''))
                    logger.info(f"Loan value: {loan_value}, New total initial margin: {init_margin_after}")
                    result.required_initial_margin = loan_value
                    result.initial_margin_after = init_margin_after
                    break

        if not is_cancelled:
            result.trade = trade

        result.success = not is_cancelled
        return result

    def calculate_minimal_sell_price(self, last_price, expiration_date):
        if (self.account_data.is_portfolio_margin() and is_late_regular_hours() and
                expiration_date == datetime.today().strftime('%Y%m%d')):
            return 0
        if (last_price == 0.05 and is_regular_hours() and
                expiration_date == datetime.today().strftime('%Y%m%d')):
            return 0.1
        return MINIMAL_SELL_PRICE

    def buy_low_cost(self, option, quantity, limit=0.05):
        order = LimitOrder('BUY', quantity, limit, account=MY_ACCOUNT, usePriceMgmtAlgo=False)
        order.outsideRth = True
        order.tif = 'GTC'
        trade = self.ib.placeOrder(option, order)
        return trade

    async def get_initial_margin_change(self, option, quantity, limit=0.05):
        order = LimitOrder('BUY', quantity, limit, whatIf=True, account=MY_ACCOUNT,
                           usePriceMgmtAlgo=False, outsideRth=True, tif='GTC')
        order_state = await self.ib.whatIfOrderAsync(option, order)

        if float(order_state.equityWithLoanAfter) == sys.float_info.max:
            logger.error(f"Response has no real data, the market is probably closed")
            return 0

        return float(order_state.initMarginChange)

    async def modify_limit_order(self, limit_buy_trade, raw_limit):
        limit_price = self.adjust_limit_to_market_rules(limit_buy_trade.contract, raw_limit)
        if limit_price == limit_buy_trade.order.lmtPrice:
            logger.info(f"Skipping modification for {get_option_name(limit_buy_trade.contract)} as limit price {limit_price} is unchanged")
            return limit_buy_trade

        limit_buy_trade.order.lmtPrice = limit_price
        limit_buy_trade.order.usePriceMgmtAlgo = False
        limit_buy_trade.order.outsideRth = True
        limit_buy_trade.order.tif = 'GTC'
        limit_buy_trade.order.transmit = True
        trade = self.ib.placeOrder(limit_buy_trade.contract, limit_buy_trade.order)
        return trade

