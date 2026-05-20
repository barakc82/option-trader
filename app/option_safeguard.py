import asyncio
import math
from datetime import timedelta

from utilities.utils import *
from .max_loss_calculator import calculate_max_loss

from .trading_bot import TradingBot
from .positions_manager import PositionsManager
from .market_data_fetcher import MarketDataFetcher
from .connection_manager import ConnectionManager

from utilities.ib_utils import is_hollow, req_id_to_comment, find_high_limit_buy_trade, get_time_passed_since_submission

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

                if not self.should_guard_positions:
                    await asyncio.sleep(1)
                    continue

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

                await asyncio.sleep(0 if is_market_open() else 0.1)

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
            await asyncio.gather(*(self.handle_current_risk(position, open_trades) for position in positions))

    def find_stop_loss_trade(self, position, open_trades):
        option = position.contract
        for open_trade in open_trades:
            if (option.conId == open_trade.contract.conId and open_trade.order.orderType == 'STP LMT'
                    and open_trade.remaining() == abs(position.position)):
                return open_trade
        return None

    async def handle_current_risk(self, position, open_trades):
        if position.contract.conId in self.positions_manager.done_contract_ids:
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
        high_limit_buy_trade = find_high_limit_buy_trade(option, open_trades)
        if not stop_loss_trade and not high_limit_buy_trade:
            logger.error(f"No stop loss nor buy trade is set for position of {get_option_name(option)}")
            stop_loss_per_option = await calculate_max_loss(option.right, should_consider_only_effective=True)
            limit = position.avgCost / 100 + stop_loss_per_option
            if limit < last_price:
                logger.info(f"Creating missing limit order for {get_option_name(option)}, limit: {limit}")
                await self.trading_bot.create_limit_order(position, limit)
            return

        if stop_loss_trade:
            stop_loss = stop_loss_trade.order.auxPrice
            if last_price >= stop_loss * 0.5:
                logger.info(f"Watching the current price of {get_option_name(option)}: {last_price:.2f}, stop loss is at {stop_loss:.2f}")
            return

        assert high_limit_buy_trade
        current_limit_price = high_limit_buy_trade.order.lmtPrice
        logger.warning(
            f"Risky position detected: {get_option_name(option)}, current price is {last_price}, trying to close it using limit of {current_limit_price}")

        time_passed_since_submission = get_time_passed_since_submission(high_limit_buy_trade)
        stop_loss_per_option = await calculate_max_loss(option.right, should_consider_only_effective=True)
        initial_stop_loss_price = position.avgCost / 100 + stop_loss_per_option

        total_increment_period = await self.calculate_total_increment_period(option, last_price)
        required_limit_price = initial_stop_loss_price + stop_loss_per_option * time_passed_since_submission / total_increment_period
        logger.info(f"The required limit price for {get_option_name(option)} is {required_limit_price:.2f}, "
                    f"total increment period is {total_increment_period}, time passed since submission is {time_passed_since_submission}, "
                    f"initial stop loss is {initial_stop_loss_price:.2f}, maximal additional increment is {stop_loss_per_option:.2f}")

        if abs(required_limit_price - current_limit_price) < 0.025:
            logger.info(f"The required limit price ({required_limit_price}) is close to the current limit price ({current_limit_price}), leaving it as is")
            return

        logger.warning(f"Trying to close risky position {get_option_name(option)} at limit of {required_limit_price:.2f}, replacing current limit of {current_limit_price}")
        req_id_to_comment[high_limit_buy_trade.order.orderId] = f"Limit order: {current_limit_price}"

        await self.trading_bot.modify_limit_order(high_limit_buy_trade, required_limit_price)

    async def calculate_total_increment_period(self, option, option_price) -> timedelta:
        strike_suffix = option.strike % 100
        if strike_suffix in [0, 25, 50, 75]:
            return timedelta(minutes=10)

        open_trades = self.trading_bot.get_cache_open_trades()
        higher_strike_trades = [trade for trade in open_trades if trade.contract.strike > option.strike]
        lower_strike_trades = [trade for trade in open_trades if trade.contract.strike < option.strike]
        riskier_trades = lower_strike_trades if option.right == 'C' else higher_strike_trades

        if option.strike % 100 in [10, 20, 30, 40, 60, 70, 80, 90]:
            for riskier_trade in riskier_trades:
                riskier_option = riskier_trade.contract
                if riskier_option.strike in [5, 15, 35, 45, 55, 65, 85, 95]:
                    continue
                ticker = self.ib.ticker(riskier_option)
                if ticker is not None and ticker.last < option_price:
                    return timedelta(minutes=20)

            return timedelta(minutes=15)

        for riskier_trade in riskier_trades:
            riskier_option = riskier_trade.contract
            ticker = self.ib.ticker(riskier_option)
            if ticker is not None and ticker.last < option_price:
                return timedelta(minutes=25)

        return timedelta(minutes=20)
