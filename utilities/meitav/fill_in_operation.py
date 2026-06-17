import time

from selenium.webdriver.support.ui import WebDriverWait
from selenium.common import ElementNotVisibleException
from selenium.webdriver.common.by import By

from utilities.meitav.meitav_common import *


def fill_in_operation_by_total_value(driver, operation_type, security_id, total_value):
    element = driver.find_element(By.XPATH, f"//*[text()='{security_id}']")
    element.click()
    time.sleep(1)

    if operation_type == 'buy':
        buy_button = driver.find_element(By.CSS_SELECTOR, '.buy-btn.stock-info-header')
        buy_button.click()

    if operation_type == 'sell':
        buy_button = driver.find_element(By.CSS_SELECTOR, '.sell-btn.stock-info-header')
        buy_button.click()

    wait_object = WebDriverWait(driver, 40, 1, ([ElementNotVisibleException]))
    wait_object.until(lambda x: x.find_element(By.XPATH, f"//*[text()='מחיר באגורות']") is not None)

    inputs = driver.find_elements(By.CSS_SELECTOR, ".send-order-control-item-body input")
    units_input = inputs[3]
    units_input.clear()
    units_input.send_keys(str(total_value))


def fill_in_operation(driver, operation_type, security_id, units, price, target_date):
    element = driver.find_element(By.XPATH, f"//*[text()='{security_id}']")
    element.click()
    time.sleep(1)

    if operation_type == 'buy':
        buy_button = driver.find_element(By.CSS_SELECTOR, '.buy-btn.stock-info-header')
        buy_button.click()

    if operation_type == 'sell':
        buy_button = driver.find_element(By.CSS_SELECTOR, '.sell-btn.stock-info-header')
        buy_button.click()

    wait_object = WebDriverWait(driver, 40, 1, ([ElementNotVisibleException]))
    wait_object.until(lambda x: x.find_element(By.XPATH, f"//*[text()='מחיר באגורות']") is not None)

    inputs = driver.find_elements(By.CSS_SELECTOR, ".send-order-control-item-body input")
    units_input = inputs[0]
    units_input.clear()
    units_input.send_keys(str(units))

    price_input = inputs[1]
    price_input.clear()
    price_input.send_keys(str(price))

    buttons = driver.find_elements(By.CSS_SELECTOR, "button[aria-label*='לחץ לבחירת תאריך']")
    until_date_button = buttons[1]
    until_date_button.click()

    current_month = get_hebrew_month_year()
    buttons = driver.find_elements(By.XPATH, f"//button[strong[normalize-space()='{current_month}']]")
    until_date_button = buttons[1]
    until_date_button.click()

    hebrew_month = hebrew_months[target_date.month]
    today = datetime.now()
    assert target_date.year == today.year

    month_button = driver.find_element(By.XPATH, f"//button[span[normalize-space()='{hebrew_month}']]")
    month_button.click()

    formatted_day = f"{target_date.day:02d}"

    date_picker_items = driver.find_elements(By.XPATH, "//div[@class='date-picker-item']")

    day_buttons = date_picker_items[1].find_elements(By.XPATH,
                                                     f".//tbody//button[span[normalize-space()='{formatted_day}']]")
    day_buttons[0].click()
