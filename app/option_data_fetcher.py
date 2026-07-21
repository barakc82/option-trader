import asyncio
import math
import time
import logging
from datetime import datetime
from ib_insync import Option
from utilities.utils import *
from .option_cache import OptionCache
from .market_data_utils import get_implied_volatility

logger = logging.getLogger(__name__)

class OptionDataFetcher:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(OptionDataFetcher, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if not self._initialized:
            self.last_implied_volatility = {'C': 0.0, 'P': 0.0}
            self.last_implied_volatility_calculation_time = {'C': 0.0, 'P': 0.0}
            self._iv_reference_price_debug_count = 0
            self._initialized = True

    @property
    def mdf(self):
        from .market_data_fetcher import MarketDataFetcher
        return MarketDataFetcher()

    @property
    def ib(self):
        return self.mdf.ib

    async def get_chains(self, underlying):
        write_heartbeat()
        await self.mdf.qualify([underlying])
        chains = await self.ib.reqSecDefOptParamsAsync(underlying.symbol, '', underlying.secType, underlying.conId)
        write_heartbeat()
        return chains

    async def get_spx_implied_volatility(self, right):
        if self.last_implied_volatility_calculation_time[right] < self.mdf.options_dump_time:
            self.last_implied_volatility[right] = 0.0

        reference_price = self.mdf.get_reference_price()
        if self._iv_reference_price_debug_count < 10:
            self._iv_reference_price_debug_count += 1
            print(f"[get_spx_implied_volatility] call #{self._iv_reference_price_debug_count} ({right}): reference_price={reference_price}")

        if math.isnan(reference_price):
            logger.error(f"The reference price is NaN")
            return self.last_implied_volatility[right]

        options_cache = OptionCache()
        options = options_cache.load_cached_options()
        if not options:
            logger.error("No options cached in options_cache")
            return self.last_implied_volatility[right]

        sample_option = options[0]
        expiration_date = datetime.strptime(sample_option.lastTradeDateOrContractMonth, "%Y%m%d").date()
        now_nyc = datetime.now(new_york_timezone)

        if self._iv_reference_price_debug_count < 10:
            self._iv_reference_price_debug_count += 1
            print(f"[get_spx_implied_volatility] call #{self._iv_reference_price_debug_count} ({right}): last Trade Date = {sample_option.lastTradeDateOrContractMonth}")

        if (expiration_date < now_nyc.date() or
                (expiration_date == now_nyc.date() and now_nyc.time() > REGULAR_HOURS_END_TIME)):
            logger.warning(f"Options in cache expire on {expiration_date}. Returning last implied volatility for {right}: {self.last_implied_volatility[right]}")
            return self.last_implied_volatility[right]

        candidate_options = sorted((o for o in options if o.right == right), key=lambda o: abs(o.strike - reference_price))[:5]
        if not candidate_options:
            logger.error(f"At the money level could not be found for {right}")
            return self.last_implied_volatility[right]

        await self.mdf.request_snapshots(candidate_options)

        implied_volatility = math.nan
        for i, option in enumerate(candidate_options):
            if not hasattr(option, "ticker"):
                logger.error(f"Option {get_option_name(option)} has no ticker field")
                continue
            if option.ticker is None:
                logger.error(f"Option {get_option_name(option)} has an empty ticker field")
                continue
            iv = get_implied_volatility(option.ticker)
            if not math.isnan(iv):
                implied_volatility = iv
                logger.info(f"Found implied volatility using [{i}] {get_option_name(option)}: {implied_volatility:.2f}. Reference level: {reference_price}, options list: {[o for o in options if o.right == right][0].strike} -> {[o for o in options if o.right == right][-1].strike}")
                break

        if math.isnan(implied_volatility):
            logger.warning(f"Implied volatility missing for ATM {right} option. SPX: {reference_price}. Using last known: {self.last_implied_volatility[right]}")
            return self.last_implied_volatility[right]

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

    async def get_options(self, date):
        options_cache = OptionCache()
        options = options_cache.load_cached_options()

        options_obtained = False

        if options:
            options = [] if options[0].lastTradeDateOrContractMonth != date else options
            if options:
                put_options = [option.strike for option in options if option.right == 'P']
                call_options = [option.strike for option in options if option.right == 'C']
                if put_options and call_options:
                    options_obtained = True
                else:
                    logger.error(f"Options could not be obtained from cache, number of options is {len(options)}")

        if not options_obtained:
            logger.info(f"Fetching fresh options for {date}")
            chains = await self.get_chains(self.mdf.index_manager.spx)
            chain = next(c for c in chains if c.exchange == 'CBOE' and c.tradingClass == 'SPXW')

            put_options = []
            call_options = []
            all_options = []
            for strike in chain.strikes:
                option = Option(symbol='SPX', lastTradeDateOrContractMonth=date, strike=strike, right='P',
                                exchange='CBOE', currency='USD', tradingClass='SPXW')
                all_options.append(option)
                option = Option(symbol='SPXW', lastTradeDateOrContractMonth=date, strike=strike, right='P',
                                exchange='CBOE', currency='USD', tradingClass='SPXW')
                all_options.append(option)

                option = Option(symbol='SPX', lastTradeDateOrContractMonth=date, strike=strike, right='C',
                                exchange='CBOE', currency='USD', tradingClass='SPXW')
                all_options.append(option)
                option = Option(symbol='SPXW', lastTradeDateOrContractMonth=date, strike=strike, right='C',
                                exchange='CBOE', currency='USD', tradingClass='SPXW')
                all_options.append(option)

            options = []
            for i in range(0, len(all_options), 50):
                batch = all_options[i:i + 50]
                options.extend(await self.mdf.qualify(batch))
            put_strikes = [o.strike for o in options if o.right == 'P']
            call_strikes = [o.strike for o in options if o.right == 'C']

            if put_strikes:
                logger.info(f"Minimal strike for put options: {min(put_strikes)}, Maximal strike for put options: {max(put_strikes)}")
                logger.info(f"Minimal strike for call options: {min(call_strikes)}, Maximal strike for call options: {max(call_strikes)}")
                options_cache.save(options)
                self.mdf.options_dump_time = time.time()
            else:
                logger.error(f"No put strikes found for {date}")
            self.mdf.notify_switch_to_new_options()

        if options:
            from .trading_bot import TradingBot
            await TradingBot().fetch_price_increments(options[0])

        assert options
        return options
