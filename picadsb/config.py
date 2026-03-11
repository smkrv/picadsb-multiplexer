"""Configuration management for PicADSB Multiplexer."""

import os
import sys
from dataclasses import dataclass


_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


@dataclass
class Config:
    """All configurable parameters in one place."""

    # Network
    tcp_port: int = 30002
    max_clients: int = 50
    listen_backlog: int = 10

    # Serial
    serial_port: str = "/dev/ttyACM0"
    serial_baudrate: int = 115200
    serial_timeout: float = 0.1
    serial_buffer_size: int = 131072
    skip_init: bool = False

    # Remote forwarding (optional)
    remote_host: str | None = None
    remote_port: int | None = None
    remote_check_interval: int = 60
    remote_reconnect_cooldown: int = 60

    # Timing
    heartbeat_interval: int = 30
    stats_interval: int = 60
    no_data_timeout: int = 600
    sync_check_interval: int = 1

    # Health check
    health_no_data_timeout: int = 14400
    health_startup_grace: int = 300

    # Reconnection
    max_reconnect_attempts: int = 50
    reconnect_delay: int = 5
    max_reconnect_delay: int = 300

    # Processing
    max_message_length: int = 256
    queue_maxsize: int = 5000
    signal_level: int = 0xFF

    # Logging
    log_level: str = "INFO"
    log_dir: str = "logs"

    def __post_init__(self):
        """Validate configuration values."""
        if not 1 <= self.tcp_port <= 65535:
            raise ValueError(f"tcp_port must be 1-65535, got {self.tcp_port}")
        if not 1 <= self.max_clients <= 1000:
            raise ValueError(f"max_clients must be 1-1000, got {self.max_clients}")
        if not 1 <= self.listen_backlog <= 128:
            raise ValueError(f"listen_backlog must be 1-128, got {self.listen_backlog}")
        if self.remote_port is not None and not 1 <= self.remote_port <= 65535:
            raise ValueError(f"remote_port must be 1-65535, got {self.remote_port}")
        if bool(self.remote_host) != bool(self.remote_port):
            raise ValueError("remote_host and remote_port must both be set or both be None")
        if not 0 <= self.signal_level <= 0xFF:
            raise ValueError(f"signal_level must be 0-255, got {self.signal_level}")
        if self.max_reconnect_attempts < 1:
            raise ValueError(f"max_reconnect_attempts must be >= 1, got {self.max_reconnect_attempts}")
        if self.reconnect_delay < 1:
            raise ValueError(f"reconnect_delay must be >= 1, got {self.reconnect_delay}")
        if self.log_level.upper() not in _VALID_LOG_LEVELS:
            raise ValueError(f"log_level must be one of {_VALID_LOG_LEVELS}, got {self.log_level!r}")

    @classmethod
    def from_env(cls) -> "Config":
        """Create Config from environment variables (for Docker)."""
        kwargs = {}
        env_map = {
            "ADSB_TCP_PORT": ("tcp_port", int),
            "ADSB_DEVICE": ("serial_port", str),
            "ADSB_LOG_LEVEL": ("log_level", str),
            "ADSB_REMOTE_HOST": ("remote_host", str),
            "ADSB_REMOTE_PORT": ("remote_port", int),
            "ADSB_NO_INIT": ("skip_init", lambda x: x.lower() == "true"),
            "ADSB_MAX_CLIENTS": ("max_clients", int),
        }
        for env_key, (attr, converter) in env_map.items():
            val = os.environ.get(env_key)
            if val is not None and val != "":
                try:
                    kwargs[attr] = converter(val)
                except (ValueError, TypeError) as e:
                    print(f"Warning: ignoring invalid env var {env_key}={val!r}: {e}",
                          file=sys.stderr)
        return cls(**kwargs)
