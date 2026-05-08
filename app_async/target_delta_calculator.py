import statistics
import math
from collections import deque
import traceback

from utilities.utils import *

from .market_data_fetcher import MarketDataFetcher
from .max_loss_calculator import DEFAULT_MAX_LOSS, calculate_max_loss

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

DEFAULT_TARGET_DELTA = 0.008
MIN_TARGET_DELTA = 0.003
MAX_TARGET_DELTA = 0.013

IMPLIED_VOLATILITY_FILE_NAME = "cache/iv_log.txt"
DEFAULT_MAX_ENTRIES = 10000

def get_implied_volatility(ticker):
    if ticker.lastGreeks and ticker.lastGreeks.impliedVol:
        return ticker.lastGreeks.impliedVol
    if ticker.modelGreeks and ticker.modelGreeks.impliedVol:
        return ticker.modelGreeks.impliedVol
    return None


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
            self.last_target_delta = DEFAULT_TARGET_DELTA
            self.last_target_delta_calculation_time = 0
            self.last_target_delta_increase = 0
            self.max_entries = DEFAULT_MAX_ENTRIES
            self.iv_history = deque(maxlen=self.max_entries)
            
            self.load_config()
            self.load_iv_history()

            self._initialized = True

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
                        # Re-initialize deque with new maxlen, preserving existing entries if any
                        old_history = list(self.iv_history)
                        self.iv_history = deque(old_history, maxlen=self.max_entries)
        except Exception as e:
            logger.error(f"TargetDeltaCalculator: Error reading config: {e}")

    def load_iv_history(self):
        try:
            if os.path.exists(IMPLIED_VOLATILITY_FILE_NAME):
                with open(IMPLIED_VOLATILITY_FILE_NAME, "r") as f:
                    for line in f:
                        iv_str = line.strip()
                        if iv_str:
                            self.iv_history.append(float(iv_str))
                
                # Truncate the file on startup to match the max_entries limit
                logger.info(f"TargetDeltaCalculator: Truncating IV log to {len(self.iv_history)} entries.")
                with open(IMPLIED_VOLATILITY_FILE_NAME, "w") as f:
                    for entry in self.iv_history:
                        f.write(f"{entry:.2f}\n")
            else:
                logger.warning(f"TargetDeltaCalculator: IV log file {IMPLIED_VOLATILITY_FILE_NAME} not found.")
        except Exception as e:
            logger.error(f"TargetDeltaCalculator: Error loading IV history: {e}")

    async def calculate_target_delta(self):
        self.load_config()

        if time.time() - self.last_target_delta_calculation_time < 60 or not is_market_open():
            return self.last_target_delta

        implied_volatility = None
        try:
            implied_volatility = await self.market_data_fetcher.get_spx_implied_volatility()
            if implied_volatility and not math.isnan(implied_volatility):
                logger.info(f"Implied volatility: {implied_volatility:.2f}")
                self.iv_history.append(implied_volatility)
                # Fast append to the log file
                with open(IMPLIED_VOLATILITY_FILE_NAME, "a") as f:
                    f.write(f"{implied_volatility:.2f}\n")
            else:
                logger.error(f"Invalid implied volatility: {implied_volatility}")
        except Exception as e:
            logger.error(f"{e}")
            traceback.print_exc()
        if not implied_volatility or math.isnan(implied_volatility):
            return self.last_target_delta

        if len(self.iv_history) < 2:
            logger.warning("TargetDeltaCalculator: Not enough IV history to calculate target delta.")
            return self.last_target_delta

        iv_mean = statistics.mean(self.iv_history)
        logger.info(f"Mean implied volatility: {iv_mean:.3f}")
        iv_std = statistics.stdev(self.iv_history)
        z_score = (implied_volatility - iv_mean) / iv_std
        target_delta_mean = (MAX_TARGET_DELTA + MIN_TARGET_DELTA) / 2
        target_delta_std = (target_delta_mean - MIN_TARGET_DELTA) / 2
        target_delta = target_delta_std * z_score + target_delta_mean
        if is_reduced_safe_cushion_time() or is_switched_to_overnight_trading():
            target_delta *= 0.875

        target_delta = max(target_delta, MIN_TARGET_DELTA)
        max_loss = (await calculate_max_loss('C') + await calculate_max_loss('P')) / 2
        logger.info(f"Max loss: {max_loss:.2f}")
        target_delta_increase = (max_loss - DEFAULT_MAX_LOSS) / 1000
        logger.info(f"Target delta increase: {target_delta_increase:.4f}")
        target_delta += target_delta_increase
        self.last_target_delta_calculation_time = time.time()
        self.last_target_delta = target_delta
        self.last_target_delta_increase = target_delta_increase
        return target_delta
