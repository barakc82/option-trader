import time
import json
import logging
import math
import os
import sys
import aiohttp
import asyncio
import pytz
from datetime import datetime
from statistics import mean

from utilities.ib_utils import req_id_to_comment
from utilities.utils import is_market_open, is_regular_hours, SAFEGUARD_MAX_CADENCE, get_option_name, JSON_PATH, SUPERVISOR_JSON_PATH

from .account_data import AccountData
from .market_data_fetcher import MarketDataFetcher
from .max_loss_calculator import MaxLossCalculator
from .target_delta_calculator import TargetDeltaCalculator
from .trading_bot import TradingBot
from .opportunity_explorer import OpportunityExplorer
from .subscription_manager import SubscriptionManager


TEMP_PATH = 'shared/state_temp.json'
SUPERVISOR_TEMP_PATH = 'shared/supervisor_state_temp.json'
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


    async def run(self):
        """Background task to periodically update the system state."""
        logger.info("StateUpdater: Starting background state update loop...")
        while True:
            try:
                from .option_safeguard import OptionSafeguard
                safeguard = OptionSafeguard()
                if time.time() - safeguard.last_run_end_time > SAFEGUARD_MAX_CADENCE:
                    await asyncio.sleep(0)
                    continue

                if self.trading_bot.ib.isConnected():
                    is_open = is_market_open()
                    state = {
                        'market_state': 'Open' if is_open else 'Closed',
                        'status': 'Active' if is_open else 'Closed'
                    }
                    await self.update_state(state)
            except Exception:
                logger.exception("Error in StateUpdater loop:")

            await asyncio.sleep(5)

    async def _post_data(self, url, data):
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=data, timeout=15) as response:
                response.raise_for_status()

    def store_state_locally(self, state):
        os.makedirs(os.path.dirname(TEMP_PATH), exist_ok=True)
        with open(TEMP_PATH, 'w') as file:
            json.dump(state, file, indent=4)
        os.replace(TEMP_PATH, JSON_PATH)

    def store_supervisor_state_locally(self, supervisor_state):
        os.makedirs(os.path.dirname(SUPERVISOR_TEMP_PATH), exist_ok=True)
        with open(SUPERVISOR_TEMP_PATH, 'w') as file:
            json.dump(supervisor_state, file, indent=4)
        os.replace(SUPERVISOR_TEMP_PATH, SUPERVISOR_JSON_PATH)

    async def update_state(self, base_state):
        """Orchestrates the asynchronous collection and reporting of bot state."""
        state = base_state.copy()
        
        # 1. Gather account metrics
        state['cash'] = round(self.account_data.get_cash_balance_value())
        excess_liq = self.account_data.get_cached_excess_liquidity()
        state['excess_liquidity'] = '' if excess_liq == sys.float_info.max else round(excess_liq)
        state['cushion'] = round(self.account_data.get_cached_cushion(), 2)
        
        # Set last_updated in Israel time
        israel_tz = pytz.timezone('Asia/Jerusalem')
        state['last_updated'] = datetime.now(israel_tz).strftime("%d/%m/%y %H:%M")

        # 2. Gather logic metrics

        call_target_delta = self.target_delta_calculator.get_cached_target_delta('C')
        put_target_delta = self.target_delta_calculator.get_cached_target_delta('P')
        
        state['call_target_delta'] = round(call_target_delta, 4)
        state['put_target_delta'] = round(put_target_delta, 4)
        state['call_target_delta_increase'] = round(self.target_delta_calculator.last_target_delta_increase['C'], 4)
        state['put_target_delta_increase'] = round(self.target_delta_calculator.last_target_delta_increase['P'], 4)
        
        state['call_risk_fraction'] = round(self.max_loss_calculator.risk_fraction['C'], 2)
        state['put_risk_fraction'] = round(self.max_loss_calculator.risk_fraction['P'], 2)
        state['call_implied_volatility'] = round(self.market_data_fetcher.get_cached_spx_implied_volatility('C'), 2)
        state['put_implied_volatility'] = round(self.market_data_fetcher.get_cached_spx_implied_volatility('P'), 2)

        # 3. Gather positions and trades
        positions = self.trading_bot.get_short_options()
        open_trades = self.trading_bot.get_open_trades()

        if positions:
            await self.trading_bot.fetch_price_increments(positions[0].contract)

        state_positions = []
        contract_id_to_delta = {}
        subscription_manager = SubscriptionManager()
        is_reg_hours = is_regular_hours()
        indices_difference = self.market_data_fetcher.calculate_spx_es_difference()

        for position in positions:
            option = position.contract
            delta = self.market_data_fetcher.get_delta(option)
            market_price = round(self.market_data_fetcher.get_market_price(option), 2)

            stop_loss_per_option = self.max_loss_calculator.calculate_max_loss(option.right)
            raw_stop_loss = position.avgCost / 100 + stop_loss_per_option
            stop_loss = self.trading_bot.adjust_limit_to_market_rules(option, raw_stop_loss)
            
            distance_to_stop = self.market_data_fetcher.calculate_index_points_margin(option, stop_loss)
            distance_to_stop_roundness = 1 if distance_to_stop < 100 else 0
            distance_to_stop = round(distance_to_stop, distance_to_stop_roundness)

            pos_data = {
                'right': option.right, 'strike': option.strike, 'quantity': position.position,
                'date': datetime.strptime(option.lastTradeDateOrContractMonth, "%Y%m%d").strftime("%d/%m/%y"),
                'delta': delta, 'market_price': str(market_price) if not math.isnan(market_price) else '',
                'stop_loss': stop_loss,
                'distance_to_stop': distance_to_stop if not math.isnan(distance_to_stop) else ''
            }

            es_options = subscription_manager.spx_to_es_map.get(option.conId)
            if es_options and len(es_options) == 2:
                lower_es, upper_es = es_options
                lower_ticker = self.market_data_fetcher.get_ticker(lower_es)
                upper_ticker = self.market_data_fetcher.get_ticker(upper_es)
                if lower_ticker and upper_ticker:
                    lower_price = lower_ticker.marketPrice()
                    upper_price = upper_ticker.marketPrice()
                    if not math.isnan(lower_price) and not math.isnan(upper_price):
                        equivalent_es_strike = option.strike - indices_difference
                        t = (equivalent_es_strike - lower_es.strike) / (upper_es.strike - lower_es.strike)
                        adjusted_es_ask = lower_price * (1 - t) + upper_price * t
                        pos_data['es_price'] = str(round(adjusted_es_ask, 2))

            state_positions.append(pos_data)
            contract_id_to_delta[option.conId] = delta

        state['positions'] = sorted(state_positions, key=lambda x: (x['right'], x['date'], x['strike']))

        # 4. Process open trades
        state_trades = []
        for t in open_trades:
            option = t.contract
            delta = self.market_data_fetcher.get_delta(option) or contract_id_to_delta.get(option.conId, '')
            limit = t.order.lmtPrice if t.order.orderType == 'LMT' else (t.order.auxPrice if t.order.orderType == 'STP LMT' else '')

            state_trades.append({
                'action': t.order.action, 'right': option.right, 'strike': option.strike,
                'quantity': t.remaining(), 'date': datetime.strptime(option.lastTradeDateOrContractMonth, "%Y%m%d").strftime("%d/%m/%y"),
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
        state['call_margin_reduction'] = opportunity_explorer.call_margin_reduction
        state['put_margin_reduction'] = opportunity_explorer.put_margin_reduction
        
        premium = self.market_data_fetcher.calculate_spx_es_difference()
        state['spx_premium'] = round(premium, 2)

        is_reg_hours = is_regular_hours()
        if is_reg_hours:
            index_price = self.market_data_fetcher.get_spx_price()
            state['index_label'] = 'S&P 500'
        else:
            index_price = self.market_data_fetcher.get_es_price() + premium
            state['index_label'] = 'Adjusted ES'

        state['spx_price'] = round(index_price, 2) if not math.isnan(index_price) else None


        # 6. Finalize
        self.store_state_locally(state)
        try:
            await self._post_data(UPDATE_STATE_URL, state)
        except Exception as e:
            logger.error(f"Failed to post state to Render: {e}")
            
        return state

async def post_current_state(state):
    """Standalone async helper for simple state reporting."""
    updater = StateUpdater()
    updater.store_state_locally(state)
    try:
        await updater._post_data(UPDATE_STATE_URL, state)
    except Exception as e:
        logger.error(f"Failed to post current state to Render: {e}")

async def update_supervisor_state_async(supervisor_state):
    """Standalone async function for the supervisor."""
    supervisor_state['time'] = int(time.time())
    updater = StateUpdater()
    updater.store_supervisor_state_locally(supervisor_state)
    try:
        await updater._post_data(UPDATE_SUPERVISOR_STATE_URL, supervisor_state)
    except Exception as e:
        logger.error(f"Failed to post supervisor state to Render: {e}")
