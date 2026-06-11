import time

from dateutil.relativedelta import relativedelta

from selenium.common import ElementNotVisibleException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from utilities.meitav.fill_in_operation import fill_in_operation
from utilities.meitav.get_status import extract_status
from utilities.meitav.meitav_common import *
from utilities.meitav.spark_falling_knife import calculate_next_buy2
from utilities.meitav.start import start
from utilities.spreadsheet_operations import update_next_buy_in_spreadsheet

user = Barak
program_type = Gemel

person_data = users_data[user]

today = datetime.now()
start_date = None
target_date = today + relativedelta(months=3, days=-1)

driver = start(user, program_type)


try:
    status = extract_status(driver)
    if status['account_id'] != users_data[user][program_type]['account_id']:
        raise Exception("Username and program type mismatch")

    # price, units = calculate_next_buy(user, program_type)
    price, units = calculate_next_buy2(driver, status)

    fill_in_operation(driver, operation_type='buy', security_id=1144708, units=units, price=price, target_date=target_date)

    update_next_buy_in_spreadsheet(user, program_type, price, f"{target_date:%d/%m/%Y}")

finally:
    driver.quit()
