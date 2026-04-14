import re
import math
import time

from selenium.webdriver.common.by import By

from utilities.meitav.get_status import extract_status
from utilities.meitav.meitav_common import *
from utilities.meitav.start import start

CELL_REF_REGEX = re.compile(
    r'(\$?[A-Z]+)(\d+)'  # column (with optional $) + row number
)

BOTTOM = 695
TOP = 8752

# =======================

person = Barak
program_type = Gemel

# =======================

def calculate_next_buy2(driver, status):

    def leveraged_price_for_transfer_by_incrementing_price(max_leveraged_share, min_leveraged_share):
        global lower_leveraged_price, new_leveraged_sum, new_base_sum, new_total, new_leveraged_share, base_share, new_status, required_leveraged_share, required_transfer, leveraged_price_for_transfer
        previous_iteration = {}
        for lower_leveraged_price in range(leveraged_price, leveraged_price * 2):
            new_leveraged_sum = lower_leveraged_price * leveraged_quantity / 100
            new_base_sum = sum(base_quantity * base_price for base_quantity, base_price in base_etfs_data) / 100
            initial_total = new_leveraged_sum + new_base_sum + cash
            total_fees = initial_total * 0.0025 * 3
            cash_for_investment = max(cash - total_fees, 0)
            new_total = new_leveraged_sum + new_base_sum + cash_for_investment
            new_leveraged_share = new_leveraged_sum / new_total
            base_share = new_base_sum / new_total
            new_status = (lower_leveraged_price - BOTTOM) / (TOP - BOTTOM)
            required_leveraged_share = min_leveraged_share +    (max_leveraged_share - min_leveraged_share) * (1 - new_status)
            required_transfer = (required_leveraged_share - new_leveraged_share) * new_total
            print(
                f"For the price of {lower_leveraged_price} the required transfer is {required_transfer}, using total: {new_total}")
            if required_transfer < 1000 and 'new_leveraged_sum' in previous_iteration:
                new_leveraged_sum = previous_iteration["new_leveraged_sum"]
                new_base_sum = previous_iteration["new_base_sum"]
                new_total = previous_iteration["new_total"]
                new_leveraged_share = previous_iteration["new_leveraged_share"]
                base_share = previous_iteration["base_share"]
                new_status = previous_iteration["new_status"]
                required_leveraged_share = previous_iteration["required_leveraged_share"]
                required_transfer = previous_iteration["required_transfer"]

                leveraged_price_for_transfer = lower_leveraged_price - 1
                print(f"At leveraged price of {lower_leveraged_price}, new status:\t{new_status}\n"
                      f"new leveraged sum:\t{new_leveraged_sum}\n"
                      f"new base sum:\t{new_base_sum}\n"
                      f"cash for investment:\t{cash_for_investment}\n"
                      f"new total:\t{new_total}\n"
                      f"required leveraged share:\t{required_leveraged_share:.2f}\n"
                      f"new leveraged share:\t{new_leveraged_share:.2f}")
                break

            previous_iteration = {
                "new_leveraged_sum": new_leveraged_sum,
                "new_base_sum": new_base_sum,
                "new_total": new_total,
                "new_leveraged_share": new_leveraged_share,
                "base_share": base_share,
                "new_status": new_status,
                "required_leveraged_share": required_leveraged_share,
                "required_transfer": required_transfer
            }
        return leveraged_price_for_transfer

    def leveraged_price_for_transfer_by_decreasing_price(max_leveraged_share, min_leveraged_share):
        leveraged_price_for_transfer = 0

        for lower_leveraged_price in range(leveraged_price, 0, -1):
            new_leveraged_sum = lower_leveraged_price * leveraged_quantity / 100
            new_base_sum = sum(base_quantity * base_price for base_quantity, base_price in base_etfs_data) / 100
            initial_total = new_leveraged_sum + new_base_sum + cash
            total_fees = initial_total * 0.0025 * 3
            cash_for_investment = max(cash - total_fees, 0)
            new_total = new_leveraged_sum + new_base_sum + cash_for_investment
            new_leveraged_share = new_leveraged_sum / new_total
            new_status = (lower_leveraged_price - BOTTOM) / (TOP - BOTTOM)
            required_leveraged_share = min_leveraged_share+(max_leveraged_share-min_leveraged_share)*(1-new_status)
            required_transfer = (required_leveraged_share - new_leveraged_share) * new_total
            print(f"{lower_leveraged_price} ----> {required_transfer}")

            if required_transfer > 1000:
                leveraged_price_for_transfer = lower_leveraged_price
                print(f"At leveraged price of {lower_leveraged_price}, new status:\t{new_status}\n"
                      f"new leveraged sum:\t{new_leveraged_sum}\n"
                      f"new base sum:\t{new_base_sum}\n"
                      f"new total:\t{new_total}\n"
                      f"required leveraged share:\t{required_leveraged_share:.2f}\n"
                      f"new leveraged share:\t{new_leveraged_share:.2f}")
                break
        return leveraged_price_for_transfer

    element = driver.find_element(By.XPATH, f"//*[text()='1144708']")
    element.click()
    time.sleep(1)

    holdings = status['holdings']
    ta35_3x_holding = holdings[1144708]
    leveraged_quantity = ta35_3x_holding['quantity']
    leveraged_price = ta35_3x_holding['last_price']

    base_etfs_data = []
    for etf_id, holding in holdings.items():
        if etf_id == 1144708:
            continue
        base_quantity = holding['quantity']
        base_price = holding['last_price']
        base_etfs_data.append((base_quantity, base_price))
    should_buy_now = holdings[3][5] == 'Buy'
    purchase_sum = float(holdings[3][6]) if should_buy_now else 0

    cash = status['cash']

    leveraged_price_for_transfer = 0

    person_data = user_data[person]
    max_leveraged_share = person_data['max_leveraged_share']
    min_leveraged_share = person_data['min_leveraged_share']

    if should_buy_now and purchase_sum > 1000:
        leveraged_price_for_transfer = leveraged_price_for_transfer_by_incrementing_price(max_leveraged_share, min_leveraged_share)
    else:
        leveraged_price_for_transfer = leveraged_price_for_transfer_by_decreasing_price(max_leveraged_share, min_leveraged_share)

    units = math.ceil(100000 / leveraged_price_for_transfer)
    print(f"Next transfer is at {leveraged_price_for_transfer}, units: {units}")
    return leveraged_price_for_transfer, units

if __name__ == '__main__':
    driver = start(person, program_type)
    status = extract_status(driver)
    calculate_next_buy2(person, program_type)