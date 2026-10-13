import asyncio
import logging
import math

from .connection_manager import ConnectionManager

logger = logging.getLogger(__name__)


class LimitAdjuster:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(LimitAdjuster, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if not self._initialized:
            self.ib = ConnectionManager().ib
            self.price_increments = []
            self._initialized = True

    def is_ready(self) -> bool:
        return bool(self.price_increments)

    async def fetch_price_increments(self, contract):
        while not self.price_increments:
            try:
                details = await asyncio.wait_for(self.ib.reqContractDetailsAsync(contract), timeout=10)
                if not details:
                    logger.error(f"No contract details found for {contract}. Retrying...")
                    await asyncio.sleep(5)
                    continue

                market_rule_id = int(details[0].marketRuleIds.split(',')[0])
                rule = await asyncio.wait_for(self.ib.reqMarketRuleAsync(market_rule_id), timeout=10)
                if rule:
                    self.price_increments = sorted(rule, key=lambda i: i.lowEdge)
                    logger.info(f"Successfully fetched {len(self.price_increments)} price increments")
                else:
                    logger.error(f"Market rule {market_rule_id} returned no increments. Retrying...")
                    await asyncio.sleep(5)
            except asyncio.TimeoutError:
                logger.error(f"Timeout while fetching price increments for {contract}. Retrying...")
                await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"Error while fetching price increments for {contract}: {e}. Retrying...")
                await asyncio.sleep(5)

    def adjust_limit_to_market_rules(self, raw_limit):
        current_increment = self.price_increments[0].increment
        for i in self.price_increments:
            if raw_limit > i.lowEdge:
                current_increment = i.increment
        if current_increment == 0:
            logger.error("current increment is 0, so cannot adjust the limit for the order")
        if math.isnan(current_increment):
            logger.error("current increment is NaN, so cannot adjust the limit for the order")
        if math.isnan(raw_limit):
            logger.error("raw limit is NaN, so cannot adjust the limit for the order")
        return round(round(raw_limit / current_increment) * current_increment, 6)
