from utilities.database_access import get_worksheet
from utilities.meitav.meitav_common import user_data

ETF_ID_ORDER = [1144708, 5112628, 5109889, 5114657, 5122510, 5113345]

def update_status_in_spreadsheet(name, program_type, status):
    this_user_data = user_data[name]
    main_sheet_name = this_user_data['main_sheet_name']
    user_sheet = get_worksheet(main_sheet_name)
    reference_row = this_user_data[program_type]['starting_row']
    user_sheet.update(values=[[status['cash']]], range_name=f"D{reference_row}")

    current_etf_id_order = [etf_id for etf_id in ETF_ID_ORDER if etf_id in status['holdings']]

    for etf_index, etf_id in enumerate(current_etf_id_order):
        holding = status['holdings'][etf_id]
        print(f"{etf_id} ---> {reference_row + etf_index + 3}")
        user_sheet.update(values=[[holding['quantity']]], range_name=f"B{reference_row + etf_index + 3}")

    #ta125_ptf_holding = status['holdings'][5112628]
    #user_sheet.update(values=[[ta125_ptf_holding['quantity']]], range_name=f"B{reference_row + 4}")


def update_next_buy_in_spreadsheet(name, program_type, price, deadline):
    this_user_data = user_data[name]
    main_sheet_name = this_user_data['main_sheet_name']
    user_sheet = get_worksheet(main_sheet_name)
    reference_row = this_user_data[program_type]['starting_row']

    buy_row_index = reference_row-3
    user_sheet.update(values=[[price, deadline]], range_name=f"D{buy_row_index}:E{buy_row_index}")
