import asyncio
import math
import time
import logging
import pandas as pd
import exchange_calendars as ecals
import yfinance as yf
from ib_insync import Index, Future
from utilities.utils import *
from utilities.ib_utils import get_delta

from .connection_manager import ConnectionManager
from .market_data_utils import LIVE_DATA, FROZEN_DATA, SPXESPair, get_gamma
from .index_price_manager import IndexPriceManager
from .option_data_fetcher import OptionDataFetcher

logger = logging.getLogger(__name__)

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
            self.registered_contracts = {}
            self.options_dump_time = 0
            
            self.index_manager = IndexPriceManager( )
            self.option_fetcher = OptionDataFetcher(self)
            
            self._state_lock = asyncio.Lock()
            self._risk_free_rate = None
            self._risk_free_rate_fetched_at = None

            logger.info("MarketDataFetcher initialized.")
            self._initialized = True

    @property
    def spx(self):
        return self.index_manager.spx

    @property
    def es(self):
        return self.index_manager.es

    @es.setter
    def es(self, value):
        self.index_manager.es = value

    def register_ticker(self, ticker):
        if not ticker:
            return
        con_id = ticker.contract.conId
        if con_id not in self.registered_contracts:
            ticker.updateEvent += self.on_ticker_update
            self.registered_contracts[con_id] = ticker
            logger.info(f"Registered update handler for {ticker.contract.symbol} {get_option_name(ticker.contract)}")

    def get_spx_price(self):
        return self.index_manager.get_spx_price()

    def get_es_price(self):
        return self.index_manager.get_es_price()

    async def fetch_es_future(self):
        return await self.index_manager.fetch_es_future()

    def calculate_spx_es_difference(self):
        return self.index_manager.calculate_spx_es_difference()

    def get_cached_risk_free_rate(self):
        now = time.time()
        if self._risk_free_rate is None or now - self._risk_free_rate_fetched_at >= 86400:
            try:
                irx = yf.Ticker("^IRX")
                rate_pct = irx.info.get('regularMarketPrice')
                self._risk_free_rate = rate_pct / 100.0 if rate_pct else 0.05
                self._risk_free_rate_fetched_at = now
                logger.info(f"Fetched risk-free rate from yfinance: r={self._risk_free_rate:.4f}")
            except Exception as e:
                logger.error(f"Failed to fetch risk-free rate: {e}")
                self._risk_free_rate = self._risk_free_rate or 0.05
                self._risk_free_rate_fetched_at = now
        return self._risk_free_rate

    async def ensure_market_data_type(self):
        """Ensures the correct market data type (Live vs Frozen) based on market hours."""
        async with self._state_lock:
            cboe = ecals.get_calendar("XCBF")
            is_open = cboe.is_open_at_time(pd.Timestamp.now(), side="both")

            target_state = LIVE_DATA if is_open else FROZEN_DATA
            if self.market_data_state != target_state:
                logger.info(f"Switching market data type to {'LIVE' if target_state == LIVE_DATA else 'FROZEN'}")
                self.ib.reqMarketDataType(target_state)
                self.market_data_state = target_state

    def on_ticker_update(self, ticker):
        """Handle real-time updates for options, with throttling."""
        now = time.time()
        last_time = getattr(ticker, 'last_processed_time', 0)
        
        gamma = get_gamma(ticker)
        price = ticker.last
        contract = ticker.contract

        # Slower updates for low-gamma options (further out of money or illiquid)
        throttle_interval = 0.5 if contract.symbol == 'SPX' else 1.0
        if (math.isnan(gamma) or gamma < 0.002):
            throttle_interval = 5.0 if contract.symbol == 'SPX' else 10.0
        if math.isnan(price):
            throttle_interval = 20.0 if contract.symbol == 'SPX' else 40.0

        if now - last_time < throttle_interval:
            return
        ticker.last_processed_time = now

        if ticker.contract in [self.spx, self.es]:
            self.index_manager.on_index_ticker_update()

        delta = get_delta(ticker)
        delta_str = f"{abs(delta):.3f}" if delta is not None else "N/A"
        gamma_str = f"{gamma:.3f}" if not math.isnan(gamma) else "N/A"
        logger.info(f"Update: {contract.symbol} {get_option_name(contract)} | Price: {price} | Delta: {delta_str} | Gamma: {gamma_str}")

    async def request_subscriptions(self, contracts):
        if not contracts:
            return

        contracts = await self.qualify(contracts)
        if not contracts:
            logger.error("Could not qualify any of the contracts")
            return

        await self.ensure_market_data_type()

        new_tickers = [self.ib.reqMktData(c) for c in contracts]

        timeout = 5 * math.pow(len(contracts), 0.2)
        start = time.time()
        while time.time() - start < timeout:
            if all(t.last and not math.isnan(t.last) for t in new_tickers):
                break
            await asyncio.sleep(0.1)

        for ticker in new_tickers:
            self.register_ticker(ticker)

        for contract in contracts:
            contract.ticker = self.ib.ticker(contract)

    async def request_snapshots(self, contracts):
        if not contracts:
            return

        await self.qualify(contracts)
        await self.ensure_market_data_type()

        missing = [c for c in contracts if c.conId not in self.registered_contracts]

        if missing:
            write_heartbeat()
            ticker_snapshots = await self.ib.reqTickersAsync(*missing)
            write_heartbeat()

            for ticker_snapshot in ticker_snapshots:
                ticker_snapshot.contract.ticker = ticker_snapshot

            received_con_ids = {t.contract.conId for t in ticker_snapshots}
            still_missing = [c for c in missing if c.conId not in received_con_ids]
            if still_missing:
                logger.info(f"The following contracts were requested but not fetched as snapshots: {[get_option_name(c) for c in still_missing]}")

    def cancel_market_data(self, contract):
        if not contract or not contract.conId:
            return

        if contract.conId in self.registered_contracts:
            ticker = self.ib.ticker(contract)
            if ticker:
                ticker.updateEvent -= self.on_ticker_update
                self.ib.cancelMktData(contract)

            self.registered_contracts.pop(contract.conId)
            logger.info(f"Unsubscribed from market data for {get_option_name(contract)}")

    def get_ticker(self, option):
        ticker = self.ib.ticker(option)
        if ticker:
            return ticker
        return getattr(option, 'ticker', None)

    def get_market_price(self, option):
        ticker = self.get_ticker(option)
        if not ticker:
            return math.nan
        return ticker.marketPrice()

    def get_delta(self, option):
        ticker = self.get_ticker(option)
        delta = get_delta(ticker) if ticker else None
        if delta is None or math.isnan(delta):
            return ""
        return str(round(abs(delta), 3))

    def get_ask(self, option):
        ticker = self.get_ticker(option)
        if not ticker or math.isnan(ticker.ask) or ticker.ask < 0:
            return math.nan
        return ticker.ask

    def get_reference_price(self):
        return self.index_manager.get_spot_price()

    async def get_chains(self, underlying):
        return await self.option_fetcher.get_chains(underlying)

    async def get_options(self, date):
        return await self.option_fetcher.get_options(date)

    async def get_spx_implied_volatility(self, right):
        return await self.option_fetcher.get_spx_implied_volatility(right)

    def get_cached_spx_implied_volatility(self, right):
        return self.option_fetcher.last_implied_volatility[right]

    async def qualify(self, contracts):
        return await self.ib.qualifyContractsAsync(*contracts)
