#!/bin/bash

LOG_FILE="/home/ibgateway/ibgateway.log"
MAX_LINES=10000

# 1. Check if the log file exists and is not empty
if [ -s "$LOG_FILE" ]; then

    # 2. Get the last MAX_LINES and pipe them to a temporary file
    tail -n $MAX_LINES "$LOG_FILE" > "$LOG_FILE.tmp"

    # 3. Overwrite the original log file with the trimmed content
    mv "$LOG_FILE.tmp" "$LOG_FILE"

    echo "$(date): Trimmed $LOG_FILE to the last $MAX_LINES lines."
else
    echo "$(date): Log file $LOG_FILE not found or is empty. Skipping trim."
fi