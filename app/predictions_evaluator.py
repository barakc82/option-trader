import asyncio
import bisect
import csv
import glob
import logging
import math
import os
from datetime import datetime, timezone

from utilities.utils import new_york_timezone, REGULAR_HOURS_END_TIME, is_early_closing_time, is_early_trade
from .market_data_fetcher import MarketDataFetcher

logger = logging.getLogger(__name__)

PREDICTIONS_CSV_PATTERN = "cache/predictions_*.csv"
PREDICTION_AVERAGES_CSV_PATH = "cache/prediction_averages.csv"
PROBABILITY_BUCKETS_CSV_PATH = "cache/probability_buckets.csv"
EARLY_TRADE_PROBABILITY_BUCKETS_CSV_PATH = "cache/early_trade_probability_buckets.csv"
PROCESS_LOOP_INTERVAL_SECONDS = 300
BUCKET_LOWER_BOUNDS = [0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95]

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
            reader = csv.DictReader(f, delimiter='\t')
            for row in reader:
                key = f"{row['strike']}_{row['right']}"
                grouped.setdefault(key, []).append(row)

        for records in grouped.values():
            records.sort(key=lambda row: int(row['minutes_to_expiration']))

        expected_profits = []
        realized_profits = []
        bucket_record_counts = [0] * len(BUCKET_LOWER_BOUNDS)
        bucket_hit_counts = [0] * len(BUCKET_LOWER_BOUNDS)
        early_closing_bucket_record_counts = [0] * len(BUCKET_LOWER_BOUNDS)
        early_closing_bucket_hit_counts = [0] * len(BUCKET_LOWER_BOUNDS)
        for key, records in grouped.items():
            strike = float(records[0]['strike'])
            right = records[0]['right']
            logger.info(f"Processing the predictions for {right} {strike}")
            oldest_minutes_to_expiration = int(records[-1]['minutes_to_expiration'])

            bars = await self.market_data_fetcher.get_minute_bars(
                strike, right, expiration_date_str, oldest_minutes_to_expiration,
            )
            # Paces successive reqHistoricalDataAsync calls across distinct (strike, right)
            # groups so this loop doesn't trip IB's historical-data pacing limits.
            await asyncio.sleep(4)
            if not bars:
                logger.warning(f"No bars found for {right} {strike} ({expiration_date_str}); "
                               f"skipping its {len(records)} record(s)")
                continue

            for record in records:
                expected_profit = float(record['expected_profit'])
                if math.isnan(expected_profit):
                    logger.error(f"Could not find expected profits for {right} {strike}")
                    continue

                record_start_time = self._parse_record_time(record['datetime'])
                # bar.date is tz-aware in TWS's timezone (US/Central) while record_start_time
                # is NY-localized; comparing aware datetimes accounts for the offset.
                relevant_highs = [bar.high for bar in bars if bar.date >= record_start_time]
                if not relevant_highs:
                    relevant_highs = [bars[-1].high]
                max_high = max(relevant_highs)

                estimated_sell_price = float(record['estimated_sell_price'])
                stop_loss = float(record['stop_loss'])
                hit_stop_loss = max_high >= stop_loss
                realized_profit = estimated_sell_price - stop_loss if hit_stop_loss else estimated_sell_price
                if realized_profit < 0:
                    logger.info(f"Negative realized profit for {right} {strike}: {realized_profit}, max high: {max_high:.2f}, stop loss: {stop_loss}")

                record['max_high'] = max_high
                record['hit_stop_loss'] = hit_stop_loss
                record['realized_profit'] = realized_profit

                expected_profits.append(expected_profit)
                realized_profits.append(realized_profit)

                probability = float(record['probability'])
                bucket_index = bisect.bisect_right(BUCKET_LOWER_BOUNDS, probability) - 1
                if bucket_index >= 0:
                    bucket_record_counts[bucket_index] += 1
                    if hit_stop_loss:
                        bucket_hit_counts[bucket_index] += 1
                    # Same window is_early_closing_hours() checks, applied to when the record was sampled.
                    if is_early_closing_time(record_start_time) or is_early_trade(record_start_time):
                        early_closing_bucket_record_counts[bucket_index] += 1
                        if hit_stop_loss:
                            early_closing_bucket_hit_counts[bucket_index] += 1

        if not realized_profits:
            logger.error(f"Could not find any realized profits for {expiration_date_str}")
            return False

        average_realized_profit = round(sum(realized_profits) / len(realized_profits), 3)
        average_expected_profit = round(sum(expected_profits) / len(expected_profits), 3)
        profit_deviation = round(average_realized_profit - average_expected_profit, 3)
        self._log_averages(expiration_date_str, average_expected_profit, average_realized_profit, profit_deviation)
        self._log_buckets(PROBABILITY_BUCKETS_CSV_PATH, expiration_date_str, bucket_record_counts, bucket_hit_counts)
        self._log_buckets(EARLY_TRADE_PROBABILITY_BUCKETS_CSV_PATH, expiration_date_str,
                          early_closing_bucket_record_counts, early_closing_bucket_hit_counts)

        logger.info(f"PredictionsEvaluator: Processed {csv_path} into {len(grouped)} strike/right group(s); "
                    f"average expected profit over {len(expected_profits)} record(s) = {average_expected_profit:.4f}; "
                    f"average realized profit = {average_realized_profit:.4f}")
        return True

    @staticmethod
    def _parse_record_time(datetime_str):
        """The record's sampling time, NY-localized. A naive value (e.g.
        2026-10-15T20:00:14.862913) is UTC, so it's tagged as UTC before converting --
        never read as NY wall-clock time."""
        record_time = datetime.fromisoformat(datetime_str)
        if record_time.tzinfo is None:
            record_time = record_time.replace(tzinfo=timezone.utc)
        return record_time.astimezone(new_york_timezone)

    @staticmethod
    def _log_averages(expiration_date_str, average_expected_profit, average_realized_profit, profit_deviation):
        write_header = not os.path.exists(PREDICTION_AVERAGES_CSV_PATH)
        with open(PREDICTION_AVERAGES_CSV_PATH, 'a', newline='') as f:
            writer = csv.writer(f, delimiter='\t')
            if write_header:
                writer.writerow(['expiration_date', 'average_expected_profit', 'average_realized_profit',
                                  'profit_deviation'])
            writer.writerow([expiration_date_str, average_expected_profit, average_realized_profit,
                              profit_deviation])

    @staticmethod
    def _log_buckets(log_file, expiration_date_str, bucket_record_counts, bucket_hit_counts):
        """One row per BUCKET_LOWER_BOUNDS bucket: how many records fell in it, and the
        fraction of those that didn't hit the stop loss (blank if the bucket is empty)."""
        write_header = not os.path.exists(log_file)
        with open(log_file, 'a', newline='') as f:
            writer = csv.writer(f, delimiter='\t')
            if write_header:
                writer.writerow(['expiration_date', 'bucket_lower_bound', 'record_count',
                                  'not_hit_stop_loss_probability'])
            for lower_bound, record_count, hit_count in zip(BUCKET_LOWER_BOUNDS, bucket_record_counts,
                                                            bucket_hit_counts):
                not_hit_probability = round((record_count - hit_count) / record_count, 3) if record_count else ''
                writer.writerow([expiration_date_str, lower_bound, record_count, not_hit_probability])

        PredictionsEvaluator._log_aggregated_buckets(log_file, expiration_date_str)

    @staticmethod
    def _log_aggregated_buckets(log_file, expiration_date_str):
        """Appends one row to aggregated_<log_file>: each bucket's not-hit-stop-loss
        probability pooled over every expiration logged in log_file so far (blank if
        the bucket has no records yet)."""
        logger.info(f"Writing aggregated buckets to {log_f:qile}...")
        total_record_counts = [0] * len(BUCKET_LOWER_BOUNDS)
        total_not_hit_counts = [0] * len(BUCKET_LOWER_BOUNDS)
        with open(log_file, 'r', newline='') as f:
            for row in csv.DictReader(f, delimiter='\t'):
                record_count = int(row['record_count'])
                if not record_count:
                    continue
                bucket_index = BUCKET_LOWER_BOUNDS.index(float(row['bucket_lower_bound']))
                total_record_counts[bucket_index] += record_count
                # The probability was rounded to 3 decimals, so rounding back recovers the exact count.
                total_not_hit_counts[bucket_index] += round(float(row['not_hit_stop_loss_probability']) * record_count)

        aggregated_log_file = os.path.join(os.path.dirname(log_file), f"aggregated_{os.path.basename(log_file)}")
        write_header = not os.path.exists(aggregated_log_file)
        with open(aggregated_log_file, 'a', newline='') as f:
            writer = csv.writer(f, delimiter='\t')
            if write_header:
                upper_bounds = BUCKET_LOWER_BOUNDS[1:] + [1.0]
                writer.writerow(['results_date'] + [f"{round(lower * 100)}-{round(upper * 100)}%"
                                                    for lower, upper in zip(BUCKET_LOWER_BOUNDS, upper_bounds)])
            writer.writerow([expiration_date_str] + [
                round(not_hit_count / record_count, 3) if record_count else ''
                for record_count, not_hit_count in zip(total_record_counts, total_not_hit_counts)
            ])
