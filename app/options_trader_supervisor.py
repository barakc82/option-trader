import time
import os
import json
import logging
import traceback
import requests
import asyncio
from zoneinfo import ZoneInfo
from datetime import datetime

from utilities.utils import is_in_docker, acquire_single_instance_lock, SUCCESS, ERROR, REGULAR_HOURS_END_TIME, new_york_timezone
from .state_updater import update_supervisor_state_async, post_current_state

from .supervisor_utils import (
    send_telegram_message, count_text_in_file,
    find_latest_option_trader_log, store_platform_log,
    switch_supervisor_log, LOGS_DIR
)
from .supervisor_health import (
    analyze_option_trader_log, is_process_active, 
    check_ib_gateway_health, is_session_expired, 
    test_connection_to_platform, IBGATEWAY_RESTART_REQUIRED
)
from .supervisor_control import (
    start_option_trader, kill_option_trader, 
    restart_ibgateway, soft_restart
)

# Configure the root logger to catch logs from all modules
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Console handler
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_formatter = logging.Formatter('%(asctime)s %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
console_handler.setFormatter(console_formatter)

# File handler
file_handler = logging.FileHandler(f'{LOGS_DIR}/supervisor.log')
file_handler.setLevel(logging.INFO)
file_formatter = logging.Formatter('%(asctime)s %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
file_handler.setFormatter(file_formatter)

logger.addHandler(console_handler)
logger.addHandler(file_handler)

MONITOR_STATE = 1
RESTART_PLATFORM_STATE = 2
CANNOT_RESTART_OPTION_TRADER_STATE = 3
CANNOT_RESTART_TWS_STATE = 4

state = MONITOR_STATE
RESET_READY_TO_AUTHENTICATE_STATUS_URL = "https://auth-ready-server.onrender.com/set-status-ok"

last_update_time = 0
last_sunday_expiration_check_date = None

def monitor_option_trader():
    global state
    is_process_alive = is_process_active()
    if not is_process_alive:
        logger.warning(f"Option Trader is not alive.")

        kill_option_trader()
        analysis_result = analyze_option_trader_log()
        logger.info(f"Log analysis result after killing Option Trader: {analysis_result}")
        if analysis_result == IBGATEWAY_RESTART_REQUIRED:
            logger.info("Calling soft restart for IBGateway")
            soft_restart()

        asyncio.run(post_current_state({'status': 'Terminated'}))
        
        if is_session_expired():
            logger.warning("IBGateway session expired, switching to restart platform state")
            store_platform_log()
            set_switch_to_restart_platform_state()
            return
        
        start_result = start_option_trader()
        if start_result == ERROR:
            state = CANNOT_RESTART_OPTION_TRADER_STATE
            return
        
        send_telegram_message('Option Trader stopped, but TWS remains active - Option Trader successfully restarted')
    else:
        logger.info(f"Option Trader is alive.")
        global last_update_time
        if time.time() - last_update_time > 60:
            update_state('Active')
            last_update_time = time.time()
        
        latest_log = find_latest_option_trader_log()
        if latest_log:
            missing_deltas = count_text_in_file(latest_log, "No delta data was available for")
            if missing_deltas > 10:
                logger.warning("Excessive missing deltas detected, performing soft restart")
                soft_restart()

        check_for_manual_restart_request()

def check_for_manual_restart_request():
    global state
    config_path = "config/supervisor_config.json"
    if not os.path.exists(config_path): return

    try:
        with open(config_path, 'r') as f:
            config = json.load(f)

        if config.get('should_restart_option_trader') == 1:
            logger.info("Manual restart requested.")
            kill_option_trader()
            asyncio.run(post_current_state({'status': 'Restarting'}))
            
            if start_option_trader() == SUCCESS:
                config['should_restart_option_trader'] = 0
                with open(config_path, 'w') as f:
                    json.dump(config, f, indent=4)
                send_telegram_message("Option Trader successfully restarted via manual request.")
            else:
                state = CANNOT_RESTART_OPTION_TRADER_STATE
    except Exception as e:
        logger.error(f"Error checking manual restart: {e}")

def set_switch_to_restart_platform_state():
    global state
    logger.warning(f"Switching to RESTART_PLATFORM_STATE")
    state = RESTART_PLATFORM_STATE

def restart_platform():
    global state
    if is_in_docker():
        restart_ibgateway(post_current_state)
    
    kill_option_trader()
    start_option_trader()
    time.sleep(20)
    
    if is_process_active():
        send_telegram_message('Platform and Option Trader are back')
        while True:
            try:
                response = requests.get(RESET_READY_TO_AUTHENTICATE_STATUS_URL, timeout=10)
                if response.status_code == requests.codes.ok: break
            except Exception: pass
            time.sleep(5)
        state = MONITOR_STATE
    else:
        state = CANNOT_RESTART_OPTION_TRADER_STATE

def check_sunday_expiration():
    global last_sunday_expiration_check_date
    now_et = datetime.now(ZoneInfo("America/New_York"))
    current_date = now_et.date()

    if now_et.weekday() == 6 and now_et.hour >= 1:
        if last_sunday_expiration_check_date != current_date:
            logger.info("Executing weekly Sunday session check...")
            soft_restart()
            last_sunday_expiration_check_date = current_date
            return is_session_expired()
    return False

def update_state(status):
    asyncio.run(update_supervisor_state_async({'status': status}))

def check_log_size():
    global file_handler
    log_path = f'{LOGS_DIR}/supervisor.log'
    if os.path.exists(log_path) and os.path.getsize(log_path) > 10 * 1024 * 1024:
        logger.info("supervisor.log reached 10MB, switching logs...")
        
        logger.removeHandler(file_handler)
        file_handler.close()
        
        if switch_supervisor_log():
            print("Successfully trimmed supervisor.log")
        else:
            print("Failed to trim supervisor.log")
            
        file_handler = logging.FileHandler(log_path)
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(file_formatter)
        logger.addHandler(file_handler)
        logger.info("Log trimming process completed.")

def monitor(interval=5):
    global state
    while True:
        try:
            check_log_size()
            if state == MONITOR_STATE:
                monitor_option_trader()
                status = check_ib_gateway_health()
                if not status["is_healthy"]:
                    logger.error(f"ibgateway unhealthy: {status}")

                if check_sunday_expiration():
                    state = RESTART_PLATFORM_STATE

            elif state == RESTART_PLATFORM_STATE:
                restart_platform()
            elif state == CANNOT_RESTART_OPTION_TRADER_STATE:
                send_telegram_message('Option Trader cannot restart')
                state = MONITOR_STATE
        except Exception:
            logger.error(f"Error in monitor loop:\n{traceback.format_exc()}")
        time.sleep(interval)

if __name__ == "__main__":
    _lock = acquire_single_instance_lock(lock_path='cache/supervisor_script.lock', process_name='Supervisor')
    try:
        logger.info("Supervisor started")
        monitor()
    except Exception:
        logger.error(f"Critical error:\n{traceback.format_exc()}")
