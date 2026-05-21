import asyncio
import time
import math
import logging
from datetime import timedelta
from typing import Any

from ib_insync import Trade

from utilities.utils import is_trade_cancelled, write_heartbeat, get_option_name, is_final_hours
from utilities.ib_utils import req_id_to_comment, MINIMAL_SELL_PRICE, find_high_limit_buy_trade, \
    POSITION_BUYBACK_ORDERR_EXPIRATION_TIME, get_time_passed_since_submission

from .max_loss_calculator import calculate_max_loss
from .opportunity_explorer import OpportunityExplorer
from .trading_bot import TradingBot


logger = logging.getLogger(__name__)

MINIMAL_SELL_PRICE_TO_CLOSE_POSITION = MINIMAL_SELL_PRICE + 0.05


class PositionsManager:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(PositionsManager, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if not self._initialized:
            # Accessing the TradingBot singleton internally
            self.trading_bot = TradingBot()
            self.done_contract_ids = set()
            logger.info("PositionsManager singleton initialized.")
            self._initialized = True


    def find_all_stop_loss_trades(self, option, open_stop_loss_trades):
        stop_loss_trades_for_position = []
        for open_stop_loss_trade in open_stop_loss_trades:
            if option.conId == open_stop_loss_trade.contract.conId and open_stop_loss_trade.order.orderType == 'STP LMT':
                stop_loss_trades_for_position.append(open_stop_loss_trade)
        return stop_loss_trades_for_position


    def find_low_limit_buy_trade(self, option, open_buy_trades):
        for open_buy_trade in open_buy_trades:
            if (option.conId == open_buy_trade.contract.conId and open_buy_trade.order.action.upper() == 'BUY' and
                open_buy_trade.order.orderType == 'LMT' and open_buy_trade.order.lmtPrice == 0.05):
                return open_buy_trade
        return None

    async def manage_current_positions(self):
        logger.info("Checking current positions")
        positions, open_trades = await asyncio.gather(
            self.trading_bot.get_short_options(),
            self.trading_bot.get_open_trades()
        )
        open_buy_trades = [trade for trade in open_trades if trade.order.action.upper() == 'BUY' and
                           not is_trade_cancelled(trade) and trade.order.orderType == 'LMT']
        open_stop_loss_trades = [trade for trade in open_trades if trade.order.action.upper() == 'BUY' and
                                 not is_trade_cancelled(trade) and trade.order.orderType == 'STP LMT']

        current_con_ids = {p.contract.conId for p in positions}
        self.done_contract_ids &= current_con_ids

        for position in positions:
            write_heartbeat()
            option = position.contract
            stop_loss_trades_for_position = self.find_all_stop_loss_trades(position.contract, open_stop_loss_trades)
            if len(stop_loss_trades_for_position) > 1:
                for stop_loss_trade in stop_loss_trades_for_position:
                    self.trading_bot.cancel_trade(stop_loss_trade)
                stop_loss_trades_for_position = []
            if len(stop_loss_trades_for_position) == 1:
                stop_loss_trade = stop_loss_trades_for_position[0]
                if stop_loss_trade.remaining() != abs(position.position):
                    self.trading_bot.cancel_trade(stop_loss_trade)
                    stop_loss_trades_for_position = []

            high_limit_buy_trade = find_high_limit_buy_trade(option, open_buy_trades)
            if not stop_loss_trades_for_position and not high_limit_buy_trade:
                stop_loss_per_option = await calculate_max_loss(option.right, should_consider_only_effective=True)
                logger.info(f"Adding stop loss for {get_option_name(option)}, potential loss per option: {stop_loss_per_option}")
                stop_loss_trade = await self.trading_bot.add_stop_loss(position, stop_loss_per_option)
                req_id_to_comment[stop_loss_trade.order.orderId] = "Stop loss activated"

            limit_buy_trade = self.find_low_limit_buy_trade(option, open_buy_trades)
            opportunity_explorer = OpportunityExplorer()
            current_price_level = opportunity_explorer.last_call_option_price if option.right == 'C' else opportunity_explorer.last_put_option_price

            if current_price_level < MINIMAL_SELL_PRICE_TO_CLOSE_POSITION:
                options_type = 'Put' if option.right == 'P' else 'Call'
                if limit_buy_trade:
                    time_passed_since_submission = get_time_passed_since_submission(limit_buy_trade)
                    if time_passed_since_submission > POSITION_BUYBACK_ORDERR_EXPIRATION_TIME:
                        logger.info(
                            f"Cancelling a buy trade for position of {get_option_name(option)} since sell price for {options_type} options is too low ({current_price_level})")
                        self.trading_bot.cancel_trade(limit_buy_trade)
                    else:
                        logger.info(
                            f"The current price level for {options_type} options is {current_price_level}, but keeping buy trade for position {get_option_name(option)} as it was recently submitted")
                else:
                    logger.info(
                        f"The current price level for {options_type} options is {current_price_level}, thus no point in buying back position {get_option_name(option)}")
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
            if not self.can_buy_options() or math.isnan(bid) or bid > 0.05 or math.isnan(ask) or ask > 0.2 or ask < 0:
                continue

            logger.info(
                f"Submitting a buy trade for position of {get_option_name(position.contract)}, quantity: {position.position}, bid is {bid}")
            close_position_trade = await self.trading_bot.close_short_option(option, abs(position.position), limit=0.05)
            req_id_to_comment[close_position_trade.order.orderId] = "Position buyback"


    def can_buy_options(self):
        return not is_final_hours()

    def on_fill(self, trade):
        logger.info(f"Trade filled: {get_option_name(trade.contract)} {trade.order.action}")
        self.done_contract_ids.add(trade.contract.conId)
