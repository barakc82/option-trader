from datetime import datetime


Barak = 1
Mom = 2
Hilush = 3

Hishtalmut = 0
Gemel = 1

users_data = {
    Barak: {
        'main_sheet_name': 'ברק',
        'transactions_sheet_name': "Barak-transactions",
        'max_leveraged_share': 0.9,
        'min_leveraged_share': 0.5,
        'next_sell_price_cell': 'V10',
        Hishtalmut: {
            "username": "1366666962",
            "password": "29",
            'account_id': "098274",
            'starting_row': 160
        },
        Gemel: {
            "username": "1320193631",
            "password": "29",
            'account_id': "099305",
            'starting_row': 172
        }
    },

    Mom: {
        'main_sheet_name': 'אמא',
        'transactions_sheet_name': "Mom-transactions",
        'max_leveraged_share': 0.2,
        'min_leveraged_share': 0.05,
        'next_sell_price_cell': 'E66',
        Hishtalmut: {
            "username": "1320194894",
            "password": "29",
            'starting_row': 72,
            'account_id': "099563"
        },
        Gemel: {
            "username": "1320194895",
            "password": "29",
            'starting_row': 88,
            'account_id': "099562"
        }
    },

Hilush: {
        'main_sheet_name': 'הילוש',
        'transactions_sheet_name': "Hilush-transactions",
        'max_leveraged_share': 0.7,
        'min_leveraged_share': 0.1,
        'next_sell_price_cell': 'F67',
        Hishtalmut: {
            "username": "1320195347",
            "password": "29",
            'starting_row': 76,
            'account_id': "099623"
        },
        Gemel: {
            "username": "1320195719",
            "password": "29",
            'starting_row': 88,
            'account_id': "099686"
        }
    }
}

hebrew_months = {
        1: "ינואר",
        2: "פברואר",
        3: "מרץ",
        4: "אפריל",
        5: "מאי",
        6: "יוני",
        7: "יולי",
        8: "אוגוסט",
        9: "ספטמבר",
        10: "אוקטובר",
        11: "נובמבר",
        12: "דצמבר"
    }

def get_hebrew_month_year():
    now = datetime.now()
    month_hebrew = hebrew_months[now.month]
    return f"{month_hebrew} {now.year}"
