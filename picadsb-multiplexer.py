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

    # Constants for keepalive
    KEEPALIVE_MARKER = b'\x00'
    KEEPALIVE_INTERVAL = 30  # seconds

    def validate_message(self, msg: bytes) -> bool:
        """
        Simplified message validation for 1090 MHz signals.
        Only checks for valid hex characters and message length.

        Args:
            msg (bytes): Raw message from receiver

        Returns:
            bool: True if message has valid format
        """
        try:
            # Clean up received message
            hex_msg = msg.decode().strip('*;\r\n')

            # Log incoming message for debugging
            self.logger.debug(f"Validating message: {msg!r}, cleaned: {hex_msg!r}")

            # Skip empty messages
            if not hex_msg:
                self.logger.debug("Empty message rejected")
                return False

            # Check for valid hex characters only
            if not all(c in '0123456789ABCDEFabcdef' for c in hex_msg):
                self.logger.debug(f"Invalid hex characters in message: {hex_msg}")
                return False

            # Verify standard message length (56 or 112 bits)
            if len(hex_msg) not in (14, 28):
                self.logger.debug(f"Invalid length {len(hex_msg)}: {hex_msg}")
                return False

            self.logger.debug(f"Message validated successfully: {hex_msg}")
            return True

        except Exception as e:
            self.logger.debug(f"Validation error: {str(e)}, Message: {msg!r}")
            return False

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
            'messages_dropped': 0
        }

        # Timing controls
        self.last_version_check = time.time()
        self.version_check_interval = 300  # 5 minutes
        self.last_stats_update = time.time()
        self.stats_interval = 60  # 1 minute

        # Message handling
        self.message_queue = queue.Queue(maxsize=5000)
        self.clients: List[socket.socket] = []

        # Client activity tracking
        self.client_last_active = {}  # {socket: last_active_time}

        # Initialize interfaces
        self._init_socket()
        self._init_serial()

        # Setup signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _setup_logging(self, log_level: str):
        """Configure logging with both file and console output."""
        numeric_level = getattr(logging, log_level.upper(), None)
        if not isinstance(numeric_level, int):
            raise ValueError(f'Invalid log level: {log_level}')

        self.logger = logging.getLogger('PicADSB')
        self.logger.setLevel(numeric_level)

        # Create logs directory
        os.makedirs('logs', exist_ok=True)

        # File handler for all messages
        fh = logging.FileHandler(
            f'logs/picadsb_{datetime.now():%Y%m%d_%H%M%S}.log'
        )
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            '%(asctime)s.%(msecs)03d - %(name)s - %(levelname)s - %(message)s',
            '%Y-%m-%d %H:%M:%S'
        ))

        # Console handler for stderr
        ch_err = logging.StreamHandler(sys.stderr)
        ch_err.setLevel(numeric_level)
        ch_err.setFormatter(logging.Formatter(
            '%(asctime)s.%(msecs)03d - %(levelname)s - %(message)s',
            '%Y-%m-%d %H:%M:%S'
        ))

        self.logger.addHandler(fh)
        self.logger.addHandler(ch_err)

    def format_command(self, cmd_bytes: bytes) -> bytes:
        """Format command according to device protocol."""
        cmd_str = '-'.join([f"{b:02X}" for b in cmd_bytes])
        return f"#{cmd_str}\r".encode()

    def verify_response(self, cmd: bytes, response: bytes) -> bool:
        """Verify device response to command."""
        if not response:
            return False

        # Remove prefix '#' and split into bytes
        resp_bytes = [int(x, 16) for x in response[1:].decode().strip('-').split('-')]

        # Version command response
        if cmd[0] == 0x00:
            return resp_bytes[0] == 0x00 and resp_bytes[2] == 0x05

        # Mode set command response
        elif cmd[0] == 0x43:
            return resp_bytes[0] == 0x43 and resp_bytes[1] == cmd[1]

        # Filter command response
        elif cmd[0] == 0x37:
            return resp_bytes[0] == 0x37 and resp_bytes[1] == cmd[1]

        # Start command response
        elif cmd[0] == 0x38:
            return resp_bytes[0] == 0x38 and resp_bytes[2] == 0x01

        return True

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

    def _init_serial(self):
        """Initialize serial port with device configuration."""
        try:
            self.ser = serial.Serial(
                port=self.serial_port,
                baudrate=115200,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=1,
                xonxoff=False,     # disable software flow control
                rtscts=False,      # disable hardware (RTS/CTS) flow control
                dsrdtr=False       # disable hardware (DSR/DTR) flow control
            )

            # Set buffer sizes
            self.ser.set_buffer_size(rx_size=4096, tx_size=4096)

            # Clear buffers
            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()

            # Initialize CDC
            self.ser.setDTR(False)
            self.ser.setRTS(False)
            time.sleep(0.25)
            self.ser.setDTR(True)
            self.ser.setRTS(True)
            time.sleep(0.25)

            if not self.skip_init:
                if not self._initialize_device():
                    raise Exception("Device initialization failed")
            else:
                self.logger.info("Skipping device initialization (--no-init mode)")
                self.ser.write(self.format_command(b'\x00'))
                response = self._read_response()
                if not response or not self.verify_response(b'\x00', response):
                    raise Exception("Device version check failed")

            self.logger.info(f"Serial port {self.serial_port} initialized successfully")

        except Exception as e:
            self.logger.error(f"Failed to initialize serial port: {e}")
            raise

    def _initialize_device(self) -> bool:
        """Initialize device with specific command sequence."""
        commands = [
            (b'\x00', "Version request"),
            (b'\x43\x00', "Stop reception"),
            (b'\x51\x01\x00', "Set mode"),
            (b'\x37\x03', "Set filter"),
            (b'\x43\x00', "Status check 1"),
            (b'\x43\x00', "Status check 2"),
            (b'\x51\x00\x00', "Reset mode"),
            (b'\x37\x03', "Set filter"),
            (b'\x43\x00', "Final status check"),
            (b'\x38', "Start reception")
        ]

        for cmd, desc in commands:
            self.logger.debug(f"Sending {desc}: {cmd.hex()}")
            formatted_cmd = self.format_command(cmd)
            self.ser.write(formatted_cmd)
            time.sleep(0.1)

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
            time.sleep(0.01)
        return None

    def _cleanup_invalid_clients(self):
        """Remove invalid client sockets."""
        invalid_clients = []
        for client in self.clients:
            try:
                client.getpeername()
            except OSError:
                invalid_clients.append(client)

        for client in invalid_clients:
            self._remove_client(client)

    def _handle_client_connections(self):
        """Handle new client connections and data with keepalive."""
        try:
            current_time = time.time()

            try:
                readable, _, _ = select.select([self.server_socket] + self.clients, [], [], 0.1)
            except select.error:
                self._cleanup_invalid_clients()
                return

            for sock in readable:
                if sock is self.server_socket:
                    client_socket, address = self.server_socket.accept()
                    self._configure_client_socket(client_socket)
                    self.clients.append(client_socket)
                    self.stats['clients_total'] += 1
                    self.stats['clients_current'] = len(self.clients)
                    self.logger.info(f"New client connected from {address}")
                else:
                    try:
                        data = sock.recv(1024)
                        if not data:
                            self._remove_client(sock)
                            continue

                        if data == self.KEEPALIVE_MARKER:
                            self.client_last_active[sock] = current_time
                            self.logger.debug(f"Received keepalive from {sock.getpeername()}")
                        else:
                            self._handle_client_data(sock, data)

                    except (ConnectionError, OSError) as e:
                        self.logger.debug(f"Connection error: {e}")
                        self._remove_client(sock)

            self._check_client_keepalive(current_time)

        except Exception as e:
            self.logger.error(f"Error in client connections handler: {e}")

    def _check_client_keepalive(self, current_time: float):
        """Check clients for keepalive and send if needed."""
        disconnected_clients = []

        for client in self.clients:
            try:
                last_active = self.client_last_active.get(client, 0)
                if current_time - last_active > self.KEEPALIVE_INTERVAL:
                    try:
                        sent = client.send(self.KEEPALIVE_MARKER)
                        if sent == 0:
                            raise BrokenPipeError("Connection lost during keepalive")
                        self.client_last_active[client] = current_time
                        self.logger.debug(f"Sent keepalive to {client.getpeername()}")
                    except Exception as e:
                        self.logger.debug(f"Keepalive failed for client: {e}")
                        disconnected_clients.append(client)

            except Exception as e:
                self.logger.debug(f"Error checking client keepalive: {e}")
                disconnected_clients.append(client)

        for client in disconnected_clients:
            self._remove_client(client)

    def _remove_client(self, client: socket.socket):
        """Remove client and clean up resources."""
        try:
            if client in self.clients:
                self.clients.remove(client)
            if client in self.client_last_active:
                self.client_last_active.pop(client)

            try:
                client.getpeername()
                client.close()
            except OSError:
                pass

            self.stats['clients_current'] = len(self.clients)
            self.logger.info("Client disconnected")
        except Exception as e:
            self.logger.debug(f"Error removing client: {e}")

    def _handle_client_data(self, client: socket.socket, data: bytes):
        """Handle and log client data for debugging."""
        try:
            peer = client.getpeername()
            self.client_last_active[client] = time.time()

            hex_str = ' '.join([f'{b:02X}' for b in data])
            ascii_str = ''.join([chr(b) if 32 <= b <= 126 else '.' for b in data])

            self.logger.debug(
                f"Client data from {peer}:\n"
                f"  HEX: {hex_str}\n"
                f"ASCII: {ascii_str}"
            )

        except OSError:
            self._remove_client(client)
        except Exception as e:
            self.logger.error(f"Error handling client data: {e}")

    def _process_serial_data(self):
        """Process incoming data from the ADSB device."""
        try:
            if self.ser.in_waiting:
                self._last_data_time = time.time()
                self._no_data_logged = False

                self.logger.debug(f"Available bytes: {self.ser.in_waiting}")
                byte = self.ser.read()
                self.logger.debug(f"Read byte: {byte!r}")

                if byte in [b'\r', b'\n']:
                    return

                if byte in [b'#', b'*']:
                    if self._buffer:
                        self.logger.debug(f"Discarding incomplete buffer: {self._buffer!r}")
                    self._buffer = byte
                elif byte == b';':
                    self._buffer += byte

                    if len(self._buffer) > 2:
                        if self.validate_message(self._buffer):
                            try:
                                clean_msg = self._buffer.rstrip(b'\r\n;')
                                if clean_msg.startswith(b'#'):
                                    clean_msg = b'*' + clean_msg[1:]
                                formatted_msg = clean_msg + b';\n'

                                self.logger.debug(f"Putting message in queue: {formatted_msg!r}")
                                self.message_queue.put_nowait(formatted_msg)
                                self.stats['messages_processed'] += 1

                                print(formatted_msg.decode().rstrip(), flush=True)

                            except queue.Full:
                                self.logger.warning("Message queue full, dropping message")
                                self.stats['messages_dropped'] += 1
                        else:
                            self.logger.debug(f"Invalid message: {self._buffer[1:-1].decode()}")
                    self._buffer = b''
                else:
                    self._buffer += byte

                if len(self._buffer) > 100:
                    self.logger.debug(f"Buffer overflow, clearing: {self._buffer!r}")
                    self._buffer = b''

            else:
                time.sleep(0.001)

        except Exception as e:
            self.logger.error(f"Error processing serial data: {e}")
            self.stats['errors'] += 1

    def _broadcast_messages(self):
        """Broadcast queued messages to all connected clients."""
        try:
            while not self.message_queue.empty():
                message = self.message_queue.get_nowait()
                self.logger.debug(f"Broadcasting message from queue: {message!r}")

                disconnected_clients = []

                for client in self.clients:
                    try:
                        self.client_last_active[client] = time.time()
                        sent = client.send(message)

                        if sent == 0:
                            raise BrokenPipeError("Connection lost")
                        elif sent != len(message):
                            self.logger.warning(
                                f"Incomplete send to client {client.getpeername()}: "
                                f"sent {sent} of {len(message)} bytes"
                            )
                        else:
                            self.logger.debug(f"Successfully sent message to {client.getpeername()}")

                    except Exception as e:
                        self.logger.debug(f"Error sending to client: {e}")
                        disconnected_clients.append(client)

                for client in disconnected_clients:
                    self._remove_client(client)

        except Exception as e:
            self.logger.error(f"Error broadcasting messages: {e}")

    def _check_device_status(self):
        """Check device status if no data received for a while."""
        current_time = time.time()

        if current_time - self._last_data_time > 30 and not self._no_data_logged:
            self.logger.warning("No data received for 30 seconds, checking device...")

            self.ser.write(self.format_command(b'\x00'))
            response = self._read_response()

            if not response or not self.verify_response(b'\x00', response):
                self.logger.error("Device not responding properly")
                if not self._reconnect():
                    self.running = False
            else:
                self.logger.info("Device responding normally despite no data")

            self._no_data_logged = True

    def _check_device_version(self):
        """Periodic device version check."""
        current_time = time.time()
        if current_time - self.last_version_check >= self.version_check_interval:
            self.logger.debug("Performing periodic version check")
            self.ser.write(self.format_command(b'\x00'))
            response = self._read_response()

            if not response or not self.verify_response(b'\x00', response):
                self.logger.error("Version check failed, attempting reconnect")
                if not self._reconnect():
                    self.running = False
            else:
                self.logger.debug("Version check successful")

            self.last_version_check = current_time

    def _update_stats(self):
        """Update and log statistics."""
        current_time = time.time()
        if current_time - self.last_stats_update >= self.stats_interval:
            messages_per_minute = (self.stats['messages_processed'] -
                                 self.stats['last_minute_count'])

            self.logger.info(
                f"Statistics: Messages/min: {messages_per_minute}, "
                f"Total: {self.stats['messages_processed']}, "
                f"Dropped: {self.stats['messages_dropped']}, "
                f"Clients: {self.stats['clients_current']}, "
                f"Errors: {self.stats['errors']}, "
                f"Queue: {self.message_queue.qsize()}/{self.message_queue.maxsize}"
            )

            if self.message_queue.qsize() > self.message_queue.maxsize * 0.8:
                self.logger.warning("Message queue is more than 80% full!")

            self.stats['messages_per_minute'] = messages_per_minute
            self.stats['last_minute_count'] = self.stats['messages_processed']
            self.last_stats_update = current_time

    def _reconnect(self) -> bool:
        """Attempt to reconnect to the device."""
        self.logger.info("Attempting to reconnect...")
        retry_count = 3

        for attempt in range(retry_count):
            try:
                self.ser.close()
                time.sleep(2)
                self._init_serial()
                self.stats['reconnects'] += 1
                return True
            except Exception as e:
                self.logger.error(f"Reconnection attempt {attempt + 1} failed: {e}")
                time.sleep(5)

        return False

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals."""
        self.logger.info(f"Received signal {signum}, shutting down...")
        self.running = False

    def run(self):
        """Main operation loop."""
        self.logger.info("Starting ADS-B multiplexer")

        try:
            while self.running:
                cycle_start = time.time()
                messages_at_start = self.stats['messages_processed']

                self._check_device_version()
                self._handle_client_connections()
                self._process_serial_data()
                self._broadcast_messages()
                self._update_stats()
                self._check_device_status()

                cycle_end = time.time()
                messages_processed = self.stats['messages_processed'] - messages_at_start

                if messages_processed > 0:
                    self.logger.debug(
                        f"Cycle time: {(cycle_end - cycle_start)*1000:.2f}ms, "
                        f"Messages processed: {messages_processed}"
                    )

                time.sleep(0.01)

        except KeyboardInterrupt:
            self.logger.info("Keyboard interrupt received")
        except Exception as e:
            self.logger.error(f"Error in main loop: {e}")
        finally:
            self.cleanup()

    def cleanup(self):
        """Clean up resources on shutdown."""
        self.logger.info("Cleaning up...")

        for client in self.clients[:]:
            self._remove_client(client)

        try:
            self.server_socket.close()
        except:
            pass

        try:
            if self.ser and self.ser.is_open:
                self.ser.write(self.format_command(b'\x43\x00'))
                time.sleep(0.1)
                self.ser.close()
        except:
            pass

        self.logger.info("Cleanup complete")

def main():
    """Entry point with command line argument parsing."""
    import argparse

    parser = argparse.ArgumentParser(description='ADS-B Multiplexer for MicroADSB/adsbPIC devices')
    parser.add_argument('--port', type=int, default=30002,
                      help='TCP port number (default: 30002)')
    parser.add_argument('--device', type=str, default='/dev/ttyACM0',
                      help='Serial device (default: /dev/ttyACM0)')
    parser.add_argument('--log', type=str, default='INFO',
                      choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
                      help='Logging level (default: INFO)')
    parser.add_argument('--no-init', action='store_true',
                      help='Skip device initialization (raw mode)')

    args = parser.parse_args()

    try:
        muxer = PicADSBMultiplexer(
            tcp_port=args.port,
            serial_port=args.device,
            log_level=args.log,
            skip_init=args.no_init
        )
        muxer.run()
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
