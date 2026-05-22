import time
import unittest
from datetime import timedelta, datetime
from unittest.mock import Mock, patch, MagicMock, AsyncMock
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


class TestOptionSafeguardIncrement(unittest.IsolatedAsyncioTestCase):
    @patch('app.option_safeguard.ConnectionManager')
    @patch('app.option_safeguard.TradingBot')
    @patch('app.option_safeguard.MarketDataFetcher')
    @patch('app.option_safeguard.PositionsManager')
    def setUp(self, mock_pm, mock_mdf, mock_tb, mock_cm):
        self.mock_ib = MagicMock()
        mock_cm.return_value.ib = self.mock_ib
        self.safeguard = OptionSafeguard()
        self.mock_tb = self.safeguard.trading_bot

    async def test_calculate_increment_period_base_cases(self):
        # Base cases: strike % 100 in [0, 25, 50, 75]
        for strike in [5000, 5025, 5050, 5075]:
            option = Option(symbol='SPX', right='P', strike=strike)
            result = await self.safeguard.calculate_total_increment_period(option, 5.0)
            self.assertEqual(result, timedelta(minutes=10), f"Failed for strike {strike}")

    async def test_calculate_increment_period_middle_tier_no_riskier(self):
        # Middle tier: strike % 100 in [10, 20, 30, 40, 60, 70, 80, 90]
        self.mock_tb.get_cache_open_trades.return_value = []
        option = Option(symbol='SPX', right='P', strike=5010)
        result = await self.safeguard.calculate_total_increment_period(option, 5.0)
        self.assertEqual(result, timedelta(minutes=15))

    async def test_calculate_increment_period_middle_tier_with_riskier_higher_price(self):
        # Middle tier with riskier trade but higher price
        riskier_option = Option(symbol='SPX', right='P', strike=5020) # Riskier for P is higher strike
        riskier_trade = MagicMock(spec=Trade)
        riskier_trade.contract = riskier_option
        self.mock_tb.get_cache_open_trades.return_value = [riskier_trade]
        
        mock_ticker = MagicMock(spec=Ticker)
        mock_ticker.last = 6.0 # Higher than option price 5.0
        self.mock_ib.ticker.return_value = mock_ticker
        
        option = Option(symbol='SPX', right='P', strike=5010)
        result = await self.safeguard.calculate_total_increment_period(option, 5.0)
        self.assertEqual(result, timedelta(minutes=15))

    async def test_calculate_increment_period_middle_tier_with_riskier_lower_price(self):
        # Middle tier with riskier trade and lower price
        riskier_option = Option(symbol='SPX', right='P', strike=5020)
        riskier_trade = MagicMock(spec=Trade)
        riskier_trade.contract = riskier_option
        self.mock_tb.get_cache_open_trades.return_value = [riskier_trade]
        
        mock_ticker = MagicMock(spec=Ticker)
        mock_ticker.last = 4.0 # Lower than option price 5.0
        self.mock_ib.ticker.return_value = mock_ticker
        
        option = Option(symbol='SPX', right='P', strike=5010)
        result = await self.safeguard.calculate_total_increment_period(option, 5.0)
        self.assertEqual(result, timedelta(minutes=20))

    async def test_calculate_increment_period_others_tier_no_riskier(self):
        # Others tier: e.g., strike % 100 == 5
        self.mock_tb.get_cache_open_trades.return_value = []
        option = Option(symbol='SPX', right='P', strike=5005)
        result = await self.safeguard.calculate_total_increment_period(option, 5.0)
        self.assertEqual(result, timedelta(minutes=20))

    async def test_calculate_increment_period_others_tier_with_riskier_lower_price(self):
        # Others tier with riskier trade and lower price
        riskier_option = Option(symbol='SPX', right='P', strike=5010)
        riskier_trade = MagicMock(spec=Trade)
        riskier_trade.contract = riskier_option
        self.mock_tb.get_cache_open_trades.return_value = [riskier_trade]
        
        mock_ticker = MagicMock(spec=Ticker)
        mock_ticker.last = 4.0 # Lower than option price 5.0
        self.mock_ib.ticker.return_value = mock_ticker
        
        option = Option(symbol='SPX', right='P', strike=5005)
        result = await self.safeguard.calculate_total_increment_period(option, 5.0)
        self.assertEqual(result, timedelta(minutes=25))

    async def test_calculate_increment_period_call_riskier_logic(self):
        # For Calls, riskier means lower strike
        riskier_option = Option(symbol='SPX', right='C', strike=4990)
        riskier_trade = MagicMock(spec=Trade)
        riskier_trade.contract = riskier_option
        self.mock_tb.get_cache_open_trades.return_value = [riskier_trade]
        
        mock_ticker = MagicMock(spec=Ticker)
        mock_ticker.last = 4.0
        self.mock_ib.ticker.return_value = mock_ticker
        
        option = Option(symbol='SPX', right='C', strike=5000) # strike % 100 == 0 -> 10min anyway
        # Use a strike that doesn't trigger 10min
        option = Option(symbol='SPX', right='C', strike=5005)
        result = await self.safeguard.calculate_total_increment_period(option, 5.0)
        self.assertEqual(result, timedelta(minutes=25))

    async def test_calculate_increment_period_middle_tier_skipped_strikes(self):
        # Middle tier with riskier trade that should be skipped
        # For P, riskier is higher strike.
        option = Option(symbol='SPX', right='P', strike=10) # strike % 100 == 10
        riskier_option = Option(symbol='SPX', right='P', strike=15) # 15 > 10, and 15 is in skipped list
        riskier_trade = MagicMock(spec=Trade)
        riskier_trade.contract = riskier_option
        self.mock_tb.get_cache_open_trades.return_value = [riskier_trade]
        
        mock_ticker = MagicMock(spec=Ticker)
        mock_ticker.last = 4.0 
        self.mock_ib.ticker.return_value = mock_ticker
        
        result = await self.safeguard.calculate_total_increment_period(option, 5.0)
        self.assertEqual(result, timedelta(minutes=15))


class TestOptionSafeguardRiskPaths(unittest.IsolatedAsyncioTestCase):
    @patch('app.option_safeguard.ConnectionManager')
    @patch('app.option_safeguard.TradingBot')
    @patch('app.option_safeguard.MarketDataFetcher')
    @patch('app.option_safeguard.PositionsManager')
    def setUp(self, mock_pm, mock_mdf, mock_tb, mock_cm):
        self.mock_ib = MagicMock()
        mock_cm.return_value.ib = self.mock_ib
        self.safeguard = OptionSafeguard()
        self.mock_tb = self.safeguard.trading_bot
        self.mock_mdf = self.safeguard.market_data_fetcher
        self.mock_pm = self.safeguard.positions_manager
        
        # Default async mocks
        self.mock_tb.get_short_options = AsyncMock(return_value=[])
        self.mock_tb.get_open_trades = AsyncMock(return_value=[])
        self.mock_tb.create_limit_order = AsyncMock()
        self.mock_tb.modify_limit_order = AsyncMock()

    @patch('app.option_safeguard.is_market_open', return_value=True)
    async def test_handle_current_risk_missing_ticker_in_search(self, _):
        # Case: option.ticker is missing, and not found in market_data_fetcher.get_ticker
        option = Option(symbol='SPX', right='P', strike=5000, conId=123)
        # option has no ticker attribute or it is None
        
        position = Position(contract=option, position=-1, avgCost=200, account='DU123')
        self.mock_tb.get_short_options.return_value = [position]
        
        self.mock_mdf.get_ticker.return_value = None
        mock_ticker = MagicMock(spec=Ticker)
        mock_ticker.last = 10.0
        self.mock_mdf.req_mkt_data = AsyncMock(return_value=mock_ticker)
        
        await self.safeguard.guard_current_positions()
        
        self.mock_mdf.req_mkt_data.assert_called_once_with(option, is_snapshot=False)
        self.assertEqual(option.ticker, mock_ticker)

    @patch('app.option_safeguard.is_market_open', return_value=True)
    async def test_handle_current_risk_found_ticker_in_search(self, _):
        # Case: option.ticker is missing, but found in market_data_fetcher.get_ticker
        option = Option(symbol='SPX', right='P', strike=5000, conId=123)
        position = Position(contract=option, position=-1, avgCost=200, account='DU123')
        self.mock_tb.get_short_options.return_value = [position]
        
        mock_ticker = MagicMock(spec=Ticker)
        mock_ticker.last = 10.0
        self.mock_mdf.get_ticker.return_value = mock_ticker
        
        await self.safeguard.guard_current_positions()
        
        self.assertEqual(option.ticker, mock_ticker)
        self.mock_mdf.req_mkt_data.assert_not_called()

    @patch('app.option_safeguard.is_market_open', return_value=True)
    @patch('app.option_safeguard.calculate_max_loss', new_callable=AsyncMock)
    async def test_handle_current_risk_hollow_ticker(self, mock_cml, _):
        # Case: option.ticker is hollow
        mock_cml.return_value = 2.0
        mock_ticker = MagicMock(spec=Ticker)
        option = Option(symbol='SPX', right='P', strike=5000, conId=123)
        option.ticker = mock_ticker
        position = Position(contract=option, position=-1, avgCost=200, account='DU123')
        self.mock_tb.get_short_options.return_value = [position]
        
        new_ticker = MagicMock(spec=Ticker)
        new_ticker.last = 10.0
        self.mock_mdf.req_mkt_data = AsyncMock(return_value=new_ticker)
        
        with patch('app.option_safeguard.is_hollow', return_value=True):
            await self.safeguard.guard_current_positions()
        
        self.mock_mdf.req_mkt_data.assert_called_once_with(option, is_snapshot=False)
        self.assertEqual(option.ticker, new_ticker)

    @patch('app.option_safeguard.is_market_open', return_value=False)
    async def test_run_market_closed(self, mock_market_open):
        # Test one iteration of run() when market is closed
        self.safeguard.ib.isConnected.return_value = True
        self.safeguard.should_guard_positions = True
        
        with patch('app.option_safeguard.asyncio.sleep', side_effect=KeyboardInterrupt("Stop loop")):
            with patch('app.option_safeguard.logger') as mock_logger:
                try:
                    await self.safeguard.run()
                except KeyboardInterrupt:
                    pass
                mock_logger.debug.assert_any_call("Market is closed")

    async def test_run_not_guarding(self):
        with patch.object(OptionSafeguard, 'load_config'):
            self.safeguard.should_guard_positions = False
            with patch('app.option_safeguard.asyncio.sleep', side_effect=KeyboardInterrupt("Stop loop")):
                try:
                    await self.safeguard.run()
                except KeyboardInterrupt:
                    pass
                # Should skip connection check
                self.safeguard.ib.isConnected.assert_not_called()

    async def test_run_exception_path(self):
        with patch.object(OptionSafeguard, 'load_config', side_effect=Exception("Config error")):
            with patch('app.option_safeguard.asyncio.sleep', side_effect=KeyboardInterrupt("Stop loop")):
                with patch('app.option_safeguard.logger') as mock_logger:
                    try:
                        await self.safeguard.run()
                    except KeyboardInterrupt:
                        pass
                    mock_logger.exception.assert_called()

    async def test_run_not_connected(self):
        with patch.object(OptionSafeguard, 'load_config'):
            self.safeguard.should_guard_positions = True
            self.safeguard.ib.isConnected.return_value = False
            with patch('app.option_safeguard.asyncio.sleep', side_effect=KeyboardInterrupt("Stop loop")):
                with patch('app.option_safeguard.logger') as mock_logger:
                    try:
                        await self.safeguard.run()
                    except KeyboardInterrupt:
                        pass
                    mock_logger.warning.assert_any_call("OptionSafeguard: Task is waiting for IB connection...")

    @patch('app.option_safeguard.is_market_open', return_value=True)
    async def test_handle_current_risk_done_contract(self, _):
        option = Option(symbol='SPX', right='P', strike=5000, conId=123)
        position = Position(contract=option, position=-1, avgCost=200, account='DU123')
        self.mock_tb.get_short_options.return_value = [position]
        
        self.safeguard.positions_manager.done_contract_ids = {123}
        
        await self.safeguard.guard_current_positions()
        
        self.mock_mdf.get_ticker.assert_not_called()

    @patch('app.option_safeguard.is_market_open', return_value=True)
    @patch('app.option_safeguard.calculate_max_loss', new_callable=AsyncMock)
    @patch('app.option_safeguard.get_time_passed_since_submission', return_value=timedelta(minutes=30))
    async def test_handle_current_risk_update_limit_log(self, mock_gtpss, mock_cml, _):
        mock_cml.return_value = 5.0 # stop loss per option
        option = Option(symbol='SPX', right='P', strike=5000, conId=123)
        mock_ticker = MagicMock(spec=Ticker)
        mock_ticker.last = 8.0 # risky
        option.ticker = mock_ticker
        position = Position(contract=option, position=-1, avgCost=200, account='DU123') 
        self.mock_tb.get_short_options.return_value = [position]
        
        # limit_buy_trade
        order = Order(orderId=1, lmtPrice=5.0, action='BUY', orderType='LMT')
        trade = MagicMock(spec=Trade)
        trade.contract = option
        trade.order = order
        trade.remaining.return_value = 1
        
        self.mock_tb.get_open_trades.return_value = [trade]
        
        with patch('app.option_safeguard.logger') as mock_logger:
            with patch('app.option_safeguard.find_high_limit_buy_trade', return_value=trade):
                await self.safeguard.guard_current_positions()
                found = any("Trying to close risky position" in str(call) for call in mock_logger.warning.call_args_list)
                self.assertTrue(found)

    @patch('app.option_safeguard.is_market_open', return_value=True)
    @patch('app.option_safeguard.calculate_max_loss', new_callable=AsyncMock)
    @patch('app.option_safeguard.get_time_passed_since_submission', return_value=timedelta(minutes=30))
    async def test_handle_current_risk_limit_close(self, mock_gtpss, mock_cml, _):
        mock_cml.return_value = 5.0 
        option = Option(symbol='SPX', right='P', strike=5000, conId=123)
        mock_ticker = MagicMock(spec=Ticker)
        mock_ticker.last = 8.0 
        option.ticker = mock_ticker
        position = Position(contract=option, position=-1, avgCost=200, account='DU123') 
        self.mock_tb.get_short_options.return_value = [position]
        
        # Calculate what would be required_limit_price
        # initial_stop_loss = 2.0 (avg cost) + 5.0 (stop loss) = 7.0
        # total_increment = 10 min
        # time_passed = 30 min
        # required = 7.0 + 5.0 * 30/10 = 22.0
        
        # Set current limit close to 22.0
        order = Order(orderId=1, lmtPrice=21.99, action='BUY', orderType='LMT')
        trade = MagicMock(spec=Trade)
        trade.contract = option
        trade.order = order
        trade.remaining.return_value = 1
        
        self.mock_tb.get_open_trades.return_value = [trade]
        
        with patch('app.option_safeguard.logger') as mock_logger:
            with patch('app.option_safeguard.find_high_limit_buy_trade', return_value=trade):
                await self.safeguard.guard_current_positions()
                found = any("is close to the current limit price" in str(call) for call in mock_logger.info.call_args_list)
                self.assertTrue(found)

    async def test_run_connection_resolved_log(self):
        with patch.object(OptionSafeguard, 'load_config'):
            self.safeguard.should_guard_positions = True
            self.safeguard.ib.isConnected.return_value = True
            self.safeguard.connection_failure_start_time = time.time()
            
            with patch('app.option_safeguard.is_market_open', return_value=True):
                with patch('app.option_safeguard.asyncio.sleep', side_effect=KeyboardInterrupt("Stop loop")):
                    with patch('app.option_safeguard.logger') as mock_logger:
                        try:
                            await self.safeguard.run()
                        except KeyboardInterrupt:
                            pass
                        mock_logger.info.assert_any_call("OptionSafeguard: Connection error resolved.")
                        self.assertIsNone(self.safeguard.connection_failure_start_time)

    async def test_run_exception_sleep(self):
        with patch.object(OptionSafeguard, 'load_config', side_effect=Exception("Config error")):
            self.safeguard.connection_failure_start_time = time.time() - 400 # Persistent
            with patch('app.option_safeguard.asyncio.sleep', side_effect=KeyboardInterrupt("Stop loop")) as mock_sleep:
                try:
                    await self.safeguard.run()
                except KeyboardInterrupt:
                    pass
                # Check if sleep was called with progressive backoff (max 60)
                # elapsed = 400, sleep_time = min(10 + (400//60)*10, 60) = 60
                mock_sleep.assert_called_with(60)

    def test_load_config(self):
        # Case: load_config with a mock file
        config_data = '{"should_guard_positions": false}'
        with patch("builtins.open", unittest.mock.mock_open(read_data=config_data)):
            with patch("os.path.exists", return_value=True):
                self.safeguard.load_config()
                self.assertFalse(self.safeguard.should_guard_positions)

    @patch('app.option_safeguard.is_market_open', return_value=True)
    async def test_handle_current_risk_watching_log(self, _):
        # Case: stop_loss_trade exists and price >= stop_loss * 0.5
        mock_ticker = MagicMock(spec=Ticker)
        mock_ticker.last = 3.0
        option = Option(symbol='SPX', right='P', strike=5000, conId=123)
        option.ticker = mock_ticker
        position = Position(contract=option, position=-1, avgCost=200, account='DU123')
        self.mock_tb.get_short_options = AsyncMock(return_value=[position])
        
        stop_loss_trade = MagicMock(spec=Trade)
        stop_loss_trade.contract = option
        stop_loss_trade.order = MagicMock(orderType='STP LMT', auxPrice=5.0)
        stop_loss_trade.remaining.return_value = 1
        self.mock_tb.get_open_trades = AsyncMock(return_value=[stop_loss_trade])
        
        with patch('app.option_safeguard.logger') as mock_logger:
            await self.safeguard.guard_current_positions()
            mock_logger.info.assert_any_call(f"Watching the current price of P 5000: 3.00, stop loss is at 5.00")

    def test_load_config_error(self):
        # Case: load_config with a json error
        with patch("os.path.exists", return_value=True):
            with patch("builtins.open", unittest.mock.mock_open(read_data='invalid json')):
                with patch('app.option_safeguard.logger') as mock_logger:
                    self.safeguard.load_config()
                    mock_logger.error.assert_called()

if __name__ == '__main__':
    unittest.main()
