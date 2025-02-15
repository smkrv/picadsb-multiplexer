#!/usr/bin/env python3

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
    KEEPALIVE_MARKER = b'\x00'
    KEEPALIVE_INTERVAL = 30
    SERIAL_BUFFER_SIZE = 131072
    MAX_MESSAGE_LENGTH = 50

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

    def __init__(self, tcp_port: int = 30002, serial_port: str = '/dev/ttyACM0',
                 log_level: str = 'INFO', skip_init: bool = False):
        self.tcp_port = tcp_port
        self.serial_port = serial_port
        self.skip_init = skip_init
        self._setup_logging(log_level)

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

        # Runtime state
        self.running = True
        self.firmware_version = None
        self.device_id = None
        self._buffer = b''
        self._last_data_time = time.time()
        self._no_data_logged = False
        self._sync_state = True

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

        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _init_serial(self):
        try:
            self.ser = serial.Serial(
                port=self.serial_port,
                baudrate=115200,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=1,
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
                if not self._initialize_device():
                    raise Exception("Device initialization failed")
            else:
                self.logger.info("Skipping device initialization (--no-init mode)")
                self._check_device_version()

            self.logger.info(f"Serial port {self.serial_port} initialized successfully")

        except Exception as e:
            self.logger.error(f"Failed to initialize serial port: {e}")
            raise

    def _process_serial_data(self):
        try:
            if not self.ser.is_open:
                self.logger.error("Serial port is closed")
                return

            if self.ser.in_waiting:
                self._last_data_time = time.time()
                self._no_data_logged = False

                try:
                    byte = self.ser.read()
                    self.stats['bytes_received'] += 1
                except serial.SerialException as e:
                    self.logger.error(f"Serial read error: {e}")
                    if not self._reconnect():
                        self.running = False
                    return

                if byte in [b'\r', b'\n']:
                    return

                if byte in [b'#', b'*']:
                    if self._buffer:
                        self.logger.debug(f"Discarding incomplete buffer: {self._buffer!r}")
                        self.stats['sync_losses'] += 1
                    self._buffer = byte
                    self._sync_state = True

                elif byte == b';':
                    if not self._sync_state:
                        self.logger.debug("Message received while out of sync")
                        self._buffer = b''
                        return

                    self._buffer += byte

                    if len(self._buffer) > 2:
                        if self._buffer.startswith((b'*', b'#')) and self._buffer.endswith(b';'):
                            if self.validate_message(self._buffer):
                                try:
                                    clean_msg = self._buffer.rstrip(b'\r\n;')
                                    if clean_msg.startswith(b'#'):
                                        clean_msg = b'*' + clean_msg[1:]
                                    formatted_msg = clean_msg + b';\n'

                                    self.stats['bytes_processed'] += len(formatted_msg)

                                    try:
                                        self.message_queue.put_nowait(formatted_msg)
                                        self.stats['messages_processed'] += 1
                                        self.logger.debug(f"Message queued: {formatted_msg!r}")
                                    except queue.Full:
                                        self.logger.warning("Message queue full, dropping message")
                                        self.stats['messages_dropped'] += 1

                                except Exception as e:
                                    self.logger.error(f"Error processing message: {e}")
                                    self.stats['errors'] += 1
                            else:
                                self.logger.debug(f"Invalid message: {self._buffer!r}")
                                self.stats['invalid_messages'] += 1
                        else:
                            self.logger.debug(f"Malformed message: {self._buffer!r}")
                            self.stats['invalid_messages'] += 1

                    self._buffer = b''
                    self._sync_state = False

                else:
                    if not self._sync_state:
                        return

                    self._buffer += byte

                    if len(self._buffer) > self.MAX_MESSAGE_LENGTH:
                        self.logger.warning(f"Buffer overflow, discarding: {self._buffer!r}")
                        self.stats['buffer_overflows'] += 1
                        self._buffer = b''
                        self._sync_state = False

            else:
                time.sleep(0.01)

        except Exception as e:
            self.logger.error(f"Error in serial processing: {e}")
            self.stats['errors'] += 1
            if not self._reconnect():
                self.running = False

    def _update_stats(self):
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

    def _reconnect(self) -> bool:
        self.stats['reconnects'] += 1
        self.logger.warning("Attempting to reconnect...")

        try:
            if self.ser.is_open:
                self.ser.close()

            time.sleep(1)

            self.ser.open()
            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()

            self.ser.setDTR(False)
            self.ser.setRTS(False)
            time.sleep(0.25)
            self.ser.setDTR(True)
            self.ser.setRTS(True)
            time.sleep(0.25)

            if not self.skip_init and not self._initialize_device():
                raise Exception("Device initialization failed during reconnect")

            self.logger.info("Successfully reconnected")
            return True

        except Exception as e:
            self.logger.error(f"Reconnection failed: {e}")
            return False

    def validate_message(self, message: bytes) -> bool:
        try:
            if len(message) < 3:
                return False

            if not message.startswith((b'*', b'#')):
                return False

            if not message.endswith(b';'):
                return False

            valid_chars = set(b'0123456789ABCDEF*#;')
            if not all(b in valid_chars for b in message):
                return False

            return True

        except Exception as e:
            self.logger.error(f"Message validation error: {e}")
            return False

    def run(self):
        """Основной цикл работы"""
        self.logger.info("Starting multiplexer...")

        while self.running:
            try:
                self._process_serial_data()

                readable, writable, exceptional = select.select(
                    [self.server_socket] + self.clients,
                    [], [], 0.1)

                for sock in readable:
                    if sock is self.server_socket:
                        self._accept_new_client()
                    else:
                        self._handle_client_data(sock)

                while not self.message_queue.empty():
                    message = self.message_queue.get_nowait()
                    self._broadcast_message(message)

                self._update_stats()

                self._check_timeouts()

            except Exception as e:
                self.logger.error(f"Main loop error: {e}")
                self.stats['errors'] += 1
                time.sleep(1)

    def _check_timeouts(self):
        current_time = time.time()

        if current_time - self._last_data_time > 30 and not self._no_data_logged:
            self.logger.warning("No data received for 30 seconds")
            self._no_data_logged = True

        for client in list(self.clients):
            if current_time - self.client_last_active.get(client, 0) > 60:
                self.logger.info(f"Closing inactive client {client.getpeername()}")
                self._remove_client(client)

    def cleanup(self):
        self.logger.info("Shutting down...")

        for client in self.clients[:]:
            self._remove_client(client)

        if hasattr(self, 'server_socket'):
            self.server_socket.close()

        if hasattr(self, 'ser') and self.ser.is_open:
            self.ser.close()

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

    args = parser.parse_args()

    multiplexer = PicADSBMultiplexer(
        tcp_port=args.port,
        serial_port=args.serial,
        log_level=args.log_level,
        skip_init=args.no_init
    )

    try:
        multiplexer.run()
    except KeyboardInterrupt:
        pass
    finally:
        multiplexer.cleanup()
