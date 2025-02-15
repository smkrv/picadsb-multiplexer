#!/bin/bash

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

# Set device permissions
chmod 666 ${ADSB_DEVICE}

# Start the multiplexer
exec python3 -u picadsb-multiplexer.py \
    --port ${ADSB_TCP_PORT} \
    --device ${ADSB_DEVICE} \
    --log ${ADSB_LOG_LEVEL} \
    $([ "$ADSB_NO_INIT" = "true" ] && echo "--no-init")
