FROM --platform=$TARGETPLATFORM python:3.11-slim

# Install required system packages
RUN apt-get update && apt-get install -y \
    udev \
    build-essential \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements
COPY requirements.txt ./

# Install Python dependencies with platform-specific handling
RUN if [ "$TARGETPLATFORM" = "linux/arm/v7" ]; then \
    pip install --no-cache-dir numpy==1.26.4 --only-binary=:all: && \
    pip install --no-cache-dir -r requirements.txt; \
    else \
    pip install --no-cache-dir -r requirements.txt; \
    fi

# Copy application
COPY picadsb-multiplexer.py .
COPY entrypoint.sh .

# Create directory for logs
RUN mkdir -p /app/logs

# Make entrypoint executable
RUN chmod +x /app/entrypoint.sh

# Set environment variables with defaults
ENV ADSB_TCP_PORT=30002
ENV ADSB_DEVICE=/dev/ttyACM0
ENV ADSB_LOG_LEVEL=INFO

# Expose the default port
EXPOSE ${ADSB_TCP_PORT}

# Run entrypoint script
ENTRYPOINT ["/app/entrypoint.sh"]
