import unittest
from unittest.mock import MagicMock, AsyncMock, patch
from app.strike_finder import StrikeFinder

class TestStrikeFinder(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        # Patch MarketDataFetcher singleton
        self.patcher = patch('app.strike_finder.MarketDataFetcher')
        self.mock_mkt_class = self.patcher.start()
        self.mock_mkt = self.mock_mkt_class.return_value
        self.mock_mkt.update_ticker_data = AsyncMock()
        
        self.strike_finder = StrikeFinder()

    def tearDown(self):
        self.patcher.stop()

    def create_mock_option(self, strike, right, delta=None, ask=None):
        option = MagicMock()
        option.strike = float(strike)
        option.right = right
        option.conId = int(strike * 100) # Dummy conId
        option.symbol = "SPX"
        
        ticker = MagicMock()
        ticker.contract = option
        # Mocking greeks for utilities.ib_utils.get_delta and get_delta_for_sell
        greeks = MagicMock()
        greeks.delta = delta
        ticker.lastGreeks = greeks
        ticker.askGreeks = greeks
        ticker.modelGreeks = None
        
        ticker.ask = ask
        option.ticker = ticker
        return option

    async def test_get_low_delta_put_option_initial_deltas_too_high(self):
        options = [self.create_mock_option(4000 - i*5, 'P', delta=-0.1 + (i*0.0005)) for i in range(200)]
        for i in range(50, 151):
            options[i].ticker.lastGreeks.delta = -0.05 # Higher than target 0.03
        
        # Ensure the fallback block has the target
        options[10].ticker.lastGreeks.delta = -0.02
        
        result = await self.strike_finder.get_low_delta_put_option(options, 0.03)
        self.assertIsNotNone(result)

    async def test_get_low_delta_put_option_initial_deltas_too_low(self):
        options = [self.create_mock_option(4000 - i*5, 'P', delta=-0.01) for i in range(200)]
        options[180].ticker.lastGreeks.delta = -0.04
        
        result = await self.strike_finder.get_low_delta_put_option(options, 0.05)
        self.assertIsNotNone(result)

    async def test_get_low_delta_put_liquidity_skip(self):
        # Target 0.10. 3905 (delta 0.09) is in [5, 15, ...] and > 0.10*0.875 (0.0875)
        options = [
            self.create_mock_option(3910, 'P', delta=-0.11),
            self.create_mock_option(3905, 'P', delta=-0.09),
            self.create_mock_option(3900, 'P', delta=-0.08)
        ]
        result = await self.strike_finder.get_low_delta_put_option(options, 0.10)
        self.assertEqual(result.strike, 3900)

    async def test_get_low_delta_call_option_initial_deltas_too_high(self):
        options = [self.create_mock_option(4000 + i*5, 'C', delta=0.1) for i in range(200)]
        options[10].ticker.lastGreeks.delta = 0.04 # strike 4050
        result = await self.strike_finder.get_low_delta_call_option(options, 0.05)
        self.assertIsNotNone(result)

    async def test_get_low_delta_call_option_initial_deltas_too_low(self):
        options = [self.create_mock_option(4000 + i*5, 'C', delta=0.01) for i in range(200)]
        options[180].ticker.lastGreeks.delta = 0.04
        result = await self.strike_finder.get_low_delta_call_option(options, 0.05)
        self.assertIsNotNone(result)

    async def test_get_low_delta_call_liquidity_skip(self):
        options = [
            self.create_mock_option(4100, 'C', delta=0.11),
            self.create_mock_option(4105, 'C', delta=0.09),
            self.create_mock_option(4110, 'C', delta=0.08)
        ]
        result = await self.strike_finder.get_low_delta_call_option(options, 0.10)
        self.assertEqual(result.strike, 4110)

    async def test_get_low_delta_put_no_data(self):
        options = [self.create_mock_option(4000, 'P', delta=None)]
        result = await self.strike_finder.get_low_delta_put_option(options, 0.05)
        self.assertIsNone(result)

    async def test_get_low_delta_call_no_data(self):
        options = [self.create_mock_option(4000, 'C', delta=None)]
        result = await self.strike_finder.get_low_delta_call_option(options, 0.05)
        self.assertIsNone(result)

    async def test_get_available_cheap_call_option_fetch_more(self):
        options = [self.create_mock_option(4000 + i*5, 'C', ask=0.1) for i in range(200)]
        options[150].ticker.ask = 0.05
        options[180].ticker.ask = 0.05
        result = await self.strike_finder.get_available_cheap_call_option(options, 4000)
        self.assertIsNotNone(result)

    async def test_get_available_cheap_put_option_fetch_more_lower(self):
        options = [self.create_mock_option(4000 - i*5, 'P', ask=0.1) for i in range(200)]
        options[50].ticker.ask = 0.1
        options[150].ticker.ask = 0.05
        options[10].ticker.ask = 0.05
        options[180].ticker.ask = 0.05
        result = await self.strike_finder.get_available_cheap_put_option(options, 4000)
        self.assertIsNotNone(result)

    async def test_get_available_cheap_put_option_lower_index_zero_edge(self):
        # Case where first_ask > 0.05 and lower_strike_index is already 0
        options = [self.create_mock_option(4000 - i*5, 'P', ask=0.1) for i in range(5)]
        # mid is 2. lower is max(2-50, 0) = 0.
        result = await self.strike_finder.get_available_cheap_put_option(options, 4100)
        self.assertIsNone(result)

    async def test_get_available_cheap_call_option_success(self):
        # available_cheap_option is None initially, then found in loop
        options = [self.create_mock_option(4000 + i*5, 'C', ask=0.1) for i in range(10)]
        options[5].ticker.ask = 0.05
        result = await self.strike_finder.get_available_cheap_call_option(options, 4000)
        self.assertEqual(result.strike, 4025.0)

    async def test_get_available_cheap_put_option_success(self):
        # available_cheap_option is None initially, then found in loop
        options = [self.create_mock_option(4000 - i*5, 'P', ask=0.1) for i in range(10)]
        options[5].ticker.ask = 0.05
        result = await self.strike_finder.get_available_cheap_put_option(options, 4100)
        self.assertEqual(result.strike, 3975.0)

    async def test_get_low_delta_put_option_candidate_no_delta_and_too_high(self):
        options = [self.create_mock_option(4000, 'P', delta=-0.04)]
        with patch('app.strike_finder.get_delta') as mock_gd:
            mock_gd.side_effect = [-0.04, -0.04, None]
            result = await self.strike_finder.get_low_delta_put_option(options, 0.05)
            self.assertIsNone(result)

    async def test_get_low_delta_option_no_candidate(self):
        # Trigger "No {right} option candidate found" (line 87)
        # All options have delta >= target_delta and none are selected.
        options = [self.create_mock_option(4000, 'P', delta=-0.06)]
        result = await self.strike_finder.get_low_delta_put_option(options, 0.05)
        self.assertIsNone(result)

    async def test_get_available_cheap_option_fetch_more_put_edge_cases(self):
        # Trigger line 142 (options_block = await self.fetch_options_block(0, l_idx - 1...))
        # Trigger line 145/146 (last_ask == 0.05 and h_idx + 1 < num_strikes)
        options = [self.create_mock_option(4000 - i*5, 'P', ask=0.1) for i in range(200)]
        # mid=100. l_idx=50, h_idx=150.
        # strikes[50]=3750, strikes[150]=3250.
        # first_ask = ask at strike 3750.
        options[50].ticker.ask = 0.06 # > 0.05
        # last_ask = ask at strike 3250.
        options[150].ticker.ask = 0.05
        
        # We also need a cheap option in the new blocks to avoid getting None if we want to check success,
        # but here we just want to hit the lines.
        options[0].ticker.ask = 0.05 # For the first block fetch (0 to 49)
        
        result = await self.strike_finder.get_available_cheap_put_option(options, 4100)
        self.assertIsNotNone(result)

    async def test_get_low_delta_call_option_block_end_edge(self):
        # higher_strike_index + 1 > number_of_strikes - 1
        options = [self.create_mock_option(4000 + i*5, 'C', delta=0.1) for i in range(10)]
        options[9].ticker.lastGreeks.delta = 0.04 # One option < target
        # This will make lowest_delta = 0.04.
        # So it doesn't enter the `if lowest_delta > target_delta` block.
        # Let's make ALL deltas in the block > target EXCEPT one that we fetch later? 
        # No, we want to hit the False branch of `if higher_strike_index + 1 <= number_of_strikes - 1`.
        # So we need all in block to be > target.
        for opt in options: opt.ticker.lastGreeks.delta = 0.1
        # Now lowest_delta = 0.1 > 0.05. Enters 145.
        # current_candidate = options[9] (delta 0.1).
        # higher=9, count=10. 10 <= 9 is False. Skips fetch.
        # To avoid AssertionError at end, we need the loop at 170 to find a better one.
        # But we didn't fetch more, so options_block is still options[0:10].
        # If we make one of THEM have a delta < 0.05 in the second loop:
        with patch('app.strike_finder.get_delta') as mock_gd:
            # 10 calls for first loop, then it uses the same options_block.
            # 10 calls for second loop.
            mock_gd.side_effect = [0.1]*10 + [0.04]*10 + [0.04]
            result = await self.strike_finder.get_low_delta_call_option(options, 0.05)
            self.assertIsNotNone(result)
            self.assertEqual(result.strike, 4000.0)

    async def test_get_available_cheap_put_option_first_ask_low(self):
        options = [self.create_mock_option(4000 - i*5, 'P', ask=0.05) for i in range(10)]
        result = await self.strike_finder.get_available_cheap_put_option(options, 4100)
        self.assertIsNotNone(result)

    async def test_get_low_delta_call_option_loop_no_break(self):
        options = [self.create_mock_option(4000 + i*5, 'C', delta=0.01) for i in range(200)]
        # mid=100. lower=50. higher=150.
        # To enter 155, highest_delta < 0.05 and lower > 0.
        with patch('app.strike_finder.get_delta') as mock_gd:
            # First loop: 101 calls. Let's say all 0.01.
            # Second loop: 101 calls. Let's say all None.
            # Third loop (after fetch 0-49): 50 calls. Let's say some 0.04.
            mock_gd.side_effect = [0.01]*101 + [None]*101 + [0.04]*50 + [0.04]
            result = await self.strike_finder.get_low_delta_call_option(options, 0.05)
            self.assertIsNotNone(result)

if __name__ == '__main__':
    unittest.main()
