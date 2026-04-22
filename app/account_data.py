import sys

from utilities.ib_utils import PORTFOLIO_MARGIN
from utilities.utils import MY_ACCOUNT, current_thread

import logging

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


class AccountData:

    def __init__(self):
        self.ib = current_thread.ib
        self.margin_type = PORTFOLIO_MARGIN

    def get_quantity(self, option):
        self.ib.sleep(0.5)
        positions = self.ib.positions(MY_ACCOUNT)
        for position in positions:
            if position.contract.conId == option.contract.conId:
                return position.position

    def get_cushion(self):
        return self.get_account_summary_item('Cushion')

    def get_account_value(self, item_tag, data_type='float', currency=None):
        account_values = self.ib.accountValues(account=MY_ACCOUNT)
        for account_value in account_values:
            if account_value.tag == item_tag and (currency is None or account_value.currency == currency):
                if data_type == 'float':
                    return float(account_value.value)
                else:
                    return account_value.value
        return sys.float_info.max

    def get_account_summary_item(self, item_tag, data_type='float'):
        account_summary = self.ib.accountSummary(account=MY_ACCOUNT)
        for item in account_summary:
            if item.tag == item_tag:
                if data_type == 'float':
                    return float(item.value)
                else:
                    return item.value
        return sys.float_info.max

    def get_previous_day_equity_with_loan(self):
        return self.get_account_summary_item('PreviousDayEquityWithLoanValue')

    def get_excess_liquidity(self):
        return self.get_account_summary_item('ExcessLiquidity')

    def get_net_liquidation_value(self):
        return self.get_account_summary_item('NetLiquidation')

    def get_margin_maintenance_requirement(self):
        return self.get_account_summary_item('MaintMarginReq')

    def get_available_funds(self):
        return self.get_account_summary_item('AvailableFunds')

    def get_margin_related_values(self):
        item_names = ["SMA"]
        margin_items = {}
        for item_name in item_names:
            margin_items[item_name] = self.get_account_summary_item(item_name)
        return margin_items

    async def get_margin_related_values_async(self):
        item_names = ["SMA",
                      "LookAheadAvailableFunds", "LookAheadExcessLiquidity", "HighestSeverity"]
        margin_items = {}
        for item_name in item_names:
            account_summary = await self.ib.accountSummaryAsync(account=MY_ACCOUNT)
            for item in account_summary:
                if item.tag == item_name:
                    margin_items[item_name] = float(item.value)
                    break
            margin_items[item_name] = sys.float_info.max
        return margin_items

    def is_portfolio_margin(self):
        return self.margin_type == PORTFOLIO_MARGIN

    def get_cash_balance_value(self):
        return self.get_account_value("CashBalance", currency='USD')
