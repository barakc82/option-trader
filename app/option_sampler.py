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
            self.collected_samples = []
            self._load_cached_collected_samples()
            self._initialized = True

    def _load_cached_collected_samples(self):
        try:
            with open(CACHED_JSON_PATH, 'r') as f:
                state = json.load(f)
            for sample in state.get('random_states', []):
                date = sample.get('date')
                strike = sample.get('strike')
                right = sample.get('right')
                if not date or strike is None or not right:
                    continue

                expiry = datetime.strptime(date, "%d/%m/%y").strftime("%Y%m%d")

                target_delta = sample.get('target_delta')
                quantity = sample.get('quantity')
                estimated_sell_price = sample.get('estimated_sell_price')
                stop_loss_per_option = sample.get('stop_loss_per_option')
                bid_delta = sample.get('bid_delta')
                ask_delta = sample.get('ask_delta')
                last_delta = sample.get('last_delta')
                model_delta = sample.get('model_delta')
                minutes_to_expiration = sample.get('minutes_to_expiration')
                distance_to_stop_pct = sample.get('distance_to_stop_pct')
                implied_volatility = sample.get('implied_volatility')
                self.collected_samples.append(PositionInitialState(
                    is_executed=0,
                    strike=float(strike), right=right, expiry=expiry,
                    target_delta=float(target_delta) if target_delta not in (None, '') else 0.0,
                    quantity=int(quantity) if quantity not in (None, '') else 0,
                    estimated_sell_price=float(estimated_sell_price) if estimated_sell_price not in (None, '') else 0.0,
                    stop_loss_per_option=float(stop_loss_per_option) if stop_loss_per_option not in (None, '') else 0.0,
                    bid_delta=float(bid_delta) if bid_delta not in (None, '') else None,
                    ask_delta=float(ask_delta) if ask_delta not in (None, '') else None,
                    last_delta=float(last_delta) if last_delta not in (None, '') else None,
                    model_delta=float(model_delta) if model_delta not in (None, '') else None,
                    minutes_to_expiration=int(minutes_to_expiration) if minutes_to_expiration not in (None, '') else None,
                    distance_to_stop_pct=float(distance_to_stop_pct) if distance_to_stop_pct not in (None, '') else None,
                    implied_volatility=float(implied_volatility) if implied_volatility not in (None, '') else None,
                ))
            logger.info(f"Loaded {len(self.collected_samples)} random samples from cache")
        except Exception as e:
            logger.warning(f"Could not load cached random samples: {e}")

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

        number_of_collected_samples = len(self.collected_samples)
        self.sample_times = self.sample_times[number_of_collected_samples:]

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
        random_sample = PositionInitialState(
            is_executed=0,
            strike=option.strike, right=option.right, expiry=option.lastTradeDateOrContractMonth,
            estimated_sell_price=estimated_sell_price, stop_loss_per_option=stop_loss_per_option,
            target_delta=target_delta,
            bid_delta=bid_delta, ask_delta=ask_delta, last_delta=last_delta, model_delta=model_delta,
            minutes_to_expiration=get_minutes_to_expiration(option),
            implied_volatility=self.market_data_fetcher.get_cached_spx_implied_volatility(right),
            distance_to_stop_pct=get_distance_to_stop_pct(option, estimated_sell_price, stop_loss_per_option, self.market_data_fetcher),
        )

        self.collected_samples.append(random_sample)
