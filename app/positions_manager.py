import asyncio

from utilities.utils import is_trade_cancelled, write_heartbeat, get_option_name, is_final_hours
from utilities.ib_utils import *

from .max_loss_calculator import MaxLossCalculator
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
            self.max_loss_calculator = MaxLossCalculator()
            self.done_contract_ids = set()
            logger.info("PositionsManager singleton initialized.")
            self._initialized = True


    def find_low_limit_buy_trade(self, option, open_buy_trades) -> Trade | None:
        for open_buy_trade in open_buy_trades:
            if (option.conId == open_buy_trade.contract.conId and open_buy_trade.order.action.upper() == 'BUY' and
                open_buy_trade.order.orderType == 'LMT' and open_buy_trade.order.lmtPrice == 0.05):
                return open_buy_trade
        return None

    async def manage_current_positions(self):
        logger.info("Checking current positions")
        positions = self.trading_bot.get_short_options()
        open_trades = self.trading_bot.get_open_trades()

        open_buy_trades = [trade for trade in open_trades if trade.order.action.upper() == 'BUY' and
                           not is_trade_cancelled(trade) and trade.order.orderType == 'LMT']

        current_con_ids = {p.contract.conId for p in positions}
        self.done_contract_ids &= current_con_ids

        for position in positions:
            write_heartbeat()
            option = position.contract

            opportunity_explorer = OpportunityExplorer()
            current_price_level = opportunity_explorer.last_call_option_price if option.right == 'C' else opportunity_explorer.last_put_option_price

            if current_price_level < MINIMAL_SELL_PRICE_TO_CLOSE_POSITION:
                continue

            limit_buy_trade = self.find_low_limit_buy_trade(option, open_buy_trades)
            if limit_buy_trade:
                continue

            if not hasattr(option, "ticker") or option.ticker is None:
                logger.info(f"Option {get_option_name(option)} has no ticker")
                continue

            bid = option.ticker.bid
            ask = option.ticker.ask
            if not self.can_buy_options() or math.isnan(bid) or bid > 0.05 or math.isnan(ask) or ask > 0.2 or ask < 0:
                continue

            logger.info(
                f"Submitting a buy trade for position of {get_option_name(position.contract)}, quantity: {position.position}, bid is {bid}, current price level is {current_price_level}")
            close_position_trade = await self.trading_bot.close_short_option(option, abs(position.position), limit=0.05)
            req_id_to_comment[close_position_trade.order.orderId] = "Position buyback"


    def can_buy_options(self):
        return not is_final_hours()

    def on_fill(self, trade):
        logger.info(f"Trade filled: {get_option_name(trade.contract)} {trade.order.action}")
        if trade.order.action.upper() == 'BUY':
            self.done_contract_ids.add(trade.contract.conId)
        if trade.order.orderId in req_id_to_comment and "Margin" in req_id_to_comment[trade.order.orderId]:
            opportunity_explorer = OpportunityExplorer()
            opportunity_explorer.notify_margin_lock_resolution_attempted()
