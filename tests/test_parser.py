"""Tests for adsb_message_parser.py (ADS-B Message Monitor)."""
import importlib.util
import os

import pytest

_MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "adsb_message_parser.py",
)
_spec = importlib.util.spec_from_file_location("adsb_message_parser", _MODULE_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
ADSBMonitor = _mod.ADSBMonitor


@pytest.fixture
def monitor():
    return ADSBMonitor(raw_mode=False)


@pytest.fixture
def raw_monitor():
    return ADSBMonitor(raw_mode=True)


class TestFeedSplitting:
    def test_all_messages_in_one_read_processed(self, monitor, capsys):
        # Regression: formatted mode used to drop every message except the
        # first per recv() because fragments kept their leading \r\n.
        monitor._feed(b"*8D4840D6202CC3;\r\n*02E19700000000;\r\n*5D4CA7B3AABBCC;\r\n")
        assert monitor.stats['total_messages'] == 3

    def test_message_split_across_reads(self, monitor):
        monitor._feed(b"*8D4840")
        assert monitor.stats['total_messages'] == 0  # incomplete, buffered
        monitor._feed(b"D6202CC3;\r\n")
        assert monitor.stats['total_messages'] == 1

    def test_blank_fragments_not_printed(self, raw_monitor, capsys):
        raw_monitor._feed(b"*8D4840D6202CC3;\r\n\r\n;;\r\n")
        out = capsys.readouterr().out
        assert out.strip() == "*8D4840D6202CC3"

    def test_runaway_garbage_dropped(self, monitor):
        monitor._feed(b"A" * (ADSBMonitor.MAX_BUFFER + 1))
        assert monitor._buffer == b""

    def test_buffer_reset_on_reconnect(self, monitor):
        monitor._feed(b"*8D4840")  # partial message buffered
        assert monitor._buffer == b"*8D4840"
        # connect() resets the buffer; emulate the relevant part
        monitor._buffer = b""
        monitor._feed(b"D6202CC3;")  # tail of the OLD message must not survive
        assert monitor.stats['total_messages'] == 0


class TestRawMode:
    def test_total_messages_counted(self, raw_monitor, capsys):
        # Regression for issue #1: raw mode never incremented total_messages
        raw_monitor._feed(b"*8D4840D6202CC3;\r\n*02E19700000000;\r\n")
        assert raw_monitor.stats['total_messages'] == 2
        out = capsys.readouterr().out
        assert "*8D4840D6202CC3" in out
        assert "*02E19700000000" in out

    def test_non_message_data_skipped(self, raw_monitor, capsys):
        raw_monitor._feed(b"#GARBAGE;\r\n")
        assert raw_monitor.stats['total_messages'] == 0


class TestFormattedMode:
    def test_keepalive_not_counted(self, monitor, capsys):
        monitor._feed(b"*00000000000000;\r\n")
        assert monitor.stats['total_messages'] == 0

    def test_message_type_identified(self, monitor, capsys):
        monitor._feed(b"*28000000000000;\r\n")
        assert monitor.stats['messages_by_type'].get('28') == 1
        out = capsys.readouterr().out
        assert "Extended Squitter (DF5)" in out
