import math
import asyncio
import gspread
from ib_insync import *
from datetime import datetime

from utilities.ib_utils import connect
from utilities.database_access import SERVICE_ACCOUNT_FILE, get_worksheet

# --- CONFIGURATION ---
SYMBOLS = ['VT', 'AVUV', 'AVDV', 'VGT', 'UPRO', 'SP5Y', 'SPHD', 'SCHD', 'SCHY', 'VIG', 'VIGI']
SHEET_NAME = '$$$$'
UPDATE_INTERVAL_SECONDS = 60
QUOTE_UPDATER_CLIENT_ID = 3

# Map each symbol to its specific row in the spreadsheet (assuming start at row 4).
SYMBOL_ROW_MAP = {symbol: 4 + i for i, symbol in enumerate(SYMBOLS)}

# Track the absolute latest quotes coming in from IB
latest_quotes = {}

# Track what was previously sent to Google Sheets to detect changes
last_sent_prices = {symbol: None for symbol in SYMBOLS}


def on_pending_tickers(tickers):
    """
    Reactive callback triggered by ib_insync whenever new tick data arrives.
    We constantly overwrite our local dictionary with the latest valid price.
    """
    for ticker in tickers:
        symbol = ticker.contract.symbol

        # Only process if we have a valid, non-NaN price
        if not math.isnan(ticker.last) and ticker.last > 0:
            if symbol == "SP5Y" and latest_quotes.get("SP5Y", [0, 0])[1] != ticker.last:
                print(f"SP5Y: {ticker.last}")
            latest_quotes[symbol] = [symbol, ticker.last]


def update_google_sheet_sync(worksheet, updates_payload):
    """
    Synchronous function that uses batch_update to only touch specific rows.
    """
    if not updates_payload:
        return

    try:
        worksheet.batch_update(updates_payload)
        print(f"[{datetime.now().strftime('%X')}] Batch updated {len(updates_payload)} symbols with new prices.")
    except Exception as e:
        print(f"Failed to update Google Sheet: {e}")


async def periodic_sheet_updater(worksheet):
    """
    Background task that wakes up once a minute, compares current prices
    to the last sent prices, and only sends the changes.
    """
    loop = asyncio.get_running_loop()

    while True:
        await asyncio.sleep(UPDATE_INTERVAL_SECONDS)

        updates_payload = []

        # Loop through our strictly ordered symbols list
        for symbol in SYMBOLS:
            if symbol in latest_quotes:
                data = latest_quotes[symbol]
                current_price = data[1]  # Index 1 is ticker.last

                # Check if the price is new or different from the last sent price
                if current_price != last_sent_prices[symbol]:
                    row = SYMBOL_ROW_MAP[symbol]
                    updates_payload.append({
                        'range': f'A{row}',
                        'values': [data]
                    })
                    # Update our memory so we don't send this again unless it changes
                    last_sent_prices[symbol] = current_price

        # Execute the blocking gspread network call in a separate thread, only if there are changes
        if updates_payload:
            await loop.run_in_executor(None, update_google_sheet_sync, worksheet, updates_payload)


async def main():
    # 1. Initialize Google Sheets connection
    print("Authenticating with Google Sheets...")
    gc = gspread.service_account(filename=SERVICE_ACCOUNT_FILE)

    try:
        worksheet = get_worksheet(SHEET_NAME)
    except gspread.exceptions.SpreadsheetNotFound:
        print(
            f"Error: Could not find spreadsheet '{SHEET_NAME}'. Please create it and share it with the service account.")
        return

    # 2. Initialize IB connection
    print("Connecting to IB Gateway...")
    tws_connection = connect(QUOTE_UPDATER_CLIENT_ID)
    ib = tws_connection.ib

    # 3. Create contracts and request market data
    contracts = [Stock(symbol, 'SMART', 'USD') for symbol in SYMBOLS]

    # Update SP5Y to use the correct primary exchange
    if 'SP5Y' in SYMBOLS:
        sp5y_index = SYMBOLS.index('SP5Y')
        contracts[sp5y_index].primaryExchange = 'LSEETF'

    await ib.qualifyContractsAsync(*contracts)

    for contract in contracts:
        ib.reqMktData(contract, '', False, False)

    # 4. Attach the reactive callback to pending tickers
    ib.pendingTickersEvent += on_pending_tickers

    # 5. Start the background periodic updater task
    updater_task = asyncio.create_task(periodic_sheet_updater(worksheet))

    print(f"System active. Reacting to live quotes and updating sheet every {UPDATE_INTERVAL_SECONDS} seconds...")

    try:
        await asyncio.Future()
    except KeyboardInterrupt:
        print("Shutting down...")
    finally:
        updater_task.cancel()
        ib.disconnect()


if __name__ == '__main__':
    asyncio.run(main())