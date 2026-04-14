from datetime import datetime

from utilities.meitav.meitav_common import *
from utilities.database_access import get_worksheet

user = Barak
program_type = Gemel
units = 16
purchase_price = 6591

person_data = user_data[user]
sheet_name = person_data['transactions_sheet_name']
sheet = get_worksheet(sheet_name)

sheet_values = sheet.get()
new_row_index =len(sheet_values) - 1

current_date = datetime.now().strftime("%d.%m.%y")
program_name = "השתלמות" if program_type == Hishtalmut else "גמל"

new_row_values = [current_date, program_name, units, purchase_price]

sheet.insert_row(new_row_values, index=new_row_index)
