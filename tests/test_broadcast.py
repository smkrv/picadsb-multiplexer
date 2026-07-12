"""Tests for _broadcast_message, _send_heartbeat and client lifecycle."""
import select
import time
from unittest.mock import MagicMock

import pytest

from tests.conftest import PicADSBMultiplexer, TimestampGenerator
from picadsb.config import Config

BEAST_FRAME = bytes([0x1A, 0x32]) + bytes(6) + b"\xff" + bytes(7)


@pytest.fixture
def mux():
    obj = object.__new__(PicADSBMultiplexer)
    obj.logger = MagicMock()
    obj.config = Config()
    obj.timestamp_gen = TimestampGenerator()
    obj.stats = {'clients_current': 0}
    obj.clients = []
    obj.client_last_active = {}
    obj.remote_socket = None
    return obj


class TestBroadcastMessage:
    def test_failing_client_removed_others_still_served(self, mux, monkeypatch):
        good, bad = MagicMock(), MagicMock()
        bad.sendall.side_effect = BrokenPipeError("client gone")
        mux.clients = [good, bad]
        mux.client_last_active = {good: 0, bad: 0}
        monkeypatch.setattr(select, "select", lambda r, w, x, t=0: ([], list(w), []))

        mux._broadcast_message(BEAST_FRAME)

        good.sendall.assert_called_once_with(BEAST_FRAME)
        assert bad not in mux.clients
        assert good in mux.clients
        assert mux.stats['clients_current'] == 1

    def test_remote_failure_closes_socket(self, mux, monkeypatch):
        remote = MagicMock()
        remote.sendall.side_effect = ConnectionResetError("remote gone")
        mux.remote_socket = remote
        monkeypatch.setattr(select, "select", lambda r, w, x, t=0: ([], [], []))

        mux._broadcast_message(BEAST_FRAME)

        assert mux.remote_socket is None
        remote.close.assert_called_once()


class TestHeartbeat:
    def test_heartbeat_refreshes_client_activity(self, mux):
        # Regression: read-only clients (dump1090) send nothing upstream;
        # a delivered heartbeat must count as activity or _check_timeouts
        # evicts them during quiet periods.
        client = MagicMock()
        mux.clients = [client]
        mux.client_last_active = {client: 0}

        mux._send_heartbeat()

        client.sendall.assert_called_once()
        assert mux.client_last_active[client] > 0

    def test_heartbeat_failure_disconnects_client(self, mux):
        client = MagicMock()
        client.sendall.side_effect = BrokenPipeError("client gone")
        mux.clients = [client]
        mux.client_last_active = {client: 0}

        mux._send_heartbeat()

        assert client not in mux.clients


class TestCheckTimeouts:
    def test_active_client_kept_inactive_evicted(self, mux):
        active, stale = MagicMock(), MagicMock()
        now = time.time()
        mux.clients = [active, stale]
        mux.client_last_active = {
            active: now,
            stale: now - mux.config.no_data_timeout - 1,
        }

        mux._check_timeouts()

        assert active in mux.clients
        assert stale not in mux.clients
