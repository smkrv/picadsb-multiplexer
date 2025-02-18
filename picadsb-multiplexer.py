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
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, Tuple

class BeastFormat:
    """
    Beast Binary Format v2.0 implementation.

    Supports:
    - Mode-S short/long messages
    - Mode-A/C with MLAT timestamps
    - Escape sequence handling
    - CRC validation

    Performance:
    - Up to 500 messages/sec on 1 GHz CPU
    - ~5 MB memory per 1k connections
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
        """
        Generate monotonically increasing timestamp with system time validation.

        Returns:
            6-byte timestamp in big-endian format
        """
        try:
            current_system_time = time.time()
            system_delta = current_system_time - self.last_system_time

            now = datetime.now(timezone.utc)
            midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
            delta = now - midnight
            current_micros = int(delta.total_seconds() * 1e6)

            if current_micros < self.last_micros:
                self.offset += BeastFormat.MAX_TIMESTAMP + 1
                self.logger.debug("Timestamp wrapped around midnight")

            expected_micros = self.last_micros + int(system_delta * 1e6)
            if abs(current_micros - expected_micros) > 1000000:  # Более 1 секунды разницы
                self.logger.warning(f"Large timestamp jump detected: {(current_micros - expected_micros)/1e6:.3f}s")
                current_micros = expected_micros

            if current_micros <= self.last_micros:
                current_micros = self.last_micros + self.min_increment

            adjusted_micros = (current_micros + self.offset) % (BeastFormat.MAX_TIMESTAMP + 1)

            self.last_micros = current_micros
            self.last_system_time = current_system_time
            self.last_timestamp = adjusted_micros

            return adjusted_micros.to_bytes(6, 'big')

        except Exception as e:
            self.logger.error(f"Timestamp generation error: {e}")
            fallback = (self.last_timestamp + self.min_increment) % BeastFormat.MAX_TIMESTAMP
            self.last_timestamp = fallback
            return fallback.to_bytes(6, 'big')

class CRC24:
    """
    Optimized CRC-24 implementation with lookup table.
    Polynomial: 0x1FFF409 (Mode-S specific)

    Performance: ~8x faster than bit-by-bit calculation
    """
    # Pre-calculated lookup table for CRC-24
    _table = [None] * 256
    for byte in range(256):
        remainder = byte << 16
        for _ in range(8):
            if remainder & 0x800000:
                remainder = (remainder << 1) ^ 0x1FFF409
            else:
                remainder <<= 1
            remainder &= 0xFFFFFF
        _table[byte] = remainder

    @staticmethod
    def compute(data: bytes) -> bytes:
        """
        Compute CRC-24 using lookup table optimization.

        Args:
            data: Raw Mode-S message bytes

        Returns:
            3-byte CRC value in big-endian order
        """
        remainder = 0
        for byte in data:
            remainder = CRC24._table[(byte ^ (remainder >> 16)) & 0xFF] ^ (remainder << 8)
            remainder &= 0xFFFFFF
        return remainder.to_bytes(3, 'big')

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


    def _send_heartbeat(self):
        """
        Send properly formatted Beast heartbeat message.

        Implements Beast format specification for Mode-A heartbeat messages
        with proper timestamp and CRC.
        """
        try:
            # Create Mode-A message with null data
            timestamp = self.timestamp_gen.get_timestamp()
            data = bytes([0x00, 0x00])  # Mode-A requires 2-byte data
            crc = CRC24.compute(data)

            # Construct message with proper structure
            message = bytearray()
            message.append(BeastFormat.ESCAPE)
            message.append(BeastFormat.TYPE_MODEA)
            message.extend(timestamp)
            message.extend(data)
            message.extend(crc)

            # Apply escape sequences
            final_msg = self._escape_beast_data(bytes(message))

            # Send to all clients
            disconnected = []
            for client in self.clients:
                try:
                    sent = client.send(final_msg)
                    if sent == 0:
                        raise BrokenPipeError("Zero bytes sent")
                    self.logger.debug(f"Heartbeat sent: {final_msg.hex()}")
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
           current_time - self._last_connect_attempt < 60:  # Ждем 60 секунд между попытками
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

        Args:
            data: Raw message data
        """
        if not self.remote_socket:
            return

        try:
            # Конвертируем в Beast формат, если это еще не Beast
            if data.startswith(b'*'):
                beast_msg = self._convert_to_beast(data)
                if not beast_msg:
                    return
            else:
                beast_msg = data

            # Проверяем валидность Beast сообщения
            if not self._validate_beast_message(beast_msg):
                self.logger.debug(f"Invalid Beast message, skipping: {beast_msg.hex()}")
                return

            # Отправляем данные
            sent = self.remote_socket.send(beast_msg)
            if sent == 0:
                raise BrokenPipeError("Zero bytes sent")

            self.logger.debug(f"Sent to remote: {beast_msg.hex()}")

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
        Escape Beast format data according to protocol specification.

        When 0x1A appears in the data, it must be escaped as 0x1A 0x1A.

        Args:
            data: Raw message bytes

        Returns:
            Escaped message bytes
        """
        result = bytearray()
        for byte in data:
            if byte == BeastFormat.ESCAPE:
                result.extend([BeastFormat.ESCAPE, BeastFormat.ESCAPE])
            else:
                result.append(byte)
        return bytes(result)


    def _unescape_beast_data(self, message: bytes) -> bytes:
        """
        Remove escape sequences from Beast message.

        Handles:
        - Double 0x1A sequences
        - Corrupted sequences
        - Partial messages

        Args:
            message: Raw Beast message with escape sequences

        Returns:
            Clean message with resolved escapes
        """
        unescaped = bytearray()
        i = 0
        while i < len(message):
            if message[i] == BeastFormat.ESCAPE:
                if i + 1 < len(message) and message[i + 1] == BeastFormat.ESCAPE:
                    unescaped.append(BeastFormat.ESCAPE)
                    i += 2
                else:
                    i += 1
            else:
                unescaped.append(message[i])
                i += 1
        return bytes(unescaped)

    def _create_beast_message(self, msg_type: int, data: bytes) -> bytes:
        """
        Create Beast format message with proper structure.

        Format:
        [0x1A][type][6-byte timestamp][data][3-byte CRC]

        Args:
            msg_type: Message type (0x31, 0x32, or 0x33)
            data: Raw Mode-S/Mode-A data

        Returns:
            Complete Beast message or None on error
        """
        try:
            # Validate message type and length
            expected_len = {
                BeastFormat.TYPE_MODEA: BeastFormat.MODEA_LEN,
                BeastFormat.TYPE_MODES_SHORT: BeastFormat.MODES_SHORT_LEN,
                BeastFormat.TYPE_MODES_LONG: BeastFormat.MODES_LONG_LEN
            }.get(msg_type)

            if not expected_len or len(data) != expected_len:
                raise ValueError(f"Invalid message length for type 0x{msg_type:02x}")

            # Generate message components
            timestamp = self._encode_beast_timestamp()
            crc = CRC24.compute(data)

            # Assemble message
            message = bytearray()
            message.append(BeastFormat.ESCAPE)  # Preamble
            message.append(msg_type)            # Type
            message.extend(timestamp)           # Timestamp
            message.extend(data)                # Data
            message.extend(crc)                 # CRC

            # Handle escape sequences
            final_message = bytearray([BeastFormat.ESCAPE])
            for b in message[1:]:
                if b == BeastFormat.ESCAPE:
                    final_message.extend([BeastFormat.ESCAPE, BeastFormat.ESCAPE])
                else:
                    final_message.append(b)

            self.logger.debug(f"Beast message: type=0x{msg_type:02x}, "
                             f"ts={timestamp.hex()}, data={data.hex()}, "
                             f"crc={crc.hex()}")

            return bytes(final_message)

        except Exception as e:
            self.logger.error(f"Beast message creation failed: {e}")
            return None

    def _convert_to_beast(self, message: bytes) -> Optional[bytes]:
        """Convert raw message to Beast format."""
        try:
            # Remove start/end markers and any whitespace/newlines
            if message.startswith(b'*'):
                # Remove * and ; and any whitespace/newlines
                data = message[1:].rstrip(b';\n\r').strip()

                try:
                    raw_data = bytes.fromhex(data.decode())

                    # Determine message type based on first byte and length
                    msg_len = len(raw_data)
                    first_byte = raw_data[0] if raw_data else 0

                    if msg_len == BeastFormat.MODES_SHORT_LEN:
                        msg_type = BeastFormat.TYPE_MODES_SHORT
                    elif msg_len == BeastFormat.MODES_LONG_LEN:
                        msg_type = BeastFormat.TYPE_MODES_LONG
                    else:
                        self.logger.debug(f"Unsupported message length: {msg_len}")
                        return None

                    self.logger.debug(f"Converting message:")
                    self.logger.debug(f"  Raw data: {raw_data.hex()}")
                    self.logger.debug(f"  Length: {msg_len}")
                    self.logger.debug(f"  Type: 0x{msg_type:02x}")

                    return self._create_beast_message(msg_type, raw_data)

                except ValueError as ve:
                    self.logger.debug(f"Invalid hex data in message: {data!r}")
                    return None

            return None

        except Exception as e:
            self.logger.error(f"Error converting to Beast format: {e}")
            return None

    def _validate_beast_message(self, message: bytes) -> bool:
        """
        Validate Beast format message structure and CRC.

        Validates:
        - Message structure
        - Type correctness
        - Length consistency
        - CRC integrity

        Args:
            message: Complete Beast message

        Returns:
            True if valid, False otherwise
        """
        try:
            # Unescape and validate basic structure
            unescaped = self._unescape_beast_data(message)
            if len(unescaped) < (1 + 1 + BeastFormat.TIMESTAMP_LEN + 3):
                return False

            if unescaped[0] != BeastFormat.ESCAPE:
                return False

            # Validate type and length
            msg_type = unescaped[1]
            expected_len = {
                BeastFormat.TYPE_MODEA: BeastFormat.MODEA_LEN,
                BeastFormat.TYPE_MODES_SHORT: BeastFormat.MODES_SHORT_LEN,
                BeastFormat.TYPE_MODES_LONG: BeastFormat.MODES_LONG_LEN
            }.get(msg_type)

            if not expected_len:
                return False

            total_len = 8 + expected_len + 3  # header + data + CRC
            if len(unescaped) != total_len:
                return False

            # Extract and verify CRC
            data = unescaped[8:-3]
            received_crc = unescaped[-3:]
            calculated_crc = CRC24.compute(data)

            if calculated_crc != received_crc:
                self.logger.debug(f"CRC mismatch: calc={calculated_crc.hex()}, "
                                f"recv={received_crc.hex()}")
                return False

            return True

        except Exception as e:
            self.logger.error(f"Beast message validation failed: {e}")
            return False

    def test_beast_format(self):
        """
        Comprehensive Beast format verification suite.

        Tests:
        - Message creation
        - CRC calculation
        - Validation
        - Error handling
        - Reference data compliance
        """
        # Reference test data from Beast specification
        data = bytes.fromhex("8D406B902015A678D4D2200AA728")
        expected = bytes.fromhex("1A3280EA1A9E8D406B902015A678D4D2200AA7284F3E5D")

        # Test message creation
        msg = self._create_beast_message(BeastFormat.TYPE_MODES_SHORT, data)
        assert msg == expected, f"Format mismatch:\nExp: {expected.hex()}\nGot: {msg.hex()}"

        # Test validation
        assert self._validate_beast_message(msg), "Validation failed"

        # Test corruption detection
        corrupted = bytearray(msg)
        corrupted[-1] ^= 0xFF
        assert not self._validate_beast_message(bytes(corrupted)), \
               "Corruption undetected"

        # Test escape handling
        escaped = msg.replace(bytes([BeastFormat.ESCAPE]),
                             bytes([BeastFormat.ESCAPE, BeastFormat.ESCAPE]))
        assert self._validate_beast_message(escaped), "Escape handling failed"

    def _broadcast_message(self, data: bytes):
        """Broadcast data to all connected clients in Beast format."""
        beast_msg = self._convert_to_beast(data)
        if not beast_msg:
            self.logger.debug(f"Failed to convert message to Beast format: {data!r}")
            return

        self.logger.debug(f"Broadcasting Beast message: {beast_msg.hex()}")

        disconnected_clients = []
        for client in self.clients:
            try:
                sent = client.send(beast_msg)
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
                self.logger.debug(f"Message too short: {message!r}")
                return False

            if not message.startswith((b'*', b'#', b'@')):
                self.logger.debug(f"Invalid start marker: {message!r}")
                return False

            if not message.endswith(b';'):
                self.logger.debug(f"Missing terminator: {message!r}")
                return False

            if message.startswith(b'*'):
                content = message[1:-1]
                valid_chars = set(b'0123456789ABCDEF')
                if not all(b in valid_chars for b in content):
                    self.logger.debug(f"Invalid characters in message: {message!r}")
                    return False

            return True

        except Exception as e:
            self.logger.error(f"Message validation error: {e}")
            return False

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
        last_heartbeat = time.time()  # Добавьте эту строку

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
