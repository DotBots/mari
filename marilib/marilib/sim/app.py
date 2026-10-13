"""Seam B: what runs on top of mari on each simulated node.

The node model polls `next_uplink` right before each of its own uplink cells,
draining it into the mari queue, and hands every downlink addressed to it to
`on_downlink`. Uplinks produced while the node is not joined are discarded at
the next join, as the firmware's queue reset does.
"""

from __future__ import annotations

import random
from collections import deque
from dataclasses import dataclass, field
from typing import Protocol

from marilib.mari_protocol import DefaultPayloadType, Frame, MetricsProbePayload, NextProto

PROBE_SIZE = MetricsProbePayload().size

# 03app_node/main.c: status_packet_mock, sent every 500 ms
STATUS_PAYLOAD = bytes([0x80, 0x01, 0x01, 0x00])
STATUS_PERIOD_S = 0.5


@dataclass(frozen=True)
class HandoverInfo:
    duration_ms: int = 0
    seq: int = 0
    reason: int = 0


@dataclass(frozen=True)
class RxInfo:
    asn: int
    rssi_dbm: int
    handover: HandoverInfo
    gateway_asn: int  # the ASN as the node's MAC counts it: synced to the gateway, which restarts at 0 on reset


class NodeApp(Protocol):
    next_proto: int

    def position_m(self) -> tuple[float, float]:
        """Where the node is, in meters, in the gateway's frame (gateway at the origin)."""

    def on_downlink(self, frame: Frame, rx: RxInfo) -> None:
        """A data frame from the gateway, addressed to this node or broadcast."""

    def next_uplink(self, asn: int) -> bytes | tuple[bytes, int] | None:
        """The next payload to enqueue, or None when there is nothing more for now.

        A (payload, next_proto) pair sends that payload under its own next_proto
        rather than the app's.
        """


def is_probe(payload: bytes) -> bool:
    return len(payload) == PROBE_SIZE and payload[0] == DefaultPayloadType.METRICS_PROBE


@dataclass
class ProbeResponder:
    """The node half of a metrics probe, as 03app_node handles it: stamp and echo."""

    rx_count: int = 0
    tx_count: int = 0

    def reply(self, payload: bytes, rx: RxInfo) -> bytes:
        probe = MetricsProbePayload().from_bytes(payload)
        self.rx_count += 1
        self.tx_count += 1
        probe.node_rx_count = self.rx_count
        probe.node_rx_asn = rx.gateway_asn
        probe.node_tx_count = self.tx_count
        probe.node_tx_enqueued_asn = rx.gateway_asn
        probe.rssi_at_node = rx.rssi_dbm & 0xFF
        probe.node_handover_duration_ms = rx.handover.duration_ms
        probe.node_handover_seq = rx.handover.seq
        probe.node_handover_reason = rx.handover.reason
        return bytes(probe.to_bytes())


@dataclass
class SyntheticApp:
    """The stock mari node app: a periodic status uplink and probe echo, at a fixed position.

    `period_slots` of 0 disables the periodic uplink. The first status is due at
    a random phase within one period, drawn from `rng`. Uplinks come out in the
    order the firmware app would have queued them: a status at its due slot, a
    probe reply at the slot the probe arrived.
    """

    position: tuple[float, float] = (2.0, 0.0)
    period_slots: int = 0
    payload: bytes = STATUS_PAYLOAD
    rng: random.Random | None = None
    next_proto: int = NextProto.MARI_INTERNAL
    received: int = 0
    probes: ProbeResponder = field(default_factory=ProbeResponder)
    _pending: deque[tuple[int, bytes]] = field(default_factory=deque, repr=False)
    _next_due: int = field(default=0, repr=False)

    def __post_init__(self):
        if self.period_slots:
            self._next_due = (self.rng or random.Random(0)).randrange(self.period_slots)

    def position_m(self) -> tuple[float, float]:
        return self.position

    def on_downlink(self, frame: Frame, rx: RxInfo) -> None:
        self.received += 1
        if is_probe(frame.payload):
            self._pending.append((rx.asn, self.probes.reply(frame.payload, rx)))

    def next_uplink(self, asn: int) -> bytes | None:
        status_due = self.period_slots and self._next_due <= asn
        if self._pending and not (status_due and self._next_due < self._pending[0][0]):
            return self._pending.popleft()[1]
        if status_due:
            self._next_due += self.period_slots
            return self.payload
        return None
