import json
import logging
import os

from utilities.utils import OPTION_TRADER_CONFIG_PATH


logger = logging.getLogger(__name__)


class MachineLearningExposureManager:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(MachineLearningExposureManager, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if not self._initialized:
            self.contract_id_to_slots_used = {}
            self.trade_id_to_slots_used = {}
            logger.info("MachineLearningExposureManager singleton initialized.")
            self._initialized = True



