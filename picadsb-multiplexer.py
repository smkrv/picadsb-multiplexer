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
    KEEPALIVE_INTERVAL = 30
    SERIAL_BUFFER_SIZE = 131072
    MAX_MESSAGE_LENGTH = 256
    NO_DATA_TIMEOUT = 600
    MODE_CHECK_TIMEOUT = 300
    MAX_RECONNECT_ATTEMPTS = 999
    RECONNECT_DELAY = 10
    SYNC_CHECK_INTERVAL = 1
    RESET_TIMEOUT = 5
    KEEPALIVE_MARKER = b'\n'

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
        self.firmware_version = None
        self.device_id = None
        self._buffer = b''
        self._last_data_time = time.time()
        self._no_data_logged = False
        self._sync_state = True
        self._last_sync_time = time.time()
        # Add heartbeat configuration
        self.HEARTBEAT_INTERVAL = 30  # 30 seconds
        self.last_heartbeat = time.time()

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
        self.last_mode_check = time.time()
        self.mode_check_interval = 300
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

        Message structure:
        - 0x1A (escape)
        - 0x31 (Mode-A type)
        - 6 bytes timestamp
        - 1 byte signal level (0xFF)
        - 2 bytes Mode-A data (0x00, 0x00)
        - 1 byte CRC
        """
        try:
            # Get current timestamp
            timestamp = self.timestamp_gen.get_timestamp()

            # Create message components
            msg_type = BeastFormat.TYPE_MODEA
            signal_level = 0xFF
            data = bytes([0x00, 0x00])  # Null Mode-A data

            # Construct message for CRC computation
            msg_for_crc = bytearray()
            msg_for_crc.append(msg_type)
            msg_for_crc.extend(timestamp)
            msg_for_crc.append(signal_level)
            msg_for_crc.extend(data)

            # Compute CRC
            crc = CRC24.compute(bytes(msg_for_crc))

            # Construct complete message
            message = bytearray()
            message.append(BeastFormat.ESCAPE)
            message.extend(msg_for_crc)
            message.append(crc)  # Add single CRC byte

            # Apply escape sequences
            final_msg = self._escape_beast_data(bytes(message))

            # Log the message in debug mode
            self.logger.debug(f"Heartbeat message: {final_msg.hex().upper()}")

            # Send to all clients
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

            # Send to remote if configured
            if self.remote_socket:
                try:
                    self.remote_socket.send(final_msg)
                except Exception as e:
                    self.logger.error(f"Remote heartbeat failed: {e}")
                    self.remote_socket = None

            self.last_heartbeat = time.time()

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
        if hasattr(self, '_last_connect_attempt') and \
           current_time - self._last_connect_attempt < 60:
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

    def _send_to_remote(self, data: bytes):
        """
        Send data to remote server in Beast format.
        """
        if not self.remote_socket:
            return

        try:
            sent = self.remote_socket.send(data)
            if sent == 0:
                raise BrokenPipeError("Zero bytes sent")

            self.logger.debug(f"Sent to remote: {data.hex()}")

        except Exception as e:
            self.logger.error(f"Error sending to remote server: {e}")
            self.remote_socket = None
            # Try to reconnect
            self._connect_to_remote()

    def _check_remote_connection(self):
        """
        Check remote connection status and attempt to reconnect if needed.
        This check runs every 60 seconds to ensure stable remote connection.
        """
        current_time = time.time()

        if self.remote_host and self.remote_port:
            if current_time - self.last_remote_check >= 60:
                self.last_remote_check = current_time

                if not self.remote_socket:
                    self._connect_to_remote()
                else:
                    try:
                        self.remote_socket.send(b'\n')
                    except Exception as e:
                        self.logger.warning(f"Remote connection test failed: {e}")
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
        """Process incoming data from the ADSB device with relaxed validation."""
        try:
            # Check if serial port is available
            if not self.ser.is_open:
                self.logger.error("Serial port is closed")
                return

            # Check for available data
            if self.ser.in_waiting:
                self._last_data_time = time.time()
                data = self.ser.read(self.ser.in_waiting)
                self.stats['bytes_received'] += len(data)

                for byte in data:
                    byte = bytes([byte])

                    # Extended set of message start markers
                    if byte in b'*#@$%&':
                        # Only log really long incomplete messages
                        if self._buffer and len(self._buffer) > 5:
                            self.logger.debug(f"Incomplete message: {self._buffer!r}")
                        self._buffer = byte
                        continue

                    # Skip bytes until start marker
                    if not self._buffer:
                        continue

                    # Message terminator handling
                    if byte == b';':
                        self._buffer += byte

                        # Try to process the message even if validation fails
                        if self.validate_message(self._buffer):
                            try:
                                self.message_queue.put_nowait(self._buffer + b'\n')
                                self.stats['messages_processed'] += 1
                                self.logger.debug(f"Processed message: {self._buffer!r}")
                            except queue.Full:
                                self.stats['messages_dropped'] += 1
                        else:
                            # Attempt to recover partial message if it's long enough
                            if len(self._buffer) > 3:
                                try:
                                    self.message_queue.put_nowait(self._buffer + b'\n')
                                    self.stats['recovered_messages'] += 1
                                    self.logger.debug(f"Recovered partial message: {self._buffer!r}")
                                except queue.Full:
                                    self.stats['messages_dropped'] += 1
                            else:
                                self.stats['invalid_messages'] += 1
                        self._buffer = b''
                    else:
                        self._buffer += byte
                        # Handle buffer overflow by keeping the last part
                        if len(self._buffer) > self.MAX_MESSAGE_LENGTH:
                            self._buffer = self._buffer[-self.MAX_MESSAGE_LENGTH:]
                            self.stats['buffer_truncated'] += 1

                # Handle timeout for incomplete messages
                if self._buffer and time.time() - self._last_data_time > 1.0:
                    if len(self._buffer) > 3:
                        self._buffer += b';'  # Add terminator
                        try:
                            self.message_queue.put_nowait(self._buffer + b'\n')
                            self.stats['timeout_processed'] += 1
                            self.logger.debug(f"Timeout processed message: {self._buffer!r}")
                        except queue.Full:
                            self.stats['messages_dropped'] += 1
                    self._buffer = b''

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

    def _encode_beast_timestamp(self) -> bytes:
        """
        Generate 6-byte MLAT timestamp for Beast format.

        Features:
        - Microsecond precision
        - Handles 7h58m overflow
        - UTC midnight-based

        Returns:
            6-byte timestamp in big-endian order
        """
        now = datetime.utcnow().replace(tzinfo=timezone.utc)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elapsed = (now - midnight).total_seconds()
        microseconds = int(elapsed * 1e6) % BeastFormat.MAX_TIMESTAMP
        return microseconds.to_bytes(6, 'big')

    def _escape_beast_data(self, data: bytes) -> bytes:
        """
        Apply Beast format escape sequences.

        Escapes 0x1A bytes by doubling them according to Beast protocol specification.

        Args:
            data: Raw message bytes

        Returns:
            Escaped message bytes
        """
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
            return data  # Return original data on error

    def _unescape_beast_data(self, data: bytes) -> bytes:
        """
        Remove Beast format escape sequences.

        Args:
            data: Raw Beast message bytes

        Returns:
            Unescaped message bytes
        """
        unescaped_data = bytearray()
        i = 0
        while i < len(data):
            if data[i] == BeastFormat.ESCAPE:
                if i + 1 < len(data) and data[i + 1] == BeastFormat.ESCAPE:
                    unescaped_data.append(BeastFormat.ESCAPE)
                    i += 2
                else:
                    self.logger.debug("Invalid escape sequence")
                    return b''
            else:
                unescaped_data.append(data[i])
                i += 1

        return bytes(unescaped_data)

    def _create_beast_message(self, msg_type: int, data: bytes, timestamp: bytes = None) -> bytes:
        try:
            timestamp = self.timestamp_gen.get_timestamp() if timestamp is None else timestamp

            crc_input = bytes([msg_type]) + timestamp + data
            crc = CRC24.compute(crc_input)

            message = bytearray()
            message.append(BeastFormat.ESCAPE)
            message.append(msg_type)
            message.extend(timestamp)
            message.extend(data)
            message.extend(crc)

            # Apply escape sequences
            escaped_message = self._escape_beast_data(bytes(message))

            # Log the final message
            self.logger.debug(f"Final Beast message: {escaped_message.hex().upper()}")

            return escaped_message

        except Exception as e:
            self.logger.error(f"Beast creation error: {e}")
            return None

    def _convert_to_beast(self, message: bytes) -> Optional[bytes]:
        """
        Convert raw ADS-B message to Beast format with single-byte CRC.
        """
        try:
            # Remove markers and convert to bytes
            raw_data = message[1:-1].decode().strip(';')

            # Convert to bytes
            try:
                data = bytes.fromhex(raw_data)
            except ValueError as e:
                self.logger.debug(f"Hex conversion error: {e}")
                return None

            # Get timestamp and signal level
            timestamp = self.timestamp_gen.get_timestamp()
            signal_level = bytes([0xFF])

            # Determine message type
            if len(data) == 7:  # Mode-S short
                msg_type = BeastFormat.TYPE_MODES_SHORT
            elif len(data) == 14:  # Mode-S long
                msg_type = BeastFormat.TYPE_MODES_LONG
            else:
                self.logger.debug(f"Unsupported data length: {len(data)}")
                return None

            # Build message
            message = bytearray()
            message.append(BeastFormat.ESCAPE)  # Start marker
            message.append(msg_type)  # Message type
            message.extend(timestamp)  # 6 bytes timestamp
            message.append(0xFF)  # Signal level
            message.extend(data)  # ADS-B data

            # Calculate single-byte CRC
            crc = CRC24.compute(bytes(message[1:]))  # CRC of everything after escape
            message.append(crc)  # Add CRC byte

            # Apply escape sequences
            final_msg = self._escape_beast_data(bytes(message))

            self.logger.debug(f"Converted to Beast: {final_msg.hex().upper()}")
            return final_msg

        except Exception as e:
            self.logger.error(f"Beast conversion error: {e}")
            return None

    def _validate_beast_message(self, message: bytes) -> bool:
        """
        Validate Beast format message structure and integrity. 

        Args:
            message: Complete Beast message with escape sequences

        Returns:
            True if message is valid
        """
        try:
            # Remove escape sequences
            unescaped = self._unescape_beast_data(message)

            # Basic structure checks
            if len(unescaped) < 11:  # Minimum length for any Beast message
                self.logger.debug(f"Message too short after unescaping: {len(unescaped)}")
                return False

            if unescaped[0] != BeastFormat.ESCAPE:
                self.logger.debug("Invalid start byte")
                return False

            # Get message type
            msg_type = unescaped[1]

            # Calculate expected length after unescaping
            if msg_type == BeastFormat.TYPE_MODEA:
                expected_len = 12  # 1(ESC) + 1(type) + 6(timestamp) + 1(signal) + 2(data) + 1(crc)
            elif msg_type == BeastFormat.TYPE_MODES_SHORT:
                expected_len = 17  # 1(ESC) + 1(type) + 6(timestamp) + 1(signal) + 7(data) + 1(crc)
            elif msg_type == BeastFormat.TYPE_MODES_LONG:
                expected_len = 24  # 1(ESC) + 1(type) + 6(timestamp) + 1(signal) + 14(data) + 1(crc)
            else:
                self.logger.debug(f"Invalid message type: 0x{msg_type:02X}")
                return False

            if len(unescaped) != expected_len:
                self.logger.debug(f"Invalid unescaped length for type 0x{msg_type:02X}: {len(unescaped)}, expected {expected_len}")
                return False

            # Verify CRC
            return CRC24.verify(unescaped[1:], debug=True)  # Skip escape byte for CRC verification

        except Exception as e:
            self.logger.error(f"Beast message validation error: {e}")
            return False

    def _validate_message_length(self, msg_type: int, data: bytes):
        """
        Validate message length based on type.

        Args:
            msg_type: Message type (MODE_S_SHORT or MODE_S_LONG)
            data: Raw message data

        Raises:
            ValueError: If message length is invalid
        """
        if msg_type == BeastFormat.TYPE_MODES_LONG and len(data) != 14:
            raise ValueError(f"Invalid long message length: {len(data)}")
        elif msg_type == BeastFormat.TYPE_MODES_SHORT and len(data) != 7:
            raise ValueError(f"Invalid short message length: {len(data)}")

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
            # Convert to Beast format if needed
            if data.startswith(bytes([BeastFormat.ESCAPE])):
                beast_msg = data
            else:
                beast_msg = self._convert_to_beast(data)
                if not beast_msg:
                    return

            # Validate Beast message
            if not self._validate_beast_message(beast_msg):
                self.logger.warning(f"Invalid Beast message: {beast_msg.hex()}")
                return

            current_time = time.time()

            # Check broadcast delay
            if hasattr(self, '_last_broadcast_time'):
                delay = current_time - self._last_broadcast_time
                if delay > 1.0:
                    self.logger.warning(f"Large broadcast delay detected: {delay:.3f}s")

            self._last_broadcast_time = current_time

            # Broadcast to clients
            disconnected_clients = []
            for client in self.clients:
                try:
                    sent = client.send(beast_msg)
                    if sent == 0:
                        raise BrokenPipeError("Zero bytes sent")
                    self.client_last_active[client] = current_time
                except Exception as e:
                    self.logger.debug(f"Client broadcast error: {e}")
                    disconnected_clients.append(client)

            # Handle remote server
            if self.remote_socket:
                try:
                    self.remote_socket.send(beast_msg)
                except Exception as e:
                    self.logger.error(f"Remote server error: {e}")
                    self.remote_socket = None

            # Clean up disconnected clients
            for client in disconnected_clients:
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

    def run(self):
        """Main operation loop."""
        self.logger.info("Starting multiplexer...")
        last_sync_check = time.time()
        last_heartbeat = time.time()

        # Connect to remote server if specified
        if self.remote_host and self.remote_port:
            self._connect_to_remote()

        try:
            while self.running:
                try:
                    current_time = time.time()

                    # Send heartbeat if needed
                    if current_time - last_heartbeat >= self.HEARTBEAT_INTERVAL:
                        self._send_heartbeat()
                        last_heartbeat = current_time

                    self._process_serial_data()

                    # Check and maintain remote connection
                    self._check_remote_connection()

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
