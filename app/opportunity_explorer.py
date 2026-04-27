import math
from datetime import date
import random

from utilities.utils import *
from utilities.ib_utils import *
from app.account_data import AccountData
from app.max_loss_calculator import MaxLossCalculator, calculate_max_loss
from app.configuration import should_write_options_overnight, should_monitor_only
from app.option_cache import OptionCache
from app.strike_finder import StrikeFinder
from app.target_delta_calculator import TargetDeltaCalculator

TIME_UNTIL_NEXT_SELL_CHECK = 120
LOWER_MINIMAL_SELL_PRICE_TO_CLOSE_POSITION = MINIMAL_SELL_PRICE + 0.05
HIGHER_MINIMAL_SELL_PRICE_TO_CLOSE_POSITION = MINIMAL_SELL_PRICE + 0.1

logger = logging.getLogger(__name__)


def find_all_buy_trades(option, open_buy_trades):
    buy_trades_for_position = []
    for open_buy_trade in open_buy_trades:
        if option.conId == open_buy_trade.contract.conId and open_buy_trade.order.action.upper() == 'BUY':
            buy_trades_for_position.append(open_buy_trade)
    return buy_trades_for_position


def calculate_max_options_for_market_drop(put_option):
    if not is_switched_to_overnight_trading():
        return sys.float_info.max

    remaining_fraction_after_drop = 1 - 0.3
    current_price = current_thread.market_data_fetcher.get_spx_price()
    if math.isnan(current_price):
        logger.error("Cannot calculate max number of options for market drop because the S&P 500 index value is NaN")
        return 0

    price_after_drop = current_price * remaining_fraction_after_drop
    if put_option.strike < price_after_drop:
        logger.info(f"{get_option_name(put_option)} is lower than worst case scenario market drop")
        return sys.float_info.max

    positions = current_thread.trading_bot.get_short_options()
    current_total_liability = 0
    for position in positions:
        if not position.contract.secType == 'OPT' or position.position >= 0 or position.contract.right != 'P':
            continue
        if position.contract.strike < price_after_drop:
            continue
        last_trade_date = datetime.strptime(position.contract.lastTradeDateOrContractMonth, "%Y%m%d").date()
        if last_trade_date <= date.today():
            continue
        position_liability = (position.contract.strike - price_after_drop) * 100 * -position.position
        current_total_liability += position_liability

    account_data = AccountData()
    current_net_liquidation_value = account_data.get_net_liquidation_value()
    net_liquidation_value_after_drop = current_net_liquidation_value * remaining_fraction_after_drop
    net_worth_after_drop = net_liquidation_value_after_drop - current_total_liability
    if net_worth_after_drop < 0:
        logger.info(f"Negative net worth in case of a market drop")
        return 0

    liability_per_contract = (put_option.strike - price_after_drop) * 100
    return math.floor(net_worth_after_drop / liability_per_contract)


def calculate_max_options_for_market_rise(call_option):
    if not is_switched_to_overnight_trading():
        return sys.float_info.max

    fold_after_market_rise = 1 + 0.2
    current_price = current_thread.market_data_fetcher.get_spx_price()
    if math.isnan(current_price):
        logger.error("Cannot calculate max number of options for market rise because the S&P 500 index value is NaN")
        return 0

    price_after_market_rise = current_price * fold_after_market_rise
    if call_option.strike > price_after_market_rise:
        return sys.float_info.max

    positions = current_thread.trading_bot.get_short_options()
    current_total_liability = 0
    for position in positions:
        if not position.contract.secType == 'OPT' or position.position >= 0 or position.contract.right != 'C':
            continue
        if position.contract.strike > price_after_market_rise:
            continue
        last_trade_date = datetime.strptime(position.contract.lastTradeDateOrContractMonth, "%Y%m%d").date()
        if last_trade_date <= date.today():
            continue
        position_liability = (price_after_market_rise - position.contract.strike) * 100 * -position.position
        current_total_liability += position_liability

    account_data = AccountData()
    current_net_liquidation_value = account_data.get_net_liquidation_value()
    net_liquidation_value_after_rise = current_net_liquidation_value * fold_after_market_rise
    net_worth_after_rise = net_liquidation_value_after_rise - current_total_liability
    if net_worth_after_rise < 0:
        return 0

    if price_after_market_rise == call_option.strike:
        return sys.float_info.max

    liability_per_contract = (price_after_market_rise - call_option.strike) * 100
    return math.floor(net_worth_after_rise / liability_per_contract)


class OpportunityExplorer:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(OpportunityExplorer, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if not self._initialized:
            self.account_data = AccountData()
            self.market_data_fetcher = current_thread.market_data_fetcher
            self.trading_bot = current_thread.trading_bot
            self.last_submit_order_attempt_time = 0
            self.should_cancel_all_sell_orders = False
            self.no_put_options_above_minimal_sell_price = False
            self.no_call_options_above_minimal_sell_price = False
            self.can_submit_orders = True
            self.last_put_option_price = 0
            self.last_call_option_price = 0

            self._initialized = True

    def explore_opportunities(self):

        logger.info("Exploring new opportunities")
        date = get_current_trading_day()
        options_cache = OptionCache()
        options = options_cache.load(date)
        open_trades = self.trading_bot.get_open_trades()
        self.can_submit_orders = time.time() - self.last_submit_order_attempt_time > TIME_UNTIL_NEXT_SELL_CHECK

        # During after hours trading it is ok to submit orders close in time
        if is_switched_to_overnight_trading():
            self.can_submit_orders = should_write_options_overnight

        self.can_submit_orders &= not should_monitor_only

        if self.can_submit_orders:
            logger.info("Trying to sell put options")
            sell_put_option_result = self.try_to_sell_put_options(open_trades, options)

            logger.info("Trying to sell call options")
            sell_call_option_result = self.try_to_sell_call_options(open_trades, options)

            if sell_call_option_result.success or sell_put_option_result.success:
                self.last_submit_order_attempt_time = time.time()
            #self.can_submit_orders = sell_call_option_result.success or sell_put_option_result.success
            self.should_cancel_all_sell_orders = sell_call_option_result.is_low_projected_cushion or sell_put_option_result.is_low_projected_cushion
            logger.info("Done exploring possible opportunities")
        else:
            next_sell_check_time = self.last_submit_order_attempt_time + TIME_UNTIL_NEXT_SELL_CHECK
            date_str = datetime.fromtimestamp(next_sell_check_time).strftime("%H:%M")
            logger.info(f"Next sell check will be at {date_str}")

    def try_to_sell_call_options(self, open_trades, options):
        call_options = [option for option in options if option.right == 'C']
        sell_option_result = SellOptionResult()
        if not call_options:
            logger.error("No call options found")
            return sell_option_result

        target_delta_calculator = TargetDeltaCalculator()
        target_delta = target_delta_calculator.calculate_target_delta()
        strike_finder = StrikeFinder()
        call_option = strike_finder.get_low_delta_call_option(call_options, target_delta)
        if not call_option:
            logger.error("Call option candidate for selling could not be found")
            return sell_option_result

        last_price = extract_last_median_price(call_option.ticker)
        if self.last_call_option_price != last_price and not math.isnan(last_price):
            logger.info(f"The current price level for call options changed from {self.last_call_option_price} to {last_price}")
            self.last_call_option_price = last_price

        stop_loss_per_option = calculate_max_loss('C', should_consider_only_effective=True)
        if stop_loss_per_option < last_price:
            logger.warning(f"Failed to sell {get_option_name(call_option)} since the acceptable loss ({stop_loss_per_option}) is smaller than the option price ({last_price})")
            return sell_option_result

        logger.info(f"Testing sell 2 options of {get_option_name(call_option)}")
        max_options_for_market_rise = calculate_max_options_for_market_rise(call_option)
        if not max_options_for_market_rise:
            logger.warning(f"Failed to sell {get_option_name(call_option)} due to projected exposure fee")
            return sell_option_result

        self.cancel_all_buy_trades(open_trades, call_option)  # To allow a sell
        current_thread.ib.sleep(0.2)

        sell_option_result = self.try_to_sell(call_option, 2, target_delta)
        if sell_option_result.success:
            return sell_option_result

        logger.warning(f"Failed to sell 2 options of {get_option_name(call_option)}")
        logger.info(f"Testing sell 1 option of {get_option_name(call_option)}")
        sell_option_result = self.try_to_sell(call_option, 1, target_delta)
        if not sell_option_result.success:
            logger.warning(f"Failed to sell 1 options of {get_option_name(call_option)}")

            if sell_option_result.required_initial_margin and not is_switched_to_overnight_trading():
                self.try_to_reduce_initial_margin_for_call_options(call_option, sell_option_result.required_initial_margin, sell_option_result.initial_margin_after, call_options)

        self.no_call_options_above_minimal_sell_price = sell_option_result.no_option_above_minimal_sell_price
        return sell_option_result

    def try_to_sell(self, option, quantity, target_delta):
        delta = abs(get_delta(option.ticker))
        if delta > target_delta:
            logger.warning(f"Failed to sell {get_option_name(option)} since the delta has risen to {delta:.3f}, "
                           f"beyond the target delta of {target_delta:.3f}")
            result = SellOptionResult()
            result.success = False
            return result

        start_time = time.time()
        result = self.trading_bot.try_to_sell(option, quantity)
        end_time = time.time()
        duration_of_sell_operation = end_time - start_time

        delta = abs(get_delta(option.ticker))
        if result.success and delta > target_delta:
            logger.warning(f"After selling {get_option_name(option)},the delta has risen to {delta:.2f}, "
                           f"beyond the target delta of {target_delta:.2f}. "
                           f"The sell took {duration_of_sell_operation:.2f} seconds")

        if result.success:
            comment = f"Delta: {delta:.3f}, target delta: {target_delta:.3f}"
            req_id_to_comment[result.trade.order.orderId] = comment
            req_id_to_target_delta[result.trade.order.orderId] = target_delta
        return result

    def try_to_sell_put_options(self, open_trades, options):
        put_options = [option for option in options if option.right == 'P']
        sell_option_result = SellOptionResult()
        if not put_options:
            logger.error("Np put options found")
            return sell_option_result
        target_delta_calculator = TargetDeltaCalculator()
        target_delta = target_delta_calculator.calculate_target_delta()
        strike_finder = StrikeFinder()

        logger.info("Searching for a suitable low-delta put option")
        put_option = strike_finder.get_low_delta_put_option(put_options, target_delta)
        if not put_option:
            logger.error("Put option candidate for selling could not be found")
            return sell_option_result

        last_price = extract_last_median_price(put_option.ticker)
        if self.last_put_option_price != last_price and not math.isnan(last_price):
            logger.info(f"The current price level for put options changed from {self.last_put_option_price} to {last_price}")
            self.last_put_option_price = last_price

        stop_loss_per_option = calculate_max_loss('P', should_consider_only_effective=True)
        if stop_loss_per_option < last_price:
            logger.warning(f"Failed to sell {get_option_name(put_option)} since the acceptable loss ({stop_loss_per_option}) is smaller than the option price ({last_price})")
            return sell_option_result

        logger.info(f"Testing sell 2 options of {get_option_name(put_option)}")
        max_options_for_market_drop = calculate_max_options_for_market_drop(put_option)
        if not max_options_for_market_drop:
            logger.warning(f"Failed to sell {get_option_name(put_option)} due to projected exposure fee")
            return sell_option_result

        self.cancel_all_buy_trades(open_trades, put_option)  # To allow a sell
        current_thread.ib.sleep(0.2)

        quantity = min(max_options_for_market_drop, 2)
        sell_option_result = self.try_to_sell(put_option, quantity, target_delta)
        if sell_option_result.success:
            return sell_option_result

        logger.warning(f"Failed to sell 2 options of {get_option_name(put_option)}")
        logger.info(f"Testing sell 1 option of {get_option_name(put_option)}")
        sell_option_result = self.try_to_sell(put_option, 1, target_delta)
        if not sell_option_result.success:
            logger.warning(f"Failed to sell 1 options of {get_option_name(put_option)}")

            if sell_option_result.required_initial_margin and not is_switched_to_overnight_trading():
                self.try_to_reduce_initial_margin_for_put_options(put_option, sell_option_result.required_initial_margin, sell_option_result.initial_margin_after, put_options)

        self.no_put_options_above_minimal_sell_price = sell_option_result.no_option_above_minimal_sell_price
        return sell_option_result

    def cancel_all_buy_trades(self, open_buy_trades, option):
        open_buy_trades_for_option = find_all_buy_trades(option, open_buy_trades)
        for open_buy_trade in open_buy_trades_for_option:
            logger.debug(
                f"Checking if cancel needed for the stop loss of {get_option_name(open_buy_trade.contract)}, comparing between {option.conId} and {open_buy_trade.contract.conId}")
            if option.conId == open_buy_trade.contract.conId:
                logger.info(f"Cancelling stop loss for {get_option_name(option)}")
                self.trading_bot.cancel_trade(open_buy_trade)

    def calculate_minimal_sell_price_to_close_position(self, right):
        minimal_sell_price_to_close_position = HIGHER_MINIMAL_SELL_PRICE_TO_CLOSE_POSITION
        positions = current_thread.trading_bot.get_short_options()
        current_number_of_options = len([position for position in positions if position.contract.right == right])
        max_loss_calculator = MaxLossCalculator()
        max_number_of_options = max_loss_calculator.get_max_number_of_options(right)
        vacant_options_fraction = 1 - current_number_of_options / max_number_of_options
        elapsed_day_fraction = get_elapsed_day_fraction()
        lower_minimal_sell_price_probability = vacant_options_fraction * elapsed_day_fraction
        logger.info(f"Fraction of vacant slots for '{right}' options: {vacant_options_fraction:.2f}, "
                    f"fraction of day passed: {elapsed_day_fraction:.2f}, "
                    f"total probability for lower minimal sell price: {lower_minimal_sell_price_probability:.2f}")
        if random.random() < lower_minimal_sell_price_probability:
            minimal_sell_price_to_close_position = LOWER_MINIMAL_SELL_PRICE_TO_CLOSE_POSITION
        return minimal_sell_price_to_close_position

    def try_to_reduce_initial_margin_for_call_options(self, call_option_to_be_sold, required_initial_margin, initial_margin_after_sell, call_options):
        minimal_sell_price_to_close_position = self.calculate_minimal_sell_price_to_close_position('C')
        if self.last_call_option_price <= minimal_sell_price_to_close_position:
            logger.info(
                f"Will not try to reduce initial margin for call options since the price level of call options is {self.last_call_option_price} while the minimal sell price to close is {minimal_sell_price_to_close_position}")
            return

        strike_finder = StrikeFinder()
        positions = current_thread.trading_bot.get_short_options()
        min_strike = min(position.contract.strike for position in positions)
        available_cheap_call_option = strike_finder.get_available_cheap_call_option(call_options, min_strike)
        initial_margin_change = self.trading_bot.get_initial_margin_change(available_cheap_call_option, 1)
        logger.info(f"try_to_reduce_initial_margin_for_call_options, required initial margin: {required_initial_margin}, initial margin after sell: {initial_margin_after_sell},"
                    f"initial margin change due to buy: {initial_margin_change}, option to be sold: {get_option_name(call_option_to_be_sold)}, option to buy: {get_option_name(available_cheap_call_option)}")

        if initial_margin_change == 0:
            logging.info(f"Initial margin change for buying {get_option_name(available_cheap_call_option)} is 0, will not buy it")
            return

        missing_sum = required_initial_margin - initial_margin_after_sell
        required_number_of_units = math.ceil(missing_sum / initial_margin_change)

        logger.info(f"try_to_reduce_initial_margin_for_call_options, required initial margin: {required_initial_margin}, initial margin after sell: {initial_margin_after_sell}, "
                    f"initial margin change due to buy: {initial_margin_change:.2f}, option to be sold: {get_option_name(call_option_to_be_sold)}, option to buy: {get_option_name(available_cheap_call_option)}, missing_sum: {missing_sum:.0f}, required number of units = {required_number_of_units}, last call price: {self.last_call_option_price}")

        if required_number_of_units < 0:
            logger.error(f"The required number of units is {required_number_of_units}")
            return

        if self.last_call_option_price * 0.4 > 0.07 * required_number_of_units + 0.02:
            logger.info(f"Buying {required_number_of_units} units of {get_option_name(available_cheap_call_option)} would relax the required initial margin")
            trade = current_thread.trading_bot.buy_low_cost(available_cheap_call_option, required_number_of_units)
            comment = f"Margin relax"
            req_id_to_comment[trade.order.orderId] = comment
            self.can_submit_orders = True
        else:
            logger.info(f"Will not buy {get_option_name(available_cheap_call_option)} since the potential sell price is too low ({self.last_call_option_price})")

    def try_to_reduce_initial_margin_for_put_options(self, put_option_to_be_sold, required_initial_margin, initial_margin_after_sell, put_options):
        minimal_sell_price_to_close_position = self.calculate_minimal_sell_price_to_close_position('P')
        if self.last_put_option_price <= minimal_sell_price_to_close_position:
            logger.info(f"Will not try to reduce initial margin for put options since the price level of put options is {self.last_put_option_price} while the minimal sell price to close is {minimal_sell_price_to_close_position}")
            return

        strike_finder = StrikeFinder()
        positions = current_thread.trading_bot.get_short_options()
        max_strike = max(position.contract.strike for position in positions)
        available_cheap_put_option = strike_finder.get_available_cheap_put_option(put_options, max_strike)
        initial_margin_change = self.trading_bot.get_initial_margin_change(available_cheap_put_option, 1)
        if initial_margin_change == 0:
            logging.info(f"Initial margin change for buying {get_option_name(available_cheap_put_option)} is 0, will not buy it")
            initial_margin_change = self.trading_bot.get_initial_margin_change(available_cheap_put_option, 2)
            logging.info(
                f"Initial margin change for 2 units is {initial_margin_change}")
            return

        missing_sum = required_initial_margin - initial_margin_after_sell
        required_number_of_units = math.ceil(missing_sum / initial_margin_change)

        logger.info(f"try_to_reduce_initial_margin_for_put_options, required initial margin: {required_initial_margin}, initial margin after sell: {initial_margin_after_sell}, "
                    f"initial margin change due to buy: {initial_margin_change}, option to be sold: {get_option_name(put_option_to_be_sold)}, option to buy: {get_option_name(available_cheap_put_option)}, missing_sum: {missing_sum:.0f}, required number of units = {required_number_of_units}, last put price: {self.last_put_option_price}")

        if required_number_of_units < 0:
            logger.error(f"The required number of units is {required_number_of_units}")
            return

        if self.last_put_option_price * 0.4 > 0.07 * required_number_of_units + 0.02:
            logger.info(f"Buying {required_number_of_units} units of {get_option_name(available_cheap_put_option)} would relax the required initial margin")
            trade = current_thread.trading_bot.buy_low_cost(available_cheap_put_option, required_number_of_units)
            comment = f"Margin relax"
            req_id_to_comment[trade.order.orderId] = comment
            self.can_submit_orders = True
        else:
            logger.info(f"Will not buy {get_option_name(available_cheap_put_option)} since the potential sell price is too low ({self.last_put_option_price})")
