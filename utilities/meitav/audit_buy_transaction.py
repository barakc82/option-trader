from datetime import datetime

from utilities.database_access import get_worksheet
from utilities.meitav.audit import extract_completed_operations
from utilities.meitav.meitav_common import *
from utilities.meitav.start import start
from utilities.meitav.get_status import extract_status

user = Barak
program_type = Hishtalmut

person_data = users_data[user]

driver = start(user, program_type)


try:
    status = extract_status(driver)
    if status['account_id'] != users_data[user][program_type]['account_id']:
        raise Exception("Username and program type mismatch")

    units = 15
    purchase_price = 7114

    person_data = users_data[user]
    sheet_name = person_data['transactions_sheet_name']
    sheet = get_worksheet(sheet_name)

    all_completed_operations = extract_completed_operations(driver)

    col_k_values = sheet.col_values(11)
    k_column_values = [int(v) for v in col_k_values[50:] if v]
    sells_last_row_index = max(k_column_values)
    buys_first_row_index = sells_last_row_index + 3
    buys_sum_row_index = col_k_values.index(str(sells_last_row_index)) + 2
    buys_last_row_index = buys_sum_row_index-2
    audited_buys = sheet.get(f"A{buys_first_row_index}:D{buys_last_row_index}")

    for completed_operation in all_completed_operations:
        if completed_operation['security_id'] != 1144708.0:
            continue
        print(f"Completed leveraged operation: {completed_operation}")

    sheet_values = sheet.get()
    new_row_index = len(sheet_values) - 1

    current_date = datetime.now().strftime("%d.%m.%y")
    program_name = "השתלמות" if program_type == Hishtalmut else "גמל"

    new_row_values = [current_date, program_name, units, purchase_price]

    sheet.insert_row(new_row_values, index=new_row_index)

finally:
    driver.quit()
