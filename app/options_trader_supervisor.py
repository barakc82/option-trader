import shutil
import time
import os
import glob
import random
import json
import logging
import subprocess
from zoneinfo import ZoneInfo
import traceback
import requests
from pathlib import Path

from utilities.utils import is_in_docker, acquire_single_instance_lock
from app.state_updater import update_supervisor_state, post_current_state

# Create a logger
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

OPTION_TRADER_DIR = "/home/option-trader"
LOGS_DIR = f"{OPTION_TRADER_DIR}/logs"

# Console handler (prints to stdout)
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_formatter = logging.Formatter('%(asctime)s %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
console_handler.setFormatter(console_formatter)

# File handler (logs to a file)
file_handler = logging.FileHandler(f'{LOGS_DIR}/supervisor.log')
file_handler.setLevel(logging.INFO)
file_formatter = logging.Formatter('%(asctime)s %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
file_handler.setFormatter(file_formatter)

# Add handlers to the logger
logger.addHandler(console_handler)
logger.addHandler(file_handler)

MONITOR_STATE = 1
RESTART_PLATFORM_STATE = 2
CANNOT_RESTART_OPTION_TRADER_STATE = 3
CANNOT_RESTART_TWS_STATE = 4

state = MONITOR_STATE
OPTION_TRADER_SUPERVISOR_CLIENT_ID = 100
TWS_PROCESS_CMD = ["C:\\jts\\tws.exe", "main.py"]
READY_TO_AUTHENTICATE_STATUS_URL = "https://auth-ready-server.onrender.com/status"
RESET_READY_TO_AUTHENTICATE_STATUS_URL = "https://auth-ready-server.onrender.com/set-status-ok"

TELEGRAM_BOT_TOKEN = '8161204170:AAGRCLXSgBzmhukhFPlTTnAXeagv7LJmE3o'
TELEGRAM_CHAT_ID = '1796107185'

last_update_time = 0
start_time = 0
last_sunday_expiration_check_date = None

mem_limit_mb = 0
"""Reads the actual memory limit set for the Docker container."""
paths = ['/sys/fs/cgroup/memory/memory.limit_in_bytes', '/sys/fs/cgroup/memory.max']
for path in paths:
    if os.path.exists(path):
        with open(path, 'r') as f:
            limit = f.read().strip()
            if limit and limit != "max":
                mem_limit_mb = int(limit) / (1024 * 1024)  # Convert to MB

import datetime


def count_text_in_file(path, text):
    count = 0
    # 1. Define the time threshold (2 hours ago)
    now = datetime.datetime.now()
    threshold = now - datetime.timedelta(hours=2)

    # 2. Match your date format: 2026-04-15 14:12:15
    date_format = "%Y-%m-%d %H:%M:%S"

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                # Based on your format: [%(levelname)s] %(asctime)s - ...
                # We find the part after the first ']'
                # Example line: [INFO] 2026-04-15 13:00:00 - Main: process started
                parts = line.split('] ', 1)
                if len(parts) < 2:
                    continue

                # Extract the first 19 characters (the length of your datefmt)
                timestamp_str = parts[1][:19]

                # 3. Convert string to datetime object
                log_time = datetime.datetime.strptime(timestamp_str, date_format)

                # 4. Only count if the log time is within the last 2 hours
                if log_time >= threshold:
                    count += line.count(text)

            except (ValueError, IndexError):
                # This handles empty lines or lines without a valid timestamp
                continue

    return count

def find_latest_option_trader_log():
    directory = Path(LOGS_DIR)

    # Find all matching files
    log_files = list(directory.glob("option_trader*.log"))
    if not log_files:
        return None

    # Return the file with the newest modification time
    latest = max(log_files, key=lambda f: f.stat().st_mtime)
    return latest


def test_connection_to_platform():
    start_time = time.time()
    is_resolution_required = False
    retry_scripts = ["reconnectdata", "reconnectdata", "restart"]
    for attempt_index in range(3):
        # while time.time() - start_time < 1200:
        try:
            if is_resolution_required:
                logger.error(f"Connection to trader station platform restored")
            return True
        except ConnectionError as e:
            logger.error(f"Connection Error: trader station platform is down - {e} {type(e).__name__}")
            time.sleep(2)
            is_resolution_required = True

        retry_script = f"/home/ibgateway/ibc/{retry_scripts[attempt_index]}.sh"
        logger.info(f"Calling {retry_script} ...")
        result = subprocess.run([retry_script], capture_output=True, text=True)
        if result.returncode == 0:
            logger.info(f"{retry_scripts[attempt_index]}.sh finished successfully")
        else:
            logger.info(f"{retry_scripts[attempt_index]}.sh failed with code {result.returncode}")
            logger.info(result.stderr)

        time.sleep(60)

    send_telegram_message("Supervisor failed to connect to platform")

    return False

"""
def start_tws():
    logger.info("Restarting TWS...")
    tws_process = subprocess.Popen(TWS_PROCESS_CMD)
    login_window = None
    for _ in range(180):
        from pywinauto import Desktop
        windows = Desktop(backend="uia").windows()
        for window in windows:
            if window.process_id() == tws_process.pid:
                logger.info(f"Found window: {window.window_text()}")
                if window.window_text() == "Login":
                    logger.info("TWS login window is ready")
                    login_window = window
                    break
        if login_window:
            break
        time.sleep(1)

    return {"login_window": login_window, "tws_pid": tws_process.pid}
"""

def start_option_trader():
    logger.info("Restarting process...")
    if is_in_docker():
        option_trader_start_command = ["python3", "-m", "app.main"]
    else:
        option_trader_start_command = ["..\\.venv\\Scripts\\python.exe", "-m", "app.main"]
    p = subprocess.Popen(option_trader_start_command)
    option_trader_process = psutil.Process(p.pid)

    for _ in range(10):
        if option_trader_process.is_running():
            break
        time.sleep(1)
    if option_trader_process.is_running():
        logger.info(f"Process restarted, pid: {p.pid}. Waiting for the pid to be stored in the heartbeat file")
        try:
            for _ in range(10):
                with open(f"{OPTION_TRADER_DIR}/cache/heartbeat.txt", "r") as file:
                    heartbeat = json.load(file)
                    heartbeat_pid = heartbeat['pid']
                    if heartbeat_pid == option_trader_process.pid:
                        logger.info(f"The required pid was found in the heartbeat file")
                        break
                time.sleep(5)
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in heartbeat file: {e}")
    else:
        logger.info("Process did not restart")


def kill_option_trader():
    with open(f"{OPTION_TRADER_DIR}/cache/heartbeat.txt", "r") as file:
        try:
            heartbeat = json.load(file)
            pid = heartbeat['pid']
            if psutil.pid_exists(pid):
                proc = psutil.Process(pid)
                proc.terminate()
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in heartbeat file: {e}")


def store_platform_log():
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    shutil.copy('/home/ibgateway/ibgateway.log', f'/home/option-trader/logs/ibgateway_{timestamp}.log')
    shutil.copy('/home/ibgateway/Jts/launcher.log', f'/home/option-trader/logs/launcher_{timestamp}.log')


def is_session_expired():
    # The two target phrases
    auth_text = "Authentication completed"
    expired_text = "The security tokens associated with your login credentials have expired"

    # 1. Gather all launcher log files
    log_pattern = "/home/ibgateway/Jts/launcher*.log"
    log_files = glob.glob(log_pattern)

    # 2. Sort files by modification time, descending (newest first)
    # In your environment, launcher.log (Mar 30 01:55) will be first,
    # followed by 20260329, 20260328, etc.
    log_files.sort(key=os.path.getmtime, reverse=True)

    for file_path in log_files:
        if not os.path.exists(file_path):
            continue

        try:
            with open(file_path, 'r', encoding='utf-8') as file:
                # Read all lines into memory (safe given your max file size is ~256KB)
                lines = file.readlines()

                # 3. Iterate through the lines in reverse order (bottom to top)
                for line in reversed(lines):
                    if expired_text in line:
                        logger.warning(f"Session expired text was found in {file_path}")
                        return True
                    if auth_text in line:
                        logger.info(f"Authentication completed text was found in {file_path}")
                        return False

        except Exception as e:
            logger.warning(f"Warning: Could not read {file_path} due to {e}")

    # If neither text is found in any of the logs, default to False
    logger.info(f"No text was found in the Jts log files")
    return False


def soft_restart():
    logger.info("Running 'restart.sh'")
    subprocess.run(['/home/ibgateway/ibc/restart.sh'], check=True)
    time.sleep(5)


def monitor_option_trader():
    is_process_alive = is_process_active()
    if not is_process_alive:
        logger.warning(f"Option Trader is not alive.")
        kill_option_trader()
        post_current_state({'status': 'Terminated'})
        is_session_expired_result = is_session_expired()
        if is_session_expired_result:
            logger.warning("IBGateway session expired, switching to restart platform state")
            store_platform_log()
            set_switch_to_restart_platform_state()
            return
        latest_log = find_latest_option_trader_log()
        is_connection_error = count_text_in_file(latest_log, " Cannot overcome connection error, exiting")
        start_option_trader()
        time.sleep(20)
        if not is_process_active():
            state = CANNOT_RESTART_OPTION_TRADER_STATE
            return
        if is_process_active() and not is_process_active():
            message = 'Option Trader stopped, but TWS remains active - Option Trader successfully restarted'
            send_telegram_message(message)
    else:
        logger.info(f"Option Trader is alive.")
        global last_update_time
        if time.time() - last_update_time > 60:
            update_state('Active')
            last_update_time = time.time()
        latest_log = find_latest_option_trader_log()
        number_of_missing_delta_occurrences = count_text_in_file(latest_log, "No delta data was available for")
        if number_of_missing_delta_occurrences > 10:
            logger.warning("It seems that option trader has difficulties getting delta data, switching to restart"
                           "platform state")
            soft_restart()
            # set_switch_to_restart_platform_state()


def set_switch_to_restart_platform_state():
    global state
    logger.warning(f"Switching to 'wait for user to be ready for login' state")
    state = RESTART_PLATFORM_STATE

"""
def send_whatsapp(message):
    PHONE_NUMBER_ID = 837497922783970
    ACCESS_TOKEN = "<YOUR_ACCESS_TOKEN>"
    RECIPIENT_NUMBER = "972528268777"  # e.g. "972501234567"

    url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"

    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

    data = {
        "messaging_product": "whatsapp",
        "to": RECIPIENT_NUMBER,
        "type": "text",
        "text": {
            "body": "Hello! This is a message sent automatically using the WhatsApp Cloud API 🤖💬"
        }
    }

    response = requests.post(url, headers=headers, json=data)

    print("Status:", response.status_code)
    print("Response:", response.text)
"""

def check_process_state(process, should_print_health=False):
    status = process.status()
    if status == psutil.STATUS_ZOMBIE:
        logger.error("PROBLEM: Process is a ZOMBIE (terminated but not reaped).")
    elif status == psutil.STATUS_DISK_SLEEP:
        logger.warning("WARNING: Process is in UNINTERRUPTIBLE SLEEP (likely stuck on I/O or Network).")

    # 2. Check Resources
    mem_mb = process.memory_info().rss / (1024 * 1024)
    cpu_pct = process.cpu_percent(interval=0.1)

    # Memory Warning (if usage > 85% of container limit)
    global mem_limit_mb
    if mem_limit_mb and (mem_mb / mem_limit_mb) > 0.85:
        logger.warning(f"CRITICAL: Memory near limit! {mem_mb:.1f}MB / {mem_limit_mb:.1f}MB")

    # CPU Warning (if pegged at 100% - likely an infinite loop)
    if cpu_pct > 95.0:
        logger.warning(f"⚠️ WARNING: High CPU usage detected: {cpu_pct}%")

    if random.random() < 0.01 or should_print_health:
        logger.info(f"Subprocess Health - CPU: {cpu_pct}% | MEM: {mem_mb:.2f} MB")
        if should_print_health:
            logger.info(f"Subprocess status {status}")

def is_process_active(debug=False):
    last_ping = 0
    last_pid = 0
    for _ in range(24):
        try:
            with open(f"{OPTION_TRADER_DIR}/cache/heartbeat.txt", "r") as file:
                heartbeat = json.load(file)
                pid = heartbeat['pid']
                timestamp = heartbeat['timestamp']
                if timestamp:
                    last_ping = float(timestamp)
                if pid:
                    try:
                        option_trader_process = psutil.Process(pid)
                        check_process_state(option_trader_process)
                        last_pid = pid
                    except psutil.NoSuchProcess:
                        logger.error(f"Could not find process {pid}")

            time_since_last_ping = time.time() - last_ping
            is_process_alive = time_since_last_ping < 60
            if not is_process_alive:
                logger.error(f"No heartbeat from option trader, time since last ping: {time_since_last_ping:.0f}s")
                if last_pid:
                    option_trader_process = psutil.Process(last_pid)
                    check_process_state(option_trader_process, should_print_health=True)
            return is_process_alive
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in heartbeat file: {e}")
            time.sleep(5)

    logger.error(f"No heartbeat from option trader")
    return False

"""
def login_tws(start_tws_data):
    login_window = start_tws_data["login_window"]
    tws_pid = start_tws_data["tws_pid"]
    time.sleep(0.5)
    login_window.set_focus()
    login_window.type_keys("barakc1982{TAB}ufekze96{ENTER}", with_spaces=True)
    sent_notification_window = None
    for _ in range(30):
        from pywinauto import Desktop
        windows = Desktop(backend="uia").windows()
        for window in windows:
            if window.process_id() == tws_pid:
                logger.info(f"Found window: {window.window_text()}")
                if window.window_text() == "Login":
                    logger.info("TWS 'notification sent' window is ready")
                    sent_notification_window = window
                    break
        if sent_notification_window:
            break
        time.sleep(0.2)

    if not sent_notification_window:
        return False

    while sent_notification_window.is_active():
        time.sleep(0.5)

    tws_main_window_ready = False
    for _ in range(60):
        from pywinauto import Desktop
        windows = Desktop(backend="uia").windows()
        for window in windows:
            if window.process_id() == tws_pid:
                logger.info(f"Found window: {window.window_text()}")
                if window.window_text() == f"{MY_ACCOUNT} Interactive Brokers":
                    tws_main_window_ready = True
                break
        if tws_main_window_ready:
            break
        time.sleep(1)

    return tws_main_window_ready
"""

def kill_tws():
    killed = []
    for proc in psutil.process_iter(['pid', 'name']):
        try:
            if 'tws.exe' in proc.name().lower():
                proc.kill()
                killed.append(proc.pid)
                logger.info(f"Killed: {proc.name()} (PID {proc.pid})")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    if not killed:
        logger.info("No TWS processes found.")
    else:
        logger.info(f"Terminated {len(killed)} TWS process(es).")

"""
def restart_tws():
    global state

    kill_tws()
    result = start_tws()
    if result["login_window"]:
        message = 'TWS stopped, click the following link when you are ready to authenticate:\n'
        message += 'https://auth-ready-server.onrender.com/set-ready-to-authenticate'
        send_telegram_message(message)
    else:
        state = CANNOT_RESTART_TWS_STATE
        return

    wait_for_user_to_be_ready_to_login()

    login_success = login_tws(result)
    if not login_success:
        state = CANNOT_RESTART_TWS_STATE
        return
"""

def wait_for_user_to_be_ready_to_login():
    logger.info(f"Waiting for user to be ready to authenticate...")
    waiting_start_time = time.time()
    message_sent_in_telegram_too = False
    message = 'Platform stopped, click the following link when you are ready to authenticate:\n'
    message += 'https://auth-ready-server.onrender.com/set-ready-to-authenticate'
    send_telegram_message(message)
    while True:
        time.sleep(0.5)
        if random.random() < 0.07:
            logger.info("Waiting for user to be ready to authenticate")
        post_current_state({'status:': 'Waiting for user to be ready to authenticate'})
        response = requests.get(READY_TO_AUTHENTICATE_STATUS_URL)
        if response.status_code != requests.codes.ok:
            logger.error(f"Response status code for checking whether the user is ready to authenticate: {response.status_code}")
            continue
        json_response = response.json()
        is_ready_to_authenticate = json_response["is_ready_to_authenticate"]
        if is_ready_to_authenticate:
            logger.info(f"User is ready to authenticate")
            break

        check_time = time.time()
        if check_time - waiting_start_time > 600 and not message_sent_in_telegram_too:
            send_telegram_message(message)
            message_sent_in_telegram_too = True
    logger.info(f"Leaving wait_for_user_to_be_ready_to_login")


def restart_ibgateway():
    wait_for_user_to_be_ready_to_login()

    stop_ibgateway_command = ['/home/ibgateway/ibc/stop.sh', '']
    return_code = subprocess.run(stop_ibgateway_command)

    if return_code == 0:
        logger.info("Success: IB Gateway stopped successfully.")
    else:
        logger.error(f"Error: Command failed with return code {return_code}")

    run_ibgateway_command = ['/home/ibgateway/scripts/run.sh', '']
    p = subprocess.Popen(run_ibgateway_command)
    time.sleep(5)


def restart_platform():
    if is_in_docker():
        restart_ibgateway()
    else:
        pass # restart_tws()
    global state

    start_option_trader()
    time.sleep(20)
    is_process_alive = is_process_active()
    if is_process_alive:
        message = 'Platform and Option Trader are back'
        send_telegram_message(message)
        while True:
            response = requests.get(RESET_READY_TO_AUTHENTICATE_STATUS_URL)
            if response.status_code == requests.codes.ok:
                break
            time.sleep(5)
        state = MONITOR_STATE
    else:
        state = CANNOT_RESTART_OPTION_TRADER_STATE


def handle_cannot_restart_option_trader_state():
    message = 'Option Trader stopped. TWS remains active but Option Trader cannot restart'
    send_telegram_message(message)


def handle_cannot_restart_tws_state():
    message = 'TWS cannot restart'
    send_telegram_message(message)


def update_state(status):
    supervisor_state = {'status': status}
    update_supervisor_state(supervisor_state)

import psutil
import os

def check_ib_gateway_health(api_port=4001, ibc_port=7462, log_path="~/ibc/logs"):
    """
    Checks the multi-layer health of an IB Gateway + IBC instance.
    Returns a dictionary with status details.
    """
    status = {
        "process_found": False,
        "cpu_stable": False,
        "is_healthy": False
    }

    # 1. Process Check: Is Java running the Gateway/IBC?
    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            cmd = " ".join(proc.info['cmdline'] or [])
            if "java" in proc.info['name'].lower() and "ibgateway" in cmd: #or "ibc" in cmd):
                status["process_found"] = True
                # Check CPU usage (critical for 2-core setups)
                cpu_pct = proc.cpu_percent(interval=0.2)
                status["cpu_stable"] = cpu_pct < 90.0
                if not status["cpu_stable"] or random.random() < 0.01:
                    logger.info(f"ibgateway CPU usage is {cpu_pct}")
                break
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    # Final Verdict
    status["is_healthy"] = all([
        status["process_found"],
        status["cpu_stable"]
    ])

    return status

def monitor_platform():
    status = check_ib_gateway_health()
    if not status["is_healthy"]:
        logger.error(f"ibgateway is not healthy: {status}")
    success = True #test_connection_to_platform()
    """if not success:
        logger.error("Cannot connect to ibgateway")
        store_platform_log()
        set_switch_to_restart_platform_state()"""
    return success


def check_sunday_expiration():
    """
    Evaluates if it is Sunday after 1:00 AM ET and checks session expiration.
    Returns: (needs_restart: bool, updated_check_date: datetime.date)
    """
    global last_sunday_expiration_check_date

    # 1. Get the current time natively in US Eastern Time
    now_et = datetime.datetime.now(ZoneInfo("America/New_York"))
    current_date = now_et.date()

    # 2. Check if it's Sunday (weekday 6) and exactly or past 1:00 AM ET
    if now_et.weekday() == 6 and now_et.hour >= 1:

        # 3. Check the latch to ensure we only execute this ONCE today
        if last_sunday_expiration_check_date != current_date:
            logger.info("Executing weekly Sunday 2FA/Session check - starting with a soft restart...")
            soft_restart()

            if is_session_expired():
                logger.info("Session is expired. Flagging for restart.")
                last_sunday_expiration_check_date = current_date
                return True
            else:
                logger.info("Session is still active. No action needed.")
                last_sunday_expiration_check_date = current_date
                return False

                # Conditions not met; do not restart, and leave the latch unmodified
    return False


def monitor(interval=5):
    global state
    start_time = time.time()

    while True:
        try:
            if state == MONITOR_STATE:
                monitor_option_trader()
                monitor_platform()

                is_restart_required = check_sunday_expiration()
                if is_restart_required:
                    state = RESTART_PLATFORM_STATE

            elif state == RESTART_PLATFORM_STATE:
                logger.info("Switched to restart platform state")
                restart_platform()
            elif state == CANNOT_RESTART_OPTION_TRADER_STATE:
                handle_cannot_restart_option_trader_state()
                return
            elif state == CANNOT_RESTART_TWS_STATE:
                handle_cannot_restart_tws_state()
                return
        except Exception as e:
            logger.info(f"While monitoring got the following error: {e}")
            # traceback.print_exc()
            logger.error("Unhandled exception during the monitoring of option trader:\n%s", traceback.format_exc())
        time.sleep(interval)


def send_telegram_message(message):
    url = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage'
    params = {'chat_id': TELEGRAM_CHAT_ID, 'text': message}
    response = requests.get(url, params=params)
    if response.status_code == 200:
        logger.info(f'Message sent: {message}')
    else:
        logger.info(f'Failed to send message: {response.status_code}')


if __name__ == "__main__":
    _lock = acquire_single_instance_lock(lock_path='/tmp/supervisor_script.lock', process_name='Supervisor')

    try:
        logger.info("Supervisor started")
        monitor()
    except Exception:
        traceback.print_exc()
        logger.error("Unhandled exception:\n%s", traceback.format_exc())
