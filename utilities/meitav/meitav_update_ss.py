from utilities.meitav.get_status import extract_status
from utilities.meitav.meitav_common import *
from utilities.meitav.start import start
from utilities.spreadsheet_operations import update_status_in_spreadsheet

# & C:\\"Program Files"\\Google\\Chrome\\Application\\chrome.exe --remote-debugging-port=9222 --user-data-dir=C:\\ChromeDebug --no-first-run


def main(user, program_type):
    driver = start(user, program_type)

    try:
        status = extract_status(driver)
        assert status['user'] == user
        assert status['program_type'] == program_type
        update_status_in_spreadsheet(user, program_type, status)
    finally:
        driver.quit()


if __name__ == "__main__":
    main(Barak, Hishtalmut)
