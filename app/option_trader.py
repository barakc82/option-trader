import math
import asyncio
from datetime import datetime, timedelta

from utilities.utils import *
from utilities.ib_utils import *

from .max_loss_calculator import MaxLossCalculator
from .net_worth_calculator import NetWorthCalculator
from .opportunity_explorer import OpportunityExplorer
from .pending_trades_manager import PendingTradesManager
from .positions_manager import PositionsManager
from .trading_bot import TradingBot
from .connection_manager import ConnectionManager
from .market_data_fetcher import MarketDataFetcher
from .target_delta_calculator import TargetDeltaCalculator
from .state_updater import StateUpdater, post_current_state


logger = logging.getLogger(__name__)


class OptionTrader:
    def __init__(self):
        # Accessing singleton instances
        self.connection_manager = ConnectionManager()
        self.ib = self.connection_manager.ib
        self.trading_bot = TradingBot()
        self.max_loss_calculator = MaxLossCalculator()
        self.net_worth_calculator = NetWorthCalculator()
        self.opportunity_explorer = OpportunityExplorer()
        self.positions_manager = PositionsManager()
        self.market_data_fetcher = MarketDataFetcher()
        self.target_delta_calculator = TargetDeltaCalculator()
        self.pending_trades_manager = PendingTradesManager()
        
        self.connection_failure_start_time = None
        self.config = {}

    async def run(self):
        logger.info("OptionTrader: Starting trading loop...")
        await post_current_state({'status': 'Loading...'})

        while True:
            try:
                from .option_safeguard import OptionSafeguard
                safeguard = OptionSafeguard()
                if time.time() - safeguard.last_run_end_time > SAFEGUARD_MAX_CADENCE:
                    await asyncio.sleep(0)
                    continue

                if not self.ib.isConnected():
                    logger.warning("OptionTrader: Task is waiting for IB connection...")
                    await asyncio.sleep(2)
                    continue

                write_heartbeat()

                if is_market_open():
                    logger.info(f"OptionTrader: Checking market status...")
                    await self.trade()
                else:
                    if int(time.time()) % 100 == 0:
                        logger.info(f"Market is closed")
                    if is_night_break():
                        await self.verify_no_open_trades()

                if self.connection_failure_start_time is not None:
                    logger.info("OptionTrader: Connection error resolved.")
                    self.connection_failure_start_time = None

                await self.yield_execution()

            except Exception:
                if self.connection_failure_start_time is None:
                    self.connection_failure_start_time = time.time()
                
                elapsed = time.time() - self.connection_failure_start_time
                logger.exception(f"OptionTrader: Loop error ({elapsed:.0f}s):")
                
                if elapsed > 300:
                    logger.error("OptionTrader: Persistent failure detected. Continuing to retry indefinitely...")
                
                # Attempt to report error status
                try:
                    await post_current_state({'status': 'Error'})
                except:
                    pass
                
                # Progressive backoff for sleep
                sleep_time = min(10 + (elapsed // 60) * 10, 60)
                await asyncio.sleep(sleep_time)

    async def yield_execution(self):
        write_heartbeat()
        await asyncio.sleep(0.5)

    async def trade(self):
        await self.positions_manager.manage_current_positions()
        await self.pending_trades_manager.guard_pending_trades()
        await self.opportunity_explorer.explore_opportunities()

    async def verify_no_open_trades(self):
        open_trades = self.trading_bot.get_open_trades()
        for open_trade in open_trades:
            if open_trade.orderStatus.status == 'PendingCancel':
                continue
            self.trading_bot.cancel_trade(open_trade)
