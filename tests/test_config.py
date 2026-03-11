"""Tests for Config dataclass."""
import pytest

from picadsb.config import Config


class TestConfigDefaults:
    def test_default_port(self):
        c = Config()
        assert c.tcp_port == 30002

    def test_default_serial(self):
        c = Config()
        assert c.serial_port == "/dev/ttyACM0"

    def test_default_max_clients(self):
        c = Config()
        assert c.max_clients == 50

    def test_default_log_level(self):
        c = Config()
        assert c.log_level == "INFO"

    def test_default_remote_none(self):
        c = Config()
        assert c.remote_host is None
        assert c.remote_port is None


class TestConfigCustom:
    def test_custom_port(self):
        c = Config(tcp_port=31002)
        assert c.tcp_port == 31002

    def test_custom_max_clients(self):
        c = Config(max_clients=100)
        assert c.max_clients == 100

    def test_remote_pair(self):
        c = Config(remote_host="192.168.1.1", remote_port=30005)
        assert c.remote_host == "192.168.1.1"
        assert c.remote_port == 30005


class TestConfigValidation:
    def test_invalid_port_zero(self):
        with pytest.raises(ValueError, match="tcp_port"):
            Config(tcp_port=0)

    def test_invalid_port_high(self):
        with pytest.raises(ValueError, match="tcp_port"):
            Config(tcp_port=70000)

    def test_invalid_max_clients_zero(self):
        with pytest.raises(ValueError, match="max_clients"):
            Config(max_clients=0)

    def test_invalid_max_clients_high(self):
        with pytest.raises(ValueError, match="max_clients"):
            Config(max_clients=1001)

    def test_remote_host_without_port(self):
        with pytest.raises(ValueError, match="remote_host and remote_port"):
            Config(remote_host="host.example.com")

    def test_remote_port_without_host(self):
        with pytest.raises(ValueError, match="remote_host and remote_port"):
            Config(remote_port=30005)

    def test_invalid_signal_level(self):
        with pytest.raises(ValueError, match="signal_level"):
            Config(signal_level=256)

    def test_invalid_log_level(self):
        with pytest.raises(ValueError, match="log_level"):
            Config(log_level="TRACE")

    def test_invalid_reconnect_attempts(self):
        with pytest.raises(ValueError, match="max_reconnect_attempts"):
            Config(max_reconnect_attempts=0)

    def test_valid_boundary_port(self):
        c = Config(tcp_port=1)
        assert c.tcp_port == 1
        c = Config(tcp_port=65535)
        assert c.tcp_port == 65535


class TestConfigFromEnv:
    def test_from_env_port(self, monkeypatch):
        monkeypatch.setenv("ADSB_TCP_PORT", "31002")
        c = Config.from_env()
        assert c.tcp_port == 31002

    def test_from_env_device(self, monkeypatch):
        monkeypatch.setenv("ADSB_DEVICE", "/dev/ttyUSB0")
        c = Config.from_env()
        assert c.serial_port == "/dev/ttyUSB0"

    def test_from_env_log_level(self, monkeypatch):
        monkeypatch.setenv("ADSB_LOG_LEVEL", "DEBUG")
        c = Config.from_env()
        assert c.log_level == "DEBUG"

    def test_from_env_no_init(self, monkeypatch):
        monkeypatch.setenv("ADSB_NO_INIT", "true")
        c = Config.from_env()
        assert c.skip_init is True

    def test_from_env_no_init_false(self, monkeypatch):
        monkeypatch.setenv("ADSB_NO_INIT", "false")
        c = Config.from_env()
        assert c.skip_init is False

    def test_from_env_ignores_empty(self, monkeypatch):
        monkeypatch.setenv("ADSB_REMOTE_HOST", "")
        c = Config.from_env()
        assert c.remote_host is None

    def test_from_env_ignores_invalid(self, monkeypatch):
        monkeypatch.setenv("ADSB_TCP_PORT", "not_a_number")
        c = Config.from_env()
        assert c.tcp_port == 30002

    def test_from_env_max_clients(self, monkeypatch):
        monkeypatch.setenv("ADSB_MAX_CLIENTS", "25")
        c = Config.from_env()
        assert c.max_clients == 25
