"""Tests for remote-server connection handling."""
import socket
import time
from unittest.mock import MagicMock

import pytest

from tests.conftest import PicADSBMultiplexer
from picadsb.config import Config


@pytest.fixture
def mux():
    obj = object.__new__(PicADSBMultiplexer)
    obj.logger = MagicMock()
    obj.config = Config(remote_host="example.invalid", remote_port=30004)
    obj.remote_socket = None
    obj._last_connect_attempt = 0
    obj.last_remote_check = 0
    return obj


class TestConnectToRemote:
    def test_cooldown_prevents_reconnect_hammering(self, mux, monkeypatch):
        factory = MagicMock()
        monkeypatch.setattr(socket, "socket", factory)
        mux._last_connect_attempt = time.time()

        mux._connect_to_remote()

        factory.assert_not_called()

    def test_failed_connect_closes_socket_and_resets_state(self, mux, monkeypatch):
        sock = MagicMock()
        sock.connect.side_effect = OSError("unreachable")
        monkeypatch.setattr(socket, "socket", MagicMock(return_value=sock))

        mux._connect_to_remote()

        assert mux.remote_socket is None
        sock.close.assert_called_once()

    def test_successful_connect_sets_nonblocking(self, mux, monkeypatch):
        sock = MagicMock()
        monkeypatch.setattr(socket, "socket", MagicMock(return_value=sock))

        mux._connect_to_remote()

        assert mux.remote_socket is sock
        sock.connect.assert_called_once_with(("example.invalid", 30004))
        sock.setblocking.assert_called_once_with(False)


class TestCheckRemoteConnection:
    def test_keepalive_failure_closes_socket(self, mux):
        remote = MagicMock()
        remote.sendall.side_effect = ConnectionResetError("gone")
        mux.remote_socket = remote
        mux._build_heartbeat_message = MagicMock(return_value=b"\x1a\x31hb")

        mux._check_remote_connection()

        assert mux.remote_socket is None
        remote.close.assert_called_once()

    def test_not_configured_is_noop(self, mux):
        mux.config = Config()  # no remote host/port
        mux.remote_socket = None
        mux._connect_to_remote = MagicMock()

        mux._check_remote_connection()

        mux._connect_to_remote.assert_not_called()
