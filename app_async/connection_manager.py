import asyncio
import logging
import random
from typing import Optional
from ib_insync import IB
from utilities.utils import is_in_docker

logger = logging.getLogger(__name__)

class ConnectionManager:
    """
    Singleton manager for the Interactive Brokers connection.
    Handles automatic reconnection with exponential backoff.
    """
    _instance: Optional['ConnectionManager'] = None

    def __new__(cls) -> 'ConnectionManager':
        if cls._instance is None:
            cls._instance = super(ConnectionManager, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        
        self.ib = IB()
        self.client_id = 1
        self.host = '127.0.0.1'
        self.port = 4001 if is_in_docker() else 7496
        self.reconnect_delay = 1.0
        self.is_connecting = False
        
        # Hook events
        self.ib.disconnectedEvent += self.on_disconnected
        self.ib.errorEvent += self.on_error
        
        self._initialized = True

    async def connect(self, client_id: int = 1) -> None:
        """
        Main entry point to start the connection and keep-alive loop.
        
        Args:
            client_id: The IB API clientId to use.
        """
        self.client_id = client_id
        
        while True:
            await self._ensure_connected()
            # Periodically check status; event-driven reconnection handles drops
            await asyncio.sleep(60)

    async def _ensure_connected(self) -> None:
        """Attempts to connect if not currently connected or already connecting."""
        if self.ib.isConnected() or self.is_connecting:
            return

        self.is_connecting = True
        try:
            logger.info(f"Connecting to IB on {self.host}:{self.port} (clientId={self.client_id})...")
            await self.ib.connectAsync(self.host, self.port, clientId=self.client_id)
            logger.info("Successfully connected to IB.")
            self.reconnect_delay = 1.0 # Reset delay on success
        except Exception as e:
            logger.error(f"Connection failed: {e}. Retrying soon...")
        finally:
            self.is_connecting = False

    def on_disconnected(self) -> None:
        """Event handler for IB disconnection."""
        logger.warning("Disconnected from IB.")
        asyncio.create_task(self.reconnect())

    def on_error(self, reqId: int, errorCode: int, errorString: str, contract: Optional[object]) -> None:
        """
        Event handler for IB errors.
        Filters for specific connectivity-related error codes.
        """
        if errorCode in [1100, 1101, 1102, 2110]: 
            logger.warning(f"IB Connectivity Error {errorCode}: {errorString}")

    async def reconnect(self) -> None:
        """Performs a reconnection attempt with exponential backoff and jitter."""
        if self.ib.isConnected() or self.is_connecting:
            return
            
        delay = self.reconnect_delay
        # Exponential backoff with ceiling and random jitter
        self.reconnect_delay = min(delay * 2, 60.0)
        actual_delay = delay + random.uniform(0, 1)
        
        logger.info(f"Reconnecting in {actual_delay:.2f} seconds...")
        await asyncio.sleep(actual_delay)
        await self._ensure_connected()

    def disconnect(self) -> None:
        """Gracefully disconnects from the IB server."""
        if self.ib.isConnected():
            self.ib.disconnect()
            logger.info("Disconnected from IB.")
