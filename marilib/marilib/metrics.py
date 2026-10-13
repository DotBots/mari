import sys
import threading
import time
from collections import deque
from typing import TYPE_CHECKING

from rich import print

from marilib.mari_protocol import (
    MARI_BROADCAST_ADDRESS,
    DefaultPayload,
    DefaultPayloadType,
    Frame,
    Header,
    MetricsProbePayload,
)
from marilib.model import MariGateway, MariNode, SCHEDULES, TestState
from marilib.probe_tracker import MAX_PROBE_RETRIES, PendingProbe

if TYPE_CHECKING:
    from marilib.marilib_edge import MarilibEdge


# Wire-byte offset of edge_tx_ts_us in an outbound probe frame.
# Layout of the bytes that MarilibEdge.send_probe hands to the serial
# adapter:
#   [1 byte EdgeEvent prefix]   (prepended by send_probe;
#                                EdgeEvent.NODE_DATA in EdgeEvent.to_bytes)
#   [Header bytes]              (sum of Header().metadata field lengths,
#                                today 21: version + type_ + network_id
#                                + dst + src + next_proto)
#   [MetricsProbePayload bytes] (the fields preceding edge_tx_ts_us in
#                                MetricsProbePayload().metadata: type,
#                                cloud_tx_ts_us, cloud_rx_ts_us,
#                                cloud_tx_count, cloud_rx_count, today 25)
# Only the leading 1 byte (EdgeEvent prefix) is a literal; the rest is
# derived from the dataclass metadata so the offset survives any
# reordering of the Mari header or probe layout.
# Used by MarilibEdge.send_probe to overwrite edge_tx_ts_us with a
# fresh monotonic timestamp inside the serial-adapter lock, just
# before the bytes leave the UART.
EDGE_TX_TS_WIRE_OFFSET = (
    1
    + sum(f.length for f in Header().metadata)
    + sum(
        f.length
        for f in MetricsProbePayload().metadata
        if f.name
        in {
            "type",
            "cloud_tx_ts_us",
            "cloud_rx_ts_us",
            "cloud_tx_count",
            "cloud_rx_count",
        }
    )
)

_TIMEOUT_TICK_S = 0.02


class MetricsTester:
    """A thread-based class to periodically test metrics to all nodes."""

    def __init__(self, marilib: "MarilibEdge", interval: float = 3):
        self.marilib = marilib
        self.set_interval(interval)
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        # Monotonic timestamps of recent probe transmissions, for the measured
        # send rate. Retries are counted, which is the point: the nominal rate
        # (nodes / interval) understates the link's real probe load whenever
        # probes are timing out. deque append/popleft are thread-safe, so the
        # TUI can read this while the tester thread writes.
        self._sent_ts: deque[float] = deque(maxlen=4096)

    def set_interval(self, interval: float):
        """Set the seconds between probes to the same node. 0 disables probing.

        Only non-negative is enforced. There is no upper bound to enforce: a
        long interval simply samples sparsely and widens the rolling PDR
        window, since MARI_PROBE_STATS_MAX_LEN caps how many probe payloads are
        retained per node, not how far apart they may be. (It was previously
        used as a seconds ceiling, comparing a duration against a deque
        length.)

        The bound that would be worth having is at the other end - an interval
        short enough that N/interval exceeds the gateway's downlink capacity -
        but that depends on the schedule and the node count, neither of which
        is known here or fixed for the run. It is reported instead: the TUI
        shows the probe stream's live share of downlink.
        """
        if interval < 0:
            raise ValueError(f"Probe interval must be >= 0, got {interval}")
        self.interval = interval

    def start(self):
        """Starts the metrics testing thread."""
        if self.interval == 0:
            print("[yellow]Metrics tester disabled.[/]")
            return
        print(f"[yellow]Metrics tester started with interval {self.interval} seconds.[/]")
        self._thread.start()

    def stop(self):
        """Stops the metrics testing thread."""
        self._stop_event.set()
        if self._thread.is_alive():
            self._thread.join()
        print("[yellow]Metrics tester stopped.[/]")

    def _probe_timeout_ms(self) -> float | None:
        schedule = SCHEDULES.get(self.marilib.gateway.info.schedule_id)
        if schedule is None:
            return None
        sf_duration = float(schedule["sf_duration"])
        return 2.0 * sf_duration  # tune other value if needed

    def _wait_with_timeout_checks(self, duration_s: float) -> None:
        """Sleep in small chunks so probe timeouts are checked promptly."""
        deadline = time.monotonic() + duration_s
        while not self._stop_event.is_set():
            self.check_timeouts()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._stop_event.wait(min(_TIMEOUT_TICK_S, remaining))

    def _run(self):
        """The main loop for the testing thread."""
        self._wait_with_timeout_checks(self.interval)

        while not self._stop_event.is_set():
            nodes = list(self.marilib.gateway.nodes)
            if not nodes:
                self._wait_with_timeout_checks(self.interval)
                continue

            for node in nodes:
                if self._stop_event.is_set():
                    break
                self._send_edge_probe(node)
                sleep_duration = self.interval / len(nodes)
                self._wait_with_timeout_checks(sleep_duration)

    def timestamp_us(self) -> int:
        """Returns a monotonic timestamp in microseconds.

        Monotonic (not wall-clock), so an NTP step adjustment cannot
        create apparent jumps in measured latency. Edge stamps both
        ends of the probe round trip with the same clock; the node
        firmware echoes edge_tx_ts_us back unchanged as an opaque
        8-byte blob.
        """
        return time.monotonic_ns() // 1000

    def _register_pending_probe(
        self, node: MariNode, edge_tx_ts_us: int, sent_at_us: int, retry_count: int = 0
    ) -> None:
        node.probe_tracker.register_pending(
            PendingProbe(
                edge_tx_ts_us=edge_tx_ts_us,
                node_address=node.address,
                sent_at_us=sent_at_us,
                retry_count=retry_count,
            )
        )

    def _transmit_probe(self, node: MariNode, retry_count: int = 0) -> int | None:
        """Send a probe and register it as pending. Returns edge_tx_ts_us."""
        payload = MetricsProbePayload()
        with self.marilib.lock:
            payload.edge_tx_count = node.probe_increment_tx_count()
        payload_bytes = payload.to_bytes()
        # send_probe takes mari.lock too — don't call it under that lock.
        edge_tx_ts_us = self.marilib.send_probe(node.address, payload_bytes)
        if edge_tx_ts_us is None:
            return None
        self._sent_ts.append(time.monotonic())
        with self.marilib.lock:
            self._register_pending_probe(node, edge_tx_ts_us, edge_tx_ts_us, retry_count)
        return edge_tx_ts_us

    def probe_rate_hz(self, window_s: float = 10.0) -> float:
        """Measured probe transmissions per second over the last `window_s`.

        Measured rather than nominal, so retries after a timeout show up. A
        reading well above nodes/interval means probes are being retransmitted,
        which puts more on the downlink than the requested probe share.
        """
        now = time.monotonic()
        while self._sent_ts and now - self._sent_ts[0] > window_s:
            self._sent_ts.popleft()
        return len(self._sent_ts) / window_s

    def _send_edge_probe(self, node: MariNode) -> None:
        """Send a probe if the node has no pending one."""
        with self.marilib.lock:
            if node.has_pending_probe():
                return
        self._transmit_probe(node)

    def send_metrics_request(self, node: MariNode, marilib_type: str):
        """Sends a metrics request packet to a specific address."""
        if marilib_type == "edge":
            self._send_edge_probe(node)
        elif marilib_type == "cloud":
            # Cloud probes are still stamped on call (MQTT publish is
            # async and the cloud's MetricsTester is currently never
            # started — marilib_cloud.py:55-57 — so this path is unused).
            payload = MetricsProbePayload()
            payload.cloud_tx_ts_us = self.timestamp_us()
            payload.cloud_tx_count = node.probe_increment_tx_count()
            payload_bytes = payload.to_bytes()
            self.marilib.send_frame(node.address, payload_bytes)

    def check_timeouts(self) -> None:
        """Expire pending probes, record effective latency, and retry."""
        timeout_ms = self._probe_timeout_ms()
        if timeout_ms is None:
            return

        timeout_us = int(timeout_ms * 1000)
        now_us = self.timestamp_us()
        expired: list[tuple[MariNode, PendingProbe]] = []
        retries: list[tuple[MariNode, int]] = []

        with self.marilib.lock:
            for node in self.marilib.gateway.nodes:
                for pending in list(node.sent_probe_packets.values()):
                    if now_us - pending.sent_at_us > timeout_us:
                        expired.append((node, pending))

            for node, pending in expired:
                node.probe_tracker.pop_pending(pending.edge_tx_ts_us)
                node.probe_tracker.record_effective_sample(timeout_ms)

                if pending.retry_count < MAX_PROBE_RETRIES:
                    retries.append((node, pending.retry_count + 1))

        for node, retry_count in retries:
            self._transmit_probe(node, retry_count=retry_count)

    def _complete_pending_probe(
        self, node: MariNode, edge_tx_ts_us: int, rx_ts_us: int
    ) -> PendingProbe | None:
        pending = node.probe_tracker.pop_pending(edge_tx_ts_us)
        if pending is None:
            return None
        effective_ms = (rx_ts_us - pending.sent_at_us) / 1000.0
        node.probe_tracker.record_effective_sample(effective_ms)
        return pending

    def handle_response_edge(self, frame: Frame, rx_ts_us: int | None = None):
        """
        Processes a metrics response frame.
        This should be called when a LATENCY_DATA event is received.

        `rx_ts_us` is the monotonic-microsecond timestamp captured by
        the serial adapter when the HDLC frame became READY (i.e.
        right after the wire bytes arrived). When supplied, it's used
        as edge_rx_ts_us instead of stamping here — handler runs
        inside mari.lock, which can be held by render_tui / update /
        send_frame for tens of ms, so stamping here would inflate the
        measured RTT.
        """
        node = self.marilib.gateway.get_node(frame.header.source)
        if not node:
            print(f"[red]Node not found: {frame.header.source:016x}[/]")
            return

        try:
            payload = MetricsProbePayload().from_bytes(frame.payload)
            if payload.type_ != DefaultPayloadType.METRICS_PROBE:
                print(f"[red]Expected METRICS_PROBE, got {payload.type_}[/]")
                return

        except Exception as e:
            print(f"[red]Error parsing metrics response: {e}[/]")
            return

        rx_ts = rx_ts_us if rx_ts_us is not None else self.timestamp_us()

        # Stamp before matching. A reply that arrives after its timeout has no
        # pending entry left, but it did arrive, so edge_rx_ts_us and
        # edge_rx_count are both known and both belong on the wire: the frame
        # is forwarded to the cloud right after this returns, and an unstamped
        # edge_rx_ts_us of 0 makes latency_roundtrip_node_edge_ms() read as a
        # large negative value there. Counting every reply in edge_rx_count
        # also makes pdr_uplink_uart (edge_rx vs gw_rx) count what it names.
        payload.edge_rx_ts_us = rx_ts
        payload.edge_rx_count = node.probe_increment_rx_count()

        pending = self._complete_pending_probe(node, payload.edge_tx_ts_us, rx_ts)
        if pending is None:
            # Late reply: check_timeouts already recorded this probe as a
            # timeout in the effective-latency samples, so keep it out of
            # probe_stats rather than counting one probe twice. Returned
            # stamped so the cloud still sees its true round trip.
            return payload

        node.save_probe_stats(payload)

        # print(f"<<< received metrics probe from {frame.header.source:016x}: {payload}")
        # print(f"    size is {len(frame.payload)} bytes: {frame.payload.hex()}\n")

        # print(f"    latency_roundtrip_node_edge_ms: {payload.latency_roundtrip_node_edge_ms()}")
        # print(f"    pdr_uplink_radio: {payload.pdr_uplink_radio(node.probe_stats_start_epoch)}")
        # print(f"    pdr_downlink_radio: {payload.pdr_downlink_radio(node.probe_stats_start_epoch)}")
        # print(f"    pdr_uplink_uart: {payload.pdr_uplink_uart(node.probe_stats_start_epoch)}")
        # print(f"    pdr_downlink_uart: {payload.pdr_downlink_uart(node.probe_stats_start_epoch)}")
        # print(f"    rssi_at_node_dbm: {payload.rssi_at_node_dbm()}")
        # print(f"    rssi_at_gw_dbm: {payload.rssi_at_gw_dbm()}")

        return payload

    def handle_response_cloud(self, frame: Frame, gateway: MariGateway, node: MariNode):
        """
        Processes a metrics response frame.
        This should be called when a LATENCY_DATA event is received.
        """
        try:
            payload = MetricsProbePayload().from_bytes(frame.payload)
            if payload.type_ != DefaultPayloadType.METRICS_PROBE:
                print(f"[red]Expected METRICS_PROBE, got {payload.type_}[/]")
                return

        except Exception as e:
            print(f"[red]Error parsing metrics response: {e}[/]")
            return

        payload.cloud_rx_ts_us = self.timestamp_us()
        payload.cloud_rx_count = node.probe_increment_rx_count()

        node.save_probe_stats(payload)

        # print(f"<<< received metrics probe from {frame.header.source:016x}: {payload}")
        # print(f"    size is {len(frame.payload)} bytes: {frame.payload.hex()}\n")

        # print(f"    latency_roundtrip_node_edge_ms: {payload.latency_roundtrip_node_edge_ms()}")
        # print(f"    pdr_uplink_radio: {payload.pdr_uplink_radio(node.probe_stats_start_epoch)}")
        # print(f"    pdr_downlink_radio: {payload.pdr_downlink_radio(node.probe_stats_start_epoch)}")
        # print(f"    pdr_uplink_uart: {payload.pdr_uplink_uart(node.probe_stats_start_epoch)}")
        # print(f"    pdr_downlink_uart: {payload.pdr_downlink_uart(node.probe_stats_start_epoch)}")
        # print(f"    rssi_at_node_dbm: {payload.rssi_at_node_dbm()}")
        # print(f"    rssi_at_gw_dbm: {payload.rssi_at_gw_dbm()}")

        return payload


def probe_interval_for_slotframes(mari: "MarilibEdge", every: int) -> float | None:
    """Seconds between probes to a node, for one probe every `every` slotframes.

    The slotframe is the network's own clock - latency, queue depth and the
    probe timeout are all measured in it - so a cadence expressed in slotframes
    samples every schedule at the same rate relative to its own dynamics, which
    is what makes latency comparable across them.

    It also holds the probe's share of downlink nearly constant without being
    asked to. That share is N / (every * d_down), and N/d_down is 5.00, 4.40,
    4.13, 4.64 for tiny, medium, big and huge - the schedules give each node an
    uplink slot and scale downlink alongside, so the ratio barely moves. The
    share is therefore about 4.6/every on any of them: ~23% at every=20, ~15%
    at 30. Below about 15 the probe stream starts crowding out the traffic it
    is supposed to be measuring.

    Returns None until the gateway has reported a schedule we know.
    """
    schedule = SCHEDULES.get(mari.gateway.info.schedule_id)
    if schedule is None or every <= 0:
        return None
    return every * float(schedule["sf_duration"]) / 1000.0


class LoadTester(threading.Thread):
    """Generates filler downlink traffic up to a target link occupancy.

    `--load` is the share of the gateway's downlink capacity the link should
    carry in total, metrics probes included. Probes are unicast downlink
    packets competing for the same D slots as the filler, so the filler rate
    is the target minus the probe rate. Without that subtraction, asking for
    75% on the huge schedule puts 90% on the link, which measures the knee
    rather than a loaded network.
    """

    def __init__(
        self,
        mari: "MarilibEdge",
        test_state: TestState,
        stop_event: threading.Event,
        probe_interval: float = 0.0,
    ):
        super().__init__(daemon=True)
        self.mari = mari
        self.test_state = test_state
        self._stop_event = stop_event
        self.probe_interval = probe_interval
        self._warned_over_budget = False

    def run(self):
        while not self._stop_event.is_set():
            delay = self.compute_delay()
            if delay is None:
                # Gateway schedule not known yet, or the probe stream already
                # fills the budget. Re-check shortly: both can change as the
                # gateway reports in and as nodes join or leave.
                self._stop_event.wait(0.1)
                continue

            with self.mari.lock:
                nodes_exist = bool(self.mari.gateway.nodes)

            if nodes_exist:
                self.mari.send_frame(
                    MARI_BROADCAST_ADDRESS,
                    DefaultPayload(type_=DefaultPayloadType.METRICS_LOAD).with_filler_bytes(180),
                )
            self._stop_event.wait(delay)

    def probe_rate(self) -> float:
        """Nominal probe packets/s: one per node per probe interval.

        Nominal, not measured: a probe that times out is retransmitted (up to
        MAX_PROBE_RETRIES), so under heavy loss the real probe rate is higher
        and the link carries more than the requested share. That regime is
        visible in the log as a non-zero pending-probe count and an effective
        latency pinned at two slotframes.
        """
        if self.probe_interval <= 0:
            return 0.0
        with self.mari.lock:
            node_count = len(self.mari.gateway.nodes)
        return node_count / self.probe_interval

    def compute_delay(self) -> float | None:
        """Seconds between filler packets, or None if none should be sent.

        Recomputed per packet rather than latched at startup: the probe stream
        scales with the node count, so a rate fixed during formation would
        overshoot the target once the network filled up.
        """
        if self.test_state.load == 0:
            return None
        max_rate = self.mari.get_max_downlink_rate()
        if max_rate == 0:
            return None  # gateway schedule not reported yet

        self.test_state.rate = int(max_rate)
        target_pps = max_rate * (self.test_state.load / 100.0)
        filler_pps = target_pps - self.probe_rate()

        if filler_pps <= 0:
            if not self._warned_over_budget:
                self._warned_over_budget = True
                sys.stderr.write(
                    f"Warning: metrics probes alone offer "
                    f"{self.probe_rate() / max_rate:.0%} of downlink capacity, at or above the "
                    f"requested {self.test_state.load}%. Sending no filler traffic; either raise "
                    f"--load or raise --metrics-probe-interval.\n"
                )
            return None

        self._warned_over_budget = False
        return 1.0 / filler_pps
