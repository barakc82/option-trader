import json
import os
from datetime import datetime
from pathlib import Path

Barak = 1
Mom = 2
Hilush = 3

Hishtalmut = 0
Gemel = 1

# Configuration file is now in the same folder as this script
CONFIG_FILE = Path(__file__).resolve().parent / "meitav_accounts.json"

def _load_users_data():
    if not CONFIG_FILE.exists():
        print(f"Warning: Configuration file {CONFIG_FILE} not found.")
        return {}
    
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            # Convert string keys back to integers for compatibility
            processed_data = {}
            for user_id, user_info in data.items():
                processed_user_info = {}
                for key, value in user_info.items():
                    if key.isdigit():
                        processed_user_info[int(key)] = value
                    else:
                        processed_user_info[key] = value
                processed_data[int(user_id)] = processed_user_info
            return processed_data
    except Exception as e:
        print(f"Error loading meitav_accounts.json: {e}")
        return {}

users_data = _load_users_data()

hebrew_months = {
    1: "ינואר",
    2: "פברואר",
    3: "מרץ",
    4: "אפריל",
    5: "מאי",
    6: "יוני",
    7: "יולי",
    8: "אוגוסט",
    9: "ספטמבר",
    10: "אוקטובר",
    11: "נובמבר",
    12: "דצמבר"
}

def get_hebrew_month_year():
    now = datetime.now()
    month_hebrew = hebrew_months[now.month]
    return f"{month_hebrew} {now.year}"
