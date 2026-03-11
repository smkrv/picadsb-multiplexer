#!/bin/bash
set -euo pipefail

# Set defaults if environment variables are not set
ADSB_TCP_PORT="${ADSB_TCP_PORT:-31002}"
ADSB_DEVICE="${ADSB_DEVICE:-/dev/ttyACM0}"
ADSB_LOG_LEVEL="${ADSB_LOG_LEVEL:-INFO}"
ADSB_MAX_CLIENTS="${ADSB_MAX_CLIENTS:-50}"
MAX_LOG_SIZE="${MAX_LOG_SIZE:-100M}"
LOG_RETENTION_DAYS="${LOG_RETENTION_DAYS:-7}"

# Validate required variables
if [ -z "${ADSB_DEVICE}" ]; then
    echo "Error: ADSB_DEVICE environment variable is empty"
    exit 1
fi

# Wait for device to appear
timeout=30
counter=0
while [ ! -e "${ADSB_DEVICE}" ] && [ "$counter" -lt "$timeout" ]; do
    echo "Waiting for device ${ADSB_DEVICE}..."
    sleep 1
    counter=$((counter + 1))
done

if [ ! -e "${ADSB_DEVICE}" ]; then
    echo "Error: Device ${ADSB_DEVICE} not found after ${timeout} seconds"
    exit 1
fi

# Verify logs directory is writable (bind-mount may be owned by root)
if [ -d "/app/logs" ] && [ ! -w "/app/logs" ]; then
    echo "Error: /app/logs is not writable by user $(id -un). Fix host permissions: sudo chown $(id -u):$(id -g) ./logs"
    exit 1
fi

# Check if logs directory exists and manage size
if [ -d "/app/logs" ]; then
    current_size=$(du -sm /app/logs | cut -f1)
    # Strip trailing M/m/MB/mb suffix to get numeric value
    max_size=$(echo "${MAX_LOG_SIZE}" | sed 's/[Mm][Bb]\?$//')

    if ! echo "$max_size" | grep -qE '^[0-9]+$'; then
        echo "Warning: invalid MAX_LOG_SIZE='${MAX_LOG_SIZE}', using default 100M"
        max_size=100
    fi

    if [ "$current_size" -gt "$max_size" ]; then
        echo "Log directory size ($current_size MB) exceeds limit ($max_size MB). Cleaning old logs..."
        find /app/logs \( -name "*.log" -o -name "*.log.[0-9]*" \) -type f -mtime +"${LOG_RETENTION_DAYS}" -delete
    fi
fi

# Build command as array for safe execution (prevents shell injection)
CMD=(python3 -u picadsb-multiplexer.py
    --port "${ADSB_TCP_PORT}"
    --serial "${ADSB_DEVICE}"
    --log-level "${ADSB_LOG_LEVEL}"
    --max-clients "${ADSB_MAX_CLIENTS}"
)

# Add optional remote parameters only if both host and port are set
if [ -n "${ADSB_REMOTE_HOST:-}" ] && [ -n "${ADSB_REMOTE_PORT:-}" ]; then
    CMD+=(--remote-host "${ADSB_REMOTE_HOST}" --remote-port "${ADSB_REMOTE_PORT}")
fi

# Add no-init flag if set
if [ "${ADSB_NO_INIT:-false}" = "true" ]; then
    CMD+=(--no-init)
fi

# Execute the constructed command
exec "${CMD[@]}"
