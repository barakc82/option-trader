import logging
import os
import time
import json
import subprocess
import requests
import psutil
import asyncio
from .supervisor_utils import OPTION_TRADER_DIR, send_telegram_message
from utilities.utils import is_in_docker, SUCCESS, ERROR

logger = logging.getLogger(__name__)

READY_TO_AUTHENTICATE_STATUS_URL = "https://auth-ready-server.onrender.com/status"

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
            except Exception as e:
                logger.error(f"Error killing option trader: {e}")

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

def wait_for_user_to_be_ready_to_login(post_current_state_callback):
    logger.info(f"Waiting for user to be ready to authenticate...")
    waiting_start_time = time.time()
    message_sent_in_telegram_too = False
    message = 'Platform stopped, click the following link when you are ready to authenticate:\n'
    message += 'https://auth-ready-server.onrender.com/set-ready-to-authenticate'
    send_telegram_message(message)
    
    while True:
        time.sleep(0.5)
        import random
        if random.random() < 0.07:
            logger.info("Waiting for user to be ready to authenticate")
        
        asyncio.run(post_current_state_callback({'status': 'Waiting for user to be ready to authenticate'}))
        
        try:
            response = requests.get(READY_TO_AUTHENTICATE_STATUS_URL, timeout=10)
            if response.status_code == 200:
                json_response = response.json()
                if json_response.get("is_ready_to_authenticate"):
                    logger.info(f"User is ready to authenticate")
                    break
        except Exception as e:
            logger.error(f"Error checking user auth readiness: {e}")

        if time.time() - waiting_start_time > 600 and not message_sent_in_telegram_too:
            send_telegram_message(message)
            message_sent_in_telegram_too = True
    logger.info(f"Leaving wait_for_user_to_be_ready_to_login")

def restart_ibgateway(post_current_state_callback, timeout: int = 30):
    logger.info("Restarting IB Gateway...")
    wait_for_user_to_be_ready_to_login(post_current_state_callback)

    logger.info("Running stop.sh...")
    try:
        subprocess.run(['/home/ibgateway/ibc/stop.sh'], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout, check=True)
        logger.info("IB Gateway stopped successfully")
    except Exception as e:
        logger.error(f"Failed to stop IB Gateway: {e}")

    logger.info("Running run.sh...")
    try:
        subprocess.Popen(['/home/ibgateway/scripts/run.sh'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        logger.info("IB Gateway start initiated successfully")
    except Exception as e:
        logger.error(f"Failed to start IB Gateway: {e}")

    time.sleep(5)

def soft_restart(timeout: int = 30):
    logger.info("Running 'restart.sh'")
    try:
        result = subprocess.run(['/home/ibgateway/ibc/restart.sh'], check=True, capture_output=True, text=True, timeout=timeout)
        logger.info(f"Soft restart script executed successfully: {result.stdout}")
        time.sleep(5)
    except Exception as e:
        logger.error(f"Soft restart failed: {e}")
