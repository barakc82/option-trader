import asyncio
import datetime
import os
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
        futures = [d.contract for d in future_details]
        futures.sort(key=lambda c: c.lastTradeDateOrContractMonth)

        self.es_future = futures[0]
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
                df.to_parquet(filename, engine='fastparquet')

    def run(self):
        """Starts the event loop."""
        self.setup_contracts()
        self.start_data_streams()

        # Schedule the background flusher
        loop = asyncio.get_event_loop()
        loop.create_task(self.buffer_flush_loop(interval_seconds=300))

        print("Tick Logger Live. Streaming data...")
        try:
            self.ib.run()
        except KeyboardInterrupt:
            print("\nShutting down. Executing emergency flush...")
            if self.tick_buffer:
                df = pd.DataFrame(self.tick_buffer)
                df.to_parquet("emergency_flush.parquet", engine='fastparquet')
            self.ib.disconnect()


# Execution
if __name__ == "__main__":
    logger = ESTickLogger()
    logger.run()