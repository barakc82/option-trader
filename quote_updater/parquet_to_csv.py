import pandas as pd
import argparse
import os


def convert_parquet_to_csv(target_date: str, session: str, type: str):
    """
    Loads a daily tick Parquet file and exports it as a readable text CSV.
    """
    # Enforce uppercase for the session tag
    session = session.upper()

    # Construct the exact filenames
    parquet_file = f"{type}_data/ES_Ticks_{target_date}_{session}.parquet"
    csv_file = f"ES_Ticks_{target_date}_{session}.csv"

    # Verify the file exists before attempting to load
    if not os.path.exists(parquet_file):
        print(f"Error: Could not find '{parquet_file}' in the current directory.")
        return

    print(f"Loading '{parquet_file}' into memory...")

    try:
        # With 64GB of RAM, loading even a massive daily Parquet file into memory is trivial
        df = pd.read_parquet(parquet_file)

        print(f"Successfully loaded {len(df):,} rows. Writing to CSV...")

        # Export to CSV. index=False prevents Pandas from adding a useless numbered column.
        df.to_csv(csv_file, index=False)

        print(f"Success! Data exported to '{csv_file}'.")

    except Exception as e:
        print(f"An error occurred during conversion: {e}")


if __name__ == "__main__":
    # Execute the conversion
    convert_parquet_to_csv(target_date='20260615', session='RTH', type='processed')