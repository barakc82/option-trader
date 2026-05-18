import asyncio
import logging
import random
from ib_insync import IB
from utilities.utils import is_in_docker

logger = logging.getLogger(__name__)

class ConnectionManager:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(ConnectionManager, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if not self._initialized:
            self.ib = IB()
            self.client_id = 1
            self.host = '127.0.0.1'
            self.port = 4001 if is_in_docker() else 7496
            self.reconnect_delay = 1
            self.is_connecting = False
            
            # Hook events
            self.ib.disconnectedEvent += self.on_disconnected
            self.ib.errorEvent += self.on_error
            self.ib.orderStatusEvent += self.on_order_status
            
            self._initialized = True

    def on_order_status(self, trade):
        if trade.orderStatus.status == 'Filled' and trade.contract.secType == 'OPT':
            from .positions_manager import PositionsManager
            PositionsManager().on_fill(trade)

    async def connect(self, client_id=1):
        """Handle the initial connection and keep-alive loop."""
        self.client_id = client_id
        
        while True:
            await self._ensure_connected()
            if self.ib.isConnected():
                await self._check_health()
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
            await self.request_account_updates()
        except Exception as e:
            logger.error(f"Connection failed: {e}. Retrying soon...")
            # We don't call reconnect() here directly to avoid nested loops; 
            # the while True in connect() will trigger it.
        finally:
            self.is_connecting = False

    async def request_account_updates(self):
        """Request account updates for the primary account."""
        try:
            await asyncio.sleep(2)  # let connection stabilize before re-subscribing

            # Wait briefly for accounts to be populated if necessary
            for _ in range(10):
                if self.ib.wrapper.accounts:
                    break
                await asyncio.sleep(0.1)

            if self.ib.isConnected() and self.ib.wrapper.accounts:
                account = self.ib.wrapper.accounts[0]
                logger.info(f"Requesting account updates for {account}")
                self.ib.reqAccountUpdates(True, account)
            else:
                logger.debug("Cannot request account updates: Not connected or no accounts available.")
        except Exception as e:
            logger.error(f"Error requesting account updates: {e}")

    async def _check_health(self):
        """Check if the connection is actually responsive."""
        try:
            # reqCurrentTimeAsync is a lightweight way to ping the server
            await asyncio.wait_for(self.ib.reqCurrentTimeAsync(), timeout=10)
        except Exception as e:
            logger.warning(f"Connection health check failed: {e}. Forcing reconnection.")
            self.ib.disconnect()
            await self.reconnect()

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
            if errorCode in [1101, 1102]:
                logger.info(f"Connectivity restored (error {errorCode}). Requesting account updates...")
                asyncio.create_task(self.request_account_updates())

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
