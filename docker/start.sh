#!/bin/bash

# Start the IB Gateway in the background
# The default startup script in the gnzsnz image is /opt/ibgateway/run.sh
#/home/ibgateway/scripts/run.sh 2>&1 | tee gateway.log &

IBG_LOG_FILE="/home/ibgateway/ibgateway.log"
DOCKER_START_LOG_FILE="/home/option-trader/docker_start.log"
echo "$(date): Starting..." | tee "$DOCKER_START_LOG_FILE"

# Replace this with the actual command to launch your IB Gateway/process.
# Example using an IBC wrapper:
IBG_COMMAND="/home/ibgateway/scripts/run.sh"

echo "$(date): cp /usr/bin/telnet /usr/local/bin" >> "$IBG_LOG_FILE"
cp /usr/bin/telnet /usr/local/bin

echo "$(date): sed -i 's/^CommandServerPort=.*/CommandServerPort=7462/' /home/ibgateway/ibc/config.ini" >> "$IBG_LOG_FILE"
sed -i 's/^CommandServerPort=.*/CommandServerPort=7462/' /home/ibgateway/ibc/config.ini
sed -i 's/^ReloginAfterSecondFactorAuthenticationTimeout=.*/ReloginAfterSecondFactorAuthenticationTimeout=yes/' /home/ibgateway/ibc/config.ini
sed -i 's/^CommandServerPort=.*/CommandServerPort=7462/' /home/ibgateway/ibc/config.ini.tmpl
sed -i 's/^ReloginAfterSecondFactorAuthenticationTimeout=.*/ReloginAfterSecondFactorAuthenticationTimeout=yes/' /home/ibgateway/ibc/config.ini.tmpl
sed -i 's/-Xmx[0-9]\+[mg]/-Xmx2g/' /home/ibgateway/Jts/ibgateway/*/ibgateway.vmoptions

echo "$(date): Starting IB Gateway..." >> "$DOCKER_START_LOG_FILE"

# Start the command, append output (>>), and redirect errors to output (2>&1)
echo "$(date): Starting IB Gateway..." >> "$IBG_LOG_FILE"
nohup $IBG_COMMAND >> "$IBG_LOG_FILE" 2>&1 &
echo "$(date): IB Gateway started with PID $! and logging to $IBG_LOG_FILE" | tee "$DOCKER_START_LOG_FILE"


# --- Wait for Gateway to be ready ---
# Adjust the port if you are using a different one (e.g., 4002 for paper trading)
PORT=4001
echo "Waiting for IB Gateway to listen on port $PORT..." >> "$DOCKER_START_LOG_FILE"

WAIT_TEXT="Login has completed"
until grep -q "$WAIT_TEXT" "$IBG_LOG_FILE"; do
  echo "Waiting for login to complete..." >> "$DOCKER_START_LOG_FILE"
  sleep 2
done

echo "" # Newline for cleaner logs
echo "IB Gateway is ready. Starting ib_insync script." >> "$DOCKER_START_LOG_FILE"

echo "$(date): Starting option trader supervisor..." >> "$DOCKER_START_LOG_FILE"
python3 /home/option-trader/app/options_trader_supervisor.py
echo "$(date): Option trader supervisor started"

# Wait for the Gateway process to exit. This keeps the container running.
#wait $GATEWAY_PID

# keep the container running
sleep infinity