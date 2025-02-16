FROM --platform=$TARGETPLATFORM python:3.11-slim

# Install required system packages
RUN apt-get update && apt-get install -y \
    udev \
    logrotate \
    && rm -rf /var/lib/apt/lists/*

# Create app directory
WORKDIR /app

# Configure logrotate
RUN echo '/app/logs/*.log { daily rotate 7 compress delaycompress missingok notifempty create 644 root root size 10M }' > /etc/logrotate.d/application

# Copy requirements
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY picadsb-multiplexer.py .
COPY entrypoint.sh .

# Create directory for logs
RUN mkdir -p /app/logs

# Make entrypoint executable
RUN chmod +x /app/entrypoint.sh

# Set environment variables with defaults
ENV ADSB_TCP_PORT=31002
ENV ADSB_DEVICE=/dev/ttyACM0
ENV ADSB_LOG_LEVEL=INFO
ENV ADSB_NO_INIT=false
ENV ADSB_REMOTE_HOST=
ENV ADSB_REMOTE_PORT=

# Log management settings
ENV MAX_LOG_SIZE=100M
ENV LOG_RETENTION_DAYS=7

# Expose the default port
EXPOSE ${ADSB_TCP_PORT}

# Run entrypoint script
ENTRYPOINT ["/app/entrypoint.sh"]
