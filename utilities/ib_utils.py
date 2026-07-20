from dataclasses import dataclass
import time
import numpy as np
import logging
import math
from datetime import datetime, timedelta
from typing import Any
from ib_insync import Trade

from utilities.utils import get_option_name, is_after_hours, is_regular_hours, new_york_timezone, REGULAR_HOURS_END_TIME
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


@dataclass
class SellOptionResult:
    success: bool = False
    trade = None
    no_option_above_minimal_sell_price: bool = False
    required_initial_margin: float = 0
    initial_margin_after: float = 0


@dataclass
class PositionInitialState:
    is_executed: int
    strike: float
    right: str
    expiry: str
    estimated_sell_price: float
    stop_loss_per_option: float
    target_delta: float
    bid_delta: float | None = None
    ask_delta: float | None = None
    last_delta: float | None = None
    model_delta: float | None = None
    minutes_to_expiration: int | None = None
    quantity: int = 0
    implied_volatility: float | None = None
    distance_to_stop_pct: float | None = None
    stop_loss_activated: int = 0


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
    deltas_to_consider = []
    if (ticker.bidGreeks and ticker.bidGreeks.delta and math.isnan(ticker.bidGreeks.delta) and
            ticker.askGreeks and ticker.askGreeks.delta and math.isnan(ticker.askGreeks.delta)):
        bid_ask_delta = (abs(ticker.bidGreeks.delta) + abs(ticker.askGreeks.delta)) / 2
        deltas_to_consider.append(bid_ask_delta)
    if ticker.lastGreeks and ticker.lastGreeks.delta:
        deltas_to_consider.append(abs(ticker.lastGreeks.delta))
    if ticker.modelGreeks and ticker.modelGreeks.delta:
        deltas_to_consider.append(abs(ticker.modelGreeks.delta))
    if not deltas_to_consider:
        return None
    return max(deltas_to_consider)


def get_individual_deltas(ticker):
    bid_delta = abs(ticker.bidGreeks.delta) if ticker.bidGreeks and ticker.bidGreeks.delta is not None else None
    ask_delta = abs(ticker.askGreeks.delta) if ticker.askGreeks and ticker.askGreeks.delta is not None else None
    last_delta = abs(ticker.lastGreeks.delta) if ticker.lastGreeks and ticker.lastGreeks.delta is not None else None
    model_delta = abs(ticker.modelGreeks.delta) if ticker.modelGreeks and ticker.modelGreeks.delta is not None else None
    return bid_delta, ask_delta, last_delta, model_delta


def get_delta_for_sell(ticker):
    deltas_to_consider = []
    if ticker.askGreeks and ticker.askGreeks.delta is not None:
        deltas_to_consider.append(abs(ticker.askGreeks.delta))
    if ticker.lastGreeks and ticker.lastGreeks.delta is not None:
        deltas_to_consider.append(abs(ticker.lastGreeks.delta))
    if ticker.modelGreeks and ticker.modelGreeks.delta is not None:
        deltas_to_consider.append(abs(ticker.modelGreeks.delta))
    if not deltas_to_consider:
        return None
    return max(deltas_to_consider)


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
        logger.error(f"interpolate_es_price returned negative value: {result:.4f} "
                     f"(spx_strike={spx_strike}, indices_difference={indices_difference}, "
                     f"lower_es.strike={lower_es.strike}, upper_es.strike={upper_es.strike}, "
                     f"lower_price={lower_price}, upper_price={upper_price}, t={t:.4f})")
    return result


def _norm_cdf(x):
    return (1 + math.erf(x / math.sqrt(2))) / 2


def _bs_price(S, K, T, r, sigma, right):
    if T <= 0 or sigma <= 0 or S <= 0:
        return math.nan
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if right == 'C':
        return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    else:
        return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def calculate_distance_to_stop(option, ticker, stop_loss, spot_price, r):
    """Return SPX points between spot_price and the level where the option hits stop_loss."""
    greeks = ticker.modelGreeks or ticker.lastGreeks
    if not greeks or greeks.impliedVol is None or math.isnan(greeks.impliedVol) or greeks.impliedVol <= 0:
        return math.nan

    sigma = greeks.impliedVol
    K = option.strike
    right = option.right
    expiry_date = datetime.strptime(option.lastTradeDateOrContractMonth, '%Y%m%d').date()
    expiry_datetime = new_york_timezone.localize(datetime.combine(expiry_date, REGULAR_HOURS_END_TIME))
    T = (expiry_datetime - datetime.now(new_york_timezone)).total_seconds() / (365.25 * 24 * 3600)
    if T <= 0:
        return math.nan

    # For puts: price decreases as S rises → S* < spot (SPX must fall to hit stop loss)
    # For calls: price increases as S rises → S* > spot (SPX must rise to hit stop loss)
    if right == 'P':
        S_low, S_high = spot_price * 0.3, spot_price * 1.2
        if _bs_price(S_low, K, T, r, sigma, right) < stop_loss:
            return math.nan
        if _bs_price(S_high, K, T, r, sigma, right) > stop_loss:
            return math.nan
    else:
        S_low, S_high = spot_price * 0.8, spot_price * 2.0
        if _bs_price(S_low, K, T, r, sigma, right) > stop_loss:
            return math.nan
        if _bs_price(S_high, K, T, r, sigma, right) < stop_loss:
            return math.nan

    for _ in range(60):
        S_mid = (S_low + S_high) / 2
        price_mid = _bs_price(S_mid, K, T, r, sigma, right)
        if right == 'P':
            if price_mid > stop_loss:
                S_low = S_mid
            else:
                S_high = S_mid
        else:
            if price_mid < stop_loss:
                S_low = S_mid
            else:
                S_high = S_mid

    S_star = (S_low + S_high) / 2
    return spot_price - S_star if right == 'P' else S_star - spot_price


def calculate_distance_to_stop_pct(option, ticker, stop_loss, spot_price, r):
    """Return distance to the stop-loss level as a percentage of spot_price, instead of points."""
    distance = calculate_distance_to_stop(option, ticker, stop_loss, spot_price, r)
    if math.isnan(distance) or spot_price == 0:
        return math.nan
    return distance / spot_price * 100


def get_minutes_to_expiration(option):
    expiry_date = datetime.strptime(option.lastTradeDateOrContractMonth, '%Y%m%d').date()
    expiry_datetime = new_york_timezone.localize(datetime.combine(expiry_date, REGULAR_HOURS_END_TIME))
    return round((expiry_datetime - datetime.now(new_york_timezone)).total_seconds() / 60)


def get_distance_to_stop_pct(option, estimated_sell_price, stop_loss_per_option, market_data_fetcher):
    indices_difference = market_data_fetcher.calculate_spx_es_difference()
    spot_price = market_data_fetcher.get_spx_price() if is_regular_hours() else market_data_fetcher.get_es_price() + indices_difference
    r = market_data_fetcher.get_cached_risk_free_rate()
    raw_stop_loss = estimated_sell_price + stop_loss_per_option
    return calculate_distance_to_stop_pct(option, option.ticker, raw_stop_loss, spot_price, r)
