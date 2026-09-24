"""Reads the first sheet of a Google spreadsheet listing foreign vocabulary
and generates a CSV of (foreign word, meaning) pairs.

Expected spreadsheet layout, one row per foreign word:
    word, meaning_1, count_1, meaning_2, count_2, ...
Each (meaning, count) pair after the word is optional -- a pair whose count
cell is empty is ignored. A pair is only written to the output CSV if its
count is a number >= 2 (i.e. that meaning occurred at least twice in the
source text).

Requires resources/service_account.json (see utilities/database_access.py)
to have viewer access to the target spreadsheet.
"""
import csv
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from utilities.database_access import get_client

SPREADSHEET_ID = "1edPqPRZ_IvkZjHnkJS2baiYjAEQcYpQjpvW_mH67h20"
OUTPUT_CSV = Path(__file__).resolve().parent / "vocabulary.csv"
MIN_OCCURRENCES = 2


def parse_count(count_str: str) -> int | None:
    """Returns the occurrence count as an int, or None if the cell is blank
    or not a parseable number (e.g. a header row)."""
    count_str = count_str.strip()
    if not count_str:
        return None
    try:
        return int(float(count_str))
    except ValueError:
        return None


def extract_pairs(row: list[str]) -> list[tuple[str, str]]:
    """From one spreadsheet row, returns the (word, meaning) pairs whose
    occurrence count is present and >= MIN_OCCURRENCES."""
    if not row or not row[0].strip():
        return []

    word = row[0].strip()
    rest = row[1:]

    pairs = []
    for i in range(0, len(rest), 2):
        meaning = rest[i].strip()
        count_str = rest[i + 1] if i + 1 < len(rest) else ""
        count = parse_count(count_str)
        if count is not None and count >= MIN_OCCURRENCES:
            pairs.append((word, meaning))
    return pairs


def fetch_rows() -> list[list[str]]:
    client = get_client()
    worksheet = client.open_by_key(SPREADSHEET_ID).sheet1
    return worksheet.get_all_values()


def main():
    rows = fetch_rows()

    output_pairs = []
    for row in rows:
        output_pairs.extend(extract_pairs(row))

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerows(output_pairs)

    print(f"Wrote {len(output_pairs)} (word, meaning) pairs to {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
