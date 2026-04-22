from datetime import datetime
import sys
import os
from statistics import mean
import math
import logging
import json
import requests

from utilities.utils import current_thread
from utilities.ib_utils import req_id_to_comment
from app.account_data import AccountData
from app.market_data_fetcher import MarketDataFetcher
from app.max_loss_calculator import MaxLossCalculator
from app.target_delta_calculator import TargetDeltaCalculator

TEMP_PATH = 'shared/state_temp.json'
JSON_PATH = 'shared/state.json'

API_URL = "https://option-trader.onrender.com/api"
UPDATE_STATE_URL = API_URL + "/update-state"
UPDATE_SUPERVISOR_STATE_URL = API_URL + "/update-supervisor-state"
TIMEOUT_SETTINGS = (3.05, 27)

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

def update_supervisor_state(supervisor_state):
    try:
        response  = requests.post(UPDATE_SUPERVISOR_STATE_URL, json=supervisor_state, timeout=TIMEOUT_SETTINGS)
        response.raise_for_status()
    except requests.exceptions.Timeout:
        logger.error("Supervisor update timed out. Server is ghosting us.")
    except requests.exceptions.RequestException as e:
        logger.error(f"Network error during supervisor update: {e}")

"""
def find_nan(obj, path="state"):
    if isinstance(obj, dict):
        for k, v in obj.items():
            find_nan(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            find_nan(v, f"{path}[{i}]")
    elif isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            logger.error(f"❌ Invalid float at {path}: {obj}")
"""

def post_current_state(state):
    #find_nan(state)
    try:
        response  = requests.post(UPDATE_STATE_URL, json=state, timeout=TIMEOUT_SETTINGS)
        response.raise_for_status()
    except requests.exceptions.Timeout:
        logger.error("Supervisor update timed out. Server is ghosting us.")
    except requests.exceptions.RequestException as e:
        logger.error(f"Network error during supervisor update: {e}")


def store_state(state):
    with open(TEMP_PATH, 'w') as file:
        json.dump(state, file, indent=4)
    os.replace(TEMP_PATH, JSON_PATH)

class StateUpdater:

    def __init__(self, trading_bot):
        self.account_data = AccountData()
        self.market_data_fetcher: MarketDataFetcher = current_thread.market_data_fetcher
        self.trading_bot = trading_bot

    def get_last_price(self, option):
        last_price = self.market_data_fetcher.get_last_price(option)
        if not last_price or math.isnan(last_price):
            return ''
        return str(last_price)

    def update_state(self, state):
        state['cash'] = round(self.account_data.get_cash_balance_value())
        excess_liquidity = self.account_data.get_excess_liquidity()
        state['excess_liquidity'] = '' if excess_liquidity == sys.float_info.max else round(excess_liquidity)
        state['cushion'] = round(self.account_data.get_cushion(), 2)
        target_delta_calculator = TargetDeltaCalculator()
        state['target_delta'] = round(target_delta_calculator.calculate_target_delta(), 4)
        state['target_delta_increase'] = round(target_delta_calculator.last_target_delta_increase, 4)
        max_loss_calculator = MaxLossCalculator()
        state['risk_fraction'] = round(mean(max_loss_calculator.risk_fraction.values()), 2)
        state['implied_volatility'] = round(self.market_data_fetcher.get_spx_implied_volatility(), 2)
        positions = self.trading_bot.get_short_options()
        open_trades = self.trading_bot.get_open_trades()
        state_positions = []
        contract_id_to_delta = {}
        for position in positions:
            option = position.contract
            date_obj = datetime.strptime(option.lastTradeDateOrContractMonth, "%Y%m%d")
            date = date_obj.strftime("%d/%m/%y")
            logger.debug(f"option of position: {id(option)}")
            delta = self.market_data_fetcher.get_delta(option)
            last_price = self.get_last_price(option)
            stop_loss = 0
            for trade in open_trades:
                if trade.contract.conId == option.conId and trade.order.orderType == 'STP':
                    stop_loss = trade.order.auxPrice
            state_position = {'right': option.right, 'strike': option.strike, 'quantity': position.position,
                              'date': date, 'delta': delta, 'last_price': last_price, 'stop_loss': stop_loss}
            state_positions.append(state_position)
            contract_id_to_delta[option.conId] = delta
        state['positions'] = sorted(state_positions, key=lambda pos: (pos['right'], pos['date'], pos['strike']))

        state_open_trades = []
        for open_trade in open_trades:
            option = open_trade.contract
            date_obj = datetime.strptime(option.lastTradeDateOrContractMonth, "%Y%m%d")
            date = date_obj.strftime("%d/%m/%y")
            logger.debug(f"option of position: {id(option)}")
            delta = self.market_data_fetcher.get_delta(option)
            if delta == '':
                delta = contract_id_to_delta.get(option.conId, '')
            limit = open_trade.order.lmtPrice if open_trade.order.orderType == 'LMT' else ''
            limit = open_trade.order.auxPrice if open_trade.order.orderType == 'STP' else limit
            open_trade = {'action': open_trade.order.action, 'right': option.right, 'strike': option.strike,
                          'quantity': open_trade.remaining(), 'date': date,
                          'delta': delta, 'order_type': open_trade.order.orderType, 'limit': limit}
            state_open_trades.append(open_trade)
        state['trades'] = sorted(state_open_trades, key=lambda trade: (
            trade['order_type'], trade['action'], trade['right'], trade['date'], trade['strike']))

        fills = self.trading_bot.get_fills()
        state_fills = []
        for fill in fills:
            contract = fill.contract
            execution = fill.execution
            if not contract.secType == 'OPT':
                continue

            comment = 'Liquidation' if execution.liquidation == 1 else ''
            if execution.orderId in req_id_to_comment:
                comment = req_id_to_comment[execution.orderId]
            state_fill = {'action': execution.side, 'right': contract.right, 'strike': contract.strike,
                          'quantity': execution.shares, 'price': execution.price, 'time': fill.time.timestamp(),
                          'comment': comment}
            state_fills.append(state_fill)
        state['fills'] = sorted(state_fills, key=lambda s_fill: s_fill['time'], reverse=True)

        store_state(state)
        post_current_state(state)
        return state
