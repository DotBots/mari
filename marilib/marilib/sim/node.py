"""State of one simulated mari node, mirroring firmware/mari/association.c and queue.c."""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from enum import IntEnum

from marilib.sim.app import HandoverInfo, NodeApp


class NodeState(IntEnum):
    """mr_assoc_state_t, plus OFF for a node that is not powered."""

    OFF = 0
    IDLE = 1
    SCANNING = 2
    SYNCED = 4
    JOINING = 8
    JOINED = 16


class DisconnectReason(IntEnum):
    """mr_event_tag_t values a node disconnects with."""

    PEER_LOST_TIMEOUT = 5
    PEER_LOST_BLOOM = 6


class MariQueue:
    """The firmware's packet ring, including its empty/full ambiguity.

    `add` never refuses: after `size` adds without a pop, `last` catches up with
    `current` and the ring reads as empty, losing everything in it. `add`
    returns False when that happens so the engine can trace it.
    """

    def __init__(self, size: int):
        self.size = size
        self._packets: list = [None] * size
        self.current = 0
        self.last = 0

    def __len__(self) -> int:
        return (self.last - self.current) % self.size

    def add(self, packet) -> bool:
        self._packets[self.last] = packet
        self.last = (self.last + 1) % self.size
        return self.last != self.current

    def pop(self):
        if self.current == self.last:
            return None
        packet = self._packets[self.current]
        self.current = (self.current + 1) % self.size
        return packet

    def reset(self) -> None:
        self.current = self.last = 0


@dataclass
class LinkStats:
    sent: int = 0
    received: int = 0


@dataclass
class SimNode:
    address: int
    app: NodeApp
    rng: random.Random
    queue: MariQueue
    state: NodeState = NodeState.OFF

    # Bumped on every state change; a timer armed in an older epoch is stale.
    epoch: int = 0
    # First slot the node takes part in, the one after it synced.
    active_from: int = 0

    scan_round: int = 0
    scan_channel: int = 0
    scan_end_asn: int = 0
    scan_beacon_capacity: int | None = None

    sync_asn: int = 0
    sync_epoch: int = 0  # bumped on every sync; the synced timeout spans join retries
    remaining_capacity: int = 0
    backoff_n: int = -1
    backoff_time: int = 0
    consecutive_max_backoff: int = 0
    joining_asn: int = 0

    cell: int | None = None
    last_rx_asn: int = 0
    pending_disconnect: int = 0

    handover_pending: bool = False
    handover_start_asn: int = 0
    handover_reason: int = 0
    handover: HandoverInfo = field(default_factory=HandoverInfo)

    joins: int = 0
    first_joined_asn: int | None = None
    uplink: LinkStats = field(default_factory=LinkStats)
    downlink: LinkStats = field(default_factory=LinkStats)

    @property
    def is_listening(self) -> bool:
        """Synced to the gateway's slot clock, so it takes part in B, S and D cells."""
        return self.state >= NodeState.SYNCED

    def distance_m(self) -> float:
        x, y = self.app.position_m()
        return (x * x + y * y) ** 0.5

    def draw_backoff(self) -> None:
        # association.c: an RNG byte modulo the window, bias and all
        self.backoff_time = self.rng.randrange(256) % (1 << self.backoff_n)

    def init_backoff(self, n_min: int) -> None:
        self.backoff_n = n_min
        self.consecutive_max_backoff = 0
        self.draw_backoff()

    def reset_backoff(self) -> None:
        self.backoff_n = -1
        self.consecutive_max_backoff = 0
        self.backoff_time = 0

    def register_collision_backoff(self, n_min: int, n_max: int) -> None:
        self.backoff_n = n_min if self.backoff_n == -1 else min(self.backoff_n + 1, n_max)
        if self.backoff_n >= n_max:
            self.consecutive_max_backoff += 1
        else:
            self.consecutive_max_backoff = 0
        self.draw_backoff()
