import asyncio
from datetime import timedelta

from utilities.utils import *
from utilities.ib_utils import *

from .net_worth_calculator import NetWorthCalculator
from .price_levels_manager import PriceLevelsManager
from .opportunity_explorer import OpportunityExplorer, MINIMAL_SELL_PRICE_FOR_GENERAL_MARGIN_REDUCTION
from .positions_manager import MINIMAL_SELL_PRICE_TO_CLOSE_POSITION, PositionsManager
from .trading_bot import TradingBot
from .target_delta_calculator import TargetDeltaCalculator
from .strike_finder import StrikeFinder


logger = logging.getLogger(__name__)


class PendingTradesManager:
    """Guards the open (pending) trades: cancels sells that went stale, crossed their
    delta limit or would cause an exposure fee, and buy-limit orders that are no
    longer needed. A data member of OpportunityExplorer (its pending_trades_manager);
    OptionTrader calls guard_pending_trades through it every loop."""

    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(PendingTradesManager, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if not self._initialized:
            self.positions_manager = PositionsManager()
            self.trading_bot = TradingBot()
            self.net_worth_calculator = NetWorthCalculator()
            self.target_delta_calculator = TargetDeltaCalculator()
            self.options_of_submitted_ml_derived_sell_orders = []

            self._initialized = True

    @property
    def opportunity_explorer(self):
        # Looked up on use rather than stored in __init__: this manager is first created
        # inside OpportunityExplorer's own __init__, where calling OpportunityExplorer()
        # would re-enter that unfinished __init__.
        return OpportunityExplorer()

    async def guard_pending_trades(self):
        logger.info("Checking pending trades")
        positions = self.trading_bot.get_short_options()
        open_trades = self.trading_bot.get_open_trades()

        open_sell_trades = [trade for trade in open_trades if trade.order.action.upper() == 'SELL']
        logger.info(f"Number of open sell trades: {len(open_sell_trades)}")

        call_delta_task = self.target_delta_calculator.calculate_target_delta('C')
        put_delta_task = self.target_delta_calculator.calculate_target_delta('P')
        call_delta, put_delta = await asyncio.gather(call_delta_task, put_delta_task)
        
        target_deltas = {
            'C': call_delta,
            'P': put_delta
        }
        logger.info(f"Target deltas: C={target_deltas['C']:.3f}, P={target_deltas['P']:.3f}")

        strike_finder = StrikeFinder()
        strike_finder.load_config()
        max_deltas = strike_finder.max_delta

        for open_sell_trade in open_sell_trades:
            logger.info(f"Working on open sell trade of option {get_option_name(open_sell_trade.contract)}")
            time_passed_since_submission = get_time_passed_since_submission(open_sell_trade)
            expiration_time = get_open_sell_order_expiration_time()
            if time_passed_since_submission > expiration_time:
                logger.info(
                    f"Cancelling sell of {get_option_name(open_sell_trade.contract)} since it has not been filled for {expiration_time.total_seconds() / 60:.0f} minutes")
                self.trading_bot.cancel_trade(open_sell_trade)
                continue

            option = open_sell_trade.contract
            if not hasattr(option, "ticker") or option.ticker is None:
                logger.error(f"Option {get_option_name(option)} has a missing or an empty ticker field")
                continue
            delta = get_delta_for_sell(option.ticker)
            if delta is None:
                logger.error(f"Could not get the delta for sell for {get_option_name(option)}")
                continue

            # ML-derived sells are chosen by expected profit under {call,put}_max_delta rather
            # than the target delta, so they're held to the max delta and the others to the
            # target delta. An order without metadata is treated as not ML-derived.
            position_initial_state = self.trading_bot.req_id_to_order_metadata.get(open_sell_trade.order.orderId)
            is_ml_derived = position_initial_state is not None and position_initial_state.is_ml_derived
            max_delta = max_deltas[option.right]
            if is_ml_derived and delta > max_delta:
                logger.info(
                    f"Cancelling ML-derived sell of {get_option_name(open_sell_trade.contract)} since the delta ({delta:.3f}) is higher than the max delta ({max_delta:.3f})")
                self.trading_bot.cancel_trade(open_sell_trade)
                continue

            target_delta = target_deltas[option.right]
            if not is_ml_derived and delta > target_delta:
                logger.info(
                    f"Cancelling sell of {get_option_name(open_sell_trade.contract)} since the delta ({delta:.3f}) is higher than the target delta ({target_delta:.3f})")

                self.trading_bot.cancel_trade(open_sell_trade)
                continue

            remaining = open_sell_trade.remaining()
            if remaining:
                if open_sell_trade.contract.right == 'P':
                    max_options_for_market_drop = await self.net_worth_calculator.calculate_max_options_for_market_drop(open_sell_trade.contract)
                    if max_options_for_market_drop < remaining:
                        logger.info(
                            f"Cancelling sell of {get_option_name(open_sell_trade.contract)} to avoid exposure fee")
                        self.trading_bot.cancel_trade(open_sell_trade)
                if open_sell_trade.contract.right == 'C':
                    max_options_for_market_rise = await self.net_worth_calculator.calculate_max_options_for_market_rise(open_sell_trade.contract)
                    if max_options_for_market_rise < remaining:
                        logger.info(
                            f"Cancelling sell of {get_option_name(open_sell_trade.contract)} to avoid exposure fee")
                        self.trading_bot.cancel_trade(open_sell_trade)

        logger.info("Checking for collective excessive sell trades leading to exposure fee")
        exposure_result = await self.net_worth_calculator.ensure_safe_exposure_with_all_trades()
        if exposure_result == FAILED:
            max_time_passed_since_submission = timedelta(days=1)
            trade_to_cancel = None
            for open_sell_trade in open_sell_trades:
                time_passed_since_submission = get_time_passed_since_submission(open_sell_trade)
                if time_passed_since_submission > max_time_passed_since_submission:
                    max_time_passed_since_submission = time_passed_since_submission
                    trade_to_cancel = open_sell_trade
            if trade_to_cancel:
                logger.info(
                    f"Cancelling sell of {get_option_name(trade_to_cancel.contract)} because if all the trades get filled they will lead to exposure fee")
                self.trading_bot.cancel_trade(open_sell_trade)

        logger.info("Checking for excessive buy-limit trades")
        buy_limit_trades = [trade for trade in open_trades if
                            trade.order.orderType == 'LMT' and trade.order.action.upper() == 'BUY']

        available_ml_slots_count = {
            'C': self.positions_manager.get_available_ml_slots_count('C'),
            'P': self.positions_manager.get_available_ml_slots_count('P')
        }

        for buy_limit_trade in buy_limit_trades:
            option = buy_limit_trade.contract
            corresponding_position = next((position for position in positions if option.conId == position.contract.conId), None)
            #option_ask_value = self.market_data_fetcher.get_ask(option)
            order_limit = buy_limit_trade.order.lmtPrice
            price_level = PriceLevelsManager().get_last_option_price(option.right)
            time_passed_since_submission = get_time_passed_since_submission(buy_limit_trade)

            if corresponding_position and order_limit == 0.05:
                position_quantity = abs(corresponding_position.position)
                if (position_quantity != buy_limit_trade.remaining() and
                        not (PriceLevelsManager().is_last_option_ml_derived[option.right] and buy_limit_trade.remaining() != available_ml_slots_count[option.right])):
                    logger.info(f"Cancelling {get_option_name(option)} because the position has {position_quantity} units "
                                    f"while there are {buy_limit_trade.remaining()} remaining units in the trade")
                    logger.info(
                        f"barak: is last option ml derived: {PriceLevelsManager().is_last_option_ml_derived[option.right]}, available_ml_slots_count: {available_ml_slots_count[option.right]}")
                    self.trading_bot.cancel_trade(buy_limit_trade)
                elif price_level < MINIMAL_SELL_PRICE_TO_CLOSE_POSITION and time_passed_since_submission > POSITION_BUYBACK_ORDER_EXPIRATION_TIME:
                    logger.info(f"Cancelling {get_option_name(option)} because it has the current price level for "
                                f"'{option.right}' is too low ({price_level}) for position buyback")
                    self.trading_bot.cancel_trade(buy_limit_trade)
                elif option.right == self.options_of_submitted_ml_derived_sell_orders:
                    logger.info(f"Cancelling {get_option_name(option)} because the sell order of the ML-derived "
                                f"'{option.right}' option managed to get submitted")
                    self.trading_bot.cancel_trade(buy_limit_trade)

            if corresponding_position and order_limit == 0.1 and buy_limit_trade.remaining() == 1 and time_passed_since_submission > OPEN_SELL_ORDER_GENERAL_EXPIRATION_TIME:
                logger.info(f"Cancelling {get_option_name(option)} since this margin lock resolution order has not been filled for 20 minutes")
                self.trading_bot.cancel_trade(buy_limit_trade)

            if not corresponding_position and order_limit > 0.05:
                logger.info(f"Cancelling {get_option_name(option)} because it has no corresponding "
                            f"position and it has an order limit of {order_limit}, so no reason to keep it")
                self.trading_bot.cancel_trade(buy_limit_trade)

            if not corresponding_position and order_limit == 0.05:
                if price_level < MINIMAL_SELL_PRICE_FOR_GENERAL_MARGIN_REDUCTION:
                    logger.info(f"Cancelling {get_option_name(option)} because it has the current price level for "
                                f"'{option.right}' is too low ({price_level}) for general margin reduction")
                    self.trading_bot.cancel_trade(buy_limit_trade)
                elif time_passed_since_submission > OPEN_GENERAL_MARGIN_REDUCTION_BUY_ORDER_EXPIRATION_TIME:
                    logger.info(
                        f"Cancelling buy of {get_option_name(option)} since it has not been filled for the 5 minutes")
                    self.trading_bot.cancel_trade(buy_limit_trade)

        self.options_of_submitted_ml_derived_sell_orders.clear()

    def notify_ml_derived_option_sell_submitted(self, option):
        self.options_of_submitted_ml_derived_sell_orders.append(option)
