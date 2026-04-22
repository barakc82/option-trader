import math

from dataclasses import dataclass
from datetime import date

from utilities.utils import *
from utilities.ib_utils import req_id_to_comment, req_id_to_target_delta
from app.account_data import AccountData
from app.margin_manager import MarginManager
from app.max_loss_calculator import calculate_max_loss
from app.opportunity_explorer import OpportunityExplorer
from app.target_delta_calculator import TargetDeltaCalculator

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


def find_all_stop_loss_trades(option, open_stop_loss_trades):
    stop_loss_trades_for_position = []
    for open_stop_loss_trade in open_stop_loss_trades:
        if option.conId == open_stop_loss_trade.contract.conId and open_stop_loss_trade.order.orderType == 'STP':
            stop_loss_trades_for_position.append(open_stop_loss_trade)
    return stop_loss_trades_for_position


def find_all_sell_trades(option, open_sell_trades):
    open_sell_trades_for_position = []
    for open_sell_trade in open_sell_trades:
        if option.conId == open_sell_trade.contract.conId:
            open_sell_trades_for_position.append(open_sell_trade)
    return open_sell_trades_for_position


def find_limit_buy_trade(option, open_buy_trades):
    for open_buy_trade in open_buy_trades:
        if option.conId == open_buy_trade.contract.conId and open_buy_trade.order.action.upper() == 'BUY' and open_buy_trade.order.orderType == 'LMT':
            return open_buy_trade
    return None


@dataclass
class FilledTrade:
    option_name: str = ""
    action: str = ""
    conId: int = 0
    fill_time = 0


def is_switched_to_overnight_trading():
    now_in_nyc = datetime.now(new_york_timezone).time()
    return NEW_OPTION_EXPLORATION_START_TIME < now_in_nyc < AFTER_HOURS_END_TIME


class PositionsManager:

    def __init__(self, trading_bot):
        self.market_data_fetcher = current_thread.market_data_fetcher
        self.target_delta_calculator = TargetDeltaCalculator()
        self.account_data = AccountData()
        self.trading_bot = trading_bot
        self.margin_manager = None
        self.was_option_positions_data_changed = False
        self.filled_trades = []
        self.previous_number_of_options = 0

    def set_margin_manager(self, margin_manager: MarginManager):
        self.margin_manager = margin_manager

    def manage_current_positions(self):
        logger.info("Checking current positions")

        for _ in range(10):
            positions = self.trading_bot.get_short_options(should_use_cache=False)
            """number_of_puts = len([position for position in positions if position.contract.right == 'P'])
            number_of_calls = len([position for position in positions if position.contract.right == 'C'])
            minority_right = 'C' if number_of_puts > number_of_calls else 'P'"""
            """ can_submit_orders = self.update_option_quantities(positions)
            opportunity_explorer = OpportunityExplorer()
            if can_submit_orders:
                opportunity_explorer.can_submit_orders = True """
            open_trades = self.trading_bot.get_open_trades()
            open_buy_trades = [trade for trade in open_trades if trade.order.action.upper() == 'BUY' and
                               not is_trade_cancelled(trade) and trade.order.orderType == 'LMT']
            open_sell_trades = [trade for trade in open_trades if trade.order.action.upper() == 'SELL' and
                                not is_trade_cancelled(trade)]
            open_stop_loss_trades = [trade for trade in open_trades if trade.order.action.upper() == 'BUY' and
                                     not is_trade_cancelled(trade) and trade.order.orderType == 'STP']

            for position in positions:
                write_heartbeat()
                option = position.contract
                stop_loss_trades_for_position = find_all_stop_loss_trades(position.contract, open_stop_loss_trades)
                if len(stop_loss_trades_for_position) > 1:
                    for stop_loss_trade in stop_loss_trades_for_position:
                        self.trading_bot.cancel_trade(stop_loss_trade)
                    stop_loss_trades_for_position = []
                if len(stop_loss_trades_for_position) == 1:
                    stop_loss_trade = stop_loss_trades_for_position[0]
                    if stop_loss_trade.remaining() != abs(position.position):
                        self.trading_bot.cancel_trade(stop_loss_trade)
                        stop_loss_trades_for_position = []
                open_sell_trades_for_position = find_all_sell_trades(option, open_sell_trades)
                for open_sell_trade in open_sell_trades_for_position:
                    self.trading_bot.cancel_trade(open_sell_trade)

                """
                logger.info(f"barak: working on {get_option_name(option)}")
                if len(stop_loss_trades_for_position) > 0:
                    logger.info(f"barak: stop loss {len(stop_loss_trades_for_position)} exists: {get_option_name(stop_loss_trades_for_position[0].contract)}")
                else:
                    logger.info(f"barak: stop loss does not exists")
                """

                # Should     skip not after hours? (23:00-24:00) it was part of the code in the past for no apparent reason
                if not stop_loss_trades_for_position and not self.is_recent_buy_filled(position):
                    stop_loss_per_option = calculate_max_loss(option.right, should_consider_only_effective=True)
                    logger.info(f"Adding stop loss for {get_option_name(option)}, potential loss per option: {stop_loss_per_option}")
                    stop_loss_trade = self.trading_bot.add_stop_loss(position, stop_loss_per_option)
                    req_id_to_comment[stop_loss_trade.order.orderId] = "Stop loss activated"

                """
                if position.avgCost / 100 < MINIMAL_SELL_PRICE and (self.account_data.get_available_funds() >= 0 or position.contract.right == minority_right):
                    logger.info(
                        f"Position {get_option_name(option)} was sold at the price of {(position.avgCost / 100):.2f} so no point in buying it back")
                    continue
                """

                limit_buy_trade = find_limit_buy_trade(option, open_buy_trades)

                opportunity_explorer = OpportunityExplorer()
                current_price_level = opportunity_explorer.last_call_option_price if option.right == 'C' else opportunity_explorer.last_put_option_price
                if current_price_level < opportunity_explorer.calculate_minimal_sell_price_to_close_position(option.right):
                    options_type = 'Put' if option.right == 'P' else 'Call'
                    logger.info(
                        f"The current price level for {options_type} options is {current_price_level}, thus no point in buying back position {get_option_name(option)}")
                    if limit_buy_trade:
                        logger.info(
                            f"Cancelling a buy trade for position of {get_option_name(option)} since sell price for {options_type} options is too low ({current_price_level})")
                        self.trading_bot.cancel_trade(limit_buy_trade)
                    continue

                if limit_buy_trade:
                    if limit_buy_trade.remaining() == abs(position.position):
                        continue
                    else:
                        logger.info(
                            f"Cancelling a buy trade for position of {get_option_name(option)}, trade quantity: {limit_buy_trade.remaining()}, position quantity: {position.position}")
                        self.trading_bot.cancel_trade(limit_buy_trade)

                bid = option.ticker.bid
                ask = option.ticker.ask
                if not self.can_buy_options() or not bid or bid > 0.05 or not ask or ask > 0.2 or ask < 0:
                    continue

                if self.was_option_positions_data_changed:
                    logger.info(f"Option positions changed, re-checking current positions")
                    self.was_option_positions_data_changed = False
                    break

                logger.info(
                    f"Submitting a buy trade for position of {get_option_name(position.contract)}, quantity: {position.position}")
                self.trading_bot.close_position_at_limit(position, 0.05)

            if not self.was_option_positions_data_changed:
                logger.info("Checking of current positions is done")
                break

        self.was_option_positions_data_changed = False

    def on_fill(self, trade):
        logger.info(
            f"Trade filled, option: {get_option_name(trade.contract)}, action: {trade.order.action}, quantity filled: {trade.orderStatus.filled}, quantity remaining: {trade.orderStatus.remaining}, price: {trade.orderStatus.lastFillPrice}, order ID: {trade.order.orderId}")
        self.was_option_positions_data_changed = True
        filled_trade = FilledTrade()
        filled_trade.option_name = get_option_name(trade.contract)
        filled_trade.action = trade.order.action
        filled_trade.conId = trade.contract.conId
        filled_trade.fill_time = time.time()
        self.filled_trades.append(filled_trade)
        logger.info(f"on fill, Filled trades: {len(self.filled_trades)}")

        req_id_to_target_delta.pop(trade.order.orderId, None)

    def handle_negative_excess_liquidity_warning(self, excess_liquidity):

        logger.info(
            f"Checking margin how to handle negative excess liquidity warning, excess liquidity is {excess_liquidity}")
        positions = self.trading_bot.get_short_options()
        option_candidate_info_list = []
        for right in ['P', 'C']:
            minimal_ask = sys.float_info.max
            minimal_ask_option = None
            for position in positions:
                if not position.contract.secType == 'OPT' or position.position >= 0 or position.contract.right != right:
                    continue
                option = position.contract
                ask = self.market_data_fetcher.get_ask(option)
                if ask < minimal_ask:
                    minimal_ask_option = option
                    minimal_ask = ask

            if not minimal_ask_option:
                logger.info(f"Could not find a {right} option")
                continue

            logger.info(f"Checking relief in excess liquidity using {get_option_name(minimal_ask_option)}")
            order_state = self.trading_bot.preview_order_status(minimal_ask_option)
            if float(order_state.equityWithLoanAfter) == sys.float_info.max:
                logger.error(f"Response has no real data, the market is probably closed")
                continue

            if order_state.warningText:
                logger.warning(f"  Warning Text:           {order_state.warningText}")

            margin_relief = abs(float(order_state.initMarginChange))
            if margin_relief > abs(excess_liquidity):
                option_candidate_info = {'option': minimal_ask_option, 'ask': minimal_ask, 'relief': margin_relief}
                option_candidate_info_list.append(option_candidate_info)

        option_candidate_info_list.sort(key=lambda info_item: (info_item['ask'], -info_item['relief']))
        selected_option = option_candidate_info_list[0]
        logger.info(f"Selected option to relieve negative excess liquidity: {get_option_name(selected_option)}")
        trade = self.trading_bot.close_short_option(selected_option, 1)
        comment = "Negative excess liquidity relief"
        req_id_to_comment[trade.order.orderId] = comment
        logger.info(f"Comment on close: {comment}")


    def handle_margin_lock_warning(self):

        logger.info(f"Checking margin how to handle margin lock warning")
        positions = self.trading_bot.get_short_options()
        restricting_option = None
        restriction_found = False
        for right in ['P', 'C']:
            minimal_ask = sys.float_info.max
            minimal_ask_option = None
            for position in positions:
                if not position.contract.secType == 'OPT' or position.position >= 0 or position.contract.right != right:
                    continue
                option = position.contract
                ask = self.market_data_fetcher.get_ask(option)
                if ask < minimal_ask:
                    minimal_ask_option = option
                    minimal_ask = ask

            if not minimal_ask_option:
                logger.info(f"Could not find a {right} option")
                return
            logger.info(f"Checking margin barrier using {get_option_name(minimal_ask_option)}")
            order_state = self.trading_bot.preview_order_status(minimal_ask_option)
            if float(order_state.equityWithLoanAfter) == sys.float_info.max:
                logger.error(f"Response has no real data, the market is probably closed")
                return

            if order_state.warningText:
                logger.warning(f"  Warning Text:           {order_state.warningText}")

            margin_relief = abs(float(order_state.initMarginChange))
            threshold = 5 + (minimal_ask * 100)
            logger.debug(f"Right: {right}, Upper bound test: {threshold}, Margin relief: {margin_relief}")

            if threshold < margin_relief:
                restricting_option = minimal_ask_option
            else:
                restriction_found = True

        if restriction_found:
            trade = self.trading_bot.close_short_option(restricting_option, 1)
            comment = "Negative excess liquidity relief"
            req_id_to_comment[trade.order.orderId] = "Margin unlock due to warning"
            logger.info(f"Comment on close: {comment}")
        else:
            logger.info(f"No restriction found as part of margin unlock due to warning")

    def handle_margin_lock(self, restricting_option):

        restricting_right = restricting_option.right
        if not self.can_buy_options():
            logger.info("Cannot resolve margin lock because options should not be bought now")
            return

        opportunity_explorer = OpportunityExplorer()
        if restricting_right == 'P' and opportunity_explorer.no_call_options_above_minimal_sell_price:
            # Puts Restricting, calls restricted. Thus, we should buy puts only if we can sell calls
            logger.info("Should not resolve margin lock because calls cannot be sold")
            return
        if restricting_right == 'C' and opportunity_explorer.no_put_options_above_minimal_sell_price:
            logger.info("Should not resolve margin lock because puts cannot be sold")
            return

        positions = self.trading_bot.get_short_options()
        # check if restricted positions have positions whose bid is above minimum of 10c
        number_of_restricting_options = sum(
            abs(position.position) for position in positions if position.contract.right == restricting_right)
        number_of_restricted_options = sum(
            abs(position.position) for position in positions if position.contract.right != restricting_right)
        restriction_difference = number_of_restricting_options - number_of_restricted_options
        logger.info(f"Restriction difference: {restriction_difference}, impact: {restriction_difference * 0.05}")
        ask = self.market_data_fetcher.get_ask(restricting_option)
        if ask > restriction_difference * 0.05:
            logger.info(
                f"Margin will remain locked since the ask price ({ask}) is greater than the option number difference ({restriction_difference}) x 0.05")
            return

        logger.info(f"Buying 1 option of {get_option_name(restricting_option)} to unlock margin")
        trade = self.trading_bot.close_short_option(restricting_option, 1)
        comment = "Unlock margin"
        req_id_to_comment[trade.order.orderId] = comment
        logger.info(f"Comment on close: {comment}")

    def can_buy_options(self):
        return not is_final_hours() or not self.account_data.is_portfolio_margin()

    def calculate_max_options_for_market_rise(self, call_option):
        if not is_switched_to_overnight_trading():
            return sys.float_info.max

        fold_after_market_rise = 1 + 0.2
        current_price = self.market_data_fetcher.get_spx_price()
        price_after_market_rise = current_price * fold_after_market_rise
        if call_option.strike > price_after_market_rise:
            return sys.float_info.max

        positions = self.trading_bot.get_short_options()
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

        current_net_liquidation_value = self.account_data.get_net_liquidation_value()
        net_liquidation_value_after_rise = current_net_liquidation_value * fold_after_market_rise
        net_worth_after_rise = net_liquidation_value_after_rise - current_total_liability
        if net_worth_after_rise < 0:
            return 0

        liability_per_contract = (price_after_market_rise - call_option.strike) * 100
        return math.floor(net_worth_after_rise / liability_per_contract)

    def calculate_max_options_for_market_drop(self, put_option):
        if not is_switched_to_overnight_trading():
            return sys.float_info.max

        remaining_fraction_after_drop = 1 - 0.3
        current_price = self.market_data_fetcher.get_spx_price()
        price_after_drop = current_price * remaining_fraction_after_drop
        if put_option.strike < price_after_drop:
            logger.info(f"{get_option_name(put_option)} is lower than worst case scenario market drop")
            return sys.float_info.max

        positions = self.trading_bot.get_short_options()
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

        current_net_liquidation_value = self.account_data.get_net_liquidation_value()
        net_liquidation_value_after_drop = current_net_liquidation_value * remaining_fraction_after_drop
        net_worth_after_drop = net_liquidation_value_after_drop - current_total_liability
        if net_worth_after_drop < 0:
            logger.info(f"Negative net worth in case of a market drop")
            return 0

        liability_per_contract = (put_option.strike - price_after_drop) * 100
        return math.floor(net_worth_after_drop / liability_per_contract)

    def is_recent_order_filled(self, position, action):
        for filled_trade in self.filled_trades:
            if filled_trade.action == action and filled_trade.conId == position.contract.conId and time.time() - filled_trade.fill_time < 60:
                # if filled_trade.conId == position.contract.conId and time.time() - filled_trade.fill_time < 60:
                logger.info(
                    f"Found a matching recent filled trade: {filled_trade.option_name}, contract id {filled_trade.conId}, action: {filled_trade.action}")
                return True
        return False

    def is_recent_buy_filled(self, position):
        return self.is_recent_order_filled(position, 'BUY')

    def update_option_quantities(self, positions):
        current_number_of_options = sum(abs(position.position) for position in positions)
        if current_number_of_options != self.previous_number_of_options:
            logger.info("Change in number of options detected")
            self.previous_number_of_options = current_number_of_options
            return True
        return False

    def get_recent_trades(self):
        return [filled_trade for filled_trade in self.filled_trades if time.time() - filled_trade.fill_time < 300]
