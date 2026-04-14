import time
from dateutil.relativedelta import relativedelta

from selenium.common import ElementNotVisibleException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from utilities.meitav.get_status import extract_status
from utilities.meitav.meitav_common import *
from utilities.meitav.ss_falling_knife import calculate_next_buy
from utilities.meitav.start import start

user = Barak
program_type = Gemel

person_data = user_data[user]

today = datetime.now()
start_date = None
end_date = target_date = today + relativedelta(months=3, days=-1)

driver = start(user, program_type)
status = extract_status(driver)

price, units = calculate_next_buy(user, program_type)
#price, units = calculate_next_buy2(driver, status)

# wait = WebDriverWait(driver, 10)
# wait.until(EC.invisibility_of_element_located((By.CLASS_NAME, "send-order-modal")))
element = driver.find_element(By.XPATH, f"//*[text()='1144708']")
element.click()
time.sleep(1)

buy_button = driver.find_element(By.CSS_SELECTOR, '.buy-btn.stock-info-header')
print(buy_button)
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
assert target_date.year == today.year

month_button = driver.find_element(By.XPATH, f"//button[span[normalize-space()='{hebrew_month}']]")
month_button.click()

formatted_day = f"{target_date.day:02d}"

date_picker_items = driver.find_elements(By.XPATH, "//div[@class='date-picker-item']")

day_buttons = date_picker_items[1].find_elements(By.XPATH, f".//tbody//button[span[normalize-space()='{formatted_day}']]")
day_buttons[0].click()