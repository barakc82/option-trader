import os
import pickle
import time
import logging

file_path = "cache/option_store.pql"

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


class OptionCache:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(OptionCache, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if not self._initialized:
            self._initialized = True

    def save(self, options):
        """Saves options to the cache file."""
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, "wb") as file:
            pickle.dump(options, file)
        logger.info(f"Options saved to cache at {file_path}")

    def load_cached_options(self):
        """Loads options from the cache file if it exists."""
        if os.path.exists(file_path):
            try:
                with open(file_path, 'rb') as file:
                    options = pickle.load(file)
                    return options
            except (EOFError, pickle.UnpicklingError) as e:
                logger.error(f"Failed to load options from cache: {e}")
                try:
                    os.remove(file_path)
                except OSError:
                    pass
        return []
