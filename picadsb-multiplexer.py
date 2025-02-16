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

    def __init__(self, tcp_port: int = 30002, serial_port: str = '/dev/ttyACM0',
                 log_level: str = 'INFO', skip_init: bool = False):
        """Initialize the multiplexer with given parameters."""
        self.tcp_port = tcp_port
        self.serial_port = serial_port
        self.skip_init = skip_init
        self._setup_logging(log_level)

        # Runtime state
        self.running = True
        self.firmware_version = None
        self.device_id = None
        self._buffer = b''
        self._last_data_time = time.time()
        self._no_data_logged = False
        self._sync_state = True

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
            'sync_losses': 0
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

    def _init_serial(self):
        """Initialize serial port with device configuration."""
        try:
            self.ser = serial.Serial(
                port=self.serial_port,
                baudrate=115200,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.1,
                xonxoff=False,
                rtscts=False,
                dsrdtr=False
            )

            if hasattr(self.ser, 'set_buffer_size'):
                self.ser.set_buffer_size(rx_size=self.SERIAL_BUFFER_SIZE)

            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()

            self.ser.setDTR(False)
            self.ser.setRTS(False)
            time.sleep(0.25)
            self.ser.setDTR(True)
            self.ser.setRTS(True)
            time.sleep(0.25)

            if not self.skip_init:
                retry_count = 3
                while retry_count > 0:
                    if self._initialize_device():
                        break
                    retry_count -= 1
                    time.sleep(1)
                    self.logger.warning(f"Retrying initialization, attempts left: {retry_count}")

                if retry_count == 0:
                    raise Exception("Device initialization failed after all retries")
            else:
                self.logger.info("Skipping device initialization (--no-init mode)")
                self._check_device_version()

            self.logger.info(f"Serial port {self.serial_port} initialized successfully")

        except Exception as e:
            self.logger.error(f"Failed to initialize serial port: {e}")
            raise

    def _read_response(self) -> Optional[bytes]:
        """Read response from device with timeout."""
        buffer = b''
        timeout = time.time() + 1.0  # 1 second timeout

        while time.time() < timeout:
            if self.ser.in_waiting:
                byte = self.ser.read()
                if byte == b'#' or byte == b'*':  # message start
                    buffer = byte
                elif byte in [b'\r', b'\n']:  # message end
                    if buffer:
                        return buffer
                else:
                    buffer += byte

                if len(buffer) > self.MAX_MESSAGE_LENGTH:
                    self.logger.warning(f"Response buffer overflow: {buffer!r}")
                    return None

            time.sleep(0.01)

        if buffer:
            self.logger.warning(f"Incomplete response (timeout): {buffer!r}")
        else:
            self.logger.warning("No response received (timeout)")

        return None

    def _reset_device(self):
        """Reset device to recover from errors."""
        try:
            self.logger.info("Performing device reset")

            self.ser.setDTR(False)
            self.ser.setRTS(False)
            time.sleep(0.25)
            self.ser.setDTR(True)
            self.ser.setRTS(True)
            time.sleep(0.25)

            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()
            self._buffer = b''

            if not self._initialize_device():
                raise Exception("Device reset failed")

            self._sync_state = True
            self._last_sync_time = time.time()
            self.logger.info("Device reset completed successfully")

        except Exception as e:
            self.logger.error(f"Error during device reset: {e}")
            raise

    def _initialize_device(self) -> bool:
        """Initialize device with specific command sequence."""
        commands = [
            (b'\x00', "Version check"),
            (b'\x43\x00', "Stop reception"),
            (b'\x51\x01\x00', "Set mode"),
            (b'\x37\x03', "Set filter"),
            (b'\x43\x02', "Status check 1"),
            (b'\x43\x00', "Status check 2"),
            (b'\x51\x00\x00', "Reset mode"),
            (b'\x37\x03', "Set filter again"),
            (b'\x43\x02', "Status check 3"),
            (b'\x38', "Start reception")
        ]

        for cmd, desc in commands:
            self.logger.debug(f"Sending {desc}: {cmd.hex()}")
            formatted_cmd = self.format_command(cmd)
            self.ser.write(formatted_cmd)
            time.sleep(0.5)

            response = self._read_response()
            if response:
                self.logger.debug(f"Response to {desc}: {response}")
                if not self.verify_response(cmd, response):
                    self.logger.error(f"Invalid response to {desc}")
                    return False
            else:
                self.logger.warning(f"No response to {desc}")
                return False

        return True

    def format_command(self, cmd_bytes: bytes) -> bytes:
        """Format command according to device protocol."""
        cmd_str = '-'.join([f"{b:02X}" for b in cmd_bytes])
        return f"#{cmd_str}\r".encode()

    def verify_response(self, cmd: bytes, response: bytes) -> bool:
        """Verify device response to command."""
        if not response:
            return False

        try:
            resp_bytes = [int(x, 16) for x in response[1:].decode().strip('-').split('-')]

            if cmd[0] == 0x00:  # Version command
                return (resp_bytes[0] == 0x00 and
                       resp_bytes[2] == 0x05 and
                       resp_bytes[3] == 0x04)
            elif cmd[0] == 0x43:  # Mode commands
                return (resp_bytes[0] == 0x43 and
                       resp_bytes[1] == cmd[1] and
                       all(x == 0 for x in resp_bytes[2:]))
            elif cmd[0] == 0x44:  # UREF offset command
                return (resp_bytes[0] == 0x44 and
                       resp_bytes[1] == cmd[1])
            elif cmd[0] == 0x37:  # Filter command
                return (resp_bytes[0] == 0x37 and
                       resp_bytes[1] == 0x03)
            elif cmd[0] == 0x38:  # Start command
                return (resp_bytes[0] == 0x38 and
                       resp_bytes[2] == 0x01 and
                       resp_bytes[3] == 0x64)

            return True
        except Exception as e:
            self.logger.error(f"Error verifying response: {e}")
            return False

    def _check_device_version(self):
        """Check device version."""
        self.ser.write(self.format_command(b'\x00'))
        response = self._read_response()
        if not response or not self.verify_response(b'\x00', response):
            raise Exception("Device version check failed")
        return True

    def _process_serial_data(self):
        """Process incoming data from the ADSB device."""
        try:
            if not self.ser.is_open:
                self.logger.error("Serial port is closed")
                return

            if self.ser.in_waiting:
                data = self.ser.read(self.ser.in_waiting)
                self.stats['bytes_received'] += len(data)

                for byte in data:
                    if byte in b'*#@':
                        if self._buffer:
                            self.logger.debug(f"Discarding buffer: {self._buffer!r}")
                        self._buffer = bytes([byte])
                        self._sync_state = True
                    elif byte in b'\r\n;':
                        if self._buffer:
                            if self.validate_message(self._buffer):
                                try:
                                    self.message_queue.put_nowait(self._buffer + b'\n')
                                    self.stats['messages_processed'] += 1
                                except queue.Full:
                                    self.stats['messages_dropped'] += 1
                            self._buffer = b''
                    else:
                        if self._sync_state:
                            self._buffer += bytes([byte])
                            if len(self._buffer) > self.MAX_MESSAGE_LENGTH:
                                self._buffer = b''
                                self._sync_state = False
                                self.stats['buffer_overflows'] += 1

        except Exception as e:
            self.logger.error(f"Error in serial processing: {e}")
            self.stats['errors'] += 1

    def _check_device_status(self):
        """Check device status if no data received for a while."""
        current_time = time.time()

        if current_time - self._last_data_time > self.NO_DATA_TIMEOUT:
            if not self._no_data_logged:
                self.logger.warning(f"No data received for {self.NO_DATA_TIMEOUT} seconds, checking device...")
                self._no_data_logged = True

            self.ser.write(self.format_command(b'\x00'))
            response = self._read_response()

            if not response or not self.verify_response(b'\x00', response):
                self.logger.error("Device not responding to version check")
                if not self._reconnect():
                    self.logger.error("Failed to reconnect to device")
                    self.running = False
            else:
                self.logger.info("Device responding normally despite no data")
                self._no_data_logged = False
                self._last_data_time = current_time

    def _reconnect(self) -> bool:
        """Attempt to reconnect to the device with multiple retries."""
        self.logger.info("Attempting to reconnect...")

        for attempt in range(self.MAX_RECONNECT_ATTEMPTS):
            try:
                self.logger.info(f"Reconnection attempt {attempt + 1}/{self.MAX_RECONNECT_ATTEMPTS}")

                if hasattr(self, 'ser') and self.ser.is_open:
                    self.ser.close()

                time.sleep(self.RECONNECT_DELAY)

                self._init_serial()

                for _ in range(3):
                    self.ser.write(self.format_command(b'\x00'))
                    response = self._read_response()
                    if response and self.verify_response(b'\x00', response):
                        self.logger.info("Successfully reconnected to device")
                        self.stats['reconnects'] += 1
                        self._last_data_time = time.time()
                        self._no_data_logged = False
                        self._sync_state = True
                        return True
                    time.sleep(0.1)

            except Exception as e:
                self.logger.error(f"Reconnection attempt {attempt + 1} failed: {e}")

            self.logger.warning(f"Reconnection attempt {attempt + 1} failed, waiting {self.RECONNECT_DELAY} seconds...")
            time.sleep(self.RECONNECT_DELAY)

        self.logger.error(f"Failed to reconnect after {self.MAX_RECONNECT_ATTEMPTS} attempts")
        return False

    def _check_sync_state(self):
        """Check and maintain synchronization state."""
        current_time = time.time()

        if not self._sync_state:
            if current_time - self._last_sync_time > 5:
                self.logger.warning("Lost synchronization, attempting reset")
                self._reset_device()
                return

            if current_time - self._last_sync_time > 1:
                self.ser.reset_input_buffer()
                self._buffer = b''
                self.logger.debug("Resetting input buffer due to sync loss")

        self._last_sync_time = current_time

    def _update_stats(self):
        """Update and log periodic statistics."""
        current_time = time.time()
        if current_time - self.last_stats_update >= self.stats_interval:
            messages_per_minute = (self.stats['messages_processed'] -
                                 self.stats['last_minute_count'])

            bytes_per_minute = self.stats['bytes_processed'] - \
                              getattr(self, '_last_bytes_processed', 0)

            self.logger.info(
                f"Statistics:\n"
                f"  Messages/min: {messages_per_minute}\n"
                f"  Bytes/min: {bytes_per_minute}\n"
                f"  Total messages: {self.stats['messages_processed']}\n"
                f"  Total bytes: {self.stats['bytes_processed']}\n"
                f"  Dropped messages: {self.stats['messages_dropped']}\n"
                f"  Invalid messages: {self.stats['invalid_messages']}\n"
                f"  Buffer overflows: {self.stats['buffer_overflows']}\n"
                f"  Sync losses: {self.stats['sync_losses']}\n"
                f"  Errors: {self.stats['errors']}\n"
                f"  Connected clients: {self.stats['clients_current']}\n"
                f"  Queue: {self.message_queue.qsize()}/{self.message_queue.maxsize}"
            )

            self.stats['messages_per_minute'] = messages_per_minute
            self.stats['last_minute_count'] = self.stats['messages_processed']
            self._last_bytes_processed = self.stats['bytes_processed']
            self.last_stats_update = current_time

    def _accept_new_client(self):
        """Accept new client connection."""
        try:
            client_socket, address = self.server_socket.accept()
            self._configure_client_socket(client_socket)
            self.clients.append(client_socket)
            self.stats['clients_total'] += 1
            self.stats['clients_current'] = len(self.clients)
            self.logger.info(f"New client connected from {address}")
        except Exception as e:
            self.logger.error(f"Error accepting client: {e}")

    def _configure_client_socket(self, client_socket: socket.socket):
        """Configure new client socket with keepalive settings."""
        try:
            client_socket.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            if sys.platform.startswith('linux'):
                client_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60)
                client_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
                client_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 6)
            client_socket.setblocking(False)
            self.client_last_active[client_socket] = time.time()
        except Exception as e:
            self.logger.warning(f"Failed to configure keepalive for client: {e}")

    def _handle_client_data(self, client: socket.socket):
        """Handle data received from client."""
        try:
            data = client.recv(1024)
            if not data:
                self._remove_client(client)
                return

            if data == self.KEEPALIVE_MARKER:
                self.client_last_active[client] = time.time()
                self.logger.debug(f"Received keepalive from {client.getpeername()}")
                return

            try:
                cmd = data.decode().strip()
                if cmd.startswith('VERSION'):
                    self._send_version_to_client(client)
                elif cmd.startswith('STATS'):
                    self._send_stats_to_client(client)
                else:
                    self.logger.debug(f"Received unknown command from client: {cmd}")
            except:
                self.logger.debug(f"Received data from client: {data!r}")

        except Exception as e:
            self.logger.debug(f"Error handling client data: {e}")
            self._remove_client(client)

    def _send_version_to_client(self, client: socket.socket):
        """Send version information to client."""
        try:
            version_info = f"PicADSB Multiplexer v1.0\n"
            client.send(version_info.encode())
        except Exception as e:
            self.logger.error(f"Error sending version to client: {e}")
            self._remove_client(client)

    def _send_stats_to_client(self, client: socket.socket):
        """Send statistics to client."""
        try:
            stats = (
                f"Messages processed: {self.stats['messages_processed']}\n"
                f"Messages/minute: {self.stats['messages_per_minute']}\n"
                f"Errors: {self.stats['errors']}\n"
                f"Connected clients: {self.stats['clients_current']}\n"
            )
            client.send(stats.encode())
        except Exception as e:
            self.logger.error(f"Error sending stats to client: {e}")
            self._remove_client(client)

    def _broadcast_message(self, data: bytes):
        """Broadcast data to all connected clients."""
        disconnected_clients = []
        for client in self.clients:
            try:
                sent = client.send(data)
                if sent == 0:
                    raise BrokenPipeError("Connection lost")
                self.client_last_active[client] = time.time()
            except Exception as e:
                self.logger.debug(f"Error sending to client: {e}")
                disconnected_clients.append(client)

        for client in disconnected_clients:
            self._remove_client(client)

    def _remove_client(self, client: socket.socket):
        """Remove client and clean up resources."""
        try:
            if client in self.clients:
                self.clients.remove(client)
            if client in self.client_last_active:
                del self.client_last_active[client]
            try:
                client.close()
            except:
                pass
            self.stats['clients_current'] = len(self.clients)
            self.logger.info("Client disconnected")
        except Exception as e:
            self.logger.error(f"Error removing client: {e}")

    def _check_timeouts(self):
        """Check for timeouts and inactive clients."""
        current_time = time.time()

        if current_time - self._last_data_time > self.NO_DATA_TIMEOUT and not self._no_data_logged:
            self.logger.warning(f"No data received for {self.NO_DATA_TIMEOUT} seconds")
            self._check_device_status()

        for client in list(self.clients):
            if current_time - self.client_last_active.get(client, 0) > self.NO_DATA_TIMEOUT:
                self.logger.warning(f"Closing inactive client {client.getpeername()}")
                self._remove_client(client)

    def validate_message(self, message: bytes) -> bool:
        """Validate message format."""
        try:
            if len(message) < 3:
                return False

            if not message.startswith((b'*', b'#', b'@')):
                return False

            if not message.rstrip(b'\r\n').endswith(b';'):
                return False

            content = message[1:-1]
            valid_chars = set(b'0123456789ABCDEF-')
            if not all(b in valid_chars for b in content):
                return False

            return True

        except Exception as e:
            self.logger.error(f"Message validation error: {e}")
            return False

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals."""
        self.logger.info(f"Received signal {signum}, shutting down...")
        self.running = False

    def run(self):
        """Main operation loop."""
        self.logger.info("Starting multiplexer...")
        last_sync_check = time.time()

        try:
            while self.running:
                try:
                    self._process_serial_data()

                    try:
                        readable, _, _ = select.select(
                            [self.server_socket] + self.clients, [], [], 0.1)
                        for sock in readable:
                            if sock is self.server_socket:
                                self._accept_new_client()
                            else:
                                self._handle_client_data(sock)
                    except select.error:
                        pass

                    while not self.message_queue.empty():
                        try:
                            message = self.message_queue.get_nowait()
                            self._broadcast_message(message)
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

    args = parser.parse_args()

    try:
        multiplexer = PicADSBMultiplexer(
            tcp_port=args.port,
            serial_port=args.serial,
            log_level=args.log_level,
            skip_init=args.no_init
        )
        multiplexer.run()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
