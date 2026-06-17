import math
import asyncio
from typing import Any

from ib_insync import Option, Trade, FuturesOption

from utilities.utils import *
from .max_loss_calculator import MaxLossCalculator

from .trading_bot import TradingBot
from .positions_manager import PositionsManager
from .market_data_fetcher import MarketDataFetcher
from .connection_manager import ConnectionManager
from .subscription_manager import SubscriptionManager

from utilities.ib_utils import *

logger = logging.getLogger(__name__)

MAX_DEVIATION = 0.15
MIN_PRICE_THRESHOLD = 1


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
            self.market_data_fetcher = MarketDataFetcher()
            self.positions_manager = PositionsManager()
            self.subscription_manager = SubscriptionManager()

            self.connection_failure_start_time = None
            self.last_alive_log_time = 0
            self.config = {}
            self.should_guard_positions = True
            self.enable_spy_option_hedging = False
            self.last_modification_times = {}
            self.last_run_end_time = 0
            self.last_unfair_ask_warning_time = 0
            self.alternative_valuation = "SPY"
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
        config_path = "config/option_trader_config.json"
        try:
            if os.path.exists(config_path):
                with open(config_path, "r") as f:
                    self.config = json.load(f)
                    self.should_guard_positions = self.config.get("should_guard_positions", True)
                    self.enable_spy_option_hedging = self.config.get("enable_spy_option_hedging", False)
                    self.alternative_valuation = self.config.get("alternative_valuation", "SPY")
        except Exception as e:
            logger.error(f"OptionSafeguard: Error reading config: {e}")

    def is_unfair_ask_value(self, option, spy_options):
        spx_ask = option.ticker.ask
        if spx_ask < MIN_PRICE_THRESHOLD:
            return False

        # spy_options is a list of [lower_strike_spy, upper_strike_spy] (or both same if exact match)
        spy_tickers = [self.market_data_fetcher.get_ticker(s) for s in spy_options]
        spy_names = [get_spy_option_name(s) for s in spy_options]

        for i in range(2):
            if not self._validate_alt_ticker(option, spy_options[i], spy_tickers[i], spy_names[i], "SPY"):
                return False
            if not self._validate_greeks(spy_tickers[i], spy_names[i]):
                return False

        # Calculate weighted average for ask and greeks
        target_spy_strike = option.strike / 10.0
        s1, s2 = spy_options[0].strike, spy_options[1].strike
        
        if s1 == s2:
            weight2 = 0.5 # Doesn't matter, they are the same
            weight1 = 0.5
        else:
            # Linear interpolation weights: weight2 = (target - s1) / (s2 - s1)
            weight2 = (target_spy_strike - s1) / (s2 - s1)
            weight1 = 1.0 - weight2

        interpolated_spy_ask = spy_tickers[0].ask * weight1 + spy_tickers[1].ask * weight2
        
        # Interpolate Greeks
        g1 = spy_tickers[0].askGreeks or spy_tickers[0].modelGreeks
        g2 = spy_tickers[1].askGreeks or spy_tickers[1].modelGreeks
        
        interpolated_delta = g1.delta * weight1 + g2.delta * weight2
        interpolated_gamma = g1.gamma * weight1 + g2.gamma * weight2

        adjusted_spy_ask, indices_difference = self._calculate_adjusted_spy_ask(interpolated_spy_ask, interpolated_delta, interpolated_gamma)

        if not self._validate_indices_difference(indices_difference):
            return False

        deviation = (spx_ask - adjusted_spy_ask) / adjusted_spy_ask

        if int(time.time() * 10) % 1000 == 0:
            logger.info(f"Checking option {get_option_name(option)} for unfair ask using {spy_names}. "
                        f"SPX ask is {spx_ask} and the adjusted SPY ask is {adjusted_spy_ask:.2f}, "
                        f"the deviation is {deviation:.2f}, SPX premium is {indices_difference:.2f}")

        if deviation < MAX_DEVIATION:
            return False

        now = time.time()
        if now - self.last_unfair_ask_warning_time >= 10:
            logger.warning(
                f"Unfair ask for {get_option_name(option)} against {spy_names}: "
                f"SPX Ask: {spx_ask}, SPY Asks: [{spy_tickers[0].ask}, {spy_tickers[1].ask}], deviation: {deviation:.2f} "
                f"(Adjusted: {adjusted_spy_ask:.2f}, SPX premium : {indices_difference:.2f})")
            self.last_unfair_ask_warning_time = now
        return True

    def _calculate_adjusted_spy_ask(self, spy_ask, spy_delta, spy_gamma):
        indices_difference = self.market_data_fetcher.calculate_spx_spy_difference()
        error_spy = indices_difference / 10.0
        delta_component = spy_delta * error_spy
        gamma_component = 0.5 * spy_gamma * (error_spy ** 2)
        adjusted_spy_baseline = spy_ask + delta_component + gamma_component
        return 10.0 * adjusted_spy_baseline, indices_difference

    async def guard_current_positions(self):
        logger.debug("Checking current positions")
        positions = self.trading_bot.get_short_options()
        open_trades = self.trading_bot.get_open_trades()

        if positions:
            await asyncio.gather(*(self.handle_current_risk(position, open_trades) for position in positions))

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

        if self.alternative_valuation == "SPY" and is_regular_hours():
            spy_options = self.subscription_manager.spx_to_spy_map.get(option.conId)
            if spy_options and self.is_unfair_ask_value(option, spy_options):
                self.handle_unfair_ask_value(high_limit_buy_trade, option, spy_options, stop_loss)
                return
        elif self.alternative_valuation == "ES":
            es_option = self.subscription_manager.spx_to_es_map.get(option.conId)
            if es_option and self.is_unfair_ask_value_es(option, es_option):
                self.handle_unfair_ask_value_es(high_limit_buy_trade, option, es_option, stop_loss)
                return

        if stop_loss * 0.5 <= current_price < stop_loss:
            logger.info(f"Watching the current price of {get_option_name(option)}: {current_price:.2f}, stop loss is at {stop_loss:.2f}")

        if current_price < stop_loss or math.isnan(current_price):
            return

        if not high_limit_buy_trade:
            if current_price > stop_loss:
                logger.info(f"Creating missing limit order for {get_option_name(option)}, limit: {stop_loss}")
                await self.trading_bot.close_short_option_position(position, limit=stop_loss)
            return

        await self.handle_high_limit_buy_trade(high_limit_buy_trade, position, stop_loss_per_option)

    async def handle_high_limit_buy_trade(self, high_limit_buy_trade: Trade, position,
                                          stop_loss_per_option: float):
        assert high_limit_buy_trade

        option = position.contract
        last_mod_time = self.last_modification_times.get(high_limit_buy_trade.order.orderId, 0)
        if time.time() - last_mod_time < 2:
            logger.info(
                f"Skipping modification for {get_option_name(option)} as it was modified less than 2 seconds ago")
            return

        current_price = option.ticker.marketPrice()
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

    def handle_unfair_ask_value(self, high_limit_buy_trade: Any | None, option, spy_options: list[Option],
                                      stop_loss: Any):
        logger.warning(
            f"Ask value of {get_option_name(option)} is unfair (Ask: {option.ticker.ask}) against SPY, "
            f"will not close position")

        if high_limit_buy_trade:
            logger.warning(
                f"Cancelling buy order for {get_option_name(option)} since the ask value is unfair against SPY")
            self.trading_bot.cancel_order(high_limit_buy_trade.order)

        # Simplified for hedging recommendation - just use the first one or both
        for spy_option in spy_options:
            spy_ticker = self.ib.ticker(spy_option)
            if spy_ticker:
                spy_current_price = spy_ticker.marketPrice()

                if not math.isnan(spy_current_price):
                    spy_current_adjusted_price = spy_current_price * 10
                    if spy_current_adjusted_price > stop_loss:
                        logger.warning(f"Should consider buying {get_spy_option_name(spy_option)}, since the ask value of "
                                       f"{get_option_name(option)} is unfair, and the fair price is above the stop loss")

                        if self.enable_spy_option_hedging:
                            pass

    def is_unfair_ask_value_es(self, option, es_option):
        spx_ask = option.ticker.ask
        if spx_ask < MIN_PRICE_THRESHOLD:
            return False

        es_ticker = self.market_data_fetcher.get_ticker(es_option)
        es_name = f"ES {es_option.right} {es_option.strike}"

        if not self._validate_alt_ticker(option, es_option, es_ticker, es_name, "ES"):
            return False
        if not self._validate_greeks(es_ticker, es_name):
            return False

        greeks = es_ticker.askGreeks or es_ticker.modelGreeks
        indices_difference = self.market_data_fetcher.calculate_spx_es_difference()
        adjusted_es_ask = calculate_adjusted_es_price(es_ticker.ask, greeks.delta, greeks.gamma, indices_difference)

        if not self._validate_indices_difference(indices_difference):
            return False

        deviation = (spx_ask - adjusted_es_ask) / adjusted_es_ask

        if int(time.time() * 10) % 1000 == 0:
            logger.info(f"Checking option {get_option_name(option)} for unfair ask using {es_name}. "
                        f"SPX ask is {spx_ask} and the Adjusted ES ask is {adjusted_es_ask:.2f}, "
                        f"the deviation is {deviation:.2f}, SPX premium is {indices_difference:.2f}")

        if deviation < MAX_DEVIATION:
            return False

        now = time.time()
        if now - self.last_unfair_ask_warning_time >= 10:
            logger.warning(
                f"Unfair ask for {get_option_name(option)} against {es_name}: "
                f"SPX Ask: {spx_ask}, ES Ask: {es_ticker.ask}, deviation: {deviation:.2f} "
                f"(Adjusted: {adjusted_es_ask:.2f}, SPX premium : {indices_difference:.2f})")
            self.last_unfair_ask_warning_time = now
        return True

    def handle_unfair_ask_value_es(self, high_limit_buy_trade: Any | None, option, es_option: FuturesOption,
                                        stop_loss: Any):
        logger.warning(
            f"Ask value of {get_option_name(option)} ({option.ticker.ask}) is unfair against ES option ask value (Ask: {es_option.ticker.ask}), "
            f"will not close position")

        if high_limit_buy_trade:
            logger.warning(
                f"Cancelling buy order for {get_option_name(option)} since the ask value is unfair against ES")
            self.trading_bot.cancel_order(high_limit_buy_trade.order)

        es_ticker = self.ib.ticker(es_option)
        if es_ticker:
            es_current_price = es_ticker.marketPrice()

            if not math.isnan(es_current_price):
                if es_current_price > stop_loss:
                    logger.warning(f"Should consider buying ES hedge {es_option.strike}, since the ask value of "
                                   f"{get_option_name(option)} is unfair, and the fair price is above the stop loss")

    def _validate_alt_ticker(self, option, alt_option, alt_ticker, alt_name, alt_type):
        if not alt_ticker:
            logger.info(f"Matching {alt_type} ticker for {get_option_name(option)} ({alt_name}) not found in tickers cache. "
                        f"Invalidating subscription in SubscriptionManager. Unfairness is not detected")
            if alt_type == "SPY":
                self.subscription_manager.spx_to_spy_map.pop(option.conId, None)
            else:
                self.subscription_manager.spx_to_es_map.pop(option.conId, None)
            return False

        if math.isnan(alt_ticker.ask) or alt_ticker.ask <= 0:
            logger.info(f"Matching {alt_type} ticker for {get_option_name(option)} ({alt_name}) has an invalid ask value: "
                        f"{alt_ticker.ask}. Unfairness is not detected")
            return False
        
        return True

    def _validate_greeks(self, ticker, name):
        greeks = ticker.askGreeks or ticker.modelGreeks
        if not greeks:
            logger.info(f"Matching ticker for {name} has no ask greeks nor model greeks. Unfairness is not detected")
            return False

        if math.isnan(greeks.delta) or math.isnan(greeks.gamma):
            logger.info(f"Matching ticker for {name} has an invalid delta and gamma values: "
                        f"delta: {greeks.delta}, gamma: {greeks.gamma}. Unfairness is not detected")
            return False

        return True

    def _validate_indices_difference(self, indices_difference):
        if math.isnan(indices_difference):
            logger.info(f"Indices difference is NaN. Unfairness is not detected")
            return False
        return True
