import sys
import logging
import time
from utilities.utils import MY_ACCOUNT
from utilities.ib_utils import PORTFOLIO_MARGIN
from .connection_manager import ConnectionManager

logger = logging.getLogger(__name__)

class AccountData:
    _instance = None
    SUMMARY_CACHE_TTL = 5.0  # seconds

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(AccountData, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if not self._initialized:
            self.ib = ConnectionManager().ib
            self.margin_type = PORTFOLIO_MARGIN
            self._summary_cache = None
            self._last_summary_time = 0
            logger.info("AccountData singleton initialized.")
            self._initialized = True

    async def _get_summary(self):
        """Returns cached account summary or fetches a fresh one if expired."""
        now = time.time()
        if self._summary_cache is None or (now - self._last_summary_time > self.SUMMARY_CACHE_TTL):
            self._summary_cache = await self.ib.accountSummaryAsync(account=MY_ACCOUNT)
            self._last_summary_time = now
        return self._summary_cache

    def _parse_value(self, value, data_type):
        if data_type == 'float':
            try:
                return float(value)
            except (ValueError, TypeError):
                return value
        return value

    async def get_quantity(self, option):
        positions = await self.ib.reqPositionsAsync()
        for position in positions:
            if position.contract.conId == option.contract.conId:
                return position.position
        return 0

    async def get_account_summary_item(self, item_tag, data_type='float'):
        summary = await self._get_summary()
        for item in summary:
            if item.tag == item_tag:
                return self._parse_value(item.value, data_type)
        return sys.float_info.max

    def get_account_value(self, item_tag, data_type='float', currency=None):
        """Fetches from the streaming accountValues list (no network call)."""
        account_values = self.ib.accountValues(account=MY_ACCOUNT)
        for val in account_values:
            if val.tag == item_tag and (currency is None or val.currency == currency):
                return self._parse_value(val.value, data_type)
        return sys.float_info.max

    async def get_cushion(self): return await self.get_account_summary_item('Cushion')
    async def get_previous_day_equity_with_loan(self): return await self.get_account_summary_item('PreviousDayEquityWithLoanValue')
    async def get_excess_liquidity(self): return await self.get_account_summary_item('ExcessLiquidity')
    async def get_net_liquidation_value(self): return await self.get_account_summary_item('NetLiquidation')
    async def get_margin_maintenance_requirement(self): return await self.get_account_summary_item('MaintMarginReq')
    async def get_available_funds(self): return await self.get_account_summary_item('AvailableFunds')

    async def get_margin_related_values(self):
        return {
            "SMA": await self.get_account_summary_item("SMA"),
            "LookAheadAvailableFunds": await self.get_account_summary_item("LookAheadAvailableFunds"),
            "LookAheadExcessLiquidity": await self.get_account_summary_item("LookAheadExcessLiquidity"),
            "HighestSeverity": await self.get_account_summary_item("HighestSeverity")
        }

    async def get_margin_related_values_async(self):
        # Kept for backward compatibility, but now uses the cache internally
        return await self.get_margin_related_values()

    def is_portfolio_margin(self):
        return self.margin_type == PORTFOLIO_MARGIN

    def get_cash_balance_value(self):
        return self.get_account_value("CashBalance", currency='USD')
