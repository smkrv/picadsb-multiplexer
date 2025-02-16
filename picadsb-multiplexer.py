#!/usr/bin/env python3
"""
ADS-B Multiplexer for MicroADSB / adsbPIC by Sprut devices.

@license: CC BY-NC-SA 4.0 International
@author: SMKRV
@github: https://github.com/smkrv/picadsb-multiplexer
@source: https://github.com/smkrv/picadsb-multiplexer

This module implements a TCP server that reads ADS-B messages from a MicroADSB USB / adsbPIC by Sprut device
and broadcasts them to multiple clients (like dump1090).

Features:
- Handles multiple client connections
- Processes ADS-B messages in raw format
- Provides statistics and monitoring
- Supports configurable TCP port
- Implements proper device initialization sequence
- Handles device reconnection
- Supports TCP keepalive for connection monitoring
- Can connect to remote server as client

Device specifications:
- Maximum theoretical frame rate: 200,000 fpm
- Practical maximum frame rate: 5,500 fpm
- Communication: USB CDC (115200 baud)
- Message format: ASCII, prefixed with '*', terminated with ';'
"""

import serial
import socket
import select
import queue
import logging
import sys
import time
import os
import signal
from datetime import datetime
from typing import Optional, Dict, Any, List, Tuple

class PicADSBMultiplexer:
    """Main multiplexer class that handles device communication and client connections."""

    # Constants
    KEEPALIVE_INTERVAL = 30
    SERIAL_BUFFER_SIZE = 131072
    MAX_MESSAGE_LENGTH = 256
    NO_DATA_TIMEOUT = 30
    VERSION_CHECK_TIMEOUT = 30
    MAX_RECONNECT_ATTEMPTS = 3
    RECONNECT_DELAY = 2
    SYNC_CHECK_INTERVAL = 1
    RESET_TIMEOUT = 5
    KEEPALIVE_MARKER = b'\n'

    def _setup_logging(self, log_level: str):
        """Configure logging with both file and console output."""
        numeric_level = getattr(logging, log_level.upper(), None)
        if not isinstance(numeric_level, int):
            raise ValueError(f'Invalid log level: {log_level}')

        self.logger = logging.getLogger('PicADSB')
        self.logger.setLevel(numeric_level)

        os.makedirs('logs', exist_ok=True)

        fh = logging.FileHandler(
            f'logs/picadsb_{datetime.now():%Y%m%d_%H%M%S}.log'
        )
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            '%(asctime)s.%(msecs)03d - %(name)s - %(levelname)s - %(message)s',
            '%Y-%m-%d %H:%M:%S'
        ))

        ch_err = logging.StreamHandler(sys.stderr)
        ch_err.setLevel(numeric_level)
        ch_err.setFormatter(logging.Formatter(
            '%(asctime)s.%(msecs)03d - %(levelname)s - %(message)s',
            '%Y-%m-%d %H:%M:%S'
        ))

        self.logger.addHandler(fh)
        self.logger.addHandler(ch_err)

    def __init__(self, tcp_port: int = 30002, serial_port: str = '/dev/ttyACM0',
                 log_level: str = 'INFO', skip_init: bool = False,
                 remote_host: str = None, remote_port: int = None):
        """Initialize the multiplexer with given parameters."""
        self.tcp_port = tcp_port
        self.serial_port = serial_port
        self.skip_init = skip_init
        self.remote_host = remote_host
        self.remote_port = remote_port
        self.remote_socket = None
        self._setup_logging(log_level)

        # Runtime state
        self.running = True
        self.firmware_version = None
        self.device_id = None
        self._buffer = b''
        self._last_data_time = time.time()
        self._no_data_logged = False
        self._sync_state = True
        self._last_sync_time = time.time()

        # Statistics
        self.stats = {
            'messages_processed': 0,
            'messages_per_minute': 0,
            'start_time': time.time(),
            'last_minute_count': 0,
            'last_minute_time': time.time(),
            'errors': 0,
            'reconnects': 0,
            'clients_total': 0,
            'clients_current': 0,
            'messages_dropped': 0,
            'bytes_received': 0,
            'bytes_processed': 0,
            'invalid_messages': 0,
            'buffer_overflows': 0,
            'sync_losses': 0,
            'recovered_messages': 0,
            'buffer_truncated': 0,
            'timeout_processed': 0
        }

        # Timing controls
        self.last_version_check = time.time()
        self.version_check_interval = 300
        self.last_stats_update = time.time()
        self.stats_interval = 60

        # Message handling
        self.message_queue = queue.Queue(maxsize=5000)
        self.clients: List[socket.socket] = []
        self.client_last_active = {}

        # Initialize interfaces
        self._init_socket()
        self._init_serial()

        # Setup signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _init_socket(self):
        """Initialize TCP server socket."""
        try:
            self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server_socket.bind(('', self.tcp_port))
            self.server_socket.listen(5)
            self.server_socket.setblocking(False)
            self.logger.info(f"TCP server listening on port {self.tcp_port}")
        except Exception as e:
            self.logger.error(f"Failed to initialize socket: {e}")
            raise

    def _connect_to_remote(self):
        """Connect to remote server as client."""
        if not self.remote_host or not self.remote_port:
            return

        try:
            if self.remote_socket:
                try:
                    self.remote_socket.close()
                except:
                    pass

            self.remote_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.remote_socket.connect((self.remote_host, self.remote_port))
            self.remote_socket.setblocking(False)
            self.logger.info(f"Connected to remote server {self.remote_host}:{self.remote_port}")
        except Exception as e:
            self.logger.error(f"Failed to connect to remote server: {e}")
            self.remote_socket = None

    def _send_to_remote(self, data: bytes):
        """Send data to remote server."""
        if not self.remote_socket:
            return

        try:
            self.remote_socket.send(data)
        except Exception as e:
            self.logger.error(f"Error sending to remote server: {e}")
            self.remote_socket = None
            # Try to reconnect
            self._connect_to_remote()

    def run(self):
        """Main operation loop."""
        self.logger.info("Starting multiplexer...")
        last_sync_check = time.time()

        # Connect to remote server if specified
        if self.remote_host and self.remote_port:
            self._connect_to_remote()

        try:
            while self.running:
                try:
                    self._process_serial_data()

                    # Add remote_socket to read list if it exists
                    sockets_to_read = [self.server_socket] + self.clients
                    if self.remote_socket:
                        sockets_to_read.append(self.remote_socket)

                    try:
                        readable, _, _ = select.select(sockets_to_read, [], [], 0.1)
                        for sock in readable:
                            if sock is self.server_socket:
                                self._accept_new_client()
                            elif sock is self.remote_socket:
                                # Handle data from remote server
                                try:
                                    data = sock.recv(1024)
                                    if not data:
                                        self.logger.warning("Remote server disconnected")
                                        self.remote_socket = None
                                        # Try to reconnect
                                        self._connect_to_remote()
                                    else:
                                        # Process received data if needed
                                        pass
                                except Exception as e:
                                    self.logger.error(f"Error receiving from remote: {e}")
                                    self.remote_socket = None
                            else:
                                self._handle_client_data(sock)
                    except select.error:
                        pass

                    while not self.message_queue.empty():
                        try:
                            message = self.message_queue.get_nowait()
                            self._broadcast_message(message)
                            # Send data to remote server
                            self._send_to_remote(message)
                        except queue.Empty:
                            break

                    current_time = time.time()

                    if current_time - last_sync_check >= self.SYNC_CHECK_INTERVAL:
                        self._check_sync_state()
                        last_sync_check = current_time

                    self._update_stats()
                    self._check_timeouts()

                except Exception as e:
                    self.logger.error(f"Error in main loop iteration: {e}")
                    self.stats['errors'] += 1
                    time.sleep(0.1)

        except KeyboardInterrupt:
            self.logger.info("Keyboard interrupt received")
        except Exception as e:
            self.logger.error(f"Fatal error in main loop: {e}")
        finally:
            self.cleanup()

    def cleanup(self):
        """Clean up resources before shutdown."""
        self.logger.info("Cleaning up...")

        # Close remote connection
        if self.remote_socket:
            try:
                self.remote_socket.close()
            except:
                pass

        # Close all client connections
        for client in self.clients:
            try:
                client.close()
            except:
                pass

        # Close server socket
        try:
            self.server_socket.close()
        except:
            pass

        # Close serial port
        try:
            self.shutdown()
        except:
            pass

        self.logger.info("Cleanup completed")

if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='PicADSB Multiplexer')
    parser.add_argument('--port', type=int, default=30002,
                      help='TCP port (default: 30002)')
    parser.add_argument('--serial', default='/dev/ttyACM0',
                      help='Serial port (default: /dev/ttyACM0)')
    parser.add_argument('--log-level', default='INFO',
                      choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
                      help='Log level (default: INFO)')
    parser.add_argument('--no-init', action='store_true',
                      help='Skip device initialization')
    parser.add_argument('--remote-host',
                      help='Remote server host to connect to')
    parser.add_argument('--remote-port', type=int,
                      help='Remote server port to connect to')

    args = parser.parse_args()

    try:
        multiplexer = PicADSBMultiplexer(
            tcp_port=args.port,
            serial_port=args.serial,
            log_level=args.log_level,
            skip_init=args.no_init,
            remote_host=args.remote_host,
            remote_port=args.remote_port
        )
        multiplexer.run()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
