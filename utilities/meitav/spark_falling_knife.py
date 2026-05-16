import math
import time

from selenium.webdriver.common.by import By

from utilities.meitav.get_status import extract_status
from utilities.meitav.meitav_common import *
from utilities.meitav.start import start
from utilities.meitav.falling_knife_logic import FallingKnifeCalculator

# =======================

person = Barak
program_type = Hishtalmut

# =======================

def calculate_next_buy2(driver, status):
    # Select leveraged ETF to ensure UI state
    element = driver.find_element(By.XPATH, f"//*[text()='1144708']")
    element.click()
    time.sleep(1)

    holdings = status['holdings']
    ta35_3x_holding = holdings[1144708]
    leveraged_quantity = ta35_3x_holding['quantity']
    current_leveraged_price = int(ta35_3x_holding['last_price'])

    base_etfs_data = []
    for etf_id, holding in holdings.items():
        if etf_id == 1144708:
            continue
        base_etfs_data.append((holding['quantity'], holding['last_price']))

    cash = status['cash']
    person_data = users_data[person]
    
    calculator = FallingKnifeCalculator(
        leveraged_quantity, 
        current_leveraged_price, 
        base_etfs_data, 
        cash, 
        person_data
    )
    
    target_price, result = calculator.find_next_transfer_price()
    units = calculator.print_summary(target_price, result)
    
    return target_price, units


if __name__ == '__main__':
    driver = start(person, program_type)
    try:
        status = extract_status(driver)
        calculate_next_buy2(driver, status)
    finally:
        driver.quit()
