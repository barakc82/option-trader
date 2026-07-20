import asyncio
import json
import os
import random
from datetime import datetime

from utilities.utils import *
from utilities.ib_utils import *

from .max_loss_calculator import MaxLossCalculator
from .target_delta_calculator import TargetDeltaCalculator
from .strike_finder import StrikeFinder
from .opportunity_explorer import OpportunityExplorer
from .market_data_fetcher import MarketDataFetcher

logger = logging.getLogger(__name__)

DEFAULT_NUMBER_OF_SAMPLES_PER_DAY = 1


class OptionSampler:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(OptionSampler, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if not self._initialized:
            self.max_loss_calculator = MaxLossCalculator()
            self.target_delta_calculator = TargetDeltaCalculator()
            self.strike_finder = StrikeFinder()
            self.opportunity_explorer = OpportunityExplorer()
            self.market_data_fetcher = MarketDataFetcher()

            self.number_of_samples_per_day = DEFAULT_NUMBER_OF_SAMPLES_PER_DAY
            self.schedule_date = None
            self.sample_times = []
            self._initialized = True

    def load_config(self):
        """Reads configuration from OPTION_TRADER_CONFIG_PATH."""
        try:
            if os.path.exists(OPTION_TRADER_CONFIG_PATH):
                with open(OPTION_TRADER_CONFIG_PATH, "r") as f:
                    config = json.load(f)

                new_number_of_samples_per_day = config.get("number_of_samples_per_day", DEFAULT_NUMBER_OF_SAMPLES_PER_DAY)
                if new_number_of_samples_per_day != self.number_of_samples_per_day:
                    logger.info(f"OptionSampler: number_of_samples_per_day changed from {self.number_of_samples_per_day} to {new_number_of_samples_per_day}")
                    self.number_of_samples_per_day = new_number_of_samples_per_day
        except Exception as e:
            logger.error(f"OptionSampler: Error reading config: {e}")

    def build_schedule(self, now_nyc):
        """Divide [previous SPX expiration close, next SPX expiration close) into number_of_samples_per_day periods."""
        cal = get_nyse_calendar()
        start_time = cal.previous_close(now_nyc).astimezone(new_york_timezone)
        next_expiration_date = datetime.strptime(get_current_trading_day(), '%Y%m%d').date()
        expiration_time = new_york_timezone.localize(datetime.combine(next_expiration_date, REGULAR_HOURS_END_TIME))

        period_length = (expiration_time - start_time) / self.number_of_samples_per_day
        self.sample_times = [start_time + i * period_length for i in range(self.number_of_samples_per_day)]
        self.schedule_date = now_nyc.date()

        if not self.sample_times:
            return

        logger.info(
            f"Built a schedule of {self.number_of_samples_per_day} samples "
            f"from {start_time} to {expiration_time}")
        logger.info(self.sample_times)

    async def run(self):
        logger.info("Starting sampling loop...")
        while True:
            try:
                self.load_config()

                now_nyc = datetime.now(new_york_timezone)
                cal = get_nyse_calendar()
                is_trading_day = cal.is_session(now_nyc.date().strftime('%Y-%m-%d'))

                if not self.sample_times:
                    self.build_schedule(now_nyc)

                if self.sample_times and now_nyc >= self.sample_times[0]:
                    self.sample_times.pop(0)
                    if is_market_open():
                        self.collect_sample()

            except Exception:
                logger.exception("OptionSampler: Loop error:")

            await asyncio.sleep(300)

    def collect_sample(self):
        logger.info("Time to sample")

        right = random.choice(['C', 'P'])
        stop_loss_per_option = self.max_loss_calculator.calculate_max_loss(right)
        stop_loss_per_option = random.uniform(stop_loss_per_option / 2, stop_loss_per_option * 2)
        target_delta, _ = self.target_delta_calculator.calculate_max_loss_based_target_delta(right, stop_loss_per_option)
        option = self.strike_finder.get_cached_low_delta_option(target_delta, right)
        if option is None:
            return

        estimated_sell_price = self.opportunity_explorer.estimate_sell_price(option)
        bid_delta, ask_delta, last_delta, model_delta = get_individual_deltas(option.ticker)
        position_initial_state = PositionInitialState(
            is_executed=0,
            strike=option.strike, right=option.right, expiry=option.lastTradeDateOrContractMonth,
            estimated_sell_price=estimated_sell_price, stop_loss_per_option=stop_loss_per_option,
            target_delta=target_delta,
            bid_delta=bid_delta, ask_delta=ask_delta, last_delta=last_delta, model_delta=model_delta,
            minutes_to_expiration=get_minutes_to_expiration(option),
            implied_volatility=self.market_data_fetcher.get_cached_spx_implied_volatility(right),
            distance_to_stop_pct=get_distance_to_stop_pct(option, estimated_sell_price, stop_loss_per_option, self.market_data_fetcher),
        )