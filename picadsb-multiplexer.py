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
import pyModeS as pms
from pyModeS import common
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, Tuple

class CRC24:
    """
    CRC-24 implementation for Beast format messages.
    Single byte CRC for Beast protocol.
    """

    @staticmethod
    def compute(data: bytes, debug: bool = False) -> int:
        """
        Compute single-byte CRC for Beast format.

        Args:
            data: Raw message bytes including type and timestamp
            debug: Enable debug logging

        Returns:
            Single byte CRC value as integer
        """
        try:
            if len(data) < 3:
                raise ValueError("Data too short for CRC computation")

            hex_data = data.hex().upper()
            if debug:
                logging.debug(f"Computing CRC for hex data: {hex_data}")

            # Get full CRC-24 value
            full_crc = pms.common.crc(hex_data)

            # Take only least significant byte
            crc_byte = full_crc & 0xFF

            if debug:
                logging.debug(f"Full CRC: {full_crc:06X}, using byte: {crc_byte:02X}")

            return crc_byte

        except Exception as e:
            logging.error(f"CRC computation error: {e}")
            return 0

    @staticmethod
    def verify(message: bytes, debug: bool = False) -> bool:
        """
        Verify single-byte CRC of Beast message.

        Args:
            message: Complete message including type, timestamp and CRC
            debug: Enable debug logging

        Returns:
            True if CRC is valid
        """
        try:
            if len(message) < 11:  # Minimum length for Beast message
                return False

            # Split message into data and CRC
            data = message[:-1]
            received_crc = message[-1]

            # Calculate CRC
            calculated_crc = CRC24.compute(data)

            if debug:
                logging.debug(f"CRC verification: calculated=0x{calculated_crc:02X}, received=0x{received_crc:02X}")

            return calculated_crc == received_crc

        except Exception as e:
            logging.error(f"CRC verification error: {e}")
            return False

class BeastFormat:
    """
    Beast Binary Format v2.0 implementation.

    Supports:
    - Mode-S short/long messages
    - Mode-A/C with MLAT timestamps
    - Escape sequence handling
    - CRC validation
    """
    ESCAPE = 0x1A
    TYPE_MODEA = 0x31       # Mode-A/C with MLAT timestamp
    TYPE_MODES_SHORT = 0x32  # Mode-S short message (7 bytes)
    TYPE_MODES_LONG = 0x33   # Mode-S long message (14 bytes)

    MODES_SHORT_LEN = 7
    MODES_LONG_LEN = 14
    MODEA_LEN = 2
    TIMESTAMP_LEN = 6

    # Maximum value for 6-byte timestamp (2^48-1)
    MAX_TIMESTAMP = 0xFFFFFFFFFFFF

class TimestampGenerator:
    """Generates monotonic timestamps for Beast format messages."""

    def __init__(self):
        """Initialize timestamp generator with system time reference."""
        self.last_micros = 0
        self.offset = 0
        self.logger = logging.getLogger('PicADSB.Timestamp')
        self.last_system_time = time.time()
        self.last_timestamp = 0
        self.min_increment = 1

    def get_timestamp(self) -> bytes:
        try:
            current_time = time.time()
            current_micros = int((current_time % 86400) * 1e6)  # Время с начала дня в микросекундах

            if self.last_micros == 0:
                self.last_micros = current_micros
                self.last_system_time = current_time
                return current_micros.to_bytes(6, 'big')

            # Проверка перехода через полночь
            if current_micros < self.last_micros:
                self.offset += BeastFormat.MAX_TIMESTAMP + 1

            # Обеспечение монотонности
            if current_micros <= self.last_micros:
                current_micros = self.last_micros + self.min_increment

            adjusted_micros = (current_micros + self.offset) % (BeastFormat.MAX_TIMESTAMP + 1)

            self.last_micros = current_micros
            self.last_system_time = current_time
            self.last_timestamp = adjusted_micros

            return adjusted_micros.to_bytes(6, 'big')

        except Exception as e:
            self.logger.error(f"Timestamp generation error: {e}")
            fallback = (self.last_timestamp + self.min_increment) % BeastFormat.MAX_TIMESTAMP
            self.last_timestamp = fallback
            return fallback.to_bytes(6, 'big')

class PicADSBMultiplexer:
    """Main multiplexer class that handles device communication and client connections."""

    # Constants
    SERIAL_BUFFER_SIZE = 131072
    MAX_MESSAGE_LENGTH = 256
    NO_DATA_TIMEOUT = 600
    MAX_RECONNECT_ATTEMPTS = 50
    RECONNECT_DELAY = 5
    MAX_RECONNECT_DELAY = 300
    SYNC_CHECK_INTERVAL = 1
    KEEPALIVE_MARKER = b'\n'
    HEARTBEAT_INTERVAL = 30
    MAX_CLIENTS = 50

    def _setup_logging(self, log_level: str):
        """
        Configure logging with unified format for both file and console output.

        Args:
            log_level: Logging level (DEBUG, INFO, WARNING, ERROR)
        """
        numeric_level = getattr(logging, log_level.upper(), None)
        if not isinstance(numeric_level, int):
            raise ValueError(f'Invalid log level: {log_level}')

        # Create logger
        self.logger = logging.getLogger('PicADSB')
        self.logger.setLevel(numeric_level)

        # Remove any existing handlers
        self.logger.handlers = []

        # Create formatter
        formatter = logging.Formatter(
            '%(asctime)s.%(msecs)03d - %(levelname)s - %(message)s',
            '%Y-%m-%d %H:%M:%S'
        )

        # File handler
        os.makedirs('logs', exist_ok=True)
        fh = logging.FileHandler(
            f'logs/picadsb_{datetime.now():%Y%m%d_%H%M%S}.log'
        )
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(formatter)

        # Console handler
        ch = logging.StreamHandler(sys.stderr)
        ch.setLevel(numeric_level)
        ch.setFormatter(formatter)

        # Add handlers
        self.logger.addHandler(fh)
        self.logger.addHandler(ch)

        # Prevent log propagation to avoid duplicate messages
        self.logger.propagate = False

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
        self.timestamp_gen = TimestampGenerator()

        # Runtime state
        self.running = True
        self._serial_parse_buffer = bytearray()
        self._last_data_time = time.time()
        self._no_data_logged = False
        self._sync_state = True
        self._last_sync_time = time.time()
        # Add heartbeat configuration
        self._last_connect_attempt = 0

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
            'recovered_messages': 0,
            'buffer_truncated': 0
        }

        # Timing controls
        self.last_stats_update = time.time()
        self.stats_interval = 60
        self.last_remote_check = time.time()
        self.remote_check_interval = 60  # Check remote connection every 60 seconds

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

        # Perform self-test
        if not self.self_test():
            raise RuntimeError("Self-test failed, aborting startup")

    def _send_heartbeat(self):
        """
        Send Beast format heartbeat message (Mode-A null message).
        Includes proper framing with escape sequences.
        """
        try:
            # Create message with initial escape byte
            message = bytearray([BeastFormat.ESCAPE])  # Start marker

            # Build data portion
            data = bytearray()
            data.append(BeastFormat.TYPE_MODEA)  # Type
            data.extend(self.timestamp_gen.get_timestamp())  # 6 bytes timestamp
            data.append(0xFF)  # Signal level
            data.extend([0x00, 0x00])  # Null Mode-A data

            # Calculate CRC on data portion
            crc = CRC24.compute(bytes(data))
            data.append(crc)

            # Add escaped data to message
            message.extend(self._escape_beast_data(data))

            final_msg = bytes(message)
            self.logger.debug(f"Heartbeat message: {final_msg.hex().upper()}")

            # Send to clients
            disconnected = []
            for client in self.clients:
                try:
                    sent = client.send(final_msg)
                    if sent == 0:
                        raise BrokenPipeError("Zero bytes sent")
                except Exception as e:
                    self.logger.warning(f"Failed to send heartbeat: {e}")
                    disconnected.append(client)

            # Clean up disconnected clients
            for client in disconnected:
                self._remove_client(client)

            # Send to remote
            if self.remote_socket:
                try:
                    self.remote_socket.send(final_msg)
                except Exception as e:
                    self.logger.error(f"Remote heartbeat failed: {e}")
                    self.remote_socket = None

        except Exception as e:
            self.logger.error(f"Heartbeat generation failed: {e}")

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

        current_time = time.time()
        if current_time - self._last_connect_attempt < 60:
            return

        self._last_connect_attempt = current_time

        try:
            if self.remote_socket:
                try:
                    self.remote_socket.close()
                except:
                    pass

            self.remote_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.remote_socket.settimeout(5)
            self.remote_socket.connect((self.remote_host, self.remote_port))
            self.remote_socket.setblocking(False)
            self.logger.info(f"Connected to remote server {self.remote_host}:{self.remote_port}")
        except Exception as e:
            self.logger.warning(f"Failed to connect to remote server: {e}")
            self.remote_socket = None

    def _check_remote_connection(self):
        """
        Check remote connection status and attempt to reconnect if needed.

        Features:
        - Validates remote connection parameters
        - Periodic connection check (60s interval)
        - Automatic reconnection
        - Connection state logging
        - Error handling

        Note:
            Remote connection is optional and skipped if host/port not specified
        """
        # Skip if remote connection not configured
        if not all([self.remote_host, self.remote_port]):
            return

        try:
            current_time = time.time()

            if current_time - self.last_remote_check >= self.remote_check_interval:
                self.last_remote_check = current_time

                if not self.remote_socket:
                    self._connect_to_remote()
                else:
                    try:
                        # Test connection with keepalive
                        self.remote_socket.send(b'\n')
                    except Exception as e:
                        self.logger.warning(f"Remote connection test failed: {e}")
                        self.remote_socket = None
                        # Connection lost, will reconnect on next check

        except Exception as e:
            self.logger.error(f"Error checking remote connection: {e}")
            if self.remote_socket:
                try:
                    self.remote_socket.close()
                except:
                    pass
                self.remote_socket = None

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
            time.sleep(0.5)

            self.ser.setDTR(False)
            self.ser.setRTS(False)
            time.sleep(0.25)
            self.ser.setDTR(True)
            self.ser.setRTS(True)
            time.sleep(0.25)

            if not self.skip_init:
                retry_count = 10
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
                self._check_device_mode()

            self.logger.info(f"Serial port {self.serial_port} initialized successfully")

        except Exception as e:
            self.logger.error(f"Failed to initialize serial port: {e}")
            raise

    def _read_response(self) -> Optional[bytes]:
        """Read response from device with timeout."""
        buffer = b''
        timeout = time.time() + 2.0  # 2 second timeout

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
            self._serial_parse_buffer = bytearray()

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
#            (b'\xFF', "Reset all"),             # #FF-
            (b'\x00', "Version check"),         # #00-
            (b'\x43\x00', "Stop reception"),    # #43-00-
            (b'\x51\x01\x00', "Set mode"),      # #51-01-00-
            (b'\x37\x03', "Set filter"),        # #37-03-
            (b'\x43\x00', "Status check 1"),    # #43-00-
            (b'\x43\x00', "Status check 2"),    # #43-00-
            (b'\x51\x00\x00', "Reset mode"),    # #51-00-00-
            (b'\x37\x03', "Set filter again"),  # #37-03-
            (b'\x43\x00', "Status check 3"),    # #43-00-
            (b'\x38', "Start reception"),       # #38-
            (b'\x43\x02', "Enable data")        # #43-02-
        ]

        for cmd, desc in commands:
            self.logger.debug(f"Sending {desc}: {cmd.hex()}")
            formatted_cmd = self.format_command(cmd)
            self.ser.write(formatted_cmd)
            time.sleep(1.9)

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
                return resp_bytes[0] == 0x00
            elif cmd[0] == 0x43:  # Stop/Status commands
                return resp_bytes[0] == 0x43 and resp_bytes[1] == cmd[1]
            elif cmd[0] == 0x51:  # Mode commands
                return resp_bytes[0] == 0x51 and resp_bytes[1] == cmd[1]
            elif cmd[0] == 0x37:  # Filter command
                return resp_bytes[0] == 0x37
            elif cmd[0] == 0x38:  # Start reception
                return resp_bytes[0] == 0x38 and resp_bytes[2] == 0x01

            return True
        except Exception as e:
            self.logger.error(f"Error verifying response: {e}")
            return False

    def _check_device_mode(self):
        """Check device."""
        self.ser.write(self.format_command(b'\x43\x02'))
        response = self._read_response()
        if not response or not self.verify_response(b'\x43\x02', response):
            raise Exception("Device check failed")
        return True

    def _process_serial_data(self):
        """Process incoming data from the ADSB device with persistent buffer."""
        try:
            if not self.ser.is_open:
                self.logger.error("Serial port is closed")
                return

            if self.ser.in_waiting:
                self._last_data_time = time.time()
                data = self.ser.read(min(self.ser.in_waiting, 8192))
                self.stats['bytes_received'] += len(data)
                self.stats['bytes_processed'] += len(data)

                for byte in data:
                    byte_val = bytes([byte])

                    if byte_val in b'*#@$%&':
                        if self._serial_parse_buffer and len(self._serial_parse_buffer) > 5:
                            self.logger.debug(f"Incomplete message: {bytes(self._serial_parse_buffer)!r}")
                        self._serial_parse_buffer = bytearray([byte])
                        continue

                    if not self._serial_parse_buffer:
                        continue

                    if byte_val == b';':
                        self._serial_parse_buffer.append(byte)
                        final_msg = bytes(self._serial_parse_buffer)

                        if self.validate_message(final_msg):
                            try:
                                self.message_queue.put_nowait(final_msg + b'\n')
                                self.stats['messages_processed'] += 1
                                self.logger.debug(f"Processed message: {final_msg!r}")
                            except queue.Full:
                                self.stats['messages_dropped'] += 1
                        else:
                            if len(self._serial_parse_buffer) > 3:
                                try:
                                    self.message_queue.put_nowait(final_msg + b'\n')
                                    self.stats['recovered_messages'] += 1
                                    self.logger.debug(f"Recovered partial message: {final_msg!r}")
                                except queue.Full:
                                    self.stats['messages_dropped'] += 1
                            else:
                                self.stats['invalid_messages'] += 1
                        self._serial_parse_buffer = bytearray()
                    else:
                        self._serial_parse_buffer.append(byte)
                        if len(self._serial_parse_buffer) > self.MAX_MESSAGE_LENGTH:
                            self._serial_parse_buffer = self._serial_parse_buffer[-self.MAX_MESSAGE_LENGTH:]
                            self.stats['buffer_truncated'] += 1

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

            self.ser.write(self.format_command(b'\x43\x02'))
            response = self._read_response()

            if not response or not self.verify_response(b'\x43\x02', response):
                self.logger.error("Device not responding to mode check")
                if not self._reconnect():
                    self.logger.error("Failed to reconnect to device")
                    self.running = False
            else:
                self.logger.info("Device responding normally despite no data")
                self._no_data_logged = False
                self._last_data_time = current_time

    def _reconnect(self) -> bool:
        """Attempt to reconnect to the device with exponential backoff."""
        self.logger.info("Attempting to reconnect...")
        delay = self.RECONNECT_DELAY

        for attempt in range(self.MAX_RECONNECT_ATTEMPTS):
            if not self.running:
                return False

            try:
                self.logger.info(f"Reconnection attempt {attempt + 1}/{self.MAX_RECONNECT_ATTEMPTS} (delay: {delay}s)")

                if hasattr(self, 'ser') and self.ser.is_open:
                    self.ser.close()

                time.sleep(delay)
                if not self.running:
                    return False

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
                        self._serial_parse_buffer = bytearray()
                        return True
                    time.sleep(0.1)

            except Exception as e:
                self.logger.error(f"Reconnection attempt {attempt + 1} failed: {e}")

            # Exponential backoff with cap
            delay = min(delay * 2, self.MAX_RECONNECT_DELAY)

        self.logger.error(f"Failed to reconnect after {self.MAX_RECONNECT_ATTEMPTS} attempts")
        return False

    def _check_sync_state(self):
        """Check and maintain synchronization state."""
        self._last_sync_time = time.time()

    def shutdown(self):
        """Gracefully shutdown the device."""
        try:
            # Stop reception
            self.ser.write(self.format_command(b'\x43\x00'))
            time.sleep(1.0)

            # Send FF command
            self.ser.write(b'#FF-\r')
            time.sleep(0.5)

            self.ser.close()
        except Exception as e:
            self.logger.error(f"Error during shutdown: {e}")

    def _update_stats(self):
        """
        Update and log periodic statistics with enhanced metrics.

        Features:
        - Messages per minute calculation
        - Data throughput in KB/s
        - Memory usage monitoring
        - Queue utilization
        - Error rate calculation
        - Client connection stats
        """
        try:
            current_time = time.time()
            if current_time - self.last_stats_update >= self.stats_interval:
                # Calculate message rate
                messages_per_minute = (self.stats['messages_processed'] -
                                     self.stats['last_minute_count'])

                # Calculate data throughput
                bytes_per_minute = self.stats['bytes_processed'] - \
                                  getattr(self, '_last_bytes_processed', 0)
                kb_per_minute = bytes_per_minute / 1024  # Convert to KB

                # Calculate error rate
                total_messages = max(1, self.stats['messages_processed'])
                error_rate = (self.stats['errors'] / total_messages) * 100

                # Calculate queue utilization
                queue_size = self.message_queue.qsize()
                queue_capacity = self.message_queue.maxsize
                queue_utilization = (queue_size / queue_capacity) * 100 if queue_capacity > 0 else 0

                # Get process memory usage
                try:
                    import psutil
                    process = psutil.Process()
                    memory_mb = process.memory_info().rss / 1024 / 1024
                except ImportError:
                    memory_mb = 0

                # Format uptime
                uptime = int(current_time - self.stats['start_time'])
                hours = uptime // 3600
                minutes = (uptime % 3600) // 60
                seconds = uptime % 60

                self.logger.info(
                    f"Statistics:\n"
                    f"  Uptime: {hours:02d}:{minutes:02d}:{seconds:02d}\n"
                    f"  Messages/min: {messages_per_minute}\n"
                    f"  Data rate: {kb_per_minute:.2f} KB/min\n"
                    f"  Total messages: {self.stats['messages_processed']}\n"
                    f"  Total data: {self.stats['bytes_processed']/1024:.2f} KB\n"
                    f"  Dropped messages: {self.stats['messages_dropped']}\n"
                    f"  Invalid messages: {self.stats['invalid_messages']}\n"
                    f"  Error rate: {error_rate:.2f}%\n"
                    f"  Memory usage: {memory_mb:.1f} MB\n"
                    f"  Connected clients: {self.stats['clients_current']}\n"
                    f"  Queue: {queue_size}/{queue_capacity} ({queue_utilization:.1f}%)"
                )

                # Update counters for next interval
                self.stats['messages_per_minute'] = messages_per_minute
                self.stats['last_minute_count'] = self.stats['messages_processed']
                self._last_bytes_processed = self.stats['bytes_processed']
                self.last_stats_update = current_time

        except Exception as e:
            self.logger.error(f"Error updating statistics: {e}")

    def _accept_new_client(self):
        """Accept new client connection with limit enforcement."""
        try:
            client_socket, address = self.server_socket.accept()

            if len(self.clients) >= self.MAX_CLIENTS:
                self.logger.warning(f"Max clients ({self.MAX_CLIENTS}) reached, rejecting {address}")
                client_socket.close()
                return

            self._configure_client_socket(client_socket)
            self.clients.append(client_socket)
            self.stats['clients_total'] += 1
            self.stats['clients_current'] = len(self.clients)
            self.logger.info(f"New client connected from {address} ({len(self.clients)}/{self.MAX_CLIENTS})")
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

    def _escape_beast_data(self, data: bytes) -> bytes:
        """Apply Beast format escape sequences (doubles 0x1A bytes in data)."""
        try:
            escaped = bytearray()
            for byte in data:
                if byte == BeastFormat.ESCAPE:
                    escaped.extend([BeastFormat.ESCAPE, BeastFormat.ESCAPE])
                else:
                    escaped.append(byte)
            return bytes(escaped)
        except Exception as e:
            self.logger.error(f"Beast data escape error: {e}")
            return data

    def _create_beast_message(self, msg_type: int, data: bytes, timestamp: bytes = None) -> bytes:
        """
        Create Beast format message with proper framing and escaping.

        Args:
            msg_type: Message type (0x31-0x33)
            data: Raw message data
            timestamp: Optional 6-byte timestamp

        Returns:
            Complete Beast message
        """
        try:
            if timestamp is None:
                timestamp = self.timestamp_gen.get_timestamp()

            # Create message with initial escape
            message = bytearray([BeastFormat.ESCAPE])  # Start marker

            # Build data portion
            data_portion = bytearray()
            data_portion.append(msg_type)  # Type
            data_portion.extend(timestamp)  # 6 byte timestamp
            data_portion.append(0xFF)  # Signal level
            data_portion.extend(data)  # ADS-B data

            # Calculate CRC on data portion
            crc = CRC24.compute(data_portion)
            data_portion.append(crc)

            # Add escaped data to message
            message.extend(self._escape_beast_data(data_portion))

            self.logger.debug(f"Created Beast message: {message.hex().upper()}")
            return bytes(message)

        except Exception as e:
            self.logger.error(f"Beast message creation error: {e}")
            return None

    def _convert_to_beast(self, message: bytes) -> Optional[bytes]:
        """Convert raw ADS-B message to Beast format using unified creation path."""
        try:
            raw_data = message[1:-1].rstrip(b';\n')
            try:
                data = bytes.fromhex(raw_data.decode())
            except ValueError as e:
                self.logger.debug(f"Hex conversion error: {e}")
                return None

            if len(data) == BeastFormat.MODES_SHORT_LEN:
                msg_type = BeastFormat.TYPE_MODES_SHORT
            elif len(data) == BeastFormat.MODES_LONG_LEN:
                msg_type = BeastFormat.TYPE_MODES_LONG
            elif len(data) == BeastFormat.MODEA_LEN:
                msg_type = BeastFormat.TYPE_MODEA
            else:
                self.logger.debug(f"Unsupported data length: {len(data)}")
                return None

            return self._create_beast_message(msg_type, data)

        except Exception as e:
            self.logger.error(f"Beast conversion error: {e}")
            return None

    def _broadcast_message(self, data: bytes):
        """
        Broadcast Beast format message to all connected clients.

        Features:
        - Message validation
        - Performance monitoring
        - Client connection management
        - Remote server support
        - Delay detection

        Args:
            data: Message to broadcast (raw or Beast format)
        """
        try:
            if not data.startswith(bytes([BeastFormat.ESCAPE])):
                beast_msg = self._convert_to_beast(data)
                if not beast_msg:
                    return
            else:
                beast_msg = data

            current_time = time.time()

            writable = []
            try:
                _, writable, _ = select.select([], self.clients, [], 0)
            except select.error:
                pass

            disconnected = []
            for client in writable:
                try:
                    client.send(beast_msg)
                    self.client_last_active[client] = current_time
                except Exception as e:
                    disconnected.append(client)

            if self.remote_socket:
                try:
                    self.remote_socket.send(beast_msg)
                except Exception as e:
                    self.remote_socket = None

            for client in disconnected:
                self._remove_client(client)

        except Exception as e:
            self.logger.error(f"Broadcast error: {e}")

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
        """Check for inactive clients and remove them."""
        current_time = time.time()
        for client in list(self.clients):
            if current_time - self.client_last_active.get(client, 0) > self.NO_DATA_TIMEOUT:
                self.logger.warning("Closing inactive client")
                self._remove_client(client)

    def validate_message(self, message: bytes) -> bool:
        """
        Validate raw ADS-B message format and content.
        """
        try:
            if len(message) < 3:
                return False

            if not message.startswith((b'*', b'#', b'@')):
                return False

            if not message.endswith(b';'):
                return False

            if message.startswith(b'*'):
                content = message[1:-1]
                try:
                    hex_data = content.decode()
                    # Convert to check validity
                    data = bytes.fromhex(hex_data)

                    # Check supported lengths
                    if len(data) in (BeastFormat.MODEA_LEN,
                                   BeastFormat.MODES_SHORT_LEN,
                                   BeastFormat.MODES_LONG_LEN):
                        return True

                    self.logger.debug(f"Unsupported message length: {len(data)} bytes")
                    return False

                except ValueError:
                    return False

            return True

        except Exception as e:
            self.logger.error(f"Message validation error: {e}")
            return False

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals."""
        self.logger.info(f"Received signal {signum}, shutting down...")
        self.running = False

    def self_test(self) -> bool:
        """
        Perform self-test of CRC implementation using known test vectors.

        Returns:
            True if all tests pass, False otherwise
        """
        test_vectors = [
            # Test vectors from Mode-S specification
            ("8D406B902015A678D4D220AA4BDA", True),    # Example from docs, should have remainder 0
            ("8D4CA251204994B1C36E60A5343D", False),   # Example from docs, should have remainder 16
            # Additional known valid messages
            ("8D152000004F90000000002D82E0", True),    # Valid DF17 message
            ("8D4840D6202CC371C32CE0576098", True),    # Valid position message
        ]

        self.logger.info("Running CRC self-test...")
        failed = False

        for hex_msg, expected_valid in test_vectors:
            try:
                self.logger.debug(f"Testing message: {hex_msg}")

                # Verify message format
                if len(hex_msg) != 28:  # 112 bits = 28 hex chars
                    self.logger.error(f"Invalid message length: {len(hex_msg)} chars")
                    failed = True
                    continue

                # Verify using pyModeS
                remainder = pms.common.crc(hex_msg)
                is_valid = remainder == 0

                self.logger.debug(f"CRC remainder: {remainder}")
                self.logger.debug(f"Message valid: {is_valid}")

                if is_valid != expected_valid:
                    self.logger.error(
                        f"CRC test failed for {hex_msg}:\n"
                        f"  Remainder: {remainder}\n"
                        f"  Is valid: {is_valid}\n"
                        f"  Expected valid: {expected_valid}"
                    )
                    failed = True
                    continue

                # Additional validation
                if expected_valid:
                    df = pms.df(hex_msg)
                    if df != 17:  # All our test messages should be DF17 (ADS-B)
                        self.logger.error(f"Invalid DF: {df} for message: {hex_msg}")
                        failed = True
                        continue

                self.logger.debug(f"Test passed for message: {hex_msg}")

            except Exception as e:
                self.logger.error(f"Test error for {hex_msg}: {e}")
                return False

        if failed:
            return False

        self.logger.info("All CRC tests passed successfully")
        return True

    def check_health(self) -> bool:
        """
        Check if multiplexer is healthy.

        Performs basic health checks appropriate for ADS-B receiver operation:
        - Serial port status
        - Data reception within 4 hours (accommodating low traffic periods)
        - Basic message processing verification
        - System operational status

        Returns:
            bool: True if system is operating normally, False if issues detected

        Note:
            Long periods without data are normal for ADS-B receivers in low traffic areas,
            so timeout is set to 4 hours to avoid false positives.
        """
        try:
            # Check if serial port is open and system is running
            if not hasattr(self, 'ser') or not self.ser.is_open:
                self.logger.error("Health check failed: Serial port not open")
                return False

            # Check last data received time (4 hours timeout)
            current_time = time.time()
            data_gap = current_time - self._last_data_time
            if data_gap > 14400:  # 4 hours in seconds
                self.logger.error(f"Health check failed: No data received for {data_gap/3600:.1f} hours")
                return False

            # Check if system is processing messages at all
            uptime = current_time - self.stats['start_time']
            if self.stats['messages_processed'] == 0 and uptime > 300:
                # Only fail if no messages after 5 minutes of startup
                self.logger.error(f"Health check failed: No messages processed in {uptime:.1f} seconds since startup")
                return False

            self.logger.debug(
                f"Health check passed: Uptime={uptime:.1f}s, "
                f"Last data={data_gap:.1f}s ago, "
                f"Messages={self.stats['messages_processed']}"
            )
            return True

        except Exception as e:
            self.logger.error(f"Health check error: {e}")
            return False

    def run(self):
        """
        Main operation loop.

        Features:
        - Serial data processing
        - Client connection handling
        - Message broadcasting
        - Statistics updates
        - Optional remote connection
        - Error recovery
        """
        self.logger.info("Starting multiplexer...")
        last_sync_check = time.time()
        last_heartbeat = time.time()

        # Initialize remote connection if configured
        if all([self.remote_host, self.remote_port]):
            self._connect_to_remote()
            self.logger.info(f"Remote connection enabled: {self.remote_host}:{self.remote_port}")
        else:
            self.logger.info("Remote connection disabled")

        try:
            while self.running:
                try:
                    current_time = time.time()

                    # Send heartbeat if needed
                    if current_time - last_heartbeat >= self.HEARTBEAT_INTERVAL:
                        self._send_heartbeat()
                        last_heartbeat = current_time

                    self._process_serial_data()

                    # Check remote connection only if configured
                    if all([self.remote_host, self.remote_port]):
                        self._check_remote_connection()

                    # Prepare sockets for select()
                    sockets_to_read = [self.server_socket] + self.clients
                    if self.remote_socket:
                        sockets_to_read.append(self.remote_socket)

                    try:
                        readable, _, _ = select.select(sockets_to_read, [], [], 0.1)
                        for sock in readable:
                            if sock is self.server_socket:
                                self._accept_new_client()
                            elif sock is self.remote_socket:
                                try:
                                    data = sock.recv(1024)
                                    if not data:
                                        self.logger.warning("Remote server disconnected")
                                        self.remote_socket = None
                                    else:
                                        # Process remote data if needed
                                        pass
                                except Exception as e:
                                    self.logger.error(f"Error receiving from remote: {e}")
                                    self.remote_socket = None
                            else:
                                self._handle_client_data(sock)
                    except select.error:
                        pass

                    # Process message queue
                    while not self.message_queue.empty():
                        try:
                            message = self.message_queue.get_nowait()
                            self._broadcast_message(message)
                        except queue.Empty:
                            break

                    # Periodic checks
                    current_time = time.time()
                    if current_time - last_sync_check >= self.SYNC_CHECK_INTERVAL:
                        self._check_sync_state()
                        last_sync_check = current_time

                    self._update_stats()
                    self._check_timeouts()
                    self._check_device_status()

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

    parser = argparse.ArgumentParser(
        description='PicADSB Multiplexer',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )

    # Required arguments
    parser.add_argument(
        '--port',
        type=int,
        default=30002,
        help='Local TCP port to listen on'
    )

    parser.add_argument(
        '--serial',
        default='/dev/ttyACM0',
        help='Serial port device path'
    )

    # Optional arguments
    parser.add_argument(
        '--log-level',
        default='INFO',
        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
        help='Logging verbosity level'
    )

    parser.add_argument(
        '--no-init',
        action='store_true',
        help='Skip device initialization sequence'
    )

    # Remote connection group
    remote_group = parser.add_argument_group('Remote connection (optional)')
    remote_group.add_argument(
        '--remote-host',
        metavar='HOST',
        help='Remote server hostname or IP'
    )

    remote_group.add_argument(
        '--remote-port',
        metavar='PORT',
        type=int,
        help='Remote server port number'
    )

    args = parser.parse_args()

    # Validate remote connection arguments
    if bool(args.remote_host) != bool(args.remote_port):
        parser.error("Both --remote-host and --remote-port must be specified together")

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
