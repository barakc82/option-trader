from datetime import datetime


Barak = 1
Mom = 2
Hilush = 3

Hishtalmut = 0
Gemel = 1

user_data = {
    Barak: {
        'main_sheet_name': 'ברק',
        'transactions_sheet_name': "Barak-transactions",
        'max_leveraged_share': 0.9,
        'min_leveraged_share': 0.5,
        Hishtalmut: {
            "username": "1366666962",
            "password": "28",
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
        Hishtalmut: {
            "username": "1320194894",
            "password": "29",
            'starting_row': 72
        },
        Gemel: {
            "username": "1320194895",
            "password": "28",
            'starting_row': 88
        }
    },

Hilush: {
        'main_sheet_name': 'הילוש',
        'transactions_sheet_name': "Hilush-transactions",
        Hishtalmut: {
            "username": "1320195347",
            "password": "28",
            'starting_row': 76
        },
        Gemel: {
            "username": "1320195719",
            "password": "28",
            'starting_row': 88
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
