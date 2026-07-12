#!/usr/bin/env python3
"""
ADS-B Message Monitor
Displays ADS-B messages in both formatted and RAW formats with message type identification and timestamp.
"""

import socket
import time
import signal
from datetime import datetime
from typing import Optional
import select

class ADSBMonitor:
    # Dictionary of ADS-B message types and their descriptions
    MESSAGE_TYPES = {
        '00': 'Short Air-to-Air ACAS (DF0)',
        '02': 'Short Air-to-Air ACAS (DF0)',
        '20': 'Altitude Reply (DF4)',
        '21': 'Identity Reply (DF4)',
        '27': 'Reserved for ACAS (DF5)',
        '28': 'Extended Squitter (DF5)',
        '29': 'Military Extended Squitter (DF5)',
        '2A': 'Military Extended Squitter (DF5)',
        '2B': 'Military Extended Squitter (DF5)',
        '2C': 'Military Extended Squitter (DF5)',
        '2D': 'Military Extended Squitter (DF5)',
        '2E': 'Military Extended Squitter (DF5)',
        '2F': 'Military Extended Squitter (DF5)',
        '5D': 'Extended Squitter ID and Position (DF17)',
        '8D': 'Extended Squitter (DF17)',
        'A0': 'ACAS Resolution Advisory (DF20)',
        'A1': 'ACAS Resolution Advisory (DF20)',
        'A2': 'ACAS Resolution Advisory (DF20)',
        'A3': 'ACAS Resolution Advisory (DF20)',
        'A4': 'ACAS Resolution Advisory (DF20)',
        'A5': 'ACAS Resolution Advisory (DF20)',
        'A6': 'ACAS Resolution Advisory (DF20)',
        'A7': 'ACAS Resolution Advisory (DF20)',
    }

    MAX_BUFFER = 4096  # Cap for the partial-message buffer (garbage guard)

    def __init__(self, host: str = 'localhost', port: int = 30002, raw_mode: bool = False):
        self.host = host
        self.port = port
        self.running = True
        self.socket = None
        self.raw_mode = raw_mode
        self.reconnect_delay = 5  # Initial reconnect delay in seconds
        self.max_reconnect_delay = 30  # Maximum reconnect delay
        self.header_printed = False  # Flag to track if header was printed
        self._buffer = b''  # Holds a partial message between recv() calls
        self.stats = {
            'total_messages': 0,
            'start_time': time.time(),
            'messages_by_type': {}
        }

        # Setup signal handlers for graceful shutdown
        signal.signal(signal.SIGINT, self.signal_handler)
        signal.signal(signal.SIGTERM, self.signal_handler)

    def print_header(self):
        """Print the table header with column names"""
        if not self.header_printed:
            print("\nADS-B Message Monitor")
            print(f"Connected to {self.host}:{self.port}")
            if not self.raw_mode:
                print("\n{:<23} | {:<8} | {:<45} | {:<20}".format(
                    "Timestamp", "Type", "Message", "Description"))
                print("-" * 100)
            self.header_printed = True

    def format_message(self, msg_str: str) -> Optional[tuple]:
        """Format a decoded message and return its display components"""
        # Skip keep-alive messages
        if msg_str.startswith('*00'):
            return None

        msg_type = msg_str[1:3]
        description = self.MESSAGE_TYPES.get(msg_type, "Unknown Type")

        # Update message statistics
        self.stats['total_messages'] += 1
        self.stats['messages_by_type'][msg_type] = self.stats['messages_by_type'].get(msg_type, 0) + 1

        return (
            datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3],
            msg_type,
            msg_str,
            description
        )

    def print_stats(self):
        """Print session statistics and message type distribution"""
        runtime = time.time() - self.stats['start_time']
        msgs_per_sec = self.stats['total_messages'] / runtime if runtime > 0 else 0

        print("\nSession Statistics:")
        print(f"Runtime: {runtime:.1f} seconds")
        print(f"Total Messages: {self.stats['total_messages']}")
        print(f"Messages per second: {msgs_per_sec:.1f}")

        if self.stats['total_messages'] > 0:
            print("\nMessage Types Distribution:")
            for msg_type, count in sorted(self.stats['messages_by_type'].items()):
                description = self.MESSAGE_TYPES.get(msg_type, "Unknown Type")
                percentage = (count / self.stats['total_messages'] * 100)
                print(f"{msg_type}: {count} messages ({percentage:.1f}%) - {description}")

    def signal_handler(self, signum, frame):
        """Handle system signals for graceful shutdown"""
        print("\nShutdown signal received, closing...")
        self.running = False

    def cleanup(self):
        """Clean up resources and close connections"""
        if self.socket:
            try:
                self.socket.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass  # Ignore shutdown errors
            try:
                self.socket.close()
            except Exception:
                pass  # Ignore close errors
            self.socket = None

    def connect(self) -> bool:
        """Establish connection to the ADS-B server"""
        try:
            if self.socket:
                self.cleanup()

            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.settimeout(5)  # Connection timeout
            self.socket.connect((self.host, self.port))
            self.socket.setblocking(False)  # Set non-blocking mode
            self._buffer = b''  # Drop any partial message from the old connection
            if not self.header_printed:
                self.print_header()
            return True
        except Exception as e:
            print(f"\rConnection error: {e}", end='')
            return False

    def _feed(self, data: bytes):
        """Split incoming bytes into messages, keeping the incomplete tail.

        recv() is not message-aligned: a message can straddle two reads,
        so the tail after the last ';' stays in the buffer until more
        data arrives.
        """
        self._buffer += data
        parts = self._buffer.split(b';')
        self._buffer = parts[-1]
        if len(self._buffer) > self.MAX_BUFFER:
            self._buffer = b''  # Runaway garbage, drop it

        for msg in parts[:-1]:
            if msg.strip():  # Skip empty fragments (\r\n between messages)
                self.process_message(msg)

    def process_message(self, msg: bytes):
        """Process and display message in either RAW or formatted mode"""
        try:
            text = msg.decode('ascii').strip()
        except UnicodeDecodeError as e:
            print(f"Error decoding message: {e}")
            return

        if not text.startswith('*'):
            return

        if self.raw_mode:
            # RAW mode - display message as is (issue #1: count it too)
            self.stats['total_messages'] += 1
            print(text)
        else:
            # Formatted mode
            formatted_msg = self.format_message(text + ';')
            if formatted_msg:  # Only print if not a keep-alive message
                timestamp, msg_type, message, description = formatted_msg
                print("\r{:<23} | {:<8} | {:<45} | {:<20}".format(
                    timestamp, msg_type, message, description))
                # Print RAW format below
                print(f"\rRAW: {message}")
                print("\r" + "-" * 100)

    def run(self):
        """Main processing loop with automatic reconnection"""
        while self.running:
            try:
                if not self.connect():
                    print(f"\rWaiting for connection... ", end='')
                    time.sleep(self.reconnect_delay)
                    self.reconnect_delay = min(self.reconnect_delay * 2, self.max_reconnect_delay)
                    continue

                self.reconnect_delay = 5  # Reset reconnect delay after successful connection

                while self.running:
                    ready = select.select([self.socket], [], [], 1.0)

                    if ready[0]:
                        try:
                            data = self.socket.recv(1024)
                            if not data:
                                print("\rConnection lost. Attempting to reconnect...", end='')
                                break

                            self._feed(data)

                        except BlockingIOError:
                            continue
                        except socket.error as e:
                            print(f"\rSocket error: {e}", end='')
                            break

            except KeyboardInterrupt:
                print("\nUser interrupted")
                break
            except Exception as e:
                print(f"\rError: {e}", end='')
                time.sleep(self.reconnect_delay)
                self.reconnect_delay = min(self.reconnect_delay * 2, self.max_reconnect_delay)

        self.cleanup()
        self.print_stats()

def main():
    """Entry point of the program"""
    import argparse

    parser = argparse.ArgumentParser(description='ADS-B Message Monitor')
    parser.add_argument('--host', default='localhost', help='Server host (default: localhost)')
    parser.add_argument('--port', type=int, default=30002, help='Server port (default: 30002)')
    parser.add_argument('--raw', action='store_true', help='Display messages in RAW format only')

    args = parser.parse_args()

    monitor = ADSBMonitor(args.host, args.port, args.raw)
    monitor.run()

if __name__ == "__main__":
    main()
