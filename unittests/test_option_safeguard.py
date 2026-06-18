import unittest
from unittest.mock import MagicMock, AsyncMock, patch
import math
import time
from app.option_safeguard import OptionSafeguard
from ib_insync import Contract, Ticker

class TestOptionSafeguard(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Reset OptionSafeguard singleton
        OptionSafeguard._instance = None
        
        # Patch dependencies that are initialized in __init__
        self.patch_cm = patch('app.option_safeguard.ConnectionManager')
        self.patch_tb = patch('app.option_safeguard.TradingBot')
        self.patch_mdf = patch('app.option_safeguard.MarketDataFetcher')
        self.patch_pm = patch('app.option_safeguard.PositionsManager')
        self.patch_sm = patch('app.option_safeguard.SubscriptionManager')
        self.patch_mlc = patch('app.option_safeguard.MaxLossCalculator')

        self.mock_cm = self.patch_cm.start()
        self.mock_tb = self.patch_tb.start()
        self.mock_mdf = self.patch_mdf.start()
        self.mock_pm = self.patch_pm.start()
        self.mock_sm = self.patch_sm.start()
        self.mock_mlc = self.patch_mlc.start()

        self.safeguard = OptionSafeguard()

    def tearDown(self):
        self.patch_cm.stop()
        self.patch_tb.stop()
        self.patch_mdf.stop()
        self.patch_pm.stop()
        self.patch_sm.stop()
        self.patch_mlc.stop()

    async def test_ensure_ticker_returns_error_if_none(self):
        option = MagicMock()
        option.ticker = None
        result = await self.safeguard._ensure_ticker(option)
        self.assertEqual(result, 1) # ERROR is 1

    async def test_ensure_ticker_returns_error_if_hollow(self):
        option = MagicMock()
        ticker = MagicMock()
        # is_hollow returns True if last, bid, and ask are NaN
        ticker.last = math.nan
        ticker.bid = math.nan
        ticker.ask = math.nan
        option.ticker = ticker
        
        with patch('app.option_safeguard.is_hollow', return_value=True):
            result = await self.safeguard._ensure_ticker(option)
        self.assertEqual(result, 1)

    async def test_ensure_ticker_success(self):
        option = MagicMock()
        ticker = MagicMock()
        ticker.last = 1.0
        ticker.bid = 0.9
        ticker.ask = 1.1
        option.ticker = ticker
        
        with patch('app.option_safeguard.is_hollow', return_value=False):
            result = await self.safeguard._ensure_ticker(option)
        self.assertEqual(result, 0) # SUCCESS is 0

    @patch('app.option_safeguard.is_regular_hours', return_value=True)
    async def test_handle_current_risk_closes_position_on_stop_loss(self, mock_hours):
        # Setup position
        contract = Contract(conId=123, right='P')
        position = MagicMock()
        position.contract = contract
        position.avgCost = 1.0 * 100 # avgCost is usually total cost, so /100 is price
        
        # Setup ticker
        ticker = MagicMock(spec=Ticker)
        ticker.bid = 2.0
        ticker.ask = 2.2
        ticker.last = math.nan
        contract.ticker = ticker
        
        # Setup MaxLossCalculator
        self.mock_mlc.return_value.calculate_max_loss.return_value = 0.5
        # Stop loss = 1.0 + 0.5 = 1.5. Current price = 2.1. Should close.
        
        self.safeguard.positions_manager.done_contract_ids = set()
        
        # Mock spy_option to None to avoid is_unfair_ask_value for this test
        self.mock_sm.return_value.spx_to_spy_map.get.return_value = None

        with patch.object(self.safeguard, '_ensure_ticker', return_value=0):
            with patch.object(self.safeguard, 'calculate_current_price', return_value=2.1):
                await self.safeguard.handle_current_risk(position, [])
        
        # Verify close_short_option_position was called
        self.safeguard.trading_bot.close_short_option_position.assert_called_once()

    @patch('app.option_safeguard.is_regular_hours', return_value=True)
    @patch('app.option_safeguard.get_spy_option_name', return_value="SPY P 100")
    def test_is_unfair_ask_value_detects_deviation(self, mock_name, mock_hours):
        option = MagicMock()
        option.conId = 123
        option.ticker.ask = 10.0
        
        spy_option = MagicMock()
        spy_ticker = MagicMock()
        spy_ticker.ask = 0.8 # Adjusted = 8.0. Deviation = (10-8)/8 = 0.25 > 0.1
        
        self.mock_mdf.return_value.get_ticker.return_value = spy_ticker
        
        result = self.safeguard.is_unfair_ask_value(option, spy_option)
        self.assertTrue(result)

    @patch('app.option_safeguard.is_regular_hours', return_value=True)
    @patch('app.option_safeguard.get_spy_option_name', return_value="SPY P 100")
    def test_is_unfair_ask_value_accepts_fair_price(self, mock_name, mock_hours):
        option = MagicMock()
        option.conId = 123
        option.ticker.ask = 10.0
        
        spy_option = MagicMock()
        spy_ticker = MagicMock()
        spy_ticker.ask = 0.95 # Adjusted = 9.5. Deviation = 0.5/9.5 < 0.1
        
        self.mock_mdf.return_value.get_ticker.return_value = spy_ticker
        
        result = self.safeguard.is_unfair_ask_value(option, spy_option)
        self.assertFalse(result)

    async def test_handle_high_limit_buy_trade_throttling(self):
        # Setup position
        contract = Contract(conId=123, right='P')
        position = MagicMock()
        position.contract = contract
        
        # Setup high_limit_buy_trade
        trade = MagicMock()
        trade.order.orderId = 999
        
        # Set modification time within 2 seconds
        now = time.time()
        self.safeguard.last_modification_times[999] = now
        
        # Mock logger.info to check if it gets called
        with patch('app.option_safeguard.logger') as mock_logger:
            # First call: should log because last_skipping_log_times has no entry
            await self.safeguard.handle_high_limit_buy_trade(trade, position, 0.5)
            self.assertEqual(mock_logger.info.call_count, 1)
            
            # Second call immediately after: should not log (throttled)
            await self.safeguard.handle_high_limit_buy_trade(trade, position, 0.5)
            self.assertEqual(mock_logger.info.call_count, 1) # Still 1
            
            # Set skipping log time to more than 10 seconds ago
            self.safeguard.last_skipping_log_times[123] = now - 11
            await self.safeguard.handle_high_limit_buy_trade(trade, position, 0.5)
            self.assertEqual(mock_logger.info.call_count, 2) # Should log now

if __name__ == '__main__':
    unittest.main()
