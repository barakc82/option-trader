from pathlib import Path
import gspread
from google.oauth2.service_account import Credentials

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SERVICE_ACCOUNT_FILE = f"{PROJECT_ROOT}/resources/service_account.json"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

credentials = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
client = gspread.authorize(credentials)

def get_worksheet(sheet_name):
    return client.open_by_key("1u2uLtVFnRCimMfymDMYygqg2zk2eK2TrQ4Bl18j8Uwc").worksheet(sheet_name)
