#!/bin/bash

# Start the IB Gateway in the background
# The default startup script in the gnzsnz image is /opt/ibgateway/run.sh
#/home/ibgateway/scripts/run.sh 2>&1 | tee gateway.log &

LOG_FILE="/home/ibgateway/ibgateway.log"
# Replace this with the actual command to launch your IB Gateway/process.
# Example using an IBC wrapper:
IBG_COMMAND="/home/ibgateway/scripts/run.sh"

sed -i 's/^CommandServerPort=.*/CommandServerPort=7462/' /home/ibgateway/ibc/config.ini

# Start the command, append output (>>), and redirect errors to output (2>&1)
echo "$(date): Starting IB Gateway..." >> "$LOG_FILE"
nohup $IBG_COMMAND >> "$LOG_FILE" 2>&1 &
echo "$(date): IB Gateway started with PID $! and logging to $LOG_FILE"


# --- Wait for Gateway to be ready ---
# Adjust the port if you are using a different one (e.g., 4002 for paper trading)
PORT=4001
echo "Waiting for IB Gateway to listen on port $PORT..."

WAIT_TEXT="Login has completed"
until grep -q "$WAIT_TEXT" "$LOG_FILE"; do
  echo "⏳ Waiting for login to complete..."
  sleep 2
done

echo "" # Newline for cleaner logs
echo "✅ IB Gateway is ready. Starting ib_insync script."

# Run your Python script
# Make sure the path '/opt/scripts/my_script.py' matches where you copy it in the Dockerfile
python3 /home/option-trader/app/options_trader_supervisor.py

# Wait for the Gateway process to exit. This keeps the container running.
wait $GATEWAY_PID