#!/bin/bash

# Health check script for ADS-B multiplexer
# Performs basic operational checks

# Check if main process is running (using ps instead of pgrep)
if ! ps aux | grep "python3 -u picadsb-multiplexer.py" | grep -v grep > /dev/null; then
    echo "Process not running"
    exit 1
fi

# Check if serial device exists
if [ ! -e "${ADSB_DEVICE}" ]; then
    echo "Device ${ADSB_DEVICE} not found"
    exit 1
fi

# Check if TCP port is listening
if ! netstat -ln | grep -q ":${ADSB_TCP_PORT}"; then
    echo "Port ${ADSB_TCP_PORT} not listening"
    exit 1
fi

# Check application logs for errors
if [ -d "/app/logs" ]; then
    if grep -q "Health check failed" /app/logs/picadsb_*.log 2>/dev/null; then
        latest_error=$(grep "Health check failed" /app/logs/picadsb_*.log | tail -1)
        echo "Health check failure found: ${latest_error}"
        exit 1
    fi
fi

# All checks passed
exit 0
