import json
from pathlib import Path
import gspread
from google.oauth2.service_account import Credentials

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SERVICE_ACCOUNT_FILE = str(PROJECT_ROOT / "resources" / "service_account.json")
CONFIG_FILE = PROJECT_ROOT / "config" / "option_trader_config.json"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

_client = None
_spreadsheet_id = None

def _get_spreadsheet_id():
    global _spreadsheet_id
    if _spreadsheet_id is None:
        try:
            with open(CONFIG_FILE, 'r') as f:
                config = json.load(f)
                _spreadsheet_id = config.get("spreadsheet_id")
        except Exception as e:
            print(f"Error loading config for spreadsheet_id: {e}")
            # Fallback to hardcoded ID if config fails
            _spreadsheet_id = "1u2uLtVFnRCimMfymDMYygqg2zk2eK2TrQ4Bl18j8Uwc"
    return _spreadsheet_id

def get_client():
    global _client
    if _client is None:
        try:
            credentials = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
            _client = gspread.authorize(credentials)
        except Exception as e:
            print(f"Failed to authorize Google Sheets client: {e}")
            raise
    return _client

def get_worksheet(sheet_name):
    client = get_client()
    spreadsheet_id = _get_spreadsheet_id()
    return client.open_by_key(spreadsheet_id).worksheet(sheet_name)
