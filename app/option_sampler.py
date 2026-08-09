import asyncio
import json
import os
import random
from datetime import datetime, timedelta

from utilities.utils import *
from utilities.ib_utils import *

from .max_loss_calculator import MaxLossCalculator
from .target_delta_calculator import TargetDeltaCalculator
from .strike_finder import StrikeFinder
from .price_estimator import PriceEstimator
from .market_data_fetcher import MarketDataFetcher
from .positions_manager import PositionsManager
from .trading_bot import TradingBot

logger = logging.getLogger(__name__)

DEFAULT_NUMBER_OF_SAMPLES_PER_DAY = 4
DEFAULT_TARGET_DELTA_TOP_MULTIPLIER = 3


class OptionSampler:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(OptionSampler, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if not self._initialized:
            self.market_data_fetcher = MarketDataFetcher()
            self.max_loss_calculator = MaxLossCalculator()
            self.target_delta_calculator = TargetDeltaCalculator()
            self.strike_finder = StrikeFinder()
            self.trading_bot = TradingBot()
            self.price_estimator = PriceEstimator()

            self.number_of_samples_per_day = DEFAULT_NUMBER_OF_SAMPLES_PER_DAY
            self.target_delta_top_multiplier = DEFAULT_TARGET_DELTA_TOP_MULTIPLIER
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
                estimated_sell_price = sample.get('estimated_sell_price')
                stop_loss = sample.get('stop_loss')
                bid_delta = sample.get('bid_delta')
                ask_delta = sample.get('ask_delta')
                last_ask = sample.get('last_ask')
                last_delta = sample.get('last_delta')
                model_delta = sample.get('model_delta')
                gamma = sample.get('gamma')
                vega = sample.get('vega')
                theta = sample.get('theta')
                minutes_to_expiration = sample.get('minutes_to_expiration')
                distance_to_strike_pct = sample.get('distance_to_strike_pct')
                atm_iv = sample.get('atm_iv')
                self.collected_samples.append(PositionInitialState(
                    is_executed=0,
                    strike=float(strike), right=right, expiry=expiry,
                    target_delta=float(target_delta) if target_delta not in (None, '') else 0.0,
                    estimated_sell_price=float(estimated_sell_price) if estimated_sell_price not in (None, '') else 0.0,
                    stop_loss=float(stop_loss) if stop_loss not in (None, '') else None,
                    bid_delta=float(bid_delta) if bid_delta not in (None, '') else None,
                    ask_delta=float(ask_delta) if ask_delta not in (None, '') else None,
                    last_ask=float(last_ask) if last_ask not in (None, '') else None,
                    last_delta=float(last_delta) if last_delta not in (None, '') else None,
                    model_delta=float(model_delta) if model_delta not in (None, '') else None,
                    gamma=float(gamma) if gamma not in (None, '') else None,
                    vega=float(vega) if vega not in (None, '') else None,
                    theta=float(theta) if theta not in (None, '') else None,
                    minutes_to_expiration=int(minutes_to_expiration) if minutes_to_expiration not in (None, '') else None,
                    distance_to_strike_pct=float(distance_to_strike_pct) if distance_to_strike_pct not in (None, '') else None,
                    atm_iv=float(atm_iv) if atm_iv not in (None, '') else None,
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
                    self.schedule_date = None

                new_target_delta_top_multiplier = config.get("target_delta_top_multiplier", DEFAULT_TARGET_DELTA_TOP_MULTIPLIER)
                if new_target_delta_top_multiplier != self.target_delta_top_multiplier:
                    logger.info(f"OptionSampler: target_delta_top_multiplier changed from {self.target_delta_top_multiplier} to {new_target_delta_top_multiplier}")
                    self.target_delta_top_multiplier = new_target_delta_top_multiplier
        except Exception as e:
            logger.error(f"OptionSampler: Error reading config: {e}")


    def _get_closed_periods(self, cal, start_time, expiration_time):
        """Returns the sorted (start, end) NYC-localized closed-market intervals that fall between
        consecutive trading sessions in [start_time, expiration_time). When those sessions are on
        consecutive calendar days this is just that day's nightly break; when a holiday or weekend
        separates them, the interval spans the whole non-trading gap."""
        sessions = cal.sessions_in_range(start_time.date(), expiration_time.date())
        closed_periods = []
        for prev_session, next_session in zip(sessions[:-1], sessions[1:]):
            period_start = new_york_timezone.localize(datetime.combine(prev_session.date(), AFTER_HOURS_END_TIME))
            period_end = new_york_timezone.localize(
                datetime.combine(next_session.date() - timedelta(days=1), PREMARKET_START_TIME))
            if period_end > period_start:
                closed_periods.append((period_start, period_end))
        return closed_periods

    def _skip_closed_periods(self, start_time, elapsed_open, closed_periods):
        """Maps an elapsed "market open" duration measured from start_time to the actual wall-clock
        time, skipping over any closed_periods encountered along the way."""
        cursor = start_time
        remaining = elapsed_open
        for period_start, period_end in closed_periods:
            if period_start <= cursor:
                continue
            gap = period_start - cursor
            if remaining <= gap:
                return cursor + remaining
            remaining -= gap
            cursor = period_end
        return cursor + remaining

    def build_schedule(self, now_nyc):
        """Divide [previous SPX expiration close, next SPX expiration close) into number_of_samples_per_day
        periods, considering only the time the market is actually open (excluding nightly breaks and any
        holiday/weekend closures in between)."""
        cal = get_nyse_calendar()
        start_time = cal.previous_close(now_nyc).astimezone(new_york_timezone)
        current_trading_day = get_current_trading_day()
        next_expiration_date = datetime.strptime(current_trading_day, '%Y%m%d').date()
        expiration_time = new_york_timezone.localize(datetime.combine(next_expiration_date, REGULAR_HOURS_END_TIME))

        closed_periods = self._get_closed_periods(cal, start_time, expiration_time)
        total_closed = sum((end - start for start, end in closed_periods), timedelta())
        open_duration = (expiration_time - start_time) - total_closed

        period_length = open_duration / self.number_of_samples_per_day
        self.sample_times = [
            self._skip_closed_periods(start_time, i * period_length, closed_periods)
            for i in range(self.number_of_samples_per_day)
        ]
        self.schedule_date = current_trading_day

        number_of_collected_samples = sum(
            1 for sample in self.collected_samples
            if new_york_timezone.localize(datetime.combine(datetime.strptime(sample.expiry, '%Y%m%d').date(), REGULAR_HOURS_END_TIME)) >= now_nyc
        )
        self.sample_times = self.sample_times[number_of_collected_samples:]

        if not self.sample_times:
            logger.warning(f"Will not build a schedule since all slots were already used")
            return

        logger.info(
            f"Built a schedule of {len(self.sample_times)} samples "
            f"from {start_time} to {expiration_time}")
        formatted_sample_times = [t.strftime('%d/%m/%Y %H:%M') for t in self.sample_times]
        logger.info(f"Sample times: {formatted_sample_times}")

    async def run(self):
        logger.info("Starting sampling loop...")
        while True:
            try:
                self.load_config()

                now_nyc = datetime.now(new_york_timezone)

                if is_night_break():
                    logger.info("Starting storing the expired samples...")
                    for sample in list(self.collected_samples):
                        expiry_date = datetime.strptime(sample.expiry, '%Y%m%d').date()
                        expiry_datetime = new_york_timezone.localize(datetime.combine(expiry_date, REGULAR_HOURS_END_TIME))
                        if expiry_datetime < now_nyc:
                            max_ask = await self.market_data_fetcher.find_max_ask(sample)
                            if max_ask == 0:
                                logger.error(f"No max ask found for {get_option_name(sample)}")
                                continue
                            sample.max_ask = max_ask
                            logger.info(f"Storing an expired sample for {get_option_name(sample)}")
                            PositionsManager()._log_close_event(sample)
                            self.collected_samples.remove(sample)
                    logger.info("Done storing the expired samples")

                if self.schedule_date != get_current_trading_day() and not (
                        NEW_OPTION_EXPLORATION_START_TIME < now_nyc.time() < REGULAR_HOURS_END_TIME):
                    self.build_schedule(now_nyc)

                if self.sample_times and now_nyc >= self.sample_times[0]:
                    logger.info("Checking next sample...")
                    if is_market_open():
                        result = self.collect_sample()
                    else:
                        logger.warning("Market is closed, skipping this sample")
                        result = SUCCESS
                    if result == SUCCESS:
                        self.sample_times.pop(0)

                if is_market_open() and self.collected_samples:
                    cached_options = self.strike_finder.get_cached_options()
                    for sample in self.collected_samples:
                        expiry_date = datetime.strptime(sample.expiry, '%Y%m%d').date()
                        expiry_datetime = new_york_timezone.localize(datetime.combine(expiry_date, REGULAR_HOURS_END_TIME))
                        if expiry_datetime < now_nyc:
                            continue
                        cached_option = cached_options[sample.right].get(sample.strike)
                        if cached_option:
                            sample.last_ask = extract_ask(cached_option.ticker)

            except Exception:
                logger.exception("OptionSampler: Loop error:")

            await asyncio.sleep(300)

    def collect_sample(self):
        logger.info("Collecting the next sample...")
        right = random.choice(['C', 'P'])
        stop_loss_per_option = self.max_loss_calculator.calculate_max_loss(right)
        stop_loss_per_option = random.uniform(stop_loss_per_option * 0.50, stop_loss_per_option * 1.5)
        target_delta_base, _ = self.target_delta_calculator.calculate_max_loss_based_target_delta(right, stop_loss_per_option)
        target_delta = random.uniform(target_delta_base * 0.75, target_delta_base * self.target_delta_top_multiplier)
        option = self.strike_finder.get_cached_low_delta_option(target_delta, right)
        if option is None:
            logger.warning("No option could be found for sample collection")
            return FAILED

        estimated_sell_price = self.price_estimator.estimate_sell_price(option)
        minimal_sell_price = self.trading_bot.calculate_minimal_sell_price(option.ticker.last, option.lastTradeDateOrContractMonth)
        if estimated_sell_price < minimal_sell_price:
            logger.warning(f"Sampled option is sold for {estimated_sell_price} but the minimal sell price is {minimal_sell_price}")
            return FAILED

        bid_delta, ask_delta, last_delta, model_delta = get_individual_deltas(option.ticker)
        random_sample = PositionInitialState(
            is_executed=0,
            strike=option.strike, right=option.right, expiry=option.lastTradeDateOrContractMonth,
            estimated_sell_price=estimated_sell_price,
            target_delta=target_delta,
            stop_loss=estimated_sell_price + stop_loss_per_option,
            bid_delta=bid_delta, ask_delta=ask_delta, last_delta=last_delta, model_delta=model_delta,
            gamma=get_model_gamma(option.ticker),
            vega=get_model_vega(option.ticker), theta=get_model_theta(option.ticker),
            minutes_to_expiration=get_minutes_to_expiration(option),
            atm_iv=self.market_data_fetcher.get_cached_spx_implied_volatility(right),
            contract_iv=get_model_iv(option.ticker),
            distance_to_strike_pct=get_distance_to_strike_pct(option, self.market_data_fetcher),
        )

        self.collected_samples.append(random_sample)
        return SUCCESS
