from string import ascii_uppercase
from datetime import datetime
from gspread_formatting import CellFormat, TextFormat, format_cell_range, get_effective_format
import re

from utilities.database_access import get_worksheet
from utilities.meitav.meitav_common import *

CELL_REF_REGEX = re.compile(
    r'(\$?[A-Z]+)(\d+)'  # column (with optional $) + row number
)

# =======================

person = Hilush
program_type = Gemel
shares = 111
trade_price = 8932

# =======================

barak_data = {
    'person': Barak,
    'sheet_name': "Barak-transactions",
    'trade_starting_row': 149,
    'trade_starting_column': 'A'
}

mom_data = {
    'person': Mom,
    'sheet_name': "Mom-transactions",
    'trade_starting_row': 180,
    'trade_starting_column': 'O'
}

hilush_data = {
    'person': Hilush,
    'sheet_name': "Hilush-transactions",
    'trade_starting_row': 50,
    'trade_starting_column': 'A'
}

data_list = [barak_data, mom_data, hilush_data]

def remove_bold_from_row(sheet, row, column_index):
    unbold_fmt = CellFormat(
        textFormat=TextFormat(bold=False)
    )
    current_column = chr(65 + column_index - 1)
    format_cell_range(sheet, f"{current_column}{row}", unbold_fmt)

def copy_formats(sheet, starting_column_index, range_length, row):
    for i in range(range_length):
        current_column = chr(65 + starting_column_index + i - 1)
        format_source_cell = f"{current_column}{row}"
        format = get_effective_format(sheet, format_source_cell)
        format_target_cell = f"{current_column}{row + 1}"
        format_cell_range(sheet, format_target_cell, format)


def normalize_formula(cell):
    if isinstance(cell, str) and cell.startswith("'="):
        return cell[1:]  # remove leading apostrophe
    return cell


def find_next_row(start_row, col_values):
    # Scan from start_row forward to find first empty
    row = start_row
    while True:
        # Expand list if needed
        if row > len(col_values):
            break

        value = col_values[row - 1]  # convert to 0-based
        if value == "" or value is None:
            break

        row += 1
    return row


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


def add_formulas(column_letter, update_data, row):
    formulas_range = calculate_trade_formulas_range(column_letter, update_data, row - 1)
    print(f"range: {formulas_range}")
    formulas = sheet.get(formulas_range, value_render_option='FORMULA')
    print(f"range: {formulas}")
    """fixed_formulas = [
        [normalize_formula(cell) for cell in row]
        for row in formulas
    ]"""
    updated_formulas = [
        [
            increment_unfixed_rows(cell) if isinstance(cell, str) and cell.startswith('=') else cell
            for cell in row
        ]
        for row in formulas
    ]
    formulas = updated_formulas
    return update_data[0] + formulas[0]


def calculate_trade_ending_column(column_letter, update_data):
    column_index = ascii_uppercase.index(column_letter.upper()) + 1
    return chr(65 + column_index + len(update_data[0]) - 1)


def calculate_trade_formulas_range(column_letter, update_data, row):
    column_index = ascii_uppercase.index(column_letter.upper())
    range_start = 65 + column_index + len(update_data[0])
    range_end = range_start + 4
    return f"{chr(range_start)}{row}:{chr(range_end)}{row}"


person_data = next((m for m in data_list if m.get("person") == person), None)

sheet_name = person_data['sheet_name']
sheet = get_worksheet(sheet_name)
column_letter = person_data['trade_starting_column']
column_index = ascii_uppercase.index(column_letter.upper()) + 1

start_row = person_data['trade_starting_row']
col_values = sheet.col_values(column_index)

# If column shorter than start_row, next empty is just start_row
if len(col_values) < start_row:
    raise f"{column_letter}{start_row}"

row = find_next_row(start_row, col_values)

current_date = datetime.now().strftime("%d.%m.%y")
program_name = "השתלמות" if program_type == Hishtalmut else "גמל"
update_data = [[current_date, program_name, shares, trade_price]]

update_data[0] = add_formulas(column_letter, update_data, row)
trade_ending_column = calculate_trade_ending_column(column_letter, update_data)
range_name = f"{column_letter}{row}:{trade_ending_column}{row}"

print(update_data)
print(range_name)

sheet.update(values=update_data, range_name=range_name, value_input_option='USER_ENTERED')

copy_formats(sheet, column_index, len(update_data[0]), row - 1)
remove_bold_from_row(sheet, row - 1, column_index + 6)
remove_bold_from_row(sheet, row - 1, column_index + 8)

if person == Mom:
    update_data = [[current_date, shares, trade_price]]
    if program_type == Hishtalmut:
        start_row = 120
        column_letter = 'A'
        column_index = ascii_uppercase.index(column_letter.upper()) + 1
        col_values = sheet.col_values(column_index)
        row = find_next_row(start_row, col_values)
        update_data[0] = add_formulas(column_letter, update_data, row)
        trade_ending_column = calculate_trade_ending_column(column_letter, update_data)
        range_name = f"{column_letter}{row}:{trade_ending_column}{row}"
        sheet.update(values=update_data, range_name=range_name, value_input_option='USER_ENTERED')
        copy_formats(sheet, column_index, len(update_data[0]), row - 1)
        remove_bold_from_row(sheet, row - 1, column_index + 4)

    if program_type == Gemel:
        start_row = 80
        column_letter = 'H'
        column_index = ascii_uppercase.index(column_letter.upper()) + 1
        col_values = sheet.col_values(column_index)
        row = find_next_row(start_row, col_values)
        update_data[0] = add_formulas(column_letter, update_data, row)
        trade_ending_column = calculate_trade_ending_column(column_letter, update_data)
        range_name = f"{column_letter}{row}:{trade_ending_column}{row}"
        sheet.update(values=update_data, range_name=range_name, value_input_option='USER_ENTERED')
        copy_formats(sheet, column_index, len(update_data[0]), row - 1)
        remove_bold_from_row(sheet, row - 1, column_index + 4)
