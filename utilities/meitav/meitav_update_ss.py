import re
import traceback

from selenium.common import NoSuchElementException
from selenium.webdriver.common.by import By

from utilities.meitav.get_status import extract_status
from utilities.meitav.meitav_common import *
from utilities.meitav.start import start
from utilities.spreadsheet_update import update_status_in_spreadsheet

# & C:\\"Program Files"\\Google\\Chrome\\Application\\chrome.exe --remote-debugging-port=9222 --user-data-dir=C:\\ChromeDebug --no-first-run

if __name__ == "__main__":
    name = Mom
    program_type = Hishtalmut

    driver = start(name, program_type)

    try:
        status = extract_status(driver)
        update_status_in_spreadsheet(name, program_type, status)
    finally:
        driver.quit()
