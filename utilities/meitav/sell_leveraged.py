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
finally:
    driver.quit()
