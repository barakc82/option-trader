import time
import logging
import requests
import yfinance as yf
from datetime import datetime, timedelta
from utilities.database_access import get_worksheet

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

MAIN_SHEET              = '$$$$'
HEARTBEAT_FILE          = 'finance_updater/yahoo-heartbeat.txt'
ISRAELI_STOCK_IDS_FILE  = 'finance_updater/yahoo-israeli-stock-ids.txt'
ETF_NAMES_FILE          = 'finance_updater/yahoo-etf-names.txt'
ETF_NAMES_FOR_DIV_FILE  = 'finance_updater/yahoo-etf-names-for-div-yield.txt'
STOCK_NAMES_FILE        = 'finance_updater/yahoo-stock-names.txt'

ETF_START_ROW           = 15
SHILLER_PE_ROW          = 20
DIV_YIELD_START_ROW     = 45
ISRAELI_START_ROW       = 59

def read_ticker_names(filename):
    with open(filename, 'r') as f:
        return [line.strip() for line in f if line.strip()]


def heartbeat():
    with open(HEARTBEAT_FILE, 'w') as f:
        f.write(str(int(time.time() * 1000)))


def col_range(col, start_row, count):
    end_row = start_row + count
    return f'{col}{start_row}:{col}{end_row}'


def get_current_price(ticker_symbol):
    ticker = yf.Ticker(ticker_symbol)
    info = ticker.info
    price = info.get('regularMarketPrice') or info.get('currentPrice') or info.get('navPrice')
    return price


def get_dividend_sum(ticker_symbol):
    ticker = yf.Ticker(ticker_symbol)
    one_year_ago = datetime.now() - timedelta(days=365)
    dividends = ticker.dividends
    if dividends.empty:
        raise ValueError(f'No dividends for {ticker_symbol}')
    recent = dividends[dividends.index >= one_year_ago.strftime('%Y-%m-%d')]
    return float(recent.sum())


def get_israeli_stock_price(stock_id):
    """Fetch price from funder.co.il (mutual funds starting with '5') or TheMarker."""
    stock_id = stock_id.strip()
    if stock_id.startswith('5'):
        url = f'https://www.funder.co.il/fund/{stock_id}'
        html = requests.get(url, timeout=10).text
        parts = html.split('sellPrice')
        if len(parts) < 2:
            raise ValueError(f'Failed to parse funder price for {stock_id}')
        price_str = parts[1].split(',')[0].replace('":', '').strip()
        return float(price_str)

    url = f'https://finance.themarker.com/etf/{stock_id}'
    logger.info(url)
    html = requests.get(url, timeout=10).text
    parts = html.split('שער')
    if len(parts) < 2:
        raise ValueError(f'Failed to parse TheMarker price for {stock_id}')
    price_str = parts[1].split('>')[2].split('<')[0].replace(',', '')
    return float(price_str)


def get_shiller_pe():
    html = requests.get('https://www.multpl.com/shiller-pe', timeout=10).text
    parts = html.split('Current Shiller PE Ratio is ')
    if len(parts) < 2:
        raise ValueError('Failed to parse Shiller PE')
    return float(parts[1].split(',')[0])


def update_column(sheet_name, col, start_row, values):
    range_name = col_range(col, start_row, len(values))
    worksheet = get_worksheet(sheet_name)
    worksheet.update(range_name=range_name, values=[[v] for v in values], value_input_option='RAW')


def update_single(sheet_name, col, row, value):
    update_column(sheet_name, col, row, [value])


class FinanceUpdater:
    def __init__(self):
        self.prev_etf_quotes = None
        self.last_daily_run = None
        self.shiller_pe = 0

    def run(self):
        while True:
            heartbeat()
            now = datetime.now()
            logger.info(f'Starting update cycle at {now}')

            try:
                self._update_shiller_pe()
                self._update_israeli_stocks()
                self._update_daily_dividends_if_needed()

                is_short_sleep = self.prev_etf_quotes is None or (14 < now.hour < 23)
                sleep_seconds = 240 if is_short_sleep else 1800
                label = '4 minutes' if is_short_sleep else 'half an hour'
                logger.info(f'Sleeping for {label}')
                heartbeat()
                time.sleep(sleep_seconds)

            except IOError as e:
                logger.error(f'IO error: {e}')
                if '401' in str(e):
                    time.sleep(120)
                else:
                    time.sleep(3)
                logger.info('Retrying...')

            except Exception as e:
                logger.exception(f'Unexpected error: {e}')
                time.sleep(10)

    def _update_shiller_pe(self):
        try:
            pe = get_shiller_pe()
            if pe != self.shiller_pe:
                self.shiller_pe = pe
                update_single(MAIN_SHEET, 'F', SHILLER_PE_ROW, pe)
                logger.info(f'Shiller PE updated: {pe}')
        except Exception as e:
            logger.error(f'Failed to fetch Shiller PE: {e}')

    def _update_israeli_stocks(self):
        stock_ids = read_ticker_names(ISRAELI_STOCK_IDS_FILE)
        prices = []
        for stock_id in stock_ids:
            try:
                price = get_israeli_stock_price(stock_id)
                logger.info(f'{stock_id}: {price}')
                prices.append(price)
            except Exception as e:
                logger.error(f'Failed to fetch Israeli stock {stock_id}: {e}')
                prices.append('')
            time.sleep(0.2)
        update_column(MAIN_SHEET, 'B', ISRAELI_START_ROW, prices)

    def _update_daily_dividends_if_needed(self):
        today = datetime.now().date()
        if self.last_daily_run == today:
            return
        self.last_daily_run = today

        etfs = read_ticker_names(ETF_NAMES_FILE)
        self._store_total_dividends(etfs, MAIN_SHEET, 'K', ETF_START_ROW)

        stock_names = read_ticker_names(STOCK_NAMES_FILE)
        stocks_start_row = ETF_START_ROW + len(etfs) + 2
        self._store_total_dividends(stock_names, MAIN_SHEET, 'C', stocks_start_row)

        div_etfs = read_ticker_names(ETF_NAMES_FOR_DIV_FILE)
        yields = []
        for etf in div_etfs:
            try:
                div_sum = get_dividend_sum(etf)
                price = get_current_price(etf)
                yield_val = div_sum / price if price else 0
                logger.info(f'{etf} yield: {yield_val:.4f}')
                yields.append(yield_val)
            except Exception as e:
                logger.error(f'Failed to compute yield for {etf}: {e}')
                yields.append('')
            time.sleep(0.2)
        update_column(MAIN_SHEET, 'I', DIV_YIELD_START_ROW, yields)

    def _store_total_dividends(self, tickers, sheet, col, start_row):
        div_sums = []
        for ticker in tickers:
            try:
                div_sum = get_dividend_sum(ticker)
                div_sums.append(div_sum)
            except Exception as e:
                logger.error(f'Failed to get dividends for {ticker}: {e}')
                div_sums.append('')
            time.sleep(0.2)
        update_column(sheet, col, start_row, div_sums)


if __name__ == '__main__':
    updater = FinanceUpdater()
    updater.run()
