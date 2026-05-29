import unittest
from unittest.mock import MagicMock, AsyncMock, patch
import asyncio
from app.trading_bot import TradingBot
from ib_insync import Contract, Order, Trade

class TestTradingBot(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Reset TradingBot singleton
        TradingBot._instance = None

    @patch('app.trading_bot.ConnectionManager')
    @patch('app.trading_bot.MarketDataFetcher')
    @patch('app.trading_bot.AccountData')
    async def test_modify_limit_order_skips_if_same_price(self, mock_account, mock_mdf, mock_cm):
        # Setup mocks
        mock_ib = MagicMock()
        mock_cm.return_value.ib = mock_ib
        
        bot = TradingBot()
        
        # Mock adjust_limit_to_market_rules to return the same price
        bot.adjust_limit_to_market_rules = AsyncMock(return_value=1.5)
        
        contract = Contract(conId=123)
        order = Order(lmtPrice=1.5)
        trade = MagicMock(spec=Trade)
        trade.contract = contract
        trade.order = order
        
        with patch('app.trading_bot.logger') as mock_logger:
            with patch('app.trading_bot.get_option_name', return_value="TEST OPTION"):
                result = await bot.modify_limit_order(trade, 1.5)
        
        # Check that placeOrder was NOT called
        mock_ib.placeOrder.assert_not_called()
        # Check that logger.info was called
        mock_logger.info.assert_called()
        log_message = mock_logger.info.call_args[0][0]
        self.assertIn("Skipping modification", log_message)
        self.assertIn("1.5", log_message)
        self.assertEqual(result, trade)

    @patch('app.trading_bot.ConnectionManager')
    @patch('app.trading_bot.MarketDataFetcher')
    @patch('app.trading_bot.AccountData')
    async def test_modify_limit_order_updates_if_different_price(self, mock_account, mock_mdf, mock_cm):
        # Setup mocks
        mock_ib = MagicMock()
        mock_cm.return_value.ib = mock_ib
        
        bot = TradingBot()
        
        # Mock adjust_limit_to_market_rules to return a different price
        bot.adjust_limit_to_market_rules = AsyncMock(return_value=1.6)
        
        contract = Contract(conId=123)
        order = Order(lmtPrice=1.5)
        trade = MagicMock(spec=Trade)
        trade.contract = contract
        trade.order = order
        
        mock_ib.placeOrder.return_value = "NEW_TRADE"
        
        result = await bot.modify_limit_order(trade, 1.6)
        
        # Check that placeOrder WAS called
        mock_ib.placeOrder.assert_called_once_with(contract, order)
        self.assertEqual(order.lmtPrice, 1.6)
        self.assertEqual(result, "NEW_TRADE")

if __name__ == '__main__':
    unittest.main()
