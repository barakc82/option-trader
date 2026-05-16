import math
import logging

from utilities.database_access import get_worksheet
from utilities.meitav.meitav_common import *
from utilities.meitav.falling_knife_logic import FallingKnifeCalculator

# =======================

person = Barak
program_type = Gemel

# =======================

should_decrease_base = True

def calculate_next_buy(person, program_type):
    person_data = users_data[person]

    sheet_name = person_data['main_sheet_name']
    sheet = get_worksheet(sheet_name)

    # 1. Fetch holdings data from spreadsheet
    start_row = person_data[program_type]['starting_row']
    number_of_rows = 9
    holdings = sheet.get(f'A{start_row}:G{int(start_row) + number_of_rows - 1}')

    # Leveraged ETF is expected at index 3
    leveraged_quantity = int(holdings[3][1])
    leveraged_price = round(float(holdings[3][2]))

    # Base ETFs follow starting from index 4
    base_etfs_data = []
    for base_etf_index in range(4, number_of_rows):
        potential_base_quantity = holdings[base_etf_index][1]
        if not potential_base_quantity:
            break
        base_quantity = int(holdings[base_etf_index][1])
        base_price = float(holdings[base_etf_index][2])
        base_etfs_data.append((base_quantity, base_price))
    
    print(f"The base ETF data is {base_etfs_data}")
    
    # Cash balance from row 0, column 3
    cash = float(holdings[0][3].replace(",", ""))

    # 2. Run simulation using shared logic
    calculator = FallingKnifeCalculator(
        leveraged_quantity,
        leveraged_price,
        base_etfs_data,
        cash,
        person_data,
        should_decrease_base=should_decrease_base
    )

    target_price, result = calculator.find_next_transfer_price()
    units = calculator.print_summary(target_price, result)

    return target_price, units

if __name__ == '__main__':
    calculate_next_buy(person, program_type)
