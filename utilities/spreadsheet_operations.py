import re
from string import ascii_uppercase

from utilities.database_access import get_worksheet
from utilities.meitav.meitav_common import users_data, Hishtalmut, Gemel

ETF_ID_ORDER = [1144708, 5112628, 5109889, 5114657, 5122510, 5113345]
CELL_REF_REGEX = re.compile(
    r'(\$?[A-Z]+)(\d+)'  # column (with optional $) + row number
)

def update_status_in_spreadsheet(name, program_type, status):
    this_user_data = users_data[name]
    main_sheet_name = this_user_data['main_sheet_name']
    user_sheet = get_worksheet(main_sheet_name)
    user_reference_row = this_user_data[program_type]['starting_row']

    prices_sheet = get_worksheet('$$$$')

    # Collect all updates into a batch payload
    user_updates = [
        {
            'range': f"D{user_reference_row}",
            'values': [[status['cash']]]
        }
    ]
    price_updates = []
    etf_index_to_price_row_index = {
        1144708: 59,
        5112628: 60
    }

    current_etf_id_order = [etf_id for etf_id in ETF_ID_ORDER if etf_id in status['holdings']]

    for etf_index, etf_id in enumerate(current_etf_id_order):
        holding = status['holdings'][etf_id]
        print(f"{etf_id} ---> {user_reference_row + etf_index + 3}")
        user_updates.append({
            'range': f"B{user_reference_row + etf_index + 3}",
            'values': [[holding['quantity']]]
        })
        row_index = etf_index_to_price_row_index[etf_id]
        price_updates.append({
            'range': f"B{row_index}",
            'values': [[holding['last_price']]]
        })

    user_sheet.batch_update(user_updates)
    prices_sheet.batch_update(price_updates)


def update_next_operation_in_spreadsheet(name, program_type, price, deadline, lines_before_reference_row):
    this_user_data = users_data[name]
    main_sheet_name = this_user_data['main_sheet_name']
    user_sheet = get_worksheet(main_sheet_name)
    reference_row = this_user_data[program_type]['starting_row']

    operation_row_index = reference_row - lines_before_reference_row
    user_sheet.update(values=[[price, deadline]], range_name=f"D{operation_row_index}:E{operation_row_index}")


def update_next_buy_in_spreadsheet(name, program_type, price, deadline):
    update_next_operation_in_spreadsheet(name, program_type, price, deadline, lines_before_reference_row=3)


def update_next_sell_in_spreadsheet(name, program_type, price, deadline):
    update_next_operation_in_spreadsheet(name, program_type, price, deadline, lines_before_reference_row=4)
    other_program_type = Gemel if program_type == Hishtalmut else Hishtalmut
    this_user_data = users_data[name]
    reference_row = this_user_data[other_program_type]['starting_row']
    sell_row_index = reference_row - 4
    ranges_to_clear = [f"D{sell_row_index}:E{sell_row_index}"]
    main_sheet_name = this_user_data['main_sheet_name']
    user_sheet = get_worksheet(main_sheet_name)
    user_sheet.batch_clear(ranges_to_clear)


def extract_next_sell_price(name):
    this_user_data = users_data[name]
    main_sheet_name = this_user_data['main_sheet_name']
    user_sheet = get_worksheet(main_sheet_name)
    next_sell_price_cell = this_user_data['next_sell_price_cell']
    next_sell_price = user_sheet.get(next_sell_price_cell)
    return int(next_sell_price[0][0])


def extract_excessive_cash(name, program_type):
    this_user_data = users_data[name]
    main_sheet_name = this_user_data['main_sheet_name']
    user_sheet = get_worksheet(main_sheet_name)
    starting_row = this_user_data[program_type]['starting_row']
    excessive_cash = user_sheet.get(f"D{int(starting_row)+2}")
    return excessive_cash[0][0]


def calculate_trade_formulas_range(column_letter, update_data, row):
    column_index = ascii_uppercase.index(column_letter.upper())
    range_start = 65 + column_index + len(update_data[0])
    range_end = range_start + 4
    return f"{chr(range_start)}{row}:{chr(range_end)}{row}"


def increment_unfixed_rows(formula: str) -> str:
    def replacer(match):
        col, row = match.groups()

        # If row is fixed ($ before row), do nothing
        if col.endswith('$'):  # not possible here, safeguard
            return match.group(0)

        # Check if row is fixed (preceded by $ in original text)
        start = match.start(2)
        if formula[start - 1] == '$':
            return match.group(0)

        return f"{col}{int(row) + 1}"

    return CELL_REF_REGEX.sub(replacer, formula)


def copy_formulas(sheet, column_letter, update_data, row):
    formulas_range = calculate_trade_formulas_range(column_letter, update_data, row - 1)
    print(f"range: {formulas_range}")
    formulas = sheet.get(formulas_range, value_render_option='FORMULA')
    print(f"range: {formulas}")
    updated_formulas = [
        [
            increment_unfixed_rows(cell) if isinstance(cell, str) and cell.startswith('=') else cell
            for cell in row
        ]
        for row in formulas
    ]
    formulas = updated_formulas
    return update_data[0] + formulas[0]