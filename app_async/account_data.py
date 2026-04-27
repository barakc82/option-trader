import sys
import logging

from utilities.utils import MY_ACCOUNT
from utilities.ib_utils import PORTFOLIO_MARGIN

from .connection_manager import ConnectionManager


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class AccountData:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(AccountData, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if not self._initialized:
            # Accessing shared singleton connection
            self.ib = ConnectionManager().ib
            self.margin_type = PORTFOLIO_MARGIN
            logger.info("AccountData singleton initialized.")
            self._initialized = True

    async def get_quantity(self, option):
        positions = await self.ib.reqPositionsAsync()
        for position in positions:
            if position.contract.conId == option.contract.conId:
                return position.position
        return 0

    async def get_cushion(self):
        return await self.get_account_summary_item('Cushion')

    def get_account_value(self, item_tag, data_type='float', currency=None):
        account_values = self.ib.accountValues(account=MY_ACCOUNT)
        for account_value in account_values:
            if account_value.tag == item_tag and (currency is None or account_value.currency == currency):
                if data_type == 'float':
                    try:
                        return float(account_value.value)
                    except ValueError:
                        return account_value.value
                return account_value.value
        return sys.float_info.max

    async def get_account_summary_item(self, item_tag, data_type='float'):
        account_summary = await self.ib.accountSummaryAsync(account=MY_ACCOUNT)
        for item in account_summary:
            if item.tag == item_tag:
                if data_type == 'float':
                    try:
                        return float(item.value)
                    except ValueError:
                        return item.value
                return item.value
        return sys.float_info.max

    async def get_previous_day_equity_with_loan(self):
        return await self.get_account_summary_item('PreviousDayEquityWithLoanValue')

    async def get_excess_liquidity(self):
        return await self.get_account_summary_item('ExcessLiquidity')

    async def get_net_liquidation_value(self):
        return await self.get_account_summary_item('NetLiquidation')

    async def get_margin_maintenance_requirement(self):
        return await self.get_account_summary_item('MaintMarginReq')

    async def get_available_funds(self):
        return await self.get_account_summary_item('AvailableFunds')

    async def get_margin_related_values(self):
        item_names = ["SMA"]
        margin_items = {}
        for item_name in item_names:
            margin_items[item_name] = await self.get_account_summary_item(item_name)
        return margin_items

    async def get_margin_related_values_async(self):
        item_names = ["SMA", "LookAheadAvailableFunds", "LookAheadExcessLiquidity", "HighestSeverity"]
        margin_items = {}
        # Fetch summary once for all items (Efficiency)
        account_summary = await self.ib.accountSummaryAsync(account=MY_ACCOUNT)
        for item_name in item_names:
            margin_items[item_name] = sys.float_info.max
            for item in account_summary:
                if item.tag == item_name:
                    try:
                        margin_items[item_name] = float(item.value)
                    except ValueError:
                        margin_items[item_name] = item.value
                    break
        return margin_items

    def is_portfolio_margin(self):
        return self.margin_type == PORTFOLIO_MARGIN

    def get_cash_balance_value(self):
        return self.get_account_value("CashBalance", currency='USD')
