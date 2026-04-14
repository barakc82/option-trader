import os
import subprocess
import requests
import time

from selenium import webdriver
from selenium.common import ElementNotVisibleException, ElementClickInterceptedException
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options

from utilities.meitav.meitav_common import *

DEBUG_PORT = 9222
TARGET_URL = "https://sparkmeitav.ordernet.co.il/#/auth"


def is_chrome_debug_active(port=9222):
    try:
        # Chrome exposes this JSON endpoint when remote debugging is active
        response = requests.get(f"http://127.0.0.1:{port}/json/version", timeout=1)
        return response.status_code == 200
    except requests.exceptions.ConnectionError:
        return False


def launch_chrome_debug():
    """Launches Chrome in debug mode using your specific command."""
    # Using 'r' (raw strings) ensures the backslashes in Windows paths don't cause escape character errors
    chrome_path = r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
    user_data_dir = r"C:\ChromeDebug"
    port = 9222

    # Breaking the command into a list is the standard way to use subprocess
    command = [
        chrome_path,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run"
    ]
    print(command)
    print("No active session found. Launching Chrome...")

    # Define the flags needed to detach the process in Windows
    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP

    # Pass the flags to Popen
    subprocess.Popen(command, creationflags=flags)

    # Give Chrome a couple of seconds to fully spin up and open the local port
    time.sleep(2)

# & "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="C:\ChromeDebug" --no-first-run
def connect_to_investment_tab(name, account_type):
    # 1. Setup options to connect to the existing Chrome instance
    os.environ['NO_PROXY'] = '127.0.0.1,localhost'
    chrome_options = Options()
    chrome_options.add_experimental_option("debuggerAddress", "127.0.0.1:9222")

    # 2. Attach the driver (this won't open a new window)
    driver = webdriver.Chrome(options=chrome_options)

    # 3. Identify the correct tab
    # We loop through every open tab to find the one belonging to the investment house
    target_keyword = "Meitav"  # <-- CHANGE THIS to your investment house name (e.g., "IBI", "Psagot", "Bank")

    found = False
    for handle in driver.window_handles:
        driver.switch_to.window(handle)
        if target_keyword.lower() in driver.title.lower() or target_keyword.lower() in driver.current_url.lower():
            print(f"Successfully attached to: {driver.title}")
            found = True
            break

    if not found:
        print(f"Navigating to {TARGET_URL}...")
        driver.get(TARGET_URL)

    return driver


def try_to_click(btn):
    try:
        btn.click()
        return True
    except ElementClickInterceptedException:
        print(f"Button is not ready: {btn.text}")
        return False

def login(driver, name, account_type):
    print(f"Starting login")
    account_data = user_data[name][account_type]
    username_field = driver.find_element(By.NAME, "username")
    print(f"Is the username field enabled? {username_field.is_enabled()}")
    username_field.clear()
    username_field.send_keys(account_data["username"])
    password_field = driver.find_element(By.NAME, "password")
    password_field.clear()
    password_field.send_keys("Cz9nqupx" + account_data["password"])
    enter_button = driver.find_element(By.ID, "btnSubmit")
    enter_button.click()
    wait_object = WebDriverWait(driver, 40, 1, ([ElementNotVisibleException]))
    print(f"Waiting for INDshadowRootWrap to be displayed")
    # time.sleep(40)
    iNDshadowRootWrap = driver.find_element(By.ID, "INDshadowRootWrap")
    # print(iNDshadowRootWrap.is_displayed()) # False
    #is_disappeared = wait_object.until(lambda x: x.find_element(By.ID, "INDshadowRootWrap") is not None)
    wait_object.until(lambda x: x.find_element(By.XPATH, '//*[text()="דף הבית"]') is not None)
    #homepage_text = driver.find_element(By.XPATH, '//*[text()="דף הבית"]')
    #print(f"Was the homepage text found? {homepage_text is not None}")

    wait_object.until(lambda x: len(x.find_elements(By.CSS_SELECTOR, "div.btn-container > button")) > 0)
    driver.find_elements(By.CSS_SELECTOR, "div.btn-container > button")
    all_buttons = [None]
    while all_buttons:
        enter_system_buttons = driver.find_elements(By.CSS_SELECTOR, "div.btn-container > button")
        approve_buttons = [] # driver.find_elements(By.CSS_SELECTOR, "button[ng-click='select()']")
        close_buttons = driver.find_elements(By.XPATH, "//button[normalize-space()='סגור']")
        all_buttons = enter_system_buttons + approve_buttons + close_buttons
        print(f"barak: number of buttons is {len(all_buttons)}")
        for btn in all_buttons:
            if "כניסה למערכת" in btn.text:
                success_result = try_to_click(btn)
                if success_result:
                    print("✅ 'Enter system' button clicked.")
                    break
            if "אישור" in btn.text:
                success_result = try_to_click(btn)
                if success_result:
                    print("✅ 'Confirm' button clicked.")
                    break
            if "סגור" in btn.text:
                success_result = try_to_click(btn)
                if success_result:
                    print("✅ 'Close' button clicked.")
                    break
            time.sleep(0.5)

def start(name, program_type):

    if not is_chrome_debug_active(DEBUG_PORT):
        launch_chrome_debug()

    driver = connect_to_investment_tab(name, program_type)

    # We use a Wait here in case the page is still loading or refreshing
    wait = WebDriverWait(driver, 10)
    current_url = driver.current_url
    print(f"Current URL: {current_url}")
    if current_url.endswith('auth'):
        login(driver, name, program_type)

    return driver