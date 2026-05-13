import unittest
import logging

from unittest.mock import Mock, patch, MagicMock

from ib_insync import Ticker, Trade, Option, Order

from utilities.ib_utils import connect
from app.option_safeguard import OptionSafeguard

from ib_insync.objects import Position

class TestOptionSafeguard(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # Enable logging once for the test class
        logging.basicConfig(level=logging.DEBUG)
        logging.getLogger('option_safeguard').setLevel(logging.DEBUG)

    @patch('tws_connection.IB')
    def test_no_risk(self, ib):
        position_manager = Mock()
        position_manager.get_recent_trades.return_value = []

        connect(-1)

        option = Option(right='P', strike=1000)
        option.ticker = Ticker()
        option.ticker.last = 1
        position = Position(contract=option, position=-1, avgCost=0.15, account="my account")

        stop_loss_order = Order(orderType='STP', totalQuantity=1, auxPrice=2)
        stop_loss_trade = Trade(order=stop_loss_order)

        ib.positions.return_value = [position]
        ib.openTrades.return_value = [stop_loss_trade]

        option_safeguard = OptionSafeguard(position_manager)
        option_safeguard.guard_current_positions()

        ib.placeOrder.assert_not_called()


    @patch('option_safeguard.is_regular_hours')
    @patch('tws_connection.IB')
    def test_handle_current_risk_outside_regular_trading_hours(self, ib, is_regular_hours):

        is_regular_hours.return_value = False

        position_manager = Mock()
        position_manager.get_recent_trades.return_value = []
        position_manager.is_recent_buy_filled.return_value = False

        option = Option(right='P', strike=1000)
        option.ticker = Ticker()
        option.ticker.last = 2
        position = Position(contract=option, position=-1, avgCost=0.15, account="my account")

        stop_loss_order = Order(orderType='STP', totalQuantity=1, auxPrice=2)
        stop_loss_trade = Trade(order=stop_loss_order)

        buy_order = Order(orderType='LMT', action='BUY', totalQuantity=1, lmtPrice=2)
        buy_trade = Trade(order=buy_order)

        """trading_bot = MagicMock()
        trading_bot.get_short_options.return_value = [position]
        trading_bot.get_open_trades.return_value = [stop_loss_trade]
        trading_bot.close_short_option_position.return_value = buy_trade
        trading_bot.get_open_trades.return_value = [stop_loss_trade]
        trading_bot_class.return_value = trading_bot"""

        ib.positions.return_value = [position]
        ib.openTrades.return_value = [stop_loss_trade]

        connect(-1)

        option_safeguard = OptionSafeguard(position_manager)
        option_safeguard.guard_current_positions()

        ib.placeOrder.assert_called_once()
        trading_bot.get_open_trades.return_value.append(buy_trade)

        option_safeguard.guard_current_positions()

        ib.placeOrder.assert_called_once()
        trading_bot.get_open_trades.return_value.remove(buy_trade)
        position_manager.is_recent_buy_filled.return_value = True

        option_safeguard.guard_current_positions()

        ib.placeOrder.assert_called_once()

    @patch('option_safeguard.time')
    @patch('option_safeguard.connect')
    @patch('option_safeguard.is_regular_hours')
    @patch('option_safeguard.MarketDataFetcher')
    @patch('option_safeguard.TradingBot')
    def test_handle_current_risk_slow_close_of_short_option(self, trading_bot_class, market_data_fetcher_class, is_regular_hours, connect, time):
        is_regular_hours.return_value = False
        time.time.return_value = 0
        position_manager = Mock()
        position_manager.get_recent_trades.return_value = []
        position_manager.is_recent_buy_filled.return_value = False
        option_safeguard = OptionSafeguard(position_manager)

        option = Option(right='P', strike=1000)
        option.ticker = Ticker()
        option.ticker.last = 2
        position = Position(contract=option, position=-1, avgCost=0.15, account="my account")

        stop_loss_order = Order(orderType='STP', totalQuantity=1, auxPrice=2)
        stop_loss_trade = Trade(order=stop_loss_order)

        buy_order = Order(orderType='LMT', action='BUY', totalQuantity=1, lmtPrice=2)
        buy_trade = Trade(order=buy_order)

        trading_bot = MagicMock()
        trading_bot.get_short_options.return_value = [position]
        trading_bot.get_open_trades.return_value = [stop_loss_trade]
        trading_bot.close_short_option_position.return_value = buy_trade
        trading_bot.get_open_trades.return_value = [stop_loss_trade]
        trading_bot_class.return_value = trading_bot

        option_safeguard.initialize()
        option_safeguard.guard_current_positions()

        trading_bot.close_short_option_position.assert_called_once()

        trading_bot.get_open_trades.return_value.append(buy_trade)

        option_safeguard.guard_current_positions()
        trading_bot.close_short_option_position.assert_called_once()

        time.time.return_value = 11

        option_safeguard.guard_current_positions()
        trading_bot.close_short_option_position.assert_called_once()

        buy_trade.orderStatus.status = 'PendingCancel'
        trading_bot.cancel_trade.return_value = buy_trade

        option_safeguard.guard_current_positions()
        trading_bot.close_short_option_position.assert_called_once()
        trading_bot.cancel_trade.assert_called()

        buy_trade.orderStatus.status = 'Cancelled'

        option_safeguard.guard_current_positions()
        self.assertTrue(trading_bot.close_short_option_position.call_count == 2)


if __name__ == '__main__':
    unittest.main()
