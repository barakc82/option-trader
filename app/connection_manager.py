import asyncio
import logging
import random
from ib_insync import IB
from utilities.utils import is_in_docker, MY_ACCOUNT

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
            self._managed_tasks: list[asyncio.Task] = []

            # Hook events
            self.ib.disconnectedEvent += self.on_disconnected
            self.ib.errorEvent += self.on_error
            self.ib.orderStatusEvent += self.on_order_status

            self._initialized = True

    def register_task(self, task: asyncio.Task):
        self._managed_tasks = [t for t in self._managed_tasks if not t.done()]
        self._managed_tasks.append(task)

    async def _restart_managed_tasks(self):
        logger.info(f"Cancelling {len(self._managed_tasks)} managed tasks for restart after reconnect...")
        for task in self._managed_tasks:
            if not task.done():
                task.cancel()
        self._managed_tasks.clear()
        await asyncio.sleep(0)  # Let cancellations propagate

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

            await self.initialize_data()
            await self._restart_managed_tasks()

        except Exception as e:
            logger.error(f"Connection failed: {e}. Retrying soon...")
            # We don't call reconnect() here directly to avoid nested loops; 
            # the while True in connect() will trigger it.
        finally:
            self.is_connecting = False

    async def safe_account_updates(self, timeout=10):
        try:
            await asyncio.wait_for(self.ib.reqAccountUpdatesAsync(MY_ACCOUNT), timeout=timeout)
        except asyncio.TimeoutError:
            logging.warning(
                "accountDownloadEnd never arrived after reconnect; "
                "continuing — values still stream via accountValueEvent"
            )

    async def initialize_data(self):
        """Initializes positions, orders, and account data after connection."""
        try:
            if not self.ib.isConnected():
                logger.debug("Cannot initialize data: Not connected.")
                return

            logger.info("Initializing session data...")
            await self.ib.reqPositionsAsync()
            await self.ib.reqAllOpenOrdersAsync()

            # Stabilization for account-based data
            await asyncio.sleep(2)
            
            logger.info(f"Initializing account data for {MY_ACCOUNT}...")
            self.ib.client.reqAccountUpdates(False, MY_ACCOUNT)  # cancel first
            self.ib.client.reqAccountUpdates(True, MY_ACCOUNT)

            logger.info(f"Initializing summery data...")
            await self.ib.reqAccountSummaryAsync()
            logger.info("Initializing data done")

        except Exception as e:
            logger.error(f"Error during data initialization: {e}")
    
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
                logger.info(f"Connectivity restored (error {errorCode}). Re-initializing data...")
                asyncio.create_task(self.initialize_data())

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

    def is_connected(self):
        return not self.is_connecting
