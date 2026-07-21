import asyncio
import csv
import json
import os
from datetime import datetime

from utilities.utils import is_trade_cancelled, write_heartbeat, get_option_name, is_final_hours, CACHED_JSON_PATH
from utilities.ib_utils import *

from .max_loss_calculator import MaxLossCalculator
from .opportunity_explorer import OpportunityExplorer
from .trading_bot import TradingBot


logger = logging.getLogger(__name__)

MINIMAL_SELL_PRICE_TO_CLOSE_POSITION = MINIMAL_SELL_PRICE + 0.05


class PositionsManager:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(PositionsManager, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if not self._initialized:
            # Accessing the TradingBot singleton internally
            self.trading_bot = TradingBot()
            self.max_loss_calculator = MaxLossCalculator()
            self.done_contract_ids = set()
            self.position_initial_state_map = {}
            self._load_cached_position_initial_states()
            logger.info("PositionsManager singleton initialized.")
            self._initialized = True


    def _load_cached_position_initial_states(self):
        try:
            with open(CACHED_JSON_PATH, 'r') as f:
                state = json.load(f)
            for pos in state.get('position_initial_states', []):
                date = pos.get('date')
                strike = pos.get('strike')
                right = pos.get('right')
                con_id = pos.get('contract_id')
                if not date or strike is None or not right or not con_id:
                    continue

                expiry = datetime.strptime(date, "%d/%m/%y").strftime("%Y%m%d")
                key = int(con_id)

                is_executed = pos.get('is_executed')
                target_delta = pos.get('target_delta')
                estimated_sell_price = pos.get('estimated_sell_price')
                stop_loss_per_option = pos.get('stop_loss_per_option')
                bid_delta = pos.get('bid_delta')
                ask_delta = pos.get('ask_delta')
                last_delta = pos.get('last_delta')
                model_delta = pos.get('model_delta')
                minutes_to_expiration = pos.get('minutes_to_expiration')
                distance_to_stop_pct = pos.get('distance_to_stop_pct')
                implied_volatility = pos.get('implied_volatility')
                self.position_initial_state_map.setdefault(key, []).append(PositionInitialState(
                    is_executed=int(is_executed) if is_executed not in (None, '') else 1,
                    strike=float(strike), right=right, expiry=expiry,
                    target_delta=float(target_delta) if target_delta not in (None, '') else 0.0,
                    estimated_sell_price=float(estimated_sell_price) if estimated_sell_price not in (None, '') else 0.0,
                    stop_loss_per_option=float(stop_loss_per_option) if stop_loss_per_option not in (None, '') else 0.0,
                    bid_delta=float(bid_delta) if bid_delta not in (None, '') else None,
                    ask_delta=float(ask_delta) if ask_delta not in (None, '') else None,
                    last_delta=float(last_delta) if last_delta not in (None, '') else None,
                    model_delta=float(model_delta) if model_delta not in (None, '') else None,
                    minutes_to_expiration=int(minutes_to_expiration) if minutes_to_expiration not in (None, '') else None,
                    distance_to_stop_pct=float(distance_to_stop_pct) if distance_to_stop_pct not in (None, '') else None,
                    implied_volatility=float(implied_volatility) if implied_volatility not in (None, '') else None,
                ))
            logger.info(f"Loaded {len(self.position_initial_state_map)} target delta entries from cache")
        except Exception as e:
            logger.warning(f"Could not load cached position initial states: {e}")

    def find_low_limit_buy_trade(self, option, open_buy_trades) -> Trade | None:
        for open_buy_trade in open_buy_trades:
            if (option.conId == open_buy_trade.contract.conId and open_buy_trade.order.action.upper() == 'BUY' and
                open_buy_trade.order.orderType == 'LMT' and open_buy_trade.order.lmtPrice == 0.05):
                return open_buy_trade
        return None

    async def manage_current_positions(self):
        logger.info("Checking current positions")
        positions = self.trading_bot.get_short_options()
        open_trades = self.trading_bot.get_open_trades()

        open_buy_trades = [trade for trade in open_trades if trade.order.action.upper() == 'BUY' and
                           not is_trade_cancelled(trade) and trade.order.orderType == 'LMT']

        current_con_ids = {p.contract.conId for p in positions}
        self.done_contract_ids &= current_con_ids

        now_nyc = datetime.now(new_york_timezone)
        for key, entries in list(self.position_initial_state_map.items()):
            expiry_date = datetime.strptime(entries[0].expiry, '%Y%m%d').date()
            expiry_datetime = new_york_timezone.localize(datetime.combine(expiry_date, REGULAR_HOURS_END_TIME))
            if expiry_datetime < now_nyc:
                for entry in entries:
                    self._log_close_event(entry)
                del self.position_initial_state_map[key]

        for position in positions:
            write_heartbeat()
            option = position.contract

            opportunity_explorer = OpportunityExplorer()
            current_price_level = opportunity_explorer.last_call_option_price if option.right == 'C' else opportunity_explorer.last_put_option_price

            if current_price_level < MINIMAL_SELL_PRICE_TO_CLOSE_POSITION:
                continue

            limit_buy_trade = self.find_low_limit_buy_trade(option, open_buy_trades)
            if limit_buy_trade:
                continue

            if not hasattr(option, "ticker") or option.ticker is None:
                logger.info(f"Option {get_option_name(option)} has no ticker")
                continue

            bid = option.ticker.bid
            ask = option.ticker.ask
            if not self.can_buy_options() or math.isnan(bid) or bid > 0.05 or math.isnan(ask) or ask > 0.2 or ask < 0:
                continue

            logger.info(
                f"Submitting a buy trade for position of {get_option_name(position.contract)}, quantity: {position.position}, bid is {bid}, current price level is {current_price_level}")
            close_position_trade = await self.trading_bot.close_short_option(option, abs(position.position), limit=0.05)
            req_id_to_comment[close_position_trade.order.orderId] = "Position buyback"


    def can_buy_options(self):
        return not is_final_hours()

    def on_fill(self, trade):
        position_initial_state = self.trading_bot.req_id_to_order_metadata.get(trade.order.orderId)
        logger.info(f"Trade filled: {get_option_name(trade.contract)} {trade.order.action}, position initial state: {position_initial_state}")
        if trade.order.action.upper() == 'SELL' and position_initial_state is not None:
            self.update_position_entry(position_initial_state, trade)
        if trade.order.action.upper() == 'BUY':
            self.done_contract_ids.add(trade.contract.conId)
            c = trade.contract
            for entry in self.position_initial_state_map.pop(c.conId, []):
                entry.stop_loss_activated = trade.order.lmtPrice > 0.1
                asyncio.get_running_loop().run_in_executor(None, self._log_close_event, entry)
        if not position_initial_state:
            logger.error(f"Could not find target delta entry for order ID {trade.order.orderId}, here is what we have:")
            for order_id, entry in self.trading_bot.req_id_to_order_metadata.items():
                logger.info(f"Order ID {order_id} ==> {entry}")
        if trade.order.orderId in req_id_to_comment and "Margin" in req_id_to_comment[trade.order.orderId]:
            opportunity_explorer = OpportunityExplorer()
            opportunity_explorer.notify_margin_lock_resolution_attempted()

    def _log_close_event(self, position_initial_state: PositionInitialState):
        csv_path = 'cache/close_events.csv'
        write_header = not os.path.exists(csv_path)
        with open(csv_path, 'a', newline='') as f:
            writer = csv.writer(f)
            if write_header:
                writer.writerow([
                    'datetime', 'is_executed', 'right', 'strike', 'expiration',
                    'estimated_sell_price', 'stop_loss_per_option',
                    'target_delta', 'bid_delta', 'ask_delta', 'last_delta', 'model_delta',
                    'minutes_to_expiration', 'implied_volatility', 'distance_to_stop_pct',
                    'stop_loss_activated',
                ])
            writer.writerow([
                datetime.now().isoformat(), position_initial_state.is_executed,
                position_initial_state.right, position_initial_state.strike,
                position_initial_state.expiry,
                position_initial_state.estimated_sell_price, position_initial_state.stop_loss_per_option,
                position_initial_state.target_delta, position_initial_state.bid_delta,
                position_initial_state.ask_delta, position_initial_state.last_delta,
                position_initial_state.model_delta,
                position_initial_state.minutes_to_expiration,
                position_initial_state.implied_volatility, position_initial_state.distance_to_stop_pct,
                position_initial_state.stop_loss_activated,
            ])

    def update_position_entry(self, position_initial_state: PositionInitialState, trade):
        c = trade.contract
        key = c.conId
        self.position_initial_state_map.setdefault(key, []).append(position_initial_state)
