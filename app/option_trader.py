import math
import asyncio

from utilities.ib_utils import get_delta
from utilities.utils import *

from .max_loss_calculator import calculate_max_loss
from .opportunity_explorer import OpportunityExplorer, calculate_max_options_for_market_drop, \
    calculate_max_options_for_market_rise, MINIMAL_SELL_PRICE_FOR_GENERAL_MARGIN_REDUCTION
from .positions_manager import PositionsManager, MINIMAL_SELL_PRICE_TO_CLOSE_POSITION
from .trading_bot import TradingBot
from .connection_manager import ConnectionManager
from .market_data_fetcher import MarketDataFetcher
from .target_delta_calculator import TargetDeltaCalculator
from .state_updater import StateUpdater, post_current_state


logger = logging.getLogger(__name__)
OPEN_SELL_ORDER_EXPIRATION_TIME = timedelta(minutes=20)
OPEN_GENERAL_MARGIN_REDUCTION_BUY_ORDER_EXPIRATION_TIME = timedelta(minutes=5)


class OptionTrader:
    def __init__(self):
        # Accessing singleton instances
        self.connection_manager = ConnectionManager()
        self.ib = self.connection_manager.ib
        self.trading_bot = TradingBot()
        self.opportunity_explorer = OpportunityExplorer()
        self.positions_manager = PositionsManager()
        self.market_data_fetcher = MarketDataFetcher()
        self.target_delta_calculator = TargetDeltaCalculator()
        self.state_updater = StateUpdater()
        
        self.connection_failure_start_time = None
        self.config = {}

    async def run(self):
        logger.info("OptionTrader: Starting trading loop...")
        await post_current_state({'status': 'Loading...'})

        while True:
            try:
                if not self.ib.isConnected():
                    logger.warning("OptionTrader: Task is waiting for IB connection...")
                    await asyncio.sleep(2)
                    continue

                write_heartbeat()

                # Consistent status message
                logger.info(f"OptionTrader: Checking market status...")

                is_open = is_market_open()
                state = {'market_state': 'Open' if is_open else 'Closed'}
                
                if is_open:
                    await self.trade()
                    state['status'] = 'Active'
                else:
                    await self.verify_no_open_trades()
                    state['status'] = 'Closed'

                # Report state to Render/Local JSON
                await self.state_updater.update_state(state)
                
                if self.connection_failure_start_time is not None:
                    logger.info("OptionTrader: Connection error resolved.")
                    self.connection_failure_start_time = None

                await self.yield_execution()

            except Exception:
                if self.connection_failure_start_time is None:
                    self.connection_failure_start_time = time.time()
                
                elapsed = time.time() - self.connection_failure_start_time
                logger.exception(f"OptionTrader: Loop error ({elapsed:.0f}s):")
                
                if elapsed > 300:
                    logger.error("OptionTrader: Persistent failure detected. Continuing to retry indefinitely...")
                
                # Attempt to report error status
                try:
                    await post_current_state({'status': 'Error'})
                except:
                    pass
                
                # Progressive backoff for sleep
                sleep_time = min(10 + (elapsed // 60) * 10, 60)
                await asyncio.sleep(sleep_time)

    async def yield_execution(self):
        write_heartbeat()
        await asyncio.sleep(0)

    async def trade(self):
        await self.positions_manager.manage_current_positions()
        await self.guard_pending_trades()
        await self.opportunity_explorer.explore_opportunities()

    async def guard_pending_trades(self):
        logger.info("Checking pending trades")
        positions, open_trades = await asyncio.gather(
            self.trading_bot.get_short_options(),
            self.trading_bot.get_open_trades()
        )
        open_sell_trades = [trade for trade in open_trades if trade.order.action.upper() == 'SELL']
        logger.info(f"Number of open sell trades: {len(open_sell_trades)}")
        target_delta = await self.target_delta_calculator.calculate_target_delta()
        logger.info(f"Target delta: {target_delta:.3f}")
        for open_sell_trade in open_sell_trades:
            logger.info(f"Working on open sell trade of option {get_option_name(open_sell_trade.contract)}")
            if open_sell_trade.log and open_sell_trade.log[0].status == 'Submitted':
                submission_entry = open_sell_trade.log[0]
                timezone = submission_entry.time.tzinfo
                if datetime.now(timezone) - submission_entry.time > OPEN_SELL_ORDER_EXPIRATION_TIME:
                    logger.info(
                        f"Cancelling sell of {get_option_name(open_sell_trade.contract)} since it has not been filled for the 20 minutes")
                    self.trading_bot.cancel_trade(open_sell_trade)
                    continue

            option = open_sell_trade.contract
            delta = get_delta(option.ticker)
            if delta > target_delta:
                logger.info(
                    f"Cancelling sell of {get_option_name(open_sell_trade.contract)} since the delta ({delta:.2f}) is higher than the target delta ({target_delta:.3f})")
                self.trading_bot.cancel_trade(open_sell_trade)
                continue

            remaining = open_sell_trade.remaining()
            if remaining and is_after_hours():
                if open_sell_trade.contract.right == 'P':
                    max_options_for_market_drop = await calculate_max_options_for_market_drop(open_sell_trade.contract)
                    if max_options_for_market_drop < remaining:
                        logger.info(
                            f"Cancelling sell of {get_option_name(open_sell_trade.contract)} to avoid exposure fee")
                        self.trading_bot.cancel_trade(open_sell_trade)
                        continue
                if open_sell_trade.contract.right == 'C':
                    max_options_for_market_rise = await calculate_max_options_for_market_rise(open_sell_trade.contract)
                    if max_options_for_market_rise < remaining:
                        logger.info(
                            f"Cancelling sell of {get_option_name(open_sell_trade.contract)} to avoid exposure fee")
                        self.trading_bot.cancel_trade(open_sell_trade)
                        continue

        logger.info("Checking for excessive buy-limit trades")
        buy_limit_trades = [trade for trade in open_trades if
                            trade.order.orderType == 'LMT' and trade.order.action.upper() == 'BUY']
        for buy_limit_trade in buy_limit_trades:
            option = buy_limit_trade.contract
            corresponding_position_found = any(option.conId == position.contract.conId for position in positions)
            option_ask_value = self.market_data_fetcher.get_ask(option)
            price_level = self.opportunity_explorer.last_call_option_price if option.right == 'C' else self.opportunity_explorer.last_put_option_price

            if corresponding_position_found and option_ask_value == 0.05:
                if price_level < MINIMAL_SELL_PRICE_TO_CLOSE_POSITION:
                    logger.info(f"Cancelling {get_option_name(option)} because it has the current price level for "
                                f"'{option.right}' is too low ({price_level}) for position buyback")
                    self.trading_bot.cancel_trade(buy_limit_trade)

            if not corresponding_position_found and option_ask_value > 0.05:
                logger.info(f"Cancelling {get_option_name(option)} because it has no corresponding "
                            f"position and it has an ask value of {option_ask_value}, so no reason to keep it")
                self.trading_bot.cancel_trade(buy_limit_trade)

            if not corresponding_position_found and option_ask_value == 0.05:
                if price_level < MINIMAL_SELL_PRICE_FOR_GENERAL_MARGIN_REDUCTION:
                    logger.info(f"Cancelling {get_option_name(option)} because it has the current price level for "
                                f"'{option.right}' is too low ({price_level}) for general margin reduction")
                    self.trading_bot.cancel_trade(buy_limit_trade)
                else:
                    submission_entry = buy_limit_trade.log[0]
                    timezone = submission_entry.time.tzinfo
                    if datetime.now(timezone) - submission_entry.time > OPEN_GENERAL_MARGIN_REDUCTION_BUY_ORDER_EXPIRATION_TIME:
                        logger.info(
                            f"Cancelling buy of {get_option_name(option)} since it has not been filled for the 5 minutes")
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
                required_max_loss_per_option = await calculate_max_loss(right, should_consider_only_effective=True)
                required_stop_loss = required_max_loss_per_option + sell_price
                stop_loss_ratio = required_stop_loss / current_stop_loss

                current_price = self.market_data_fetcher.get_last_price(option)
                logger.info(
                    f"Matching position found for {get_option_name(option)}, the current stop loss is {current_stop_loss:.2f} and the required "
                    f"stop loss is {required_stop_loss:.2f}, current price is {current_price}, "
                    f"stop loss ratio: ({stop_loss_ratio:.2f})")
                if math.isnan(current_price):
                    ticker = self.ib.ticker(option)
                    if ticker is None:
                        logger.info(f"The ticker for {get_option_name(option)} is missing")

                if stop_loss_ratio > 1.1 or (stop_loss_ratio < 0.9 and required_stop_loss > current_price * 2):
                    logger.info(f"Going to modify stop loss for {get_option_name(option)} from {current_stop_loss} to {required_stop_loss:.2f}, "
                                f"since the max loss ratio is {stop_loss_ratio:.2f}. Order status is {open_stop_loss_trade.orderStatus.status}")
                    await self.trading_bot.modify_stop_loss(open_stop_loss_trade, required_stop_loss)
                if stop_loss_ratio > 1.1 or (stop_loss_ratio < 0.9 and required_stop_loss <= current_price * 2):
                    logger.warning(f"Cannot modify stop loss because the required stop loss ratio ({required_stop_loss:.2f})"
                                   f"is too close the current price ({current_price})")

            if not matching_position:
                logger.info(f"Cancelling the stop loss for {get_option_name(open_stop_loss_trade.contract)} because it has no matching position")
                self.trading_bot.cancel_trade(open_stop_loss_trade)

    async def verify_no_open_trades(self):
        open_trades = await self.trading_bot.get_open_trades()
        for open_trade in open_trades:
            if open_trade.order.orderType != 'STP':
                self.trading_bot.cancel_trade(open_trade)
