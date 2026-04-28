import json
import logging
import math
import os
import sys
import aiohttp
from datetime import datetime
from statistics import mean

from utilities.ib_utils import req_id_to_comment

from .account_data import AccountData
from .market_data_fetcher import MarketDataFetcher
from .max_loss_calculator import MaxLossCalculator
from .target_delta_calculator import TargetDeltaCalculator
from .trading_bot import TradingBot
from .opportunity_explorer import OpportunityExplorer


TEMP_PATH = 'shared/state_temp.json'
JSON_PATH = 'shared/state.json'
API_URL = "https://option-trader.onrender.com/api"
UPDATE_STATE_URL = API_URL + "/update-state"
UPDATE_SUPERVISOR_STATE_URL = API_URL + "/update-supervisor-state"

logger = logging.getLogger(__name__)

class StateUpdater:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(StateUpdater, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if not self._initialized:
            self.account_data = AccountData()
            self.market_data_fetcher = MarketDataFetcher()
            self.trading_bot = TradingBot()
            self.target_delta_calculator = TargetDeltaCalculator()
            self.max_loss_calculator = MaxLossCalculator()
            self._initialized = True

    async def _post_data(self, url, data):
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=data, timeout=15) as response:
                response.raise_for_status()

    def store_state_locally(self, state):
        os.makedirs(os.path.dirname(TEMP_PATH), exist_ok=True)
        with open(TEMP_PATH, 'w') as file:
            json.dump(state, file, indent=4)
        os.replace(TEMP_PATH, JSON_PATH)

    async def update_state(self, base_state):
        """Orchestrates the asynchronous collection and reporting of bot state."""
        state = base_state.copy()
        
        # 1. Gather account metrics
        state['cash'] = round(self.account_data.get_cash_balance_value())
        excess_liq = await self.account_data.get_excess_liquidity()
        state['excess_liquidity'] = '' if excess_liq == sys.float_info.max else round(excess_liq)
        state['cushion'] = round(await self.account_data.get_cushion(), 2)

        # 2. Gather logic metrics
        state['target_delta'] = round(await self.target_delta_calculator.calculate_target_delta(), 4)
        state['target_delta_increase'] = round(self.target_delta_calculator.last_target_delta_increase, 4)
        state['risk_fraction'] = round(mean(self.max_loss_calculator.risk_fraction.values()), 2)
        state['implied_volatility'] = round(await self.market_data_fetcher.get_spx_implied_volatility(), 2)

        # 3. Gather positions and trades
        positions = await self.trading_bot.get_short_options()
        open_trades = await self.trading_bot.get_open_trades()

        state_positions = []
        contract_id_to_delta = {}
        for pos in positions:
            opt = pos.contract
            delta = self.market_data_fetcher.get_delta(opt)
            last_price = self.market_data_fetcher.get_last_price(opt)

            # Find stop loss if it exists
            stop_loss = 0
            for t in open_trades:
                if t.contract.conId == opt.conId and t.order.orderType == 'STP':
                    stop_loss = t.order.auxPrice

            state_positions.append({
                'right': opt.right, 'strike': opt.strike, 'quantity': pos.position,
                'date': datetime.strptime(opt.lastTradeDateOrContractMonth, "%Y%m%d").strftime("%d/%m/%y"),
                'delta': delta, 'last_price': str(last_price) if not math.isnan(last_price) else '',
                'stop_loss': stop_loss
            })
            contract_id_to_delta[opt.conId] = delta

        state['positions'] = sorted(state_positions, key=lambda x: (x['right'], x['date'], x['strike']))

        # 4. Process open trades
        state_trades = []
        for t in open_trades:
            opt = t.contract
            delta = self.market_data_fetcher.get_delta(opt) or contract_id_to_delta.get(opt.conId, '')
            limit = t.order.lmtPrice if t.order.orderType == 'LMT' else (t.order.auxPrice if t.order.orderType == 'STP' else '')

            state_trades.append({
                'action': t.order.action, 'right': opt.right, 'strike': opt.strike,
                'quantity': t.remaining(), 'date': datetime.strptime(opt.lastTradeDateOrContractMonth, "%Y%m%d").strftime("%d/%m/%y"),
                'delta': delta, 'order_type': t.order.orderType, 'limit': limit
            })
        state['trades'] = sorted(state_trades, key=lambda x: (x['order_type'], x['action'], x['right'], x['date'], x['strike']))

        # 5. Process fills
        fills = self.trading_bot.ib.fills()
        state_fills = []
        for f in fills:
            if f.contract.secType != 'OPT': continue
            comment = 'Liquidation' if f.execution.liquidation == 1 else req_id_to_comment.get(f.execution.orderId, '')
            state_fills.append({
                'action': f.execution.side, 'right': f.contract.right, 'strike': f.contract.strike,
                'quantity': f.execution.shares, 'price': f.execution.price,
                'time': f.time.timestamp(), 'comment': comment
            })
        state['fills'] = sorted(state_fills, key=lambda x: x['time'], reverse=True)

        opportunity_explorer = OpportunityExplorer()
        state['last_put_option_price'] = round(opportunity_explorer.last_put_option_price, 2)
        state['last_call_option_price'] = round(opportunity_explorer.last_call_option_price, 2)

        # 6. Finalize and Post
        self.store_state_locally(state)
        logger.info(f"Sending state to {UPDATE_STATE_URL}")
        await self._post_data(UPDATE_STATE_URL, state)
            
        return state

async def post_current_state(state):
    """Standalone async helper for simple state reporting."""
    updater = StateUpdater()
    await updater._post_data(UPDATE_STATE_URL, state)

async def update_supervisor_state_async(supervisor_state):
    """Standalone async function for the supervisor."""
    updater = StateUpdater()
    await updater._post_data(UPDATE_SUPERVISOR_STATE_URL, supervisor_state)
