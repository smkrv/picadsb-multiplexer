"""Tests for Beast escape sequence handling."""
import pytest
from unittest.mock import MagicMock

from tests.conftest import BeastFormat, PicADSBMultiplexer
from picadsb.config import Config


@pytest.fixture
def multiplexer_stub():
    """Create a minimal PicADSBMultiplexer without serial/socket init."""
    obj = object.__new__(PicADSBMultiplexer)
    obj.logger = MagicMock()
    obj.config = Config()
    return obj


class TestEscapeBeastData:
    def test_no_escape_needed(self, multiplexer_stub):
        data = bytes([0x01, 0x02, 0x03])
        result = multiplexer_stub._escape_beast_data(data)
        assert result == data

    def test_single_escape(self, multiplexer_stub):
        data = bytes([0x1A])
        result = multiplexer_stub._escape_beast_data(data)
        assert result == bytes([0x1A, 0x1A])

    def test_multiple_escapes(self, multiplexer_stub):
        data = bytes([0x1A, 0x00, 0x1A])
        result = multiplexer_stub._escape_beast_data(data)
        assert result == bytes([0x1A, 0x1A, 0x00, 0x1A, 0x1A])

    def test_all_escape_bytes(self, multiplexer_stub):
        data = bytes([0x1A, 0x1A, 0x1A])
        result = multiplexer_stub._escape_beast_data(data)
        assert result == bytes([0x1A, 0x1A, 0x1A, 0x1A, 0x1A, 0x1A])

    def test_empty_data(self, multiplexer_stub):
        result = multiplexer_stub._escape_beast_data(b"")
        assert result == b""

    def test_escape_preserves_other_bytes(self, multiplexer_stub):
        data = bytes(range(256))
        result = multiplexer_stub._escape_beast_data(data)
        # Only byte 0x1A should be doubled
        assert len(result) == 257  # 256 + 1 extra for the 0x1A


class TestCreateBeastMessage:
    def test_long_message_framing(self, multiplexer_stub):
        from tests.conftest import TimestampGenerator
        multiplexer_stub.timestamp_gen = TimestampGenerator()

        data = bytes.fromhex("8D406B902015A678D4D220AA4BDA")  # 14 bytes
        msg = multiplexer_stub._create_beast_message(BeastFormat.TYPE_MODES_LONG, data)

        assert msg is not None
        assert msg[0] == BeastFormat.ESCAPE
        # Type byte follows (may be escaped if 0x1A, but 0x33 != 0x1A)
        assert msg[1] == BeastFormat.TYPE_MODES_LONG

    def test_short_message_framing(self, multiplexer_stub):
        from tests.conftest import TimestampGenerator
        multiplexer_stub.timestamp_gen = TimestampGenerator()

        data = bytes(7)  # 7 zero bytes
        msg = multiplexer_stub._create_beast_message(BeastFormat.TYPE_MODES_SHORT, data)

        assert msg is not None
        assert msg[0] == BeastFormat.ESCAPE
        assert msg[1] == BeastFormat.TYPE_MODES_SHORT

    def test_modea_message_framing(self, multiplexer_stub):
        from tests.conftest import TimestampGenerator
        multiplexer_stub.timestamp_gen = TimestampGenerator()

        data = bytes(2)  # 2 zero bytes
        msg = multiplexer_stub._create_beast_message(BeastFormat.TYPE_MODEA, data)

        assert msg is not None
        assert msg[0] == BeastFormat.ESCAPE
        assert msg[1] == BeastFormat.TYPE_MODEA

    def test_no_crc_appended(self, multiplexer_stub):
        """Beast format has NO CRC field — verify message structure is correct."""
        from tests.conftest import TimestampGenerator
        multiplexer_stub.timestamp_gen = TimestampGenerator()

        data = bytes(14)  # 14 zero bytes, no 0x1A so no escaping
        ts = bytes(6)  # zero timestamp, no 0x1A
        msg = multiplexer_stub._create_beast_message(
            BeastFormat.TYPE_MODES_LONG, data, timestamp=ts
        )

        # Expected: 1 (escape) + 1 (type) + 6 (ts) + 1 (signal) + 14 (data) = 23
        # Signal level is 0xFF (default), not 0x1A, so no escaping
        assert msg is not None
        assert len(msg) == 23
