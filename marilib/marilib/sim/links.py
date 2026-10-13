"""Link models: the probability that one frame between a node and its gateway arrives.

The engine asks `pdr(link)` once per frame on the air - beacons, join requests
and responses, data and keep-alives - and draws against it. A model may use any
field of `Link`; a model that lets the node count leak in would reproduce the
selection-bias artifact the reference campaign had to correct for.

A link file is the PyDotBot simulator's world file, PDRs in percent:

    [network]
    uplink_pdr = 98        # every robot, unless it overrides
    downlink_pdr = 98

    [[dotbots]]
    address = "BADCAFE111111111"
    uplink_pdr = 91
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

UP = "up"
DOWN = "down"

# Reported for every received frame by models that do not compute one; it only
# fills the probe's rssi fields.
DEFAULT_RSSI_DBM = -50


@dataclass(frozen=True)
class Link:
    """One frame's link, as the engine sees it when the frame is on the air."""

    node: int
    gateway: int
    direction: str  # UP: node to gateway, DOWN: gateway to node
    asn: int
    channel: int  # BLE channel index the frame is sent on
    node_pos: tuple[float, float, float]  # metres
    gateway_pos: tuple[float, float, float]


class LinkModel(Protocol):
    def pdr(self, link: Link) -> float:
        """Probability in [0, 1] that this frame is received."""

    def rssi_dbm(self, link: Link) -> int:
        """RSSI reported for a received frame."""


class PerfectLinks:
    """Every frame arrives. For tests that need the MAC alone."""

    def pdr(self, link: Link) -> float:
        return 1.0

    def rssi_dbm(self, link: Link) -> int:
        return DEFAULT_RSSI_DBM


@dataclass
class StaticLinks:
    """A fixed PDR per node and direction: per-node overrides, else the defaults."""

    uplink: float = 1.0
    downlink: float = 1.0
    nodes: dict[int, tuple[float | None, float | None]] = field(default_factory=dict)

    def __post_init__(self):
        for name, value in [("uplink", self.uplink), ("downlink", self.downlink)] + [
            (f"node 0x{a:X}", v) for a, pair in self.nodes.items() for v in pair if v is not None
        ]:
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name}: PDR {value} is outside [0, 1]")

    def pdr(self, link: Link) -> float:
        up, down = self.nodes.get(link.node, (None, None))
        if link.direction == UP:
            return self.uplink if up is None else up
        return self.downlink if down is None else down

    def rssi_dbm(self, link: Link) -> int:
        return DEFAULT_RSSI_DBM

    @classmethod
    def from_file(cls, path: str | Path) -> "StaticLinks":
        """Read a link file; its robots, in file order, are `addresses`."""
        with open(path, "rb") as f:
            data = tomllib.load(f)
        network = data.get("network", {})
        default = network.get("pdr", 100)
        links = cls(
            uplink=_fraction(network.get("uplink_pdr", default), "network.uplink_pdr"),
            downlink=_fraction(network.get("downlink_pdr", default), "network.downlink_pdr"),
        )
        for i, robot in enumerate(data.get("dotbots", [])):
            where = f"dotbots[{i}]"
            if "address" not in robot:
                raise ValueError(f"{where}: no address")
            address = int(str(robot["address"]), 16)
            if address in links.nodes:
                raise ValueError(f"{where}: address {robot['address']} listed twice")
            links.nodes[address] = tuple(
                _fraction(robot[key], f"{where}.{key}") if key in robot else None
                for key in ("uplink_pdr", "downlink_pdr")
            )
        return links

    @property
    def addresses(self) -> list[int]:
        return list(self.nodes)


def _fraction(percent, where: str) -> float:
    if not isinstance(percent, (int, float)) or not 0 <= percent <= 100:
        raise ValueError(f"{where}: {percent!r} is not a PDR in percent (0-100)")
    return percent / 100
