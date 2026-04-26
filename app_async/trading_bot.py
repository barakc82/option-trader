import asyncio
import math
from datetime import date, datetime

from ib_insync import IB, LimitOrder, MarketOrder, StopOrder

from utilities.utils import *
from .market_data_fetcher import MarketDataFetcher

logger = logging.getLogger(__name__)

class TradingBot:
    def __init__(self, ib: IB, market_data_fetcher: MarketDataFetcher):
        self.ib = ib
        self.market_data_fetcher = market_data_fetcher
        self.last_request_all_open_trades_time = 0
        self.price_increments = []
        logger.info("TradingBot initialized (MarketDataFetcher injected).")

    async def get_short_options(self, should_use_cache=True):
        for attempt in range(2):
            if not should_use_cache or attempt == 1:
                original_request_timeout = self.ib.RequestTimeout
                self.ib.RequestTimeout = 10.0
                try:
                    await self.ib.reqPositionsAsync()
                except TimeoutError:
                    logger.warning("reqPositions timed out")
                finally:
                    await asyncio.sleep(2)
                    self.ib.RequestTimeout = original_request_timeout

            logger.debug(f"Requesting positions (use_cache={should_use_cache}, attempt={attempt})")
            positions = self.ib.positions(MY_ACCOUNT)
            
            if positions or not should_use_cache:
                break
            
            logger.info("No positions found in cache, retrying with server request...")

        option_positions = []
        for position in positions:
            if position.contract.secType == 'OPT' and position.position < 0:
                last_trade_date = datetime.strptime(position.contract.lastTradeDateOrContractMonth, "%Y%m%d").date()
                if last_trade_date < date.today() or (last_trade_date == date.today() and is_after_hours()):
                    continue
                option_positions.append(position)

        options = [position.contract for position in option_positions]
        if options:
            logger.debug(f"Updating {len(options)} tickers of existing option positions")
            await self.market_data_fetcher.update_ticker_data(options)
        return option_positions

    async def get_open_trades(self):
        should_use_cache = time.time() - self.last_request_all_open_trades_time < 300
        if not should_use_cache:
            logger.info("Order cache stale, refreshing from IB...")
            await self.ib.reqAllOpenOrdersAsync()
            self.last_request_all_open_trades_time = time.time()

        open_trades = self.ib.openTrades()
        open_trades = [trade for trade in open_trades if
                       not is_trade_cancelled(trade) and trade.contract.secType == 'OPT']

        if not should_use_cache:
            for open_trade in open_trades:
                logger.debug(f"Open trade: {open_trade.order.orderId}, stop loss: {open_trade.order.auxPrice}")

        tickers = self.ib.tickers()
        for open_trade in open_trades:
            if not hasattr(open_trade.contract, 'ticker'):
                for ticker in tickers:
                    if ticker.contract.conId == open_trade.contract.conId:
                        open_trade.contract.ticker = ticker
                        break

        open_sell_trades = [trade for trade in open_trades if trade.order.action.upper() == 'SELL']
        if open_sell_trades:
            contracts = [trade.contract for trade in open_sell_trades]
            await self.market_data_fetcher.update_ticker_data(contracts)
        return open_trades

    # Point 1: Reset cache on order placement
    def place_order(self, contract, order):
        logger.info(f"Placing {order.action} order for {get_option_name(contract)}")
        trade = self.ib.placeOrder(contract, order)
        self.last_request_all_open_trades_time = 0 # Invalidate cache
        return trade

    # Point 1: Reset cache on cancellation
    def cancel_order(self, order):
        trade = self.ib.cancelOrder(order)
        logger.info(f"Status of cancel: {trade.orderStatus.status}")
        self.last_request_all_open_trades_time = 0 # Invalidate cache
        return trade

    def cancel_trade(self, trade):
        return self.cancel_order(trade.order)

    async def close_short_option(self, option, quantity):
        open_trades = await self.get_open_trades()
        for open_trade in open_trades:
            if option.conId == open_trade.contract.conId and open_trade.order.action.upper() == 'BUY':
                logger.info(f"Cancelling buy trade for {get_option_name(option)}")
                self.cancel_trade(open_trade)

        ticker = self.ib.ticker(option)
        if is_regular_hours():
            order = MarketOrder('BUY', quantity, account=MY_ACCOUNT, usePriceMgmtAlgo=False)
        else:
            limit = ticker.ask
            order = LimitOrder('BUY', quantity, limit, account=MY_ACCOUNT, usePriceMgmtAlgo=False)
            order.outsideRth = True
            order.tif = 'GTC'
        
        # Use the cache-invalidating place_order
        return self.place_order(option, order)

    async def close_short_option_position(self, position):
        return await self.close_short_option(position.contract, -position.position)

    def verify_price_increments_exist(self, contract):
        if not self.price_increments:
            contract_details = self.ib.reqContractDetails(contract)
            market_rule_ids_str = contract_details[0].marketRuleIds
            market_rule_id_str = market_rule_ids_str.split(',')[0]
            market_rule_id = int(market_rule_id_str)
            market_rule = self.ib.reqMarketRule(int(market_rule_id))
            assert market_rule
            for tier in market_rule:
                self.price_increments.append(tier)
            self.price_increments = sorted(self.price_increments, key=lambda increment_tier: increment_tier.lowEdge)


    def adjust_limit_to_market_rules(self, raw_limit):
        assert self.price_increments
        assert raw_limit
        assert not math.isnan(raw_limit)
        current_increment = self.price_increments[0].increment
        for price_increment in self.price_increments:
            if raw_limit > price_increment.lowEdge:
                current_increment = price_increment.increment
        assert not math.isnan(current_increment)
        limit = round(round(raw_limit / current_increment) * current_increment, 6)
        return limit


    def add_stop_loss(self, position, stop_loss_per_option):
        self.verify_price_increments_exist(position.contract)
        raw_stop_loss_price = position.avgCost / 100 + stop_loss_per_option
        stop_loss_price = self.adjust_limit_to_market_rules(raw_stop_loss_price)
        stop_loss_order = StopOrder('BUY', abs(position.position), stop_loss_price, account=MY_ACCOUNT)
        # limit_price = stop_loss_price + 0.15
        # stop_loss_order = StopLimitOrder('BUY', abs(position.position), lmtPrice=limit_price, stopPrice=stop_loss_price)
        stop_loss_order.usePriceMgmtAlgo = False
        stop_loss_order.tif = 'GTC'

        logger.info(f"Placing a stop loss order for {get_option_name(position.contract)}")
        trade = self.ib.placeOrder(position.contract, stop_loss_order)

        # is this really required?
        # self.ib.sleep(2)
        return trade
