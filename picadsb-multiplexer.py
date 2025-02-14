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
import pyModeS as pms
from datetime import datetime
from typing import Optional, Dict, Any, List, Tuple

class PicADSBMultiplexer:
    """Main multiplexer class that handles device communication and client connections."""

    def validate_message(self, msg: bytes) -> bool:  
        """
        Validate Mode-S and Mode-A/C messages on 1090 MHz frequency.
        Optimized for aviation-specific messages with minimal filtering.

        Args:
            msg (bytes): Raw message from 1090 MHz receiver

        Returns:
            bool: True if message appears to be valid aviation data
        """
        try:
            # Clean up received message
            hex_msg = msg.decode().strip('*;\r\n')

            # Basic format check
            if not hex_msg:
                self.logger.debug("Empty message rejected")
                return False

            # Verify hexadecimal format
            if not all(c in '0123456789ABCDEFabcdef' for c in hex_msg):
                self.logger.debug(f"Non-hex characters in message: {hex_msg}")
                return False

            # Check aviation message length
            msg_len = len(hex_msg)
            if msg_len not in (14, 28):  # Short (56 bits) or Long (112 bits) Mode-S
                self.logger.debug(f"Non-standard message length ({msg_len}): {hex_msg}")
                return False

            # Decode Downlink Format
            try:
                df = pms.df(hex_msg)
            except:
                self.logger.debug(f"Unable to decode DF from message: {hex_msg}")
                return False

            # Aviation message type validation
            if df == 17:  # ADS-B
                # Full validation for ADS-B
                if not pms.crc(hex_msg):
                    self.logger.debug(f"Invalid ADS-B message CRC: {hex_msg}")
                    return False

            elif df in [0]:  # Short ACAS
                pass  # Accept all ACAS

            elif df in [4, 5]:  # Altitude and Identity replies
                pass  # Accept all altitude/identity data

            elif df in [11]:  # All-call replies
                pass  # Accept all all-call replies

            elif df in [16]:  # Long ACAS
                pass  # Accept all TCAS

            elif df in [20, 21]:  # Comm-B altitude/identity
                pass  # Accept all Mode-S communications

            elif df in [24, 28]:  # Comm-D ELM
                pass  # Accept Extended Length Messages

            # Log accepted aviation message
            self.logger.debug(f"Valid aviation message DF{df}: {hex_msg}")
            return True

        except Exception as e:
            self.logger.debug(f"Validation error: {str(e)}, Message: {msg}")
            return False

    def __init__(self, tcp_port: int = 30002, serial_port: str = '/dev/ttyACM0', log_level: str = 'INFO'):
        """Initialize the multiplexer."""
        self.tcp_port = tcp_port
        self.serial_port = serial_port
        self._setup_logging(log_level)

        # Runtime state
        self.running = True
        self.firmware_version = None
        self.device_id = None
        self._buffer = b''

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

        # Console handler for stderr (debug/info messages)
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
        """Initialize serial port and perform device initialization sequence."""
        try:
            self.ser = serial.Serial(
                port=self.serial_port,
                baudrate=115200,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=1
            )

            # Clear buffers
            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()

            # Initialize CDC
            self.ser.setDTR(False)
            self.ser.setRTS(False)
            time.sleep(0.1)
            self.ser.setDTR(True)
            self.ser.setRTS(True)
            time.sleep(0.1)

            # Perform device initialization
            if not self._initialize_device():
                raise Exception("Device initialization failed")

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

    def is_adsb_message(self, msg: bytes) -> bool:
        """Check if message is valid ADSB format."""
        return any(msg.startswith(prefix) for prefix in self.ADSB_PREFIXES)

    def _process_serial_data(self):
        """Process incoming data from the ADSB device byte by byte."""
        try:
            if self.ser.in_waiting:
                byte = self.ser.read()

                # Skip single \r and \n
                if byte in [b'\r', b'\n']:
                    return

                if byte in [b'#', b'*']:  # message start
                    self._buffer = byte
                elif byte == b';':  # message end
                    self._buffer += byte
                    if len(self._buffer) > 2:  # check minimum message length
                        if self.validate_message(self._buffer):
                            try:
                                # Ensure correct format for dump1090: *MSG;\n
                                message = self._buffer.rstrip(b'\r\n;') + b';\n'

                                # Send to clients
                                self.message_queue.put_nowait(message)
                                self.stats['messages_processed'] += 1

                                # Print to stdout for piping
                                print(message.decode().rstrip(), flush=True)

                            except queue.Full:
                                self.logger.warning("Message queue full, dropping message")
                        else:
                            # Invalid messages to debug log only
                            self.logger.debug(f"Invalid Mode-S message: {self._buffer[1:-1].decode()}")
                    self._buffer = b''
                else:
                    self._buffer += byte

                # Limit buffer size
                if len(self._buffer) > 100:
                    self._buffer = b''

            else:
                # If no data, small delay
                time.sleep(0.01)

        except Exception as e:
            self.logger.error(f"Error processing serial data: {e}")

    def _handle_client_connections(self):
        """Handle new client connections and data."""
        try:
            readable, _, _ = select.select([self.server_socket] + self.clients, [], [], 0.1)

            for sock in readable:
                if sock is self.server_socket:
                    client_socket, address = self.server_socket.accept()
                    client_socket.setblocking(False)
                    self.clients.append(client_socket)
                    self.stats['clients_total'] += 1
                    self.stats['clients_current'] = len(self.clients)
                    self.logger.info(f"New client connected from {address}")
                else:
                    try:
                        data = sock.recv(1024)
                        if not data:  # Client disconnected
                            self.clients.remove(sock)
                            sock.close()
                            self.stats['clients_current'] = len(self.clients)
                            self.logger.info("Client disconnected")
                    except:
                        self.clients.remove(sock)
                        sock.close()
                        self.stats['clients_current'] = len(self.clients)
                        self.logger.info("Client connection lost")

        except Exception as e:
            self.logger.error(f"Error handling client connections: {e}")

    def _broadcast_messages(self):
        """Broadcast queued messages to all connected clients."""
        try:
            while not self.message_queue.empty():
                message = self.message_queue.get_nowait()
                disconnected_clients = []

                for client in self.clients:
                    try:
                        sent = client.send(message)
                        if sent == 0:
                            raise BrokenPipeError("Connection lost")
                    except Exception as e:
                        disconnected_clients.append(client)

                for client in disconnected_clients:
                    try:
                        self.clients.remove(client)
                        client.close()
                        self.stats['clients_current'] = len(self.clients)
                    except:
                        pass

        except Exception as e:
            self.logger.error(f"Error broadcasting messages: {e}")

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
                self._check_device_version()
                self._handle_client_connections()
                self._process_serial_data()
                self._broadcast_messages()
                self._update_stats()
                time.sleep(0.001)  # Prevent CPU overload

        except KeyboardInterrupt:
            self.logger.info("Keyboard interrupt received")
        except Exception as e:
            self.logger.error(f"Error in main loop: {e}")
        finally:
            self.cleanup()

    def cleanup(self):
        """Clean up resources on shutdown."""
        self.logger.info("Cleaning up...")

        for client in self.clients:
            try:
                client.close()
            except:
                pass

        try:
            self.server_socket.close()
        except:
            pass

        try:
            if self.ser and self.ser.is_open:
                self.ser.write(self.format_command(b'\x43\x00'))  # Stop reception
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

    args = parser.parse_args()

    try:
        muxer = PicADSBMultiplexer(
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
