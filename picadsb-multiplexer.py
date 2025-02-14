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
from typing import Optional, Dict, Any
import threading

class picADSB_multiplexer:
    """
    Main multiplexer class that handles device communication and client connections.

    Attributes:
        tcp_port (int): TCP port for client connections
        serial_port (str): Serial port device path
        stats (dict): Runtime statistics
        clients (list): Connected TCP clients
        running (bool): Main loop control flag
        keepalive_interval (float): Interval between keepalive messages
        keepalive_message (bytes): Keepalive message format
    """

    def __init__(self, tcp_port: int = 30002, serial_port: str = '/dev/ttyACM0', log_level: str = 'INFO'):
        """
        Initialize the multiplexer.

        Args:
            tcp_port: TCP port number for client connections
            serial_port: Serial port device path
            log_level: Logging level (DEBUG, INFO, WARNING, ERROR)
        """
        self.tcp_port = tcp_port
        self.serial_port = serial_port

        # Setup logging with specified level
        self._setup_logging(log_level)

        # Initialize statistics
        self.stats = {
            'messages_processed': 0,
            'messages_per_minute': 0,
            'start_time': time.time(),
            'last_minute_count': 0,
            'last_minute_time': time.time(),
            'errors': 0,
            'keepalives_sent': 0
        }

        # Initialize message queue and client list
        self.message_queue = queue.Queue(maxsize=1000)  # Buffer for 1000 messages
        self.clients = []
        self.running = True

        # Keepalive configuration
        self.keepalive_interval = 5.0  # seconds
        self.last_keepalive_time = time.time()
        self.keepalive_message = b"*0000;\r\n"  # Null ADSB message as keepalive
        self.last_data_time = time.time()
        self.data_timeout = 30.0  # seconds before sending keepalive

        # TCP socket options for keepalive
        self.tcp_keepalive_options = {
            socket.SO_KEEPALIVE: 1,    # Enable keepalive
            socket.TCP_KEEPIDLE: 60,   # Start sending keepalive after 60 seconds of idle
            socket.TCP_KEEPINTVL: 10,  # Send keepalive every 10 seconds
            socket.TCP_KEEPCNT: 5      # Drop connection after 5 failed keepalives
        }

        # Initialize network and serial interfaces
        self._init_socket()
        self._init_serial()

        # Setup signal handlers for graceful shutdown
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _setup_logging(self, log_level: str = 'INFO'):
        """
        Configure logging with both file and console output.

        Args:
            log_level: Logging level (DEBUG, INFO, WARNING, ERROR)
        """
        # Convert string level to logging constant
        numeric_level = getattr(logging, log_level.upper(), None)
        if not isinstance(numeric_level, int):
            raise ValueError(f'Invalid log level: {log_level}')

        self.logger = logging.getLogger('picADSB_multiplexer')
        self.logger.setLevel(numeric_level)

        # Create logs directory if needed
        os.makedirs('logs', exist_ok=True)

        # File handler for detailed logging
        fh = logging.FileHandler(f'logs/picadsb-multiplexer_{datetime.now():%Y%m%d_%H%M%S}.log')
        fh.setLevel(numeric_level)

        # Console handler for important messages
        ch = logging.StreamHandler()
        ch.setLevel(numeric_level)

        # Formatting
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        fh.setFormatter(formatter)
        ch.setFormatter(formatter)

        self.logger.addHandler(fh)
        self.logger.addHandler(ch)

    def _init_socket(self):
        """Initialize TCP server socket with keepalive support."""
        try:
            self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

            # Set TCP keepalive options
            for option, value in self.tcp_keepalive_options.items():
                try:
                    self.server_socket.setsockopt(socket.SOL_TCP, option, value)
                except:
                    self.logger.warning(f"Failed to set TCP keepalive option {option}")

            self.server_socket.bind(('', self.tcp_port))
            self.server_socket.listen(5)
            self.server_socket.setblocking(False)
            self.logger.info(f"Listening on TCP port {self.tcp_port}")
        except Exception as e:
            self.logger.error(f"Failed to initialize socket: {e}")
            raise

    def _init_serial(self):
        """Initialize serial port and device configuration."""
        try:
            self.ser = serial.Serial(
                port=self.serial_port,
                baudrate=115200,
                timeout=0.1
            )

            # Clear buffers
            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()

            # Initialize device
            self._initialize_device()

            self.logger.info(f"Serial port {self.serial_port} initialized")
        except Exception as e:
            self.logger.error(f"Failed to initialize serial port: {e}")
            raise

    def _initialize_device(self):
        """
        Initialize ADS-B device with proper command sequence.

        Command sequence:
        1. Stop decoding
        2. Set mode 2 (all frames)
        3. Enable timestamp
        4. Start reception
        """
        init_sequence = [
            (b'#43-00\r', "Stop decoding"),
            (b'#51-01-00\r', "Set mode"),
            (b'#37-03\r', "Set filter"),
            (b'#43-00\r', "Status check"),
            (b'#51-00-00\r', "Reset mode"),
            (b'#37-03\r', "Set filter"),
            (b'#38\r', "Start reception")
        ]

        for cmd, desc in init_sequence:
            try:
                self.logger.debug(f"Sending {desc} command: {cmd!r}")
                self.ser.write(cmd)
                time.sleep(0.1)
                response = self.ser.read_all()
                self.logger.debug(f"{desc} response: {response!r}")
            except Exception as e:
                self.logger.error(f"Error during {desc}: {e}")
                raise

    def _update_stats(self):
        """Update and log message processing statistics."""
        current_time = time.time()
        if current_time - self.stats['last_minute_time'] >= 60:
            messages_per_minute = self.stats['messages_processed'] - self.stats['last_minute_count']
            self.stats['messages_per_minute'] = messages_per_minute
            self.stats['last_minute_count'] = self.stats['messages_processed']
            self.stats['last_minute_time'] = current_time

            self.logger.info(
                f"Statistics: Messages/min: {messages_per_minute}, "
                f"Total: {self.stats['messages_processed']}, "
                f"Keepalives: {self.stats['keepalives_sent']}, "
                f"Errors: {self.stats['errors']}"
            )

    def _handle_client_connections(self):
        """Handle new client connections and set keepalive options."""
        try:
            readable, _, _ = select.select([self.server_socket] + self.clients, [], [], 0.1)

            for sock in readable:
                if sock is self.server_socket:
                    # New connection
                    client_socket, address = self.server_socket.accept()
                    client_socket.setblocking(False)

                    # Set keepalive options for new client
                    for option, value in self.tcp_keepalive_options.items():
                        try:
                            client_socket.setsockopt(socket.SOL_TCP, option, value)
                        except:
                            self.logger.warning(f"Failed to set keepalive option {option} for {address}")

                    self.clients.append(client_socket)
                    self.logger.info(f"New client connected from {address}")
                else:
                    # Handle existing client
                    try:
                        data = sock.recv(1024)
                        if not data:
                            self.clients.remove(sock)
                            sock.close()
                            self.logger.info(f"Client {sock.getpeername()} disconnected")
                    except:
                        try:
                            self.clients.remove(sock)
                            sock.close()
                            self.logger.info(f"Client connection lost")
                        except:
                            pass
        except Exception as e:
            self.logger.error(f"Error handling client connections: {e}")

    def _send_keepalive(self):
        """
        Send keepalive messages to all connected clients.
        Sends keepalive if no data has been received for data_timeout seconds
        and last keepalive was sent more than keepalive_interval seconds ago.
        """
        current_time = time.time()

        if (current_time - self.last_data_time >= self.data_timeout and
            current_time - self.last_keepalive_time >= self.keepalive_interval):

            disconnected_clients = []
            for client in self.clients:
                try:
                    client.send(self.keepalive_message)
                    self.stats['keepalives_sent'] += 1
                    self.logger.debug(f"Sent keepalive to {client.getpeername()}")
                except:
                    disconnected_clients.append(client)
                    self.logger.warning(f"Failed to send keepalive, marking client for removal")

            # Remove disconnected clients
            for client in disconnected_clients:
                try:
                    self.clients.remove(client)
                    client.close()
                    self.logger.info(f"Removed disconnected client")
                except:
                    pass

            self.last_keepalive_time = current_time

    def _process_serial_data(self):
        """
        Process incoming data from the ADS-B device.
        Updates last_data_time when real data is received.
        """
        try:
            if self.ser.in_waiting:
                data = self.ser.read_all()
                if data:
                    self.logger.debug(f"Raw data received: {data!r}")

                    # Update last data time
                    self.last_data_time = time.time()

                    # Split into individual messages
                    messages = data.split(b';')
                    for msg in messages:
                        if msg.startswith(b'*'):
                            # Add terminator and queue message
                            full_msg = msg + b';'
                            try:
                                self.message_queue.put_nowait(full_msg)
                                self.stats['messages_processed'] += 1
                                self.logger.debug(f"Processed message: {full_msg!r}")
                            except queue.Full:
                                self.logger.warning("Message queue full, dropping message")
                        else:
                            self.logger.debug(f"Skipped non-ADSB message: {msg!r}")
        except Exception as e:
            self.logger.error(f"Error processing serial data: {e}")
            self.stats['errors'] += 1

    def _broadcast_messages(self):
        """Broadcast queued messages to all connected clients."""
        try:
            while not self.message_queue.empty():
                message = self.message_queue.get_nowait()
                disconnected_clients = []

                for client in self.clients:
                    try:
                        client.send(message)
                    except:
                        disconnected_clients.append(client)

                # Remove disconnected clients
                for client in disconnected_clients:
                    try:
                        self.clients.remove(client)
                        client.close()
                    except:
                        pass
        except Exception as e:
            self.logger.error(f"Error broadcasting messages: {e}")

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals (SIGINT, SIGTERM)."""
        self.logger.info(f"Received signal {signum}, shutting down...")
        self.running = False

    def run(self):
        """
        Main operation loop.

        Handles:
        - Client connections
        - Message reception
        - Message broadcasting
        - Keepalive messages
        - Statistics updates
        """
        self.logger.info("Starting ADS-B multiplexer")

        try:
            while self.running:
                self._handle_client_connections()
                self._process_serial_data()
                self._broadcast_messages()
                self._send_keepalive()
                self._update_stats()
                time.sleep(0.001)  # Prevent CPU overload

        except Exception as e:
            self.logger.error(f"Error in main loop: {e}")
        finally:
            self.cleanup()

    def cleanup(self):
        """Clean up resources and connections on shutdown."""
        self.logger.info("Cleaning up...")

        # Close client connections
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
            self.ser.close()
        except:
            pass

        self.logger.info("Cleanup complete")

def main():
    """
    Entry point with command line argument parsing.

    Usage:
        picadsb-multiplexer.py [--port PORT] [--device DEVICE] [--log LEVEL]
    """
    import argparse

    parser = argparse.ArgumentParser(description='ADS-B Multiplexer')
    parser.add_argument('--port', type=int, default=30002,
                      help='TCP port number (default: 30002)')
    parser.add_argument('--device', type=str, default='/dev/ttyACM0',
                      help='Serial device (default: /dev/ttyACM0)')
    parser.add_argument('--log', type=str, default='INFO',
                      choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
                      help='Logging level (default: INFO)')

    args = parser.parse_args()

    try:
        muxer = picADSB_multiplexer(
            tcp_port=args.port,
            serial_port=args.device,
            log_level=args.log
        )
        muxer.run()
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
