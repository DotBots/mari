"""Tests for MQTTAdapter URL parsing and credential handling."""

import socket
import ssl

import pytest

from marilib.communication_adapter import (
    MQTT_INSECURE_ENV,
    MQTTAdapter,
    mqtt_options_from_env,
    parse_mqtt_url,
)


def test_parse_mqtt_url_parts():
    assert parse_mqtt_url("mqtts://argus:8883") == ("argus", 8883, True, None, None)
    assert parse_mqtt_url("mqtt://localhost:1883") == (
        "localhost",
        1883,
        False,
        None,
        None,
    )


def test_parse_mqtt_url_defaults_port():
    assert parse_mqtt_url("mqtts://argus")[1] == 8883
    assert parse_mqtt_url("mqtt://argus")[1] == 1883


def test_parse_mqtt_url_credentials():
    host, port, use_tls, user, pw = parse_mqtt_url("mqtts://alice:s3cret@argus:8883")
    assert (host, port, use_tls, user, pw) == ("argus", 8883, True, "alice", "s3cret")


def test_from_url_plain():
    a = MQTTAdapter.from_url("mqtt://localhost:1883", is_edge=True)
    assert a.host == "localhost"
    assert a.port == 1883
    assert a.use_tls is False
    assert a.username is None
    assert a.password is None


def test_from_url_tls():
    a = MQTTAdapter.from_url("mqtts://argus.paris.inria.fr:8883", is_edge=False)
    assert a.host == "argus.paris.inria.fr"
    assert a.port == 8883
    assert a.use_tls is True


def test_from_url_defaults_port_when_missing():
    # No explicit port → 8883 for TLS, 1883 for plain.
    assert MQTTAdapter.from_url("mqtts://argus", is_edge=False).port == 8883
    assert MQTTAdapter.from_url("mqtt://argus", is_edge=True).port == 1883


def test_from_url_with_embedded_credentials():
    # A userinfo prefix must not break the host:port split.
    a = MQTTAdapter.from_url("mqtts://alice:s3cret@argus:8883", is_edge=False)
    assert a.host == "argus"
    assert a.port == 8883
    assert a.username == "alice"
    assert a.password == "s3cret"


def test_from_url_explicit_credentials_override_url():
    a = MQTTAdapter.from_url(
        "mqtts://urluser:urlpass@argus:8883",
        is_edge=False,
        username="envuser",
        password="envpass",
    )
    # Explicit (env-fed) credentials win over URL-embedded ones.
    assert a.username == "envuser"
    assert a.password == "envpass"


def test_from_url_explicit_credentials_without_url_userinfo():
    a = MQTTAdapter.from_url("mqtts://argus:8883", is_edge=False, username="alice", password="pw")
    assert a.username == "alice"
    assert a.password == "pw"


def test_from_url_rejects_bad_scheme():
    import pytest

    with pytest.raises(ValueError):
        MQTTAdapter.from_url("http://nope:1", is_edge=True)


def test_constructor_credentials_default_none():
    a = MQTTAdapter("h", 1883, is_edge=True)
    assert a.username is None
    assert a.password is None


@pytest.mark.parametrize("value", ["1", "true", "YES", " on "])
def test_env_insecure_accepts_truthy_values(monkeypatch, value):
    monkeypatch.setenv(MQTT_INSECURE_ENV, value)
    assert mqtt_options_from_env()["insecure"] is True


@pytest.mark.parametrize("value", ["", "0", "no", "off", "maybe"])
def test_env_insecure_rejects_anything_else(monkeypatch, value):
    monkeypatch.setenv(MQTT_INSECURE_ENV, value)
    assert mqtt_options_from_env()["insecure"] is False


def test_env_options_carry_credentials(monkeypatch):
    monkeypatch.setenv("MARI_MQTT_USER", "alice")
    monkeypatch.setenv("MARI_MQTT_PASS", "pw")
    monkeypatch.delenv(MQTT_INSECURE_ENV, raising=False)
    a = MQTTAdapter.from_url("mqtts://argus:8883", is_edge=True, **mqtt_options_from_env())
    assert (a.username, a.password, a.insecure) == ("alice", "pw", False)


def test_default_tls_context_is_paho_verifying_default():
    assert MQTTAdapter("h", 8883, is_edge=True, use_tls=True)._tls_context() is None


def test_insecure_tls_context_skips_verification():
    a = MQTTAdapter.from_url("mqtts://argus:8883", is_edge=True, insecure=True)
    context = a._tls_context()
    assert context.verify_mode == ssl.CERT_NONE
    assert context.check_hostname is False


def test_unreachable_broker_does_not_raise():
    """init() runs on the serial reader thread; raising there kills it."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    a = MQTTAdapter("127.0.0.1", port, is_edge=True)
    try:
        a.update("1234", lambda data: None)
        assert a.client is not None
        assert not a.is_ready()
    finally:
        a.close()
