FROM --platform=$TARGETPLATFORM python:3.11-slim

# Install required system packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    procps \
    iproute2 \
    && rm -rf /var/lib/apt/lists/*

# Create app directory
WORKDIR /app

# Copy requirements
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY picadsb-multiplexer.py .
COPY picadsb/ ./picadsb/
COPY entrypoint.sh .
COPY health_check.sh .

# Create directory for logs and set permissions
RUN mkdir -p /app/logs

# Make scripts executable
RUN chmod +x /app/entrypoint.sh /app/health_check.sh

# Precompile Python files for read_only filesystem
RUN python -m compileall /app

# Create non-root user
RUN groupadd -r adsb && useradd -r -g adsb -G dialout -d /app -s /sbin/nologin adsb && \
    chown -R adsb:adsb /app

USER adsb

# Set environment variables with defaults
ENV ADSB_TCP_PORT=31002
ENV ADSB_DEVICE=/dev/ttyACM0
ENV ADSB_LOG_LEVEL=INFO
ENV ADSB_NO_INIT=false
ENV ADSB_MAX_CLIENTS=50
ENV ADSB_REMOTE_HOST=
ENV ADSB_REMOTE_PORT=

# Log management settings
ENV MAX_LOG_SIZE=100M
ENV LOG_RETENTION_DAYS=7
ENV PYTHONDONTWRITEBYTECODE=1

# Expose the default port
EXPOSE 31002

# Add healthcheck with parameters suitable for ADS-B operation
HEALTHCHECK --interval=300s --timeout=30s --start-period=60s --retries=3 \
    CMD /app/health_check.sh

# Run entrypoint script
ENTRYPOINT ["/app/entrypoint.sh"]
