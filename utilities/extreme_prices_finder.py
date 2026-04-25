import sys
from ib_insync import *
from datetime import datetime, timedelta

from utilities.database_access import get_worksheet

barak_sheet = get_worksheet("ברק")
quotes_sheet = get_worksheet("$$$$")

date_arr = barak_sheet.get("U47")
history_days_arr = barak_sheet.get("V51")
start_date_str = date_arr[0][0]
history_days = int(history_days_arr[0][0])
print(f"Start date: {start_date_str}")
print(f"History days: {history_days}")
start_date = datetime.strptime(start_date_str, "%d/%m/%y")

ib = IB()
ib.connect('127.0.0.1', 7496, clientId=10)

security_names = ['VT', 'AVUV', 'VGT', 'UPRO', 'SCHD', 'SCHY', 'SPHD', 'VIG', 'VIGI', 'INTU', 'MA', 'AXP', 'ASML', 'OXY']
row_indices = [177, 178, None, None, 182, 183, 184, 185, 186, 189, 194, 198, 201, 202]

start_date = datetime(2026, 4, 23)
history_days = 36

history_end_date = start_date - timedelta(days=1)
print(f"History end date: {history_end_date}")

history_start_date = history_end_date - timedelta(days=history_days-1)
print(f"History start date: {history_start_date}")

days = (datetime.now() - history_start_date).days

etfs_status_starting_row_index = 4
etfs_status = quotes_sheet.get(range_name=f"A{etfs_status_starting_row_index}:E13")


def update_short_historical_bounds():
    global update_data
    lowest_price = sys.float_info.max
    highest_price = 0
    for bar in bars:
        if bar.date < history_start_date.date():
            continue
        if bar.close < lowest_price:
            lowest_price = bar.close
        if bar.close > highest_price:
            highest_price = bar.close
    print(security_name, lowest_price, highest_price)
    update_data = [[highest_price, lowest_price]]
    barak_sheet.update(values=update_data, range_name=f"T{row_index}:U{row_index}")


def update_long_historical_bounds():
    global row_index, update_data
    last_bar = bars[-1]
    last_close = float(last_bar.close)
    date = last_bar.date.strftime('%d.%m.%y')
    print(f"Last close price for {security_name} is {last_close}, date: {date}")
    row_index = etfs_status_starting_row_index + etf_index
    update_data = [[last_close, date]]
    if last_close < float(etf_status_cells[2]):
        print(f"Updating minimal price for {security_name}")
        quotes_sheet.update(values=update_data, range_name=f"C{row_index}:D{row_index}")
    if last_close > float(etf_status_cells[4]):
        print(f"Updating maximal price for {security_name}")
        quotes_sheet.update(values=update_data, range_name=f"E{row_index}:F{row_index}")


for security_name, row_index in zip(security_names, row_indices):
    contract = Stock(security_name, 'SMART', 'USD')

    bars = ib.reqHistoricalData(
        contract,
        endDateTime='',            # now
        durationStr=f'{days} D',
        barSizeSetting='1 day',    # daily bars
        whatToShow='TRADES',       # use 'MIDPOINT' for forex etc.
        useRTH=True,               # regular trading hours only
        formatDate=1
    )

    if row_index is not None:
        update_short_historical_bounds()

    etf_index, etf_status_cells = next(((i, row) for i, row in enumerate(etfs_status) if row[0] == security_name), (None, None))
    if etf_status_cells is not None:
        update_long_historical_bounds()