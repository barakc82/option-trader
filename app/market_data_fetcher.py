import math

import exchange_calendars as ecals
import pandas as pd
from ib_insync import Index

from utilities.utils import *
from utilities.ib_utils import req_id_to_target_delta, get_delta
from app.option_cache import OptionCache

LIVE_DATA = 1
FROZEN_DATA = 2

logger = logging.getLogger(__name__)
last_implied_volatility = 0

def on_spx_ticker_update(ticker):
    logger.info(f"{ticker.contract} {ticker.contract.symbol} {ticker.contract.secType}, ticker update")

def get_gamma(ticker):
    if ticker.lastGreeks and ticker.lastGreeks.gamma:
        return ticker.lastGreeks.gamma
    if ticker.modelGreeks and ticker.modelGreeks.gamma:
        return ticker.modelGreeks.gamma
    return None


def get_implied_volatility(ticker):
    if ticker.lastGreeks and ticker.lastGreeks.impliedVol:
        return ticker.lastGreeks.impliedVol
    if ticker.modelGreeks and ticker.modelGreeks.impliedVol:
        return ticker.modelGreeks.impliedVol
    return None


class MarketDataFetcher:

    def __init__(self):
        self.ib = current_thread.ib
        self.market_data_state = LIVE_DATA
        self.subscribed_contracts = set()

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

    def req_mkt_data(self, contract, is_snapshot=True):

        ticker = self.ib.ticker(contract)
        if ticker is not None:

            is_ticker_valid = is_snapshot or datetime.now().astimezone() - ticker.time < timedelta(seconds=4)
            if is_ticker_valid:
                contract.ticker = ticker
                return ticker

        self.ib.qualifyContracts(contract)
        self.ib.sleep(2)

        self.set_market_data_state()

        ticker = self.ib.reqMktData(contract, "", snapshot=is_snapshot, regulatorySnapshot=False)
        self.ib.sleep(2)

        if math.isnan(ticker.last):
            tickers = self.ib.reqTickers(contract)
            self.ib.sleep(2)
            ticker = tickers[0]
            if math.isnan(ticker.last):
                logger.warning(f"Last price of {contract.symbol} is unknown")

        self.subscribed_contracts.add(contract.conId)
        return ticker

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

    def update_ticker_data(self, contracts):

        assert contracts
        qualified_contracts = self.ib.qualifyContracts(*contracts)

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
        new_tickers = self.ib.reqTickers(*missing_tickers_in_cache)
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

    def get_chains(self, underlying):

        self.ib.qualifyContracts(underlying)
        result = self.ib.reqSecDefOptParams(underlying.symbol, '', underlying.secType, underlying.conId)
        return result

    def qualify(self, options_to_check):

        return self.ib.qualifyContracts(*options_to_check)

    def is_connected(self):
        return self.ib.isConnected() and self.ib.reqCurrentTime()

    def get_ticker(self, option):
        ticker = self.ib.ticker(option)
        if ticker:
            return ticker
        if hasattr(option, "ticker"):
            return option.ticker
        return None

    def get_delta(self, option):
        ticker = self.get_ticker(option)
        if not ticker:
            return ''
        delta = get_delta(ticker)
        if not delta or math.isnan(delta):
            return ''
        return str(round(abs(delta) * 1000) / 1000)

    def get_ask(self, option):
        ticker = self.get_ticker(option)
        if not ticker:
            return math.nan
        ask = ticker.ask
        if not ask or math.isnan(ask):
            return sys.float_info.max
        return ask

    def get_last_price(self, option):
        ticker = self.get_ticker(option)
        if not ticker:
            return math.nan
        last_price = ticker.last
        if not last_price:
            return math.nan
        return last_price

    def get_spx_price(self):
        spx = Index('SPX', 'CBOE', 'USD')
        spx_ticker = self.ib.ticker(spx)
        if not spx_ticker:
            spx_ticker = self.req_mkt_data(spx)
        if not spx_ticker:
            return math.nan
        spx_ticker.updateEvent += on_spx_ticker_update
        if math.isnan(spx_ticker.last):
            if not is_market_open():
                logger.warning("The close price of S&P 500 is used instead of the last price")
            return spx_ticker.close
        return spx_ticker.last

    def get_spx_implied_volatility(self):

        global last_implied_volatility
        spx_price = self.get_spx_price()
        options_cache = OptionCache()
        options = options_cache.load_cached_options()
        if not options:
            return last_implied_volatility

        at_the_money_options = {'C': options[0], 'P': options[0]}
        for right in ['C', 'P']:
            for option in options:
                if option.right == right and abs(option.strike - spx_price) < abs(
                        at_the_money_options[right].strike - spx_price):
                    at_the_money_options[right] = option

        logger.info("Obtaining implied volatility")
        write_heartbeat()
        self.update_ticker_data(list(at_the_money_options.values()))
        write_heartbeat()

        at_the_money_call_option = at_the_money_options['C']
        if not hasattr(at_the_money_call_option, "ticker"):
            return last_implied_volatility

        call_ticker = at_the_money_call_option.ticker
        call_implied_volatility = get_implied_volatility(call_ticker)

        if not call_implied_volatility:
            logger.warning(f"Implied volatility missing in the ticker of {get_option_name(at_the_money_call_option)}, "
                           f"S&P 500: {spx_price}, thus using an implied volatility of {last_implied_volatility}")
            return last_implied_volatility

        at_the_money_put_option = at_the_money_options['P']
        if not hasattr(at_the_money_put_option, "ticker"):
            return last_implied_volatility

        put_ticker = at_the_money_put_option.ticker
        put_implied_volatility = get_implied_volatility(put_ticker)

        if not put_implied_volatility:
            logger.warning(f"Implied volatility missing in the ticker of {get_option_name(at_the_money_put_option)}, s&P 500: {spx_price}")
            return last_implied_volatility

        implied_volatility = (call_implied_volatility + put_implied_volatility) / 2
        if implied_volatility > 1.9:
            logger.error(
                f"Implied volatility is {implied_volatility}, discarding. last call iv: {call_ticker.lastGreeks.impliedVol}, model call iv: {call_ticker.modelGreeks.impliedVol}, "
                f"last put iv: {put_ticker.lastGreeks.impliedVol if put_ticker.lastGreeks else 'lastGreeks is None'},  "
                f"model put iv: {put_ticker.modelGreeks.impliedVol if put_ticker.modelGreeks else 'modelGreeks is None'}, "
                f"call: {get_option_name(at_the_money_options['C'])}, put: {get_option_name(at_the_money_options['P'])}")
            return last_implied_volatility if last_implied_volatility else 0

        if abs(implied_volatility - last_implied_volatility) > 1:
            logger.error(
                f"Implied volatility is {implied_volatility} while the last implied volatility is {last_implied_volatility}, "
                f"discarding. last call iv: {call_ticker.lastGreeks.impliedVol}, model call iv: {call_ticker.modelGreeks.impliedVol},"
                f" last put iv: {put_ticker.lastGreeks.impliedVol},  model put iv: {put_ticker.modelGreeks.impliedVol}, "
                f"call: {get_option_name(at_the_money_options['C'])}, put: {get_option_name(at_the_money_options['P'])}")
            return last_implied_volatility if last_implied_volatility else 0

        last_implied_volatility = implied_volatility
        return implied_volatility
