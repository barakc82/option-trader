import pyautogui
import pyperclip
import time
import sys
import platform


def find_tab_gui(search_string, max_tabs=20):
    # Determine if we are on Mac (Darwin) or Windows/Linux to set the correct keys
    is_mac = platform.system() == 'Darwin'

    # Key mappings
    select_url_key = 'command' if is_mac else 'ctrl'  # Cmd+L or Ctrl+L
    copy_key = 'command' if is_mac else 'ctrl'  # Cmd+C or Ctrl+C

    print("--- GUI AUTOMATION STARTED ---")
    print(f"I need to find a tab with: '{search_string}'")
    print("PLEASE CLICK ON YOUR CHROME BROWSER NOW.")
    print("You have 5 seconds before I start hijacking your keyboard...")

    # 1. Give the user time to focus the correct window
    for i in range(5, 0, -1):
        print(f"{i}...", end=' ', flush=True)
        time.sleep(1)
    print("\nStarting search! Do not touch the mouse/keyboard.")

    # 2. Iterate through tabs
    for i in range(max_tabs):
        # A. Highlight the Address Bar (Ctrl+L / Cmd+L)
        pyautogui.hotkey(select_url_key, 'l')
        time.sleep(0.1)  # Small pause for stability

        # B. Copy the URL (Ctrl+C / Cmd+C)
        pyautogui.hotkey(copy_key, 'c')
        time.sleep(0.1)

        # C. Read the clipboard
        try:
            current_url = pyperclip.paste()
        except Exception:
            current_url = ""

        # D. Check if we found it
        if search_string in current_url:
            print(f"\n✅ FOUND IT!")
            print(f"URL: {current_url}")
            print("Stopping script here. The correct tab is currently open.")
            return

        # E. If not found, switch to the next tab (Ctrl+Tab is standard for both)
        # Note: On some Macs, Ctrl+Tab works, but sometimes you might need Cmd+Option+Right
        pyautogui.hotkey('ctrl', 'tab')
        time.sleep(0.3)  # Wait for the tab to visually switch

    print("\n❌ Checked multiple tabs but didn't find the URL.")
    print("Make sure the tab is open and you focused the browser correctly.")


if __name__ == "__main__":
    # Fail-safe: You can slam the mouse into any corner of the screen to kill the script instantly
    pyautogui.FAILSAFE = True

    find_tab_gui("sparkmeitav")