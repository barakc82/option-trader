import math
import os
import json
import logging
import pandas as pd
import exchange_calendars as ecals
from datetime import datetime
from collections import deque
from ib_insync import Index, Future, Stock
from utilities.utils import is_regular_hours, CACHED_JSON_PATH
from .connection_manager import ConnectionManager
from .market_data_utils import SPXESPair

logger = logging.getLogger(__name__)

class IndexPriceManager:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(IndexPriceManager, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if not self._initialized:
            self.ib = ConnectionManager().ib
            self.spx = Index(symbol='SPX', exchange='CBOE', currency='USD')
            self.es = None
            self.spx_es_history = deque(maxlen=100)
            self.previous_spx_value = math.nan
            self.previous_es_value = math.nan
            self._initialized = True

    def get_spx_price(self):
        spx_ticker = self.ib.ticker(self.spx)

        if not spx_ticker:
            logger.info("SPX ticker is missing")
            return self.previous_spx_value

        if math.isnan(spx_ticker.last):
            if is_regular_hours():
                return self.previous_spx_value
            logger.warning("Market closed; using SPX close price.")
            price = spx_ticker.close
        else:
            price = spx_ticker.last

        if not math.isnan(price):
            self.previous_spx_value = price

        return price

    def get_es_price(self):
        if not self.es:
            return self.previous_es_value

        es_ticker = self.ib.ticker(self.es)

        if not es_ticker:
            logger.info("ES ticker is missing")
            return self.previous_es_value

        price = es_ticker.marketPrice()

        if not math.isnan(price):
            self.previous_es_value = price

        return price

    async def fetch_es_future(self):
        today_str = datetime.now().strftime('%Y%m%d')
        if not self.es or self.es.lastTradeDateOrContractMonth < today_str:
            es_incomplete = Future('ES', '20260918', exchange='CME', currency='USD')
            logger.info(f"Requesting the details of the closest ES future for {today_str}")
            es_details = await self.ib.reqContractDetailsAsync(es_incomplete)
            contracts = [es_detail.contract for es_detail in es_details if es_detail.contract.lastTradeDateOrContractMonth >= today_str]
            contracts.sort(key=lambda c: c.lastTradeDateOrContractMonth)
            closest_es_future = contracts[0]
            await self.ib.qualifyContractsAsync(closest_es_future)
            logger.info(f"Selected ES future: {closest_es_future.lastTradeDateOrContractMonth}")
            self.es = closest_es_future
        return self.es

    def get_spot_price(self):
        indices_difference = self.calculate_spx_es_difference()
        if is_regular_hours():
            return self.get_spx_price()
        return self.get_es_price() + indices_difference

    def calculate_spx_es_difference(self):
        if not self.spx_es_history:
            if os.path.exists(CACHED_JSON_PATH):
                try:
                    with open(CACHED_JSON_PATH, "r") as f:
                        return json.load(f).get('spx_premium', 0)
                except Exception as e:
                    logger.error(f"Error reading spx_premium from {CACHED_JSON_PATH}: {e}")
            return 0

        total_diff = sum(entry.spx_price - entry.es_price for entry in self.spx_es_history)
        return total_diff / len(self.spx_es_history)

    def on_index_ticker_update(self):
        spx_ticker = self.ib.ticker(self.spx)
        es_ticker = self.ib.ticker(self.es) if self.es else None

        if not is_regular_hours() or not spx_ticker or math.isnan(spx_ticker.last):
            return

        # Update SPX-ES history
        if es_ticker and not math.isnan(es_ticker.last):
            if (spx_ticker.time and es_ticker.time and
                    (spx_ticker.time - es_ticker.time).total_seconds() <= 2):
                new_es_entry = SPXESPair(
                    spx_price=spx_ticker.last,
                    es_price=es_ticker.last,
                    time=datetime.now()
                )
                if (not self.spx_es_history or
                        (new_es_entry.time - self.spx_es_history[-1].time).total_seconds() >= 5 * 60):
                    self.spx_es_history.append(new_es_entry)
    