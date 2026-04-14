import socket
import subprocess
import time
import logging
import requests

# =======================
# CONFIGURATION
# =======================
VM_USER = "barak"
VM_NAME = "medium-sc"
ZONE = "us-east1-c"
CHECK_INTERVAL = 300  # seconds between checks
GCLOUD_PATH = r"C:\\Users\\Barak\\AppData\\Local\\Google\\Cloud SDK\\google-cloud-sdk\\bin\gcloud.cmd"

# Create a logger
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

TELEGRAM_BOT_TOKEN = '8161204170:AAGRCLXSgBzmhukhFPlTTnAXeagv7LJmE3o'
TELEGRAM_CHAT_ID = '1796107185'


# =======================
# FUNCTIONS
# =======================
def send_telegram_message(message):
    url = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage'
    params = {'chat_id': TELEGRAM_CHAT_ID, 'text': message}
    response = requests.get(url, params=params)
    if response.status_code == 200:
        logger.info('Message sent!')
    else:
        logger.info(f'Failed to send message: {response.status_code}')


def check_ssh():
    try:
        command_list = [GCLOUD_PATH, "compute", "ssh", VM_NAME, "--zone", ZONE, "--command", "echo ok", "--quiet"]
        print(
            f"Running: & 'C:\\Users\\Barak\\AppData\\Local\\Google\\Cloud SDK\\google-cloud-sdk\\bin\\gcloud.cmd' compute ssh {VM_NAME} --zone {ZONE} --command 'echo ok'")
        result = subprocess.run(
            command_list,
            capture_output=True,
            text=True,
            timeout=60
        )
        if not "ok" in result.stdout:
            print(f"Result: {result.stdout}")
        return "ok" in result.stdout
    except Exception as e:
        print(f"SSH check failed: {e}")
        return False


def reboot_vm(vm_name, zone):
    """Reboot the VM using gcloud CLI."""
    try:
        print("Rebooting VM...")
        send_telegram_message("Rebooting VM...")
        subprocess.run([GCLOUD_PATH, "compute", "instances", "reset", vm_name, "--zone", zone], check=True)
        print("VM reboot command sent successfully.")
    except subprocess.CalledProcessError as e:
        print(f"Failed to reboot VM: {e}")


# =======================
# MAIN LOOP
# =======================
def check_internet_is_down():
    """Check if the internet is up by trying to resolve and connect to a host."""
    try:
        # Connect to a known internet host (Google DNS)
        # using port 53 (DNS) or 80 (HTTP)
        host = "8.8.8.8"
        port = 53
        socket.create_connection((host, port), timeout=5)  # Set a timeout for efficiency
        return False
    except OSError:
        # An exception is raised if the connection fails
        return True


if __name__ == "__main__":
    attempted_retries = 0
    max_attempts = 10
    while True:
        if check_ssh():
            print("VM is healthy.")
        else:
            print(f"VM might not be healthy. Number of attempts: {attempted_retries}")

            if check_internet_is_down():
                time.sleep(CHECK_INTERVAL)
                continue

            attempted_retries += 1
            if attempted_retries > max_attempts:
                print(f"VM is not healthy. Number of attempts: {attempted_retries}")
                reboot_vm(VM_NAME, ZONE)
                attempted_retries = 0
                is_vm_loading = False
                while not is_vm_loading:
                    time.sleep(CHECK_INTERVAL)
                    is_vm_loading = not check_ssh()
                send_telegram_message("VM loaded successfully after reboot...")

        time.sleep(CHECK_INTERVAL)
