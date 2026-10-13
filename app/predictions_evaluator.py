import asyncio
import csv
import glob
import logging
import os
from datetime import datetime, timedelta

from utilities.utils import new_york_timezone, REGULAR_HOURS_END_TIME
from .market_data_fetcher import MarketDataFetcher

logger = logging.getLogger(__name__)

PREDICTIONS_CSV_PATTERN = "cache/predictions_*.csv"
PREDICTION_AVERAGES_CSV_PATH = "cache/prediction_averages.csv"
PROCESS_LOOP_INTERVAL_SECONDS = 300


class PredictionsEvaluator:
    """Groups each day's sampled sell-opportunity predictions by (strike, right) once that
    expiration's regular session has closed, then -- for each record --
    fetches that option's minute-by-minute bar highs from the record's own
    sampling time through expiration to determine whether the stop loss was
    hit, and computes the resulting realized profit. Results are kept in
    memory (self.grouped_predictions / self.average_realized_profit), keyed
    by expiration date -- no output file is written."""

    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(PredictionsEvaluator, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if not self._initialized:
            self.market_data_fetcher = MarketDataFetcher()
            self._initialized = True

    async def run(self):
        logger.info("Starting predictions processing loop...")
        while True:
            try:
                 await self.process_expired_prediction_files()
            except Exception:
                logger.exception("PredictionsEvaluator: Loop error:")
            await asyncio.sleep(PROCESS_LOOP_INTERVAL_SECONDS)

    async def process_expired_prediction_files(self):
        now_nyc = datetime.now(new_york_timezone)
        for csv_path in glob.glob(PREDICTIONS_CSV_PATTERN):
            expiration_date_str = self._extract_expiration_date(csv_path)
            if expiration_date_str is None:
                continue

            try:
                expiration_date = datetime.strptime(expiration_date_str, '%Y%m%d').date()
            except ValueError:
                logger.warning(f"PredictionsEvaluator: Could not parse expiration date from {csv_path}")
                continue

            regular_hours_end = new_york_timezone.localize(datetime.combine(expiration_date, REGULAR_HOURS_END_TIME))
            if now_nyc <= regular_hours_end:
                continue

            averages_logged = await self._process_file(csv_path, expiration_date_str)
            if averages_logged:
                os.remove(csv_path)
                logger.info(f"PredictionsEvaluator: Deleted {csv_path} after processing")

    @staticmethod
    def _extract_expiration_date(csv_path):
        filename = os.path.basename(csv_path)
        prefix, suffix = "predictions_", ".csv"
        if not (filename.startswith(prefix) and filename.endswith(suffix)):
            return None
        return filename[len(prefix):-len(suffix)]

    @staticmethod
    def _as_naive(dt):
        """Strips tzinfo so bar.date (whatever tz-awareness ib_insync gives it) can be
        compared against the tz-aware NY-localized record_start_time below."""
        return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt

    async def _process_file(self, csv_path, expiration_date_str):
        """Groups every sampled record in csv_path by (strike, right) -- the
        same option can appear many times, once per sampling pass during the
        day -- sorted by minutes_to_expiration. For each (strike, right)
        group, fetches that option's minute bars once (covering the group's
        earliest sample through expiration) and, for every record, finds the
        maximal high from that record's own sampling time until expiration
        to decide whether the stop loss was hit: realized_profit is
        estimated_sell_price if not, or estimated_sell_price - stop_loss if
        it was. Finally averages realized_profit across every record in the
        file."""

        logger.info(f"Processing predictions for {expiration_date_str}")
        grouped = {}
        with open(csv_path, 'r', newline='') as f:
            reader = csv.DictReader(f)
            for row in reader:
                key = f"{row['strike']}_{row['right']}"
                grouped.setdefault(key, []).append(row)

        for records in grouped.values():
            records.sort(key=lambda row: int(row['minutes_to_expiration']))

        expiry_date = datetime.strptime(expiration_date_str, '%Y%m%d').date()
        day_end = new_york_timezone.localize(datetime.combine(expiry_date, REGULAR_HOURS_END_TIME))

        expected_profits = []
        realized_profits = []
        for key, records in grouped.items():
            strike = float(records[0]['strike'])
            right = records[0]['right']
            logger.info(f"Processing the predictions for {right} {strike}")
            oldest_minutes_to_expiration = max(int(r['minutes_to_expiration']) for r in records)

            bars = await self.market_data_fetcher.get_minute_bars(
                strike, right, expiration_date_str, oldest_minutes_to_expiration,
            )
            # Paces successive reqHistoricalDataAsync calls across distinct (strike, right)
            # groups so this loop doesn't trip IB's historical-data pacing limits.
            await asyncio.sleep(4)
            if not bars:
                logger.warning(f"PredictionsEvaluator: No bars found for {right} {strike} "
                                f"({expiration_date_str}); skipping its {len(records)} record(s)")
                continue

            for record in records:
                record_start_time = day_end - timedelta(minutes=int(record['minutes_to_expiration']))
                relevant_highs = [bar.high for bar in bars
                                  if self._as_naive(bar.date) >= self._as_naive(record_start_time)]
                max_high = max(relevant_highs) if relevant_highs else 0.0

                estimated_sell_price = float(record['estimated_sell_price'])
                stop_loss = float(record['stop_loss'])
                hit_stop_loss = max_high >= stop_loss
                realized_profit = estimated_sell_price - stop_loss if hit_stop_loss else estimated_sell_price

                record['max_high'] = max_high
                record['hit_stop_loss'] = hit_stop_loss
                record['realized_profit'] = realized_profit
                expected_profits.append(float(record['expected_profit']))
                realized_profits.append(realized_profit)

        if not realized_profits:
            logger.error(f"Could not find any realized profits for {expiration_date_str}")
            return False

        average_realized_profit = sum(realized_profits) / len(realized_profits)
        average_expected_profit = sum(expected_profits) / len(expected_profits)
        profit_deviation = average_realized_profit - average_expected_profit
        self._log_averages(expiration_date_str, average_expected_profit, average_realized_profit, profit_deviation)

        logger.info(f"PredictionsEvaluator: Processed {csv_path} into {len(grouped)} strike/right group(s); "
                    f"average expected profit over {len(expected_profits)} record(s) = {average_expected_profit:.4f}; "
                    f"average realized profit = {average_realized_profit:.4f}")
        return True

    @staticmethod
    def _log_averages(expiration_date_str, average_expected_profit, average_realized_profit, profit_deviation):
        write_header = not os.path.exists(PREDICTION_AVERAGES_CSV_PATH)
        with open(PREDICTION_AVERAGES_CSV_PATH, 'a', newline='') as f:
            writer = csv.writer(f)
            if write_header:
                writer.writerow(['expiration_date', 'average_expected_profit', 'average_realized_profit',
                                  'profit_deviation'])
            writer.writerow([expiration_date_str, average_expected_profit, average_realized_profit,
                              profit_deviation])
