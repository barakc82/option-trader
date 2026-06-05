import logging
import os
import time
import glob
import random
import datetime
import psutil
from zoneinfo import ZoneInfo
from .supervisor_utils import find_latest_option_trader_log, OPTION_TRADER_DIR

logger = logging.getLogger(__name__)

IBGATEWAY_RESTART_REQUIRED = "IBGATEWAY_RESTART_REQUIRED"
UNKNOWN_ISSUE = "UNKNOWN_ISSUE"

mem_limit_mb = 0
paths = ['/sys/fs/cgroup/memory/memory.limit_in_bytes', '/sys/fs/cgroup/memory.max']
for path in paths:
    if os.path.exists(path):
        with open(path, 'r') as f:
            limit = f.read().strip()
            if limit and limit != "max":
                mem_limit_mb = int(limit) / (1024 * 1024)

def analyze_option_trader_log():
    latest_log = find_latest_option_trader_log()
    if not latest_log:
        return UNKNOWN_ISSUE

    try:
        with open(latest_log, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            if not lines:
                return UNKNOWN_ISSUE
            
            last_line = lines[-1]
            if "Task is waiting for IB connection" in last_line or "No position were found" in last_line:
                return IBGATEWAY_RESTART_REQUIRED
    except Exception as e:
        logger.error(f"Error analyzing log {latest_log}: {e}")

    return UNKNOWN_ISSUE

def check_process_state(process, should_print_health=False):
    try:
        status = process.status()
        if status == psutil.STATUS_ZOMBIE:
            logger.error("PROBLEM: Process is a ZOMBIE (terminated but not reaped).")
        elif status == psutil.STATUS_DISK_SLEEP:
            logger.warning("WARNING: Process is in UNINTERRUPTIBLE SLEEP (likely stuck on I/O or Network).")

        mem_mb = process.memory_info().rss / (1024 * 1024)
        cpu_pct = process.cpu_percent(interval=0.1)

        global mem_limit_mb
        if mem_limit_mb and (mem_mb / mem_limit_mb) > 0.85:
            logger.warning(f"CRITICAL: Memory near limit! {mem_mb:.1f}MB / {mem_limit_mb:.1f}MB")

        if cpu_pct > 95.0:
            logger.warning(f"⚠️ WARNING: High CPU usage detected: {cpu_pct}%")

        if random.random() < 0.01 or should_print_health:
            logger.info(f"Subprocess Health - CPU: {cpu_pct}% | MEM: {mem_mb:.2f} MB")
            if should_print_health:
                logger.info(f"Subprocess status {status}")
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass

def is_process_active():
    time_since_last_ping = 0
    hb_path = f"{OPTION_TRADER_DIR}/cache/heartbeat.txt"
    last_pid = 0
    for _ in range(24):
        try:
            pid_found = False
            if os.path.exists(hb_path):
                with open(hb_path, "r") as file:
                    import json
                    heartbeat = json.load(file)
                    pid = heartbeat.get('pid')
                    timestamp = heartbeat.get('timestamp')
                    if timestamp:
                        time_since_last_ping = float(timestamp)
                    if pid:
                        try:
                            option_trader_process = psutil.Process(pid)
                            check_process_state(option_trader_process)
                            last_pid = pid
                            pid_found = True
                        except psutil.NoSuchProcess:
                            logger.error(f"Could not find process {pid}")
                    else:
                        logger.info(f"No PID found in heartbeat file")

            time_since_last_ping = time.time() - time_since_last_ping
            is_process_alive = time_since_last_ping < 60 and pid_found
            if is_process_alive:
                return True

            logger.error(f"No heartbeat from option trader, time since last ping: {time_since_last_ping:.0f}s")
            if last_pid:
                try:
                    option_trader_process = psutil.Process(last_pid)
                    check_process_state(option_trader_process, should_print_health=True)
                except psutil.NoSuchProcess:
                    pass

        except (Exception) as e:
            logger.error(f"Invalid data in heartbeat file: {e}")
            time.sleep(5)

    logger.error(f"No heartbeat from option trader")
    return False

def is_session_expired():
    auth_text = "Authentication completed"
    expired_text = "The security tokens associated with your login credentials have expired"
    log_pattern = "/home/ibgateway/Jts/launcher*.log"
    log_files = glob.glob(log_pattern)
    log_files.sort(key=os.path.getmtime, reverse=True)

    logger.info("Checking the state of IBGateway session")

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
    logger.info(f"No text was found in the Jts log files")
    return False

def check_ib_gateway_health(api_port=4001, ibc_port=7462, log_path="~/ibc/logs"):
    status = {"process_found": False, "cpu_stable": False, "is_healthy": False}
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
    status["is_healthy"] = all([status["process_found"], status["cpu_stable"]])
    return status

def test_connection_to_platform(send_telegram_callback):
    retry_scripts = ["reconnectdata", "reconnectdata", "restart"]
    import subprocess
    for attempt_index in range(3):
        try:
            # Note: This was a placeholder in original code
            return True
        except ConnectionError as e:
            logger.error(f"Connection Error: trader station platform is down - {e}")
            time.sleep(2)

        retry_script = f"/home/ibgateway/ibc/{retry_scripts[attempt_index]}.sh"
        logger.info(f"Calling {retry_script} ...")
        result = subprocess.run([retry_script], capture_output=True, text=True)
        if result.returncode == 0:
            logger.info(f"{retry_scripts[attempt_index]}.sh finished successfully")
        else:
            logger.info(f"{retry_scripts[attempt_index]}.sh failed with code {result.returncode}")
        time.sleep(60)

    send_telegram_callback("Supervisor failed to connect to platform")
    return False
