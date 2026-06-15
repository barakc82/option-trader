import asyncio
import math
import time
import json
import os
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from ib_insync import Index, Option, Stock, Future, ContFuture
from utilities.utils import *

from utilities.ib_utils import get_delta

from .connection_manager import ConnectionManager
from .option_cache import OptionCache

logger = logging.getLogger(__name__)

# Market Data Types
LIVE_DATA = 1
FROZEN_DATA = 2


def get_gamma(ticker):
    if ticker.lastGreeks and ticker.lastGreeks.gamma is not None:
        return ticker.lastGreeks.gamma
    if ticker.modelGreeks and ticker.modelGreeks.gamma is not None:
        return ticker.modelGreeks.gamma
    return math.nan

def get_implied_volatility(ticker):
    if ticker.lastGreeks and ticker.lastGreeks.impliedVol is not None:
        return ticker.lastGreeks.impliedVol
    if ticker.modelGreeks and ticker.modelGreeks.impliedVol is not None:
        return ticker.modelGreeks.impliedVol
    return math.nan

@dataclass
class SPXSpyPair:
    spx_price: float
    spy_price: float
    time: datetime


@dataclass
class SPXESPair:
    spx_price: float
    es_price: float
    time: datetime


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
            self.last_implied_volatility = {'C': 0.0, 'P': 0.0}
            self.last_implied_volatility_calculation_time = {'C': 0.0, 'P': 0.0}
            self.options_dump_time = 0
            self.previous_spx_value = math.nan
            self.previous_spy_value = math.nan
            self.previous_es_value = math.nan
            self.spx = Index(symbol='SPX', exchange='CBOE', currency='USD')
            self.spy = Stock(symbol='SPY', exchange='SMART', currency='USD')
            self.es = None
            self.spx_spy_history = deque(maxlen=100)
            self.spx_es_history = deque(maxlen=100)
            self.alternative_valuation = "SPY"
            self.load_config()

            # Use a lock for market data type switching

            self._state_lock = asyncio.Lock()
            
            logger.info("MarketDataFetcher initialized.")
            self._initialized = True

    def load_config(self):
        """Reads configuration from config/option_trader_config.json."""
        config_path = "config/option_trader_config.json"
        try:
            if os.path.exists(config_path):
                with open(config_path, "r") as f:
                    config = json.load(f)
                    self.alternative_valuation = config.get("alternative_valuation", "SPY")
        except Exception as e:
            logger.error(f"MarketDataFetcher: Error reading config: {e}")

    def register_ticker(self, ticker):
        if not ticker:
            return
        con_id = ticker.contract.conId
        if con_id not in self.registered_contracts:
            ticker.updateEvent += self.on_ticker_update
            self.registered_contracts[con_id] = ticker
            logger.info(f"Registered update handler for {get_option_name(ticker.contract)}")

    def get_spx_price(self):
        spx_ticker = self.ib.ticker(self.spx)

        if not spx_ticker:
            logger.info("SPX ticker is missing")
            return self.previous_spx_value

        if math.isnan(spx_ticker.last):
            if is_regular_hours():
                return self.previous_spx_value
            logger.warning("Market closed; using SPX close price.")
            price = spx_ticker.close
        else:
            price = spx_ticker.last

        if not math.isnan(price):
            self.previous_spx_value = price

        return price

    def get_spy_price(self):
        spy_ticker = self.ib.ticker(self.spy)

        if not spy_ticker:
            logger.info("SPY ticker is missing")
            return self.previous_spy_value

        price = spy_ticker.marketPrice()

        if not math.isnan(price):
            self.previous_spy_value = price

        return price

    def calculate_spx_spy_difference(self):
        if not self.spx_spy_history:
            if os.path.exists(JSON_PATH):
                try:
                    with open(JSON_PATH, "r") as f:
                        state = json.load(f)
                        if 'SPY' in state.get('index_label', ''):
                            return state.get('spx_premium', 0)
                except Exception as e:
                    logger.error(f"MarketDataFetcher: Error reading premium fallback from {JSON_PATH}: {e}")
            return 0

        total_diff = sum(entry.spx_price - 10 * entry.spy_price for entry in self.spx_spy_history)
        return total_diff / len(self.spx_spy_history)

    def calculate_spx_es_difference(self):
        if not self.spx_es_history:
            if os.path.exists(JSON_PATH):
                try:
                    with open(JSON_PATH, "r") as f:
                        state = json.load(f)
                        if 'ES' in state.get('index_label', ''):
                            return state.get('spx_premium', 0)
                except Exception as e:
                    logger.error(f"MarketDataFetcher: Error reading premium fallback from {JSON_PATH}: {e}")
            return 0

        total_diff = sum(entry.spx_price - entry.es_price for entry in self.spx_es_history)
        return total_diff / len(self.spx_es_history)

    def get_cached_spx_implied_volatility(self, right):
        return self.last_implied_volatility[right]

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

    def get_es_price(self):
        if not self.es:
            return self.previous_es_value

        es_ticker = self.ib.ticker(self.es)

        if not es_ticker:
            logger.info("ES ticker is missing")
            return self.previous_es_value

        price = es_ticker.marketPrice()

        if not math.isnan(price):
            self.previous_es_value = price

        return price

    async def fetch_es_future(self):
        if not self.es:
            es_incomplete = Future('ES', exchange='CME')

            # 2. Fetch all matching contract details from the exchange
            es_details = await self.ib.reqContractDetailsAsync(es_incomplete)
            contracts = [es_detail.contract for es_detail in es_details]

            # 3. Sort the contracts chronologically by expiration date
            contracts.sort(key=lambda c: c.lastTradeDateOrContractMonth)

            # 4. Select the closest expiration (front-month)
            closest_es_future = contracts[0]

            # 5. Fully qualify the contract before requesting live data or trading
            await self.ib.qualifyContractsAsync(closest_es_future)
            self.es = closest_es_future
        return self.es

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

    def on_index_ticker_update(self):
        spx_ticker = self.ib.ticker(self.spx)
        spy_ticker = self.ib.ticker(self.spy)
        es_ticker = self.ib.ticker(self.es) if self.es else None

        if not is_regular_hours() or not spx_ticker or math.isnan(spx_ticker.last):
            return

        # Update SPX-SPY history
        if spy_ticker and not math.isnan(spy_ticker.last):
            if (spx_ticker.time and spy_ticker.time and
                    (spx_ticker.time - spy_ticker.time).total_seconds() <= 2):
                new_spy_entry = SPXSpyPair(
                    spx_price=spx_ticker.last,
                    spy_price=spy_ticker.last,
                    time=datetime.now()
                )
                if (not self.spx_spy_history or
                        (new_spy_entry.time - self.spx_spy_history[-1].time).total_seconds() >= 5 * 60):
                    self.spx_spy_history.append(new_spy_entry)

        # Update SPX-ES history
        if es_ticker and not math.isnan(es_ticker.last):
            if (spx_ticker.time and es_ticker.time and
                    (spx_ticker.time - es_ticker.time).total_seconds() <= 2):
                new_es_entry = SPXESPair(
                    spx_price=spx_ticker.last,
                    es_price=es_ticker.last,
                    time=datetime.now()
                )
                if (not self.spx_es_history or
                        (new_es_entry.time - self.spx_es_history[-1].time).total_seconds() >= 5 * 60):
                    self.spx_es_history.append(new_es_entry)

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

        if ticker.contract in [self.spx, self.spy, self.es]:
            self.on_index_ticker_update()

        delta = get_delta(ticker)
        delta_str = f"{abs(delta):.3f}" if delta is not None else "N/A"
        gamma_str = f"{gamma:.3f}" if not math.isnan(gamma) else "N/A"
        logger.info(f"Update: {contract.symbol} {get_option_name(contract)} | Price: {price} | Delta: {delta_str} | Gamma: {gamma_str}")

        # Note: Subscription cleanup is handled separately or can be added here if highly selective.
        # Periodic cleanup is usually safer to avoid constant churning during volatile markets.

    async def request_subscriptions(self, contracts):
        """Qualify contracts and request fresh tickers for a batch of contracts."""
        if not contracts:
            return

        contracts = await self.qualify(contracts)
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

        # Ensure all contracts have their internal ticker reference updated (for convenience)
        for contract in contracts:
            contract.ticker = self.ib.ticker(contract)


    async def request_snapshots(self, contracts):
        """Qualify contracts and request fresh tickers for a batch of contracts."""
        if not contracts:
            return

        await self.qualify(contracts)
        await self.ensure_market_data_type()

        # Filter out what we already have active
        missing = [c for c in contracts if c.conId not in self.registered_contracts]

        if missing:
            write_heartbeat()
            ticker_snapshots = await self.ib.reqTickersAsync(*missing)
            write_heartbeat()

            # Ensure all contracts have their internal ticker reference updated (for convenience)
            for ticker_snapshot in ticker_snapshots:
                ticker_snapshot.contract.ticker = ticker_snapshot

            # Log contracts from the missing list which do not appear in any of the snapshot tickers
            received_con_ids = {t.contract.conId for t in ticker_snapshots}
            still_missing = [c for c in missing if c.conId not in received_con_ids]
            if still_missing:
                logger.info(f"The following contracts were requested but not fetched as snapshots: {[get_option_name(c) for c in still_missing]}")

    """
    async def request_ticker(self, contract):
        Request market data for a single contract using reqTickersAsync.
        await self.qualify([contract])
        await self.ensure_market_data_type()
        
        ticker = self.ib.reqMktData(contract)

        timeout = 5
        start = time.time()
        # Loop until we have AT LEAST ONE valid piece of pricing data, or we timeout
        while (time.time() - start) < timeout:
            has_last = not math.isnan(ticker.last)
            has_close = not math.isnan(ticker.close)
            has_bid_ask = not math.isnan(ticker.bid) and not math.isnan(ticker.ask)

            # If we have any of these, the ticker is successfully receiving data
            if has_last or has_close or has_bid_ask:
                break

            await asyncio.sleep(0.1)

        if not math.isnan(ticker.last) or not math.isnan(ticker.close) or (not math.isnan(ticker.bid) and not math.isnan(ticker.ask)):
            self.register_ticker(ticker)
        else:
            logger.error(f"Could not fetch ticker for {get_option_name(contract)}")
        return ticker
    """

    def cancel_market_data(self, contract):
        """Unsubscribe from market data updates for a given contract."""
        if not contract or not contract.conId:
            return

        if contract.conId in self.registered_contracts:
            ticker = self.ib.ticker(contract)
            if ticker:
                ticker.updateEvent -= self.on_ticker_update
                self.ib.cancelMktData(contract)

            self.registered_contracts.pop(contract.conId)
            logger.info(f"Unsubscribed from market data for {get_option_name(contract)}")

    def get_delta(self, option):
        ticker = self.get_ticker(option)
        delta = get_delta(ticker) if ticker else None
        if delta is None or math.isnan(delta):
            return ""
        return str(round(abs(delta), 3))

    def get_ask(self, option):
        ticker = self.get_ticker(option)
        if not ticker or math.isnan(ticker.ask) or ticker.ask < 0:
            return sys.float_info.max
        return ticker.ask

    def get_reference_price(self):
        self.load_config()
        if is_regular_hours():
            return self.get_spx_price()
        else:
            if self.alternative_valuation == "ES":
                return self.get_es_price()
            else: # SPY
                return self.get_spy_price() * 10

    async def get_spx_implied_volatility(self, right):
        if self.last_implied_volatility_calculation_time[right] < self.options_dump_time:
            self.last_implied_volatility[right] = 0.0

        """Calculate implied volatility for the requested side from ATM SPX options."""
        reference_price = self.get_reference_price()
        if math.isnan(reference_price):
            logger.error(f"The reference price is NaN")
            return self.last_implied_volatility[right]

        options_cache = OptionCache()
        options = options_cache.load_cached_options()
        if not options:
            logger.error("No options cached in options_cache")
            return self.last_implied_volatility[right]

        # Pick an option and check if it's expired
        sample_option = options[0]
        expiration_date = datetime.strptime(sample_option.lastTradeDateOrContractMonth, "%Y%m%d").date()
        now_nyc = datetime.now(new_york_timezone)

        if (expiration_date < now_nyc.date() or
                (expiration_date == now_nyc.date() and now_nyc.time() > REGULAR_HOURS_END_TIME)):
            logger.warning(f"Options in cache expire on {expiration_date}. Returning last implied volatility for {right}: {self.last_implied_volatility[right]}")
            return self.last_implied_volatility[right]

        # Find 5 closest ATM options for the requested side
        candidate_options = sorted((o for o in options if o.right == right), key=lambda o: abs(o.strike - reference_price))[:5]

        if not candidate_options:
            logger.error(f"At the money level could not be found for {right}")
            return self.last_implied_volatility[right]

        await self.request_snapshots(candidate_options)

        implied_volatility = math.nan
        for option in candidate_options:
            if not hasattr(option, "ticker"):
                logger.error(f"Option {get_option_name(option)} has no ticker field")
                continue
            if option.ticker is None:
                logger.error(f"Option {get_option_name(option)} has an empty ticker field")
                continue
            iv = get_implied_volatility(option.ticker)
            if not math.isnan(iv):
                implied_volatility = iv
                break

        if math.isnan(implied_volatility):
            logger.warning(f"Implied volatility missing for ATM {right} option. SPX: {reference_price}. Using last known: {self.last_implied_volatility[right]}")
            return self.last_implied_volatility[right]

        # Sanity checks
        if implied_volatility > 1.9:
            logger.error(f"Implied volatility ({right}) {implied_volatility:.3f} is too high (> 1.9), discarding. Fallback: {self.last_implied_volatility[right]}")
            return self.last_implied_volatility[right]

        if self.last_implied_volatility[right] > 0 and abs(implied_volatility - self.last_implied_volatility[right]) > 1.0:
            logger.error(f"IV jump too large for {right} ({self.last_implied_volatility[right]:.3f} -> {implied_volatility:.3f}), discarding.")
            return self.last_implied_volatility[right]

        if implied_volatility > 0.6:
            logger.info(f"High IV detected for {right}: {implied_volatility:.3f} at SPX: {reference_price}")

        self.last_implied_volatility[right] = implied_volatility
        self.last_implied_volatility_calculation_time[right] = current_time_of_the_day()
        return implied_volatility

    async def get_chains(self, underlying):
        write_heartbeat()
        await self.qualify([underlying])
        chains = await self.ib.reqSecDefOptParamsAsync(underlying.symbol, '', underlying.secType, underlying.conId)
        write_heartbeat()
        return chains

    async def qualify(self, contracts):
        return await self.ib.qualifyContractsAsync(*contracts)

    async def get_options(self, date):
        """Orchestrates the loading of options from cache or fetching them fresh."""
        options_cache = OptionCache()
        options = options_cache.load_cached_options()
        
        options_obtained = False
        reference_price = self.get_reference_price()

        if options:
            options = [] if options[0].lastTradeDateOrContractMonth != date else options
            if options:
                put_options = [option.strike for option in options if option.right == 'P']
                call_options = [option.strike for option in options if option.right == 'C']
                if put_options and call_options:
                    options_obtained = True
                    maximal_put_strike = max(put_options)
                    minimal_call_strike = min(call_options)
                    previous_spx_index_value = (maximal_put_strike + minimal_call_strike) / 2
                    
                    if math.isnan(reference_price):
                        logger.warning(f"The reference price for options categorization is missing")
                    else:
                        change_from_previous_spx_index_value = abs(reference_price - previous_spx_index_value)
                        if change_from_previous_spx_index_value / previous_spx_index_value > 0.015:
                            logger.info(f"Fetching option tickers as reference index price made a big change from {previous_spx_index_value} to {reference_price}")
                            options = []
                            options_obtained = False
                else:
                    logger.error(f"Options could not be obtained from cache, number of options is {len(options)}")

        if not options_obtained:
            logger.info(f"Fetching fresh options for {date}. Reference Price: {reference_price}")
            chains = await self.get_chains(self.spx)
            chain = next(c for c in chains if c.exchange == 'CBOE' and c.tradingClass == 'SPXW')
            
            put_options = []
            call_options = []
            for strike in chain.strikes:
                if strike < reference_price:
                    option = Option(symbol='SPX', lastTradeDateOrContractMonth=date, strike=strike, right='P',
                                    exchange='CBOE', currency='USD', tradingClass='SPXW')
                    put_options.append(option)
                else:
                    option = Option(symbol='SPXW', lastTradeDateOrContractMonth=date, strike=strike, right='C',
                                    exchange='CBOE', currency='USD', tradingClass='SPXW')
                    call_options.append(option)

            logger.info(f"Before contract qualification, number of call options: {len(call_options)}, number of put options: {len(put_options)}")
            await self.qualify(put_options + call_options)
            
            options = [o for o in (put_options + call_options) if o.conId]
            
            put_strikes = [o.strike for o in options if o.right == 'P']
            call_strikes = [o.strike for o in options if o.right == 'C']
            
            if put_strikes:
                logger.info(f"Minimal strike for put options: {min(put_strikes)}, Maximal strike for put options: {max(put_strikes)}")
                logger.info(f"Minimal strike for call options: {min(call_strikes)}, Maximal strike for call options: {max(call_strikes)}")
                options_cache.save(options)
                self.options_dump_time = time.time()
            else:
                logger.error(f"No put strikes found for {date}")

        if options:
            from .trading_bot import TradingBot
            await TradingBot().fetch_price_increments(options[0])

        assert options
        return options
