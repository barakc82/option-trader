import math
import os
import json
import time
import logging
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Tuple, Any

from utilities.utils import *
from utilities.ib_utils import get_delta
from .trading_bot import TradingBot
from .account_data import AccountData
from .market_data_fetcher import MarketDataFetcher

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

DEFAULT_MAX_LOSS = 1.0
WINDOW_SECONDS = 7 * 24 * 60 * 60  # 1 week
STOP_LOSS_CHANGE_INTERVAL = 900

CALL_OPTIONS_FILE_NAME = "cache/calls.txt"
PUT_OPTIONS_FILE_NAME = "cache/puts.txt"
options_file_names = {'C': CALL_OPTIONS_FILE_NAME, 'P': PUT_OPTIONS_FILE_NAME}

async def calculate_max_loss(right: str, should_consider_only_effective: bool = False) -> float:
    """Helper function to calculate max loss for a given side."""
    max_loss_calculator = MaxLossCalculator()
    if should_consider_only_effective:
        return await max_loss_calculator.calculate_effective_max_loss(right)
    else:
        return await max_loss_calculator.calculate_max_loss(right)

class MaxLossCalculator:
    """
    Singleton manager for calculating dynamic stop-loss levels.
    Factors in account cash, position size, delta risk, and time to expiration.
    """
    _instance: Optional['MaxLossCalculator'] = None

    def __new__(cls) -> 'MaxLossCalculator':
        if cls._instance is None:
            cls._instance = super(MaxLossCalculator, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
            
        self.account_data = AccountData()
        self.market_data_fetcher = MarketDataFetcher()
        self.trading_bot = TradingBot()
        self.last_max_loss = {'C': DEFAULT_MAX_LOSS, 'P': DEFAULT_MAX_LOSS}
        self.last_effective_max_loss = {'C': DEFAULT_MAX_LOSS, 'P': DEFAULT_MAX_LOSS}
        self.last_save_time = {'C': 0.0, 'P': 0.0}
        self.last_effective_calculation_time = {'C': 0.0, 'P': 0.0}
        self.quantity: Dict[str, List[Tuple[float, float]]] = {'C': [], 'P': []}
        self.risk_fraction = {'C': 1.0, 'P': 1.0}
        
        try:
            if os.path.exists(CALL_OPTIONS_FILE_NAME):
                with open(CALL_OPTIONS_FILE_NAME, "r") as f:
                    self.quantity['C'] = json.load(f)
            if os.path.exists(PUT_OPTIONS_FILE_NAME):
                with open(PUT_OPTIONS_FILE_NAME, "r") as f:
                    self.quantity['P'] = json.load(f)
        except Exception as e:
            logger.error(f"Error loading max loss data: {e}")

        self._initialized = True

    async def calculate_max_loss(self, right: str) -> float:
        """
        Calculates the base max loss based on historical position sizes and available cash.
        Updates the position quantity cache.
        """
        if time.time() - self.last_save_time[right] < 3600:
            return self.last_max_loss[right]

        positions = await self.trading_bot.get_short_options()
        now = time.time()

        position_quantities_for_right = [-position.position for position in positions if position.contract.right == right]
        number_of_options = sum(position_quantities_for_right)
        self.quantity[right].append((now, number_of_options))
        self.quantity[right] = [(t, v) for t, v in self.quantity[right] if now - t <= WINDOW_SECONDS]

        try:
            with open(options_file_names[right], "w") as f:
                json.dump(self.quantity[right], f)
        except Exception as e:
            logger.error(f"Failed to save max loss data: {e}")

        max_number_of_options = self.get_max_number_of_options(right)

        max_loss = DEFAULT_MAX_LOSS
        if max_number_of_options > 0:
            write_heartbeat()
            total_cash_value = self.account_data.get_cash_balance_value()
            extra_cash_per_contract = (total_cash_value - 1000) / max_number_of_options
            extra_cash_per_contract = max(extra_cash_per_contract, 0.0)
            extra_cash_per_option = extra_cash_per_contract / 100
            
            raw_risk_fraction = 1.0
            if extra_cash_per_option > 0:
                raw_risk_fraction = 1.0 / math.sqrt(extra_cash_per_option)
            risk_fraction = min(raw_risk_fraction, 1.0)
            
            logger.info(f"Risk fraction for {right}: {risk_fraction:.2f}, Cash per option: {extra_cash_per_option:.2f}")
            max_loss = max(extra_cash_per_option * risk_fraction, DEFAULT_MAX_LOSS)
            self.risk_fraction[right] = risk_fraction

        self.last_save_time[right] = time.time()
        self.last_max_loss[right] = max_loss

        return max_loss

    async def calculate_effective_max_loss(self, right: str) -> float:
        """
        Calculates a more granular max loss that factors in the current delta of positions
        and the time remaining in the trading session.
        """
        regular_hours_end_today = datetime.combine(datetime.today(), REGULAR_HOURS_END_TIME)
        end_time_ts = regular_hours_end_today.timestamp()
        now_ts = time.time()
        
        if now_ts > end_time_ts and self.last_effective_calculation_time[right] <= end_time_ts:
            self.last_effective_calculation_time[right] = 0.0

        if now_ts - self.last_effective_calculation_time[right] < STOP_LOSS_CHANGE_INTERVAL:
            return self.last_effective_max_loss[right]

        positions = await self.trading_bot.get_short_options()
        now = time.time()

        position_quantities_for_right = [-position.position for position in positions if position.contract.right == right]
        number_of_options = sum(position_quantities_for_right)
        self.quantity[right].append((now, number_of_options))
        self.quantity[right] = [(t, v) for t, v in self.quantity[right] if now - t <= WINDOW_SECONDS]

        max_number_of_options = self.get_max_number_of_options(right)
        effective_number_of_options = 0.0
        positions_for_right = [p for p in positions if p.contract.right == right]
        
        now_nyc = datetime.now(new_york_timezone)
        premarket_start_delta = 0 if now_nyc.time() > PREMARKET_START_TIME else -1
        premarket_start_day = now_nyc.date() + timedelta(days=premarket_start_delta)
        end_of_trade_day = premarket_start_day + timedelta(days=1)
        start_dt = datetime.combine(premarket_start_day, PREMARKET_START_TIME, tzinfo=new_york_timezone)
        end_dt = datetime.combine(end_of_trade_day, REGULAR_HOURS_END_TIME, tzinfo=new_york_timezone)
        
        time_fraction = 1.0 if is_after_hours() else max(0.0, (end_dt - now_nyc) / (end_dt - start_dt))
        
        for p in positions_for_right:
            delta = get_delta(p.contract.ticker)
            if delta is None or math.isnan(delta) or is_night_break() or is_after_hours():
                effective_number_of_options -= p.position
                continue

            # Risk multiplier based on delta and time remaining
            initial_risk = math.sqrt(min(abs(delta) * 100, 1.0))
            risk_by_delta_time = initial_risk + (1.0 - initial_risk) * (1.0 - time_fraction)
            effective_number_of_options -= p.position * risk_by_delta_time

        effective_number_of_options = math.ceil(effective_number_of_options)
        if effective_number_of_options == 0:
            effective_number_of_options = max_number_of_options

        write_heartbeat()
        total_cash = self.account_data.get_cash_balance_value()
        extra_cash_per_contract = max(0.0, (total_cash - 1000) / effective_number_of_options)
        extra_cash_per_option = extra_cash_per_contract / 100
        
        raw_risk_frac = 1.0 / math.sqrt(extra_cash_per_option) if extra_cash_per_option > 0 else 1.0
        risk_frac = min(raw_risk_frac, 1.0)
        
        raw_max_loss = max(extra_cash_per_option * risk_frac, DEFAULT_MAX_LOSS)
        max_loss = math.sqrt(time_fraction) * raw_max_loss
        
        logger.info(f"Effective Max Loss ({right}): {max_loss:.2f}, Time Fraction: {time_fraction:.2f}")

        self.last_effective_calculation_time[right] = time.time()
        self.last_effective_max_loss[right] = max_loss

        return max_loss

    def get_max_number_of_options(self, right: str) -> int:
        """Returns the maximum number of options held on this side within the tracking window."""
        if not self.quantity[right]:
            return 0
        return int(max(item[1] for item in self.quantity[right]))
