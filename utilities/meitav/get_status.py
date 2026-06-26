import json
import re
import traceback
from pathlib import Path

from selenium.common import NoSuchElementException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from utilities.meitav.meitav_common import *
from utilities.meitav.start import start
from utilities.spreadsheet_operations import update_status_in_spreadsheet


_FINANCE_CONFIG = Path(__file__).resolve().parent.parent / 'finance_updater' / 'config.json'


def _update_break_time():
    try:
        with open(_FINANCE_CONFIG, 'r') as f:
            config = json.load(f)
        config['break_time'] = datetime.now().isoformat()
        with open(_FINANCE_CONFIG, 'w') as f:
            json.dump(config, f, indent=2)
    except Exception as e:
        print(f'Warning: could not update break_time in config: {e}')


# & C:\\"Program Files"\\Google\\Chrome\\Application\\chrome.exe --remote-debugging-port=9222 --user-data-dir=C:\\ChromeDebug --no-first-run

def is_balance_values_tab_selected(holdings_tab_element):
    selection_div = holdings_tab_element.find_element(By.CSS_SELECTOR, ".settings-button.ng-scope")
    li_element = selection_div.find_element(By.XPATH, "following-sibling::li[1]")
    heading_value = li_element.get_attribute("heading").strip()
    print(f"Selected tab: {heading_value}")
    return heading_value == 'יתרות'


def select_tab(driver, tab_title):
    tab = driver.find_element(By.XPATH, f"//*[normalize-space(text())='{tab_title}']")
    tab.click()

def select_balance_values_tab(driver):
    select_tab(driver, tab_title='יתרות')

def select_orders_tab(driver):
    select_tab(driver, tab_title='הוראות')

def extract_holdings(driver):

    holdings_tab_element = driver.find_element(By.CSS_SELECTOR, "div[ph='ph4']")
    is_balance_values_tab_selected_result = is_balance_values_tab_selected(holdings_tab_element)
    if not is_balance_values_tab_selected_result:
        select_balance_values_tab(driver)

    try:
        container = holdings_tab_element.find_element(By.CSS_SELECTOR, "div[role='presentation']")
    except NoSuchElementException:
        container = holdings_tab_element

    wait_object = WebDriverWait(container, 40, 1, ([NoSuchElementException]))
    header_cells =wait_object.until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, ".ui-grid-header-cell")))

     #container.find_elements(By.CSS_SELECTOR, ".ui-grid-header-cell")
    assert header_cells

    headers = []
    for cell in header_cells:
        # Extract title or visible text
        title = cell.find_element(By.CLASS_NAME, "ui-grid-header-cell-label").text.strip()
        # Find the specific column class (e.g., ui-grid-coluiGrid-01QN)
        classes = cell.get_attribute("class").split()
        col_class = next((c for c in classes if "ui-grid-col" in c), None)
        headers.append({"text": title, "class": col_class})

    # 3. Get Rows
    # Wait up to 10 seconds for the rows to appear inside your specific tab
    wait = WebDriverWait(driver, 10)

    # The lambda runs repeatedly until find_elements returns a non-empty list
    rows = wait.until(
        lambda _: container.find_elements(By.CSS_SELECTOR, ".ui-grid-row")
    )
    print(f"Number of rows: {len(rows)}, number of headers: {len(headers)}")

    holdings = {}

    for row in rows:
        row_data = {}
        is_row_valid = False
        for header in headers:
            if not header["class"]: continue

            # Find the cell in this row that matches the header's column class
            try:
                cell = row.find_element(By.CLASS_NAME, header["class"])
                raw_value = cell.text.strip()

                # Cleaning the data (remove commas, percent signs, etc.)
                clean_value = re.sub(r'[^\d.\-]', '', raw_value) if raw_value else "0"
                clean_value = float(clean_value)

                terminology_map = {
                    'מספר נייר': 'security_id',
                    'כמות נוכחית': 'quantity',
                    'שער': 'last_price'
                }
                field = terminology_map.get(header["text"], None)
                if field is None:
                    continue
                row_data[field] = clean_value
                is_row_valid = True
            except:
                row_data[header["text"]] = None

        if not is_row_valid:
            continue
        if 'security_id' not in row_data:
            print(row_data)
        assert row_data['security_id']
        row_data['security_id'] = int(row_data['security_id'])
        if 'quantity' not in row_data:
            print(row_data)
        assert row_data['quantity']
        holdings[row_data['security_id']] = row_data

    assert holdings
    return holdings

def extract_field(driver, selector):
    """Helper to safely grab text from a selector"""
    try:
        return driver.find_element(By.CSS_SELECTOR, selector).text
    except:
        return "Not Found"


def extract_status(driver):
    _update_break_time()
    try:
        first_span = driver.find_element(By.CSS_SELECTOR, ".account-container .ng-binding:nth-of-type(3)")
        account_id = first_span.text.strip()
        status = {'account_id': account_id}
        for user, user_data in users_data.items():
            if user_data[Hishtalmut]['account_id'] == account_id:
                status['user'] = user
                status['program_type'] = Hishtalmut
            if user_data[Gemel]['account_id'] == account_id:
                status['user'] = user
                status['program_type'] = Gemel
        assert status['user']

        cash_word = 'מזומנים'
        income_word = 'הכנסה'
        cash_ancestor_element = driver.find_element(By.XPATH, f"//*[contains(., '{cash_word}') and not(contains(., '{income_word}'))]")

        span_ng_binding_element = cash_ancestor_element.find_element(
            By.XPATH,
            "../../descendant::*[@class='ng-binding']"
        )

        status["cash"] = float(span_ng_binding_element.text.replace(",", ""))
        assert status["cash"]

        # Example of how to loop through a table of stocks/funds
        # This usually involves finding a 'row' element

        holdings = extract_holdings(driver)
        holdings.pop(1150242, None)
        status["holdings"] = holdings
        print("--- Current Status ---")
        print(status)
        assert 5112628 in status['holdings']
        return status

    except Exception as e:
        print(f"Error reading data: {e}")
        traceback.print_exc()
        exit(1)

if __name__ == "__main__":
    name = Barak
    program_type = Hishtalmut

    driver = start(name, program_type)

    status = extract_status(driver)
    update_status_in_spreadsheet(name, program_type, status)