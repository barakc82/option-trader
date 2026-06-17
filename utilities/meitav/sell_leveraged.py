import math
import time

from dateutil.relativedelta import relativedelta

from selenium.common import ElementNotVisibleException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from utilities.meitav.fill_in_operation import fill_in_operation
from utilities.meitav.get_status import extract_status
from utilities.meitav.meitav_common import *
from utilities.meitav.start import start
from utilities.spreadsheet_operations import update_next_sell_in_spreadsheet, extract_next_sell_price

user = Barak
program_type = Gemel

today = datetime.now()
start_date = None
end_date = target_date = today + relativedelta(months=3, days=-1)

driver = start(user, program_type)
try:
    status = extract_status(driver)
    user = status['user']
    program_type = status['program_type']

    price = extract_next_sell_price(user)
    print(f"The sell price is {price}")

    sum = 10000
    units = math.floor(sum * 100 / price)
    update_next_sell_in_spreadsheet(user, program_type, price, f"{target_date:%d/%m/%Y}")

    fill_in_operation(driver, operation_type='sell', security_id=1144708, units=units, price=price,
                      target_date=target_date)

    """
    element = driver.find_element(By.XPATH, f"//*[text()='1144708']")
    element.click()
    time.sleep(1)

    buy_button = driver.find_element(By.CSS_SELECTOR, '.buy-btn.stock-info-header')
    print(buy_button)
    buy_button.click()

    wait_object = WebDriverWait(driver, 40, 1, ([ElementNotVisibleException]))
    wait_object.until(lambda x: x.find_element(By.XPATH, f"//*[text()='מחיר באגורות']") is not None)

    send_order_div = driver.find_element(By.CLASS_NAME, "sendOrder")
    sell_button = send_order_div.find_element(By.XPATH, f"//*[text()='מכירה']")
    sell_button.click()

    inputs = driver.find_elements(By.CSS_SELECTOR, ".send-order-control-item-body input")

    price_input = inputs[1]
    price_input.clear()
    price_input.send_keys(str(price))

    sum_input = inputs[3]
    sum_input.clear()
    sum_input.send_keys(str(sum))

    buttons = driver.find_elements(By.CSS_SELECTOR, "button[aria-label*='לחץ לבחירת תאריך']")
    until_date_button = buttons[1]
    until_date_button.click()

    current_month = get_hebrew_month_year()
    buttons = driver.find_elements(By.XPATH, f"//button[strong[normalize-space()='{current_month}']]")
    until_date_button = buttons[1]
    until_date_button.click()

    hebrew_month = hebrew_months[target_date.month]
    assert target_date.year == today.year

    month_button = driver.find_element(By.XPATH, f"//button[span[normalize-space()='{hebrew_month}']]")
    month_button.click()

    formatted_day = f"{target_date.day:02d}"

    date_picker_items = driver.find_elements(By.XPATH, "//div[@class='date-picker-item']")

    search_text = f"(.//tbody//button[span[normalize-space()='{formatted_day}']])"
    if target_date.day > 15:
        search_text += "[last()]"
    day_buttons = date_picker_items[1].find_elements(By.XPATH, search_text)
    day_buttons[0].click()
    """

finally:
    driver.quit()
