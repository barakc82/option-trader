import os
import sys
import time
import json
import threading
import pandas as pd
import exchange_calendars as ecals

from datetime import datetime, timedelta, time as dt_time
from zoneinfo import ZoneInfo
from functools import cache

import pytz
import logging

# Market hours configuration using New York time
new_york_timezone = pytz.timezone('America/New_York')

# Utility to get NYSE calendar
@cache
def get_nyse_calendar():
    return ecals.get_calendar("XNYS")

# Precise session markers (NYC time)
# These are still used for internal logic relative to session boundaries
JUST_BEFORE_TRADE_END_TIME = dt_time(20, 10)
PREMARKET_START_TIME = dt_time(20, 15)  # 20:15 (usually for overnight/early start)
PREMARKET_END_TIME = dt_time(9, 25)  # 16:25
REGULAR_HOURS_START_TIME = dt_time(9, 30)  # 16:30
LATE_REGULAR_HOURS_START_TIME = dt_time(14, 30)  # 21:30
REDUCE_SAFE_CUSHION_TIME = dt_time(15, 40)  # 22:40
NEW_OPTION_EXPLORATION_START_TIME = dt_time(15, 55)  # 22:55
REGULAR_HOURS_END_TIME = dt_time(16, 00)  # 23:00
EARLY_CLOSING_END_TIME = dt_time(16, 15)  # 23:15
AFTER_HOURS_END_TIME = dt_time(17, 0)  # 00:00
JUST_AFTER_TRADE_END_TIME = dt_time(17, 5)

MY_ACCOUNT = 'U15897350'

SUCCESS = 0
ERROR = 1

SAFEGUARD_MAX_CADENCE = 1.0

log_file_name = datetime.now().strftime("logs\\option_trader_%Y-%m-%d_%H-%M-%S.log")
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

current_thread = threading.local()


def current_time_of_the_day() -> float:
    return datetime.now(new_york_timezone).timestamp()

def get_current_trading_day():
    """Returns the YYYYMMDD string of the current or next trading day."""
    cal = get_nyse_calendar()
    now_in_nyc = datetime.now(new_york_timezone)
    
    # If we are in the 'overnight' window for the next day's trades
    if NEW_OPTION_EXPLORATION_START_TIME < now_in_nyc.time() < AFTER_HOURS_END_TIME:
        # Move to next trading day
        next_session = cal.next_open(now_in_nyc)
        return next_session.strftime('%Y%m%d')
    
    # If currently a trading day, return it, else return the next one
    if cal.is_session(now_in_nyc.date().strftime('%Y-%m-%d')):
        return now_in_nyc.strftime('%Y%m%d')
    
    return cal.next_open(now_in_nyc).strftime('%Y%m%d')


def is_trade_cancelled(trade_result):
    return trade_result.orderStatus.status in ['Cancelled', 'Inactive']


def get_option_name(option):
    return f"{option.right} {option.strike}"


def is_reduced_safe_cushion_time():
    now_in_nyc = datetime.now(new_york_timezone).time()
    return is_market_open() and REDUCE_SAFE_CUSHION_TIME < now_in_nyc < NEW_OPTION_EXPLORATION_START_TIME


def is_day_break():
    now_in_nyc = datetime.now(new_york_timezone).time()
    return PREMARKET_END_TIME < now_in_nyc < REGULAR_HOURS_START_TIME


def is_night_break():
    now_in_nyc = datetime.now(new_york_timezone).time()
    return AFTER_HOURS_END_TIME < now_in_nyc < PREMARKET_START_TIME


def is_regular_hours():
    now_in_nyc = datetime.now(new_york_timezone).time()
    return is_market_open() and REGULAR_HOURS_START_TIME < now_in_nyc < REGULAR_HOURS_END_TIME


def is_switched_to_overnight_trading():
    now_in_nyc = datetime.now(new_york_timezone).time()
    return NEW_OPTION_EXPLORATION_START_TIME < now_in_nyc < AFTER_HOURS_END_TIME


def is_regular_hours_with_after_hours():
    now_in_nyc = datetime.now(new_york_timezone).time()
    return is_market_open() and REGULAR_HOURS_START_TIME < now_in_nyc < AFTER_HOURS_END_TIME


def is_final_hours():
    now_in_nyc = datetime.now(new_york_timezone).time()
    return is_market_open() and LATE_REGULAR_HOURS_START_TIME < now_in_nyc < AFTER_HOURS_END_TIME


def is_late_regular_hours():
    now_in_nyc = datetime.now(new_york_timezone).time()
    return is_market_open() and LATE_REGULAR_HOURS_START_TIME < now_in_nyc < REGULAR_HOURS_END_TIME


def is_after_hours():
    now_in_nyc = datetime.now(new_york_timezone).time()
    return is_market_open() and REGULAR_HOURS_END_TIME < now_in_nyc < AFTER_HOURS_END_TIME


def is_early_closing_hours():
    now_in_nyc = datetime.now(new_york_timezone).time()
    return is_market_open() and REGULAR_HOURS_END_TIME < now_in_nyc < EARLY_CLOSING_END_TIME


def get_elapsed_day_fraction():
    if is_after_hours():
        return 0

    now_in_nyc = datetime.now(new_york_timezone)
    today_target = datetime.combine(now_in_nyc.date(), PREMARKET_START_TIME, tzinfo=ZoneInfo("America/New_York"))

    # if now is before 20:15, the previous 20:15 was yesterday
    if now_in_nyc < today_target:
        previous_target = today_target - timedelta(days=1)
    else:
        previous_target = today_target

    start_to_now_time = now_in_nyc - previous_target
    start_to_end_time = timedelta(hours=20, minutes=10)  # 20:15 → 16:25 next day
    assert start_to_now_time <= start_to_end_time

    return start_to_now_time / start_to_end_time


def is_weekend_break():
    now_in_nyc = datetime.now(new_york_timezone)
    return not is_market_open() and now_in_nyc.weekday() in [5, 6]


def is_market_open():
    """Checks if NYSE is currently open using defined times and exchange_calendars for holidays."""
    cal = get_nyse_calendar()
    now_in_nyc = datetime.now(new_york_timezone)
    now_date_str = now_in_nyc.date().strftime('%Y-%m-%d')
    
    # Holiday check: if today is a session day on NYSE
    is_holiday = not cal.is_session(now_date_str)
    
    now_time = now_in_nyc.time()
    weekday = now_in_nyc.weekday()
    
    # Day session: Mon-Fri, early morning or late afternoon
    in_day_session = (weekday in range(5) and
                      (now_time < PREMARKET_END_TIME or REGULAR_HOURS_START_TIME < now_time < AFTER_HOURS_END_TIME))
    
    # Evening session start (Sundays/Mondays etc at 20:15)
    previous_weekday = (weekday - 1) % 7
    in_evening_session = previous_weekday in [6, 0, 1, 2, 3] and now_time >= PREMARKET_START_TIME
    
    if is_holiday and not in_evening_session:
        return False
        
    return in_day_session or in_evening_session

def is_buffer_time_around_trade_time():
    now_in_nyc = datetime.now(new_york_timezone).time()
    return (JUST_BEFORE_TRADE_END_TIME < now_in_nyc < PREMARKET_START_TIME) or \
            (JUST_AFTER_TRADE_END_TIME < now_in_nyc < AFTER_HOURS_END_TIME)

def fetch_next_client_id():
    client_id_file_path = "cache/client_id.txt"
    try:
        # Try reading the current client ID from file
        with open(client_id_file_path, 'r') as file:
            content = file.read().strip()
            client_id = int(content) if content else 0
    except FileNotFoundError:
        # If the file doesn't exist, initialize client_id to 0
        client_id = 0

        # Increment the client ID
    client_id += 1

    # Write the new client ID back to the file (creates file if missing)
    with open(client_id_file_path, 'w') as file:
        file.write(str(client_id))

    return client_id


@cache
def is_in_docker() -> bool:
    # Docker creates /.dockerenv and cgroup entries
    if os.path.exists('/.dockerenv'):
        return True
    try:
        with open('/proc/1/cgroup', 'rt') as f:
            return any('docker' in line or 'containerd' in line for line in f)
    except Exception:
        return False


def write_heartbeat():
    hb_path = "cache/heartbeat.txt"
    temp_path = hb_path + ".tmp"
    try:
        os.makedirs("cache", exist_ok=True)
        with open(temp_path, "w") as file:
            heartbeat = {'timestamp': time.time(), 'pid': os.getpid()}
            json.dump(heartbeat, file, indent=4)
        # Atomic rename ensures the supervisor never reads a partially written file
        os.replace(temp_path, hb_path)
    except Exception:
        # Fallback if rename fails for some reason
        if os.path.exists(temp_path):
            os.remove(temp_path)

def acquire_single_instance_lock(lock_path, process_name):
    """
    Attempts to acquire an OS-level lock to prevent multiple instances.
    Works on both Windows and Unix.
    """
    if sys.platform == 'win32':
        import msvcrt
        try:
            lock_file = open(lock_path, 'w')
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            return lock_file
        except (IOError, OSError):
            logger.warning(f"Another instance of {process_name} is already running. Exiting.")
            sys.exit(0)
    else:
        import fcntl
        try:
            lock_file = open(lock_path, 'w')
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return lock_file
        except BlockingIOError:
            logger.warning(f"Another instance of {process_name} is already running. Exiting.")
            sys.exit(0)

