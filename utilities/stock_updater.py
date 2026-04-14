import time
from datetime import datetime

import httpx
from bs4 import BeautifulSoup

import gspread
from google.oauth2.service_account import Credentials


# =======================
# CONFIG
# =======================

GLOBES_URL = "https://www.globes.co.il/quote/PUT_STOCK_URL_HERE"
SHEET_NAME = "Stock Prices"
GOOGLE_CREDENTIALS_FILE = "credentials.json"
UPDATE_INTERVAL_SECONDS = 60  # how often to update


# =======================
# FETCH PAGE
# =======================

def fetch_page(url: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; StockTracker/1.0)"
    }
    with httpx.Client(timeout=10) as client:
        response = client.get(url, headers=headers)
        response.raise_for_status()
        return response.text


# =======================
# PARSE PRICE
# =======================

def parse_price(html: str) -> float:
    soup = BeautifulSoup(html, "lxml")

    # ⚠️ YOU MUST ADJUST THIS SELECTOR
    # Inspect the page and find the element containing the price
    price_tag = soup.select_one("span.price")

    if not price_tag:
        raise ValueError("Price element not found")

    price_text = price_tag.text.strip()
    price_text = (
        price_text
        .replace("₪", "")
        .replace(",", "")
        .strip()
    )

    return float(price_text)


# =======================
# GOOGLE SHEETS
# =======================

def connect_sheet(sheet_name: str):
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]

    creds = Credentials.from_service_account_file(
        GOOGLE_CREDENTIALS_FILE,
        scopes=scopes
    )

    client = gspread.authorize(creds)
    return client.open(sheet_name).sheet1


def update_sheet(sheet, price: float):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sheet.append_row([timestamp, price], value_input_option="USER_ENTERED")


# =======================
# MAIN LOOP
# =======================

def main():
    print("Connecting to Google Sheet...")
    sheet = connect_sheet(SHEET_NAME)
    print("Connected. Starting tracker.")

    while True:
        try:
            html = fetch_page(GLOBES_URL)
            price = parse_price(html)
            update_sheet(sheet, price)
            print(f"[OK] {price}")
        except Exception as e:
            print(f"[ERROR] {e}")

        time.sleep(UPDATE_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
