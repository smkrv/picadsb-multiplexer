"""Tests for Beast format encoding and TimestampGenerator."""
import time

import pytest

from tests.conftest import BeastFormat, TimestampGenerator


class TestBeastFormatConstants:
    def test_escape_byte(self):
        assert BeastFormat.ESCAPE == 0x1A

    def test_type_modea(self):
        assert BeastFormat.TYPE_MODEA == 0x31

    def test_type_modes_short(self):
        assert BeastFormat.TYPE_MODES_SHORT == 0x32

    def test_type_modes_long(self):
        assert BeastFormat.TYPE_MODES_LONG == 0x33

    def test_modes_short_len(self):
        assert BeastFormat.MODES_SHORT_LEN == 7

    def test_modes_long_len(self):
        assert BeastFormat.MODES_LONG_LEN == 14

    def test_modea_len(self):
        assert BeastFormat.MODEA_LEN == 2

    def test_timestamp_len(self):
        assert BeastFormat.TIMESTAMP_LEN == 6

    def test_max_timestamp(self):
        assert BeastFormat.MAX_TIMESTAMP == 0xFFFFFFFFFFFF


class TestTimestampGenerator:
    def test_returns_6_bytes(self):
        gen = TimestampGenerator()
        ts = gen.get_timestamp()
        assert isinstance(ts, bytes)
        assert len(ts) == 6

    def test_monotonic(self):
        gen = TimestampGenerator()
        ts1 = gen.get_timestamp()
        ts2 = gen.get_timestamp()
        val1 = int.from_bytes(ts1, "big")
        val2 = int.from_bytes(ts2, "big")
        assert val2 > val1

    def test_no_zero_after_first(self):
        gen = TimestampGenerator()
        _ = gen.get_timestamp()
        ts = gen.get_timestamp()
        assert int.from_bytes(ts, "big") > 0

    def test_multiple_calls_all_unique(self):
        gen = TimestampGenerator()
        timestamps = [gen.get_timestamp() for _ in range(100)]
        assert len(set(timestamps)) == 100

    def test_fits_in_6_bytes(self):
        gen = TimestampGenerator()
        for _ in range(10):
            ts = gen.get_timestamp()
            val = int.from_bytes(ts, "big")
            assert val <= BeastFormat.MAX_TIMESTAMP

    def test_microseconds_since_midnight_unit(self, monkeypatch):
        # Pin the unit contract exactly: a wrong-unit regression
        # (milliseconds, 12 MHz ticks) must fail this test.
        fixed = 1_700_000_000.123456
        monkeypatch.setattr(time, "time", lambda: fixed)
        gen = TimestampGenerator()
        expected = int((fixed % 86400) * 1e6)
        assert int.from_bytes(gen.get_timestamp(), "big") == expected

    def test_midnight_rollover_resets_to_wall_time(self, monkeypatch):
        # Just before midnight the counter is huge; right after midnight the
        # generator must accept the reset instead of crawling at +1us/msg.
        gen = TimestampGenerator()
        gen.last_micros = 86_399_999_000  # ~1ms before midnight
        after_midnight = 0.5  # 500ms past midnight
        monkeypatch.setattr(time, "time", lambda: after_midnight)
        val = int.from_bytes(gen.get_timestamp(), "big")
        assert val == 500_000

    def test_small_backward_jitter_stays_monotonic(self, monkeypatch):
        gen = TimestampGenerator()
        gen.last_micros = 1_000_000
        monkeypatch.setattr(time, "time", lambda: 0.9999995)  # 0.5us back
        val = int.from_bytes(gen.get_timestamp(), "big")
        assert val == 1_000_001
