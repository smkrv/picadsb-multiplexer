#!/bin/bash

# Health check script for ADS-B multiplexer

# Check if main process is running
if ! ps aux | grep "python3 -u picadsb-multiplexer.py" | grep -v grep > /dev/null; then
    echo "Process not running"
    exit 1
fi

# Check if serial device exists
if [ ! -e "${ADSB_DEVICE}" ]; then
    echo "Device ${ADSB_DEVICE} not found"
    exit 1
fi

# Check if TCP port is listening using ss (replaces deprecated netstat)
if ! ss -tln sport = ":${ADSB_TCP_PORT}" | grep -q LISTEN; then
    echo "Port ${ADSB_TCP_PORT} not listening"
    exit 1
fi

# All checks passed
exit 0
