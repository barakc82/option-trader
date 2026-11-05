import time

from utilities.meitav.fill_in_operation import fill_in_operation_by_total_value
from utilities.meitav.get_status import extract_status
from utilities.meitav.meitav_common import *
from utilities.meitav.start import start
from utilities.spreadsheet_operations import update_status_in_spreadsheet

LEVERAGED_SECURITY_ID = 1144708

def main(user, program_type):
    driver = start(user, program_type)
    try:
        status = extract_status(driver)
        user = status['user']
        program_type = status['program_type']

        holdings = status['holdings']
        total_holdings_value = sum(h['quantity'] * h['last_price'] for h in holdings.values())
        monthly_fees = total_holdings_value * 0.01 * 0.0025 / 12
        print(f"Monthly fees {monthly_fees}")
        three_year_fees = monthly_fees * 36
        sell_sum = round(three_year_fees - status['cash'], 2)

        largest_total = -1
        largest_security_id = None
        for security_id, holding in holdings.items():
            if security_id == LEVERAGED_SECURITY_ID:
                continue
            total = holding['quantity'] * holding['last_price']
            if total > largest_total:
                largest_total = total
                largest_security_id = security_id

        print(f"Selling security {largest_security_id} for {sell_sum}")

        fill_in_operation_by_total_value(driver, operation_type='sell', security_id=largest_security_id, total_value=sell_sum)
        status = extract_status(driver)
        assert status['user'] == user
        assert status['program_type'] == program_type
        update_status_in_spreadsheet(user, program_type, status)

    finally:
        driver.quit()


if __name__ == "__main__":
    main(Barak, Gemel)
