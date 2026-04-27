import asyncio
import math
import exchange_calendars as ecals
import pandas as pd

from ib_insync import Index

from utilities.utils import *
from utilities.ib_utils import get_delta, req_id_to_target_delta, is_hollow

from .connection_manager import ConnectionManager
from .option_cache import OptionCache

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
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(MarketDataFetcher, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if not self._initialized:
            self.ib = ConnectionManager().ib
            self.market_data_state = LIVE_DATA
            self.registered_con_ids = set()
            logger.info("MarketDataFetcher initialized.")
            self._initialized = True

    def _register_ticker(self, ticker):
        if not ticker:
            return
        con_id = ticker.contract.conId
        if con_id not in self.registered_con_ids:
            ticker.updateEvent += self.on_option_ticker_update
            self.registered_con_ids.add(con_id)
            logger.debug(f"Registered update handler for {get_option_name(ticker.contract)}")

    def on_spx_ticker_update(self, ticker):
        logger.info(f"SPX Ticker Update: {ticker.last}")

    async def get_spx_price(self):
        spx = Index('SPX', 'CBOE', 'USD')
        spx_ticker = self.ib.ticker(spx)
        if not spx_ticker or math.isnan(spx_ticker.last):
            spx_ticker = await self.req_mkt_data(spx)
        
        if not spx_ticker:
            return math.nan
            
        if math.isnan(spx_ticker.last):
            if not is_market_open():
                logger.warning("The close price of S&P 500 is used instead of the last price")
            return spx_ticker.close
        return spx_ticker.last

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
        if math.isnan(last_price):
            return math.nan
        return last_price

    def set_market_data_state(self):
        cboe = ecals.get_calendar("XCBF")
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
        # Point 2: Throttling high-frequency updates
        now = time.time()
        last_time = getattr(ticker, 'last_processed_time', 0)
        
        # Calculate gamma to determine throttle interval
        gamma = get_gamma(ticker)
        gamma = math.nan if gamma is None else gamma
        
        # If gamma is 0.0 or unknown, wait 5 seconds; otherwise 0.5 seconds
        throttle_interval = 5.0 if (math.isnan(gamma) or gamma == 0.0) else 0.5
        
        if now - last_time < throttle_interval:
            return
        ticker.last_processed_time = now

        option = ticker.contract
        delta = get_delta(ticker)
        delta = math.nan if delta is None else abs(delta)
        gamma = get_gamma(ticker)
        gamma = math.nan if gamma is None else gamma
        price = self.get_last_price(option)

        delta_str = f"{delta:.3f}" if not math.isnan(delta) else "N/A"
        gamma_str = f"{gamma:.3f}" if not math.isnan(gamma) else "N/A"
        
        logger.info(f"{get_option_name(option)} {option.symbol}, price: {price}, delta: {delta_str}, gamma: {gamma_str}")

        option_monitoring_required = False
        for trade in self.ib.openTrades():
            if trade.contract.conId == option.conId:
                option_monitoring_required = True
                target_delta = req_id_to_target_delta.get(trade.order.orderId, 1)
                if delta > target_delta:
                    logger.info(f"Should cancel order {trade.order.orderId} as delta({delta:.3f}) > target({target_delta:.3f})")

        for position in self.ib.positions():
            if position.contract.conId == option.conId:
                option_monitoring_required = True
                break

        if not option_monitoring_required:
            self.ib.cancelMktData(option)
            if option.conId in self.registered_con_ids:
                ticker.updateEvent -= self.on_option_ticker_update
                self.registered_con_ids.remove(option.conId)

    async def update_ticker_data(self, contracts):
        assert contracts
        await self.ib.qualifyContractsAsync(*contracts)
        self.set_market_data_state()

        missing_tickers_in_cache = []
        contract_id_to_ticker = {}
        current_tickers = []
        for contract in contracts:
            ticker = self.ib.ticker(contract)
            if ticker is None:
                missing_tickers_in_cache.append(contract)
            else:
                self._register_ticker(ticker)
                contract.ticker = ticker
                current_tickers.append(ticker)

        if not missing_tickers_in_cache:
            return

        new_tickers = await self.ib.reqTickersAsync(*missing_tickers_in_cache)
        for ticker in new_tickers:
            self._register_ticker(ticker)
            contract_id_to_ticker[ticker.contract.conId] = ticker
            current_tickers.append(ticker)
            ticker.contract.ticker = ticker

        if all(is_hollow(ticker) for ticker in current_tickers):
            raise ValueError(f"Could not fetch ticker data for all {len(current_tickers)} contracts")

        for contract in missing_tickers_in_cache:
            if contract.conId in contract_id_to_ticker:
                contract.ticker = contract_id_to_ticker[contract.conId]

    async def req_mkt_data(self, contract, is_snapshot=True):
        ticker = self.ib.ticker(contract)
        if ticker is not None:
            self._register_ticker(ticker)
            contract.ticker = ticker
            return ticker

        await self.ib.qualifyContractsAsync(contract)
        self.set_market_data_state()
        ticker = self.ib.reqMktData(contract, "", snapshot=is_snapshot, regulatorySnapshot=False)
        
        start_time = time.time()
        while math.isnan(ticker.last) and math.isnan(ticker.bid) and (time.time() - start_time < 3.0):
            await asyncio.sleep(0.1)

        if math.isnan(ticker.last) and math.isnan(ticker.bid):
            tickers = await self.ib.reqTickersAsync(contract)
            ticker = tickers[0]

        self._register_ticker(ticker)
        return ticker

    def get_ask(self, option):
        ticker = self.get_ticker(option)
        if not ticker or math.isnan(ticker.ask) or ticker.ask < 0:
            return sys.float_info.max
        return ticker.ask

    async def get_spx_implied_volatility(self):
        global last_implied_volatility
        spx_price = await self.get_spx_price()
        options_cache = OptionCache(self)
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
        await self.update_ticker_data(list(at_the_money_options.values()))
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

    async def get_chains(self, underlying):
        await self.ib.qualifyContractsAsync(underlying)
        result = self.ib.reqSecDefOptParams(underlying.symbol, '', underlying.secType, underlying.conId)
        return result

    async def qualify(self, options_to_check):
        return await  self.ib.qualifyContractsAsync(*options_to_check)
