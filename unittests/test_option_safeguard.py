import unittest
import asyncio
from unittest.mock import Mock, patch, MagicMock
from ib_insync import Ticker, Trade, Option, Order, Position
from app.option_safeguard import OptionSafeguard

class TestOptionSafeguardBasic(unittest.IsolatedAsyncioTestCase):

    @patch('app.option_safeguard.ConnectionManager')
    @patch('app.option_safeguard.TradingBot')
    @patch('app.option_safeguard.MarketDataFetcher')
    @patch('app.option_safeguard.PositionsManager')
    async def test_watching_scenario(self, mock_pm, mock_mdf, mock_tb, mock_cm):
        # Setup mocks
        mock_ib = MagicMock()
        mock_cm.return_value.ib = mock_ib
        
        # Instantiate safeguard
        safeguard = OptionSafeguard()
        
        # Mock position
        option = Option(symbol='SPX', right='P', strike=5000, conId=123)
        ticker = MagicMock(spec=Ticker)
        ticker.last = 1.0  # Safe price
        option.ticker = ticker
        
        position = Position(contract=option, position=-1, avgCost=200, account='DU123')
        
        # Mock open trade (STP LMT)
        stop_loss_order = Order(orderType='STP LMT', action='BUY', totalQuantity=1, auxPrice=5.0, lmtPrice=5.15)
        stop_loss_trade = MagicMock(spec=Trade)
        stop_loss_trade.contract = option
        stop_loss_trade.order = stop_loss_order
        stop_loss_trade.remaining.return_value = 1
        
        # Setup return values for TradingBot
        mock_tb.return_value.get_short_options = Mock(return_value=asyncio.Future())
        mock_tb.return_value.get_short_options.return_value.set_result([position])
        
        mock_tb.return_value.get_open_trades = Mock(return_value=asyncio.Future())
        mock_tb.return_value.get_open_trades.return_value.set_result([stop_loss_trade])
        
        # Mock is_market_open to True
        with patch('app.option_safeguard.is_market_open', return_value=True):
            # Run the check
            await safeguard.guard_current_positions()
            
        # Verify no orders were modified or placed
        # (Since price 1.0 is far from stop 5.0, it should just log "Watching")
        mock_tb.return_value.modify_limit_order.assert_not_called()
        mock_tb.return_value.create_limit_order.assert_not_called()
        mock_tb.return_value.close_short_option_position.assert_not_called()

if __name__ == '__main__':
    unittest.main()
