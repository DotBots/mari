"""The event trace: one JSON object per line, keyed by ASN.

Same seed, same trace, byte for byte. Nothing wall-clock goes in it.
"""

from __future__ import annotations

import json
from typing import IO


class Trace:
    """Collects events into a list, a text stream, or both."""

    def __init__(self, stream: IO[str] | None = None, keep: bool = False):
        self.stream = stream
        self.events: list[dict] | None = [] if keep else None

    def emit(self, asn: int, ev: str, **fields) -> None:
        event = {"asn": asn, "ev": ev, **fields}
        if self.events is not None:
            self.events.append(event)
        if self.stream is not None:
            self.stream.write(json.dumps(event, separators=(", ", ": ")) + "\n")

    def close(self) -> None:
        if self.stream is not None:
            self.stream.flush()


def node_id(address: int) -> str:
    return f"0x{address:016x}"
