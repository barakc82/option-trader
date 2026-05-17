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
import psutil

from utilities.utils import is_in_docker, acquire_single_instance_lock
from .state_updater import update_supervisor_state_async, post_current_state

# Create a logger
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

OPTION_TRADER_DIR = "/home/option-trader"
if not os.path.exists(OPTION_TRADER_DIR):
    OPTION_TRADER_DIR = "." # Fallback for local dev

LOGS_DIR = f"{OPTION_TRADER_DIR}/logs"
CONFIG_PATH = f"{OPTION_TRADER_DIR}/config/supervisor_config.json"

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

SUCCESS = 0
ERROR = 1

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
import asyncio

def count_text_in_file(path, text):
    count = 0
    # 1. Define the time threshold (2 hours ago)
    now = datetime.datetime.now()
    threshold = now - datetime.timedelta(hours=2)

    # 2. Match your date format: 2026-04-15 14:12:15
    date_format = "%Y-%m-%d %H:%M:%S"

    if not path or not os.path.exists(path):
        return 0

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

def start_option_trader():
    logger.info(f"Restarting process using async version...")

    module = "app.main"

    if is_in_docker():
        option_trader_start_command = ["python3", "-m", module]
    else:
        option_trader_start_command = [".\\.venv\\Scripts\\python.exe", "-m", module]

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
                hb_path = f"{OPTION_TRADER_DIR}/cache/heartbeat.txt"
                if os.path.exists(hb_path):
                    with open(hb_path, "r") as file:
                        heartbeat = json.load(file)
                        heartbeat_pid = heartbeat['pid']
                        if heartbeat_pid == option_trader_process.pid:
                            logger.info(f"The required pid was found in the heartbeat file")
                            return SUCCESS
                logger.info(f"The required pid ({option_trader_process.pid}) hasn't been found yet in the heartbeat file")
                time.sleep(5)
        except Exception as e:
            logger.error(f"Error checking heartbeat: {e}")
    else:
        logger.info("Process did not restart")
    return ERROR


def kill_option_trader():
    hb_path = f"{OPTION_TRADER_DIR}/cache/heartbeat.txt"
    if os.path.exists(hb_path):
        with open(hb_path, "r") as file:
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
    try:
        shutil.copy('/home/ibgateway/ibgateway.log', f'/home/option-trader/logs/ibgateway_{timestamp}.log')
        shutil.copy('/home/ibgateway/Jts/launcher.log', f'/home/option-trader/logs/launcher_{timestamp}.log')
    except Exception as e:
        logger.error(f"Failed to store platform logs: {e}")


def is_session_expired():
    # The two target phrases
    auth_text = "Authentication completed"
    expired_text = "The security tokens associated with your login credentials have expired"

    # 1. Gather all launcher log files
    log_pattern = "/home/ibgateway/Jts/launcher*.log"
    log_files = glob.glob(log_pattern)

    # 2. Sort files by modification time, descending (newest first)
    log_files.sort(key=os.path.getmtime, reverse=True)

    for file_path in log_files:
        if not os.path.exists(file_path):
            continue

        try:
            with open(file_path, 'r', encoding='utf-8') as file:
                lines = file.readlines()
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


def soft_restart(timeout: int = 30):
    logger.info("Running 'restart.sh'")
    try:
        result = subprocess.run(
            ['/home/ibgateway/ibc/restart.sh'],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout
        )
        logger.info(f"Soft restart script executed successfully: {result.stdout}")
        time.sleep(5)
    except subprocess.TimeoutExpired:
        logger.error(f"Soft restart timed out after {timeout} seconds")
    except subprocess.CalledProcessError as e:
        logger.error(f"Soft restart failed with exit code {e.returncode}: {e.stderr}")
    except Exception as e:
        logger.error(f"Soft restart failed: {e}")


def monitor_option_trader():
    is_process_alive = is_process_active()
    if not is_process_alive:
        logger.warning(f"Option Trader is not alive.")
        kill_option_trader()
        asyncio.run(post_current_state({'status': 'Terminated'}))
        is_session_expired_result = is_session_expired()
        if is_session_expired_result:
            logger.warning("IBGateway session expired, switching to restart platform state")
            store_platform_log()
            set_switch_to_restart_platform_state()
            return
        # latest_log = find_latest_option_trader_log()
        # is_connection_error = count_text_in_file(latest_log, " Cannot overcome connection error, exiting")
        is_process_active_result = start_option_trader()
        if is_process_active_result == ERROR:
            global state
            state = CANNOT_RESTART_OPTION_TRADER_STATE
            return
        
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
            logger.warning("It seems that option trader has difficulties getting delta data, switching to restart platform state")
            soft_restart()


def set_switch_to_restart_platform_state():
    global state
    logger.warning(f"Switching to 'wait for user to be ready for login' state")
    state = RESTART_PLATFORM_STATE


def check_process_state(process, should_print_health=False):
    try:
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
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass

def is_process_active():
    last_ping = 0
    last_pid = 0
    hb_path = f"{OPTION_TRADER_DIR}/cache/heartbeat.txt"
    for _ in range(24):
        try:
            pid_found = False
            if os.path.exists(hb_path):
                with open(hb_path, "r") as file:
                    heartbeat = json.load(file)
                    pid = heartbeat.get('pid')
                    timestamp = heartbeat.get('timestamp')
                    if timestamp:
                        last_ping = float(timestamp)
                    if pid:
                        try:
                            option_trader_process = psutil.Process(pid)
                            check_process_state(option_trader_process)
                            last_pid = pid
                            pid_found = True
                        except psutil.NoSuchProcess:
                            logger.error(f"Could not find process {pid}")

            time_since_last_ping = time.time() - last_ping
            is_process_alive = time_since_last_ping < 60 and pid_found
            if not is_process_alive:
                logger.error(f"No heartbeat from option trader, time since last ping: {time_since_last_ping:.0f}s")
                if last_pid:
                    try:
                        option_trader_process = psutil.Process(last_pid)
                        check_process_state(option_trader_process, should_print_health=True)
                    except psutil.NoSuchProcess:
                        pass
            return is_process_alive
        except (json.JSONDecodeError, ValueError) as e:
            logger.error(f"Invalid data in heartbeat file: {e}")
            time.sleep(5)

    logger.error(f"No heartbeat from option trader")
    return False


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
        asyncio.run(post_current_state({'status:': 'Waiting for user to be ready to authenticate'}))
        try:
            response = requests.get(READY_TO_AUTHENTICATE_STATUS_URL, timeout=10)
            if response.status_code != requests.codes.ok:
                logger.error(f"Response status code for checking whether the user is ready to authenticate: {response.status_code}")
                continue
            json_response = response.json()
            is_ready_to_authenticate = json_response["is_ready_to_authenticate"]
            if is_ready_to_authenticate:
                logger.info(f"User is ready to authenticate")
                break
        except Exception as e:
            logger.error(f"Error checking user auth readiness: {e}")

        check_time = time.time()
        if check_time - waiting_start_time > 600 and not message_sent_in_telegram_too:
            send_telegram_message(message)
            message_sent_in_telegram_too = True
    logger.info(f"Leaving wait_for_user_to_be_ready_to_login")


def restart_ibgateway(timeout: int = 30):
    logger.info("Restarting IB Gateway...")
    wait_for_user_to_be_ready_to_login()

    logger.info("Running stop.sh...")
    try:
        subprocess.run(
            ['/home/ibgateway/ibc/stop.sh'],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True
        )
        logger.info("IB Gateway stopped successfully")
    except subprocess.TimeoutExpired:
        logger.error(f"stop.sh timed out after {timeout} seconds")
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to stop IB Gateway (exit code {e.returncode}): {e.stderr}")
    except Exception as e:
        logger.error(f"Failed to stop IB Gateway: {e}")

    logger.info("Running run.sh...")
    try:
        subprocess.Popen(
            ['/home/ibgateway/scripts/run.sh'],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.info("IB Gateway start initiated successfully")
    except Exception as e:
        logger.error(f"Failed to start IB Gateway: {e}")

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
            try:
                response = requests.get(RESET_READY_TO_AUTHENTICATE_STATUS_URL, timeout=10)
                if response.status_code == requests.codes.ok:
                    break
            except Exception:
                pass
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
    asyncio.run(update_supervisor_state_async(supervisor_state))


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
            if "java" in proc.info['name'].lower() and "ibgateway" in cmd:
                status["process_found"] = True
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
    success = True
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
                logger.error("Exiting since cannot restart option trader")
                return
            elif state == CANNOT_RESTART_TWS_STATE:
                handle_cannot_restart_tws_state()
                return
        except Exception as e:
            logger.info(f"While monitoring got the following error: {e}")
            logger.error("Unhandled exception during the monitoring of option trader:\n%s", traceback.format_exc())
        time.sleep(interval)


def send_telegram_message(message):
    url = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage'
    params = {'chat_id': TELEGRAM_CHAT_ID, 'text': message}
    try:
        response = requests.get(url, params=params, timeout=10)
        if response.status_code == 200:
            logger.info(f'Message sent: {message}')
        else:
            logger.info(f'Failed to send message: {response.status_code}')
    except Exception as e:
        logger.error(f"Failed to send Telegram message: {e}")


if __name__ == "__main__":
    # Windows-compatible lock path (project root)
    lock_path = 'cache/supervisor_script.lock'
    _lock = acquire_single_instance_lock(lock_path=lock_path, process_name='Supervisor')

    try:
        logger.info("Supervisor started")
        monitor()
    except Exception:
        traceback.print_exc()
        logger.error("Unhandled exception:\n%s", traceback.format_exc())
