"""Copies the first sheet of the same vocabulary spreadsheet
export_vocabulary_csv.py reads from into a CSV, decrementing every numeric
cell by 1. If a cell's decremented value is 1, the cell is cleared instead.
Non-numeric cells are copied through unchanged.

Requires resources/service_account.json (see utilities/database_access.py)
to have viewer access to the target spreadsheet.
"""
import csv
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from utilities.database_access import get_client
from utilities.languages.export_vocabulary_csv import SPREADSHEET_ID

OUTPUT_CSV = Path(__file__).resolve().parent / "vocabulary_decremented.csv"
CLEAR_IF_EQUAL_TO = 1


def parse_number(cell: str) -> int | float | None:
    """Returns the cell's numeric value (int if it's a whole number, else
    float), or None if the cell isn't a number."""
    cell = cell.strip()
    if not cell:
        return None
    try:
        value = float(cell)
    except ValueError:
        return None
    return int(value) if value.is_integer() else value


def transform_cell(cell: str) -> str:
    """Non-numeric cells pass through unchanged. Numeric cells are
    decremented by 1; if the result equals CLEAR_IF_EQUAL_TO, the cell is
    cleared instead of written."""
    value = parse_number(cell)
    if value is None:
        return cell
    decremented = value - 1
    if decremented == CLEAR_IF_EQUAL_TO:
        return ""
    return str(decremented)


def transform_row(row: list[str]) -> list[str]:
    return [transform_cell(cell) for cell in row]


def fetch_rows() -> list[list[str]]:
    client = get_client()
    worksheet = client.open_by_key(SPREADSHEET_ID).sheet1
    return worksheet.get_all_values()


def main():
    rows = fetch_rows()
    transformed_rows = [transform_row(row) for row in rows]

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerows(transformed_rows)

    print(f"Wrote {len(transformed_rows)} rows to {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
