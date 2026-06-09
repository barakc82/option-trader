from utilities.database_access import get_worksheet
from utilities.meitav.meitav_common import users_data, Hishtalmut, Gemel

ETF_ID_ORDER = [1144708, 5112628, 5109889, 5114657, 5122510, 5113345]

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
    return next_sell_price[0][0]


def extract_excessive_cash(name, program_type):
    this_user_data = users_data[name]
    main_sheet_name = this_user_data['main_sheet_name']
    user_sheet = get_worksheet(main_sheet_name)
    starting_row = this_user_data[program_type]['starting_row']
    excessive_cash = user_sheet.get(f"D{int(starting_row)+2}")
    return excessive_cash[0][0]


