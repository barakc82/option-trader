import sys
import logging
import time
from typing import Optional, Any, Union, List, Dict
from ib_insync import Contract, Position, AccountValue
from utilities.utils import MY_ACCOUNT
from utilities.ib_utils import PORTFOLIO_MARGIN
from .connection_manager import ConnectionManager

logger = logging.getLogger(__name__)

class AccountData:
    """
    Singleton manager for account-level data and margin requirements.
    Provides cached access to IB account summaries.
    """
    _instance: Optional['AccountData'] = None
    SUMMARY_CACHE_TTL = 5.0  # seconds

    def __new__(cls) -> 'AccountData':
        if cls._instance is None:
            cls._instance = super(AccountData, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
            
        self.ib = ConnectionManager().ib
        self.margin_type = PORTFOLIO_MARGIN
        self._summary_cache: Optional[List[Any]] = None
        self._last_summary_time = 0.0
        
        logger.info("AccountData singleton initialized.")
        self._initialized = True

    async def _get_summary(self) -> List[Any]:
        """Returns cached account summary or fetches fresh data if expired."""
        now = time.time()
        if self._summary_cache is None or (now - self._last_summary_time > self.SUMMARY_CACHE_TTL):
            self._summary_cache = await self.ib.accountSummaryAsync(account=MY_ACCOUNT)
            self._last_summary_time = now
        return self._summary_cache

    def _parse_value(self, value: str, data_type: str) -> Union[float, str]:
        """Helper to parse raw IB string values into Python types."""
        if data_type == 'float':
            try:
                return float(value)
            except (ValueError, TypeError):
                return value
        return value

    async def get_quantity(self, option: Contract) -> float:
        """Returns the current position quantity for a given contract."""
        positions = await self.ib.reqPositionsAsync()
        for position in positions:
            if position.contract.conId == option.conId:
                return position.position
        return 0.0

    async def get_account_summary_item(self, item_tag: str, data_type: str = 'float') -> Union[float, str]:
        """Fetches a specific tag from the account summary."""
        summary = await self._get_summary()
        for item in summary:
            if item.tag == item_tag:
                return self._parse_value(item.value, data_type)
        return sys.float_info.max

    def get_account_value(self, item_tag: str, data_type: str = 'float', currency: Optional[str] = None) -> Union[float, str]:
        """
        Retrieves a value from the streaming accountValues list.
        This does not perform a network call.
        """
        account_values = self.ib.accountValues(account=MY_ACCOUNT)
        for val in account_values:
            if val.tag == item_tag and (currency is None or val.currency == currency):
                return self._parse_value(val.value, data_type)
        return sys.float_info.max

    async def get_cushion(self) -> float:
        """Returns the current account cushion."""
        return await self.get_account_summary_item('Cushion')

    async def get_previous_day_equity_with_loan(self) -> float:
        """Returns the previous day's equity with loan value."""
        return await self.get_account_summary_item('PreviousDayEquityWithLoanValue')

    async def get_excess_liquidity(self) -> float:
        """Returns the current excess liquidity."""
        return await self.get_account_summary_item('ExcessLiquidity')

    async def get_net_liquidation_value(self) -> float:
        """Returns the total net liquidation value."""
        return await self.get_account_summary_item('NetLiquidation')

    async def get_margin_maintenance_requirement(self) -> float:
        """Returns the maintenance margin requirement."""
        return await self.get_account_summary_item('MaintMarginReq')

    async def get_available_funds(self) -> float:
        """Returns the available funds for trading."""
        return await self.get_account_summary_item('AvailableFunds')

    def is_portfolio_margin(self) -> bool:
        """Returns True if the account uses Portfolio Margin."""
        return self.margin_type == PORTFOLIO_MARGIN

    def get_cash_balance_value(self) -> float:
        """Returns the USD cash balance."""
        return self.get_account_value("CashBalance", currency='USD')
