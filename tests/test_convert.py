"""Tests for _convert_to_beast: the production ASCII-to-Beast path."""
from unittest.mock import MagicMock

import pytest

from tests.conftest import BeastFormat, PicADSBMultiplexer, TimestampGenerator
from picadsb.config import Config


@pytest.fixture
def mux():
    obj = object.__new__(PicADSBMultiplexer)
    obj.logger = MagicMock()
    obj.config = Config()
    obj.timestamp_gen = TimestampGenerator()
    return obj


class TestConvertToBeast:
    @pytest.mark.parametrize("hex_data,expected_type", [
        ("8D406B902015A678D4D220AA4BDA", BeastFormat.TYPE_MODES_LONG),   # 14 bytes
        ("02E19700000000", BeastFormat.TYPE_MODES_SHORT),                # 7 bytes
        ("A1B2", BeastFormat.TYPE_MODEA),                                # 2 bytes
    ])
    def test_length_to_type_mapping(self, mux, hex_data, expected_type):
        # Input arrives from the queue as b"*<hex>;\n"
        msg = mux._convert_to_beast(b"*" + hex_data.encode() + b";\n")
        assert msg is not None
        assert msg[0] == BeastFormat.ESCAPE
        assert msg[1] == expected_type

    def test_payload_preserved(self, mux):
        hex_data = "8D406B902015A678D4D220AA4BDA"
        msg = mux._convert_to_beast(b"*" + hex_data.encode() + b";\n")
        # Frame: escape, type, 6-byte ts, signal, then data. With default
        # signal 0xFF and no 0x1A in this payload, data is the unescaped tail.
        payload = bytes.fromhex(hex_data)
        assert msg.endswith(payload)

    def test_unsupported_length_returns_none(self, mux):
        assert mux._convert_to_beast(b"*8D406B9020150012;\n") is None  # 8 bytes

    def test_invalid_hex_returns_none(self, mux):
        assert mux._convert_to_beast(b"*NOTHEX;\n") is None

    def test_empty_payload_returns_none(self, mux):
        assert mux._convert_to_beast(b"*;\n") is None
