import math
import traceback

from account_data import AccountData
from market_data_fetcher import MarketDataFetcher, get_delta
from utils import *
from trading_bot import TradingBot

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

DEFAULT_MAX_LOSS = 1
WINDOW_SECONDS = 7 * 24 * 60 * 60  # 1 week
STOP_LOSS_CHANGE_INTERVAL = 900

CALL_OPTIONS_FILE_NAME = "../cache/calls.txt"
PUT_OPTIONS_FILE_NAME = "../cache/puts.txt"
options_file_names = {'C': CALL_OPTIONS_FILE_NAME, 'P': PUT_OPTIONS_FILE_NAME}


def calculate_max_loss(right, should_consider_only_effective=False):
    max_loss_calculator = MaxLossCalculator()
    if should_consider_only_effective:
        return max_loss_calculator.calculate_effective_max_loss(right)
    else:
        return max_loss_calculator.calculate_max_loss(right)

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
            self.last_effective_max_loss = {'C': DEFAULT_MAX_LOSS, 'P': DEFAULT_MAX_LOSS}
            self.last_save_time = {'C': 0, 'P': 0}
            self.last_effective_calculation_time = {'C': 0, 'P': 0}
            self.quantity = {'C': [], 'P': []}
            self.risk_fraction = {'C': 1, 'P': 1}
            try:
                with open(CALL_OPTIONS_FILE_NAME, "r") as f:
                    self.quantity['C'] = json.load(f)
                with open(PUT_OPTIONS_FILE_NAME, "r") as f:
                    self.quantity['P'] = json.load(f)
            except FileNotFoundError:
                print("Log file not found.")
            except json.decoder.JSONDecodeError as e:
                print(f"Error parsing JSON: {e}")

            self._initialized = True

    def calculate_max_loss(self, right):

        if time.time() - self.last_save_time[right] < 3600:
            return self.last_max_loss[right]

        positions = self.trading_bot.get_short_options()
        now = time.time()

        position_quantities_for_right = [-position.position for position in positions if position.contract.right == right]
        number_of_options = sum(position_quantities_for_right)
        self.quantity[right].append((now, number_of_options))
        self.quantity[right] = [(t, v) for t, v in self.quantity[right] if now - t <= WINDOW_SECONDS]

        try:
            with open(options_file_names[right], "w") as f:
                json.dump(self.quantity[right], f)
        except Exception as e:
            logger.error(f"{e}")
            traceback.print_exc()

        max_number_of_options = self.get_max_number_of_options(right)

        max_loss = DEFAULT_MAX_LOSS
        if max_number_of_options > 0:
            write_heartbeat()
            total_cash_value = self.account_data.get_cash_balance_value()
            extra_cash_per_contract = (total_cash_value - 1000) / max_number_of_options
            extra_cash_per_contract = max(extra_cash_per_contract, 0)
            extra_cash_per_option = extra_cash_per_contract / 100
            raw_risk_fraction = 1
            if extra_cash_per_option > 0:
                raw_risk_fraction = 1 / math.sqrt(extra_cash_per_option)
            risk_fraction = min(raw_risk_fraction, 1)
            logger.info(f"Risk fraction for {right} is {risk_fraction:.2f}, extra cash per option: {extra_cash_per_option:.2f}, "
                        f"total cash: {total_cash_value:.2f}, max number of options: {max_number_of_options}")
            max_loss = max(extra_cash_per_option * risk_fraction, DEFAULT_MAX_LOSS)
            self.risk_fraction[right] = risk_fraction

        self.last_save_time[right] = time.time()
        self.last_max_loss[right] = max_loss

        return max_loss

    def calculate_effective_max_loss(self, right):

        regular_hours_end_time_today = datetime.combine(datetime.today(), REGULAR_HOURS_END_TIME)
        end_time_timestamp = regular_hours_end_time_today.timestamp()
        current_time = time.time()
        if current_time > end_time_timestamp and self.last_effective_calculation_time[right] <= end_time_timestamp:
            self.last_effective_calculation_time[right] = 0

        if current_time - self.last_effective_calculation_time[right] < STOP_LOSS_CHANGE_INTERVAL:
            return self.last_effective_max_loss[right]

        positions = self.trading_bot.get_short_options()
        now = time.time()

        position_quantities_for_right = [-position.position for position in positions if position.contract.right == right]
        number_of_options = sum(position_quantities_for_right)
        self.quantity[right].append((now, number_of_options))
        self.quantity[right] = [(t, v) for t, v in self.quantity[right] if now - t <= WINDOW_SECONDS]

        max_number_of_options = self.get_max_number_of_options(right)
        effective_number_of_options = 0
        positions_for_right = [position for position in positions if position.contract.right == right]
        now_in_nyc = datetime.now(new_york_timezone)
        premarket_start_days_delta = 0 if now_in_nyc.time() > PREMARKET_START_TIME else -1
        premarket_start_day = now_in_nyc.date() + timedelta(days=premarket_start_days_delta)
        end_of_trade_day = premarket_start_day + timedelta(days=1)
        start_dt = datetime.combine(premarket_start_day, PREMARKET_START_TIME, tzinfo=new_york_timezone)
        end_dt = datetime.combine(end_of_trade_day, REGULAR_HOURS_END_TIME, tzinfo=new_york_timezone)
        if is_after_hours():
            fraction_of_time_left_to_expiration = 1
        else:
            fraction_of_time_left_to_expiration = (end_dt - now_in_nyc) / (end_dt - start_dt)
        for position in positions_for_right:
            delta = get_delta(position.contract.ticker)
            if delta is None or math.isnan(delta) or is_night_break() or is_after_hours():
                effective_number_of_options -= position.position
                continue

            initial_risk_by_delta = math.sqrt(min(abs(delta) * 100, 1))
            risk_by_delta_and_time = initial_risk_by_delta + (1 - initial_risk_by_delta) * (1 - fraction_of_time_left_to_expiration)
            logger.info(f"Assessed risk for {get_option_name(position.contract)}: {risk_by_delta_and_time:.2f}, "
                        f"fraction of time left to expiration: {fraction_of_time_left_to_expiration:.2f}, "
                        f"initial risk by delta alone: {initial_risk_by_delta:.2f}")

            effective_number_of_options -= position.position * risk_by_delta_and_time

            """
            last_price = self.market_data_fetcher.get_last_price(position.contract)
            ask = self.market_data_fetcher.get_ask(position.contract)
            low_risk = last_price == 0.05 or (ask <= 0.10 and not is_late_regular_hours())
            if not low_risk:
                effective_number_of_options -= position.position
            """

        effective_number_of_options = math.ceil(effective_number_of_options)
        if effective_number_of_options == 0:
            effective_number_of_options = max_number_of_options

        write_heartbeat()
        total_cash_value = self.account_data.get_cash_balance_value()
        extra_cash_per_contract = (total_cash_value - 1000) / effective_number_of_options
        extra_cash_per_contract = max(extra_cash_per_contract, 0)
        extra_cash_per_option = extra_cash_per_contract / 100
        raw_risk_fraction = 1
        if extra_cash_per_option > 0:
            raw_risk_fraction = 1 / math.sqrt(extra_cash_per_option)
        risk_fraction = min(raw_risk_fraction, 1)
        raw_max_loss = max(extra_cash_per_option * risk_fraction, DEFAULT_MAX_LOSS)
        max_loss = math.sqrt(fraction_of_time_left_to_expiration) * raw_max_loss
        logger.info(f"Risk fraction for {right} is {risk_fraction:.2f}, extra cash per option: {extra_cash_per_option:.2f}, "
                    f"total cash: {total_cash_value:.2f}, max number of options: {max_number_of_options}, "
                    f"effective number of options: {effective_number_of_options:.2f}, "
                    f"active number of options: {number_of_options}, initial max loss: {raw_max_loss:.2f}, "
                    f"final max loss: {max_loss:.2f}, "
                    f"fraction of time left to expiration: {fraction_of_time_left_to_expiration:.2f}")

        self.last_effective_calculation_time[right] = time.time()
        self.last_effective_max_loss[right] = max_loss

        return self.last_effective_max_loss[right]

    def get_max_number_of_options(self, right):
        return max(item[1] for item in self.quantity[right])
