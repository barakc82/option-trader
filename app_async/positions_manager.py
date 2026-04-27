import asyncio
import time
import logging

from utilities.utils import is_trade_cancelled, write_heartbeat, get_option_name, is_final_hours
from utilities.ib_utils import req_id_to_comment

from .max_loss_calculator import calculate_max_loss
from .opportunity_explorer import OpportunityExplorer
from .trading_bot import TradingBot


logger = logging.getLogger(__name__)


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
            self.filled_trades = []
            self.was_option_positions_data_changed = False
            logger.info("PositionsManager singleton initialized.")
            self._initialized = True


    def get_recent_trades(self):
        return [filled_trade for filled_trade in self.filled_trades if time.time() - filled_trade.fill_time < 300]

    def is_recent_order_filled(self, position, action):
        for filled_trade in self.filled_trades:
            if filled_trade.action == action and filled_trade.conId == position.contract.conId and time.time() - filled_trade.fill_time < 60:
                logger.info(
                    f"Found a matching recent filled trade: {filled_trade.option_name}, contract id {filled_trade.conId}, action: {filled_trade.action}")
                return True
        return False

    def is_recent_buy_filled(self, position):
        return self.is_recent_order_filled(position, 'BUY')

    def on_fill(self, trade):
        logger.info(f"Trade filled: {get_option_name(trade.contract)} {trade.order.action}")
        self.filled_trades.append(trade)
        now = time.time()
        self.filled_trades = [t for t in self.filled_trades if now - getattr(t, 'fill_time', now) < 3600]

    def find_all_stop_loss_trades(self, option, open_stop_loss_trades):
        stop_loss_trades_for_position = []
        for open_stop_loss_trade in open_stop_loss_trades:
            if option.conId == open_stop_loss_trade.contract.conId and open_stop_loss_trade.order.orderType == 'STP':
                stop_loss_trades_for_position.append(open_stop_loss_trade)
        return stop_loss_trades_for_position

    def find_all_sell_trades(self, option, open_sell_trades):
        open_sell_trades_for_position = []
        for open_sell_trade in open_sell_trades:
            if option.conId == open_sell_trade.contract.conId:
                open_sell_trades_for_position.append(open_sell_trade)
        return open_sell_trades_for_position

    def find_limit_buy_trade(self, option, open_buy_trades):
        for open_buy_trade in open_buy_trades:
            if option.conId == open_buy_trade.contract.conId and open_buy_trade.order.action.upper() == 'BUY' and open_buy_trade.order.orderType == 'LMT':
                return open_buy_trade
        return None

    async def manage_current_positions(self):
        logger.info("Checking current positions")
        for _ in range(10):
            positions, open_trades = await asyncio.gather(
                self.trading_bot.get_short_options(should_use_cache=False),
                self.trading_bot.get_open_trades()
            )
            open_buy_trades = [trade for trade in open_trades if trade.order.action.upper() == 'BUY' and
                               not is_trade_cancelled(trade) and trade.order.orderType == 'LMT']
            open_sell_trades = [trade for trade in open_trades if trade.order.action.upper() == 'SELL' and
                                not is_trade_cancelled(trade)]
            open_stop_loss_trades = [trade for trade in open_trades if trade.order.action.upper() == 'BUY' and
                                     not is_trade_cancelled(trade) and trade.order.orderType == 'STP']

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
                open_sell_trades_for_position = self.find_all_sell_trades(option, open_sell_trades)
                for open_sell_trade in open_sell_trades_for_position:
                    self.trading_bot.cancel_trade(open_sell_trade)

                if not stop_loss_trades_for_position and not self.is_recent_buy_filled(position):
                    stop_loss_per_option = await calculate_max_loss(option.right, should_consider_only_effective=True)
                    logger.info(f"Adding stop loss for {get_option_name(option)}, potential loss per option: {stop_loss_per_option}")
                    stop_loss_trade = await self.trading_bot.add_stop_loss(position, stop_loss_per_option)
                    req_id_to_comment[stop_loss_trade.order.orderId] = "Stop loss activated"

                limit_buy_trade = self.find_limit_buy_trade(option, open_buy_trades)
                opportunity_explorer = OpportunityExplorer()
                current_price_level = opportunity_explorer.last_call_option_price if option.right == 'C' else opportunity_explorer.last_put_option_price
                
                min_sell_price = await opportunity_explorer.calculate_minimal_sell_price_to_close_position(option.right)
                if current_price_level < min_sell_price:
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
                if not self.can_buy_options() or math.isnan(bid) or bid > 0.05 or math.isnan(ask) or ask > 0.2 or ask < 0:
                    continue

                if self.was_option_positions_data_changed:
                    logger.info(f"Option positions changed, re-checking current positions")
                    self.was_option_positions_data_changed = False
                    break

                logger.info(
                    f"Submitting a buy trade for position of {get_option_name(position.contract)}, quantity: {position.position}")
                await self.trading_bot.close_short_option(option, abs(position.position))

            if not self.was_option_positions_data_changed:
                logger.info("Checking of current positions is done")
                break

        self.was_option_positions_data_changed = False

    def can_buy_options(self):
        return not is_final_hours()
