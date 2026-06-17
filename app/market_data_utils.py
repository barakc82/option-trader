import math
from dataclasses import dataclass
from datetime import datetime

# Market Data Types
LIVE_DATA = 1
FROZEN_DATA = 2

@dataclass
class SPXESPair:
    spx_price: float
    es_price: float
    time: datetime

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
