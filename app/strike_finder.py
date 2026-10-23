import logging
import math
import os
import time
import csv
import json
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from utilities.utils import get_option_name, new_york_timezone, OPTION_TRADER_CONFIG_PATH
from utilities.ib_utils import (extract_ask, get_delta, get_delta_for_sell, get_individual_deltas, get_model_gamma,
                                 get_model_vega, get_model_theta, get_minutes_to_expiration, get_distance_to_strike_pct,
                                 get_model_iv, calculate_expected_profit)
from .limit_adjuster import LimitAdjuster
from .market_data_fetcher import MarketDataFetcher
from .max_loss_calculator import MaxLossCalculator
from .predictor import Predictor
from .price_estimator import PriceEstimator
from .target_delta_calculator import TargetDeltaCalculator


logger = logging.getLogger(__name__)

OPTIONS_BLOCK_SIZE = 100
OPTIONS_BLOCK_LOWER_PART_SIZE = OPTIONS_BLOCK_SIZE // 2
OPTIONS_BLOCK_HIGHER_PART_SIZE = OPTIONS_BLOCK_SIZE - OPTIONS_BLOCK_LOWER_PART_SIZE
FETCHED_BLOCK_MAX_AGE_SECONDS = 5 * 60
ISRAEL_TIMEZONE = timezone(timedelta(hours=3))
DEFAULT_MAX_DELTA = 0.1
RIGHT_CONFIG_PREFIXES = {'C': 'call', 'P': 'put'}

class StrikeFinder:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(StrikeFinder, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if not self._initialized:
            self.market_data_fetcher = MarketDataFetcher()
            self.max_loss_calculator = MaxLossCalculator()
            self.predictor = Predictor()
            self.price_estimator = PriceEstimator()
            self.limit_adjuster = LimitAdjuster()
            self.middle_fetched_block = {'C': [], 'P': []}
            self.edge_fetched_block = {'C': [], 'P': []}
            self.middle_fetched_block_last_update_time = {'C': 0.0, 'P': 0.0}
            self.edge_fetched_block_last_update_time = {'C': 0.0, 'P': 0.0}
            self.max_delta = {'C': DEFAULT_MAX_DELTA, 'P': DEFAULT_MAX_DELTA}
            self.load_config()
            self._initialized = True

    def load_config(self):
        """Reads {put,call}_max_delta from OPTION_TRADER_CONFIG_PATH."""
        try:
            if os.path.exists(OPTION_TRADER_CONFIG_PATH):
                with open(OPTION_TRADER_CONFIG_PATH, "r") as f:
                    config = json.load(f)

                for right, prefix in RIGHT_CONFIG_PREFIXES.items():
                    new_max_delta = config.get(f"{prefix}_max_delta", DEFAULT_MAX_DELTA)
                    if new_max_delta != self.max_delta[right]:
                        logger.info(f"StrikeFinder: {prefix}_max_delta changed from {self.max_delta[right]} to {new_max_delta}")
                        self.max_delta[right] = new_max_delta
        except Exception as e:
            logger.error(f"StrikeFinder: Error reading config: {e}")

    def _set_middle_fetched_block(self, right, options_block):
        self.middle_fetched_block[right] = options_block
        self.middle_fetched_block_last_update_time[right] = time.time()

    def _set_edge_fetched_block(self, right, options_block):
        self.edge_fetched_block[right] = options_block
        self.edge_fetched_block_last_update_time[right] = time.time()

    async def get_low_delta_put_option(self, put_options, target_delta):
        return await self._get_low_delta_option(put_options, target_delta, 'P')

    async def get_low_delta_call_option(self, call_options, target_delta):
        return await self._get_low_delta_option(call_options, target_delta, 'C')

    def _resolve_delta_for_sell(self, option) -> float | None:
        if not hasattr(option, "ticker"):
            ticker = self.market_data_fetcher.get_ticker(option)
            option.ticker = ticker
        if option.ticker is None:
            logger.error(f"Option {get_option_name(option)} has an empty ticker field")
            return None
        delta = get_delta_for_sell(option.ticker)
        if delta is None or math.isnan(delta):
            return None
        return abs(delta)

    def _scan_block_extremes(self, options_block):
        lowest_delta, highest_delta = 1.0, 0.0
        highest_delta_option = None

        for option in options_block:
            delta = self._resolve_delta_for_sell(option)
            if delta is None: continue
            if delta < lowest_delta: lowest_delta = delta
            if delta > highest_delta:
                highest_delta = delta
                highest_delta_option = option

        return lowest_delta, highest_delta, highest_delta_option

    async def _fetch_edge_block_if_needed(self, options_block, lowest_delta, highest_delta,
                                           strike_to_option, strikes, lower_idx, higher_idx, target_delta, right):
        number_of_strikes = len(strikes)

        if lowest_delta > target_delta:
            logger.info(f"Initial block deltas too high for {right}. Lowest: {lowest_delta:.3f}")
            if right == 'P':
                logger.info(f"Fetching {right} option block: {strikes[0]} -> {strikes[lower_idx-1]}")
                options_block = await self.fetch_options_block(0, lower_idx - 1, strike_to_option, strikes)
            else:
                if higher_idx + 1 < number_of_strikes:
                    logger.info(f"Fetching {right} option block: {strikes[higher_idx+1]} -> {strikes[number_of_strikes-1]}")
                    options_block = await self.fetch_options_block(higher_idx + 1, number_of_strikes - 1, strike_to_option, strikes)
            self._set_edge_fetched_block(right, options_block)

        elif highest_delta < target_delta:
            logger.info(f"Initial block deltas too low for {right}. Highest: {highest_delta:.3f}")
            logger.info(f"Fetching {right} option block: {strikes[higher_idx]} -> {strikes[number_of_strikes - 1]}")
            options_block = await self.fetch_options_block(higher_idx, number_of_strikes - 1, strike_to_option, strikes)
            self._set_edge_fetched_block(right, options_block)

        return options_block

    def _find_highest_delta_under_target(self, options_block, target_delta):
        highest_delta_under_target = 0
        best_candidate = None
        for option in options_block:
            delta = self._resolve_delta_for_sell(option)
            if delta is None: continue

            if highest_delta_under_target < delta < target_delta:
                highest_delta_under_target = delta
                best_candidate = option

        return best_candidate

    def _find_max_expected_profit_option(self, options, right, max_delta=None):
        """Scans options for the one wit    h the highest expected profit. When max_delta is
        given, options whose highest available delta exceeds it (or that have no delta at
        all) are skipped. Returns a SimpleNamespace describing the best option, or None."""
        if not max_delta:
            max_delta = self.max_delta[right]
        stop_loss_per_option = self.max_loss_calculator.calculate_max_loss(right)
        atm_iv = self.market_data_fetcher.get_cached_spx_implied_volatility(right)

        best = None
        for option in options:
            if not hasattr(option, "ticker") or option.ticker is None:
                continue

            try:
                estimated_sell_price = self.price_estimator.estimate_sell_price(option)
                bid_delta, ask_delta, last_delta, model_delta = get_individual_deltas(option.ticker)
                gamma = get_model_gamma(option.ticker)
                vega = get_model_vega(option.ticker)
                theta = get_model_theta(option.ticker)
                minutes_to_expiration = get_minutes_to_expiration(option)
                distance_to_strike_pct = get_distance_to_strike_pct(option, self.market_data_fetcher)
                contract_iv = get_model_iv(option.ticker)
            except (TypeError, ValueError) as e:
                logger.warning(f"Skipping {get_option_name(option)} in expected-profit scan for choosing the best "
                               f"{right} option: {e}")
                continue

            if estimated_sell_price < 0:
                continue

            delta_values = [d for d in (bid_delta, ask_delta, last_delta, model_delta) if d is not None]
            option_max_delta = max(delta_values) if delta_values else None
            if option_max_delta is None or option_max_delta > max_delta:
                continue

            out_of_the_money_probability = self.predictor.predict_out_of_the_money_probability(
                option, right, estimated_sell_price, stop_loss_per_option,
                bid_delta, ask_delta, last_delta, model_delta, gamma, vega, theta,
                minutes_to_expiration, atm_iv, contract_iv, distance_to_strike_pct,
            )
            if out_of_the_money_probability is None:
                continue

            expected_profit = calculate_expected_profit(estimated_sell_price, stop_loss_per_option, out_of_the_money_probability)

            if best is None or expected_profit > best.expected_profit:
                best = SimpleNamespace(
                    option=option,
                    expected_profit=expected_profit,
                    probability=out_of_the_money_probability,
                    max_delta=option_max_delta,
                    estimated_sell_price=estimated_sell_price,
                    stop_loss_per_option=stop_loss_per_option,
                    stop_loss=self.limit_adjuster.adjust_limit_to_market_rules(estimated_sell_price + stop_loss_per_option),
                    minutes_to_expiration=minutes_to_expiration,
                )

        if best is None:
            logger.info(f"No {right} option with an expected profit was found among {len(options)} option(s) "
                        f"with max delta at most {max_delta}")
        else:
            max_delta_str = f"{best.max_delta:.3f}" if best.max_delta is not None else "n/a"
            logger.info(f"Max expected-profit {right} option among {len(options)} option(s): {get_option_name(best.option)}, "
                        f"expected profit: {best.expected_profit:.3f}, probability: {best.probability:.3f}, "
                        f"estimated sell price: {best.estimated_sell_price:.2f}, max delta: {max_delta_str} "
                        f"(limit {max_delta})")
        return best

    def _log_best_expected_profit_option(self, target_delta, right):
        options = list(self.middle_fetched_block[right])
        if self._is_recently_updated(self.edge_fetched_block_last_update_time.get(right)):
            options += self.edge_fetched_block[right]

        max_delta = target_delta * 9
        best = self._find_max_expected_profit_option(options, right, max_delta=max_delta)
        if best is None:
            return

        # Two outcomes: +estimated_sell_price if it expires out of the money,
        # -stop_loss_per_option otherwise, so Var = E[profit^2] - E[profit]^2.
        in_the_money_probability = 1 - best.probability
        profit_variance = (best.probability * best.estimated_sell_price ** 2
                           + in_the_money_probability * best.stop_loss_per_option ** 2
                           - best.expected_profit ** 2)

        max_delta_str = f"{best.max_delta:.3f}" if best.max_delta is not None else "n/a"
        logger.info(f"Best expected-profit {right} option in the scanned block: {get_option_name(best.option)}, "
                    f"expected profit: {best.expected_profit:.3f}, profit variance: {profit_variance:.3f}, "
                    f"estimated sell price: {best.estimated_sell_price:.2f}, "
                    f"stop loss: {best.stop_loss:.2f}, probability: {best.probability:.3f}, "
                    f"max delta: {max_delta_str}, target delta: {target_delta:.3f}")

        self._log_best_opportunity(
            best.option.strike, best.option.right, best.option.lastTradeDateOrContractMonth,
            best.estimated_sell_price, best.stop_loss, best.probability, best.expected_profit, best.minutes_to_expiration,
        )

    def _finalize_candidate(self, current_candidate, options_block, target_delta, right):
        final_delta = get_delta_for_sell(current_candidate.ticker)
        if final_delta is None:
            logger.error(f"No delta data available for the candidate option {get_option_name(current_candidate)}")
            return None

        if current_candidate == options_block[0] and final_delta < 0.0001:
            return None

        if final_delta > target_delta:
            logger.error(f"The selected option ({get_option_name(current_candidate)}) has a delta of {final_delta:.3f}, which is higher than the target delta ({target_delta:.3f}), thus no option selected")
            return None

        log_message = f"Selected option: {get_option_name(current_candidate)}, delta: {final_delta:.3f}, target: {target_delta:.3f}"
        if current_candidate.ticker.lastGreeks and current_candidate.ticker.lastGreeks.delta:
            log_message += f", last delta: {current_candidate.ticker.lastGreeks.delta:.3f}"
        if current_candidate.ticker.modelGreeks and current_candidate.ticker.modelGreeks.delta:
            log_message += f", model delta: {current_candidate.ticker.modelGreeks.delta:.3f}"
        if current_candidate.ticker.bidGreeks and current_candidate.ticker.bidGreeks.delta:
            log_message += f", bid delta: {current_candidate.ticker.bidGreeks.delta:.3f}"
        if current_candidate.ticker.askGreeks and current_candidate.ticker.askGreeks.delta:
            log_message += f", ask delta: {current_candidate.ticker.askGreeks.delta:.3f}"
        log_message += f", bid: {current_candidate.ticker.bid}, ask: {current_candidate.ticker.ask}"
        logger.info(log_message)
        return current_candidate

    async def _fetch_middle_block(self, options, right):
        """Fetches snapshots for the OPTIONS_BLOCK_SIZE strikes around the middle of options
        and caches them as the middle fetched block. Calls are ordered from higher strikes
        to lower, puts from lower to higher."""
        strike_to_option = {option.strike: option for option in options}
        strikes = sorted(strike_to_option.keys(), reverse=(right == 'C'))

        number_of_strikes = len(strikes)
        middle_idx = number_of_strikes // 2
        lower_idx = max(middle_idx - OPTIONS_BLOCK_LOWER_PART_SIZE, 0)
        higher_idx = min(middle_idx + OPTIONS_BLOCK_HIGHER_PART_SIZE, number_of_strikes - 1)

        logger.info(f"Fetching {right} option block: {strikes[lower_idx]} -> {strikes[higher_idx]}")
        options_block = await self.fetch_options_block(lower_idx, higher_idx, strike_to_option, strikes)
        self._set_middle_fetched_block(right, options_block)
        return options_block, strike_to_option, strikes, lower_idx, higher_idx

    async def get_max_expected_profit_option(self, options, right):
        """Fetches fresh snapshots for the middle block of options and returns the one with
        the highest expected profit whose delta is at most the configured {call,put}_max_delta,
        or None if none qualifies."""
        assert options
        self.load_config()
        middle_options_block, strike_to_option, strikes, lower_idx, higher_idx = await self._fetch_middle_block(options, right)
        lowest_delta, highest_delta, _ = self._scan_block_extremes(middle_options_block)
        edge_options_block = await self._fetch_edge_block_if_needed(
            middle_options_block, lowest_delta, highest_delta, strike_to_option, strikes, lower_idx, higher_idx,
            self.max_delta[right], right)
        options_block = middle_options_block + edge_options_block
        best = self._find_max_expected_profit_option(options_block, right)
        return best.option if best else None

    async def _get_low_delta_option(self, options, target_delta, right):
        assert options
        self.load_config()

        options_block, strike_to_option, strikes, lower_idx, higher_idx = await self._fetch_middle_block(options, right)

        lowest_delta, highest_delta, highest_delta_option = self._scan_block_extremes(options_block)

        if lowest_delta > highest_delta:
            logger.error(f"No delta data available for {right} options")
            return None

        current_candidate = None
        if lowest_delta > target_delta:
            current_candidate = options_block[0] if right == 'P' else options_block[-1]
        elif highest_delta < target_delta:
            current_candidate = highest_delta_option

        options_block = await self._fetch_edge_block_if_needed(
            options_block, lowest_delta, highest_delta, strike_to_option, strikes, lower_idx, higher_idx,
            target_delta, right)

        current_candidate = self._find_highest_delta_under_target(options_block, target_delta) or current_candidate

        if not current_candidate:
            logger.error(f"No {right} option candidate found")
            return None

        self._log_best_expected_profit_option(target_delta, right)

        return self._finalize_candidate(current_candidate, options_block, target_delta, right)

    def _is_recently_updated(self, last_update_time) -> bool:
        return last_update_time is not None and time.time() - last_update_time <= FETCHED_BLOCK_MAX_AGE_SECONDS

    def find_first_cheap_option(self, right):
        """
        Traverses middle and then edge fetched blocks to find the first option with ask 0.05.
        Calls: Traverses lower strikes to higher.
        Puts: Traverses higher strikes to lower.
        Only blocks updated within the last FETCHED_BLOCK_MAX_AGE_SECONDS are considered.
        """
        blocks_to_check = []
        if self._is_recently_updated(self.middle_fetched_block_last_update_time.get(right)):
            blocks_to_check.append(self.middle_fetched_block.get(right, []))
        if self._is_recently_updated(self.edge_fetched_block_last_update_time.get(right)):
            blocks_to_check.append(self.edge_fetched_block.get(right, []))

        for block in blocks_to_check:
            if not block:
                continue

            logger.info(f"Scanning block for first cheap option: {block[-1]} --> {block[0]}")
            # In _get_low_delta_option:
            # Calls are sorted Higher -> Lower
            # Puts are sorted Lower -> Higher
            # Traversal requested:
            # Calls: Lower -> Higher (needs reverse)
            # Puts: Higher -> Lower (needs reverse)
            for option in reversed(block):
                if not hasattr(option, "ticker"):
                    logger.error(f"Option {get_option_name(option)} has no ticker field")
                    continue
                if option.ticker is None:
                    logger.error(f"Option {get_option_name(option)} has an empty ticker field")
                    continue
                if extract_ask(option.ticker) == 0.05:
                    return option

        return None

    async def fetch_options_block(self, lower_idx, higher_idx, strike_to_option, strikes):
        if lower_idx > higher_idx: return []
        options_block = [strike_to_option[strikes[i]] for i in range(lower_idx, higher_idx + 1)]
        await self.market_data_fetcher.request_snapshots(options_block)
        return options_block

    async def get_available_cheap_call_option(self, call_options, min_strike):
        return await self._get_available_cheap_option(call_options, min_strike, 'C')

    async def get_available_cheap_put_option(self, put_options, max_strike):
        return await self._get_available_cheap_option(put_options, max_strike, 'P')

    async def _get_available_cheap_option(self, options, strike_limit, right):
        strike_to_option = {o.strike: o for o in options}
        if right == 'C':
            relevant_strikes = sorted([s for s in strike_to_option.keys() if s > strike_limit])
        else:
            relevant_strikes = sorted([s for s in strike_to_option.keys() if s < strike_limit])
        
        if not relevant_strikes: return None

        num_strikes = len(relevant_strikes)
        mid = num_strikes // 2
        l_idx = max(mid - OPTIONS_BLOCK_LOWER_PART_SIZE, 0)
        h_idx = min(mid + OPTIONS_BLOCK_HIGHER_PART_SIZE, num_strikes - 1)

        logger.info(f"Fetching {right} option block (middle): {relevant_strikes[l_idx]} -> {relevant_strikes[h_idx]}, indices: {l_idx} -> {h_idx}")
        options_block = await self.fetch_options_block(l_idx, h_idx, strike_to_option, relevant_strikes)
        if not options_block: return None

        self._set_middle_fetched_block(right, options_block)
        available_cheap = None
        
        # Check if we need to fetch more blocks based on liquidity/price
        if right == 'C':
            logger.info(f"barak: the ask value of the first option is: {extract_ask(options_block[0].ticker)}, {get_option_name(options_block[-1])}")
            if extract_ask(options_block[0].ticker) == 0.05 and h_idx + 1 < num_strikes:
                available_cheap = options_block[-1]
                logger.info(f"Fetching {right} option block (edge): {relevant_strikes[h_idx+1]} -> {relevant_strikes[num_strikes-1]}, indices: {h_idx+1} -> {num_strikes-1}")
                options_block = await self.fetch_options_block(h_idx + 1, num_strikes - 1, strike_to_option, relevant_strikes)
                self._set_edge_fetched_block(right, options_block)
        else:
            if extract_ask(options_block[0].ticker) > 0.05:
                if l_idx > 0:
                    logger.info(
                        f"Fetching {right} option block (edge): {relevant_strikes[0]} -> {relevant_strikes[l_idx-1]}, indices: 0 -> {l_idx-1}")
                    options_block = await self.fetch_options_block(0, l_idx - 1, strike_to_option, relevant_strikes)
                    self._set_edge_fetched_block(right, options_block)

            if options_block and extract_ask(options_block[-1].ticker) == 0.05 and h_idx + 1 < num_strikes:
                available_cheap = options_block[-1]
                logger.info(f"Fetching {right} option block (edge): {relevant_strikes[h_idx+1]} -> {relevant_strikes[num_strikes-1]}, indices: {h_idx+1} -> {num_strikes-1}")
                options_block = await self.fetch_options_block(h_idx + 1, num_strikes - 1, strike_to_option, relevant_strikes)
                self._set_edge_fetched_block(right, options_block)

        for option in options_block:
            if not hasattr(option, "ticker"):
                logger.error(f"Option {get_option_name(option)} has no ticker field")
                continue
            if option.ticker is None:
                logger.error(f"Option {get_option_name(option)} has an empty ticker field")
                continue
            ask = extract_ask(option.ticker)
            if ask is None or ask > 0.05: continue
            if right == 'C':
                if available_cheap is None or option.strike < available_cheap.strike:
                    available_cheap = option
            else:
                if available_cheap is None or option.strike > available_cheap.strike:
                    available_cheap = option

        if available_cheap is None:
            logger.error(f"No available cheap {right} option was found")
            return None
            
        return available_cheap


    def get_cached_options(self):
        cached_options = {}

        for right in ['C', 'P']:
            strike_to_option = {option.strike: option for option in self.edge_fetched_block[right]}
            strike_to_option.update({option.strike: option for option in self.middle_fetched_block[right]})

            cached_options[right] = strike_to_option

        return cached_options

    def get_cached_low_delta_option(self, target_delta, right):
        options_block = self.middle_fetched_block[right]
        if not options_block:
            logger.warning(f"No middle option block found for {right}")
            return None

        strike_to_option = {option.strike: option for option in options_block}
        strikes = sorted(strike_to_option.keys(), reverse=(right == 'C'))

        number_of_strikes = len(strikes)
        middle_idx = number_of_strikes // 2
        lower_idx = max(middle_idx - OPTIONS_BLOCK_LOWER_PART_SIZE, 0)
        higher_idx = min(middle_idx + OPTIONS_BLOCK_HIGHER_PART_SIZE, number_of_strikes - 1)

        lowest_delta, highest_delta = 1.0, 0.0
        highest_delta_option = None

        for option in options_block:
            if not hasattr(option, "ticker"):
                logger.error(f"Option {get_option_name(option)} has no ticker field")
                ticker = self.market_data_fetcher.get_ticker(option)
                option.ticker = ticker
            if option.ticker is None:
                logger.error(f"Option {get_option_name(option)} has an empty ticker field")
                continue
            delta = get_delta_for_sell(option.ticker)
            if delta is None or math.isnan(delta): continue
            delta = abs(delta)
            if delta < lowest_delta: lowest_delta = delta
            if delta > highest_delta:
                highest_delta = delta
                highest_delta_option = option

        if lowest_delta > highest_delta:
            logger.error(f"No delta data available for {right} options")
            return None

        current_candidate = None
        if lowest_delta > target_delta:
            current_candidate = options_block[0] if right == 'P' else options_block[-1]
            logger.info(f"Initial block deltas too high for {right}. Lowest: {lowest_delta:.3f}")
            if right == 'P':
                options_block = self.edge_fetched_block[right]
            else:
                if higher_idx + 1 < number_of_strikes:
                    options_block = self.edge_fetched_block[right]

        elif highest_delta < target_delta:
            current_candidate = highest_delta_option
            logger.info(f"Initial block deltas too low for {right}. Highest: {highest_delta:.3f}, where the target delta is {target_delta:.3f}")
            options_block = self.edge_fetched_block[right]

        if not options_block:
            logger.warning(f"No edge option block found for {right}")
            return None

        highest_delta_under_target = 0
        for option in options_block:
            if not hasattr(option, "ticker"):
                logger.error(f"Option {get_option_name(option)} has no ticker field")
                ticker = self.market_data_fetcher.get_ticker(option)
                option.ticker = ticker
            if option.ticker is None:
                logger.error(f"Option {get_option_name(option)} has an empty ticker field")
                continue
            delta = get_delta_for_sell(option.ticker)
            if delta is None or math.isnan(delta): continue
            delta = abs(delta)

            if highest_delta_under_target < delta < target_delta:
                highest_delta_under_target = delta
                current_candidate = option

        if not current_candidate:
            logger.error(f"No {right} option candidate found")
            return None

        final_delta = get_delta_for_sell(current_candidate.ticker)
        if final_delta is None:
            logger.error(f"No delta data available for the candidate option {get_option_name(current_candidate)}")
            return None

        if current_candidate == options_block[0] and final_delta < 0.0001:
            return None

        if final_delta > target_delta:
            logger.error(
                f"The selected option ({get_option_name(current_candidate)}) has a delta of {final_delta:.3f}, which is higher than the target delta ({target_delta:.3f}), thus no option selected")
            return None

        log_message = f"Selected option: {get_option_name(current_candidate)}, delta: {final_delta:.3f}, target: {target_delta:.3f}"
        if current_candidate.ticker.lastGreeks and current_candidate.ticker.lastGreeks.delta:
            log_message += f", last delta: {current_candidate.ticker.lastGreeks.delta:.3f}"
        if current_candidate.ticker.modelGreeks and current_candidate.ticker.modelGreeks.delta:
            log_message += f", model delta: {current_candidate.ticker.modelGreeks.delta:.3f}"
        if current_candidate.ticker.bidGreeks and current_candidate.ticker.bidGreeks.delta:
            log_message += f", bid delta: {current_candidate.ticker.bidGreeks.delta:.3f}"
        if current_candidate.ticker.askGreeks and current_candidate.ticker.askGreeks.delta:
            log_message += f", ask delta: {current_candidate.ticker.askGreeks.delta:.3f}"
        log_message += f", bid: {current_candidate.ticker.bid}, ask: {current_candidate.ticker.ask}"
        logger.info(log_message)

        return current_candidate

    def _log_best_opportunity(self, strike, right, expiry, estimated_sell_price, stop_loss, probability, expected_profit,
                              minutes_to_expiration):
        csv_path = f"cache/prediction_evaluation/predictions_{expiry}.csv"
        now = datetime.now(new_york_timezone)
        write_header = not os.path.exists(csv_path)
        with open(csv_path, 'a', newline='') as f:
            writer = csv.writer(f, delimiter='\t')
            if write_header:
                writer.writerow([
                    'datetime', 'israel_time', 'strike', 'right', 'estimated_sell_price',
                    'stop_loss', 'probability', 'expected_profit', 'minutes_to_expiration'
                ])
            writer.writerow([
                now.isoformat(), now.astimezone(ISRAEL_TIMEZONE).strftime('%d/%m/%Y %H:%M'), strike, right, estimated_sell_price,
                stop_loss, round(probability, 3), round(expected_profit, 2), minutes_to_expiration,
            ])
