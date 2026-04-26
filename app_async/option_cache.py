import os
import pickle

from ib_insync import Index, Option
import logging

from utilities.utils import current_thread

file_path = "cache/option_store.pql"

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


class OptionCache:

    def __init__(self):
        self.market_data_fetcher = current_thread.market_data_fetcher

    def load(self, date):

        options = []
        spx = Index('SPX', 'CBOE', 'USD')
        spx_ticker = self.market_data_fetcher.req_mkt_data(spx)

        options_obtained = False
        if os.path.exists(file_path):
            try:
                with open(file_path, 'rb') as file:
                    options = pickle.load(file)
            except EOFError as e:
                logger.error(f"{e}")
                os.remove(file_path)
            options = [] if options and options[0].lastTradeDateOrContractMonth != date else options
            if options:
                put_options = [option.strike for option in options if option.right == 'P']
                call_options = [option.strike for option in options if option.right == 'C']
                if put_options and call_options:
                    options_obtained = True
                    maximal_put_strike = max(put_options)
                    minimal_call_strike = min(call_options)
                    previous_spx_index_value = (maximal_put_strike + minimal_call_strike) / 2
                    if spx_ticker is None:
                        logger.warning(f"The ticker of SPX index is missing")
                    else:
                        change_from_previous_spx_index_value = abs(spx_ticker.last - previous_spx_index_value)
                        if change_from_previous_spx_index_value / previous_spx_index_value > 0.015:
                            logger.info(f"Fetching option tickers as SPX index made a big change from {previous_spx_index_value} to {spx_ticker.last}")
                            options = []  # Let's get a new strike list based on an updated spx index value
                            options_obtained = False
                else:
                    logger.error(f"Options could not be obtained from cache, number of options is {len(options)}, "
                                 f"number of puts is {len(put_options)}, number of calls is {len(call_options)}")

        if not options_obtained:
            print(f"SPX Last Price: {spx_ticker.last}")
            chains = self.market_data_fetcher.get_chains(spx)
            chain = next(c for c in chains if c.exchange == 'CBOE' and c.tradingClass == 'SPXW')
            put_options = []
            call_options = []
            for strike in chain.strikes:
                if strike < spx_ticker.last:
                    option = Option(symbol='SPX', lastTradeDateOrContractMonth=date, strike=strike, right='P',
                                    exchange='CBOE', currency='USD', tradingClass='SPXW')
                    put_options.append(option)
                else:  # strike > spx_ticker.last:
                    option = Option(symbol='SPXW', lastTradeDateOrContractMonth=date, strike=strike, right='C',
                                    exchange='CBOE', currency='USD', tradingClass='SPXW')
                    call_options.append(option)

            self.market_data_fetcher.qualify(put_options + call_options)
            put_options = list(filter(lambda contract: contract.conId, put_options))
            call_options = list(filter(lambda contract: contract.conId, call_options))
            options = put_options + call_options
            put_strikes = [put_option.strike for put_option in put_options]
            call_strikes = [call_option.strike for call_option in call_options]
            if put_strikes:
                logger.info(
                    f"Minimal strike for put options: {min(put_strikes)}, Maximal strike for put options: {max(put_strikes)}")
                logger.info(
                    f"Minimal strike for call options: {min(call_strikes)}, Maximal strike for call options: {max(call_strikes)}")
            else:
                logger.error(f"No put strikes, size of put options: {len(put_options)}, size of chain.strikes: {len(chain.strikes)}, date: {date}")

            with open(file_path, "wb") as file:
                # noinspection PyTypeChecker
                pickle.dump(options, file)

        assert options
        return options

    def load_cached_options(self):
        try:
            with open(file_path, 'rb') as file:
                options = pickle.load(file)
                return options
        except FileNotFoundError:
            return []

