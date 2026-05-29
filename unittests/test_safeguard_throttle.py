import unittest
from unittest.mock import MagicMock, AsyncMock, patch
import asyncio
import time
from app.option_safeguard import OptionSafeguard
from ib_insync import Contract, Order, Trade

class TestOptionSafeguardThrottle(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # We need to mock the singletons before creating the instance
        with patch('app.option_safeguard.ConnectionManager'), \
             patch('app.option_safeguard.TradingBot'), \
             patch('app.option_safeguard.MaxLossCalculator'), \
             patch('app.option_safeguard.MarketDataFetcher'), \
             patch('app.option_safeguard.PositionsManager'):
            self.safeguard = OptionSafeguard()

    async def test_modify_limit_order_throttle(self):
        # Setup mock position and trade
        position = MagicMock()
        position.contract.conId = 123
        position.contract.ticker = None
        position.contract.right = 'C'
        position.avgCost = 100
        position.position = -1

        trade = MagicMock(spec=Trade)
        trade.order = MagicMock()
        trade.order.orderId = 456
        trade.order.lmtPrice = 1.0
        trade.contract = MagicMock()
        trade.contract.conId = 123

        # Mock dependencies
        self.safeguard.positions_manager.done_contract_ids = set()
        ticker = MagicMock()
        ticker.bid = 4.8
        ticker.last = 5.0  # High enough to be risky
        ticker.ask = 4.9
        self.safeguard.market_data_fetcher.get_ticker.return_value = ticker
        self.safeguard.max_loss_calculator.calculate_max_loss = MagicMock(return_value=1.0)
        
        with patch('app.option_safeguard.is_hollow', return_value=False), \
             patch('app.option_safeguard.find_high_limit_buy_trade', return_value=trade), \
             patch('app.option_safeguard.get_option_name', return_value="TEST OPTION"), \
             patch('app.option_safeguard.time.time') as mock_time:
            
            # First call at time 100
            mock_time.return_value = 100.0
            await self.safeguard.handle_current_risk(position, [trade])
            if not self.safeguard.trading_bot.modify_limit_order.called:
                 print("modify_limit_order was NOT called in first attempt")
            self.safeguard.trading_bot.modify_limit_order.assert_called_once()
            self.safeguard.trading_bot.modify_limit_order.reset_mock()

            # Second call at time 101 (less than 2 seconds later)
            mock_time.return_value = 101.0
            await self.safeguard.handle_current_risk(position, [trade])
            self.safeguard.trading_bot.modify_limit_order.assert_not_called()

            # Third call at time 103 (more than 2 seconds later)
            mock_time.return_value = 103.0
            await self.safeguard.handle_current_risk(position, [trade])
            self.safeguard.trading_bot.modify_limit_order.assert_called_once()

if __name__ == '__main__':
    unittest.main()
