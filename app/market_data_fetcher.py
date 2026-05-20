import asyncio
import math
from ib_insync import Index

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
        logger.warning("Using model greeks to calculate implied volatility")
        return ticker.modelGreeks.impliedVol
    return math.nan

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
            self.last_implied_volatility = {'C': 0.0, 'P': 0.0}
            self.last_implied_volatility_calculation_time = {
                'C': current_time_of_the_day(),
                'P': current_time_of_the_day()
            }
            self.previous_spx_value = math.nan

            # Use a lock for market data type switching

            self._state_lock = asyncio.Lock()
            
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

    async def get_spx_price(self):
        spx = Index('SPX', 'CBOE', 'USD')
        spx_ticker = self.ib.ticker(spx)

        if not spx_ticker:
            spx_ticker = await self.req_mkt_data(spx)
            if not spx_ticker:
                logger.error("Could not obtain SPX ticker")
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

    def get_ticker(self, option):
        ticker = self.ib.ticker(option)
        if ticker:
            return ticker
        return getattr(option, 'ticker', None)

    def get_last_price(self, option):
        ticker = self.get_ticker(option)
        if not ticker or math.isnan(ticker.last):
            return math.nan
        return ticker.last

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

    def on_option_ticker_update(self, ticker):
        """Handle real-time updates for options, with throttling."""
        now = time.time()
        last_time = getattr(ticker, 'last_processed_time', 0)
        
        gamma = get_gamma(ticker)
        # Slower updates for low-gamma options (further out of money or illiquid)
        throttle_interval = 5.0 if (math.isnan(gamma) or gamma == 0.0) else 0.5
        
        if now - last_time < throttle_interval:
            return
        ticker.last_processed_time = now

        option = ticker.contract
        delta = get_delta(ticker)
        price = ticker.last

        delta_str = f"{abs(delta):.3f}" if delta is not None else "N/A"
        gamma_str = f"{gamma:.3f}" if not math.isnan(gamma) else "N/A"
        logger.info(f"Update: {get_option_name(option)} | Price: {price} | Delta: {delta_str} | Gamma: {gamma_str}")

        # Note: Subscription cleanup is handled separately or can be added here if highly selective.
        # Periodic cleanup is usually safer to avoid constant churning during volatile markets.

    async def update_ticker_data(self, contracts):
        """Qualify contracts and request fresh tickers for a batch of contracts."""
        if not contracts:
            return

        await self.qualify(contracts)
        await self.ensure_market_data_type()

        # Filter out what we already have active
        missing = [c for c in contracts if self.ib.ticker(c) is None]
        
        if missing:
            write_heartbeat()
            new_tickers = await self.ib.reqTickersAsync(*missing)
            write_heartbeat()
            for ticker in new_tickers:
                self._register_ticker(ticker)

        # Ensure all contracts have their internal ticker reference updated (for convenience)
        for contract in contracts:
            contract.ticker = self.ib.ticker(contract)

    async def req_mkt_data(self, contract, is_snapshot=False):
        """Request market data for a single contract using reqTickersAsync."""
        await self.qualify([contract])
        await self.ensure_market_data_type()
        
        tickers = await self.ib.reqTickersAsync(contract)
        ticker = tickers[0]
        self._register_ticker(ticker)
        return ticker

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

    async def get_spx_implied_volatility(self, right):
        if self.last_implied_volatility_calculation_time[right] < REGULAR_HOURS_END_TIME < current_time_of_the_day():
            self.last_implied_volatility[right] = 0.0

        """Calculate implied volatility for the requested side from ATM SPX options."""
        spx_price = await self.get_spx_price()
        if math.isnan(spx_price):
            logger.error("The SPX price is NaN")
            return self.last_implied_volatility[right]

        options_cache = OptionCache(self)
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
            logger.warning(f"Options in cache are expired ({expiration_date}). Returning last implied volatility for {right}: {self.last_implied_volatility[right]}")
            return self.last_implied_volatility[right]

        # Find ATM option for the requested side
        atm_option = min((o for o in options if o.right == right), key=lambda o: abs(o.strike - spx_price), default=None)

        if not atm_option:
            logger.error(f"At the money level could not be found for {right}")
            return self.last_implied_volatility[right]

        write_heartbeat()
        await self.update_ticker_data([atm_option])
        write_heartbeat()
        
        implied_volatility = get_implied_volatility(atm_option.ticker)

        if math.isnan(implied_volatility):
            logger.warning(f"Implied volatility missing for ATM {right} option. SPX: {spx_price}. Using last known: {self.last_implied_volatility[right]}")
            return self.last_implied_volatility[right]

        # Sanity checks
        if implied_volatility > 1.9:
            logger.error(f"Implied volatility ({right}) {implied_volatility:.3f} is too high (> 1.9), discarding. Fallback: {self.last_implied_volatility[right]}")
            return self.last_implied_volatility[right]

        if self.last_implied_volatility[right] > 0 and abs(implied_volatility - self.last_implied_volatility[right]) > 1.0:
            logger.error(f"IV jump too large for {right} ({self.last_implied_volatility[right]:.3f} -> {implied_volatility:.3f}), discarding.")
            return self.last_implied_volatility[right]

        if implied_volatility > 0.6:
            logger.info(f"High IV detected for {right}: {implied_volatility:.3f} at SPX: {spx_price}")

        self.last_implied_volatility[right] = implied_volatility
        self.last_implied_volatility_calculation_time[right] = current_time_of_the_day()
        return implied_volatility

    async def get_chains(self, underlying):
        await self.qualify([underlying])
        return await self.ib.reqSecDefOptParamsAsync(underlying.symbol, '', underlying.secType, underlying.conId)

    async def qualify(self, contracts):
        return await self.ib.qualifyContractsAsync(*contracts)
