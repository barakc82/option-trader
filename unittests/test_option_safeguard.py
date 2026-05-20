import time
import unittest
import asyncio
from datetime import timedelta, datetime
from unittest.mock import Mock, patch, MagicMock
from ib_insync import Ticker, Trade, Option, Order, Position, TradeLogEntry
from app.option_safeguard import OptionSafeguard

class TestOptionSafeguardBasic(unittest.IsolatedAsyncioTestCase):

    @patch('app.option_safeguard.ConnectionManager')
    @patch('app.option_safeguard.TradingBot')
    @patch('app.option_safeguard.MarketDataFetcher')
    @patch('app.option_safeguard.PositionsManager')
    async def test_watching_scenario(self, mock_pm, mock_mdf, mock_tb, mock_cm):
        mock_ib = MagicMock()
        mock_cm.return_value.ib = mock_ib
        safeguard = OptionSafeguard()
        
        option = Option(symbol='SPX', right='P', strike=5000, conId=123)
        ticker = MagicMock(spec=Ticker)
        ticker.last = 1.0  # Safe price
        option.ticker = ticker
        position = Position(contract=option, position=-1, avgCost=200, account='DU123')
        
        stop_loss_order = Order(orderType='STP LMT', action='BUY', totalQuantity=1, auxPrice=5.0, lmtPrice=5.15)
        stop_loss_trade = MagicMock(spec=Trade)
        stop_loss_trade.contract = option
        stop_loss_trade.order = stop_loss_order
        stop_loss_trade.remaining.return_value = 1
        
        async def get_short_options_mock(): return [position]
        mock_tb.return_value.get_short_options.side_effect = get_short_options_mock
        async def get_open_trades_mock(): return [stop_loss_trade]
        mock_tb.return_value.get_open_trades.side_effect = get_open_trades_mock
        
        with patch('app.option_safeguard.is_market_open', return_value=True):
            await safeguard.guard_current_positions()
            
        mock_tb.return_value.modify_limit_order.assert_not_called()
        mock_tb.return_value.create_limit_order.assert_not_called()
        mock_tb.return_value.close_short_option_position.assert_not_called()

    @patch('app.option_safeguard.ConnectionManager')
    @patch('app.option_safeguard.TradingBot')
    @patch('app.option_safeguard.MarketDataFetcher')
    @patch('app.option_safeguard.PositionsManager')
    @patch('app.option_safeguard.calculate_max_loss')
    async def test_limit_order_exists_and_modified(self, mock_cml, mock_pm, mock_mdf, mock_tb, mock_cm):
        mock_ib = MagicMock()
        mock_cm.return_value.ib = mock_ib
        safeguard = OptionSafeguard()
        
        option = Option(symbol='SPX', right='P', strike=5000, conId=123)
        ticker = MagicMock(spec=Ticker)
        ticker.last = 5.5  # Risky price
        option.ticker = ticker
        position = Position(contract=option, position=-1, avgCost=200, account='DU123')

        trade_entry_log = TradeLogEntry(time=datetime.now())
        limit_order = Order(orderType='LMT', action='BUY', totalQuantity=1, lmtPrice=5.15, orderId=100)
        limit_trade = MagicMock(spec=Trade)
        limit_trade.contract = option
        limit_trade.order = limit_order
        limit_trade.log = [trade_entry_log]
        
        async def get_short_options_mock(): return [position]
        mock_tb.return_value.get_short_options.side_effect = get_short_options_mock
        async def get_open_trades_mock(): return [limit_trade]
        mock_tb.return_value.get_open_trades.side_effect = get_open_trades_mock
        async def modify_limit_order_mock(*args, **kwargs): return MagicMock()
        mock_tb.return_value.modify_limit_order.side_effect = modify_limit_order_mock
        
        mock_cml.return_value = 2.0
        
        with patch('app.option_safeguard.is_market_open', return_value=True):
            await safeguard.guard_current_positions()
        
        mock_tb.return_value.modify_limit_order.assert_called_once()
        args, _ = mock_tb.return_value.modify_limit_order.call_args
        self.assertAlmostEqual(args[1], 4, delta=0.1)

    @patch('app.option_safeguard.ConnectionManager')
    @patch('app.option_safeguard.TradingBot')
    @patch('app.option_safeguard.MarketDataFetcher')
    @patch('app.option_safeguard.PositionsManager')
    @patch('app.option_safeguard.calculate_max_loss')
    @patch('app.option_safeguard.find_high_limit_buy_trade')
    async def test_no_orders_risky_position(self, mock_find_high, mock_cml, mock_pm, mock_mdf, mock_tb, mock_cm):
        mock_ib = MagicMock()
        mock_cm.return_value.ib = mock_ib
        safeguard = OptionSafeguard()
        
        option = Option(symbol='SPX', right='P', strike=5000, conId=123)
        ticker = MagicMock(spec=Ticker)
        ticker.last = 10.0  # Very risky price
        option.ticker = ticker
        position = Position(contract=option, position=-1, avgCost=200, account='DU123')
        
        async def get_short_options_mock(): return [position]
        mock_tb.return_value.get_short_options.side_effect = get_short_options_mock
        async def get_open_trades_mock(): return []
        mock_tb.return_value.get_open_trades.side_effect = get_open_trades_mock
        async def create_limit_order_mock(*args, **kwargs): return MagicMock()
        mock_tb.return_value.create_limit_order.side_effect = create_limit_order_mock
        
        mock_find_high.return_value = None
        mock_cml.return_value = 2.0 
        
        with patch('app.option_safeguard.is_market_open', return_value=True):
            await safeguard.guard_current_positions()
            
        mock_tb.return_value.create_limit_order.assert_called_once()
        args, _ = mock_tb.return_value.create_limit_order.call_args
        self.assertAlmostEqual(args[1], 4.0)
