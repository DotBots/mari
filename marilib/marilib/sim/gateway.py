"""State of the simulated gateway: cell table, join slot, downlink queue and UART output.

Mirrors firmware/mari/scheduler.c (cell assignment), association.c (node
timeouts), queue.c (one join-response slot ahead of the data queue) and the
gateway apps in firmware/app/03app_gateway_*, which put each event on the
UART as one EdgeEvent byte followed by its payload.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from marilib.mari_protocol import Frame, MetricsProbePayload
from marilib.model import EdgeEvent, GatewayInfo, NodeInfoEdge
from marilib.sim.app import is_probe
from marilib.sim.firmware import UPLINK, Schedule
from marilib.sim.node import MariQueue


@dataclass
class ProbeCounters:
    """firmware/app/03app_gateway_net/metrics.c: per-node probe counters, kept while joined."""

    tx: dict[int, int] = field(default_factory=dict)
    rx: dict[int, int] = field(default_factory=dict)

    def add(self, address: int) -> None:
        self.tx.setdefault(address, 0)
        self.rx.setdefault(address, 0)

    def clear(self, address: int) -> None:
        self.tx.pop(address, None)
        self.rx.pop(address, None)


@dataclass
class SimGateway:
    address: int
    network_id: int
    schedule: Schedule
    queue: MariQueue
    gateway_info_version: int

    # cell index -> [assigned node, last ASN heard from it]
    cells: dict[int, list] = field(default_factory=dict)
    join_response: tuple[int, int] | None = None  # (node, cell)
    sched_usage: int = 0
    probes: ProbeCounters = field(default_factory=ProbeCounters)
    uart_rx_frames: int = 0
    uart_rx_bytes: int = 0
    halted: bool = False
    asn_base: int = 0  # network ASN at the last reset, where the gateway's own ASN restarts from 0

    def __post_init__(self):
        self.cells = {i: [0, 0] for i in self.schedule.indices(UPLINK)}

    def reboot(self, asn: int) -> None:
        """A reset: every table, counter and queue starts empty, and the gateway's ASN starts at 0."""
        self.__post_init__()
        self.join_response = None
        self.sched_usage = 0
        self.probes = ProbeCounters()
        self.queue.reset()
        self.uart_rx_frames = self.uart_rx_bytes = 0
        self.halted = False
        self.asn_base = asn

    @property
    def joined(self) -> list[int]:
        return [node for node, _ in self.cells.values() if node]

    @property
    def remaining_capacity(self) -> int:
        return self.schedule.max_nodes - len(self.joined)

    def cell_of(self, node: int) -> int | None:
        for index, (assigned, _) in self.cells.items():
            if assigned == node:
                return index
        return None

    def assign_cell(self, node: int, asn: int) -> tuple[int | None, bool]:
        """mr_scheduler_gateway_assign_next_available_uplink_cell: (cell, newly assigned).

        Cells are scanned in schedule order, so a node that is still assigned
        keeps its cell only when no free cell comes before it.
        """
        for index, entry in self.cells.items():
            if entry[0] == 0:
                entry[0], entry[1] = node, asn
                return index, True
            if entry[0] == node:
                entry[1] = asn
                return index, False
        return None, False

    def keep_alive(self, node: int, asn: int) -> None:
        for entry in self.cells.values():
            if entry[0] == node:
                entry[1] = asn

    def set_usage(self, cell_index: int, used: bool) -> None:
        bit = 1 << cell_index
        self.sched_usage = (self.sched_usage | bit) if used else (self.sched_usage & ~bit)

    # ---------------------------------------------------------------- UART --

    def uart_node_event(self, event: EdgeEvent, node: int) -> bytes:
        return EdgeEvent.to_bytes(event) + NodeInfoEdge(address=node).to_bytes()

    def uart_data(self, frame: Frame) -> bytes:
        return EdgeEvent.to_bytes(EdgeEvent.NODE_DATA) + frame.to_bytes()

    def uart_gateway_info(self, asn: int, slot_us: int) -> bytes:
        info = GatewayInfo(
            version=self.gateway_info_version,
            address=self.address,
            network_id=self.network_id,
            schedule_id=self.schedule.id,
            schedule_stats=self.sched_usage,
            asn=asn,
            timer=(asn * slot_us) & 0xFFFFFFFF,
            uart_rx_bytes=self.uart_rx_bytes & 0xFFFFFFFF,
            uart_rx_frames_ok=self.uart_rx_frames & 0xFFFFFFFF,
        )
        return EdgeEvent.to_bytes(EdgeEvent.GATEWAY_INFO) + bytes(info.to_bytes())

    def stamp_rx_probe(self, frame: Frame, asn: int, rssi_dbm: int) -> None:
        """metrics_handle_rx_probe, applied to an uplink before it goes on the UART."""
        if not is_probe(frame.payload):
            return
        probe = MetricsProbePayload().from_bytes(frame.payload)
        probe.gw_rx_asn = asn
        probe.rssi_at_gw = rssi_dbm & 0xFF
        if frame.header.source in self.probes.rx:
            self.probes.rx[frame.header.source] += 1
            probe.gw_rx_count = self.probes.rx[frame.header.source]
        frame.payload = bytes(probe.to_bytes())

    def stamp_tx_probe(self, frame: Frame, asn: int) -> None:
        """metrics_handle_tx_probe, applied to a downlink as it is queued."""
        if not is_probe(frame.payload):
            return
        probe = MetricsProbePayload().from_bytes(frame.payload)
        probe.gw_tx_enqueued_asn = asn
        if frame.header.destination in self.probes.tx:
            self.probes.tx[frame.header.destination] += 1
            probe.gw_tx_count = self.probes.tx[frame.header.destination]
        frame.payload = bytes(probe.to_bytes())
