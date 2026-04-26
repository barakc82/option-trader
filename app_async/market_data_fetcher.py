import asyncio
import math
import exchange_calendars as ecals
import pandas as pd
import time
from datetime import timedelta, datetime

from ib_insync import IB

from utilities.utils import *
from utilities.ib_utils import get_delta, req_id_to_target_delta


logger = logging.getLogger(__name__)
LIVE_DATA = 1
FROZEN_DATA = 2


def get_gamma(ticker):
    if ticker.lastGreeks and ticker.lastGreeks.gamma:
        return ticker.lastGreeks.gamma
    if ticker.modelGreeks and ticker.modelGreeks.gamma:
        return ticker.modelGreeks.gamma
    return None


class MarketDataFetcher:
    def __init__(self, ib: IB):
        self.ib = ib
        self.market_data_state = LIVE_DATA
        self.registered_con_ids = set()
        logger.info("MarketDataFetcher initialized.")


    def _register_ticker(self, ticker):
        """Ensures the update handler is attached exactly once per ticker."""
        if not ticker:
            return
        con_id = ticker.contract.conId
        if con_id not in self.registered_con_ids:
            ticker.updateEvent += self.on_option_ticker_update
            self.registered_con_ids.add(con_id)
            logger.debug(f"Registered update handler for {get_option_name(ticker.contract)}")


    def get_ticker(self, option):
        ticker = self.ib.ticker(option)
        if ticker:
            return ticker
        if hasattr(option, "ticker"):
            return option.ticker
        return None


    def get_last_price(self, option):
        ticker = self.get_ticker(option)
        if not ticker:
            return math.nan
        last_price = ticker.last
        if not last_price:
            return math.nan
        return last_price


    def set_market_data_state(self):

        cboe = ecals.get_calendar("XCBF")

        # Check if the exchange is open at the current time
        is_open = cboe.is_open_at_time(pd.Timestamp.now(), side="both")

        if is_open:
            if self.market_data_state != LIVE_DATA:
                self.ib.reqMarketDataType(LIVE_DATA)
                self.market_data_state = LIVE_DATA
        else:
            if self.market_data_state != FROZEN_DATA:
                self.ib.reqMarketDataType(FROZEN_DATA)
                self.market_data_state = FROZEN_DATA


    def on_option_ticker_update(self, ticker):
        # Point 2: Throttling high-frequency updates (5-second gatekeeper)
        now = time.time()
        last_time = getattr(ticker, 'last_processed_time', 0)
        if now - last_time < 0.5:
            return
        ticker.last_processed_time = now

        option = ticker.contract

        delta = get_delta(ticker)
        delta = math.nan if delta is None else abs(delta)
        gamma = get_gamma(ticker)
        gamma = math.nan if gamma is None else gamma
        price = self.get_last_price(option)
        logger.info(
            f"{get_option_name(option)} {option.symbol} {option.secType}, price: {price}, delta: {delta:.3f}, gamma: {gamma:.3f}")

        option_monitoring_required = False
        for order in self.ib.openOrders():
            if order.referenceContractId == option.conId:
                option_monitoring_required = True
                target_delta = req_id_to_target_delta.get(order.orderId, 1)
                if delta > target_delta:
                    logger.info(f"Should cancel order {order.orderId} as the delta({delta}) is greater than the target delta ({target_delta})")

        for position in self.ib.positions():
            if position.contract.conId == option.conId:
                option_monitoring_required = True
                break

        if not option_monitoring_required:
            self.ib.cancelMktData(option)
            if option.conId in self.registered_con_ids:
                self.registered_con_ids.remove(option.conId)


    async def update_ticker_data(self, contracts):

        assert contracts
        qualified_contracts = await self.ib.qualifyContractsAsync(*contracts)

        self.set_market_data_state()

        missing_tickers_in_cache = []
        contract_id_to_ticker = {}
        current_tickers = []
        for contract in qualified_contracts:
            ticker = self.ib.ticker(contract)
            if ticker is None:
                missing_tickers_in_cache.append(contract)
            else:
                # Point 1: Ensure handler is attached even if already in cache
                self._register_ticker(ticker)
                contract.ticker = ticker
                current_tickers.append(ticker)

        logger.debug("Fetching tickers")
        write_heartbeat()
        new_tickers = await self.ib.reqTickersAsync(*missing_tickers_in_cache)
        write_heartbeat()
        logger.debug("Done fetching tickers")

        for ticker in new_tickers:
            # Point 1: Register newly created tickers
            self._register_ticker(ticker)
            contract_id_to_ticker[ticker.contract.conId] = ticker

        current_tickers.extend(new_tickers)

        if all(ticker is None or (ticker.ask is None and ticker.bid is None) for ticker in current_tickers):
            raise ValueError("Could not fetch ticker data for all contracts")

        if all(ticker is None or ticker.modelGreeks is None or ticker.lastGreeks is None for ticker in current_tickers):
            logger.warning(f"No delta was updated for any of the options")

        for contract in missing_tickers_in_cache:
            ticker = contract_id_to_ticker[contract.conId]
            contract.ticker = ticker


    async def req_mkt_data(self, contract, is_snapshot=True):

        ticker = self.ib.ticker(contract)
        if ticker is not None:
            # Point 1: Register if we just found it in the IB cache
            self._register_ticker(ticker)
            is_ticker_valid = is_snapshot or datetime.now().astimezone() - ticker.time < timedelta(seconds=4)
            if is_ticker_valid:
                contract.ticker = ticker
                return ticker

        await self.ib.qualifyContractsAsync(contract)
        
        self.set_market_data_state()

        # Optimization: Use dynamic polling instead of fixed 4-second sleep
        ticker = self.ib.reqMktData(contract, "", snapshot=is_snapshot, regulatorySnapshot=False)
        
        start_time = time.time()
        # Wait up to 3 seconds, but proceed as soon as data arrives
        while math.isnan(ticker.last) and math.isnan(ticker.bid) and (time.time() - start_time < 3.0):
            await asyncio.sleep(0.1)

        if math.isnan(ticker.last) and math.isnan(ticker.bid):
            # Try one last batch request if snapshot failed
            tickers = await self.ib.reqTickersAsync(contract)
            ticker = tickers[0]
            if math.isnan(ticker.last):
                logger.warning(f"Last price of {contract.symbol} is unknown")

        # Point 1: Register ticker found/created by req_mkt_data
        self._register_ticker(ticker)
        return ticker
