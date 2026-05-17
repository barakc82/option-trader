import asyncio
import math

from utilities.utils import *

from .trading_bot import TradingBot
from .positions_manager import PositionsManager
from .market_data_fetcher import MarketDataFetcher
from .connection_manager import ConnectionManager
from .max_loss_calculator import calculate_max_loss

from utilities.ib_utils import is_hollow, req_id_to_comment

logger = logging.getLogger(__name__)

class OptionSafeguard:
    def __init__(self):
        # Accessing singleton instances
        self.connection_manager = ConnectionManager()
        self.ib = self.connection_manager.ib
        self.trading_bot = TradingBot()
        self.market_data_fetcher = MarketDataFetcher()
        self.positions_manager = PositionsManager()
        
        self.connection_failure_start_time = None
        self.last_alive_log_time = 0
        self.config = {}
        self.should_guard_positions = True

    async def run(self):
        logger.info("OptionSafeguard: Starting safeguard loop...")
        while True:
            try:
                self.load_config()

                if not self.ib.isConnected():
                    logger.warning("OptionSafeguard: Task is waiting for IB connection...")
                    await asyncio.sleep(2)
                    continue

                if time.time() - self.last_alive_log_time > 300:
                    logger.info("Option safeguard is still running")
                    self.last_alive_log_time = time.time()

                logger.debug("OptionSafeguard: Monitoring position risk...")
                if is_market_open():
                    await self.guard_current_positions()
                else:
                    logger.debug(f"Market is closed")
                
                if self.connection_failure_start_time is not None:
                    logger.info("OptionSafeguard: Connection error resolved.")
                    self.connection_failure_start_time = None

                sleep_time = 180 if is_regular_hours_with_after_hours() or not is_market_open() else 0
                await asyncio.sleep(sleep_time)

            except Exception:
                if self.connection_failure_start_time is None:
                    self.connection_failure_start_time = time.time()
                
                elapsed = time.time() - self.connection_failure_start_time
                logger.exception(f"OptionSafeguard: Safeguard error ({elapsed:.0f}s):")

                if elapsed > 300:
                    logger.error("OptionSafeguard: Persistent failure detected. Continuing to retry indefinitely...")

                # Progressive backoff for sleep
                sleep_time = min(10 + (elapsed // 60) * 10, 60)
                await asyncio.sleep(sleep_time)

    def load_config(self):
        config_path = "config/option_trader_config.json"
        try:
            if os.path.exists(config_path):
                with open(config_path, "r") as f:
                    self.config = json.load(f)
                    self.should_guard_positions = self.config.get("should_guard_positions", True)
        except Exception as e:
            logger.error(f"Error reading safeguard config: {e}")

    async def guard_current_positions(self):
        recent_trades = self.positions_manager.get_recent_trades()
        for recent_trade in recent_trades:
            logger.info(f"Recent filled trade: {recent_trade.option_name}, contract id {recent_trade.conId}, order type: {recent_trade.action}")

        logger.debug("Checking current positions")
        positions, open_trades = await asyncio.gather(
            self.trading_bot.get_short_options(),
            self.trading_bot.get_open_trades()
        )
        
        if positions:
            await asyncio.gather(*(self.handle_current_risk(position, open_trades) for position in positions))

    def find_stop_loss_trade(self, position, open_trades):
        option = position.contract
        for open_trade in open_trades:
            if (option.conId == open_trade.contract.conId and open_trade.order.orderType == 'STP LMT'
                    and open_trade.remaining() == abs(position.position)):
                return open_trade
        return None

    def get_pending_buy(self, position, open_trades):
        open_buy_trades = [trade for trade in open_trades if trade.order.action.upper() == 'BUY' and
                           not is_trade_cancelled(trade) and trade.order.orderType == 'LMT']
        for open_buy_trade in open_buy_trades:
            if open_buy_trade.contract.conId == position.contract.conId:
                return open_buy_trade
        return None

    async def handle_current_risk(self, position, open_trades):
        option = position.contract
        if not hasattr(option, 'ticker') or option.ticker is None:
            ticker = self.market_data_fetcher.get_ticker(option)
            if ticker is None:
                logger.error(f"The ticker of {get_option_name(option)} is missing")
                ticker = await self.market_data_fetcher.req_mkt_data(option, is_snapshot=False)
                option.ticker = ticker
            else:
                logger.debug(f"The ticker of {get_option_name(option)} was found in search, attaching it to the contract")
                option.ticker = ticker
            return

        if is_hollow(option.ticker):
            logger.debug(f"The ticker of {get_option_name(option)} is hollow (no data), updating it")
            ticker = await self.market_data_fetcher.req_mkt_data(option, is_snapshot=False)
            option.ticker = ticker

        last_price = option.ticker.last
        
        stop_loss_trade = self.find_stop_loss_trade(position, open_trades)
        limit_buy_trade = self.get_pending_buy(position, open_trades)
        
        # Determine risk threshold and limit price
        if stop_loss_trade:
            stop_price = stop_loss_trade.order.auxPrice
            limit_price = stop_loss_trade.order.lmtPrice
        elif limit_buy_trade:
            # Triggered stop-limit becomes a regular limit order
            stop_price = limit_buy_trade.order.lmtPrice
            limit_price = limit_buy_trade.order.lmtPrice
        else:
            # Fallback: calculate what the stop should be if order is missing
            max_loss = await calculate_max_loss(option.right, should_consider_only_effective=True)
            stop_price = position.avgCost / 100 + max_loss
            limit_price = stop_price + 0.05
            logger.warning(f"No protection order found for {get_option_name(option)}. Using calculated stop: {stop_price:.2f}")

        if last_price >= 0.5 * stop_price:
            logger.info(f"Watching {get_option_name(option)}: {last_price:.2f}, stop: {stop_price:.2f}")

        if last_price >= stop_price:
            logger.warning(f"Risk threshold reached for {get_option_name(option)} ({last_price:.2f} >= {stop_price:.2f})")
            
            if self.positions_manager.is_recent_buy_filled(position):
                logger.info(f"Recent buy already filled, so not closing {get_option_name(option)}")
                return

            if limit_buy_trade and hasattr(limit_buy_trade, 'submission_time'):
                if time.time() - limit_buy_trade.submission_time < 10:
                    logger.info(f"Recent buy already pending, so not trying to close {get_option_name(option)} yet")
                    return

                if last_price > limit_price:
                    logger.warning(f"Price {last_price} jumped over triggered limit {limit_price}, cancelling and market buying")
                    self.trading_bot.cancel_trade(limit_buy_trade)
                    # Proceed to market order
                else:
                    logger.info(f"Waiting for pending limit buy to fill for {get_option_name(option)}")
                    return

            if stop_loss_trade:
                if last_price > limit_price:
                    logger.warning(f"Price {last_price} jumped over stop-limit limit {limit_price}, cancelling and market buying")
                    self.trading_bot.cancel_trade(stop_loss_trade)
                    # Proceed to market order
                elif is_regular_hours():
                    logger.info(f"Stop-limit exists for {get_option_name(option)} and price is within limit, so not closing manually")
                    return

            if self.should_guard_positions:
                logger.warning(f"Closing risky position {get_option_name(option)} via manual market order")
                pending_buy_trade = await self.trading_bot.close_short_option_position(position)
                req_id_to_comment[pending_buy_trade.order.orderId] = "Risk reduction"
                pending_buy_trade.submission_time = time.time()
