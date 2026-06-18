from datetime import datetime, timedelta

from utilities.database_access import get_worksheet
from utilities.meitav.audit import extract_completed_operations
from utilities.meitav.meitav_common import *
from utilities.meitav.start import start
from utilities.meitav.get_status import extract_status

user = Barak
program_type = Hishtalmut

person_data = users_data[user]

driver = start(user, program_type)

DATE = 0
PROGRAM = 1
UNITS = 2
PRICE = 3



try:
    status = extract_status(driver)
    if status['account_id'] != users_data[user][program_type]['account_id']:
        raise Exception("Username and program type mismatch")

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

    now = datetime.now()
    date = now if now.hour >= 10 else now - timedelta(days=1)
    current_date = date.strftime("%d.%m.%y")
    hebrew_program_name = "השתלמות" if program_type == Hishtalmut else "גמל"

    for completed_operation in all_completed_operations:
        if completed_operation['security_id'] != 1144708.0:
            continue
        print(f"Completed leveraged operation: {completed_operation}")
        if completed_operation['operation_type'] != 'BUY':
            continue
        is_operation_already_audited = False
        units = completed_operation['quantity']
        purchase_price = int(completed_operation['price'])
        for audited_buy in audited_buys:
            if audited_buy[DATE] != current_date or audited_buy[PROGRAM] != hebrew_program_name:
                continue

            #if audited_buy[UNITS] != completed_operation['quantity'] or audited_buy[PRICE] != completed_operation['price']:
            #    pass
            print(f"checking against: {audited_buy}")
            audited_buy_units = int(audited_buy[UNITS])
            audited_buy_price = int(audited_buy[PRICE])
            if audited_buy_units == units and audited_buy_price == purchase_price:
                is_operation_already_audited = True

        if not is_operation_already_audited:

            sheet_values = sheet.get()
            new_row_index = len(sheet_values) - 1
            new_row_values = [current_date, hebrew_program_name, units, purchase_price]

            sheet.insert_row(new_row_values, index=new_row_index)

finally:
    driver.quit()
