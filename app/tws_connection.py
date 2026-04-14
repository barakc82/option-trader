
from ib_insync import IB

import logging
from utils import current_thread, is_in_docker

CLIENT_ID_FILE_NAME = "../cache/last_client_id.txt"

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def get_and_increment_client_id():
    with open(CLIENT_ID_FILE_NAME, 'r') as f:
        content = f.read().strip()
        client_id = int(content) if content else 0

        new_client_id = client_id + 1
        # Write the updated number back to the file
        with open(CLIENT_ID_FILE_NAME, 'w') as f:
            f.write(str(new_client_id))
        return client_id


class TwsConnection:

    def __init__(self):
        self.ib = None

    def connect(self, client_id):
        port = 4001 if is_in_docker() else 7496
        try:
            self.ib = IB()
            current_thread.ib = self.ib
            connect_return_value = self.ib.connect('127.0.0.1', port, clientId=client_id)
            logger.info(f"Connected to TWS on port 7496 (Live Trading), connect return value: {str(connect_return_value)} {type(connect_return_value)}")
        except Exception as e:
            logger.error(f"Failed to connect on port {port}: {e}")
            logger.error("Could not establish a connection. Exiting.")
            raise ConnectionError()

    def disconnect(self):
        self.ib.disconnect()
