import math
import time
import gspread
from aiohttp.helpers import QUOTED_ETAG_RE
from ib_insync import *
from datetime import datetime

from yfinance.const import quote_summary_valid_modules

from utilities.ib_utils import connect
from utilities.database_access import SERVICE_ACCOUNT_FILE, get_worksheet

# --- CONFIGURATION ---
SYMBOLS = ['VT', 'AVUV', 'AVDV', 'VGT', 'UPRO', 'SP5Y', 'SPHD', 'SCHD', 'SCHY', 'VIG', 'VIGI']
QUOTED_SHEET_NAME = '$$$$'
LEVERAGE_SHEET_NAME = 'Barak-dollar-leverage'
UPDATE_INTERVAL_SECONDS = 60
QUOTE_UPDATER_CLIENT_ID = 4

SYMBOL_ROW_MAP = {symbol: 4 + i for i, symbol in enumerate(SYMBOLS)}

latest_quotes = {}
last_sent_prices = {symbol: None for symbol in SYMBOLS}


def on_pending_tickers(tickers):
    for ticker in tickers:
        symbol = ticker.contract.symbol

        if not math.isnan(ticker.last) and ticker.last > 0:
            if symbol == "SP5Y" and latest_quotes.get("SP5Y", [0, 0])[1] != ticker.last:
                print(f"SP5Y: {ticker.last}")
            latest_quotes[symbol] = [symbol, ticker.last]


def update_google_quotes_sheet_sync(worksheet, updates_payload):
    if not updates_payload:
        return

    try:
        worksheet.batch_update(updates_payload)
        print(f"[{datetime.now().strftime('%X')}] Batch updated {len(updates_payload)} symbols with new prices.")
    except Exception as e:
        print(f"Failed to update Google Sheet: {e}")


def periodic_sheet_updater(quotes_worksheet, leverage_worksheet):
    updates_payload = []
    changed_quotes = []

    for symbol in SYMBOLS:
        if symbol in latest_quotes:
            data = latest_quotes[symbol]
            current_price = data[1]

            if current_price != last_sent_prices[symbol]:
                row = SYMBOL_ROW_MAP[symbol]
                if last_sent_prices[symbol] is not None:
                    changed_quotes.append(symbol)
                updates_payload.append({
                    'range': f'A{row}',
                    'values': [data]
                })
                last_sent_prices[symbol] = current_price

    update_google_quotes_sheet_sync(quotes_worksheet, updates_payload)
    update_google_leverage_sheet_sync(leverage_worksheet, changed_quotes)


def update_google_leverage_sheet_sync(leverage_worksheet, changed_quotes):

    upro_quote = latest_quotes['UPRO'][1]
    sp5y_quote = latest_quotes['SP5Y'][1]
    print(f"upro_quote: {upro_quote}, sp5y_quote: {sp5y_quote}")
    if 'UPRO' in changed_quotes and 'SP5Y' in changed_quotes:
        leverage_worksheet.batch_update([
            {'range': 'X3', 'values': [[upro_quote]]},
            {'range': 'X4', 'values': [[sp5y_quote]]}
        ])


def main():
    # 1. Initialize Google Sheets connection
    print("Authenticating with Google Sheets...")
    gc = gspread.service_account(filename=SERVICE_ACCOUNT_FILE)

    try:
        quotes_worksheet = get_worksheet(QUOTED_SHEET_NAME)
        leverage_worksheet = get_worksheet(LEVERAGE_SHEET_NAME)
    except gspread.exceptions.SpreadsheetNotFound:
        print(f"Error: Could not find spreadsheet '{QUOTED_SHEET_NAME}'. Please create it and share it with the service account.")
        return

    # 2. Initialize IB connection
    print("Connecting to IB Gateway...")
    tws_connection = connect(QUOTE_UPDATER_CLIENT_ID)
    ib = tws_connection.ib

    # 3. Create contracts and request market data
    contracts = [Stock(symbol, 'SMART', 'USD') for symbol in SYMBOLS]

    if 'SP5Y' in SYMBOLS:
        sp5y_index = SYMBOLS.index('SP5Y')
        contracts[sp5y_index].primaryExchange = 'LSEETF'

    ib.qualifyContracts(*contracts)

    for contract in contracts:
        ib.reqMktData(contract, '', False, False)

    # 4. Attach the reactive callback to pending tickers
    ib.pendingTickersEvent += on_pending_tickers

    print(f"System active. Reacting to live quotes and updating sheet every {UPDATE_INTERVAL_SECONDS} seconds...")

    # 5. Run the event loop, waking up every UPDATE_INTERVAL_SECONDS to flush to Google Sheets
    last_update_time = time.time()

    try:
        while True:
            ib.sleep(1)

            if time.time() - last_update_time >= UPDATE_INTERVAL_SECONDS:
                periodic_sheet_updater(quotes_worksheet, leverage_worksheet)
                last_update_time = time.time()

    except KeyboardInterrupt:
        print("Shutting down...")
    finally:
        ib.disconnect()


if __name__ == '__main__':
    main()