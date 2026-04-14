import math
import random

from ib_insync import Option

from account_data import AccountData
from max_loss_calculator import calculate_max_loss
from margin_manager import MarginManager
from opportunity_explorer import OpportunityExplorer, calculate_max_options_for_market_drop, \
    calculate_max_options_for_market_rise
from option_safeguard import OptionSafeguard
from positions_manager import PositionsManager
from sheet_updater import SheetUpdater
from state_updater import StateUpdater, post_current_state
from target_delta_calculator import TargetDeltaCalculator
from ib_utils import *
from market_data_fetcher import MarketDataFetcher
from trading_bot import TradingBot

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

OPTION_TRADER_CLIENT_ID = 1
RECONNECT_WAIT_TIME = 300
OPEN_ORDER_EXPIRATION_TIME = timedelta(hours=1)


"""
 Todo:
 - market is open (holidays)
  - lower target delta over the weekend (maybe)
 - display colors for excess liquidation
 - automatically update report
 - display profit
  - handle [ERROR] 09:03:59 - ib_insync.ib: positions request timed out when connecting to tws (after night restart)
  - if req positions timed out persists - restart tws
  - Buy back options at 80% of the average cost
  - Discard open trade after 3 hours
  - Detect the case where no option data is available, then reconnect data farms
  - Check why sometimes reported delta of the selected option is greater than the target delta 
 """


def calculate_number_of_options(positions):
    return sum(abs(position.position) for position in positions)


def sleep():
    write_heartbeat()
    sleep_time_in_seconds = 180
    if is_market_open() or is_buffer_time_around_trade_time():
        sleep_time_in_seconds = 90 if is_in_docker() else 180
    if is_early_closing_hours():
        sleep_time_in_seconds = 40
    logger.info(f"Sleeping for {sleep_time_in_seconds // 60} minutes")

    times = sleep_time_in_seconds // 10
    for _ in range(times):
        write_heartbeat()
        time.sleep(10)


class OptionTrader:
    def __init__(self):

        self.tws_connection = None
        self.target_delta_calculator = None
        self.account_data = None
        self.trading_bot: TradingBot | None = None
        self.positions_manager: PositionsManager | None = None
        self.opportunity_explorer: OpportunityExplorer | None = None
        self.last_disconnect_time = 0
        self.last_liquidation_time = 0
        self.last_liquidation_alert_time = 0

    def trade_continuously(self):

        logger.info("Starting option trader...")
        write_heartbeat()
        self.initialize()

        margin_manager = MarginManager(self.trading_bot, self.positions_manager)
        self.positions_manager.set_margin_manager(margin_manager)

        option_safeguard = OptionSafeguard(self.positions_manager)
        option_safeguard_thread = threading.Thread(target=option_safeguard.guard_option_risk, daemon=True)
        option_safeguard_thread.start()

        sheet_updater = SheetUpdater()
        state_updater = StateUpdater(self.trading_bot)

        status = 'Initialized'
        while True:
            state = {}
            write_heartbeat()
            is_market_open_result = is_market_open()
            try:
                if is_market_open_result:
                    if not option_safeguard_thread.is_alive():
                        logger.error("Option safeguard thread crashed, exiting...")
                        return
                    self.positions_manager.manage_current_positions()
                    self.guard_pending_trades()
                    margin_manager.manage_margin()
                    write_heartbeat()
                    self.opportunity_explorer.explore_opportunities()
                    # sheet_updater.update()
                    status = 'Active'
                else:
                    logger.info(f"Market is closed")
                    if is_night_break():
                        self.verify_no_open_trades()
                    if current_thread.market_data_fetcher.is_connected():
                        status = 'Active'
                    else:
                        if status != 'Connection lost':
                            self.last_disconnect_time = time.time()
                            status = 'Connection lost'
                        logger.error(f"TWS connection is lost")
                        if time.time() - self.last_disconnect_time > RECONNECT_WAIT_TIME:
                            logger.info("Reconnect takes too much time, exiting...")
                            return

                previous_status = state['status'] if 'status' in state else None
                state['status'] = status
                state['market_state'] = 'Open' if is_market_open_result else 'Closed'
                is_liquidation_recent = time.time() - self.last_liquidation_time < 24 * 3600
                state['liquidation_time'] = self.last_liquidation_time if is_liquidation_recent else 0
                is_liquidation_alert_recent = time.time() - self.last_liquidation_alert_time < 24 * 3600
                state['liquidation_alert_time'] = self.last_liquidation_alert_time if is_liquidation_alert_recent else 0
                state['last_put_option_price'] = round(self.opportunity_explorer.last_put_option_price, 2)
                state['last_call_option_price'] = round(self.opportunity_explorer.last_call_option_price, 2)
                state[
                    'put_options_above_minimal_sell_price'] = not self.opportunity_explorer.no_put_options_above_minimal_sell_price
                state[
                    'call_options_above_minimal_sell_price'] = not self.opportunity_explorer.no_call_options_above_minimal_sell_price
                state['margin_lock'] = margin_manager.margin_lock_state
                if is_market_open_result or is_buffer_time_around_trade_time() or previous_status != 'Active':
                    state = state_updater.update_state(state)
            except ConnectionError as e:
                logger.error(f"Got a connection error: {e}")
                self.tws_connection.disconnect()
                if status != 'Recovering from connection error':
                    self.last_disconnect_time = time.time()
                    status = 'Recovering from connection error'
                    state['status'] = status
                if time.time() - self.last_disconnect_time > RECONNECT_WAIT_TIME:
                    logger.info("Cannot overcome connection error, exiting...")
                    return
                self.initialize()
                post_current_state(state)
            sleep()

    def initialize(self):
        self.tws_connection = connect(OPTION_TRADER_CLIENT_ID)
        current_thread.market_data_fetcher = MarketDataFetcher()
        self.target_delta_calculator = TargetDeltaCalculator()
        self.account_data = AccountData()
        current_thread.trading_bot = TradingBot()
        self.trading_bot = current_thread.trading_bot
        self.positions_manager = PositionsManager(self.trading_bot)
        self.opportunity_explorer = OpportunityExplorer()

        self.tws_connection.ib.orderStatusEvent += self.on_order_status_change
        self.tws_connection.ib.errorEvent += self.on_error

    def guard_pending_trades(self):
        logger.info("Checking pending trades")
        open_trades = self.trading_bot.get_open_trades()
        open_sell_trades = [trade for trade in open_trades if trade.order.action.upper() == 'SELL']
        logger.info(f"Number of open sell trades: {len(open_sell_trades)}")
        target_delta = self.target_delta_calculator.calculate_target_delta()
        logger.info(f"Target delta: {target_delta:.3f}")
        for open_sell_trade in open_sell_trades:
            logger.info(f"Working on open sell trade of option {get_option_name(open_sell_trade.contract)}")
            if open_sell_trade.log and open_sell_trade.log[0].status == 'Submitted':
                submission_entry = open_sell_trade.log[0]
                timezone = submission_entry.time.tzinfo
                if datetime.now(timezone) - submission_entry.time > OPEN_ORDER_EXPIRATION_TIME:
                    logger.info(
                        f"Cancelling sell of {get_option_name(open_sell_trade.contract)} since it has not been filled for the past hour")
                    self.trading_bot.cancel_trade(open_sell_trade)
                    continue

            option = open_sell_trade.contract
            model_greeks = option.ticker.modelGreeks
            if model_greeks and model_greeks.delta and abs(model_greeks.delta) > target_delta:
                logger.info(
                    f"Cancelling sell of {get_option_name(open_sell_trade.contract)} since the delta ({abs(model_greeks.delta):.2f}) is lower than the target delta ({target_delta:.3f})")
                self.trading_bot.cancel_trade(open_sell_trade)
                continue

            remaining = open_sell_trade.remaining()
            if remaining and (self.opportunity_explorer.should_cancel_all_sell_orders or is_after_hours()):
                result = self.trading_bot.test_order(open_sell_trade.contract, remaining, open_sell_trade.order.lmtPrice)
                if result.is_low_projected_cushion:
                    logger.info(
                        f"Cancelling sell of {get_option_name(open_sell_trade.contract)} since the projected cushion is too low")
                    self.trading_bot.cancel_trade(open_sell_trade)
                    continue
                if open_sell_trade.contract.right == 'P':
                    max_options_for_market_drop = calculate_max_options_for_market_drop(open_sell_trade.contract)
                    if max_options_for_market_drop < remaining:
                        logger.info(
                            f"Cancelling sell of {get_option_name(open_sell_trade.contract)} to avoid exposure fee")
                        self.trading_bot.cancel_trade(open_sell_trade)
                        continue
                if open_sell_trade.contract.right == 'C':
                    max_options_for_market_rise = calculate_max_options_for_market_rise(open_sell_trade.contract)
                    if max_options_for_market_rise < remaining:
                        logger.info(
                            f"Cancelling sell of {get_option_name(open_sell_trade.contract)} to avoid exposure fee")
                        self.trading_bot.cancel_trade(open_sell_trade)
                        continue

        positions = self.trading_bot.get_short_options()


        logger.info("Checking for excessive buy-limit trades")
        # Verify that each buy-limit trade has a corresponding position or has an ask value of 0.05/
        # No other case of buy-limit should occur. We can buy an option in order to close a position, or
        # we can buy an option with an ask of 0.05 in order to improve the margin.
        buy_limit_trades = [trade for trade in open_trades if
                            trade.order.orderType == 'LMT' and trade.order.action.upper() == 'BUY']
        for buy_limit_trade in buy_limit_trades:
            corresponding_position_found = any(
                buy_limit_trade.contract.conId == position.contract.conId for position in positions)
            option_ask_value = current_thread.market_data_fetcher.get_ask(buy_limit_trade.contract)
            if (not corresponding_position_found and option_ask_value > 0.05):
                logger.info(f"Cancelling {get_option_name(buy_limit_trade.contract)} because it has no corresponding "
                            f"position and it has an ask value of {option_ask_value}, so no reason to keep it")
                self.trading_bot.cancel_trade(buy_limit_trade)

        logger.info("Checking for invalid stop loss trades")
        open_stop_loss_trades = [trade for trade in open_trades if trade.order.orderType == 'STP']
        for open_stop_loss_trade in open_stop_loss_trades:
            matching_position = next(
                (position for position in positions
                 if position.contract.conId == open_stop_loss_trade.contract.conId),
                None
            )

            if matching_position:
                option = open_stop_loss_trade.contract
                current_stop_loss = open_stop_loss_trade.order.auxPrice
                sell_price = matching_position.avgCost / 100
                right = option.right
                required_max_loss_per_option = calculate_max_loss(right, should_consider_only_effective=True)
                required_stop_loss = required_max_loss_per_option + sell_price
                stop_loss_ratio = required_stop_loss / current_stop_loss

                market_data_fetcher = current_thread.market_data_fetcher

                current_price = market_data_fetcher.get_last_price(option)
                logger.info(
                    f"Matching position found for {get_option_name(option)}, the current stop loss is {current_stop_loss:.2f} and the required "
                    f"stop loss is {required_stop_loss:.2f}, current price is {current_price}, "
                    f"stop loss ratio: ({stop_loss_ratio:.2f})")
                if math.isnan(current_price):
                    ticker = self.tws_connection.ib.ticker(option)
                    if ticker is None:
                        logger.info(f"The ticker for {get_option_name(option)} is missing")

                if stop_loss_ratio > 1.1 or (stop_loss_ratio < 0.9 and required_stop_loss > current_price * 2):
                    logger.info(f"Going to modify stop loss for {get_option_name(option)}, since the max loss ratio between the required stop loss ({required_stop_loss:.2f} and the current "
                                f"stop loss {current_stop_loss:.2f} is {stop_loss_ratio:.2f}.")
                    # matching_position = None
                    self.trading_bot.modify_stop_loss(open_stop_loss_trade, required_stop_loss)
                if stop_loss_ratio > 1.1 or (stop_loss_ratio < 0.9 and required_stop_loss <= current_price * 2):
                    logger.warning(f"Cannot modify stop loss because the required stop loss ratio ({required_stop_loss:.2f})"
                                   f"is too close the current price ({current_price}")

            if not matching_position:
                self.trading_bot.cancel_trade(open_stop_loss_trade)

    def on_order_status_change(self, trade):
        order_status = trade.orderStatus
        if order_status.status == 'Filled':
            if isinstance(trade.contract, Option):
                self.positions_manager.on_fill(trade)
                return
            else:
                for fill in trade.fills:
                    if fill.execution.liquidation == 1:
                        self.last_liquidation_time = time.time()

        logger.info(
            f"Order status: {order_status.status}, security type: {trade.contract.secType}, action: {trade.order.action}, quantity: {trade.order.totalQuantity}")

    async def on_error(self, reqId, errorCode, errorString, contract):
        if errorCode == 2148:
            logger.error("Margin warning received!")
            logger.error(f"Details: {errorString}")
            self.last_liquidation_alert_time = time.time()
            margin_items = await self.account_data.get_margin_related_values_async()
            for item_name, item_value in margin_items.items():
                logger.info(f"{item_name}: {item_value}")
            if errorString.startswith("WARNING: Please note that the qualifying equity within your account"):
                logger.info("Liquidation is not likely due to the warning text")
                return
            if is_regular_hours():
                excess_liquidity = self.account_data.get_excess_liquidity()
                if excess_liquidity < 0:
                    self.positions_manager.handle_negative_excess_liquidity_warning(excess_liquidity)
                else:
                    self.positions_manager.handle_margin_lock_warning()
        elif errorCode not in [201, 202, 2104, 2106, 2109, 2158]:
            logger.warning(f"Other error ({errorCode}): {errorString}")

    def verify_no_open_trades(self):
        open_trades = self.trading_bot.get_open_trades()
        for open_trade in open_trades:
            if open_trade.order.orderType != 'STP':
                self.trading_bot.cancel_trade(open_trade)
