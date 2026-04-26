import asyncio

from ib_insync import IB

from utilities.utils import *

from .trading_bot import TradingBot
from .positions_manager import PositionsManager
from .market_data_fetcher import MarketDataFetcher

logger = logging.getLogger(__name__)


async def find_stop_loss_trade(position, open_trades):
    option = position.contract
    for open_trade in open_trades:
        if (option.conId == open_trade.contract.conId and open_trade.order.orderType == 'STP'
                and open_trade.remaining() == abs(position.position)):
            return open_trade
    return None


async def get_pending_buy(position, open_trades):
    open_buy_trades = [trade for trade in open_trades if trade.order.action.upper() == 'BUY' and
                       not is_trade_cancelled(trade) and trade.order.orderType == 'LMT']
    for open_buy_trade in open_buy_trades:
        if open_buy_trade.contract.conId == position.contract.conId:
            return open_buy_trade
    return None


class OptionSafeguard:
    def __init__(self, ib: IB, trading_bot: TradingBot, positions_manager: PositionsManager, market_data_fetcher: MarketDataFetcher):
        self.ib = ib
        self.trading_bot = trading_bot
        self.positions_manager = positions_manager
        self.market_data_fetcher = market_data_fetcher
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
                    await asyncio.sleep(30)
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

                sleep_time = 180 if is_regular_hours_with_after_hours() or not is_market_open() else 1
                await asyncio.sleep(sleep_time)

            except Exception:
                if self.connection_failure_start_time is None:
                    self.connection_failure_start_time = time.time()
                
                elapsed = time.time() - self.connection_failure_start_time
                if elapsed > 300:
                    logger.critical(f"OptionSafeguard: Persistent failure for {elapsed:.0f}s. Exiting.")
                    sys.exit(1)
                
                logger.exception(f"OptionSafeguard: Safeguard error ({elapsed:.0f}s):")
                await asyncio.sleep(10)

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
        positions = await self.trading_bot.get_short_options(should_use_cache=True)
        
        # Optimization: Fetch open trades ONCE for all positions
        open_trades = await self.trading_bot.get_open_trades()
        
        if positions:
            await asyncio.gather(*(self.handle_current_risk(position, open_trades) for position in positions))

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

        if datetime.now().astimezone() - option.ticker.time > timedelta(seconds=4):
            logger.debug(f"The ticker of {get_option_name(option)} is invalid, updating it")
            ticker = await self.market_data_fetcher.req_mkt_data(option, is_snapshot=False)
            option.ticker = ticker

        last_price = option.ticker.last
        
        # Optimization: Use the passed-in open_trades list
        stop_loss_trade = await find_stop_loss_trade(position, open_trades)
        if not stop_loss_trade:
            logger.warning(f"No stop loss is set for position of {get_option_name(option)}")
            await self.ib.reqAllOpenOrdersAsync()
            return

        stop_loss = stop_loss_trade.order.auxPrice
        sell_price = position.avgCost / 100
        logger.debug(f"{get_option_name(option)}, Last price: {last_price:.2f}, Sell price: {sell_price:.2f}, Stop loss for option: {stop_loss:.2f}")

        if last_price >= 0.5 * stop_loss:
            logger.info(f"Watching the current price of {get_option_name(option)}: {last_price:.2f}, stop loss is at {stop_loss:.2f}")

        if last_price >= stop_loss:
            logger.warning(f"The current price of {get_option_name(option)} ({last_price}) is higher than the stop loss: {stop_loss:}")
            if self.positions_manager.is_recent_buy_filled(position):
                logger.info(f"Recent buy already filled, so not closing {get_option_name(option)}")
                await self.ib.reqPositionsAsync()
                return

            pending_buy_trade = await get_pending_buy(position, open_trades)
            if pending_buy_trade and hasattr(pending_buy_trade, 'submission_time'):
                if time.time() - pending_buy_trade.submission_time < 10:
                    logger.info(f"Recent buy already pending, so not trying to close {get_option_name(option)} yet")
                    await self.ib.reqPositionsAsync()
                    return

                logger.info(f"Cancelling the buy of {get_option_name(option)} since it has been pending for too long")
                trade = self.trading_bot.cancel_trade(pending_buy_trade)
                if not is_trade_cancelled(trade):
                    logger.info(f"{get_option_name(option)} has not been cancelled yet")
                    return

                logger.info(f"{get_option_name(option)} is cancelled, continuing to a new close of the position")

            if is_regular_hours():
                is_stop_loss_exists = await find_stop_loss_trade(position, open_trades)
                if is_stop_loss_exists:
                    logger.info(f"Stop loss exists for {get_option_name(option)}, so not closing")
                    return

            logger.warning(f"Risky position {get_option_name(option)} during pre-market, current price is {last_price} and the stop loss is {stop_loss}")
            
            if self.should_guard_positions:
                logger.warning(f"Closing risky position {get_option_name(option)} during pre-market")
                pending_buy_trade = await self.trading_bot.close_short_option_position(position)
                pending_buy_trade.submission_time = time.time()
