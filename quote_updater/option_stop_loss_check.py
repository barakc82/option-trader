"""
Standalone script: connects to IB Gateway/TWS and checks whether a stop loss
at a given limit price would have triggered at any point during an option's
last trading day, using historical ask-price ticks (closing a short position
means buying back at the ask).

Hard-coded to the SPX 7430 put expiring 2026-07-17. Edit the constants below
to point at a different contract or limit.

Usage:
    python quote_updater/option_stop_loss_check.py
"""
import time
from datetime import datetime

from ib_insync import IB, Option

from utilities.utils import new_york_timezone, REGULAR_HOURS_END_TIME

HOST = '127.0.0.1'
PORT = 7496  # 4001 if connecting to IB Gateway running in Docker
CLIENT_ID = 60

SYMBOL = 'SPX'
TRADING_CLASS = 'SPXW'
EXCHANGE = 'CBOE'
CURRENCY = 'USD'
STRIKE = 7430
RIGHT = 'P'
EXPIRY = '20260717'  # last trading day == expiration day for SPXW options

STOP_LOSS_LIMIT = 4.0  # trigger when the ask reaches/exceeds this price

WHAT_TO_SHOW = 'BID_ASK'
USE_RTH = False  # include pre/post-market ticks, not just regular trading hours
MAX_TICKS_PER_REQUEST = 1000  # IB's per-call cap for reqHistoricalTicks
SLEEP_BETWEEN_BATCHES = 1  # seconds; avoids IB pacing violations on repeated identical requests


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


def find_stop_loss_trigger(ib, contract):
    """Page reqHistoricalTicks forward from midnight to the expiration close, looking for
    the first ask tick at/above STOP_LOSS_LIMIT. Returns that tick, or None if never triggered."""
    expiry_date = datetime.strptime(EXPIRY, '%Y%m%d').date()
    day_start = new_york_timezone.localize(datetime.combine(expiry_date, datetime.min.time()))
    day_end = new_york_timezone.localize(datetime.combine(expiry_date, REGULAR_HOURS_END_TIME))

    cursor = day_start
    last_time = None
    ticks_scanned = 0

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
        ticks_scanned += len(new_ticks)

        for tick in new_ticks:
            if tick.priceAsk >= STOP_LOSS_LIMIT:
                print(f"Scanned {ticks_scanned} ticks before trigger.")
                return tick

        last_time = raw_ticks[-1].time
        print(f"Scanned {len(new_ticks)} new ticks up to {last_time} (ask still below {STOP_LOSS_LIMIT}); total scanned: {ticks_scanned}")

        if len(raw_ticks) < MAX_TICKS_PER_REQUEST:
            # Fewer than the max means IB has no more data after this batch
            break

        cursor = last_time
        time.sleep(SLEEP_BETWEEN_BATCHES)

    print(f"Scanned {ticks_scanned} ticks total; stop loss never triggered.")
    return None


def main():
    ib = IB()
    print(f"Connecting to IB on {HOST}:{PORT} (clientId={CLIENT_ID})...")
    ib.connect(HOST, PORT, clientId=CLIENT_ID)

    try:
        contract = build_contract()
        qualified = ib.qualifyContracts(contract)
        if not qualified:
            print("Failed to qualify contract. Check strike/expiry/right/exchange.")
            return
        contract = qualified[0]
        print(f"Qualified contract: {contract}")
        print(f"Checking for a stop loss trigger at ask >= {STOP_LOSS_LIMIT}...")

        trigger = find_stop_loss_trigger(ib, contract)

        if trigger:
            print(f"STOP LOSS WOULD HAVE TRIGGERED at {trigger.time}: ask={trigger.priceAsk}, bid={trigger.priceBid}")
        else:
            print("STOP LOSS WOULD NOT HAVE TRIGGERED.")
    finally:
        ib.disconnect()


if __name__ == '__main__':
    main()
