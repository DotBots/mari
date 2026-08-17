import statistics
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import IntEnum

import rich

from marilib.mari_protocol import MARI_GATEWAY_INFO_VERSION, Frame, MetricsProbePayload
from marilib.probe_tracker import ProbeTracker
from marilib.protocol import Packet, PacketFieldMetadata

# schedules taken from: https://github.com/DotBots/mari-evaluation/blob/main/simulations/radio-schedule.ipynb
SCHEDULES = {
    # schedule_id: {name, max_nodes, d_down, sf_duration_ms}
    1: {
        "name": "huge",
        "slots": "BBB" + ("UUSDUUUUSDUUU" * 11) + "U" * 0,
        "max_nodes": 102,
        "d_down": 22,
        "sf_duration": 256.88,
    },
    3: {
        "name": "big",
        "slots": "BBB" + ("UUSDUUUUSDUU" * 8) + "U" * 0,
        "max_nodes": 66,
        "d_down": 16,
        "sf_duration": 174.12,
    },
    4: {
        "name": "medium",
        "slots": "BBB" + ("UUSDUUUUSDUU" * 5) + "U" * 0,
        "max_nodes": 44,
        "d_down": 10,
        "sf_duration": 115.51,
    },
    6: {
        "name": "tiny",
        "slots": "BBB" + ("UUSDUUUUSDUU" * 1) + "U" * 0,
        "max_nodes": 10,
        "d_down": 2,
        "sf_duration": 29.31,
    },
}

EMPTY_SCHEDULE_DATA = {
    "name": "unknown",
    "slots": "",
    "max_nodes": 0,
    "d_down": 0,
    "sf_duration": 0,
}

MARI_TIMEOUT_NODE_IS_ALIVE = 3  # seconds
MARI_TIMEOUT_GATEWAY_IS_ALIVE = 3  # seconds

# MARI_PROBE_STATS_EPOCH_DURATION_ASN = 565 * 20 # about 10 seconds
MARI_PROBE_STATS_EPOCH_DURATION_ASN = 565 * 60  # about 30 seconds
# MARI_PROBE_STATS_EPOCH_DURATION_ASN = 565 * 2 # about 1 second

MARI_PROBE_STATS_MAX_LEN = 10


@dataclass
class TestState:
    rate: int = 0
    load: int = 0
    probe_interval: float = 0.0  # seconds between probes to the same node
    probe_every: int = 0  # that cadence in slotframes, 0 when set directly in seconds


class EdgeEvent(IntEnum):
    NODE_JOINED = 1
    NODE_LEFT = 2
    NODE_DATA = 3
    NODE_KEEP_ALIVE = 4
    GATEWAY_INFO = 5
    UNKNOWN = 255

    @classmethod
    def to_bytes(cls, event: "EdgeEvent") -> bytes:
        return event.value.to_bytes(1, "little")


@dataclass
class NodeInfoCloud(Packet):
    metadata: list[PacketFieldMetadata] = field(
        default_factory=lambda: [
            PacketFieldMetadata(name="address", length=8),
            PacketFieldMetadata(name="gateway_address", length=8),
        ],
        repr=False,
    )
    address: int = 0
    gateway_address: int = 0


@dataclass
class NodeInfoEdge(Packet):
    metadata: list[PacketFieldMetadata] = field(
        default_factory=lambda: [
            PacketFieldMetadata(name="address", length=8),
        ],
        repr=False,
    )
    address: int = 0

    def to_cloud(self, gateway_address: int) -> NodeInfoCloud:
        return NodeInfoCloud(address=self.address, gateway_address=gateway_address)


@dataclass
class NodeStatsReply(Packet):
    """Dataclass representing the statistics packet sent back by a node."""

    metadata: list[PacketFieldMetadata] = field(
        default_factory=lambda: [
            PacketFieldMetadata(name="rx_app_packets", length=4),
            PacketFieldMetadata(name="tx_app_packets", length=4),
        ]
    )
    rx_app_packets: int = 0
    tx_app_packets: int = 0


@dataclass
class FrameLogEntry:
    frame: Frame
    ts: datetime = field(default_factory=lambda: datetime.now())


@dataclass
class MetricsStats:
    latencies: deque = field(default_factory=lambda: deque(maxlen=50))
    # TODO: Add PDR stats

    def add_latency(self, rtt_seconds: float):
        self.latencies.append(rtt_seconds * 1000)

    @property
    def last_ms(self) -> float:
        return self.latencies[-1] if self.latencies else 0.0

    @property
    def avg_ms(self) -> float:
        return statistics.mean(self.latencies) if self.latencies else 0.0

    @property
    def min_ms(self) -> float:
        return min(self.latencies) if self.latencies else 0.0

    @property
    def max_ms(self) -> float:
        return max(self.latencies) if self.latencies else 0.0

    # TODO: Add PDR stats


@dataclass
class FrameStats:
    window_seconds: int = 240  # set window duration
    sent: deque[FrameLogEntry] = field(default_factory=deque)
    received: deque[FrameLogEntry] = field(default_factory=deque)
    cumulative_sent: int = 0
    cumulative_received: int = 0
    cumulative_sent_non_test: int = 0
    cumulative_received_non_test: int = 0

    def add_sent(self, frame: Frame):
        """Adds a sent frame, prunes old entries, and updates counters."""
        self.cumulative_sent += 1
        if not frame.is_test_packet:
            self.cumulative_sent_non_test += 1  # NOTE: do we need this?

        entry = FrameLogEntry(frame=frame)
        self.sent.append(entry)

        # remove old entries
        while self.sent and (entry.ts - self.sent[0].ts).total_seconds() > self.window_seconds:
            self.sent.popleft()

    def add_received(self, frame: Frame):
        """Adds a received frame and prunes old entries."""
        self.cumulative_received += 1
        if not frame.is_test_packet:
            self.cumulative_received_non_test += 1  # NOTE: do we need this?

        entry = FrameLogEntry(frame=frame)
        self.received.append(entry)

        # remove old entries
        while (
            self.received and (entry.ts - self.received[0].ts).total_seconds() > self.window_seconds
        ):
            self.received.popleft()

    def sent_count(self, window_secs: int = 0, include_test_packets: bool = True) -> int:
        if window_secs == 0:
            return self.cumulative_sent if include_test_packets else self.cumulative_sent_non_test

        now = datetime.now()
        if include_test_packets:
            return len([e for e in self.sent if now - e.ts < timedelta(seconds=window_secs)])
        return len(
            [
                e
                for e in self.sent
                if now - e.ts < timedelta(seconds=window_secs) and not e.frame.is_test_packet
            ]
        )

    def received_count(self, window_secs: int = 0, include_test_packets: bool = True) -> int:
        if window_secs == 0:
            return (
                self.cumulative_received
                if include_test_packets
                else self.cumulative_received_non_test
            )

        now = datetime.now()
        if include_test_packets:
            entries = [e for e in self.received if now - e.ts < timedelta(seconds=window_secs)]
        else:
            entries = [
                e
                for e in self.received
                if now - e.ts < timedelta(seconds=window_secs) and not e.frame.is_test_packet
            ]
        return len(entries)

    def success_rate(self, window_secs: int = 0) -> float:
        s = self.sent_count(window_secs, include_test_packets=True)
        if s == 0:
            return 1.0
        r = self.received_count(window_secs, include_test_packets=True)
        return min(r / s, 1.0)


@dataclass
class MariNode:
    address: int
    gateway_address: int
    last_seen: datetime = field(default_factory=lambda: datetime.now())
    probe_stats: deque[MetricsProbePayload] = field(
        default_factory=lambda: deque(maxlen=MARI_PROBE_STATS_MAX_LEN)
    )  # NOTE: related to frequency of probe stats
    stats: FrameStats = field(default_factory=FrameStats)
    metrics_stats: MetricsStats = field(default_factory=MetricsStats)
    last_reported_rx_count: int = 0
    last_reported_tx_count: int = 0
    pdr_downlink: float = 0.0
    pdr_uplink: float = 0.0
    probe_tx_count: int = 0
    probe_rx_count: int = 0
    probe_tracker: ProbeTracker = field(default_factory=ProbeTracker)

    @property
    def sent_probe_packets(self):
        return self.probe_tracker.sent_probe_packets

    @property
    def effective_latency_samples(self):
        return self.probe_tracker.effective_latency_samples

    def stats_avg_effective_latency_ms(self) -> float:
        return self.probe_tracker.stats_avg_effective_latency_ms()

    def stats_pending_probe_count(self) -> int:
        return self.probe_tracker.stats_pending_probe_count()

    def has_pending_probe(self) -> bool:
        return self.probe_tracker.has_pending

    @property
    def is_alive(self) -> bool:
        return datetime.now() - self.last_seen < timedelta(seconds=MARI_TIMEOUT_NODE_IS_ALIVE)

    def save_probe_stats(self, probe_stats: MetricsProbePayload):
        # save the current probe stats
        self.probe_stats.append(probe_stats)

    @property
    def probe_stats_latest(self) -> MetricsProbePayload | None:
        if not self.probe_stats:
            return None
        return self.probe_stats[-1]

    @property
    def probe_stats_start_epoch(self) -> MetricsProbePayload | None:
        if len(self.probe_stats) < 2:
            return None
        return self.probe_stats[0]

    def probe_increment_tx_count(self) -> int:
        self.probe_tx_count += 1
        return self.probe_tx_count

    def probe_increment_rx_count(self) -> int:
        self.probe_rx_count += 1
        return self.probe_rx_count

    def stats_pdr_downlink_radio(self) -> float:
        if not self.probe_stats_latest:
            return 0
        return self.probe_stats_latest.pdr_downlink_radio(self.probe_stats_start_epoch)

    def stats_pdr_uplink_radio(self) -> float:
        if not self.probe_stats_latest:
            return 0
        return self.probe_stats_latest.pdr_uplink_radio(self.probe_stats_start_epoch)

    def stats_pdr_uplink_uart(self) -> float:
        if not self.probe_stats_latest:
            return 0
        return self.probe_stats_latest.pdr_uplink_uart(self.probe_stats_start_epoch)

    def stats_pdr_downlink_uart(self) -> float:
        if not self.probe_stats_latest:
            return 0
        return self.probe_stats_latest.pdr_downlink_uart(self.probe_stats_start_epoch)

    def stats_rssi_node_dbm(self) -> float:
        if not self.probe_stats_latest:
            return None
        return self.probe_stats_latest.rssi_at_node_dbm()

    def stats_rssi_gw_dbm(self) -> float:
        if not self.probe_stats_latest:
            return None
        return self.probe_stats_latest.rssi_at_gw_dbm()

    def stats_avg_latency_roundtrip_node_edge_ms(self) -> float:
        """Average latency between node and edge in milliseconds.

        Non-positive samples are skipped: a probe whose edge_rx_ts_us was
        never stamped reads as a large negative round trip and would swamp
        the mean."""
        if not self.probe_stats:
            return 0
        return self._avg_nonzero([p.latency_roundtrip_node_edge_ms() for p in self.probe_stats])

    def stats_avg_latency_roundtrip_node_cloud_ms(self) -> float:
        """Average latency between node and cloud in milliseconds"""
        if not self.probe_stats:
            return 0
        return self._avg_nonzero([p.latency_roundtrip_node_cloud_ms() for p in self.probe_stats])

    def stats_latest_latency_roundtrip_node_edge_ms(self) -> float:
        """Last latency between node and edge in milliseconds"""
        if not self.probe_stats:
            return 0
        return max(0.0, self.probe_stats_latest.latency_roundtrip_node_edge_ms())

    def stats_latest_latency_roundtrip_node_cloud_ms(self) -> float:
        """Last latency between node and cloud in milliseconds"""
        if not self.probe_stats:
            return 0
        return max(0.0, self.probe_stats_latest.latency_roundtrip_node_cloud_ms())

    # ASN-decomposed latency aggregates. Each averages over the
    # probe_stats deque, ignoring samples where the relevant ASN pair
    # is missing or non-monotonic (returns 0.0 from the underlying
    # MetricsProbePayload helper). See MetricsProbePayload docstrings.

    def _avg_nonzero(self, vals: list[float]) -> float:
        vals = [v for v in vals if v > 0]
        return sum(vals) / len(vals) if vals else 0.0

    def stats_avg_downlink_half_ms(self) -> float:
        return self._avg_nonzero([p.downlink_half_ms() for p in self.probe_stats])

    def stats_avg_node_processing_ms(self) -> float:
        return self._avg_nonzero([p.node_processing_ms() for p in self.probe_stats])

    def stats_avg_uplink_half_ms(self) -> float:
        return self._avg_nonzero([p.uplink_half_ms() for p in self.probe_stats])

    def stats_avg_wire_rtt_ms(self) -> float:
        return self._avg_nonzero([p.wire_rtt_ms() for p in self.probe_stats])

    def stats_avg_host_overhead_ms(self) -> float:
        """Host-side overhead = host RTT - wire RTT.

        Captures UART round-trip + small gateway-internal time +
        Python parse overhead. At 1 Mbps UART this is typically
        10-30 ms. Sustained higher values suggest UART congestion at
        the gateway, lock contention on the edge, or GIL pressure.

        Returns 0 if either host RTT or wire RTT are unavailable, or
        if their difference is negative (which shouldn't happen with
        the monotonic-clock fix in MetricsTester.timestamp_us; clamp
        defensively)."""
        host = self.stats_avg_latency_roundtrip_node_edge_ms()
        wire = self.stats_avg_wire_rtt_ms()
        if host <= 0 or wire <= 0:
            return 0.0
        return max(0.0, host - wire)

    def stats_queue_depth_slotframes(self, sf_duration_ms: float) -> float:
        """Estimated TX-queue depth at the node, in slotframe units.

        Each joined node has exactly one U slot per slotframe (Mari paper,
        §3.5 / Table 1), so the uplink half of the probe RTT divided by
        the slotframe duration is the number of packets queued ahead of
        the probe response when it was enqueued. Sustained values > 1
        indicate contention, > 3 is real saturation. Returns 0 when
        slotframe duration is unknown.
        """
        if sf_duration_ms <= 0:
            return 0.0
        return self.stats_avg_uplink_half_ms() / sf_duration_ms

    def register_received_frame(self, frame: Frame):
        self.stats.add_received(frame)

    def register_sent_frame(self, frame: Frame):
        self.stats.add_sent(frame)

    def as_node_info_cloud(self) -> NodeInfoCloud:
        return NodeInfoCloud(address=self.address, gateway_address=self.gateway_address)


@dataclass
class GatewayInfo(Packet):
    """Mirror of mr_uart_packet_gateway_info_t in firmware/mari/models.h.

    The gateway emits one of these per slotframe. Every field after `timer` is
    a cumulative counter that only resets when the gateway reboots, so a reader
    takes differences between consecutive packets.
    """

    metadata: list[PacketFieldMetadata] = field(
        default_factory=lambda: [
            PacketFieldMetadata(name="version", length=1),
            PacketFieldMetadata(name="address", length=8),
            PacketFieldMetadata(name="network_id", length=2),
            PacketFieldMetadata(name="schedule_id", length=2),
            PacketFieldMetadata(name="schedule_stats", length=4 * 8),  # 4 uint64_t values
            PacketFieldMetadata(name="asn", length=8),
            PacketFieldMetadata(name="timer", length=4),
            PacketFieldMetadata(name="uart_rx_bytes", length=4),
            PacketFieldMetadata(name="uart_rx_frames_ok", length=4),
            PacketFieldMetadata(name="uart_rx_hdlc_err", length=4),
            PacketFieldMetadata(name="uart_rx_hw_overrun", length=4),
            PacketFieldMetadata(name="uart_rx_hw_framing", length=4),
            PacketFieldMetadata(name="uart_rx_hw_break", length=4),
            PacketFieldMetadata(name="uart_rx_slot_full", length=4),
            PacketFieldMetadata(name="uart_tx_queue_drop", length=4),
            PacketFieldMetadata(name="ipc_u2r_lost", length=4),
            PacketFieldMetadata(name="ipc_r2u_lost", length=4),
        ]
    )
    version: int = MARI_GATEWAY_INFO_VERSION
    address: int = 0
    network_id: int = 0
    schedule_id: int = 0
    schedule_stats: bytes = b""
    asn: int = 0
    timer: int = 0
    uart_rx_bytes: int = 0
    uart_rx_frames_ok: int = 0
    uart_rx_hdlc_err: int = 0
    uart_rx_hw_overrun: int = 0
    uart_rx_hw_framing: int = 0
    uart_rx_hw_break: int = 0
    uart_rx_slot_full: int = 0
    uart_tx_queue_drop: int = 0
    ipc_u2r_lost: int = 0
    ipc_r2u_lost: int = 0

    def from_bytes(self, bytes_):
        """Parse a gateway_info payload, rejecting anything that is not one.

        The base parser stops as soon as every field is filled, so a payload
        built by a firmware with a different layout would be accepted with its
        trailing bytes ignored and its fields quietly misaligned. gateway_info
        is a fixed-size struct, so require the exact size.
        """
        if len(bytes_) != self.size:
            raise ValueError(
                f"gateway_info is {len(bytes_)} bytes, expected {self.size}: "
                "the gateway firmware and marilib disagree on the layout"
            )
        return super().from_bytes(bytes_)

    # NOTE: maybe move to a separate class, dedicated to schedule stuff
    def repr_schedule_stats(self):
        if not self.schedule_stats:
            return ""
        schedule_data = SCHEDULES.get(self.schedule_id)
        if not schedule_data:
            return ""
        all_bits = format(self.schedule_stats, f"0{4 * 8 * 8}b")
        all_bits = [all_bits[i : i + 8] for i in range(0, len(all_bits), 8)]
        all_bits.reverse()
        # print(">>>", reversed(all_bits[0].split("")))
        all_bits = [list(reversed(bits)) for bits in all_bits]
        # now just flatten the list; cell n is bit n of the little-endian
        # sched_usage bitmap
        all_bits = [item for sublist in all_bits for item in sublist]
        # cut it down to the number of slots
        all_bits = all_bits[: len(schedule_data["slots"])]
        return "".join(all_bits)

    def repr_cell_nice(self, cell: str, is_used: int):
        is_used = bool(int(is_used))
        if cell == "B":
            return rich.text.Text("B", style=f"bold white on {'red' if is_used else 'indian_red'}")
        if cell == "S":
            return rich.text.Text(
                "S", style=f"bold white on {'purple' if is_used else 'medium_purple2'}"
            )
        if cell == "D":
            return rich.text.Text(
                "D", style=f"bold white on {'green' if is_used else 'sea_green3'}"
            )
        if cell == "U":
            return rich.text.Text(
                "U", style=f"bold white on {'yellow' if is_used else 'light_yellow3'}"
            )

    def repr_schedule_cells_with_colors(self):
        schedule_data = SCHEDULES.get(self.schedule_id)
        if not schedule_data:
            return ""
        sched_stats = [
            self.repr_cell_nice(cell, is_used)
            for cell, is_used in zip(schedule_data["slots"], self.repr_schedule_stats())
        ]
        return rich.text.Text.assemble(*sched_stats)

    @property
    def schedule_name(self) -> str:
        schedule_data = SCHEDULES.get(self.schedule_id)
        return schedule_data["name"] if schedule_data else "unknown"

    @property
    def max_nodes(self) -> int:
        return SCHEDULES.get(self.schedule_id, EMPTY_SCHEDULE_DATA)["max_nodes"]

    @property
    def network_id_str(self) -> str:
        return f"{self.network_id:04X}"

    @property
    def schedule_uplink_cells(self) -> int:
        return SCHEDULES.get(self.schedule_id, EMPTY_SCHEDULE_DATA)["max_nodes"]

    @property
    def schedule_downlink_cells(self) -> int:
        return SCHEDULES.get(self.schedule_id, EMPTY_SCHEDULE_DATA)["slots"].count("D")


@dataclass
class MariGateway:
    info: GatewayInfo = field(default_factory=GatewayInfo)
    node_registry: dict[int, MariNode] = field(default_factory=dict)
    stats: FrameStats = field(default_factory=FrameStats)
    metrics_stats: MetricsStats = field(default_factory=MetricsStats)
    last_seen: datetime = field(default_factory=lambda: datetime.now())

    def __post_init__(self):
        self.last_seen = datetime.now()

    @property
    def nodes(self) -> list[MariNode]:
        return list(self.node_registry.values())

    @property
    def nodes_addresses(self) -> list[int]:
        return list(self.node_registry.keys())

    @property
    def is_alive(self) -> bool:
        return datetime.now() - self.last_seen < timedelta(seconds=MARI_TIMEOUT_GATEWAY_IS_ALIVE)

    def stats_avg_pdr_downlink_radio(self) -> float:
        if not self.nodes:
            return 0.0
        res = sum(n.stats_pdr_downlink_radio() for n in self.nodes) / len(self.nodes)
        return res if res >= 0 and res <= 1.0 else 0.0

    def stats_avg_pdr_uplink_radio(self) -> float:
        if not self.nodes:
            return 0.0
        res = sum(n.stats_pdr_uplink_radio() for n in self.nodes) / len(self.nodes)
        return res if res >= 0 and res <= 1.0 else 0.0

    def stats_avg_pdr_downlink_uart(self) -> float:
        if not self.nodes:
            return 0.0
        res = sum(n.stats_pdr_downlink_uart() for n in self.nodes) / len(self.nodes)
        return res if res >= 0 and res <= 1.0 else 0.0

    def stats_avg_pdr_uplink_uart(self) -> float:
        if not self.nodes:
            return 0.0
        res = sum(n.stats_pdr_uplink_uart() for n in self.nodes) / len(self.nodes)
        return res if res >= 0 and res <= 1.0 else 0.0

    def stats_avg_latency_roundtrip_node_edge_ms(self) -> float:
        if not self.nodes:
            return 0.0
        res = sum(n.stats_avg_latency_roundtrip_node_edge_ms() for n in self.nodes) / len(
            self.nodes
        )
        return res if res >= 0 else 0.0

    def stats_avg_effective_latency_ms(self) -> float:
        if not self.nodes:
            return 0.0
        res = sum(n.stats_avg_effective_latency_ms() for n in self.nodes) / len(self.nodes)
        return res if res >= 0 else 0.0

    def stats_pending_probe_count(self) -> int:
        return sum(n.stats_pending_probe_count() for n in self.nodes)

    # Network-level averages of the ASN-decomposed latency, skipping
    # nodes that haven't produced a usable sample yet.
    def _avg_node_metric_nonzero(self, fn) -> float:
        vals = [fn(n) for n in self.nodes]
        vals = [v for v in vals if v > 0]
        return sum(vals) / len(vals) if vals else 0.0

    def stats_avg_downlink_half_ms(self) -> float:
        return self._avg_node_metric_nonzero(MariNode.stats_avg_downlink_half_ms)

    def stats_avg_node_processing_ms(self) -> float:
        return self._avg_node_metric_nonzero(MariNode.stats_avg_node_processing_ms)

    def stats_avg_uplink_half_ms(self) -> float:
        return self._avg_node_metric_nonzero(MariNode.stats_avg_uplink_half_ms)

    def stats_avg_host_overhead_ms(self) -> float:
        return self._avg_node_metric_nonzero(MariNode.stats_avg_host_overhead_ms)

    def stats_avg_latency_roundtrip_node_cloud_ms(self) -> float:
        if not self.nodes:
            return 0.0
        res = sum(n.stats_avg_latency_roundtrip_node_cloud_ms() for n in self.nodes) / len(
            self.nodes
        )
        return res if res >= 0 else 0.0

    def stats_latest_node_tx_count(self) -> int:
        """Returns sum of tx counts for all nodes"""
        if not self.nodes:
            return 0
        return sum(n.probe_stats_latest.node_tx_count for n in self.nodes if n.probe_stats_latest)

    def stats_latest_node_rx_count(self) -> int:
        """Returns sum of rx counts for all nodes"""
        if not self.nodes:
            return 0
        return sum(n.probe_stats_latest.node_rx_count for n in self.nodes if n.probe_stats_latest)

    def stats_latest_gw_tx_count(self) -> int:
        """Returns sum of tx counts for all nodes"""
        if not self.nodes:
            return 0
        return sum(n.probe_stats_latest.gw_tx_count for n in self.nodes if n.probe_stats_latest)

    def stats_latest_gw_rx_count(self) -> int:
        """Returns sum of rx counts for all nodes"""
        if not self.nodes:
            return 0
        return sum(n.probe_stats_latest.gw_rx_count for n in self.nodes if n.probe_stats_latest)

    def update(self):
        """Recurrent bookkeeping. Don't forget to call this periodically on your main loop."""
        self.node_registry = {
            addr: node for addr, node in self.node_registry.items() if node.is_alive
        }

    def set_info(self, info: GatewayInfo):
        self.info = info
        self.last_seen = datetime.now()

    def get_node(self, addr: int) -> MariNode | None:
        return self.node_registry.get(addr)

    def add_node(self, addr: int) -> MariNode:
        if node := self.get_node(addr):
            node.last_seen = datetime.now()
            return node
        node = MariNode(addr, self.info.address)
        self.node_registry[addr] = node
        return node

    def remove_node(self, addr: int) -> MariNode | None:
        return self.node_registry.pop(addr, None)

    def update_node_liveness(self, addr: int) -> MariNode:
        node = self.get_node(addr)
        if node:
            node.last_seen = datetime.now()
        else:
            node = self.add_node(addr)
        return node

    def register_received_frame(self, frame: Frame):
        if n := self.get_node(frame.header.source):
            n.register_received_frame(frame)
        self.stats.add_received(frame)

    def register_sent_frame(self, frame: Frame):
        self.stats.add_sent(frame)
