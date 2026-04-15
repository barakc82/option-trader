import re
import traceback

from selenium.common import NoSuchElementException
from selenium.webdriver.common.by import By

from utilities.meitav.meitav_common import *
from utilities.meitav.start import start
from utilities.spreadsheet_update import update_status_in_spreadsheet


# & C:\\"Program Files"\\Google\\Chrome\\Application\\chrome.exe --remote-debugging-port=9222 --user-data-dir=C:\\ChromeDebug --no-first-run

def is_balance_values_tab_selected(holdings_tab_element):

    selection_div = holdings_tab_element.find_element(By.CSS_SELECTOR, ".settings-button.ng-scope")
    li_element = selection_div.find_element(By.XPATH, "following-sibling::li[1]")
    heading_value = li_element.get_attribute("heading").strip()
    print(f"Selected tab: {heading_value}")
    return heading_value == 'יתרות'

def extract_holdings(driver):

    holdings_tab_element = driver.find_element(By.CSS_SELECTOR, "div[ph='ph4']")

    is_balance_values_tab_selected_result = is_balance_values_tab_selected(holdings_tab_element)
    if not is_balance_values_tab_selected_result:
        print("Error: balance values tab not selected")
        exit(1)

    container = holdings_tab_element.find_element(By.CSS_SELECTOR, "div[role='presentation']")

    # 2. Get Headers - Map the column class to the header text
    header_cells = container.find_elements(By.CSS_SELECTOR, ".ui-grid-header-cell")
    headers = []
    for cell in header_cells:
        # Extract title or visible text
        title = cell.find_element(By.CLASS_NAME, "ui-grid-header-cell-label").text.strip()
        # Find the specific column class (e.g., ui-grid-coluiGrid-01QN)
        classes = cell.get_attribute("class").split()
        col_class = next((c for c in classes if "ui-grid-col" in c), None)
        headers.append({"text": title, "class": col_class})

    # 3. Get Rows
    rows = container.find_elements(By.CSS_SELECTOR, ".ui-grid-row")
    holdings = {}

    for row in rows:
        row_data = {}
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
            except:
                row_data[header["text"]] = None

        assert row_data['security_id']
        assert row_data['quantity']
        holdings[row_data['security_id']] = row_data

    return holdings

def extract_field(driver, selector):
    """Helper to safely grab text from a selector"""
    try:
        return driver.find_element(By.CSS_SELECTOR, selector).text
    except:
        return "Not Found"


def extract_status(driver):
    try:
        first_span = driver.find_element(By.CSS_SELECTOR, ".account-container .ng-binding:nth-of-type(3)")
        account_id = first_span.text.strip()
        status = {'account_id': account_id}

        all_spans = driver.find_elements(By.CSS_SELECTOR, "span.ng-binding[title]")
        for span in all_spans:
            field_value = span.get_attribute("title")
            print(f"Field value: {field_value}")
            if "." in field_value:
                field_name = ""
                try:
                    field_name = span.find_element(By.XPATH, "./../preceding-sibling::div").text
                except NoSuchElementException:
                    try:
                        field_name = span.find_element(By.XPATH, "./../../preceding-sibling::div").text
                    except NoSuchElementException:
                        pass

                print(f"Value found: {field_value} {field_name}")
                if "מזומנים" in field_name:
                    status["cash"] = float(field_value.replace(",", ""))
                if "שווי" in field_name:
                    status["total"] = float(field_value.replace(",", ""))

        assert status["cash"]

        # Example of how to loop through a table of stocks/funds
        # This usually involves finding a 'row' element

        holdings = extract_holdings(driver)
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