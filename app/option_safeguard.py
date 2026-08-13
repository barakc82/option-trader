import math
import asyncio
import csv
from datetime import datetime
from typing import Any

from ib_insync import Option, Trade, FuturesOption

from utilities.utils import *
from .index_price_manager import IndexPriceManager
from .max_loss_calculator import MaxLossCalculator

from .trading_bot import TradingBot
from .positions_manager import PositionsManager
from .market_data_fetcher import MarketDataFetcher
from .connection_manager import ConnectionManager
from .subscription_manager import SubscriptionManager
from .fairness_checker import FairnessChecker

from utilities.ib_utils import *

logger = logging.getLogger(__name__)


class OptionSafeguard:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(OptionSafeguard, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if not self._initialized:
            # Accessing singleton instances
            self.connection_manager = ConnectionManager()
            self.ib = self.connection_manager.ib
            self.trading_bot = TradingBot()
            self.max_loss_calculator = MaxLossCalculator()
            self.index_price_manager = IndexPriceManager()
            self.market_data_fetcher = MarketDataFetcher()
            self.positions_manager = PositionsManager()
            self.subscription_manager = SubscriptionManager()
            self.fairness_checker = FairnessChecker()

            self.connection_failure_start_time = None
            self.last_alive_log_time = 0
            self.config = {}
            self.should_guard_positions = True
            self.last_modification_times = {}
            self.last_run_end_time = 0
            self.last_skipping_log_times = {}
            self.last_watching_log_times = {}
            self._initialized = True

    async def run(self):
        logger.info("OptionSafeguard: Starting safeguard loop...")
        while True:
            try:
                iteration_start_time = time.time()
                
                # Check 1: Delay between iterations
                if self.last_run_end_time > 0:
                    delay_between_iterations = iteration_start_time - self.last_run_end_time
                    if delay_between_iterations > SAFEGUARD_MAX_CADENCE:
                         logger.warning(f"OptionSafeguard delay between iterations took too long: {delay_between_iterations:.2f}s (target <= {SAFEGUARD_MAX_CADENCE}s)")

                self.load_config()

                if not self.should_guard_positions:
                    self.last_run_end_time = 0
                    await asyncio.sleep(1)
                    continue

                if not self.ib.isConnected():
                    logger.warning("OptionSafeguard: Task is waiting for IB connection...")
                    self.last_run_end_time = 0
                    await asyncio.sleep(2)
                    continue

                if iteration_start_time - self.last_alive_log_time > 300:
                    logger.info("Option safeguard is still running")
                    self.last_alive_log_time = iteration_start_time

                logger.debug("OptionSafeguard: Monitoring position risk...")
                if is_market_open():
                    await self.guard_current_positions()
                else:
                    logger.debug(f"Market is closed")
                
                if self.connection_failure_start_time is not None:
                    logger.info("OptionSafeguard: Connection error resolved.")
                    self.connection_failure_start_time = None

                # Check 2: Duration of iteration
                self.last_run_end_time = time.time()
                iteration_duration = self.last_run_end_time - iteration_start_time
                if iteration_duration > SAFEGUARD_MAX_CADENCE:
                    logger.warning(f"OptionSafeguard iteration duration took too long: {iteration_duration:.2f}s (target <= {SAFEGUARD_MAX_CADENCE}s)")

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
        config_path = OPTION_TRADER_CONFIG_PATH
        try:
            if os.path.exists(config_path):
                with open(config_path, "r") as f:
                    self.config = json.load(f)
                    self.should_guard_positions = self.config.get("should_guard_positions", True)
                    self.enable_es_option_hedging = self.config.get("enable_es_option_hedging", False)
        except Exception as e:
            logger.error(f"OptionSafeguard: Error reading config: {e}")

        self.fairness_checker.load_config()


    async def guard_current_positions(self):
        logger.debug("Checking current positions")
        positions = self.trading_bot.get_short_options()
        open_trades = self.trading_bot.get_open_trades()

        if positions:
            results = await asyncio.gather(
                *(self.handle_current_risk(position, open_trades) for position in positions),
                return_exceptions=True,
            )
            for position, result in zip(positions, results):
                if isinstance(result, Exception):
                    logger.exception(
                        f"OptionSafeguard: Error handling risk for {get_option_name(position.contract)}:",
                        exc_info=result,
                    )

    async def _ensure_ticker(self, option) -> int:
        """Ensure the option has a valid, non-hollow ticker, fetching it if needed."""
        if getattr(option, 'ticker', None) is None:
            if int(time.time()) % 1000 == 0:
                logger.error(f"Failed to retrieve ticker for {get_option_name(option)}")
            return ERROR

        if is_hollow(option.ticker):
            if int(time.time()) % 1000 == 0:
                logger.error(f"Ticker for {get_option_name(option)} is hollow (no data)")
            return ERROR

        if math.isnan(option.ticker.ask) or option.ticker.ask <= 0:
            if int(time.time()) % 1000 == 0:
                logger.error(
                    f"Bad value of ask for option {get_option_name(option)}. Cannot determine whether ask value is fair")
            return ERROR

        return SUCCESS


    def _log_close_event(self, option, stop_loss, stop_loss_per_option):
        spot_price = self.index_price_manager.get_spot_price()
        risk_free_rate = self.market_data_fetcher.get_cached_risk_free_rate()
        distance_to_stop = calculate_distance_to_stop(option, option.ticker, stop_loss, spot_price, risk_free_rate)
        td_entries = self.positions_manager.position_initial_state_map.get(option.conId) or [None]
        csv_path = 'cache/in_the_money_events.csv'
        write_header = not os.path.exists(csv_path)
        with open(csv_path, 'a', newline='') as f:
            writer = csv.writer(f)
            if write_header:
                writer.writerow(['datetime', 'target_delta', 'stop_loss', 'stop_loss_per_option', 'distance_to_stop'])
            for td_entry in td_entries:
                target_delta = td_entry.target_delta if td_entry else ''
                writer.writerow([datetime.now().isoformat(), target_delta, stop_loss, stop_loss_per_option, distance_to_stop])

    async def handle_current_risk(self, position, open_trades):
        if position.contract.conId in self.positions_manager.done_contract_ids:
            return

        option = position.contract
        ensure_ticker_result = await self._ensure_ticker(option)
        if ensure_ticker_result == ERROR:
            return

        current_price = option.ticker.marketPrice()
        stop_loss_per_option = self.max_loss_calculator.calculate_max_loss(option.right)
        stop_loss = position.avgCost / 100 + stop_loss_per_option
        high_limit_buy_trade = find_high_limit_buy_trade(option, open_trades)

        es_options = self.subscription_manager.spx_to_es_map.get(option.conId)
        if self.fairness_checker.is_unfair_ask_value(option, es_options):
            self.handle_unfair_ask_value(high_limit_buy_trade, option, es_options, stop_loss)
            return

        if stop_loss * 0.5 <= current_price < stop_loss:
            now = time.time()
            if now - self.last_watching_log_times.get(option.conId, 0) > 0.1:
                spot_price = self.index_price_manager.get_spot_price()
                risk_free_rate = self.market_data_fetcher.get_cached_risk_free_rate()
                distance_to_stop = calculate_distance_to_stop(option, option.ticker, stop_loss, spot_price, risk_free_rate)
                logger.info(f"Watching the current price of {get_option_name(option)}: {current_price:.2f}, stop loss is at {stop_loss:.2f}, distance to stop is {distance_to_stop:.1f}")
                self.last_watching_log_times[option.conId] = now

        if current_price < stop_loss or math.isnan(current_price):
            return

        if not high_limit_buy_trade:
            if current_price > stop_loss:
                logger.info(f"Creating missing limit order for {get_option_name(option)}, limit: {stop_loss:.2f}, current price: {current_price:.2f}")
                self.last_skipping_log_times[option.conId] = time.time()
                await self.trading_bot.close_short_option_position(position, limit=stop_loss)
                self._log_close_event(option, stop_loss, stop_loss_per_option)
            return

        await self.handle_high_limit_buy_trade(high_limit_buy_trade, position, stop_loss_per_option)

    async def handle_high_limit_buy_trade(self, high_limit_buy_trade: Trade, position,
                                          stop_loss_per_option: float):
        assert high_limit_buy_trade

        option = position.contract
        now = time.time()
        last_mod_time = self.last_modification_times.get(high_limit_buy_trade.order.orderId, 0)
        time_interval_between_modifications = 60
        if now - last_mod_time < time_interval_between_modifications:
            if now - self.last_skipping_log_times.get(option.conId, 0) > 1:
                logger.info(
                    f"Skipping modifying the limit for {get_option_name(option)} as it was modified less than "
                    f"{time_interval_between_modifications} seconds ago, limit is {high_limit_buy_trade.order.lmtPrice}")
                self.last_skipping_log_times[option.conId] = now
            return

        current_price = option.ticker.marketPrice()
        stop_loss = position.avgCost / 100 + stop_loss_per_option
        current_limit_price = high_limit_buy_trade.order.lmtPrice
        logger.warning(
            f"Risky position detected: {get_option_name(option)}, current price is {current_price}, trying to close it using limit of {current_limit_price}")

        red_line_stop_loss = stop_loss + stop_loss_per_option * 0.5
        required_limit_price = min(current_limit_price * 1.1, red_line_stop_loss)
        logger.info(f"The required limit price for {get_option_name(option)} is {required_limit_price:.2f}, "
                    f"red line stop loss is {red_line_stop_loss:.2f}, maximal additional increment is {stop_loss_per_option:.2f}")

        logger.warning(
            f"Trying to close risky position {get_option_name(option)} at limit of {required_limit_price:.2f}, replacing current limit of {current_limit_price}")

        self.last_modification_times[high_limit_buy_trade.order.orderId] = time.time()
        req_id_to_comment[high_limit_buy_trade.order.orderId] = f"Limit order: {current_limit_price}"

        await self.trading_bot.modify_limit_order(high_limit_buy_trade, required_limit_price)


    def handle_unfair_ask_value(self, high_limit_buy_trade: Any | None, option, es_options: list,
                                        stop_loss: Any):
        lower_es, upper_es = es_options
        now = time.time()
        if now - self.fairness_checker.last_unfair_ask_warning_times.get(option.conId, 0) >= 10:
            logger.warning(
                f"Ask value of {get_option_name(option)} ({option.ticker.ask}) is unfair against interpolated ES ask "
                f"(ES {lower_es.strike}/{upper_es.strike}), will not close position")
            self.fairness_checker.last_unfair_ask_warning_times[option.conId] = now

        if high_limit_buy_trade:
            logger.warning(
                f"Cancelling buy order for {get_option_name(option)} since the ask value is unfair against ES")
            self.trading_bot.cancel_order(high_limit_buy_trade.order)

        for es_option in es_options:
            es_ticker = self.ib.ticker(es_option)
            if es_ticker:
                es_current_price = es_ticker.marketPrice()
                if not math.isnan(es_current_price) and es_current_price > stop_loss:
                    logger.warning(f"Should consider buying ES hedge {es_option.strike}, since the ask value of "
                                   f"{get_option_name(option)} is unfair, and the fair price is above the stop loss")
