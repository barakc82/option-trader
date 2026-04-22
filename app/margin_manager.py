import sys
import logging

from utilities.utils import get_option_name
from app.account_data import AccountData

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

NO_LOCK = 'No Restriction'
CALLS_LOCK_PUTS = 'Calls lock puts'
PUTS_LOCK_CALLS = 'Puts lock calls'


class MarginManager:

    def __init__(self, trading_bot, positions_manager):
        self.account_data = AccountData()
        self.market_data_fetcher = trading_bot.market_data_fetcher
        self.trading_bot = trading_bot
        self.positions_manager = positions_manager
        self.margin_lock_state = NO_LOCK

    def manage_margin(self):
        cushion = self.account_data.get_cushion()
        logger.info(f"Cushion is {cushion:.2f}")
        excess_liquidity = self.account_data.get_excess_liquidity()
        if excess_liquidity < 0 or cushion < 0.1 or self.is_margin_lock():
            logger.info(f"Excess liquidity is: {excess_liquidity}")
            if excess_liquidity > 0:
                logger.info("Avoiding margin management since excess liquidity is positive")
                return

            positions = self.trading_bot.get_short_options()
            number_of_put_positions = sum(
                abs(position.position) for position in positions if position.contract.right == 'P')
            number_of_call_positions = sum(
                abs(position.position) for position in positions if position.contract.right == 'C')

            margin_items = self.account_data.get_margin_related_values()
            for item_name, item_value in margin_items.items():
                logger.info(f"{item_name}: {item_value}")

            logger.info(
                f"Number of put positions: {number_of_put_positions}, Number of call positions: {number_of_call_positions}")
            if number_of_put_positions < number_of_call_positions:
                logger.info("Call positions are suspected of margin barrier")
            elif number_of_put_positions > number_of_call_positions:
                logger.info("Put positions are suspected of margin barrier")

            restricting_option = None
            restricted_right = None
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
                    logger.error(f"Could not find any ask price for the {right} positions")
                    return
                logger.info(f"Checking margin barrier using {get_option_name(minimal_ask_option)}")
                open_trades = self.trading_bot.get_open_trades()
                open_sell_trades = [trade for trade in open_trades if trade.order.action.upper() == 'SELL']
                for open_sell_trade in open_sell_trades:
                    if open_sell_trade.contract.conId == minimal_ask_option.conId:
                        self.trading_bot.cancel_trade(open_sell_trade)
                order_state = self.trading_bot.preview_order_status(minimal_ask_option)
                if float(order_state.equityWithLoanAfter) == sys.float_info.max:
                    logger.error(f"Response has no real data, the market is probably closed")
                    return

                if order_state.warningText:
                    logger.warning(f"  Warning Text:           {order_state.warningText}")

                margin_relief = abs(float(order_state.initMarginChange))
                threshold = 5 + (minimal_ask * 100)
                logger.debug(f"Upper bound test: {threshold}")
                logger.debug(f"Margin relief: {margin_relief:.2f}")
                if threshold < margin_relief:
                    restricting_option = minimal_ask_option if restricting_option is None else None
                    logger.debug(f"Margin relief greater than threshold, not restricted ({right})")
                else:
                    restricted_right = right
                    logger.debug(f"Restricted, margin relief less than threshold ({right})")

            if restricting_option:
                logger.info(f"Restricting right: {restricting_option.right}, restricted right: {restricted_right}")
                self.margin_lock_state = CALLS_LOCK_PUTS if restricting_option.right == 'C' else PUTS_LOCK_CALLS
                self.positions_manager.handle_margin_lock(restricting_option)
            else:
                logger.info(f"No restriction found")

    def is_margin_lock(self):
        return self.margin_lock_state != NO_LOCK
