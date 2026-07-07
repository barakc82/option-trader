import statistics
import math
from collections import deque
import traceback

from utilities.utils import *

from .market_data_fetcher import MarketDataFetcher
from .max_loss_calculator import DEFAULT_MAX_LOSS, MaxLossCalculator

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

MIN_TARGET_DELTA = {'C': 0.003, 'P': 0.002}
MAX_TARGET_DELTA = {'C': 0.011, 'P': 0.012}
AVERAGE_TARGET_DELTA = {
    'C': (MAX_TARGET_DELTA['C'] + MIN_TARGET_DELTA['C']) / 2,
    'P': (MAX_TARGET_DELTA['P'] + MIN_TARGET_DELTA['P']) / 2
}

DEFAULT_MAX_ENTRIES = 20000


class TargetDeltaCalculator:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(TargetDeltaCalculator, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if not self._initialized:
            self.market_data_fetcher = MarketDataFetcher()
            self.max_loss_calculator = MaxLossCalculator()
            self.last_target_delta = {'C': AVERAGE_TARGET_DELTA['C'], 'P': AVERAGE_TARGET_DELTA['P']}
            self.last_target_delta_calculation_time = {'C': 0, 'P': 0}
            self.last_target_delta_increase = {'C': 0, 'P': 0}
            self.max_entries = DEFAULT_MAX_ENTRIES
            self.iv_history = {
                'C': deque(maxlen=self.max_entries),
                'P': deque(maxlen=self.max_entries)
            }
            
            self.load_config()
            self.load_iv_history()

            self._initialized = True

    def get_iv_log_file_name(self, right):
        name = "calls" if right == 'C' else "puts"
        return f"cache/{name}_iv_log.txt"

    def load_config(self):
        config_path = "config/option_trader_config.json"
        try:
            if os.path.exists(config_path):
                with open(config_path, "r") as f:
                    config = json.load(f)
                    new_max_entries = config.get("iv_history_max_entries", DEFAULT_MAX_ENTRIES)
                    if new_max_entries != self.max_entries:
                        logger.info(f"TargetDeltaCalculator: Updating iv_history maxlen from {self.max_entries} to {new_max_entries}")
                        self.max_entries = new_max_entries
                        for right in ['C', 'P']:
                            old_history = list(self.iv_history[right])
                            self.iv_history[right] = deque(old_history, maxlen=self.max_entries)
        except Exception as e:
            logger.error(f"TargetDeltaCalculator: Error reading config: {e}")

    def load_iv_history(self):
        for right in ['C', 'P']:
            file_name = self.get_iv_log_file_name(right)
            try:
                if os.path.exists(file_name):
                    with open(file_name, "r") as f:
                        for line in f:
                            iv_str = line.strip()
                            if iv_str:
                                self.iv_history[right].append(float(iv_str))
                    
                    # Truncate the file on startup to match the max_entries limit
                    logger.info(f"TargetDeltaCalculator: Truncating IV log for {right} to {len(self.iv_history[right])} entries.")
                    with open(file_name, "w") as f:
                        for entry in self.iv_history[right]:
                            f.write(f"{entry:.2f}\n")
                else:
                    logger.warning(f"TargetDeltaCalculator: IV log file {file_name} not found.")
            except Exception as e:
                logger.error(f"TargetDeltaCalculator: Error loading IV history for {right}: {e}")

    def get_cached_target_delta(self, right):
        return self.last_target_delta[right]

    async def calculate_target_delta(self, right):
        self.load_config()

        if self.last_target_delta_calculation_time[right] < self.market_data_fetcher.options_dump_time:
            target_delta, _ = self.calculate_max_loss_based_target_delta(right)
            self.last_target_delta[right] = target_delta

        if time.time() - self.last_target_delta_calculation_time[right] < 60:
            return self.last_target_delta[right]

        implied_volatility = None
        try:
            implied_volatility = await self.market_data_fetcher.get_spx_implied_volatility(right)
            if implied_volatility and not math.isnan(implied_volatility):
                logger.info(f"Implied volatility ({right}): {implied_volatility:.2f}")
                self.iv_history[right].append(implied_volatility)
                # Fast append to the log file
                file_name = self.get_iv_log_file_name(right)
                with open(file_name, "a") as f:
                    f.write(f"{implied_volatility:.2f}\n")
            else:
                logger.error(f"Invalid implied volatility for '{right}': {implied_volatility}")
        except Exception as e:
            logger.error(f"Error fetching SPX IV for {right}: {e}")
            traceback.print_exc()

        if not implied_volatility or math.isnan(implied_volatility):
            return self.last_target_delta[right]

        if len(self.iv_history[right]) < 2:
            logger.warning(f"TargetDeltaCalculator: Not enough IV history for {right} to calculate target delta.")
            return self.last_target_delta[right]

        iv_mean = statistics.mean(self.iv_history[right])
        logger.info(f"Mean implied volatility ({right}): {iv_mean:.3f}")
        iv_std = statistics.stdev(self.iv_history[right])
        z_score = (implied_volatility - iv_mean) / iv_std
        target_delta_std = (AVERAGE_TARGET_DELTA[right] - MIN_TARGET_DELTA[right]) / 2
        iv_factor = target_delta_std * z_score
        target_delta, max_loss_factor = self.calculate_max_loss_based_target_delta(right)
        target_delta += iv_factor
        if is_reduced_safe_cushion_time() or is_switched_to_overnight_trading():
            target_delta *= 0.875
        target_delta = max(target_delta, 0.003)
        logger.info(f"Target delta ({right}): {target_delta:.4f}, iv factor: {iv_factor:.2f}, max loss factor: {max_loss_factor:.2f}")
        
        self.last_target_delta_calculation_time[right] = time.time()
        self.last_target_delta[right] = target_delta
        self.last_target_delta_increase[right] = iv_factor
        return target_delta

    def calculate_max_loss_based_target_delta(self, right) -> tuple[float, float]:
        max_loss = self.max_loss_calculator.calculate_max_loss(right)
        logger.info(f"Max loss ({right}): {max_loss:.2f}")
        max_loss_factor = 0.0025 if right == 'C' else 0.003
        target_delta = max_loss * max_loss_factor
        return target_delta, max_loss_factor
