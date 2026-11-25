"""Rebuilds the prediction evaluation files of PredictionsEvaluator from the united
predictions file (ALL_PREDICTIONS_CSV) alone, under NEXT_GENERATION_DIR, with every result
split into calls and puts:

    prediction_averages.csv
        expiration_date, right, average_expected_profit, average_realized_profit,
        profit_deviation
    percentage_buckets/<window>/probability_buckets.csv
        expiration_date, right, bucket_lower_bound, record_count,
        not_hit_stop_loss_probability
    percentage_buckets/<window>/aggregated_probability_buckets.csv
        results_date, right, one column per bucket, distance: per right, each bucket's
        not-hit probability pooled over every expiration up to results_date
    quantiles/<window>/quantiles_<n>.csv (n in QUANTILE_DIVISIONS)
        results_date, right, bucket, ...: per right, the equal-count quantiles of every
        prediction up to results_date

The windows (all, early_trade, very_early_trade), buckets, quantile rule and number formats
are the evaluator's own; the histogram files are not written (the quantiles are computed
from in-memory histograms). Every expiration gets rows for both rights, except in the
quantile files, which skip a right until it has predictions.

Only rows that carry the evaluation result (HIT_STOP_LOSS_COLUMN, added when the evaluator
appends a processed file) are used; rows appended before that column existed, rows the
evaluator could not evaluate, and rows without a probability are skipped and counted.

The output files are overwritten, so the script can be rerun at any time.

Run from the repo root:
    .venv/Scripts/python.exe -m utilities.rebuild_prediction_evaluation
"""
import bisect
import csv
import math
import os
from collections import defaultdict

from app.predictions_evaluator import (
    ALL_PREDICTIONS_CSV, BUCKET_LOWER_BOUNDS, EXPIRATION_DATE_COLUMN, HIT_STOP_LOSS_COLUMN, QUANTILE_DIVISIONS,
    QUANTILES_COLUMNS, PredictionsEvaluator, aggregated_bucket_columns, aggregated_bucket_values,
)
from utilities.utils import (PREDICTION_EVALUATION_DIR, is_early_closing_time, is_early_trade,
                             is_first_five_minutes_of_closing_time)

NEXT_GENERATION_DIR = os.path.join(PREDICTION_EVALUATION_DIR, 'next_generation')
RIGHTS = ['C', 'P']
WINDOWS = ['all', 'early_trade', 'very_early_trade']


def load_evaluated_records(predictions_csv):
    """The rows of predictions_csv that can be rebuilt from, each as a dict with
    expiration_date, right, probability, hit_stop_loss, estimated_sell_price, stop_loss,
    expected_profit, minutes_to_expiration and the windows it belongs to."""
    records = []
    skipped = defaultdict(int)
    with open(predictions_csv, 'r', newline='') as f:
        for row in csv.DictReader(f, delimiter='\t'):
            hit_stop_loss = (row.get(HIT_STOP_LOSS_COLUMN) or '').strip()
            if hit_stop_loss not in ('True', 'False'):
                skipped['without an evaluation result'] += 1
                continue
            probability = float(row['probability'])
            if math.isnan(probability):
                skipped['without a probability'] += 1
                continue

            record_time = PredictionsEvaluator._parse_record_time(row['datetime'])
            windows = ['all']
            # Same windows as PredictionsEvaluator._process_file.
            if is_early_closing_time(record_time) or is_early_trade(record_time):
                windows.append('early_trade')
            if is_first_five_minutes_of_closing_time(record_time):
                windows.append('very_early_trade')

            records.append({
                'expiration_date': row[EXPIRATION_DATE_COLUMN],
                'right': row['right'],
                'probability': probability,
                'hit_stop_loss': hit_stop_loss == 'True',
                'estimated_sell_price': float(row['estimated_sell_price']),
                'stop_loss': float(row['stop_loss']),
                'expected_profit': float(row['expected_profit']),
                'minutes_to_expiration': int(row['minutes_to_expiration']),
                'windows': windows,
            })

    print(f"Read {len(records)} evaluated record(s) from {predictions_csv}")
    for reason, count in skipped.items():
        print(f"  skipped {count} row(s) {reason}")
    return records


def write_tsv(path, header, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', newline='') as f:
        writer = csv.writer(f, delimiter='\t')
        writer.writerow(header)
        writer.writerows(rows)
    print(f"Wrote {len(rows)} row(s) to {path}")


def build_prediction_averages(records, expiration_dates, output_dir):
    rows = []
    for expiration_date in expiration_dates:
        for right in RIGHTS:
            day_records = [r for r in records if r['expiration_date'] == expiration_date and r['right'] == right]
            if not day_records:
                continue
            realized_profits = [r['estimated_sell_price'] - r['stop_loss'] if r['hit_stop_loss']
                                else r['estimated_sell_price'] for r in day_records]
            average_realized_profit = round(sum(realized_profits) / len(realized_profits), 3)
            average_expected_profit = round(sum(r['expected_profit'] for r in day_records) / len(day_records), 3)
            rows.append([expiration_date, right, average_expected_profit, average_realized_profit,
                         round(average_realized_profit - average_expected_profit, 3)])
    write_tsv(os.path.join(output_dir, 'prediction_averages.csv'),
              ['expiration_date', 'right', 'average_expected_profit', 'average_realized_profit', 'profit_deviation'],
              rows)


def build_probability_buckets(window_records, expiration_dates, window_dir):
    """probability_buckets.csv and aggregated_probability_buckets.csv of one window."""
    bucket_rows = []
    aggregated_rows = []
    total_record_counts = {right: [0] * len(BUCKET_LOWER_BOUNDS) for right in RIGHTS}
    total_not_hit_counts = {right: [0] * len(BUCKET_LOWER_BOUNDS) for right in RIGHTS}
    for expiration_date in expiration_dates:
        for right in RIGHTS:
            record_counts = [0] * len(BUCKET_LOWER_BOUNDS)
            hit_counts = [0] * len(BUCKET_LOWER_BOUNDS)
            for record in window_records:
                if record['expiration_date'] != expiration_date or record['right'] != right:
                    continue
                bucket_index = bisect.bisect_right(BUCKET_LOWER_BOUNDS, record['probability']) - 1
                if bucket_index < 0:
                    continue
                record_counts[bucket_index] += 1
                if record['hit_stop_loss']:
                    hit_counts[bucket_index] += 1

            for bucket_index, (lower_bound, record_count, hit_count) in enumerate(
                    zip(BUCKET_LOWER_BOUNDS, record_counts, hit_counts)):
                not_hit_probability = round((record_count - hit_count) / record_count, 3) if record_count else ''
                bucket_rows.append([expiration_date, right, lower_bound, record_count, not_hit_probability])
                total_record_counts[right][bucket_index] += record_count
                total_not_hit_counts[right][bucket_index] += record_count - hit_count

            aggregated_rows.append([expiration_date, right] + aggregated_bucket_values(
                total_record_counts[right], total_not_hit_counts[right]))

    write_tsv(os.path.join(window_dir, 'probability_buckets.csv'),
              ['expiration_date', 'right', 'bucket_lower_bound', 'record_count', 'not_hit_stop_loss_probability'],
              bucket_rows)
    write_tsv(os.path.join(window_dir, 'aggregated_probability_buckets.csv'),
              ['results_date', 'right'] + aggregated_bucket_columns(), aggregated_rows)


def build_quantiles(window_records, expiration_dates, window_dir):
    """quantiles_<n>.csv of one window, for every n in QUANTILE_DIVISIONS."""
    histograms = {right: PredictionsEvaluator._empty_histogram() for right in RIGHTS}
    rows = {number_of_quantiles: [] for number_of_quantiles in QUANTILE_DIVISIONS}
    for expiration_date in expiration_dates:
        for right in RIGHTS:
            for record in window_records:
                if record['expiration_date'] == expiration_date and record['right'] == right:
                    PredictionsEvaluator._add_to_histogram(
                        histograms[right], record['probability'], record['hit_stop_loss'],
                        record['estimated_sell_price'], record['stop_loss'], record['minutes_to_expiration'])
            for number_of_quantiles in QUANTILE_DIVISIONS:
                for group_values in PredictionsEvaluator.quantile_values(number_of_quantiles, histograms[right]):
                    rows[number_of_quantiles].append([expiration_date, right] + group_values)

    header = QUANTILES_COLUMNS[:1] + ['right'] + QUANTILES_COLUMNS[1:]
    for number_of_quantiles, quantile_rows in rows.items():
        write_tsv(os.path.join(window_dir, f"quantiles_{number_of_quantiles}.csv"), header, quantile_rows)


def rebuild(predictions_csv=ALL_PREDICTIONS_CSV, output_dir=NEXT_GENERATION_DIR):
    records = load_evaluated_records(predictions_csv)
    if not records:
        print("No evaluated records, nothing to build")
        return
    expiration_dates = sorted({record['expiration_date'] for record in records})
    print(f"{len(expiration_dates)} expiration(s): {expiration_dates[0]} -> {expiration_dates[-1]}")

    build_prediction_averages(records, expiration_dates, output_dir)
    for window in WINDOWS:
        window_records = [record for record in records if window in record['windows']]
        build_probability_buckets(window_records, expiration_dates,
                                  os.path.join(output_dir, 'percentage_buckets', window))
        build_quantiles(window_records, expiration_dates, os.path.join(output_dir, 'quantiles', window))


if __name__ == '__main__':
    rebuild()
