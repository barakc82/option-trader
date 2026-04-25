import asyncio
import logging
from ib_insync import IB
from utilities.utils import is_in_docker

logger = logging.getLogger(__name__)

class ConnectionManager:
    def __init__(self):
        self.ib = IB()

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
                
                # Check connection every 30 seconds
                await asyncio.sleep(30)
                
            except Exception:
                logger.exception("ConnectionManager: Error during connection/maintenance. Retrying in 10s...")
                await asyncio.sleep(10)

    def disconnect(self):
        if self.ib.isConnected():
            self.ib.disconnect()
            logger.info("Disconnected from IB.")
