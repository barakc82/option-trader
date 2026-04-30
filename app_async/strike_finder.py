import logging
import math
from utilities.utils import get_option_name
from utilities.ib_utils import extract_ask, get_delta
from .market_data_fetcher import MarketDataFetcher

logger = logging.getLogger(__name__)

OPTIONS_BLOCK_SIZE = 100
OPTIONS_BLOCK_LOWER_PART_SIZE = OPTIONS_BLOCK_SIZE // 2
OPTIONS_BLOCK_HIGHER_PART_SIZE = OPTIONS_BLOCK_SIZE - OPTIONS_BLOCK_LOWER_PART_SIZE

class StrikeFinder:
    def __init__(self):
        self.market_data_fetcher = MarketDataFetcher()

    async def get_low_delta_put_option(self, put_options, target_delta):
        return await self._get_low_delta_option(put_options, target_delta, 'P')

    async def get_low_delta_call_option(self, call_options, target_delta):
        return await self._get_low_delta_option(call_options, target_delta, 'C')

    async def _get_low_delta_option(self, options, target_delta, right):
        assert options
        strike_to_option = {option.strike: option for option in options}
        strikes = sorted(strike_to_option.keys(), reverse=(right == 'C'))
        
        number_of_strikes = len(strikes)
        middle_idx = number_of_strikes // 2
        lower_idx = max(middle_idx - OPTIONS_BLOCK_LOWER_PART_SIZE, 0)
        higher_idx = min(middle_idx + OPTIONS_BLOCK_HIGHER_PART_SIZE, number_of_strikes - 1)

        logger.info(f"Fetching {right} option block: {strikes[lower_idx]} -> {strikes[higher_idx]}")
        options_block = await self.fetch_options_block(lower_idx, higher_idx, strike_to_option, strikes)

        lowest_delta, highest_delta = 1.0, 0.0
        highest_delta_option = None

        for option in options_block:
            delta = get_delta(option.ticker)
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
                options_block = await self.fetch_options_block(0, lower_idx - 1, strike_to_option, strikes)
            else:
                if higher_idx + 1 < number_of_strikes:
                    options_block = await self.fetch_options_block(higher_idx + 1, number_of_strikes - 1, strike_to_option, strikes)
        
        elif highest_delta < target_delta:
            current_candidate = highest_delta_option
            logger.info(f"Initial block deltas too low for {right}. Highest: {highest_delta:.3f}")
            if right == 'P':
                options_block = await self.fetch_options_block(higher_idx, number_of_strikes - 1, strike_to_option, strikes)
            else:
                if lower_idx > 0:
                    options_block = await self.fetch_options_block(0, lower_idx - 1, strike_to_option, strikes)

        highest_delta_under_target = 0
        for option in options_block:
            delta = get_delta(option.ticker)
            if delta is None or math.isnan(delta): continue
            delta = abs(delta)

            # Liquidity check for specific strikes
            if option.strike % 100 in [5, 15, 35, 45, 55, 65, 85, 95]:
                if (target_delta * 0.875) < delta < target_delta:
                    continue

            if highest_delta_under_target < delta < target_delta:
                highest_delta_under_target = delta
                current_candidate = option

        if not current_candidate:
            logger.error(f"No {right} option candidate found")
            return None

        final_delta = abs(get_delta(current_candidate.ticker))
        if final_delta > target_delta:
            logger.error(f"Selected {right} option {get_option_name(current_candidate)} delta {final_delta:.3f} > target {target_delta:.3f}")
            return None

        logger.info(f"Selected {right} option: {get_option_name(current_candidate)}, delta: {final_delta:.3f}, target: {target_delta:.3f}")
        return current_candidate

    async def fetch_options_block(self, lower_idx, higher_idx, strike_to_option, strikes):
        if lower_idx > higher_idx: return []
        options_block = [strike_to_option[strikes[i]] for i in range(lower_idx, higher_idx + 1)]
        await self.market_data_fetcher.update_ticker_data(options_block)
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
        
        options_block = await self.fetch_options_block(l_idx, h_idx, strike_to_option, relevant_strikes)
        if not options_block: return None

        available_cheap = None
        
        # Check if we need to fetch more blocks based on liquidity/price
        if right == 'C':
            if extract_ask(options_block[-1].ticker) == 0.05 and h_idx + 1 < num_strikes:
                available_cheap = options_block[-1]
                options_block = await self.fetch_options_block(h_idx + 1, num_strikes - 1, strike_to_option, relevant_strikes)
        else:
            if extract_ask(options_block[0].ticker) > 0.05 and l_idx > 0:
                options_block = await self.fetch_options_block(0, l_idx - 1, strike_to_option, relevant_strikes)
            if options_block and extract_ask(options_block[-1].ticker) == 0.05 and h_idx + 1 < num_strikes:
                available_cheap = options_block[-1]
                options_block = await self.fetch_options_block(h_idx + 1, num_strikes - 1, strike_to_option, relevant_strikes)

        for option in options_block:
            ask = extract_ask(option.ticker)
            if ask is None or ask > 0.05: continue
            if right == 'C':
                if available_cheap is None or option.strike < available_cheap.strike:
                    available_cheap = option
            else:
                if available_cheap is None or option.strike > available_cheap.strike:
                    available_cheap = option

        return available_cheap
