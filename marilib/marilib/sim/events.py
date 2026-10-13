"""Scripted changes to a running network, keyed by simulated time.

[[event]]
at = 10.0          # seconds since the run started
action = "remove"  # "add", "remove" or "move"
node = 3           # node 3 is address 0x1003, the third node Network.for_nodes made

[[event]]
at = 20.0
action = "move"
node = 5
distance = 12.0    # meters from the gateway, or position = [x, y]

[[event]]
at = 30.0
action = "halt"    # the gateway: "halt" stops it, "reset" reboots it with empty tables
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from marilib.sim.network import DEFAULT_NODE_ADDRESS_BASE

NODE_ACTIONS = ("add", "remove", "move")
GATEWAY_ACTIONS = ("halt", "reset")
ACTIONS = NODE_ACTIONS + GATEWAY_ACTIONS


@dataclass(frozen=True)
class Event:
    at_s: float
    action: str
    node: int | None = None  # None for a gateway action
    position: tuple[float, float] | None = None

    @property
    def address(self) -> int:
        return DEFAULT_NODE_ADDRESS_BASE + self.node

    def describe(self) -> str:
        if self.node is None:
            return f"{self.action} gateway"
        text = f"{self.action} node {self.node}"
        if self.position is not None:
            text += f" to {math.hypot(*self.position):.1f} m"
        return text


def parse_events(data: dict) -> list[Event]:
    """Events from a parsed TOML document, sorted by time. Raises ValueError on a bad entry."""
    events = []
    for i, entry in enumerate(data.get("event", [])):
        where = f"event {i + 1}"
        unknown = set(entry) - {"at", "action", "node", "distance", "position"}
        if unknown:
            raise ValueError(f"{where}: unknown key(s) {', '.join(sorted(unknown))}")
        try:
            at_s, action = float(entry["at"]), entry["action"]
        except KeyError as missing:
            raise ValueError(f"{where}: missing {missing}") from None
        if action not in ACTIONS:
            raise ValueError(f"{where}: action must be one of {', '.join(ACTIONS)}, got {action!r}")
        if at_s < 0:
            raise ValueError(f"{where}: 'at' must be >= 0")
        if action in GATEWAY_ACTIONS:
            if set(entry) - {"at", "action"}:
                raise ValueError(f"{where}: '{action}' acts on the gateway and takes only 'at'")
            events.append(Event(at_s, action))
            continue
        if "node" not in entry:
            raise ValueError(f"{where}: missing 'node'")
        node = int(entry["node"])
        if node < 1:
            raise ValueError(f"{where}: 'node' must be >= 1")
        if "distance" in entry and "position" in entry:
            raise ValueError(f"{where}: give 'distance' or 'position', not both")
        position = None
        if "distance" in entry:
            position = (float(entry["distance"]), 0.0)
        elif "position" in entry:
            x, y = entry["position"]
            position = (float(x), float(y))
        if action == "move" and position is None:
            raise ValueError(f"{where}: 'move' needs 'distance' or 'position'")
        if action == "remove" and position is not None:
            raise ValueError(f"{where}: 'remove' takes no position")
        events.append(Event(at_s, action, node, position))
    return sorted(events, key=lambda e: e.at_s)


def load_events(path: str | Path) -> list[Event]:
    import tomllib

    with open(path, "rb") as f:
        return parse_events(tomllib.load(f))


def check_events(events: list[Event], n_nodes: int) -> None:
    """Raise ValueError if an event acts on a node that is not powered, or halts a halted gateway."""
    present = set(range(1, n_nodes + 1))
    halted = False
    for event in events:
        if event.action == "halt":
            if halted:
                raise ValueError(f"at {event.at_s} s: the gateway is already halted")
            halted = True
        elif event.action == "reset":
            halted = False
        elif event.action == "add":
            if event.node in present:
                raise ValueError(f"at {event.at_s} s: node {event.node} is already on")
            present.add(event.node)
        elif event.node not in present:
            raise ValueError(f"at {event.at_s} s: node {event.node} is not on")
        elif event.action == "remove":
            present.discard(event.node)
