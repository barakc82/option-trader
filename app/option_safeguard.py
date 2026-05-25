import math
import asyncio
import time
from typing import Any

from ib_insync import Option, Trade

from utilities.utils import *
from .max_loss_calculator import MaxLossCalculator

from .trading_bot import TradingBot
from .positions_manager import PositionsManager
from .market_data_fetcher import MarketDataFetcher
from .connection_manager import ConnectionManager
from .spy_subscription_manager import SpySubscriptionManager

from utilities.ib_utils import is_hollow, req_id_to_comment, find_high_limit_buy_trade, get_spy_option_name

logger = logging.getLogger(__name__)

MAX_DEVIATION = 0.05

class OptionSafeguard:
    def __init__(self):
        # Accessing singleton instances
        self.connection_manager = ConnectionManager()
        self.ib = self.connection_manager.ib
        self.trading_bot = TradingBot()
        self.max_loss_calculator = MaxLossCalculator()
        self.market_data_fetcher = MarketDataFetcher()
        self.positions_manager = PositionsManager()
        self.spy_subscription_manager = SpySubscriptionManager()

        self.connection_failure_start_time = None
        self.last_alive_log_time = 0
        self.config = {}
        self.should_guard_positions = True
        self.enable_spy_option_hedging = False
        self.last_modification_times = {}

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
                    self.enable_spy_option_hedging = self.config.get("enable_spy_option_hedging", False)
        except Exception as e:
            logger.error(f"Error reading safeguard config: {e}")

    def is_unfair_ask_value(self, option, spy_option):
        ticker = option.ticker
        spx_ask = ticker.ask

        spy_ticker = self.ib.ticker(spy_option)
        spy_name = get_spy_option_name(spy_option)

        if not spy_ticker:
            logger.info(f"Matching ticker for {get_option_name(option)} ({spy_name}) not found in tickers cache. "
                        f"Unfairness is not detected")
            return False

        if math.isnan(spy_ticker.ask) or spy_ticker.ask <= 0:
            logger.info(f"Matching ticker for {get_option_name(option)} ({spy_name}) has an invalid ask value: "
                        f"{spy_ticker.ask}. Unfairness is not detected")
            return False

        adjusted_spy_ask = spy_ticker.ask * 10.0
        deviation = (spx_ask - adjusted_spy_ask) / adjusted_spy_ask

        if deviation < MAX_DEVIATION:
            return False

        logger.warning(
            f"Unfair ask for {get_option_name(option)} against {spy_name}: SPX Ask={spx_ask}, SPY Ask={spy_ticker.ask} (Adjusted={adjusted_spy_ask})")
        return True

    async def guard_current_positions(self):
        logger.debug("Checking current positions")
        positions, open_trades = await asyncio.gather(
            self.trading_bot.get_short_options(),
            self.trading_bot.get_open_trades()
        )
        
        if positions:
            await asyncio.gather(*(self.handle_current_risk(position, open_trades) for position in positions))

    async def _ensure_ticker(self, option) -> int:
        """Ensure the option has a valid, non-hollow ticker, fetching it if needed."""
        if getattr(option, 'ticker', None) is None:
            ticker = self.market_data_fetcher.get_ticker(option)
            if ticker is not None:
                logger.debug(f"Ticker for {get_option_name(option)} found in cache, attaching to contract")
            else:
                logger.debug(f"Ticker for {get_option_name(option)} not in cache, requesting live data")
            option.ticker = ticker

        if not is_hollow(option.ticker) and not math.isnan(option.ticker.ask):
            return SUCCESS

        logger.debug(f"Ticker for {get_option_name(option)} is hollow (no data), refreshing")

        ticker = await self.market_data_fetcher.request_ticker(option, is_snapshot=False)
        if ticker is None:
            logger.error(f"Failed to retrieve ticker for {get_option_name(option)}")
            return ERROR

        option.ticker = ticker

        # Check if ask is present and positive
        if math.isnan(option.ticker.ask) or option.ticker.ask <= 0:
            logger.error(
                f"Bad value of ask for option {get_option_name(option)}. Cannot determine whether ask value is fair")
            return ERROR

        return SUCCESS


    async def handle_current_risk(self, position, open_trades):
        if position.contract.conId in self.positions_manager.done_contract_ids:
            return

        option = position.contract
        ensure_ticker_result = await self._ensure_ticker(option)
        if ensure_ticker_result == ERROR:
            return

        spy_option = self.spy_subscription_manager.create_matching_spy_contract(option)
        current_price = self.calculate_current_price(option)

        stop_loss_per_option = await self.max_loss_calculator.calculate_max_loss(option.right)
        stop_loss = position.avgCost / 100 + stop_loss_per_option

        high_limit_buy_trade = find_high_limit_buy_trade(option, open_trades)
        if self.is_unfair_ask_value(option, spy_option):
            await self.handle_unfair_ask_value(high_limit_buy_trade, option, spy_option, stop_loss)
            return

        if not high_limit_buy_trade:
            if current_price > stop_loss:
                logger.info(f"Creating missing limit order for {get_option_name(option)}, limit: {stop_loss}")
                await self.trading_bot.close_short_option_position(position, limit=stop_loss)
            return

        if stop_loss * 0.5 <= current_price < stop_loss:
            logger.info(f"Watching the current price of {get_option_name(option)}: {current_price:.2f}, stop loss is at {stop_loss:.2f}")
            return

        await self.handle_high_limit_buy_trade(high_limit_buy_trade, position, stop_loss_per_option)

    def calculate_current_price(self, option) -> Any:
        current_price = (option.ticker.bid + option.ticker.ask) / 2
        if math.isnan(option.ticker.bid):
            current_price = option.ticker.last
        return current_price

    async def handle_high_limit_buy_trade(self, high_limit_buy_trade: Trade, position,
                                          stop_loss_per_option: float):
        assert high_limit_buy_trade

        option = position.contract
        last_mod_time = self.last_modification_times.get(high_limit_buy_trade.order.orderId, 0)
        if time.time() - last_mod_time < 2:
            logger.info(
                f"Skipping modification for {get_option_name(option)} as it was modified less than 2 seconds ago")
            return

        current_price = self.calculate_current_price(option)
        stop_loss = position.avgCost / 100 + stop_loss_per_option
        current_limit_price = high_limit_buy_trade.order.lmtPrice
        logger.warning(
            f"Risky position detected: {get_option_name(option)}, current price is {current_price}, trying to close it using limit of {current_limit_price}")

        red_line_stop_loss = stop_loss + stop_loss_per_option * 0.5
        required_limit_price = min(option.ticker.ask + 0.1, red_line_stop_loss)
        logger.info(f"The required limit price for {get_option_name(option)} is {required_limit_price:.2f}, "
                    f"red line stop loss is {red_line_stop_loss:.2f}, maximal additional increment is {stop_loss_per_option:.2f}")

        logger.warning(
            f"Trying to close risky position {get_option_name(option)} at limit of {required_limit_price:.2f}, replacing current limit of {current_limit_price}")

        self.last_modification_times[high_limit_buy_trade.order.orderId] = time.time()
        req_id_to_comment[high_limit_buy_trade.order.orderId] = f"Limit order: {current_limit_price}"

        await self.trading_bot.modify_limit_order(high_limit_buy_trade, required_limit_price)

    async def handle_unfair_ask_value(self, high_limit_buy_trade: Any | None, option, spy_option: Option,
                                      stop_loss: Any):
        logger.warning(
            f"Ask value of {get_option_name(option)} is unfair (Ask: {option.ticker.ask}), "
            f"will not close position")

        if high_limit_buy_trade:
            logger.warning(
                f"Cancelling buy order for {get_option_name(option)} since the ask value is unfair")
            self.trading_bot.cancel_order(high_limit_buy_trade.order)

        spy_ticker = self.ib.ticker(spy_option)
        if spy_ticker:
            spy_current_price = (spy_ticker.bid + spy_ticker.ask) / 2
            if math.isnan(spy_ticker.bid):
                spy_current_price = spy_ticker.last

            if not math.isnan(spy_current_price):
                spy_current_adjusted_price = spy_current_price * 10
                if spy_current_adjusted_price > stop_loss:
                    logger.warning(f"Should consider buying {get_spy_option_name(spy_option)}, since the ask value of "
                                   f"{get_option_name(option)} is unfair, and the fair price is above the stop loss")
