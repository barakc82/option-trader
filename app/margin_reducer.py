from strike_finder import StrikeFinder
from utils import current_thread

from opportunity_explorer import OpportunityExplorer


class MarginReducer():
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(MarginReducer, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if not self._initialized:
            # self.account_data = AccountData()
            # self.trading_bot = TradingBot()
            self.opportunity_explorer = OpportunityExplorer()
            self.strike_finder = StrikeFinder()

            self._initialized = True

    def try_to_reduce_initial_margin_for_call_options(self):
        last_call_option_price = self.opportunity_explorer.last_call_option_price
        if last_call_option_price <= 0.2:
            return

        available_cheap_call_option = self.strike_finder.get_available_cheap_call_option()
