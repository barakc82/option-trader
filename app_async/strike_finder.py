import logging

from utilities.utils import get_option_name
from utilities.ib_utils import extract_ask, get_delta

from .market_data_fetcher import MarketDataFetcher


logger = logging.getLogger(__name__)
NUMBER_OF_CONTRACTS_PER_REQUEST = 5
OPTIONS_BLOCK_SIZE = 100
OPTIONS_BLOCK_LOWER_PART_SIZE = OPTIONS_BLOCK_SIZE // 2
OPTIONS_BLOCK_HIGHER_PART_SIZE = OPTIONS_BLOCK_SIZE - OPTIONS_BLOCK_LOWER_PART_SIZE


class StrikeFinder:

    def __init__(self):
        self.market_data_fetcher = MarketDataFetcher()

    async def get_low_delta_put_option(self, put_options, target_delta):

        assert put_options
        strike_to_option = {}
        for option in put_options:
            strike_to_option[option.strike] = option
        strikes = [option.strike for option in put_options]
        number_of_strikes = len(strikes)
        middle_strike_index = number_of_strikes // 2

        lower_strike_index = max(middle_strike_index - OPTIONS_BLOCK_LOWER_PART_SIZE, 0)
        higher_strike_index = min(middle_strike_index + OPTIONS_BLOCK_HIGHER_PART_SIZE, len(strikes) - 1)

        logger.info(f"Fetching put option block: {strikes[lower_strike_index]} -> {strikes[higher_strike_index]}")
        options_block = await self.fetch_options_block(lower_strike_index, higher_strike_index, strike_to_option, strikes)
        logger.info(f"Done fetching put option block: {strikes[lower_strike_index]} -> {strikes[higher_strike_index]}")

        lowest_delta = 1
        highest_delta = 0
        highest_delta_option = None

        for option in options_block:
            current_delta = get_delta(option.ticker)
            if current_delta is None:
                continue
            if current_delta < lowest_delta:
                lowest_delta = current_delta
            if current_delta > highest_delta:
                highest_delta = current_delta
                highest_delta_option = option
        if lowest_delta > highest_delta:
            logger.error("No delta data was available for put options")
            return None

        current_candidate_option = None
        if lowest_delta > target_delta:
            current_candidate_option = options_block[0]
            logger.info(f"The deltas in the initial option block are higher than the target delta, lowest delta is "
                        f"{lowest_delta:.3f}, block indices are: {lower_strike_index} and {higher_strike_index}, "
                        f"block strikes are: {strikes[lower_strike_index]} and {strikes[higher_strike_index]}")
            options_block = await self.fetch_options_block(0, lower_strike_index - 1, strike_to_option, strikes)
        if highest_delta < target_delta:
            current_candidate_option = highest_delta_option
            logger.info(f"The deltas in the initial option block are lower than the target delta, highest delta is "
                        f"{highest_delta:.3f}, block indices are: {lower_strike_index} and {higher_strike_index}, "
                        f"block strikes are: {strikes[lower_strike_index]} and {strikes[higher_strike_index]}")
            options_block = await self.fetch_options_block(higher_strike_index, number_of_strikes - 1, strike_to_option,
                                                     strikes)

        highest_delta_under_target = 0
        for option in options_block:
            current_delta = get_delta(option.ticker)
            if current_delta is None:
                continue

            strike_suffix = option.strike % 100
            if strike_suffix in [5, 15, 35, 45, 55, 65, 85, 95]:
                stricter_target_delta = target_delta * 0.875
                if stricter_target_delta < current_delta < target_delta:
                    logger.info(
                        f"{get_option_name(option)} is expected to have low liquidity, and since its delta ({current_delta:.3f}) is higher than the stricter target delta ({stricter_target_delta:.3f}), "
                        f"skipping this option")
                    continue

            if highest_delta_under_target < current_delta < target_delta:
                highest_delta_under_target = current_delta
                current_candidate_option = option

        if not current_candidate_option:
            logger.error("No put option candidate was found")
            return None

        signed_candidate_delta = get_delta(current_candidate_option.ticker)
        if not signed_candidate_delta:
            logger.error("No delta data was available for the candidate put option")
            return None

        candidate_delta = abs(signed_candidate_delta)
        if candidate_delta > target_delta:
            logger.error(
                f"Option candidate with higher delta than target: {get_option_name(current_candidate_option)}, delta is {candidate_delta}")
            return None

        assert candidate_delta < target_delta
        logger.info(
            f"Selected option: {get_option_name(current_candidate_option)}, option delta: {candidate_delta}, target delta: {target_delta}")
        return current_candidate_option

    async def fetch_options_block(self, lower_strike_index, higher_strike_index, strike_to_option, strikes):
        assert lower_strike_index <= higher_strike_index

        options_block = []
        for strike_index in range(lower_strike_index, higher_strike_index + 1):
            strike = strikes[strike_index]
            option = strike_to_option[strike]
            options_block.append(option)
        logger.info(f"Fetching {len(options_block)} tickers for options block")
        await self.market_data_fetcher.update_ticker_data(options_block)
        logger.info(f"Done fetching {len(options_block)} tickers for options block")
        return options_block

    async def get_low_delta_call_option(self, call_options, target_delta):

        strike_to_option = {}
        for option in call_options:
            assert option.conId
            strike_to_option[option.strike] = option
        strikes = [option.strike for option in call_options]
        number_of_strikes = len(strikes)
        middle_strike_index = number_of_strikes // 2

        lower_strike_index = max(middle_strike_index - OPTIONS_BLOCK_LOWER_PART_SIZE, 0)
        higher_strike_index = min(middle_strike_index + OPTIONS_BLOCK_HIGHER_PART_SIZE, number_of_strikes - 1)

        logger.info(f"Fetching call option block: {strikes[lower_strike_index]} -> {strikes[higher_strike_index]}")
        options_block = await self.fetch_options_block(lower_strike_index, higher_strike_index, strike_to_option, strikes)

        lowest_delta = 1
        highest_delta = 0
        for option in options_block:
            current_delta = get_delta(option.ticker)
            if current_delta is None:
                continue
            if current_delta < lowest_delta:
                lowest_delta = current_delta
            if current_delta > highest_delta:
                highest_delta = current_delta
        if lowest_delta > highest_delta:
            logger.error("No delta data was available for call options")
            return None

        current_candidate_option = None
        if lowest_delta > target_delta:
            current_candidate_option = options_block[-1]
            logger.info(f"The deltas in the initial option block are higher than the target delta, lowest delta is "
                        f"{lowest_delta:.3f}, block indices are: {lower_strike_index} and {higher_strike_index}, "
                        f"block strikes are: {strikes[lower_strike_index]} and {strikes[higher_strike_index]}")
            if higher_strike_index + 1 <= number_of_strikes - 1:
                options_block = await self.fetch_options_block(higher_strike_index + 1, number_of_strikes - 1,
                                                         strike_to_option,
                                                         strikes)

        if highest_delta < target_delta and lower_strike_index > 0:
            logger.info(f"The deltas in the initial option block are lower than the target delta, highest delta is "
                        f"{highest_delta:.3f}, block indices are: {lower_strike_index} and {higher_strike_index}, "
                        f"block strikes are: {strikes[lower_strike_index]} and {strikes[higher_strike_index]}")
            for option in options_block:
                current_candidate_option = option
                current_delta = get_delta(option.ticker)
                if current_delta is not None:
                    break

            options_block = await self.fetch_options_block(0, lower_strike_index - 1, strike_to_option, strikes)
            logger.info(f"Done fetching options block, indices are: {lower_strike_index} and {higher_strike_index}, "
                        f"block strikes are: {strikes[lower_strike_index]} and {strikes[higher_strike_index]}")

        highest_delta_under_target = 0

        for option in options_block:
            current_delta = get_delta(option.ticker)
            if current_delta is None:
                continue

            strike_suffix = option.strike % 100
            if strike_suffix in [5, 15, 35, 45, 55, 65, 85, 95]:
                stricter_target_delta = target_delta * 0.875
                if stricter_target_delta < current_delta < target_delta:
                    logger.info(
                        f"{get_option_name(option)} is expected to have low liquidity, and since its delta ({current_delta:.3f}) is higher than the stricter target delta ({stricter_target_delta:.3f}), "
                        f"skipping this option")
                    continue

            if highest_delta_under_target < current_delta < target_delta:
                highest_delta_under_target = current_delta
                current_candidate_option = option

        if current_candidate_option is None:
            logger.error("No call option candidate was found")
            return None

        current_candidate_delta = get_delta(current_candidate_option.ticker)
        if current_candidate_delta is None:
            logger.error("No delta data was available for call options")
            return None
        assert current_candidate_delta < target_delta
        logger.info(
            f"Selected option: {get_option_name(current_candidate_option)}, option delta: {current_candidate_delta}, target delta: {target_delta}")
        return current_candidate_option

    async def get_available_cheap_call_option(self, call_options, min_strike):
        strike_to_option = {}
        for option in call_options:
            assert option.conId
            strike_to_option[option.strike] = option
        strikes = [option.strike for option in call_options if option.strike > min_strike]
        number_of_strikes = len(strikes)
        middle_strike_index = number_of_strikes // 2

        lower_strike_index = max(middle_strike_index - OPTIONS_BLOCK_LOWER_PART_SIZE, 0)
        higher_strike_index = min(middle_strike_index + OPTIONS_BLOCK_HIGHER_PART_SIZE, number_of_strikes - 1)
        options_block = await self.fetch_options_block(lower_strike_index, higher_strike_index, strike_to_option, strikes)
        last_option = options_block[-1]

        available_cheap_option = None
        last_ask = extract_ask(last_option.ticker)
        if last_ask == 0.05 and higher_strike_index + 1 <= number_of_strikes - 1:
            available_cheap_option = last_option
            options_block = await self.fetch_options_block(higher_strike_index, number_of_strikes - 1,
                                                     strike_to_option,
                                                     strikes)

        for option in options_block:
            current_ask = extract_ask(option.ticker)
            if current_ask is None or current_ask > 0.05:
                continue
            if available_cheap_option is None or option.strike < available_cheap_option.strike:
                available_cheap_option = option

        assert available_cheap_option
        return available_cheap_option

    async def get_available_cheap_put_option(self, put_options, max_strike):
        strike_to_option = {}
        for option in put_options:
            assert option.conId
            strike_to_option[option.strike] = option
        strikes = [option.strike for option in put_options if option.strike < max_strike]
        number_of_strikes = len(strikes)
        middle_strike_index = number_of_strikes // 2

        lower_strike_index = max(middle_strike_index - OPTIONS_BLOCK_LOWER_PART_SIZE, 0)
        higher_strike_index = min(middle_strike_index + OPTIONS_BLOCK_HIGHER_PART_SIZE, number_of_strikes - 1)
        logger.info(f"Fetching put option block: {strikes[lower_strike_index]} -> {strikes[higher_strike_index]}")
        options_block = await self.fetch_options_block(lower_strike_index, higher_strike_index, strike_to_option, strikes)
        first_option = options_block[0]
        last_option = options_block[-1]

        available_cheap_option = None

        first_ask = extract_ask(first_option.ticker)
        if first_ask > 0.05:
            logger.info(f"The ask value of the first option in the block ({first_ask}) greater than 0.05, "
                        f"fetching a new block: {strikes[0]} -> {strikes[lower_strike_index - 1]}")
            options_block = await self.fetch_options_block(0, lower_strike_index - 1, strike_to_option, strikes)

        last_ask = extract_ask(last_option.ticker)
        if last_ask == 0.05:
            logger.info(f"The ask value of the last option in the block is 0.05, fetching a new block")
            available_cheap_option = last_option
            logger.info(f"Fetching put option block: {strikes[higher_strike_index]} -> {strikes[number_of_strikes - 1]}")
            options_block = await self.fetch_options_block(higher_strike_index, number_of_strikes - 1,
                                                     strike_to_option,
                                                     strikes)


        for option in options_block:
            current_ask = extract_ask(option.ticker)
            if current_ask is None or current_ask > 0.05:
                continue
            if available_cheap_option is None or option.strike > available_cheap_option.strike:
                available_cheap_option = option

        assert available_cheap_option
        return available_cheap_option
