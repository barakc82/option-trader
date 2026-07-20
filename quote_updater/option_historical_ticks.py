"""
Standalone script: connects to IB Gateway/TWS and dumps the full historical
tick history for a single option contract on its last trading day.

Hard-coded to the SPX 7430 put expiring 2026-07-17. Edit the constants below
to point at a different contract.

Usage:
    python quote_updater/option_historical_ticks.py
"""
import csv
import sys
from datetime import datetime, timedelta, time as dt_time
import pytz

from ib_insync import IB, Option

REGULAR_HOURS_END_TIME = dt_time(16, 00)  # 23:00
new_york_timezone = pytz.timezone('America/New_York')
HOST = '127.0.0.1'
PORT = 7496  # 4001 if connecting to IB Gateway running in Docker
CLIENT_ID = 59

SYMBOL = 'SPX'
TRADING_CLASS = 'SPXW'
EXCHANGE = 'CBOE'
CURRENCY = 'USD'
STRIKE = 7430
RIGHT = 'P'
EXPIRY = '20260717'  # last trading day == expiration day for SPXW options

WHAT_TO_SHOW = 'BID_ASK'  # also: 'TRADES', 'MIDPOINT'
USE_RTH = False  # include pre/post-market ticks, not just regular trading hours
MAX_TICKS_PER_REQUEST = 1000  # IB's per-call cap for reqHistoricalTicks
OUTPUT_CSV = f'quote_updater/historical_ticks_{SYMBOL}_{RIGHT}{STRIKE}_{EXPIRY}.csv'


def build_contract():
    return Option(
        symbol=SYMBOL,
        lastTradeDateOrContractMonth=EXPIRY,
        strike=STRIKE,
        right=RIGHT,
        exchange=EXCHANGE,
        currency=CURRENCY,
        tradingClass=TRADING_CLASS,
    )


def traverse_day(ib, contract):
    """Page reqHistoricalTicks forward from midnight to the expiration close, returning all ticks in order."""
    expiry_date = datetime.strptime(EXPIRY, '%Y%m%d').date()
    day_start = new_york_timezone.localize(datetime.combine(expiry_date, datetime.min.time()))
    day_end = new_york_timezone.localize(datetime.combine(expiry_date, REGULAR_HOURS_END_TIME))

    all_ticks = []
    cursor = day_start
    last_time = None

    while cursor < day_end:
        raw_ticks = ib.reqHistoricalTicks(
            contract,
            startDateTime=cursor,
            endDateTime='',
            numberOfTicks=MAX_TICKS_PER_REQUEST,
            whatToShow=WHAT_TO_SHOW,
            useRth=USE_RTH,
        )
        if not raw_ticks:
            print(f"No ticks returned starting at {cursor}. Stopping.")
            break

        new_ticks = [t for t in raw_ticks if last_time is None or t.time > last_time]
        all_ticks.extend(new_ticks)

        last_time = raw_ticks[-1].time
        print(f"Fetched {len(raw_ticks)} ticks (added {len(new_ticks)} new), up to {last_time}; total so far: {len(all_ticks)}")

        if len(raw_ticks) < MAX_TICKS_PER_REQUEST:
            # Fewer than the max means IB has no more data after this batch
            break

        cursor = last_time

    return all_ticks


def write_csv(ticks, path):
    if not ticks:
        print("No ticks retrieved; nothing to write.")
        return

    fields = list(ticks[0]._fields)
    with open(path, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(fields)
        for tick in ticks:
            writer.writerow([getattr(tick, field) for field in fields])

    print(f"Wrote {len(ticks)} ticks to {path}")


def main():
    ib = IB()
    print(f"Connecting to IB on {HOST}:{PORT} (clientId={CLIENT_ID})...")
    ib.connect(HOST, PORT, clientId=CLIENT_ID)

    try:
        contract = build_contract()
        qualified = ib.qualifyContracts(contract)
        if not qualified:
            print("Failed to qualify contract. Check strike/expiry/right/exchange.")
            sys.exit(1)
        contract = qualified[0]
        print(f"Qualified contract: {contract}")

        ticks = traverse_day(ib, contract)
        write_csv(ticks, OUTPUT_CSV)
    finally:
        ib.disconnect()


if __name__ == '__main__':
    main()
