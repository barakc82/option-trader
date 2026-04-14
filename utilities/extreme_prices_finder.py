import sys
from ib_insync import *
from datetime import datetime, timedelta

from utilities.database_access import get_worksheet

sheet = get_worksheet("ברק")
date_arr = sheet.get("U47")
history_days_arr = sheet.get("V51")
start_date_str = date_arr[0][0]
history_days = int(history_days_arr[0][0])
print(f"Start date: {start_date_str}")
print(f"History days: {history_days}")
start_date = datetime.strptime(start_date_str, "%d/%m/%y")

ib = IB()
ib.connect('127.0.0.1', 7496, clientId=10)

security_names = ['VT', 'AVUV', 'SCHD', 'SCHY', 'SPHD', 'VIG', 'VIGI', 'INTU', 'MA', 'AXP', 'ASML', 'OXY']
row_indices = [177, 178, 182, 183, 184, 185, 186, 189, 194, 198, 201, 202]

start_date = datetime(2026, 4, 13)
history_days = 34

history_end_date = start_date - timedelta(days=1)
print(f"History end date: {history_end_date}")

history_start_date = history_end_date - timedelta(days=history_days-1)
print(f"History start date: {history_start_date}")

days = (datetime.now() - history_start_date).days
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
    sheet.update(values=update_data, range_name=f"T{row_index}:U{row_index}")
