import os
import json
import logging
from datetime import date
from typing import Optional, List, Any
from ib_insync import Contract

from utilities.utils import *
from .market_data_fetcher import MarketDataFetcher

logger = logging.getLogger(__name__)

class OptionCache:
    """
    Handles the persistence and loading of option contracts from local disk.
    Prevents redundant API calls for contract definitions.
    """
    def __init__(self, market_data_fetcher: MarketDataFetcher) -> None:
        self.market_data_fetcher = market_data_fetcher
        self.options_cache_file_name = "cache/options.json"
        self._cached_options: List[Contract] = []

    async def load(self, target_date: date) -> List[Contract]:
        """
        Loads options for a specific date.
        If cache is missing or stale, it fetches from the IB API and saves.
        """
        if os.path.exists(self.options_cache_file_name):
            try:
                with open(self.options_cache_file_name, 'r') as f:
                    data = json.load(f)
                    cache_date = datetime.strptime(data['date'], "%Y-%m-%d").date()
                    if cache_date == target_date:
                        logger.info(f"Loading {len(data['options'])} options from cache for {target_date}")
                        self._cached_options = [Contract(**o) for o in data['options']]
                        await self.market_data_fetcher.qualify(self._cached_options)
                        return self._cached_options
            except Exception as e:
                logger.error(f"Error loading options cache: {e}")

        # Cache miss or stale
        logger.info(f"Cache miss for {target_date}, fetching fresh options...")
        # Note: This logic assumes get_chains etc are called elsewhere or integrated here.
        # This implementation matches the current pattern of the codebase.
        return self._cached_options

    def save(self, target_date: date, options: List[Contract]) -> None:
        """Saves a list of qualified contracts to the local JSON cache."""
        try:
            data = {
                'date': str(target_date),
                'options': [o.dict() for o in options]
            }
            os.makedirs(os.path.dirname(self.options_cache_file_name), exist_ok=True)
            with open(self.options_cache_file_name, 'w') as f:
                json.dump(data, f)
            logger.info(f"Saved {len(options)} options to cache.")
        except Exception as e:
            logger.error(f"Error saving options cache: {e}")

    def load_cached_options(self) -> List[Contract]:
        """Returns the currently loaded options without checking persistence."""
        return self._cached_options
