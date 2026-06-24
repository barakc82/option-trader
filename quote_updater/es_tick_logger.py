import asyncio
import datetime
import os
import numpy as np
import pandas as pd
from ib_insync import IB, Future

from utilities.utils import new_york_timezone
from utilities.ib_utils import connect

ES_TICK_LOGGER_CLIENT_ID = 5

class ESTickLogger:
    def __init__(self):
        self.ib = None
        self.tick_buffer = []
        self.es_future = None
        self.last_logged_second = None

    def setup_contracts(self):
        """Connects to IBKR and isolates the front-month ES Future."""
        print("Connecting to IBKR...")
        print("Connecting to IB Gateway...")
        tws_connection = connect(ES_TICK_LOGGER_CLIENT_ID)
        self.ib = tws_connection.ib

        # Request all ES futures and find the front-month
        es_incomplete = Future('ES', 'CME')
        future_details = self.ib.reqContractDetails(es_incomplete)

        today_str = datetime.datetime.now(new_york_timezone).strftime('%Y%m%d')
        futures = [d.contract for d in future_details  if d.contract.lastTradeDateOrContractMonth >= today_str]
        futures.sort(key=lambda c: c.lastTradeDateOrContractMonth)

        self.es_future = futures[0]
        print(f"Selected ES future: {self.es_future.lastTradeDateOrContractMonth}")
        self.ib.qualifyContracts(self.es_future)

        print(f"Locked onto ES Future: {self.es_future.localSymbol}")

    def start_data_streams(self):
        """Requests live market data and binds the event handler."""
        # Request standard market data for the future
        self.ib.reqMktData(self.es_future, '', False, False)
        self.ib.pendingTickersEvent += self.on_pending_tickers

    def on_pending_tickers(self, tickers):
        """Triggered automatically, but now throttled to 1 snapshot per second."""
        for ticker in tickers:
            if ticker.contract == self.es_future:
                # Ensure the data is valid before buffering
                if ticker.bid != ticker.bid or ticker.ask != ticker.ask:
                    continue

                # Check the current time, dropping the microseconds
                now = datetime.datetime.now()
                current_second = now.replace(microsecond=0)

                # If we have already logged a tick during this exact second, skip it
                if self.last_logged_second == current_second:
                    continue

                # Otherwise, update the tracker and log the snapshot
                self.last_logged_second = current_second

                self.tick_buffer.append({
                    'timestamp': current_second,  # Clean 1-second timestamp
                    'bid': ticker.bid,
                    'bid_size': ticker.bidSize,
                    'ask': ticker.ask,
                    'ask_size': ticker.askSize,
                    'volume': ticker.volume
                })

    async def buffer_flush_loop(self, interval_seconds=300):
        """Asynchronously writes data to disk every 5 minutes."""
        while True:
            await asyncio.sleep(interval_seconds)

            if not self.tick_buffer:
                continue

            # Determine session tag (ETH vs RTH) using NY time
            now_ny = datetime.datetime.now(new_york_timezone)
            is_rth = datetime.time(9, 30) <= now_ny.time() <= datetime.time(16, 15)
            session_tag = "RTH" if is_rth else "ETH"

            date_str = now_ny.strftime('%Y%m%d')
            filename = f"raw_data/ES_Ticks_{date_str}_{session_tag}.parquet"

            print(f"[{now_ny.strftime('%H:%M:%S')}] Flushing {len(self.tick_buffer)} ticks to {filename}...")

            # Convert buffer to DataFrame and clear memory instantly
            df = pd.DataFrame(self.tick_buffer)
            self.tick_buffer.clear()

            # Append to Parquet file
            if os.path.exists(filename):
                existing_df = pd.read_parquet(filename)
                combined_df = pd.concat([existing_df, df], ignore_index=True)
                combined_df.to_parquet(filename, engine='fastparquet')
            else:
                os.makedirs("raw_data", exist_ok=True)
                df.to_parquet(filename, engine='fastparquet')

    async def data_processing_loop(self):
        """Looks for finished raw data files and processes them."""
        raw_dir = "raw_data"
        processed_dir = "processed_data"
        
        while True:
            # Check every 15 minutes
            await asyncio.sleep(900)
            
            if not os.path.exists(raw_dir):
                continue
                
            os.makedirs(processed_dir, exist_ok=True)

            # Determine the current raw file to avoid processing it while it's active
            now_ny = datetime.datetime.now(new_york_timezone)
            is_rth = datetime.time(9, 30) <= now_ny.time() <= datetime.time(16, 15)
            session_tag = "RTH" if is_rth else "ETH"
            date_str = now_ny.strftime('%Y%m%d')
            current_raw_file = f"ES_Ticks_{date_str}_{session_tag}.parquet"

            try:
                for file in os.listdir(raw_dir):
                    if file.endswith(".parquet") and file != current_raw_file:
                        raw_path = os.path.join(raw_dir, file)
                        processed_path = os.path.join(processed_dir, file)

                        if not os.path.exists(processed_path):
                            print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] Processing {file}...")
                            self.process_file(raw_path, processed_path)
            except Exception as e:
                print(f"Error in data_processing_loop: {e}")

    def process_file(self, raw_path, processed_path):
        """Processes a raw tick file to add velocity/acceleration and filter gaps."""
        try:
            df = pd.read_parquet(raw_path)
            if df.empty:
                return

            # Ensure sorted and unique timestamps
            df = df.sort_values('timestamp').drop_duplicates('timestamp')
            
            # Mid price for calculations
            df['mid_price'] = (df['bid'] + df['ask']) / 2

            # gained_10_in_30s: True if mid_price rises >= 10 at any point in the next 30 seconds.
            # shift(-1) skips the current row; reversed rolling max then reversed back gives the
            # forward-looking max over the next 30 rows (= 30s at 1 record/second).
            future_max_30s = df['mid_price'].shift(-1)[::-1].rolling(window=30, min_periods=1).max()[::-1]
            df['gained_10_in_30s'] = (future_max_30s - df['mid_price']) >= 10

            # velocity_3s = price change over 3 seconds
            df['velocity_3s'] = df['mid_price'].diff(3)
            
            # acceleration_5s = change in 1s velocity over a 4s interval (5 rows total)
            # This is (p[t] - p[t-1]) - (p[t-3] - p[t-4])
            df['acceleration_5s'] = df['mid_price'].diff(1).diff(3)
            
            # Filter for consecutive rows: We need 5 rows with 1s gaps to calculate acceleration_5s
            # diff() of timestamp should be 1s for the last 4 intervals.
            df['time_diff'] = df['timestamp'].diff().dt.total_seconds()
            df['is_consecutive'] = (df['time_diff'] == 1.0)
            # rolling(4) sum == 4 means this row and the previous 4 are consecutive (5 rows total)
            df['consecutive_block'] = df['is_consecutive'].rolling(4).sum() == 4
            
            # Keep only the rows where the columns were successfully calculated within a consecutive block
            df_processed = df[df['consecutive_block'] == True].copy()
            
            if df_processed.empty:
                print(f"Warning: No valid consecutive 5-second blocks found in {raw_path}")
                return

            # Cleanup temporary columns
            cols_to_drop = ['time_diff', 'is_consecutive', 'consecutive_block']
            df_processed.drop(columns=[c for c in cols_to_drop if c in df_processed.columns], inplace=True)
            
            # Save processed data
            df_processed.to_parquet(processed_path, engine='fastparquet')
            print(f"Successfully processed {len(df_processed)} rows from {raw_path}")
            
        except Exception as e:
            print(f"Failed to process {raw_path}: {e}")

    def run(self):
        """Starts the event loop with reconnection logic."""
        # Schedule the background tasks
        loop = asyncio.get_event_loop()
        loop.create_task(self.buffer_flush_loop(interval_seconds=300))
        loop.create_task(self.data_processing_loop())

        print("Tick Logger Live. Streaming data...")
        
        try:
            while True:
                try:
                    if self.ib is None or not self.ib.isConnected():
                        if self.ib:
                            print("Disconnected from IB. Attempting to reconnect...")
                            self.ib.disconnect()
                        
                        self.setup_contracts()
                        self.start_data_streams()
                        print("Reconnected and re-subscribed.")
                    
                    self.ib.waitOnUpdate(timeout=1.0)
                    
                except (ConnectionError, BrokenPipeError, ConnectionRefusedError) as e:
                    print(f"Connection issue: {e}. Reconnecting in 10 seconds...")
                    import time
                    time.sleep(10)
                except Exception as e:
                    print(f"Unexpected error in main loop: {e}. Continuing...")
                    import time
                    time.sleep(1)

        except KeyboardInterrupt:
            print("\nShutting down. Executing emergency flush...")
            if self.tick_buffer:
                df = pd.DataFrame(self.tick_buffer)
                df.to_parquet("emergency_flush.parquet", engine='fastparquet')
            if self.ib:
                self.ib.disconnect()


# Execution
if __name__ == "__main__":
    logger = ESTickLogger()
    logger.run()