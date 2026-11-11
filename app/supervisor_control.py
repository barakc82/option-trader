import logging
import os
import time
import json
import subprocess
import psutil
import asyncio
from .supervisor_utils import OPTION_TRADER_DIR, send_user_telegram_message
from .supervisor_http import (is_ready_to_authenticate, clear_ready_to_authenticate, SET_READY_TO_AUTHENTICATE_PATH,
                              SUPERVISOR_HTTP_HOST, SUPERVISOR_HTTP_PORT)
from utilities.utils import is_in_docker, SUCCESS, ERROR, FAILED, OPTION_TRADER_CONFIG_PATH

logger = logging.getLogger(__name__)

# Shown instead of a link when domain isn't configured: the endpoint can still be reached from
# inside the container, straight on the supervisor's HTTP server.
READY_TO_AUTHENTICATE_FALLBACK = (f"(domain is not set in the config) run inside the container: "
                                  f"curl http://{SUPERVISOR_HTTP_HOST}:{SUPERVISOR_HTTP_PORT}"
                                  f"{SET_READY_TO_AUTHENTICATE_PATH}")


def get_ready_to_authenticate_url():
    """The link sent in Telegram: http://<domain><SET_READY_TO_AUTHENTICATE_PATH>, with domain
    from OPTION_TRADER_CONFIG_PATH (the address nginx serves on, including the port if it
    isn't 80, e.g. "1.2.3.4:8080"), which nginx forwards to the supervisor's own HTTP server."""
    try:
        with open(OPTION_TRADER_CONFIG_PATH, 'r') as f:
            domain = json.load(f).get("domain")
        if domain:
            return f"http://{domain.rstrip('/')}{SET_READY_TO_AUTHENTICATE_PATH}"
        logger.error(f"domain is not set in {OPTION_TRADER_CONFIG_PATH}")
    except Exception as e:
        logger.error(f"Could not read domain from {OPTION_TRADER_CONFIG_PATH}: {e}")
    return READY_TO_AUTHENTICATE_FALLBACK

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
    """
    Kills the option trader process.
    First tries the PID from the heartbeat file, but verifies it's the correct process.
    Then searches for any other processes running 'app.main'.
    """
    hb_path = f"{OPTION_TRADER_DIR}/cache/heartbeat.txt"
    heartbeat_pid = None
    if os.path.exists(hb_path):
        try:
            with open(hb_path, "r") as file:
                heartbeat = json.load(file)
                heartbeat_pid = heartbeat.get('pid')
        except Exception as e:
            logger.error(f"Error reading heartbeat file: {e}")

    processes_to_kill = []

    # 1. Check if the PID from heartbeat is valid and is indeed an option trader
    if heartbeat_pid:
        try:
            if psutil.pid_exists(heartbeat_pid):
                proc = psutil.Process(heartbeat_pid)
                cmdline = proc.cmdline()
                if any('app.main' in arg for arg in cmdline):
                    processes_to_kill.append(proc)
                else:
                    logger.warning(f"PID {heartbeat_pid} from heartbeat exists but does not appear to be 'app.main'.")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    # 2. Search for any other processes running 'app.main'
    my_pid = os.getpid()
    for proc in psutil.process_iter(['pid', 'cmdline']):
        try:
            pid = proc.info['pid']
            if pid == my_pid:
                continue
            
            cmdline = proc.info['cmdline']
            if cmdline and '-m' in cmdline and 'app.main' in cmdline:
                # Avoid duplicates
                if not any(p.pid == pid for p in processes_to_kill):
                    processes_to_kill.append(psutil.Process(pid))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    if not processes_to_kill:
        logger.info("No active option trader processes found to kill.")
        return

    # Terminate the identified processes
    for proc in processes_to_kill:
        try:
            logger.info(f"Terminating option trader process: PID {proc.pid}")
            proc.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    # Wait for processes to exit, then kill if still alive
    gone, alive = psutil.wait_procs(processes_to_kill, timeout=5)
    for proc in alive:
        try:
            logger.warning(f"Process {proc.pid} did not terminate after 5s. Killing it...")
            proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

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
    # Only a request made from now on counts, not one left over from an earlier wait.
    clear_ready_to_authenticate()
    waiting_start_time = time.time()
    message_sent_in_telegram_too = False
    message = 'Platform stopped, click the following link when you are ready to authenticate:\n'
    message += get_ready_to_authenticate_url()
    send_user_telegram_message(message)
    
    while True:
        time.sleep(0.5)
        import random
        if random.random() < 0.07:
            logger.info("Waiting for user to be ready to authenticate")
        
        asyncio.run(post_current_state_callback({'status': 'Waiting for user to be ready to authenticate'}))

        if is_ready_to_authenticate():
            logger.info(f"User is ready to authenticate (set via the supervisor's HTTP endpoint)")
            clear_ready_to_authenticate()
            break

        if time.time() - waiting_start_time > 600 and not message_sent_in_telegram_too:
            send_user_telegram_message(message)
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

    running_run_sh = []
    for proc in psutil.process_iter(['pid', 'cmdline']):
        try:
            cmdline = proc.info['cmdline']
            if cmdline and any('run.sh' in arg for arg in cmdline):
                running_run_sh.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    if running_run_sh:
        logger.warning(f"Found {len(running_run_sh)} lingering run.sh process(es), terminating them...")
        for proc in running_run_sh:
            try:
                proc.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        gone, alive = psutil.wait_procs(running_run_sh, timeout=5)
        for proc in alive:
            try:
                logger.warning(f"run.sh process {proc.pid} did not terminate after 5s. Killing it...")
                proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

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
        if 'ERROR' in result.stdout:
            logger.error(f"Soft restart failed:n{result.stdout}")
            return FAILED
        logger.info(f"Soft restart script executed successfully:\n{result.stdout}")
        time.sleep(5)
        return SUCCESS
    except Exception as e:
        logger.error(f"Soft restart failed: {e}")
        return FAILED
