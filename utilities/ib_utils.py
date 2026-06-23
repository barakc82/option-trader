from dataclasses import dataclass
import time
import numpy as np
import logging
import math
from datetime import datetime, timedelta
from typing import Any
from ib_insync import Trade

from utilities.utils import get_option_name, is_after_hours
from utilities.tws_connection import TwsConnection


logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

PORTFOLIO_MARGIN = "portfolio_margin"
MINIMAL_SELL_PRICE = 0.15

OPEN_SELL_ORDER_GENERAL_EXPIRATION_TIME = timedelta(minutes=20)
OPEN_SELL_ORDER_AFTER_HOURS_EXPIRATION_TIME = timedelta(minutes=5)
POSITION_BUYBACK_ORDER_EXPIRATION_TIME = timedelta(minutes=10)
OPEN_GENERAL_MARGIN_REDUCTION_BUY_ORDER_EXPIRATION_TIME = timedelta(minutes=5)


def get_open_sell_order_expiration_time():
    if is_after_hours():
        return OPEN_SELL_ORDER_AFTER_HOURS_EXPIRATION_TIME
    return OPEN_SELL_ORDER_GENERAL_EXPIRATION_TIME

req_id_to_comment = {}
req_id_to_target_delta = {}


@dataclass
class SellOptionResult:
    success: bool = False
    trade = None
    no_option_above_minimal_sell_price: bool = False
    required_initial_margin: float = 0
    initial_margin_after: float = 0


def get_time_passed_since_submission(trade: Trade) -> timedelta | Any:
    if not trade.log:
        return timedelta(0)
    submission_time = trade.log[0].time
    timezone = submission_time.tzinfo
    time_passed_since_submission = datetime.now(timezone) - submission_time
    return time_passed_since_submission


def connect(client_id):
    tws_connection = TwsConnection()
    try:
        tws_connection.connect(client_id)
    except ConnectionError as e:
        logger.error(f"Connection Error: Open TWS or IB Gateway")
    return tws_connection


def extract_ask(ticker):
    if ticker.ask is None:
        return None
    return ticker.ask


def get_es_option_name(es_contract):
    """Return a string representing the SPY option name."""
    return f"ES {es_contract.right} {es_contract.strike}"


def is_hollow(ticker):
    if ticker is None:
        return True
    return math.isnan(ticker.last) and math.isnan(ticker.bid) and math.isnan(ticker.ask)


def get_delta(ticker):
    if (ticker.bidGreeks and ticker.bidGreeks.delta and math.isnan(ticker.bidGreeks.delta) and
            ticker.askGreeks and ticker.askGreeks.delta and math.isnan(ticker.askGreeks.delta)):
        return (abs(ticker.bidGreeks.delta) + abs(ticker.askGreeks.delta)) / 2
    if ticker.lastGreeks and ticker.lastGreeks.delta:
        return abs(ticker.lastGreeks.delta)
    if ticker.modelGreeks and ticker.modelGreeks.delta:
        if int(time.time()) % 100:
            logger.warning(f"Using model greeks to calculate delta for {get_option_name(ticker.contract)}")
        return abs(ticker.modelGreeks.delta)
    return None


def get_delta_for_sell(ticker):
    if ticker.askGreeks and ticker.askGreeks.delta:
        return abs(ticker.askGreeks.delta)
    return None


def get_gamma(ticker):
    if ticker.lastGreeks and ticker.lastGreeks.gamma is not None:
        return ticker.lastGreeks.gamma
    if ticker.modelGreeks and ticker.modelGreeks.gamma is not None:
        return ticker.modelGreeks.gamma
    return math.nan


def get_implied_volatility(ticker):
    if ticker.lastGreeks and ticker.lastGreeks.impliedVol is not None:
        return ticker.lastGreeks.impliedVol
    if ticker.modelGreeks and ticker.modelGreeks.impliedVol is not None:
        return ticker.modelGreeks.impliedVol
    return math.nan


def find_high_limit_buy_trade(option, open_buy_trades):
    for open_buy_trade in open_buy_trades:
        if (option.conId == open_buy_trade.contract.conId and open_buy_trade.order.action.upper() == 'BUY' and
                open_buy_trade.order.orderType == 'LMT' and open_buy_trade.order.lmtPrice > 0.1):
            return open_buy_trade
    return None


def interpolate_es_price(spx_strike, indices_difference, lower_es, upper_es, lower_price, upper_price):
    if upper_es.strike == lower_es.strike:
        logger.error(f"For SPX strike of {spx_strike} upper_es.strike is equal to lower_es.strike: {upper_es.strike}")
        return upper_price

    equivalent_es_strike = spx_strike - indices_difference
    t = (equivalent_es_strike - lower_es.strike) / (upper_es.strike - lower_es.strike)
    result = lower_price * (1 - t) + upper_price * t
    if result < 0:
        result = upper_price if t > 0.5 else lower_price

    return result
