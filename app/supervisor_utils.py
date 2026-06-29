import logging
import os
import datetime
import requests
import shutil
from pathlib import Path

# Constants for logging and directories
OPTION_TRADER_DIR = "/home/option-trader"
if not os.path.exists(OPTION_TRADER_DIR):
    OPTION_TRADER_DIR = "."

LOGS_DIR = f"{OPTION_TRADER_DIR}/logs"

logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = '8161204170:AAGRCLXSgBzmhukhFPlTTnAXeagv7LJmE3o'
TELEGRAM_CHAT_ID = '1796107185'

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

def count_text_in_file(path, text):
    count = 0
    now = datetime.datetime.now()
    threshold = now - datetime.timedelta(hours=2)
    date_format = "%Y-%m-%d %H:%M:%S"

    if not path or not os.path.exists(path):
        return 0

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                parts = line.split('] ', 1)
                if len(parts) < 2:
                    continue

                timestamp_str = parts[1][:19]
                log_time = datetime.datetime.strptime(timestamp_str, date_format)

                if log_time >= threshold:
                    count += line.count(text)

            except (ValueError, IndexError):
                continue

    return count

def find_latest_option_trader_log():
    directory = Path(LOGS_DIR)
    log_files = list(directory.glob("option_trader*.log"))
    if not log_files:
        return None
    return max(log_files, key=lambda f: f.stat().st_mtime)

def store_platform_log():
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    try:
        shutil.copy('/home/ibgateway/ibgateway.log', f'{LOGS_DIR}/ibgateway_{timestamp}.log')
        shutil.copy('/home/ibgateway/Jts/launcher.log', f'{LOGS_DIR}/launcher_{timestamp}.log')
    except Exception as e:
        logger.error(f"Failed to store platform logs: {e}")

def switch_supervisor_log():
    log_path = f'{LOGS_DIR}/supervisor.log'
    if not os.path.exists(log_path):
        return False

    try:
        archive_path = f'{LOGS_DIR}/supervisor_old.log'
        if os.path.exists(archive_path):
            os.remove(archive_path)
        os.rename(log_path, archive_path)
        return True
    except Exception as e:
        print(f"Error archiving supervisor log: {e}")
        return False
