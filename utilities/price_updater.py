import math
from ib_insync import *
from datetime import datetime, timedelta

from utilities.database_access import get_worksheet


def req_mkt_data(contract, is_snapshot=True):
    ticker = ib.ticker(contract)
    if ticker is not None:

        is_ticker_valid = is_snapshot or datetime.now().astimezone() - ticker.time < timedelta(seconds=4)
        if is_ticker_valid:
            contract.ticker = ticker
            return ticker

    ib.qualifyContracts(contract)
    ib.sleep(2)

    LIVE_DATA = 1
    #FROZEN_DATA = 2

    ib.reqMarketDataType(LIVE_DATA)

    ticker = ib.reqMktData(contract, "", snapshot=True, regulatorySnapshot=False)
    ib.sleep(2)

    if math.isnan(ticker.last):
        tickers = ib.reqTickers(contract)
        ib.sleep(2)
        ticker = tickers[0]
        if math.isnan(ticker.last):
            print(f"Last price of {contract.symbol} is unknown")

    return ticker


def update_ticker_data(self, contracts):
    assert contracts
    qualified_contracts = ib.qualifyContracts(*contracts)

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

    new_tickers = self.ib.reqTickers(*missing_tickers_in_cache)
    for ticker in new_tickers:
        contract_id_to_ticker[ticker.contract.conId] = ticker

    current_tickers.extend(new_tickers)

    if all(ticker is None or (ticker.ask is None and ticker.bid is None) for ticker in current_tickers):
        ValueError("Could not fetch ticker data for all contracts")

    if all(ticker is None or ticker.modelGreeks is None or ticker.lastGreeks is None for ticker in current_tickers):
        print(f"No delta was updated for any of the options")

    for contract in missing_tickers_in_cache:
        ticker = contract_id_to_ticker[contract.conId]
        contract.ticker = ticker



ib = IB()
ib.connect('127.0.0.1', 7496, clientId=10)

security_names = ['VT', 'AVUV', 'AVDV', 'VGT', 'UPRO', 'SPHD', 'SCHD', 'SCHY', 'VIG', 'VIGI', 'GBTC', 'SPYU', 'SGOV',
                  'INTU', 'TXRH', 'MA', 'AXP', 'OXY']
row_indices = [4, 5, 6, 7, 8, 10, 11, 12, 13, 14, 16, 17, 18, 23, 29, 32, 36, 49]

sheet = get_worksheet("$$$$")
#update_ticker_data(s)
for security_name, row_index in zip(security_names, row_indices):
    contract = Stock(security_name, 'SMART', 'USD')
    ticker = req_mkt_data(contract)
    last_price = ticker.last
    if math.isnan(last_price):
        continue
    update_data = [[last_price]]
    sheet.update(values=update_data, range_name=f"B{row_index}:B{row_index}")
    print(f'{security_name} updated: {last_price}')
