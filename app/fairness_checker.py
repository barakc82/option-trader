import math
import time

from utilities.utils import *
from utilities.ib_utils import *

from .market_data_fetcher import MarketDataFetcher
from .subscription_manager import SubscriptionManager

logger = logging.getLogger(__name__)

MAX_DEVIATION = 0.1
MIN_PRICE_THRESHOLD = 1


class FairnessChecker:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(FairnessChecker, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if not self._initialized:
            self.market_data_fetcher = MarketDataFetcher()
            self.subscription_manager = SubscriptionManager()

            self.should_check_fairness = True
            self.config = {}
            self.last_unfair_ask_warning_times = {}
            self.last_no_es_option_log_times = {}
            self._initialized = True

    def load_config(self):
        config_path = OPTION_TRADER_CONFIG_PATH
        try:
            if os.path.exists(config_path):
                with open(config_path, "r") as f:
                    self.config = json.load(f)
                    self.should_check_fairness = self.config.get("should_check_fairness", True)
        except Exception as e:
            logger.error(f"FairnessChecker: Error reading config: {e}")

    def is_unfair_ask_value(self, option, es_options):
        if not self.should_check_fairness:
            return False

        if not es_options:
            now = time.time()
            if now - self.last_no_es_option_log_times.get(option.conId, 0) >= 60:
                logger.error(f"No ES option found for {get_option_name(option)}, will not evaluate unfairness")
                self.last_no_es_option_log_times[option.conId] = now
            return False

        spx_ask = option.ticker.ask
        if spx_ask < MIN_PRICE_THRESHOLD:
            return False

        indices_difference = self.market_data_fetcher.calculate_spx_es_difference()
        if not self._validate_indices_difference(indices_difference):
            return False

        lower_es, upper_es = es_options
        equivalent_es_strike = option.strike - indices_difference
        if equivalent_es_strike < lower_es.strike or equivalent_es_strike > upper_es.strike:
            self.subscription_manager.invalidate_key(option)
            logger.error(f"The equivalent ES strike price of {get_option_name(option)} is {equivalent_es_strike:.2f}, "
                         f"but the current matching ES options are {get_es_option_name(lower_es)} and {get_es_option_name(upper_es)}, "
                         f"going to set new matching ES options")
            return False

        lower_ticker = self.market_data_fetcher.get_ticker(lower_es)
        upper_ticker = self.market_data_fetcher.get_ticker(upper_es)

        lower_name = f"ES {lower_es.right} {lower_es.strike}"
        upper_name = f"ES {upper_es.right} {upper_es.strike}"

        if not self._validate_alt_ticker(option, lower_es, lower_ticker, lower_name, "ES"):
            return False
        if not self._validate_alt_ticker(option, upper_es, upper_ticker, upper_name, "ES"):
            return False

        adjusted_es_ask = interpolate_es_price(option.strike, indices_difference, lower_es, upper_es, lower_ticker.ask, upper_ticker.ask)

        deviation = (spx_ask - adjusted_es_ask) / adjusted_es_ask

        if int(time.time() * 10) % 1000 == 0:
            logger.info(f"Checking option {get_option_name(option)} for unfair ask using {lower_name}/{upper_name}. "
                        f"SPX ask is {spx_ask} and the interpolated ES ask is {adjusted_es_ask:.2f}, "
                        f"the deviation is {deviation:.2f}, SPX premium is {indices_difference:.2f}")

        if deviation < MAX_DEVIATION:
            return False

        now = time.time()
        if now - self.last_unfair_ask_warning_times.get(option.conId, 0) >= 4:
            logger.warning(
                f"Unfair ask for {get_option_name(option)} against {lower_name}/{upper_name}: "
                f"SPX Ask: {spx_ask}, interpolated ES ask: {adjusted_es_ask:.2f}, lower ES ask: {lower_ticker.ask},"
                f"upper ES ask: {upper_ticker.ask}, deviation: {deviation:.2f} "
                f"(SPX premium: {indices_difference:.2f})")
            self.last_unfair_ask_warning_times[option.conId] = now
        return True

    def _validate_alt_ticker(self, option, alt_option, alt_ticker, alt_name, alt_type):
        if not alt_ticker:
            logger.info(f"Matching {alt_type} ticker for {get_option_name(option)} ({alt_name}) not found in tickers cache. "
                        f"Invalidating subscription in SubscriptionManager. Unfairness is not detected")
            self.subscription_manager.invalidate_key(option)
            return False

        if math.isnan(alt_ticker.ask) or alt_ticker.ask <= 0:
            logger.info(f"Matching {alt_type} ticker for {get_option_name(option)} ({alt_name}) has an invalid ask value: "
                        f"{alt_ticker.ask}. Unfairness is not detected")
            return False

        return True

    def _validate_indices_difference(self, indices_difference):
        if math.isnan(indices_difference):
            logger.info(f"Indices difference is NaN. Unfairness is not detected")
            return False
        return True
