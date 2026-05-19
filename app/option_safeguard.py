import asyncio
import math

from utilities.utils import *

from .trading_bot import TradingBot
from .positions_manager import PositionsManager
from .market_data_fetcher import MarketDataFetcher
from .connection_manager import ConnectionManager

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
        self.done_con_ids = set()
        
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

                await asyncio.sleep(0)

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
        logger.debug("Checking current positions")
        positions, open_trades = await asyncio.gather(
            self.trading_bot.get_short_options(),
            self.trading_bot.get_open_trades()
        )
        
        if positions:
            current_con_ids = {p.contract.conId for p in positions}
            self.done_con_ids &= current_con_ids
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
        if position.contract.conId in self.done_con_ids:
            return

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
        if not stop_loss_trade:
            logger.warning(f"No stop loss is set for position of {get_option_name(option)}")
            return

        stop_loss = stop_loss_trade.order.auxPrice
        stop_loss_limit = stop_loss_trade.order.lmtPrice
        sell_price = position.avgCost / 100
        logger.debug(f"{get_option_name(option)}, Last price: {last_price:.2f}, Sell price: {sell_price:.2f}, Stop loss for option: {stop_loss:.2f}")

        if last_price * 0.5 <= stop_loss < stop_loss_limit:
            logger.info(f"Watching the current price of {get_option_name(option)}: {last_price:.2f}, stop loss is at {stop_loss:.2f}")

        if last_price >= stop_loss_limit:
            logger.warning(f"The current price of {get_option_name(option)} ({last_price}) is higher than the stop loss limit: {stop_loss_limit:.2f}")
            
            pending_buy_trade = self.get_pending_buy(position, open_trades)
            if pending_buy_trade and pending_buy_trade.order.lmtPrice > 0.05:
                return

            logger.warning(f"Risky position detected: {get_option_name(option)}, current price is {last_price} and the stop loss is {stop_loss_limit}")
            
            if self.should_guard_positions:
                logger.warning(f"Cancelling the current stop-limit order to replace it with a limit order")
                stop_loss_trade = self.trading_bot.cancel_trade(stop_loss_trade)
                if is_trade_cancelled(stop_loss_trade) or stop_loss_trade.orderStatus.status == 'Filled':
                    logger.info(f"Status of Stop loss trade for {get_option_name(option)}: {stop_loss_trade.orderStatus.status} "
                                f"- no need to send a limit order")
                    return

                self.done_con_ids.add(option.conId)
                limit = stop_loss * 2
                if option.strike % 100 in [5, 15, 35, 45, 55, 65, 85, 95]:
                    limit = stop_loss * 1.5
                if option.strike % 100 in [10, 20, 30, 40, 60, 70, 80, 90]:
                    limit = stop_loss * 1.75
                logger.warning(f"Closing risky position {get_option_name(option)} at limit of {limit:.2f}")
                pending_buy_trade = await self.trading_bot.close_short_option_position(position, limit)
                req_id_to_comment[pending_buy_trade.order.orderId] = "Risk reduction"
