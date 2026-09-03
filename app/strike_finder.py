import logging
import math
from utilities.utils import get_option_name
from utilities.ib_utils import (extract_ask, get_delta, get_delta_for_sell, get_individual_deltas, get_model_gamma,
                                 get_model_vega, get_model_theta, get_minutes_to_expiration, get_distance_to_strike_pct)
from .market_data_fetcher import MarketDataFetcher
from .max_loss_calculator import MaxLossCalculator
from .predictor import Predictor
from .price_estimator import PriceEstimator


logger = logging.getLogger(__name__)

OPTIONS_BLOCK_SIZE = 100
OPTIONS_BLOCK_LOWER_PART_SIZE = OPTIONS_BLOCK_SIZE // 2
OPTIONS_BLOCK_HIGHER_PART_SIZE = OPTIONS_BLOCK_SIZE - OPTIONS_BLOCK_LOWER_PART_SIZE

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
            self.middle_fetched_block = {'C': [], 'P': []}
            self.edge_fetched_block = {'C': [], 'P': []}
            self._initialized = True

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
            self.edge_fetched_block[right] = options_block

        elif highest_delta < target_delta:
            logger.info(f"Initial block deltas too low for {right}. Highest: {highest_delta:.3f}")
            logger.info(f"Fetching {right} option block: {strikes[higher_idx]} -> {strikes[number_of_strikes - 1]}")
            options_block = await self.fetch_options_block(higher_idx, number_of_strikes - 1, strike_to_option, strikes)
            self.edge_fetched_block[right] = options_block

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

    def _log_best_expected_profit_option(self, target_delta, right):
        stop_loss_per_option = self.max_loss_calculator.calculate_max_loss(right)
        atm_iv = self.market_data_fetcher.get_cached_spx_implied_volatility(right)

        best_option, best_expected_profit, best_probability, best_max_delta, best_estimated_sell_price = \
            None, None, None, None, None

        for option in self.middle_fetched_block[right]:
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
            except (TypeError, ValueError) as e:
                logger.warning(f"Skipping {get_option_name(option)} in expected-profit scan: {e}")
                continue

            out_of_the_money_probability = self.predictor.predict_out_of_the_money_probability(
                option, right, target_delta, estimated_sell_price, stop_loss_per_option,
                bid_delta, ask_delta, last_delta, model_delta, gamma, vega, theta,
                minutes_to_expiration, atm_iv, distance_to_strike_pct,
            )
            if out_of_the_money_probability is None:
                continue

            in_the_money_probability = 1 - out_of_the_money_probability
            expected_profit = (estimated_sell_price * out_of_the_money_probability
                                - stop_loss_per_option * in_the_money_probability)

            if best_expected_profit is None or expected_profit > best_expected_profit:
                delta_values = [d for d in (bid_delta, ask_delta, last_delta, model_delta) if d is not None]
                best_option = option
                best_expected_profit = expected_profit
                best_probability = out_of_the_money_probability
                best_max_delta = max(delta_values) if delta_values else None
                best_estimated_sell_price = estimated_sell_price

        if best_option is None:
            return

        max_delta_str = f"{best_max_delta:.3f}" if best_max_delta is not None else "n/a"
        logger.info(f"Best expected-profit {right} option in the scanned block: {get_option_name(best_option)}, "
                    f"expected profit: {best_expected_profit:.3f}, estimated sell price: {best_estimated_sell_price:.2f}, "
                    f"stop loss: {stop_loss_per_option:.2f}, probability: {best_probability:.3f}, "
                    f"max delta: {max_delta_str}, target delta: {target_delta:.3f}")

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

    async def _get_low_delta_option(self, options, target_delta, right):
        assert options
        self.middle_fetched_block[right] = []
        self.edge_fetched_block[right] = []

        strike_to_option = {option.strike: option for option in options}
        strikes = sorted(strike_to_option.keys(), reverse=(right == 'C'))
        
        number_of_strikes = len(strikes)
        middle_idx = number_of_strikes // 2
        lower_idx = max(middle_idx - OPTIONS_BLOCK_LOWER_PART_SIZE, 0)
        higher_idx = min(middle_idx + OPTIONS_BLOCK_HIGHER_PART_SIZE, number_of_strikes - 1)

        logger.info(f"Fetching {right} option block: {strikes[lower_idx]} -> {strikes[higher_idx]}")
        options_block = await self.fetch_options_block(lower_idx, higher_idx, strike_to_option, strikes)
        self.middle_fetched_block[right] = options_block

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

    def find_first_cheap_option(self, right):
        """
        Traverses middle and then edge fetched blocks to find the first option with ask 0.05.
        Calls: Traverses lower strikes to higher.
        Puts: Traverses higher strikes to lower.
        """
        blocks_to_check = [
            self.middle_fetched_block.get(right, []),
            self.edge_fetched_block.get(right, [])
        ]

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
        self.middle_fetched_block[right] = []
        self.edge_fetched_block[right] = []
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

        self.middle_fetched_block[right] = options_block
        available_cheap = None
        
        # Check if we need to fetch more blocks based on liquidity/price
        if right == 'C':
            logger.info(f"barak: the ask value of the first option is: {extract_ask(options_block[0].ticker)}, {get_option_name(options_block[-1])}")
            if extract_ask(options_block[0].ticker) == 0.05 and h_idx + 1 < num_strikes:
                available_cheap = options_block[-1]
                logger.info(f"Fetching {right} option block (edge): {relevant_strikes[h_idx+1]} -> {relevant_strikes[num_strikes-1]}, indices: {h_idx+1} -> {num_strikes-1}")
                options_block = await self.fetch_options_block(h_idx + 1, num_strikes - 1, strike_to_option, relevant_strikes)
                self.edge_fetched_block[right] = options_block
        else:
            if extract_ask(options_block[0].ticker) > 0.05:
                if l_idx > 0:
                    logger.info(
                        f"Fetching {right} option block (edge): {relevant_strikes[0]} -> {relevant_strikes[l_idx-1]}, indices: 0 -> {l_idx-1}")
                    options_block = await self.fetch_options_block(0, l_idx - 1, strike_to_option, relevant_strikes)
                    self.edge_fetched_block[right] = options_block
            
            if options_block and extract_ask(options_block[-1].ticker) == 0.05 and h_idx + 1 < num_strikes:
                available_cheap = options_block[-1]
                logger.info(f"Fetching {right} option block (edge): {relevant_strikes[h_idx+1]} -> {relevant_strikes[num_strikes-1]}, indices: {h_idx+1} -> {num_strikes-1}")
                options_block = await self.fetch_options_block(h_idx + 1, num_strikes - 1, strike_to_option, relevant_strikes)
                self.edge_fetched_block[right] = options_block

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
            strike_to_option = {}
            if self.edge_fetched_block[right]:
                strike_to_option = {option.strike: option for option in self.edge_fetched_block[right]}
            if self.middle_fetched_block[right]:
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