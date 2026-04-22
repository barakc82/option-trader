import asyncio
import traceback

from utilities.utils import *
from utilities.ib_utils import connect

from app.configuration import should_guard_positions
from app.logging_setup import setup_logging
from app.market_data_fetcher import MarketDataFetcher
from app.positions_manager import PositionsManager
from app.trading_bot import TradingBot

OPTION_SAFEGUARD_CLIENT_ID = 2

logger = logging.getLogger(__name__)


class OptionSafeguard:
    def __init__(self, positions_manager: PositionsManager):
        self.order_id_to_submission_time = {}
        self.positions_manager = positions_manager
        self.tws_connection = None
        self.trading_bot = None

    def guard_current_positions(self):

        recent_trades = self.positions_manager.get_recent_trades()
        for recent_trade in recent_trades:
            logger.info(
                f"Recent filled trade: {recent_trade.option_name}, contract id {recent_trade.conId}, order type: {recent_trade.action}")

        logger.debug("Checking current positions")
        positions = self.trading_bot.get_short_options(should_use_cache=True)
        for position in positions:
            self.handle_current_risk(position)
        setup_logging()

    def handle_current_risk(self, position):

        option = position.contract
        if not hasattr(option, 'ticker') or option.ticker is None:
            ticker = current_thread.market_data_fetcher.ib.ticker(option)
            if ticker is None:
                logger.error(f"The ticker of {get_option_name(option)} is missing")
                ticker = current_thread.market_data_fetcher.req_mkt_data(option, is_snapshot=False)
                option.ticker = ticker
            else:
                logger.debug(f"The ticker of {get_option_name(option)} was found in search, attaching it to the contract")
                option.ticker = ticker
            return
        if datetime.now().astimezone() - option.ticker.time > timedelta(seconds=4):
            logger.debug(f"The ticker of {get_option_name(option)} is invalid, updating it")
            ticker = current_thread.market_data_fetcher.req_mkt_data(option, is_snapshot=False)
            option.ticker = ticker

        last_price = option.ticker.last
        stop_loss_trade = self.find_stop_loss_trade(position)
        if not stop_loss_trade:
            logger.warning(f"No stop loss is set for position of {get_option_name(option)}")
            self.tws_connection.ib.reqAllOpenOrders()
            return

        stop_loss = stop_loss_trade.order.auxPrice
        sell_price = position.avgCost / 100
        logger.debug(
            f"{get_option_name(option)}, Last price: {last_price:.2f}, Sell price: {sell_price:.2f}, Stop loss for option: {stop_loss:.2f}, time: {option.ticker.time}")

        if last_price >= 0.5 * stop_loss:
            logger.info(f"Watching the current price of {get_option_name(option)}: {last_price:.2f}, stop loss is at {stop_loss:.2f}")

        if last_price >= stop_loss:

            logger.warning(f"The current price of {get_option_name(option)} ({last_price}) is higher than the stop loss: {stop_loss:}")

            if self.positions_manager.is_recent_buy_filled(position):
                logger.info(f"Recent buy already filled, so not closing {get_option_name(option)}")
                self.tws_connection.ib.reqPositions()
                return

            pending_buy_trade = self.get_pending_buy(position)
            if pending_buy_trade and hasattr(pending_buy_trade, 'submission_time'):
                # order_id = pending_buy_trade.order.order_id
                # order_submission_time = self.order_id_to_submission_time[order_id]
                # trade_submission_time = pending_buy_trade.submission_time
                # if order_submission_time:
                if time.time() - pending_buy_trade.submission_time < 10:
                    logger.info(f"Recent buy already pending, so not trying to close {get_option_name(option)} yet")
                    self.tws_connection.ib.reqPositions()
                    return

                logger.info(f"Cancelling the buy of {get_option_name(option)} since it has been pending for too long")
                trade = self.trading_bot.cancel_trade(pending_buy_trade)
                if not is_trade_cancelled(trade):
                    logger.info(f"{get_option_name(option)} has not been cancelled yet")
                    return

                logger.info(f"{get_option_name(option)} is cancelled, continuing to a new close of the position")

            if is_regular_hours():
                # stop_loss_price = self.trading_bot.adjust_limit_to_market_rules(sell_price + stop_loss)
                is_stop_loss_exists = self.find_stop_loss_trade(position)
                if is_stop_loss_exists:
                    logger.info(f"Stop loss exists for {get_option_name(option)}, so not closing")
                    return

            logger.warning(
                f"Risky position {get_option_name(option)} during pre-market, current price is {last_price} and the stop loss is {stop_loss}")
            if should_guard_positions:
                logger.warning(f"Closing risky position {get_option_name(option)} during pre-market")
                comment = "Risky position close"
                pending_buy_trade = self.trading_bot.close_short_option_position(position)
                # pending_buy_trade = self.get_pending_buy(position)
                pending_buy_trade.submission_time = time.time()
                # self.order_id_to_submission_time[order_id] = time.time()

    def guard_option_risk(self):

        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            last_log_message_time = time.time()
            self.initialize()
            while True:
                if is_market_open():
                    try:
                        self.guard_current_positions()
                    except ConnectionError as e:
                        logger.error(f"{e}")
                        self.initialize()
                else:
                    logger.debug(f"Market is closed")
                if time.time() - last_log_message_time > 180:
                    logger.info(f"Option safeguard is still running")
                    last_log_message_time = time.time()
                sleep_time = 180 if is_regular_hours_with_after_hours() or not is_market_open() else 1
                time.sleep(sleep_time)
        except Exception:
            traceback.print_exc()
            logger.error("Unhandled exception:\n%s", traceback.format_exc())
            sys.exit(1)

    def initialize(self):
        if self.tws_connection:
            self.tws_connection.disconnect()
        self.tws_connection = connect(OPTION_SAFEGUARD_CLIENT_ID)
        self.tws_connection.ib.reqPositions()
        self.tws_connection.ib.reqAllOpenOrders()
        self.tws_connection.ib.orderStatusEvent += self.on_order_status_change
        current_thread.market_data_fetcher = MarketDataFetcher()
        self.trading_bot = TradingBot()

    def find_stop_loss_trade(self, position):

        option = position.contract
        open_trades = self.trading_bot.get_open_trades()
        for open_trade in open_trades:
            if (option.conId == open_trade.contract.conId and open_trade.order.orderType == 'STP'
                    and open_trade.remaining() == abs(position.position)):
                return open_trade
        return None

    def on_order_status_change(self, trade):
        if trade.orderStatus.status == 'Filled':
            if trade.contract.secType == 'OPT':
                self.positions_manager.on_fill(trade)
                return

        logger.info(
            f"Order status: {trade.orderStatus.status}, security type: {trade.contract.secType}, action: {trade.order.action}, quantity: {trade.order.totalQuantity}, client ID: {trade.order.clientId}")

    def get_pending_buy(self, position):
        open_trades = self.trading_bot.get_open_trades()
        open_buy_trades = [trade for trade in open_trades if trade.order.action.upper() == 'BUY' and
                           not is_trade_cancelled(trade) and trade.order.orderType == 'LMT']
        for open_buy_trade in open_buy_trades:
            if open_buy_trade.contract.conId == position.contract.conId:
                return open_buy_trade
        return None
