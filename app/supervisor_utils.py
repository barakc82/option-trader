import logging
import os
import datetime
import json
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
SUPERVISOR_CONFIG_PATH = f"{OPTION_TRADER_DIR}/config/supervisor_config.json"
ADMIN_TELEGRAM_CHAT_ID = 1796107185


def get_user_telegram_chat_id():
    """telegram_chat_id from SUPERVISOR_CONFIG_PATH, a JSON number (e.g. 1796107185; negative
    for group chats), read on every send so an edit takes effect without a restart. Returns
    it as an int, or None (and an error logged) if it's missing, not a whole number, or the
    file can't be read."""
    try:
        with open(SUPERVISOR_CONFIG_PATH, 'r') as f:
            chat_id = json.load(f).get('telegram_chat_id')
    except Exception as e:
        logger.error(f"Could not read telegram_chat_id from {SUPERVISOR_CONFIG_PATH}: {e}")
        return None
    if chat_id is None:
        logger.error(f"telegram_chat_id is not set in {SUPERVISOR_CONFIG_PATH}")
        return None
    # bool is a subclass of int in Python, so true/false must be rejected explicitly.
    if isinstance(chat_id, bool) or not isinstance(chat_id, int):
        logger.error(f"telegram_chat_id in {SUPERVISOR_CONFIG_PATH} must be a whole JSON number, "
                     f"got {chat_id!r}")
        return None
    return chat_id


def send_telegram_message(chat_id, message):
    url = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage'
    params = {'chat_id': chat_id, 'text': message}
    try:
        response = requests.get(url, params=params, timeout=10)
        if response.status_code == 200:
            logger.info(f'Message sent: {message}')
        else:
            logger.info(f'Failed to send message: {response.status_code}')
    except Exception as e:
        logger.error(f"Failed to send Telegram message: {e}")


def send_admin_telegram_message(message):
    send_telegram_message(ADMIN_TELEGRAM_CHAT_ID, message)


def send_user_telegram_message(message):
    chat_id = get_user_telegram_chat_id()
    if chat_id is None:
        logger.error(f"Telegram message not sent (no chat ID): {message}")
        return
    send_telegram_message(chat_id, message)

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
