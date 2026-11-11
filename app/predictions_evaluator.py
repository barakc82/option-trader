import asyncio
import bisect
import csv
import glob
import logging
import math
import os
from datetime import datetime, timezone

import numpy as np

from utilities.utils import new_york_timezone, REGULAR_HOURS_END_TIME, is_early_closing_time, is_early_trade, \
    is_first_five_minutes_of_closing_time, ISRAEL_TIMEZONE, PREDICTION_EVALUATION_DIR
from .machine_learning.survival_scoring import minutes_to_expiration_multiplier
from .market_data_fetcher import MarketDataFetcher

logger = logging.getLogger(__name__)

PREDICTIONS_CSV_PATTERN = f"{PREDICTION_EVALUATION_DIR}/predictions_*.csv"
PREDICTION_AVERAGES_CSV_PATH = f"{PREDICTION_EVALUATION_DIR}/prediction_averages.csv"
PERCENTAGE_BUCKETS_DIR = f"{PREDICTION_EVALUATION_DIR}/percentage_buckets"
PROBABILITY_BUCKETS_CSV_PATH = f"{PERCENTAGE_BUCKETS_DIR}/all/probability_buckets.csv"
EARLY_TRADE_PROBABILITY_BUCKETS_CSV_PATH = f"{PERCENTAGE_BUCKETS_DIR}/early_trade/probability_buckets.csv"
VERY_EARLY_TRADE_PROBABILITY_BUCKETS_CSV_PATH = f"{PERCENTAGE_BUCKETS_DIR}/very_early_trade/probability_buckets.csv"
QUANTILES_DIR = f"{PREDICTION_EVALUATION_DIR}/quantiles"
HISTOGRAM_CSV_PATH = f"{QUANTILES_DIR}/all/histogram.csv"
QUANTILES_CSV_PATH = f"{QUANTILES_DIR}/all/quantiles.csv"
EARLY_TRADE_HISTOGRAM_CSV_PATH = f"{QUANTILES_DIR}/early_trade/histogram.csv"
EARLY_TRADE_QUANTILES_CSV_PATH = f"{QUANTILES_DIR}/early_trade/quantiles.csv"
VERY_EARLY_TRADE_HISTOGRAM_CSV_PATH = f"{QUANTILES_DIR}/very_early_trade/histogram.csv"
VERY_EARLY_TRADE_QUANTILES_CSV_PATH = f"{QUANTILES_DIR}/very_early_trade/quantiles.csv"
# Fixed-resolution probability histograms kept in the *HISTOGRAM_CSV_PATH files: one bin per
# 0.01 percentage point from 0.00% to 100.00% (HISTOGRAM_BINS bins, bin i = probability i / 10000).
HISTOGRAM_RESOLUTION = 10000
HISTOGRAM_BINS = HISTOGRAM_RESOLUTION + 1
HISTOGRAM_COLUMNS = ['probability_pct', 'prediction_count', 'survival_count', 'stop_loss_hit_count', 'weight_sum']
# After each histogram update, the matching *QUANTILES_CSV_PATH file gets one row per
# equal-count quantile of all predictions so far in that histogram.
NUMBER_OF_QUANTILES = 3
QUANTILES_COLUMNS = ['results_date', 'bucket', 'min_probability', 'max_probability', 'mean_predicted',
                     'survival_rate', 'hits', 'samples']
PROCESS_LOOP_INTERVAL_SECONDS = 300
BUCKET_LOWER_BOUNDS = [0.6, 0.7, 0.8, 0.9]

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
        histogram = self._empty_histogram()
        early_trade_histogram = self._empty_histogram()
        very_early_trade_histogram = self._empty_histogram()
        early_closing_bucket_record_counts = [0] * len(BUCKET_LOWER_BOUNDS)
        early_closing_bucket_hit_counts = [0] * len(BUCKET_LOWER_BOUNDS)
        very_early_closing_bucket_record_counts = [0] * len(BUCKET_LOWER_BOUNDS)
        very_early_closing_bucket_hit_counts = [0] * len(BUCKET_LOWER_BOUNDS)
        for option_index, (key, records) in enumerate(grouped.items(), start=1):
            strike = float(records[0]['strike'])
            right = records[0]['right']
            logger.info(f"Processing the predictions for {right} {strike} "
                        f"({option_index}/{len(grouped)}, {option_index / len(grouped):.0%})")
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
                highest_bar = max(
                    (bar for bar in bars if bar.date >= record_start_time),
                    key=lambda bar: bar.high,
                    default=None,
                )
                if not highest_bar:
                    highest_bar = bars[-1]
                max_high = highest_bar.high

                estimated_sell_price = float(record['estimated_sell_price'])
                stop_loss = float(record['stop_loss'])
                hit_stop_loss = max_high >= stop_loss
                realized_profit = estimated_sell_price - stop_loss if hit_stop_loss else estimated_sell_price
                if realized_profit < 0:
                    israel_time = record['israel_time']
                    hit_time = highest_bar.date
                    hit_israel_time = hit_time.astimezone(ISRAEL_TIMEZONE).strftime('%d/%m/%Y %H:%M')
                    logger.info(f"Negative realized profit for {right} {strike}: {realized_profit:.2f}, "
                                f"max high: {max_high:.2f}, stop loss: {stop_loss}, "
                                f"record start time (Israel): {israel_time}, hit time (Israel): {hit_israel_time}")

                record['max_high'] = max_high
                record['hit_stop_loss'] = hit_stop_loss
                record['realized_profit'] = realized_profit

                expected_profits.append(expected_profit)
                realized_profits.append(realized_profit)

                probability = float(record['probability'])
                # Same window is_early_closing_hours() checks, applied to when the record was sampled.
                is_early_trade_record = is_early_closing_time(record_start_time) or is_early_trade(record_start_time)
                is_very_early_trade_record = is_first_five_minutes_of_closing_time(record_start_time)

                histogram_args = (probability, hit_stop_loss, estimated_sell_price, stop_loss,
                                  int(record['minutes_to_expiration']))
                self._add_to_histogram(histogram, *histogram_args)
                if is_early_trade_record:
                    self._add_to_histogram(early_trade_histogram, *histogram_args)
                if is_very_early_trade_record:
                    self._add_to_histogram(very_early_trade_histogram, *histogram_args)

                bucket_index = bisect.bisect_right(BUCKET_LOWER_BOUNDS, probability) - 1
                if bucket_index >= 0:
                    bucket_record_counts[bucket_index] += 1
                    if hit_stop_loss:
                        bucket_hit_counts[bucket_index] += 1
                    if is_early_trade_record:
                        early_closing_bucket_record_counts[bucket_index] += 1
                        if hit_stop_loss:
                            early_closing_bucket_hit_counts[bucket_index] += 1
                    if is_very_early_trade_record:
                        very_early_closing_bucket_record_counts[bucket_index] += 1
                        if hit_stop_loss:
                            very_early_closing_bucket_hit_counts[bucket_index] += 1

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
        self._log_buckets(VERY_EARLY_TRADE_PROBABILITY_BUCKETS_CSV_PATH, expiration_date_str,
                          very_early_closing_bucket_record_counts, very_early_closing_bucket_hit_counts)
        self._update_probability_histogram(HISTOGRAM_CSV_PATH, QUANTILES_CSV_PATH, histogram, expiration_date_str)
        self._update_probability_histogram(EARLY_TRADE_HISTOGRAM_CSV_PATH, EARLY_TRADE_QUANTILES_CSV_PATH,
                                           early_trade_histogram, expiration_date_str)
        self._update_probability_histogram(VERY_EARLY_TRADE_HISTOGRAM_CSV_PATH, VERY_EARLY_TRADE_QUANTILES_CSV_PATH,
                                           very_early_trade_histogram, expiration_date_str)

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
    def _empty_histogram():
        return {
            'prediction_count': np.zeros(HISTOGRAM_BINS, dtype=np.int64),
            'survival_count': np.zeros(HISTOGRAM_BINS, dtype=np.int64),
            'stop_loss_hit_count': np.zeros(HISTOGRAM_BINS, dtype=np.int64),
            'weight_sum': np.zeros(HISTOGRAM_BINS, dtype=float),
        }

    @staticmethod
    def _add_to_histogram(histogram, probability, hit_stop_loss, estimated_sell_price, stop_loss,
                          minutes_to_expiration):
        """Adds one evaluated record to its 0.01-percentage-point bin. The sample weight is
        the one model selection scores with (survival_scoring.trade_weight): the room between
        the stop and the credit, times the minutes-to-expiration range multiplier."""
        if math.isnan(probability):
            return
        bin_index = min(max(round(probability * HISTOGRAM_RESOLUTION), 0), HISTOGRAM_RESOLUTION)
        histogram['prediction_count'][bin_index] += 1
        if hit_stop_loss:
            histogram['stop_loss_hit_count'][bin_index] += 1
        else:
            histogram['survival_count'][bin_index] += 1
        weight = (stop_loss - estimated_sell_price) * minutes_to_expiration_multiplier([minutes_to_expiration])[0]
        histogram['weight_sum'][bin_index] += weight

    @staticmethod
    def _update_probability_histogram(histogram_file, quantiles_file, histogram, expiration_date_str):
        """Adds this file's histogram to the running one in histogram_file (all
        HISTOGRAM_BINS rows, written to a temporary file first and then swapped in, so a
        crash mid-write can't leave a partial histogram), then logs the running histogram's
        quantiles to quantiles_file."""
        totals = PredictionsEvaluator._empty_histogram()
        if os.path.exists(histogram_file):
            with open(histogram_file, 'r', newline='') as f:
                for bin_index, row in enumerate(csv.DictReader(f, delimiter='\t')):
                    for column in ('prediction_count', 'survival_count', 'stop_loss_hit_count'):
                        totals[column][bin_index] = int(row[column])
                    totals['weight_sum'][bin_index] = float(row['weight_sum'])
        for column in totals:
            totals[column] += histogram[column]

        os.makedirs(os.path.dirname(histogram_file), exist_ok=True)
        temporary_path = f"{histogram_file}.tmp"
        with open(temporary_path, 'w', newline='') as f:
            writer = csv.writer(f, delimiter='\t')
            writer.writerow(HISTOGRAM_COLUMNS)
            for bin_index in range(HISTOGRAM_BINS):
                writer.writerow([f"{bin_index * 100 / HISTOGRAM_RESOLUTION:.2f}",
                                 totals['prediction_count'][bin_index], totals['survival_count'][bin_index],
                                 totals['stop_loss_hit_count'][bin_index], round(totals['weight_sum'][bin_index], 4)])
        os.replace(temporary_path, histogram_file)
        logger.info(f"Updated the probability histogram in {histogram_file}: "
                    f"{int(histogram['prediction_count'].sum())} new prediction(s), "
                    f"{int(totals['prediction_count'].sum())} in total")

        PredictionsEvaluator._log_quantiles(quantiles_file, totals, expiration_date_str)

    @staticmethod
    def _log_quantiles(quantiles_file, totals, expiration_date_str):
        """Splits all predictions in the running histogram, ordered by predicted
        probability, into NUMBER_OF_QUANTILES groups of (as near as possible) equal size and
        appends one row per group to quantiles_file: its probability range, mean
        predicted probability, observed survival rate, stop-loss hits and sample count.
        Bins are never split, so a group holds every prediction of each bin it covers; a bin
        goes to the group in which its first prediction falls."""
        counts = totals['prediction_count']
        total_count = int(counts.sum())
        if not total_count:
            return

        probabilities = np.arange(HISTOGRAM_BINS) / HISTOGRAM_RESOLUTION
        predictions_before_bin = np.cumsum(counts) - counts
        group_of_bin = np.minimum(predictions_before_bin * NUMBER_OF_QUANTILES // total_count, NUMBER_OF_QUANTILES - 1)

        os.makedirs(os.path.dirname(quantiles_file), exist_ok=True)
        write_header = not os.path.exists(quantiles_file)
        with open(quantiles_file, 'a', newline='') as f:
            writer = csv.writer(f, delimiter='\t')
            if write_header:
                writer.writerow(QUANTILES_COLUMNS)
            for group in range(NUMBER_OF_QUANTILES):
                in_group = (group_of_bin == group) & (counts > 0)
                samples = int(counts[in_group].sum())
                if not samples:
                    # Too few distinct bins to fill every group (e.g. very few predictions so far).
                    writer.writerow([expiration_date_str, group + 1, '', '', '', '', 0, 0])
                    continue
                group_probabilities = probabilities[in_group]
                mean_predicted = float((group_probabilities * counts[in_group]).sum() / samples)
                survival_rate = float(totals['survival_count'][in_group].sum() / samples)
                hits = int(totals['stop_loss_hit_count'][in_group].sum())
                writer.writerow([expiration_date_str, group + 1, f"{group_probabilities.min():.4f}",
                                 f"{group_probabilities.max():.4f}", f"{mean_predicted:.4f}",
                                 f"{survival_rate:.4f}", hits, samples])
        logger.info(f"Logged {NUMBER_OF_QUANTILES} quantile(s) of {total_count} prediction(s) to {quantiles_file}")

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
        logger.info(f"Writing aggregated buckets to {log_file}...")
        total_record_counts = [0] * len(BUCKET_LOWER_BOUNDS)
        total_not_hit_counts = [0] * len(BUCKET_LOWER_BOUNDS)
        skipped_old_layout_rows = 0
        with open(log_file, 'r', newline='') as f:
            for row in csv.DictReader(f, delimiter='\t'):
                record_count = int(row['record_count'])
                if not record_count:
                    continue
                lower_bound = float(row['bucket_lower_bound'])
                if lower_bound not in BUCKET_LOWER_BOUNDS:
                    # Written with an earlier bucket layout; it can't be pooled into the current buckets.
                    skipped_old_layout_rows += 1
                    continue
                bucket_index = BUCKET_LOWER_BOUNDS.index(lower_bound)
                total_record_counts[bucket_index] += record_count
                # The probability was rounded to 3 decimals, so rounding back recovers the exact count.
                total_not_hit_counts[bucket_index] += round(float(row['not_hit_stop_loss_probability']) * record_count)
        if skipped_old_layout_rows:
            logger.warning(f"Skipped {skipped_old_layout_rows} row(s) of {log_file} whose bucket lower bound isn't one "
                           f"of {BUCKET_LOWER_BOUNDS} (written with an earlier bucket layout)")

        upper_bounds = BUCKET_LOWER_BOUNDS[1:] + [1.0]
        not_hit_probabilities = [
            round(not_hit_count / record_count, 3) if record_count else ''
            for record_count, not_hit_count in zip(total_record_counts, total_not_hit_counts)
        ]
        # How far each bucket's observed not-hit probability falls outside the bucket's own
        # [lower, upper] range (0 when inside it), summed over the non-empty buckets.
        distance = 0.0
        for lower, upper, probability in zip(BUCKET_LOWER_BOUNDS, upper_bounds, not_hit_probabilities):
            if probability == '':
                continue
            if probability < lower:
                distance += lower - probability
            elif probability > upper:
                distance += probability - upper

        aggregated_log_file = os.path.join(os.path.dirname(log_file), f"aggregated_{os.path.basename(log_file)}")
        write_header = not os.path.exists(aggregated_log_file)
        with open(aggregated_log_file, 'a', newline='') as f:
            writer = csv.writer(f, delimiter='\t')
            if write_header:
                writer.writerow(['results_date'] + [f"{round(lower * 100)}-{round(upper * 100)}%"
                                                    for lower, upper in zip(BUCKET_LOWER_BOUNDS, upper_bounds)]
                                + ['distance'])
            writer.writerow([expiration_date_str] + not_hit_probabilities + [round(distance, 3)])
