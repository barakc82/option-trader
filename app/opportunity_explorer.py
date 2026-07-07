import asyncio
import math
from datetime import datetime, date, timedelta

from utilities.utils import *
from utilities.ib_utils import *
from .account_data import AccountData
from .market_data_fetcher import MarketDataFetcher
from .max_loss_calculator import MaxLossCalculator
from .net_worth_calculator import NetWorthCalculator
from .option_cache import OptionCache
from .option_data_fetcher import OptionDataFetcher
from .strike_finder import StrikeFinder
from .target_delta_calculator import TargetDeltaCalculator
from .connection_manager import ConnectionManager
from .trading_bot import TradingBot

TIME_UNTIL_NEXT_SELL_CHECK = 120
MINIMAL_SELL_PRICE_FOR_GENERAL_MARGIN_REDUCTION = MINIMAL_SELL_PRICE + 0.1

logger = logging.getLogger(__name__)


def find_all_buy_trades(option, open_buy_trades):
    buy_trades_for_position = []
    for open_buy_trade in open_buy_trades:
        if option.conId == open_buy_trade.contract.conId and open_buy_trade.order.action.upper() == 'BUY':
            buy_trades_for_position.append(open_buy_trade)
    return buy_trades_for_position


class OpportunityExplorer:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(OpportunityExplorer, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if not self._initialized:
            self.ib = ConnectionManager().ib
            self.account_data = AccountData()
            self.market_data_fetcher = MarketDataFetcher()
            self.option_data_fetcher = OptionDataFetcher()
            self.trading_bot = TradingBot()
            self.net_worth_calculator = NetWorthCalculator()
            self.max_loss_calculator = MaxLossCalculator()
            self.last_submit_order_attempt_time = 0
            self.no_put_options_above_minimal_sell_price = False
            self.no_call_options_above_minimal_sell_price = False
            self.can_submit_orders = True
            self.last_put_option_price = 0
            self.last_call_option_price = 0
            self.call_margin_reduction = None
            self.put_margin_reduction = None
            self.last_call_margin_reduction_record_time = 0
            self.last_put_margin_reduction_record_time = 0
            self.last_margin_lock_resolution_attempt_time = 0

            # Dynamic config fields
            self.should_write_options_overnight = True
            self.should_monitor_only = False
            self.should_resolve_margin_locks = True

            self._initialized = True

    def load_config(self):
        """Reads configuration from config/option_trader_config.json."""
        config_path = "config/option_trader_config.json"
        try:
            if os.path.exists(config_path):
                with open(config_path, "r") as f:
                    config = json.load(f)

                    new_write_overnight = config.get("should_write_options_overnight", True)
                    if new_write_overnight != self.should_write_options_overnight:
                        logger.info(f"OpportunityExplorer: should_write_options_overnight changed from {self.should_write_options_overnight} to {new_write_overnight}")
                        self.should_write_options_overnight = new_write_overnight

                    new_monitor_only = config.get("should_monitor_only", False)
                    if new_monitor_only != self.should_monitor_only:
                        logger.info(f"OpportunityExplorer: should_monitor_only changed from {self.should_monitor_only} to {new_monitor_only}")
                        self.should_monitor_only = new_monitor_only

                    new_resolve_margin_locks = config.get("should_resolve_margin_locks", True)
                    if new_resolve_margin_locks != self.should_resolve_margin_locks:
                        logger.info(f"OpportunityExplorer: should_resolve_margin_locks changed from {self.should_resolve_margin_locks} to {new_resolve_margin_locks}")
                        self.should_resolve_margin_locks = new_resolve_margin_locks

        except Exception as e:
            logger.error(f"OpportunityExplorer: Error reading config: {e}")

    async def explore_opportunities(self):
        # 1. Refresh dynamic config at the start of each iteration
        self.load_config()
        
        current_time = time.time()
        if self.call_margin_reduction and current_time - self.last_call_margin_reduction_record_time > 120:
            self.call_margin_reduction = None
        if self.put_margin_reduction and current_time - self.last_put_margin_reduction_record_time > 120:
            self.put_margin_reduction = None

        logger.info("Exploring new opportunities")
        reference_price = self.market_data_fetcher.get_reference_price()
        if math.isnan(reference_price):
            logger.error("Reference price is NaN, cannot explore opportunities")
            return
        date = get_current_trading_day()
        options = await self.option_data_fetcher.get_options(date)
        options = [option for option in options if (option.right == 'P' and option.strike < reference_price) or (
                    option.right == 'C' and option.strike > reference_price)]
        open_trades = self.trading_bot.get_open_trades()
        short_options = self.trading_bot.get_short_options()

        # Build a mapping of conId -> contract from existing trades and positions
        # These contract objects might already have tickers attached
        existing_contracts = {t.contract.conId: t.contract for t in open_trades}
        for pos in short_options:
            if pos.contract.conId not in existing_contracts:
                existing_contracts[pos.contract.conId] = pos.contract

        # Replace options in the list with existing ones if found
        for i, option in enumerate(options):
            if option.conId in existing_contracts:
                options[i] = existing_contracts[option.conId]

        self.can_submit_orders = time.time() - self.last_submit_order_attempt_time > TIME_UNTIL_NEXT_SELL_CHECK

        # During after hours trading it is ok to submit orders close in time
        if is_switched_to_overnight_trading():
            # Using dynamic config value
            self.can_submit_orders = self.should_write_options_overnight

        self.can_submit_orders &= not self.should_monitor_only

        if self.can_submit_orders:
            logger.info("Trying to sell put options")
            sell_put_option_result = await self.try_to_sell_put_options(open_trades, options)

            logger.info("Trying to sell call options")
            sell_call_option_result = await self.try_to_sell_call_options(open_trades, options)

            if sell_call_option_result.success or sell_put_option_result.success:
                self.last_submit_order_attempt_time = time.time()
            logger.info("Done exploring possible opportunities")
        else:
            next_sell_check_time = self.last_submit_order_attempt_time + TIME_UNTIL_NEXT_SELL_CHECK
            date_str = datetime.fromtimestamp(next_sell_check_time).strftime("%H:%M")
            logger.info(f"Next sell check will be at {date_str}")

    async def try_to_sell_call_options(self, open_trades, options):
        call_options = [option for option in options if option.right == 'C']
        if not call_options:
            logger.error("No call options found")
            return SellOptionResult()

        target_delta_calculator = TargetDeltaCalculator()
        target_delta = await target_delta_calculator.calculate_target_delta('C')
        
        call_option = await self.find_call_candidate(call_options, target_delta)
        if not call_option:
            return SellOptionResult()

        estimated_sell_price = await self.estimate_sell_price(call_option)
        if estimated_sell_price < 0:
            logger.error("Failed to estimate selling price")
            return SellOptionResult()
        self._update_last_option_price(estimated_sell_price, 'C')

        stop_loss_per_option = self.max_loss_calculator.calculate_max_loss('C')
        if stop_loss_per_option < estimated_sell_price:
            logger.warning(f"Failed to sell {get_option_name(call_option)} since the acceptable loss ({stop_loss_per_option:2f})"
                           f" is smaller than the option price ({estimated_sell_price})")
            return SellOptionResult()

        max_options_for_market_rise = await self.calculate_max_allowed_call_options(call_option)
        if not max_options_for_market_rise:
            return SellOptionResult()

        await self.cancel_all_buy_trades(open_trades, call_option)

        sell_option_result = await self.try_to_sell(call_option, 2, target_delta)
        if sell_option_result.success:
            return sell_option_result

        logger.warning(f"Failed to sell 2 options of {get_option_name(call_option)}")
        logger.info(f"Testing sell 1 option of {get_option_name(call_option)}")
        sell_option_result = await self.try_to_sell(call_option, 1, target_delta)
        if not sell_option_result.success:
            logger.warning(f"Failed to sell 1 options of {get_option_name(call_option)}")

        await self.handle_call_sell_failure(call_option, sell_option_result, call_options, open_trades)
        self.no_call_options_above_minimal_sell_price = sell_option_result.no_option_above_minimal_sell_price
        return sell_option_result

    async def handle_call_sell_failure(self, call_option, sell_option_result, call_options, open_trades):
        if sell_option_result.required_initial_margin and not is_switched_to_overnight_trading():
            result = await self.try_to_reduce_initial_margin_for_call_options(call_option, sell_option_result.required_initial_margin, sell_option_result.initial_margin_after, call_options)
            position_options = self.trading_bot.get_short_options()
            number_of_position_calls = sum([-position.position for position in position_options if position.contract.right == 'C'])
            number_of_position_puts = sum([-position.position for position in position_options if position.contract.right == 'P'])
            if result == FAILED and number_of_position_calls * 2 < number_of_position_puts:
                logger.info(f"Margin lock detected, only {number_of_position_calls} call options versus {number_of_position_puts} put options")
                position_puts = [position.contract for position in position_options if position.contract.right == 'P']
                candidate = min(position_puts, key=lambda option: option.strike)
                is_margin_lock_trade_already_open = any(trade.contract.right == 'P' and trade.contract.strike == candidate.strike and trade.order.action.upper() == 'BUY' and trade.order.lmtPrice > 0.05 for trade in open_trades)
                if is_margin_lock_trade_already_open:
                    logger.info(f"Margin lock buy trade for {get_option_name(candidate)} is already open")
                elif not self.should_resolve_margin_locks:
                    logger.info("Margin lock resolution is disabled by configuration")
                else:
                    missing_sum = sell_option_result.required_initial_margin - sell_option_result.initial_margin_after
                    await self.try_to_resolve_margin_lock(candidate, missing_sum)
                    return

        self.try_to_publish_available_cheap_option('C')

    async def find_call_candidate(self, call_options, target_delta):
        strike_finder = StrikeFinder()
        call_option = await strike_finder.get_low_delta_call_option(call_options, target_delta)
        if not call_option:
            logger.error("Call option candidate for selling could not be found")
        return call_option

    async def calculate_max_allowed_call_options(self, call_option):
        logger.info(f"Testing sell capacity for {get_option_name(call_option)}")
        max_options = await self.net_worth_calculator.calculate_max_options_for_market_rise(call_option)
        if not max_options:
            logger.warning(f"Failed to sell {get_option_name(call_option)} due to projected exposure fee")
        return max_options

    def _update_last_option_price(self, price, right):
        if math.isnan(price):
            return

        if right == 'C':
            if self.last_call_option_price != price:
                logger.info(f"The current price level for call options changed from {self.last_call_option_price} to {price}")
                if self.last_call_option_price < 0.15 < price:
                    logger.info(f"However, this is a high jump, slowing the pace of increase so that the price level for call options is {price}")
                    price = 0.15
                self.last_call_option_price = price
        else:
            if self.last_put_option_price != price:
                logger.info(f"The current price level for put options changed from {self.last_put_option_price} to {price}")
                if self.last_put_option_price < 0.15 < price:
                    logger.info(f"However, this is a high jump, slowing the pace of increase so that the price level for put options is {price}")
                    price = 0.15
                self.last_put_option_price = price

    def try_to_publish_available_cheap_option(self, right):
        strike_finder = StrikeFinder()
        available_cheap_option = strike_finder.find_first_cheap_option(right)
        if available_cheap_option:
            reduction_data = {
                'option': get_option_name(available_cheap_option)
            }
            if right == 'C' and not self.call_margin_reduction:
                self.call_margin_reduction = reduction_data
                self.last_call_margin_reduction_record_time = time.time()
            elif right == 'P' and not self.put_margin_reduction:
                self.put_margin_reduction = reduction_data
                self.last_put_margin_reduction_record_time = time.time()

    async def try_to_sell(self, option, quantity, target_delta):
        delta = get_delta_for_sell(option.ticker)
        if delta > target_delta:
            logger.warning(f"Failed to sell {get_option_name(option)} since the delta has risen to {delta:.3f}, "
                           f"beyond the target delta of {target_delta:.3f}")
            result = SellOptionResult()
            result.success = False
            return result

        start_time = time.time()
        result = await self.trading_bot.try_to_sell(option, quantity, target_delta)
        end_time = time.time()
        duration_of_sell_operation = end_time - start_time

        delta = get_delta_for_sell(option.ticker)
        if result.success and delta > target_delta:
            logger.warning(f"After selling {get_option_name(option)},the delta has risen to {delta:.2f}, "
                           f"beyond the target delta of {target_delta:.2f}. "
                           f"The sell took {duration_of_sell_operation:.2f} seconds")

        if result.success:
            comment = f"Delta: {delta:.3f}, target delta: {target_delta:.3f}"
            req_id_to_comment[result.trade.order.orderId] = comment
            logger.info(f"Submitted sell option of {get_option_name(option)}, order ID: {result.trade.order.orderId}, "
                      f"target delta: {target_delta:.3f}")
        return result

    async def try_to_sell_put_options(self, open_trades, options):
        put_options = [option for option in options if option.right == 'P']
        if not put_options:
            logger.error("Np put options found")
            return SellOptionResult()

        target_delta_calculator = TargetDeltaCalculator()
        target_delta = await target_delta_calculator.calculate_target_delta('P')

        put_option = await self.find_put_candidate(put_options, target_delta)
        if not put_option:
            return SellOptionResult()

        estimated_sell_price = await self.estimate_sell_price(put_option)
        if estimated_sell_price < 0:
            logger.error("Failed to estimate selling price")
            return SellOptionResult()
        self._update_last_option_price(estimated_sell_price, 'P')

        stop_loss_per_option = self.max_loss_calculator.calculate_max_loss('P')
        if stop_loss_per_option < estimated_sell_price:
            logger.warning(f"Failed to sell {get_option_name(put_option)} since the acceptable loss ({stop_loss_per_option}) is smaller than the option price ({estimated_sell_price})")
            return SellOptionResult()

        max_options_for_market_drop = await self.calculate_max_allowed_put_options(put_option)
        if not max_options_for_market_drop:
            return SellOptionResult()

        await self.cancel_all_buy_trades(open_trades, put_option)

        quantity = min(max_options_for_market_drop, 2)
        sell_option_result = await self.try_to_sell(put_option, quantity, target_delta)
        if sell_option_result.success:
            return sell_option_result

        logger.warning(f"Failed to sell {quantity} options of {get_option_name(put_option)}")
        if quantity == 2:
            logger.info(f"Testing sell 1 option of {get_option_name(put_option)}")
            sell_option_result = await self.try_to_sell(put_option, 1, target_delta)
            if sell_option_result.success:
                return sell_option_result
            logger.warning(f"Failed to sell 1 options of {get_option_name(put_option)}")

        await self.handle_put_sell_failure(put_option, sell_option_result, put_options, open_trades)
        self.no_put_options_above_minimal_sell_price = sell_option_result.no_option_above_minimal_sell_price
        return sell_option_result

    async def find_put_candidate(self, put_options, target_delta):
        strike_finder = StrikeFinder()
        logger.info("Searching for a suitable low-delta put option")
        put_option = await strike_finder.get_low_delta_put_option(put_options, target_delta)
        if not put_option:
            logger.error("Put option candidate for selling could not be found")
        return put_option

    async def calculate_max_allowed_put_options(self, put_option):
        logger.info(f"Testing sell capacity for {get_option_name(put_option)}")
        max_options = await self.net_worth_calculator.calculate_max_options_for_market_drop(put_option)
        if not max_options:
            logger.warning(f"Failed to sell {get_option_name(put_option)} due to projected exposure fee")
        return max_options

    async def handle_put_sell_failure(self, put_option, sell_option_result, put_options, open_trades):
        if sell_option_result.required_initial_margin and not is_switched_to_overnight_trading():
            result = await self.try_to_reduce_initial_margin_for_put_options(put_option, sell_option_result.required_initial_margin, sell_option_result.initial_margin_after, put_options)
            position_options = self.trading_bot.get_short_options()
            number_of_position_calls = sum([-position.position for position in position_options if position.contract.right == 'C'])
            number_of_position_puts = sum([-position.position for position in position_options if position.contract.right == 'P'])
            if result == FAILED and number_of_position_puts * 2 < number_of_position_calls:
                logger.info(f"Margin lock detected, only {number_of_position_puts} put options versus {number_of_position_calls} call options")
                position_calls = [position.contract for position in position_options if
                                    position.contract.right == 'C']
                candidate = max(position_calls, key=lambda option: option.strike)
                is_margin_lock_trade_already_open = any(trade.contract.right == 'C' and trade.contract.strike == candidate.strike and trade.order.action.upper() == 'BUY' and trade.order.lmtPrice > 0.05 for trade in open_trades)
                if is_margin_lock_trade_already_open:
                    logger.info(f"Margin lock buy trade for {get_option_name(candidate)} is already open")
                elif not self.should_resolve_margin_locks:
                    logger.info("Margin lock resolution is disabled by configuration")
                else:
                    missing_sum = sell_option_result.required_initial_margin - sell_option_result.initial_margin_after
                    await self.try_to_resolve_margin_lock(candidate, missing_sum)
                    return

        self.try_to_publish_available_cheap_option('P')

    async def cancel_all_buy_trades(self, open_buy_trades, option):
        open_buy_trades_for_option = find_all_buy_trades(option, open_buy_trades)
        if not open_buy_trades_for_option:
            return

        for open_buy_trade in open_buy_trades_for_option:
            logger.info(f"Cancelling the buy order for {get_option_name(option)}")
            self.trading_bot.cancel_trade(open_buy_trade)

        # Wait for all trades to reach a final status
        for _ in range(50):
            if all(trade.isDone() for trade in open_buy_trades_for_option):
                break
            await asyncio.sleep(0.1)


    async def try_to_reduce_initial_margin_for_call_options(self, call_option_to_be_sold, required_initial_margin, initial_margin_after_sell, call_options):
        if self.last_call_option_price < MINIMAL_SELL_PRICE_FOR_GENERAL_MARGIN_REDUCTION:
            logger.info(
                f"Will not try to reduce initial margin for call options since the price level of call options is {self.last_call_option_price} while the minimal sell price to close is {MINIMAL_SELL_PRICE_FOR_GENERAL_MARGIN_REDUCTION}")
            return FAILED

        strike_finder = StrikeFinder()
        positions = self.trading_bot.get_short_options()
        min_strike = min(position.contract.strike for position in positions)
        available_cheap_call_option = await strike_finder.get_available_cheap_call_option(call_options, min_strike)
        initial_margin_change = await self.trading_bot.get_initial_margin_change(available_cheap_call_option, 1)
        logger.info(f"try_to_reduce_initial_margin_for_call_options, required initial margin: {required_initial_margin}, initial margin after sell: {initial_margin_after_sell},"
                    f"initial margin change due to buy: {initial_margin_change}, option to be sold: {get_option_name(call_option_to_be_sold)}, option to buy: {get_option_name(available_cheap_call_option)}")

        missing_sum = required_initial_margin - initial_margin_after_sell
        if initial_margin_change == 0:
            logger.info(f"Initial margin change for buying {get_option_name(available_cheap_call_option)} is 0, will not buy it")
            self.call_margin_reduction = {
                'option': get_option_name(available_cheap_call_option),
                'margin_deficiency': round(abs(missing_sum))
            }
            self.last_call_margin_reduction_record_time = time.time()
            return FAILED

        required_number_of_units = math.ceil(missing_sum / initial_margin_change)
        
        margin_change = round(abs(initial_margin_change))
        required_level = round(self.calculate_required_level(required_number_of_units), 2)
        if margin_change < 100:
            margin_change = 0
        if required_level > 100:
            required_level = 0

        self.call_margin_reduction = {
            'option': get_option_name(available_cheap_call_option),
            'margin_deficiency': round(abs(missing_sum)),
            'margin_change': margin_change,
            'required_level': required_level
        }
        self.last_call_margin_reduction_record_time = time.time()

        logger.info(f"try_to_reduce_initial_margin_for_call_options, required initial margin: {required_initial_margin}, initial margin after sell: {initial_margin_after_sell:.0f}, "
                    f"initial margin change due to buy: {initial_margin_change:.0f}, option to be sold: {get_option_name(call_option_to_be_sold)}, option to buy: {get_option_name(available_cheap_call_option)}, missing sum: {missing_sum:.0f}, required number of units = {required_number_of_units}, last call price: {self.last_call_option_price}")

        if required_number_of_units < 0:
            logger.error(f"The required number of units is {required_number_of_units}")
            return FAILED

        if self.last_call_option_price * 0.4 > 0.07 * required_number_of_units + 0.02:
            logger.info(f"Buying {required_number_of_units} units of {get_option_name(available_cheap_call_option)} would relax the required initial margin")
            trade = self.trading_bot.buy_low_cost(available_cheap_call_option, required_number_of_units)
            comment = f"Margin Reduction of {round(abs(initial_margin_change))}"
            req_id_to_comment[trade.order.orderId] = comment
            self.can_submit_orders = True
            return SUCCESS

        logger.info(f"Will not buy {get_option_name(available_cheap_call_option)} since the potential sell price is too low ({self.last_call_option_price})")
        return FAILED

    def calculate_required_level(self, required_number_of_units):
        """Calculates the required level based on the number of units."""
        return math.ceil((0.07 * required_number_of_units + 0.02) / 0.02) * 0.05

    async def try_to_reduce_initial_margin_for_put_options(self, put_option_to_be_sold, required_initial_margin, initial_margin_after_sell, put_options):
        if self.last_put_option_price < MINIMAL_SELL_PRICE_FOR_GENERAL_MARGIN_REDUCTION:
            logger.info(f"Will not try to reduce initial margin for put options since the price level of put options is {self.last_put_option_price} while the minimal sell price to close is {MINIMAL_SELL_PRICE_FOR_GENERAL_MARGIN_REDUCTION}")
            return FAILED

        strike_finder = StrikeFinder()
        positions = self.trading_bot.get_short_options()
        max_strike = max(position.contract.strike for position in positions)
        available_cheap_put_option = await strike_finder.get_available_cheap_put_option(put_options, max_strike)
        initial_margin_change = await self.trading_bot.get_initial_margin_change(available_cheap_put_option, 1)
        missing_sum = required_initial_margin - initial_margin_after_sell
        if initial_margin_change == 0:
            logger.info(f"Initial margin change for buying {get_option_name(available_cheap_put_option)} is 0, will not buy it")
            initial_margin_change = await self.trading_bot.get_initial_margin_change(available_cheap_put_option, 2)
            logger.info(
                f"Initial margin change for 2 units is {initial_margin_change}")
            self.put_margin_reduction = {
                'option': get_option_name(available_cheap_put_option),
                'margin_deficiency': round(abs(missing_sum))
            }
            self.last_put_margin_reduction_record_time = time.time()
            return FAILED

        required_number_of_units = math.ceil(missing_sum / initial_margin_change)
        margin_change = round(abs(initial_margin_change))
        required_level = round(self.calculate_required_level(required_number_of_units), 2)
        if margin_change < 100:
            margin_change = 0
        if required_level > 100:
            required_level = 0

        self.put_margin_reduction = {
            'option': get_option_name(available_cheap_put_option),
            'margin_deficiency': round(abs(missing_sum)),
            'margin_change': margin_change,
            'required_level': required_level
        }
        self.last_put_margin_reduction_record_time = time.time()

        logger.info(f"try_to_reduce_initial_margin_for_put_options, required initial margin: {required_initial_margin:.0f}, initial margin after sell: {initial_margin_after_sell:.0f}, "
                    f"initial margin change due to buy: {initial_margin_change:.0f}, option to be sold: {get_option_name(put_option_to_be_sold)}, option to buy: {get_option_name(available_cheap_put_option)}, missing sum: {missing_sum:.0f}, required number of units = {required_number_of_units}, last put price: {self.last_put_option_price}")

        if required_number_of_units < 0:
            logger.error(f"The required number of units is {required_number_of_units}")
            return FAILED

        if self.last_put_option_price * 0.4 > 0.07 * required_number_of_units + 0.02:
            logger.info(f"Buying {required_number_of_units} units of {get_option_name(available_cheap_put_option)} would relax the required initial margin")
            trade = self.trading_bot.buy_low_cost(available_cheap_put_option, required_number_of_units)
            comment = f"Margin Reduction of {round(abs(initial_margin_change))}"
            req_id_to_comment[trade.order.orderId] = comment
            self.can_submit_orders = True
            return SUCCESS

        logger.info(f"Will not buy {get_option_name(available_cheap_put_option)} since the potential sell price is too low ({self.last_put_option_price})")
        return FAILED

    async def estimate_sell_price(self, option):
        if math.isnan(option.ticker.bid) or math.isnan(option.ticker.ask):
            return option.ticker.last
        return await self.trading_bot.calculate_limit(option, option.ticker.bid, option.ticker.ask)

    async def try_to_resolve_margin_lock(self, candidate_option, missing_sum):
        if time.time() - self.last_margin_lock_resolution_attempt_time < 15 * 60:
            logger.warning(f"An attempt to resolve the margin lock has been carried out recently, more time is required for the next attempt")
            return

        initial_margin_change = await self.trading_bot.get_initial_margin_change(candidate_option, quantity=1, limit=0.1)
        if abs(initial_margin_change) < abs(missing_sum):
            logger.info(f"Initial margin change for buying {get_option_name(candidate_option)} is {initial_margin_change}, "
                        f"which is not enough to cover for the missing sum of {missing_sum}, will not buy it as part of margin lock resolution")
            return

        logger.info(f"Initial margin change for buying {get_option_name(candidate_option)} is {initial_margin_change}, "
                    f"which is enough to cover for the missing sum of {missing_sum}, going to buy it as part of margin lock resolution")
        trade = self.trading_bot.buy_low_cost(candidate_option, quantity=1, limit=0.1)
        comment = f"Margin lock resolution"
        req_id_to_comment[trade.order.orderId] = comment

    def notify_margin_lock_resolution_attempted(self):
        self.last_margin_lock_resolution_attempt_time = time.time()