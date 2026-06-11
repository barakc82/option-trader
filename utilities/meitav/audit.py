import re

from selenium.common import NoSuchElementException
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from utilities.meitav.get_status import select_orders_tab

def extract_completed_operations(driver):
    select_orders_tab(driver)
    orders_tab_element = driver.find_element(By.CSS_SELECTOR, "div[ph='ph4']")
    try:
        container = orders_tab_element.find_element(By.CSS_SELECTOR, "div[role='presentation']")
    except NoSuchElementException:
        container = orders_tab_element

    wait_object = WebDriverWait(container, 40, 1, ([NoSuchElementException]))
    header_cells = wait_object.until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, ".ui-grid-header-cell")))
    assert header_cells

    headers = []
    for cell in header_cells:
        # Extract title or visible text
        title = cell.find_element(By.CLASS_NAME, "ui-grid-header-cell-label").text.strip()
        # Find the specific column class (e.g., ui-grid-coluiGrid-01QN)
        classes = cell.get_attribute("class").split()
        col_class = next((c for c in classes if "ui-grid-col" in c), None)
        headers.append({"text": title, "class": col_class})

    # Wait up to 10 seconds for the rows to appear inside your specific tab
    wait = WebDriverWait(driver, 10)

    # The lambda runs repeatedly until find_elements returns a non-empty list
    rows = wait.until(
        lambda _: orders_tab_element.find_elements(By.CSS_SELECTOR, ".ui-grid-row")
    )
    print(f"Number of rows: {len(rows)}, number of headers: {len(headers)}")

    operations = []
    for row in rows:
        operation = {}
        for header in headers:
            if not header["class"]: continue

            # Find the cell in this row that matches the header's column class
            try:
                cell = row.find_element(By.CLASS_NAME, header["class"])
                raw_value = cell.text.strip()

                #clean_value = re.sub(r'[^\d.\-]', '', raw_value) if raw_value else "0"
                if header["text"] == 'ק/מ':
                    print(f"Raw value of buy/sell: {raw_value}")
                    if not raw_value:
                        raw_value = cell.find_element(By.CSS_SELECTOR, ".ui-grid-cell-contents").text.strip()
                        print(raw_value)
                try:
                    clean_value = float(raw_value)
                except:
                    clean_value = raw_value
                    if raw_value == 'קניה':
                        clean_value = 'BUY'
                    if raw_value == 'מכירה':
                        clean_value = 'Sell'
                terminology_map = {
                    'מספר נייר': 'security_id',
                    'ק/מ': 'operation_type',
                    'כמות ביצוע': 'quantity',
                    'מחיר ביצוע': 'price'
                }
                field = terminology_map.get(header["text"], None)
                if field is None:
                    continue
                operation[field] = clean_value
            except:
                operation[header["text"]] = None

        if not operation:
            print("No operation found, continuing")
            continue
        if operation.get('quantity', None):
            assert operation['security_id']
            operations.append(operation)

    print(operations)
    assert operations
    return operations