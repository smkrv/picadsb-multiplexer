#!/bin/bash

# Set defaults if environment variables are not set
MAX_LOG_SIZE=${MAX_LOG_SIZE:-100M}
LOG_RETENTION_DAYS=${LOG_RETENTION_DAYS:-7}

# Wait for device to appear
timeout=30
counter=0
while [ ! -e "${ADSB_DEVICE}" ] && [ $counter -lt $timeout ]; do
    echo "Waiting for device ${ADSB_DEVICE}..."
    sleep 1
    counter=$((counter + 1))
done

if [ ! -e "${ADSB_DEVICE}" ]; then
    echo "Error: Device ${ADSB_DEVICE} not found after ${timeout} seconds"
    exit 1
fi

# Check if logs directory exists
if [ -d "/app/logs" ]; then
    # Get current size of logs directory in MB
    current_size=$(du -sm /app/logs | cut -f1)

    # Remove 'M' from MAX_LOG_SIZE for comparison
    max_size=${MAX_LOG_SIZE%M}

    if [ $current_size -gt $max_size ]; then
        echo "Log directory size ($current_size MB) exceeds limit ($max_size MB). Cleaning old logs..."
        find /app/logs -name "*.log" -type f -mtime +${LOG_RETENTION_DAYS} -delete
    fi
fi

# Set device permissions
chmod 666 ${ADSB_DEVICE}

# Start the multiplexer
exec python3 -u picadsb-multiplexer.py \
    --port ${ADSB_TCP_PORT} \
    --serial ${ADSB_DEVICE} \
    --log-level ${ADSB_LOG_LEVEL} \
    $([ "$ADSB_NO_INIT" = "true" ] && echo "--no-init")
