from dataclasses import dataclass
from tws_connection import TwsConnection
import numpy as np
from utils import *

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

PORTFOLIO_MARGIN = "portfolio_margin"
MINIMAL_SELL_PRICE = 0.15

req_id_to_comment = {}
req_id_to_target_delta = {}

@dataclass
class SellOptionResult:
    success: bool = False
    trade = None
    no_option_above_minimal_sell_price: bool = False
    is_low_projected_cushion: bool = False
    required_initial_margin: float = 0
    initial_margin_after: float = 0


def connect(client_id):
    tws_connection = TwsConnection()
    try:
        tws_connection.connect(client_id)
    except ConnectionError as e:
        logger.error(f"Connection Error: Open TWS or IB Gateway")
    return tws_connection


def extract_delta(ticker):
    if not ticker.modelGreeks or ticker.modelGreeks.delta is None:
        if not ticker.lastGreeks or ticker.lastGreeks.delta is None:
            return None
        return abs(ticker.lastGreeks.delta)

    return abs(ticker.modelGreeks.delta)


def extract_ask(ticker):
    if ticker.ask is None:
        return None
    return ticker.ask


def extract_last_median_price(ticker):
    return np.nanmedian([ticker.bid, ticker.ask, ticker.last])
