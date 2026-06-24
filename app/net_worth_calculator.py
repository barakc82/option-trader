from datetime import datetime, date, timedelta
import math

from ib_insync import OrderStatus, Position

from utilities.utils import *

from .account_data import AccountData
from .market_data_fetcher import MarketDataFetcher
from .trading_bot import TradingBot

FOLD_AFTER_MARKET_RISE = 1 + 0.2
FOLD_AFTER_MARKET_DROP = 1 - 0.3


class NetWorthCalculator:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(NetWorthCalculator, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if not self._initialized:
            self.account_data = AccountData()
            self.market_data_fetcher = MarketDataFetcher()
            self.trading_bot = TradingBot()

            self._initialized = True

    async def calculate_max_options_for_market_rise(self, call_option):
        expiry_date = datetime.strptime(call_option.lastTradeDateOrContractMonth, "%Y%m%d").date()
        today_nyc = datetime.now(new_york_timezone).date()
        is_expiry_or_day_before = (today_nyc == expiry_date) or (today_nyc == expiry_date - timedelta(days=1))
        if is_expiry_or_day_before and not is_switched_to_overnight_trading():
            return sys.float_info.max

        current_spx_value = self.market_data_fetcher.get_spx_price()
        if math.isnan(current_spx_value):
            logger.error(
                "Cannot calculate max number of options for market rise because the S&P 500 index value is NaN")
            return 0

        spx_value_after_market_rise = current_spx_value * FOLD_AFTER_MARKET_RISE
        if call_option.strike > spx_value_after_market_rise:
            return sys.float_info.max

        positions = self.trading_bot.get_short_options()
        net_worth_after_rise = await self.calculate_net_worth_after_change(FOLD_AFTER_MARKET_RISE, current_spx_value, positions)
        if net_worth_after_rise < 0:
            return 0

        if spx_value_after_market_rise == call_option.strike:
            return sys.float_info.max

        liability_per_contract = (spx_value_after_market_rise - call_option.strike) * 100
        return math.floor(net_worth_after_rise / liability_per_contract)

    async def calculate_max_options_for_market_drop(self, put_option):
        expiry_date = datetime.strptime(put_option.lastTradeDateOrContractMonth, "%Y%m%d").date()
        today_nyc = datetime.now(new_york_timezone).date()
        is_expiry_or_day_before = (today_nyc == expiry_date) or (today_nyc == expiry_date - timedelta(days=1))
        if is_expiry_or_day_before and not is_switched_to_overnight_trading():
            return sys.float_info.max

        current_spx_value = self.market_data_fetcher.get_spx_price()
        if math.isnan(current_spx_value):
            logger.error(
                "Cannot calculate max number of options for market drop because the S&P 500 index value is NaN")
            return 0

        spx_price_after_drop = current_spx_value * FOLD_AFTER_MARKET_DROP
        if put_option.strike < spx_price_after_drop:
            logger.info(f"{get_option_name(put_option)} is lower than worst case scenario market drop")
            return sys.float_info.max

        positions = self.trading_bot.get_short_options()
        net_worth_after_drop = await self.calculate_net_worth_after_change(FOLD_AFTER_MARKET_DROP, current_spx_value,
                                                                      positions)
        if net_worth_after_drop < 0:
            logger.info(f"Negative net worth in case of a market drop")
            return 0

        liability_per_contract = (put_option.strike - spx_price_after_drop) * 100
        return math.floor(net_worth_after_drop / liability_per_contract)

    async def calculate_net_worth_after_change(self, fold_after_market_change: float, current_spx_value, positions) -> float:

        spx_value_after_market_change = current_spx_value * fold_after_market_change

        current_total_liability = 0
        right_of_interest = 'C' if fold_after_market_change > 1 else 'P'
        for position in positions:
            if not position.contract.secType == 'OPT' or position.position >= 0 or position.contract.right != right_of_interest:
                continue
            if position.contract.strike > spx_value_after_market_change and fold_after_market_change > 1:
                continue
            if position.contract.strike < spx_value_after_market_change and fold_after_market_change < 1:
                continue
            last_trade_date = datetime.strptime(position.contract.lastTradeDateOrContractMonth, "%Y%m%d").date()
            if last_trade_date <= date.today():
                continue
            position_liability = abs(
                spx_value_after_market_change - position.contract.strike) * 100 * -position.position
            current_total_liability += position_liability

        current_net_liquidation_value = await self.account_data.get_net_liquidation_value()
        net_liquidation_value_after_change = current_net_liquidation_value * fold_after_market_change
        net_worth_after_change = net_liquidation_value_after_change - current_total_liability
        return net_worth_after_change

    async def ensure_safe_exposure_with_all_trades(self):
        positions = self.trading_bot.get_short_options()
        open_trades = self.trading_bot.get_open_trades()
        today_nyc = datetime.now(new_york_timezone).date()
        current_spx_value = self.market_data_fetcher.get_spx_price()
        if math.isnan(current_spx_value):
            logger.error(
                "Cannot calculate max number of options for market rise because the S&P 500 index value is NaN")
            return FAILED

        potential_portfolio = positions.copy()
        open_sell_call_trades = [trade for trade in open_trades if trade.order.action.upper() == 'SELL' and trade.contract.right == 'C']
        for open_sell_call_trade in open_sell_call_trades:
            if open_sell_call_trade.orderStatus.status == OrderStatus.PendingCancel or is_trade_cancelled(open_sell_call_trade):
                continue

            call_option = open_sell_call_trade.contract
            expiry_date = datetime.strptime(call_option.lastTradeDateOrContractMonth, "%Y%m%d").date()
            is_expiry_or_day_before = (today_nyc == expiry_date) or (today_nyc == expiry_date - timedelta(days=1))
            if is_expiry_or_day_before and not is_switched_to_overnight_trading():
                continue

            position = Position(account='', contract=call_option, position=open_sell_call_trade.remaining(), avgCost=0)
            potential_portfolio.append(position)

        net_worth_after_rise = await self.calculate_net_worth_after_change(FOLD_AFTER_MARKET_RISE, current_spx_value, potential_portfolio)
        if net_worth_after_rise < 0:
            logger.info(f"Negative net worth in case of a market rise")
            return FAILED

        potential_portfolio = positions.copy()
        open_sell_put_trades = [trade for trade in open_trades if
                                trade.order.action.upper() == 'SELL' and trade.contract.right == 'P']
        for open_sell_put_trade in open_sell_put_trades:
            if open_sell_put_trade.orderStatus.status == OrderStatus.PendingCancel or is_trade_cancelled(open_sell_put_trade):
                continue

            put_option = open_sell_put_trade.contract
            expiry_date = datetime.strptime(put_option.lastTradeDateOrContractMonth, "%Y%m%d").date()
            is_expiry_or_day_before = (today_nyc == expiry_date) or (today_nyc == expiry_date - timedelta(days=1))
            if is_expiry_or_day_before and not is_switched_to_overnight_trading():
                continue

            position = Position(account='', contract=put_option, position=open_sell_put_trade.remaining(), avgCost=0)
            potential_portfolio.append(position)

        net_worth_after_drop = await self.calculate_net_worth_after_change(FOLD_AFTER_MARKET_DROP, current_spx_value, potential_portfolio)
        if net_worth_after_drop < 0:
            logger.info(f"Negative net worth in case of a market drop")
            return FAILED

        return SUCCESS