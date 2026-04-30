import unittest
from unittest.mock import MagicMock, AsyncMock, patch
from app_async.strike_finder import StrikeFinder

class TestStrikeFinder(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        # Patch MarketDataFetcher singleton
        self.patcher = patch('app_async.strike_finder.MarketDataFetcher')
        self.mock_mkt_class = self.patcher.start()
        self.mock_mkt = self.mock_mkt_class.return_value
        self.mock_mkt.update_ticker_data = AsyncMock()
        
        self.strike_finder = StrikeFinder()

    def tearDown(self):
        self.patcher.stop()

    def create_mock_option(self, strike, right, delta=None, ask=None):
        option = MagicMock()
        option.strike = strike
        option.right = right
        option.conId = int(strike * 100) # Dummy conId
        
        ticker = MagicMock()
        ticker.contract = option
        # Mocking greeks for utilities.ib_utils.get_delta
        ticker.lastGreeks = MagicMock()
        ticker.lastGreeks.delta = delta
        ticker.modelGreeks = None
        
        ticker.ask = ask
        option.ticker = ticker
        return option

    async def test_get_low_delta_put_option_basic(self):
        # target delta 0.05
        # Strikes: 4000 (delta 0.08), 3900 (delta 0.04), 3800 (delta 0.02)
        options = [
            self.create_mock_option(4000, 'P', delta=-0.08),
            self.create_mock_option(3900, 'P', delta=-0.04),
            self.create_mock_option(3800, 'P', delta=-0.02)
        ]
        
        # In refactored StrikeFinder, we expect it to find the highest delta < target
        # 0.04 is the highest delta under 0.05
        result = await self.strike_finder.get_low_delta_put_option(options, 0.05)
        self.assertIsNotNone(result)
        self.assertEqual(result.strike, 3900)

    async def test_get_low_delta_call_option_basic(self):
        # target delta 0.05
        # Strikes: 5000 (delta 0.08), 5100 (delta 0.04), 5200 (delta 0.02)
        options = [
            self.create_mock_option(5000, 'C', delta=0.08),
            self.create_mock_option(5100, 'C', delta=0.04),
            self.create_mock_option(5200, 'C', delta=0.02)
        ]
        
        # 0.04 is the highest delta under 0.05
        result = await self.strike_finder.get_low_delta_call_option(options, 0.05)
        self.assertIsNotNone(result)
        self.assertEqual(result.strike, 5100)

    async def test_liquidity_skip(self):
        # target delta 0.10
        # option with strike ending in 5: 3905, delta 0.09. 
        # stricter_target = 0.10 * 0.875 = 0.0875
        # 0.09 > 0.0875 and 0.09 < 0.10 -> should skip
        options = [
            self.create_mock_option(3910, 'P', delta=-0.11),
            self.create_mock_option(3905, 'P', delta=-0.09),
            self.create_mock_option(3900, 'P', delta=-0.08)
        ]
        
        result = await self.strike_finder.get_low_delta_put_option(options, 0.10)
        # Should skip 3905 and pick 3900
        self.assertEqual(result.strike, 3900)

    async def test_get_available_cheap_call_option(self):
        # min_strike = 5000
        options = [
            self.create_mock_option(5000, 'C', ask=0.15),
            self.create_mock_option(5100, 'C', ask=0.10),
            self.create_mock_option(5200, 'C', ask=0.05),
            self.create_mock_option(5300, 'C', ask=0.05)
        ]
        
        # Should pick the lowest strike with ask <= 0.05
        result = await self.strike_finder.get_available_cheap_call_option(options, 5000)
        self.assertEqual(result.strike, 5200)

    async def test_get_available_cheap_put_option(self):
        # max_strike = 4000
        options = [
            self.create_mock_option(3900, 'P', ask=0.15),
            self.create_mock_option(3800, 'P', ask=0.10),
            self.create_mock_option(3700, 'P', ask=0.05),
            self.create_mock_option(3600, 'P', ask=0.05)
        ]
        
        # Should pick the highest strike with ask <= 0.05
        result = await self.strike_finder.get_available_cheap_put_option(options, 4000)
        self.assertEqual(result.strike, 3700)

if __name__ == '__main__':
    unittest.main()
