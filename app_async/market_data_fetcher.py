import asyncio
import math
import exchange_calendars as ecals
import pandas as pd

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
        logger.info("MarketDataFetcher initialized.")


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
                else:
                    logger.info(
                        f"For {get_option_name(option)} is greater than the target delta ({target_delta})")

        for position in self.ib.positions():
            if position.contract.conId == option.conId:
                option_monitoring_required = True
                break

        if not option_monitoring_required:
            self.ib.cancelMktData(option)


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
                contract.ticker = ticker
                current_tickers.append(ticker)

        logger.debug("Fetching tickers")
        write_heartbeat()
        new_tickers = await self.ib.reqTickersAsync(*missing_tickers_in_cache)
        write_heartbeat()
        logger.debug("Done fetching tickers")

        for ticker in new_tickers:
            ticker.updateEvent += self.on_option_ticker_update
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

            is_ticker_valid = is_snapshot or datetime.now().astimezone() - ticker.time < timedelta(seconds=4)
            if is_ticker_valid:
                contract.ticker = ticker
                return ticker

        await self.ib.qualifyContractsAsync(contract)
        await asyncio.sleep(2)

        self.set_market_data_state()

        ticker = self.ib.reqMktData(contract, "", snapshot=is_snapshot, regulatorySnapshot=False)
        await asyncio.sleep(2)

        if math.isnan(ticker.last):
            tickers = await self.ib.reqTickersAsync(contract)
            await asyncio.sleep(2)
            ticker = tickers[0]
            if math.isnan(ticker.last):
                logger.warning(f"Last price of {contract.symbol} is unknown")

        return ticker