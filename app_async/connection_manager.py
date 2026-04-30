import asyncio
import logging
import random
from ib_insync import IB
from utilities.utils import is_in_docker

logger = logging.getLogger(__name__)

class ConnectionManager:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(ConnectionManager, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        
        self.ib = IB()
        self.client_id = 1
        self.host = '127.0.0.1'
        self.port = 4001 if is_in_docker() else 7496
        self.reconnect_delay = 1
        self.is_connecting = False
        
        # Hook events
        self.ib.disconnectedEvent += self.on_disconnected
        self.ib.errorEvent += self.on_error
        
        self._initialized = True

    async def connect(self, client_id=1):
        """Handle the initial connection and keep-alive loop."""
        self.client_id = client_id
        
        while True:
            await self._ensure_connected()
            # Sleep to allow the event loop to run and periodically check status
            await asyncio.sleep(60)

    async def _ensure_connected(self):
        if self.ib.isConnected() or self.is_connecting:
            return

        self.is_connecting = True
        try:
            logger.info(f"Connecting to IB on {self.host}:{self.port} (clientId={self.client_id})...")
            await self.ib.connectAsync(self.host, self.port, clientId=self.client_id)
            logger.info("Successfully connected to IB.")
            self.reconnect_delay = 1 # Reset delay on success
        except Exception as e:
            logger.error(f"Connection failed: {e}. Retrying soon...")
            # We don't call reconnect() here directly to avoid nested loops; 
            # the while True in connect() will trigger it.
        finally:
            self.is_connecting = False

    def on_disconnected(self):
        logger.warning("Disconnected from IB.")
        # Trigger an immediate reconnection attempt in a new task
        asyncio.create_task(self.reconnect())

    def on_error(self, reqId, errorCode, errorString, contract):
        # Specific IB error codes that indicate connection issues
        # 1100: Connectivity between IB and Trader Workstation has been lost.
        # 2110: Connectivity between Trader Workstation and server is broken.
        if errorCode in [1100, 1101, 1102, 2110]: 
            logger.warning(f"IB Connectivity Error {errorCode}: {errorString}")

    async def reconnect(self):
        if self.ib.isConnected() or self.is_connecting:
            return
            
        delay = self.reconnect_delay
        # Exponential backoff with jitter
        self.reconnect_delay = min(delay * 2, 60)
        actual_delay = delay + random.uniform(0, 1)
        
        logger.info(f"Reconnecting in {actual_delay:.2f} seconds...")
        await asyncio.sleep(actual_delay)
        await self._ensure_connected()

    def disconnect(self):
        if self.ib.isConnected():
            self.ib.disconnect()
            logger.info("Disconnected from IB.")
