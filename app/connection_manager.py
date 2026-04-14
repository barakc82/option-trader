import asyncio
import logging
import random

from tws_connection import TwsConnection

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


class ConnectionManager:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(ConnectionManager, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self, max_retries=5, base_delay=1, host='127.0.0.1', port=7497, client_id=1):
        if not self._initialized:
            self.max_retries = max_retries
            self.base_delay = base_delay
            self.host = host
            self.port = port
            self.client_id = client_id
            #self.ib = IB()  # Initial IB instance
            self.is_connected = False
            self.connection_lock = asyncio.Lock()
            self.tasks_paused = False

            self._initialized = True

    async def connect(self):
        """Attempt to connect with exponential backoff."""
        retries = 0
        while retries < self.max_retries:
            try:
                tws_connection = TwsConnection()
                tws_connection.connect(self.client_id)
                #wait self.ib.connectAsync(self.host, self.port, clientId=self.client_id)
                self.is_connected = True
                logger.info("Connected to IB API")
                #self.ib.disconnectedEvent += self.on_disconnect
                tws_connection.ib.disconnectedEvent += self.on_disconnect
                #self.ib.errorEvent += self.on_error
                tws_connection.ib.errorEvent += self.on_error
                return
            except Exception as e:
                retries += 1
                delay = self.base_delay * (2 ** retries) + random.uniform(0, 0.1)
                logger.error(f"Connection failed: {e}. Retrying in {delay:.2f}s ({retries}/{self.max_retries})")
                await asyncio.sleep(delay)
        logger.warning("Max retries reached. Creating new IB instance.")
        await self.create_new_instance()

    async def create_new_instance(self):
        """Create a new IB instance as a fallback."""
        try:
            # Disconnect and clean up old instance
            tws_connection = TwsConnection()
            tws_connection.disconnect()
            #self.ib.disconnect()
            tws_connection = TwsConnection()
            tws_connection.connect(self.client_id)
            #self.ib = IB()  # Create new instance
            # Try connecting with new instance
            # await self.ib.connectAsync(self.host, self.port, clientId=self.client_id + 1)  # Increment clientId
            # self.client_id += 1  # Update clientId to avoid conflicts
            self.is_connected = True
            logger.info("Successfully connected with new IB instance")
            # self.ib.disconnectedEvent += self.on_disconnect
            tws_connection.ib.disconnectedEvent += self.on_disconnect
            # self.ib.errorEvent += self.on_error
            tws_connection.ib.errorEvent += self.on_error
            # Reinitialize subscriptions (example, adjust based on your needs)
            await self.reinitialize_subscriptions()
        except Exception as e:
            logger.critical(f"Failed to connect with new IB instance: {e}")
            raise ConnectionError("Failed to connect with new IB instance")

    async def reinitialize_subscriptions(self):
        """Reinitialize market data or other subscriptions after creating new instance."""
        # Example: Re-subscribe to market data or re-request open orders
        # Replace with actual subscription logic from option_safeguard and option_trader
        logger.info("Reinitializing subscriptions for new IB instance")
        # Example: self.ib.reqMktData(...) or self.ib.reqOpenOrders()

    def on_disconnect(self):
        """Handle disconnection event."""
        logger.warning("Disconnected from IB API")
        self.is_connected = False
        self.tasks_paused = True
        asyncio.create_task(self.reconnect())

    def on_error(self, reqId, errorCode, errorString, contract):
        """Handle IB API errors."""
        logger.error(f"IB Error: reqId={reqId}, code={errorCode}, message={errorString}, contract={contract}")
        if errorCode in [502, 504, 1100]:  # Connection-related error codes
            self.is_connected = False
            self.tasks_paused = True
            asyncio.create_task(self.reconnect())

    async def reconnect(self):
        """Reconnect or create new instance if needed."""
        async with self.connection_lock:
            if self.is_connected:
                return
            logger.info("Attempting to reconnect...")
            try:
                await self.connect()
                self.tasks_paused = False
                logger.info("Reconnection successful, resuming tasks")
            except ConnectionError:
                logger.critical("Reconnection failed, stopping tasks")
                raise

    async def ensure_connected(self):
        """Ensure connection is active before proceeding."""
        if not self.is_connected:
            await self.connect()

    def disconnect(self):
        tws_connection = TwsConnection()
        tws_connection.disconnect()
