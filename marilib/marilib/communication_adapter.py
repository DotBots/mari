import base64
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from urllib.parse import urlparse

import paho.mqtt.client as mqtt
from rich import print

from marilib.serial_hdlc import (
    HDLCDecodeException,
    HDLCHandler,
    HDLCState,
    hdlc_encode,
)
from marilib.serial_uart import SERIAL_DEFAULT_BAUDRATE, SerialInterface


class CommunicationAdapterBase(ABC):
    """Base class for interface adapters."""

    @abstractmethod
    def init(self, on_data_received: callable):
        """Initialize the interface."""

    @abstractmethod
    def close(self):
        """Close the interface."""


@dataclass
class SerialAdapterStats:
    """Host-side counters for the UART hop, the mirror of the gateway's own.

    The gateway counts what it received; these count what the host sent and
    what came back. A discrepancy between the two sides localizes a loss to
    the wire rather than to either endpoint.
    """

    write_calls: int = 0  # serial.write() calls, one per frame
    write_bytes: int = 0  # HDLC-encoded bytes handed to pyserial
    write_errors: int = 0  # writes that raised
    rx_frames_ok: int = 0  # HDLC frames decoded from the gateway
    rx_hdlc_err: int = 0  # frames the host decoder rejected

    def as_dict(self) -> dict:
        return asdict(self)


class SerialAdapter(CommunicationAdapterBase):
    """Class used to interface with the serial port."""

    def __init__(self, port, baudrate=SERIAL_DEFAULT_BAUDRATE):
        self.port = port
        self.baudrate = baudrate
        self.hdlc_handler = HDLCHandler()
        self.stats = SerialAdapterStats()

    def on_byte_received(self, byte):
        self.hdlc_handler.handle_byte(byte)
        if self.hdlc_handler.state == HDLCState.READY:
            # Capture the RX timestamp as close to wire-arrival as we
            # can — before downstream callbacks acquire mari.lock,
            # which can be held by render_tui / update for tens of ms
            # and would otherwise inflate measured RTT.
            rx_ts_us = time.monotonic_ns() // 1000
            try:
                payload = self.hdlc_handler.payload
            except HDLCDecodeException as e:
                self.stats.rx_hdlc_err += 1
                print(f"Error decoding payload: {e}")
                return
            if not payload:
                # a short frame or a bad FCS; HDLCHandler.payload logs the
                # reason and hands back an empty buffer
                self.stats.rx_hdlc_err += 1
                return
            self.stats.rx_frames_ok += 1
            self.on_data_received(payload, rx_ts_us)

    def init(self, on_data_received: callable):
        self.on_data_received = on_data_received
        self.serial = SerialInterface(self.port, self.baudrate, self.on_byte_received)
        print(f"[yellow]Connected to serial port {self.port} at {self.baudrate} baud[/]")

    def close(self):
        print("[yellow]Disconnect from gateway...[/]")

    def send_data(self, data, late_stamp_offset: int | None = None) -> int | None:
        """Write data over the UART.

        If `late_stamp_offset` is given, overwrite
        bytes[late_stamp_offset : late_stamp_offset + 8] of `data` with
        time.monotonic_ns() // 1000 as little-endian uint64 AFTER the
        serial lock is acquired and just before HDLC encoding. Used by
        metrics probes to capture their TX timestamp as close to
        wire-departure as possible.

        Returns the stamped microsecond value when `late_stamp_offset`
        is set, otherwise None.
        """
        stamp_us = None
        with self.serial.lock:  # Use the existing lock for thread safety
            if late_stamp_offset is not None:
                data = bytearray(data)
                stamp_us = time.monotonic_ns() // 1000
                data[late_stamp_offset : late_stamp_offset + 8] = stamp_us.to_bytes(8, "little")
            self.serial.serial.flush()
            encoded = hdlc_encode(data)
            try:
                self.serial.write(encoded)
            except Exception:
                self.stats.write_errors += 1
                raise
            self.stats.write_calls += 1
            self.stats.write_bytes += len(encoded)
        return stamp_us


def parse_mqtt_url(url: str):
    """Parse a `mqtt(s)://[user:pass@]host[:port]` URL into its parts.

    Returns `(host, port, use_tls, username, password)`. A missing port
    defaults to 8883 (TLS) / 1883 (plain). `username`/`password` are
    None when no userinfo is present. Raises ValueError on a bad scheme.

    The single source of truth for the MQTT-URL → parts mapping, so the
    `dotbot controller` / `dotbot swarm` CLIs (which can't import each
    other) agree with `MQTTAdapter.from_url` on the parse + default port.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("mqtt", "mqtts"):
        raise ValueError(f"Invalid MQTT URL: {url} (must start with mqtt:// or mqtts://)")
    use_tls = parsed.scheme == "mqtts"
    port = parsed.port or (8883 if use_tls else 1883)
    return parsed.hostname, port, use_tls, parsed.username, parsed.password


class MQTTAdapter(CommunicationAdapterBase):
    """Class used to interface with MQTT."""

    def __init__(
        self,
        host,
        port,
        is_edge: bool,
        use_tls: bool = False,
        username: str | None = None,
        password: str | None = None,
    ):
        self.host = host
        self.port = port
        self.is_edge = is_edge
        self.network_id = None
        self.client = None
        self.on_data_received = None
        self.use_tls = use_tls
        self.username = username
        self.password = password
        # optimize qos for throughput
        # 0 = no delivery guarantee, 1 = at least once, 2 = exactly once
        self.qos = 0

    @classmethod
    def from_url(
        cls,
        url: str,
        is_edge: bool,
        username: str | None = None,
        password: str | None = None,
    ):
        """Build an MQTTAdapter from a `mqtt(s)://[user:pass@]host[:port]` URL.

        Credentials may be embedded in the URL (`mqtts://user:pass@host:port`)
        or passed explicitly via `username` / `password` (explicit wins —
        callers thread env-var credentials this way).
        """
        host, port, use_tls, url_user, url_pass = parse_mqtt_url(url)
        return cls(
            host,
            port,
            is_edge,
            use_tls=use_tls,
            username=username if username is not None else url_user,
            password=password if password is not None else url_pass,
        )

    # ==== public methods ====

    def is_ready(self) -> bool:
        return self.client is not None and self.client.is_connected()

    def set_network_id(self, network_id: str):
        self.network_id = network_id

    def set_on_data_received(self, on_data_received: callable):
        self.on_data_received = on_data_received

    def update(self, network_id: str, on_data_received: callable):
        if self.network_id is None:
            # might have been set by set_network_id
            self.network_id = network_id
        else:
            # TODO: handle the case when the network_id changes
            pass
        if self.network_id is None:
            # wait a bit, network_id not set yet
            return
        if self.on_data_received is None:
            self.set_on_data_received(on_data_received)
        if not self.is_ready():
            self.init()

    def init(self):
        if self.client:
            # already initialized, do nothing
            return
        if self.network_id is None:
            # network_id not set yet
            return

        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            protocol=mqtt.MQTTProtocolVersion.MQTTv5,
        )
        if self.username is not None:
            self.client.username_pw_set(self.username, self.password)
        if self.use_tls:
            self.client.tls_set_context(context=None)
        self.client.on_log = self._on_log
        self.client.on_connect = self._on_connect_edge if self.is_edge else self._on_connect_cloud
        self.client.on_message = self._on_message_edge if self.is_edge else self._on_message_cloud
        self.client.connect(self.host, self.port, 60)
        print(f"[yellow]Connected to MQTT broker on {self.host}:{self.port}[/]")
        self.client.loop_start()

    def close(self):
        if self.client is None:
            return
        self.client.disconnect()
        self.client.loop_stop()

    def send_data_to_edge(self, data):
        if not self.is_ready():
            return
        self.client.publish(
            f"/mari/{self.network_id}/to_edge",
            base64.b64encode(data).decode(),
            qos=self.qos,
        )

    def send_data_to_cloud(self, data):
        if not self.is_ready():
            return
        self.client.publish(
            f"/mari/{self.network_id}/to_cloud",
            base64.b64encode(data).decode(),
            qos=self.qos,
        )

    # ==== private methods ====

    # TODO: de-duplicate the _on_message_* functions? decide as the integration evolves
    def _on_message_edge(self, client, userdata, message):
        try:
            data = base64.b64decode(message.payload)
        except Exception as e:
            # print the error and a stacktrace
            print(f"[red]Error decoding MQTT message: {e}[/]")
            print(f"[red]Message: {message.payload}[/]")
            return
        self.on_data_received(data)

    def _on_message_cloud(self, client, userdata, message):
        try:
            data = base64.b64decode(message.payload)
        except Exception as e:
            # print the error and a stacktrace
            print(f"[red]Error decoding MQTT message: {e}[/]")
            print(f"[red]Message: {message.payload}[/]")
            return
        self.on_data_received(data)

    def _on_log(self, client, userdata, paho_log_level, messages):
        # print(messages)
        pass

    def _on_connect_edge(self, client, userdata, flags, reason_code, properties):
        self.client.subscribe(f"/mari/{self.network_id}/to_edge", qos=self.qos)
        print(f"[yellow]Subscribed to /mari/{self.network_id}/to_edge[/]")

    def _on_connect_cloud(self, client, userdata, flags, reason_code, properties):
        self.client.subscribe(f"/mari/{self.network_id}/to_cloud", qos=self.qos)
        print(f"[yellow]Subscribed to /mari/{self.network_id}/to_cloud[/]")


class MQTTAdapterDummy(MQTTAdapter):
    """Dummy MQTT adapter, does nothing, for when edge runs only locally, without a cloud."""

    def __init__(self, host="", port=0, is_edge=True):
        super().__init__(host, port, is_edge)

    def is_ready(self) -> bool:
        """Dummy adapter is never ready."""
        return False

    def init(self):
        pass

    def close(self):
        pass

    def send_data_to_edge(self, data):
        pass

    def send_data_to_cloud(self, data):
        pass
