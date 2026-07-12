"""Tests for _process_serial_data: buffer persistence, validation gating, device loss."""
import queue
from unittest.mock import MagicMock

import pytest
import serial

from tests.conftest import PicADSBMultiplexer
from picadsb.config import Config

STATS_KEYS = (
    'messages_processed', 'messages_dropped', 'invalid_messages',
    'buffer_truncated', 'errors', 'bytes_received', 'bytes_processed',
)


class FakeSerial:
    """Serial stub that hands out queued chunks, one per read()."""

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.is_open = True

    @property
    def in_waiting(self):
        return len(self._chunks[0]) if self._chunks else 0

    def read(self, n):
        return self._chunks.pop(0)


class DeadSerial:
    """Serial stub behaving like a physically unplugged device."""

    is_open = True

    @property
    def in_waiting(self):
        raise serial.SerialException("device disconnected")


@pytest.fixture
def mux():
    obj = object.__new__(PicADSBMultiplexer)
    obj.logger = MagicMock()
    obj.config = Config()
    obj.stats = {k: 0 for k in STATS_KEYS}
    obj.message_queue = queue.Queue(maxsize=10)
    obj._serial_parse_buffer = bytearray()
    obj._last_data_time = 0
    obj.running = True
    return obj


class TestBufferPersistence:
    def test_message_split_across_reads(self, mux):
        mux.ser = FakeSerial([b"*8D406B90", b"2015A678D4D220AA4BDA;"])
        mux._process_serial_data()
        mux._process_serial_data()
        assert mux.message_queue.get_nowait() == b"*8D406B902015A678D4D220AA4BDA;\n"
        assert mux.stats['messages_processed'] == 1

    def test_new_start_marker_resets_buffer(self, mux):
        # First message loses its terminator; the next '*' must restart parsing
        mux.ser = FakeSerial([b"*8D406B9020" b"*02E19700000000;"])
        mux._process_serial_data()
        assert mux.message_queue.get_nowait() == b"*02E19700000000;\n"
        assert mux.message_queue.empty()

    def test_oversized_buffer_truncated(self, mux):
        mux.ser = FakeSerial([b"*" + b"A" * (mux.config.max_message_length + 1)])
        mux._process_serial_data()
        assert mux.stats['buffer_truncated'] == 1
        assert mux.message_queue.empty()


class TestValidationGating:
    def test_unsupported_length_counted_invalid_not_queued(self, mux):
        # 8-byte payload: valid hex, unsupported length. Must not be queued -
        # _convert_to_beast would drop it at broadcast anyway.
        mux.ser = FakeSerial([b"*8D406B9020150012;"])
        mux._process_serial_data()
        assert mux.stats['invalid_messages'] == 1
        assert mux.message_queue.empty()

    def test_bad_hex_counted_invalid(self, mux):
        mux.ser = FakeSerial([b"*NOTHEX;"])
        mux._process_serial_data()
        assert mux.stats['invalid_messages'] == 1
        assert mux.message_queue.empty()

    def test_unknown_prefix_ignored_entirely(self, mux):
        # '$' is not a start marker: bytes must be discarded without stats noise
        mux.ser = FakeSerial([b"$8D406B90;"])
        mux._process_serial_data()
        assert mux.stats['invalid_messages'] == 0
        assert mux.message_queue.empty()

    def test_queue_full_counts_dropped(self, mux):
        mux.message_queue = queue.Queue(maxsize=1)
        mux.ser = FakeSerial([b"*A1B2;*C3D4;"])
        mux._process_serial_data()
        assert mux.stats['messages_processed'] == 1
        assert mux.stats['messages_dropped'] == 1


class TestDeviceLoss:
    def test_serial_exception_triggers_reconnect(self, mux):
        mux.ser = DeadSerial()
        mux._reconnect = MagicMock(return_value=True)
        mux._process_serial_data()
        mux._reconnect.assert_called_once()
        assert mux.running is True

    def test_failed_reconnect_stops_multiplexer(self, mux):
        mux.ser = DeadSerial()
        mux._reconnect = MagicMock(return_value=False)
        mux._process_serial_data()
        assert mux.running is False

    def test_check_device_status_survives_dead_device(self, mux):
        # Regression: a hard unplug during the no-data check must route to
        # _reconnect instead of raising out of _check_device_status.
        mux.ser = MagicMock()
        mux.ser.write.side_effect = serial.SerialException("gone")
        mux._no_data_logged = False
        mux.config = Config(no_data_timeout=0)
        mux._reconnect = MagicMock(return_value=True)
        mux._check_device_status()
        mux._reconnect.assert_called_once()
