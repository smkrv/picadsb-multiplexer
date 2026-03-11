"""Tests for message validation."""
import pytest
from unittest.mock import MagicMock

from tests.conftest import PicADSBMultiplexer
from picadsb.config import Config


@pytest.fixture
def validator():
    """Create a minimal PicADSBMultiplexer with just validate_message."""
    obj = object.__new__(PicADSBMultiplexer)
    obj.logger = MagicMock()
    obj.config = Config()
    return obj


class TestValidateMessage:
    def test_valid_long_message(self, validator):
        """14-byte Mode-S long (28 hex chars)."""
        msg = b"*8D406B902015A678D4D220AA4BDA;"
        assert validator.validate_message(msg) is True

    def test_valid_short_message(self, validator):
        """7-byte Mode-S short (14 hex chars)."""
        msg = b"*8D406B90201500;"
        assert validator.validate_message(msg) is True

    def test_valid_modea_message(self, validator):
        """2-byte Mode-A/C (4 hex chars)."""
        msg = b"*A1B2;"
        assert validator.validate_message(msg) is True

    def test_too_short(self, validator):
        assert validator.validate_message(b"*;") is False

    def test_empty(self, validator):
        assert validator.validate_message(b"") is False

    def test_no_start_marker(self, validator):
        assert validator.validate_message(b"8D406B90;") is False

    def test_no_end_marker(self, validator):
        assert validator.validate_message(b"*8D406B90") is False

    def test_invalid_hex(self, validator):
        assert validator.validate_message(b"*ZZZZZZZZZZZZZZ;") is False

    def test_hash_message_accepted(self, validator):
        """Device response messages (#...) are accepted."""
        assert validator.validate_message(b"#43-02;") is True

    def test_at_message_accepted(self, validator):
        """@ prefixed messages are accepted."""
        assert validator.validate_message(b"@ABCDEF;") is True

    def test_unsupported_length(self, validator):
        """Data length not matching 2, 7, or 14 bytes is rejected."""
        # 8 bytes = 16 hex chars — not a valid Mode-S length
        msg = b"*8D406B9020150012;"
        assert validator.validate_message(msg) is False

    def test_single_byte_message(self, validator):
        assert validator.validate_message(b"x") is False

    def test_valid_hex_wrong_length(self, validator):
        """Valid hex but 5 bytes (10 hex chars) — unsupported."""
        msg = b"*8D406B902015;"  # 7 hex pairs would be 14 chars, this is 12 = 6 bytes
        assert validator.validate_message(msg) is False
