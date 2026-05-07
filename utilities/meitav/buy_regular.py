import sys

from dateutil.relativedelta import relativedelta

from utilities.meitav.fill_in_operation import fill_in_operation_by_total_value
from utilities.meitav.get_status import extract_status
from utilities.meitav.meitav_common import *
from utilities.meitav.start import start
from utilities.spreadsheet_operations import update_next_buy_in_spreadsheet, extract_excessive_cash

user = Barak
program_type = Gemel

person_data = users_data[user]

today = datetime.now()
start_date = None
target_date = today + relativedelta(months=3, days=-1)

driver = start(user, program_type)


try:
    status = extract_status(driver)
    user = status['user']
    program_type = status['program_type']

    if status['account_id'] != users_data[user][program_type]['account_id']:
        raise Exception("Username and program type mismatch")

    excessive_cash = extract_excessive_cash(user, program_type)
    print(excessive_cash)

    holdings = status['holdings']
    minimal_total = sys.float_info.max
    minimal_total_security_id = None
    for security_id, holding in holdings.items():
        quantity = holding['quantity']
        last_price = holding['last_price']
        total = quantity * last_price
        if total < minimal_total:
            minimal_total = total
            minimal_total_security_id = security_id

    fill_in_operation_by_total_value(driver, operation_type='buy', security_id=minimal_total_security_id, total_value=excessive_cash)

finally:
    driver.quit()
