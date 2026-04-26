import asyncio
import logging
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
            self._initialized = True

    async def connect(self, client_id=1):
        """Handle the initial connection and reconnection logic."""
        port = 4001 if is_in_docker() else 7496
        host = '127.0.0.1'
        
        while True:
            try:
                if not self.ib.isConnected():
                    logger.info(f"Connecting to IB on {host}:{port} (clientId={client_id})...")
                    await self.ib.connectAsync(host, port, clientId=client_id)
                    logger.info("Successfully connected to IB.")
                
                await asyncio.sleep(30)
                
            except Exception:
                logger.exception("ConnectionManager: Error during connection. Retrying in 10s...")
                await asyncio.sleep(10)

    def disconnect(self):
        if self.ib.isConnected():
            self.ib.disconnect()
            logger.info("Disconnected from IB.")
