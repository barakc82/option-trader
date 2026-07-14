import math

from utilities.utils import *
from .trading_bot import TradingBot
from .account_data import AccountData
from .market_data_fetcher import MarketDataFetcher

MIN_NUMBER_OF_RECORDED_OPTIONS_QUANTITIES = 5

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

DEFAULT_MAX_LOSS = 1.0
WINDOW_SECONDS = 7 * 24 * 60 * 60  # 1 week
STOP_LOSS_CHANGE_INTERVAL = 900

INITIAL_FULL_MODE = 0
LIMITED_MODE = 1
LASTING_FULL_MODE = 2

CALL_OPTIONS_FILE_NAME = "cache/calls.txt"
PUT_OPTIONS_FILE_NAME = "cache/puts.txt"
options_file_names = {'C': CALL_OPTIONS_FILE_NAME, 'P': PUT_OPTIONS_FILE_NAME}


class MaxLossCalculator:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(MaxLossCalculator, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if not self._initialized:
            self.account_data = AccountData()
            self.market_data_fetcher = MarketDataFetcher()
            self.trading_bot = TradingBot()
            self.last_max_loss = {'C': DEFAULT_MAX_LOSS, 'P': DEFAULT_MAX_LOSS}
            self.last_calculation_time = {'C': 0.0, 'P': 0.0}
            self.last_dump_time = {'C': 0.0, 'P': 0.0}
            self.quantity = {'C': [], 'P': []}
            self.risk_fraction = {'C': 1.0, 'P': 1.0}
            self.operation_modes = {'C': INITIAL_FULL_MODE, 'P': INITIAL_FULL_MODE}

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

    def calculate_max_loss(self, right):
        regular_hours_end_time_today = datetime.combine(datetime.today(), REGULAR_HOURS_END_TIME)
        end_time_timestamp = regular_hours_end_time_today.timestamp()
        current_time = time.time()
        if current_time > end_time_timestamp >= self.last_calculation_time[right]:
            self.last_calculation_time[right] = 0

        if time.time() - self.last_calculation_time[right] < 60:
            return self.last_max_loss[right]

        now = time.time()
        if now - self.last_dump_time[right] >= 3600:
            number_of_options = self.get_current_number_of_options(right)
            self.quantity[right].append((now, number_of_options))
            self.quantity[right] = [(t, v) for t, v in self.quantity[right] if now - t <= WINDOW_SECONDS]
            try:
                with open(options_file_names[right], "w") as f:
                    json.dump(self.quantity[right], f)
                self.last_dump_time[right] = now
            except Exception as e:
                logger.error(f"{e}")

        number_of_options = 0
        if self.operation_modes[right] == LIMITED_MODE:
            number_of_options = max(self.get_max_number_of_options_before_spreads(right), number_of_options - 1)
        else:
            number_of_options = max(self.get_max_number_of_options(right), number_of_options - 1)

        max_loss = DEFAULT_MAX_LOSS
        if number_of_options and len(self.quantity[right]) >= MIN_NUMBER_OF_RECORDED_OPTIONS_QUANTITIES:
            total_cash_value = self.account_data.get_cash_balance_value()
            extra_cash_per_contract = (total_cash_value - 1000) / number_of_options
            extra_cash_per_contract = max(extra_cash_per_contract, 0)

            extra_cash_per_option = extra_cash_per_contract / 100
            raw_risk_fraction = 1
            if extra_cash_per_option > 0:
                raw_risk_fraction = 1 / math.sqrt(extra_cash_per_option)
            risk_fraction = min(raw_risk_fraction, 1)

            logger.info(f"Risk fraction for {right} is {risk_fraction:.2f}, extra cash per option: {extra_cash_per_option:.2f}, "
                        f"total cash: {total_cash_value:.2f}, max number of options: {number_of_options}")
            max_loss = max(extra_cash_per_option * risk_fraction, DEFAULT_MAX_LOSS)
            self.risk_fraction[right] = risk_fraction

        self.last_calculation_time[right] = time.time()
        self.last_max_loss[right] = max_loss

        return max_loss

    def get_current_number_of_options(self, right):
        positions = self.trading_bot.get_short_options()
        position_quantities_for_right = [-position.position for position in positions if
                                         position.contract.right == right]
        number_of_options = sum(position_quantities_for_right)
        return number_of_options

    def get_max_number_of_options(self, right):
        if not self.quantity[right]:
            return 0
        historical_max = max(item[1] for item in self.quantity[right])
        current_number_of_options = self.get_current_number_of_options(right)
        return max(current_number_of_options, historical_max)

    def get_max_number_of_options_before_spreads(self, right):
        if not self.quantity[right]:
            return 0
        historical_max = max(item[1] for item in self.quantity[right])
        current_number_of_options = self.get_current_number_of_options(right)
        return max(current_number_of_options, historical_max)

    def notify_below_minimal_price_level(self, right):
        if self.operation_modes[right] != LASTING_FULL_MODE:
            self.operation_modes[right] = LIMITED_MODE

    def notify_spread_usage(self, right):
        self.operation_modes[right] = LASTING_FULL_MODE
