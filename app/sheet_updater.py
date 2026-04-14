from typing import List, Optional, Tuple, Callable
import math
from datetime import datetime
import logging

from ib_insync import Index, Option

from market_data_fetcher import get_delta
from utils import current_thread, write_heartbeat
from ib_utils import timedelta
import gspread
from google.oauth2.service_account import Credentials


SERVICE_ACCOUNT_FILE = "../resources/service_account.json"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
UPDATE_TIME_INTERVAL = timedelta(hours=2)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class SheetUpdater:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(SheetUpdater, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if not self._initialized:
            self.market_data_fetcher = current_thread.market_data_fetcher
            self.last_update_time = datetime.fromtimestamp(0)
            creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
            client = gspread.authorize(creds)
            self.sheet = client.open_by_key("1u2uLtVFnRCimMfymDMYygqg2zk2eK2TrQ4Bl18j8Uwc").worksheet("$$$$")
            self._initialized = True

    def update(self):

        if datetime.now() - self.last_update_time < UPDATE_TIME_INTERVAL:
            return

        underlying_symbol = 'SPX'
        exchange = 'CBOE'
        target_expiration_dates = ['20260115', '20270114']

        spx = Index(underlying_symbol, exchange, 'USD')
        chains = self.market_data_fetcher.get_chains(spx)
        assert chains
        chain = next(c for c in chains if c.exchange == exchange and c.tradingClass == 'SPXW')

        # Filter strikes and expirations
        strikes = chain.strikes
        strikes = sorted(strikes)

        update_data = []
        for target_expiration_date in target_expiration_dates:
            best_contract = self.find_contract(strikes, target_expiration_date)
            closest_delta = get_delta(best_contract.ticker)
            current_price = best_contract.ticker.last
            if math.isnan(best_contract.ticker.last) or current_price == -1:
                current_price = (best_contract.ticker.ask + best_contract.ticker.bid) / 2

            if closest_delta:
                logger.info(
                    f"Best contract: {best_contract.localSymbol}, "
                    f"Strike: {best_contract.strike}, "
                    f"Delta: {closest_delta:.4f}, "
                    f"Price: {current_price}"
                )
                update_data.append([current_price, best_contract.strike])

        if update_data:
            self.sheet.update(values=update_data, range_name="W25:X26")
            self.last_update_time = datetime.now()

    def find_contract(self, strikes, expiration_date) -> Optional[Tuple[float, float]]:
        """
        Binary search for the strike whose delta is closest to 0.5.

        Args:
            strikes: ascending list of strike prices
            get_delta: function(strike) -> delta (descending w.r.t strike), or None if invalid

        Returns:
            (strike, delta) of the closest valid match, or None if no valid deltas
        """

        candidate_options = []
        for strike in strikes:
            option = Option(symbol='SPX', lastTradeDateOrContractMonth=expiration_date, strike=strike, right='C',
                            exchange='CBOE', currency='USD', tradingClass='SPX')
            candidate_options.append(option)

        candidate_options = self.market_data_fetcher.ib.qualifyContracts(*candidate_options)

        lo = 0
        hi = len(candidate_options) - 1
        best = None  # (strike, delta, distance to 0.5)


        def consider(option):
            nonlocal best
            delta = get_delta(option.ticker)
            if delta is None:  # invalid, skip
                return
            dist = abs(delta - 0.5)
            if best is None or dist < best[1]:
                best = (option, dist)

        while hi - lo > 10:
            write_heartbeat()
            mid = (lo + hi) // 2
            mid_option = candidate_options[mid]
            ticker = self.market_data_fetcher.req_mkt_data(mid_option)
            mid_option.ticker = ticker
            mid_delta = get_delta(mid_option.ticker)

            if mid_delta is None:
                # invalid, move search window inward
                # shrink both ends to keep progress
                lo += 1
                hi -= 1
                continue

            consider(mid_option)
            logger.info(f"{mid_option.strike} {get_delta(mid_option.ticker):.2f}")

            # since deltas decrease with strike, compare to 0.5
            if mid_delta > 0.5:
                lo = mid + 1
            else:
                hi = mid - 1

        write_heartbeat()

        # Final linear scan
        final_candidate_options = candidate_options[lo:hi+1]
        self.market_data_fetcher.update_ticker_data(final_candidate_options)
        for option in final_candidate_options:
            consider(option)

        return 0 if best is None else best[0]
