import statistics
from collections import deque
import traceback

from max_loss_calculator import DEFAULT_MAX_LOSS, calculate_max_loss
from utils import *

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

DEFAULT_TARGET_DELTA = 0.008
MIN_TARGET_DELTA = 0.003
MAX_TARGET_DELTA = 0.013

IMPLIED_VOLATILITY_FILE_NAME = "../cache/iv_log.txt"
MAX_ENTRIES = 10000

iv_history = deque(maxlen=MAX_ENTRIES)


class TargetDeltaCalculator:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(TargetDeltaCalculator, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if not self._initialized:
            self.market_data_fetcher = current_thread.market_data_fetcher
            self.last_target_delta = DEFAULT_TARGET_DELTA
            self.last_target_delta_calculation_time = 0
            self.last_target_delta_increase = 0

            try:
                with open(IMPLIED_VOLATILITY_FILE_NAME, "r") as f:
                    for line in f:
                        iv_str = line.strip()
                        if iv_str:
                            iv_history.append(float(iv_str))
            except FileNotFoundError:
                logger.error("Log file not found.")

            self._initialized = True

    def calculate_target_delta(self):

        if time.time() - self.last_target_delta_calculation_time < 60 or not is_market_open():
            return self.last_target_delta

        implied_volatility = None
        try:
            implied_volatility = self.market_data_fetcher.get_spx_implied_volatility()
            if implied_volatility:
                logger.info(f"Implied volatility: {implied_volatility:.2f}")
                iv_history.append(implied_volatility)
                with open(IMPLIED_VOLATILITY_FILE_NAME, "w") as f:
                    for entry in iv_history:
                        f.write(f"{entry:.2f}\n")
        except Exception as e:
            logger.error(f"{e}")
            traceback.print_exc()
        if not implied_volatility:
            return self.last_target_delta

        iv_mean = statistics.mean(iv_history)
        logger.info(f"Mean implied volatility: {iv_mean:.3f}")
        iv_std = statistics.stdev(iv_history)
        z_score = (implied_volatility - iv_mean) / iv_std
        target_delta_mean = (MAX_TARGET_DELTA + MIN_TARGET_DELTA) / 2
        target_delta_std = (target_delta_mean - MIN_TARGET_DELTA) / 2
        target_delta = target_delta_std * z_score + target_delta_mean
        if is_reduced_safe_cushion_time() or is_switched_to_overnight_trading():
            target_delta *= 0.875

        target_delta = max(target_delta, MIN_TARGET_DELTA)
        max_loss = (calculate_max_loss('C') + calculate_max_loss('P')) / 2
        logger.info(f"Max loss: {max_loss:.2f}")
        target_delta_increase = (max_loss - DEFAULT_MAX_LOSS) / 1000
        logger.info(f"Target delta increase: {target_delta_increase:.4f}")
        target_delta += target_delta_increase
        self.last_target_delta_calculation_time = time.time()
        self.last_target_delta = target_delta
        self.last_target_delta_increase = target_delta_increase
        return target_delta
