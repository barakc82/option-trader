import math
import asyncio
from datetime import datetime, timedelta

from utilities.utils import *
from utilities.ib_utils import *

from .max_loss_calculator import MaxLossCalculator
from .net_worth_calculator import NetWorthCalculator
from .opportunity_explorer import OpportunityExplorer, MINIMAL_SELL_PRICE_FOR_GENERAL_MARGIN_REDUCTION
from .positions_manager import PositionsManager, MINIMAL_SELL_PRICE_TO_CLOSE_POSITION
from .trading_bot import TradingBot
from .connection_manager import ConnectionManager
from .market_data_fetcher import MarketDataFetcher
from .target_delta_calculator import TargetDeltaCalculator
from .state_updater import StateUpdater, post_current_state


logger = logging.getLogger(__name__)


class OptionTrader:
    def __init__(self):
        # Accessing singleton instances
        self.connection_manager = ConnectionManager()
        self.ib = self.connection_manager.ib
        self.trading_bot = TradingBot()
        self.max_loss_calculator = MaxLossCalculator()
        self.net_worth_calculator = NetWorthCalculator()
        self.opportunity_explorer = OpportunityExplorer()
        self.positions_manager = PositionsManager()
        self.market_data_fetcher = MarketDataFetcher()
        self.target_delta_calculator = TargetDeltaCalculator()
        
        self.connection_failure_start_time = None
        self.config = {}

    async def run(self):
        logger.info("OptionTrader: Starting trading loop...")
        await post_current_state({'status': 'Loading...'})

        while True:
            try:
                from .option_safeguard import OptionSafeguard
                safeguard = OptionSafeguard()
                if time.time() - safeguard.last_run_end_time > SAFEGUARD_MAX_CADENCE:
                    await asyncio.sleep(0)
                    continue

                if not self.ib.isConnected():
                    logger.warning("OptionTrader: Task is waiting for IB connection...")
                    await asyncio.sleep(2)
                    continue

                write_heartbeat()

                if is_market_open():
                    logger.info(f"OptionTrader: Checking market status...")
                    await self.trade()
                else:
                    if int(time.time()) % 100 == 0:
                        logger.info(f"Market is closed")
                    await self.verify_no_open_trades()
                
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
        await asyncio.sleep(0.5)

    async def trade(self):
        await self.positions_manager.manage_current_positions()
        await self.guard_pending_trades()
        await self.opportunity_explorer.explore_opportunities()

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

            target_delta = target_deltas[option.right]
            if delta > target_delta:
                logger.info(
                    f"Cancelling sell of {get_option_name(open_sell_trade.contract)} since the delta ({delta:.2f}) is higher than the target delta ({target_delta:.3f})")

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
                logger.info(
                    f"Cancelling sell of {get_option_name(trade_to_cancel.contract)} because if all the trades get filled they will lead to exposure fee")
                self.trading_bot.cancel_trade(open_sell_trade)

        logger.info("Checking for excessive buy-limit trades")
        buy_limit_trades = [trade for trade in open_trades if
                            trade.order.orderType == 'LMT' and trade.order.action.upper() == 'BUY']
        for buy_limit_trade in buy_limit_trades:
            option = buy_limit_trade.contract
            corresponding_position = next((position for position in positions if option.conId == position.contract.conId), None)
            #option_ask_value = self.market_data_fetcher.get_ask(option)
            order_limit = buy_limit_trade.order.lmtPrice
            price_level = self.opportunity_explorer.last_call_option_price if option.right == 'C' else self.opportunity_explorer.last_put_option_price
            time_passed_since_submission = get_time_passed_since_submission(buy_limit_trade)

            if corresponding_position and order_limit == 0.05:
                position_quantity = abs(corresponding_position.position)
                if position_quantity != buy_limit_trade.remaining():
                    logger.info(f"Cancelling {get_option_name(option)} because the position has {position_quantity} units "
                                f"while there are {buy_limit_trade.remaining()} remaining units in the trade")
                    self.trading_bot.cancel_trade(buy_limit_trade)
                elif price_level < MINIMAL_SELL_PRICE_TO_CLOSE_POSITION and time_passed_since_submission > POSITION_BUYBACK_ORDER_EXPIRATION_TIME:
                    logger.info(f"Cancelling {get_option_name(option)} because it has the current price level for "
                                f"'{option.right}' is too low ({price_level}) for position buyback")
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


    async def verify_no_open_trades(self):
        open_trades = self.trading_bot.get_open_trades()
        for open_trade in open_trades:
            self.trading_bot.cancel_trade(open_trade)
