import asyncio
import logging
from typing import Optional, List, Dict
from ib_insync import Position, Trade, Contract

from utilities.utils import *
from .trading_bot import TradingBot
from .market_data_fetcher import MarketDataFetcher
from .connection_manager import ConnectionManager

logger = logging.getLogger(__name__)

class PositionsManager:
    """
    Singleton manager for tracking and updating open positions and trades.
    Synchronizes the bot's internal view with the IB account state.
    """
    _instance: Optional['PositionsManager'] = None

    def __new__(cls) -> 'PositionsManager':
        if cls._instance is None:
            cls._instance = super(PositionsManager, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
            
        self.ib = ConnectionManager().ib
        self.trading_bot = TradingBot()
        self.market_data_fetcher = MarketDataFetcher()
        self.open_positions: List[Position] = []
        self.open_trades: List[Trade] = []
        
        logger.info("PositionsManager singleton initialized.")
        self._initialized = True

    async def update(self) -> None:
        """Refreshes the lists of open positions and open trades."""
        self.open_positions = await self.trading_bot.get_short_options()
        self.open_trades = await self.trading_bot.get_open_trades()
        logger.debug(f"Positions updated: {len(self.open_positions)} positions, {len(self.open_trades)} trades")

    def get_open_positions(self) -> List[Position]:
        """Returns the current list of open short option positions."""
        return self.open_positions

    def get_open_trades(self) -> List[Trade]:
        """Returns the current list of open trades."""
        return self.open_trades

    def get_position_for_contract(self, con_id: int) -> Optional[Position]:
        """Returns the position object for a specific conId, if it exists."""
        for p in self.open_positions:
            if p.contract.conId == con_id:
                return p
        return None

    def get_trades_for_contract(self, con_id: int) -> List[Trade]:
        """Returns all open trades matching a specific conId."""
        return [t for t in self.open_trades if t.contract.conId == con_id]
