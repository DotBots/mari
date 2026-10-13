"""Print the simulator's formation time, RTT and PDR beside the hardware reference.

Nothing here asserts: hardware is one campaign and the comparison is for a
human to read. The metric definitions follow the campaign's analysis scripts
so the columns are comparable: formation is the nearest-rank percentile of
each node's first NODE_JOINED, offset from the first join of the run, averaged
over runs; PDR is single-attempt uplink delivery, which every link is set to, so its column
only checks that the engine delivers what the link model asks for.
"""

from __future__ import annotations

import math
import statistics
import time
from dataclasses import dataclass, field

import click

from marilib.mari_protocol import Frame, Header, MetricsProbePayload
from marilib.model import EdgeEvent, NodeInfoEdge
from marilib.sim.app import is_probe
from marilib.sim.links import StaticLinks
from marilib.sim.network import Network

# The mari single-gateway dongle campaign (mari-evaluation, 2026), at 2 m.
REFERENCE_FORMATION_P95_S = {10: 0.50, 40: 1.24, 60: 1.68, 100: 3.14}
REFERENCE_RTT_P50_MS = {"tiny": 40, "medium": 96, "big": 146, "huge": 233}
REFERENCE_PDR = 0.986

# The campaign held the probe stream at 15% of downlink capacity, so RTT is
# measured on a lightly loaded downlink at every schedule.
PROBE_SHARE = 0.15
FORMATION_CAP_S = 30.0


def nearest_rank(sorted_values: list[float], pct: float) -> float:
    return sorted_values[max(1, math.ceil(pct / 100 * len(sorted_values))) - 1]


@dataclass
class EdgeProbe:
    """A minimal edge on the network's UART: records joins and round-trips probes in slot time."""

    net: Network
    first_join: dict[int, int] = field(default_factory=dict)
    pending: dict[int, int] = field(default_factory=dict)
    rtt_slots: list[int] = field(default_factory=list)

    def __post_init__(self):
        self.net.on_uart = self.on_uart

    def on_uart(self, data: bytes) -> None:
        event = data[0]
        if event == EdgeEvent.NODE_JOINED:
            self.first_join.setdefault(NodeInfoEdge().from_bytes(data[1:]).address, self.net.asn)
        elif event == EdgeEvent.NODE_DATA:
            frame = Frame().from_bytes(data[1:])
            if is_probe(frame.payload):
                probe = MetricsProbePayload().from_bytes(frame.payload)
                if self.pending.get(frame.header.source) == probe.edge_tx_ts_us:
                    del self.pending[frame.header.source]
                    self.rtt_slots.append(self.net.asn - probe.edge_tx_ts_us)

    def send_probe(self, address: int) -> None:
        payload = MetricsProbePayload(edge_tx_ts_us=self.net.asn).to_bytes()
        frame = Frame(Header(destination=address), bytes(payload))
        self.net.edge_tx(EdgeEvent.to_bytes(EdgeEvent.NODE_DATA) + frame.to_bytes())
        self.pending[address] = self.net.asn

    def measure(self, seconds: float) -> None:
        """Probe the nodes round-robin, at PROBE_SHARE of the schedule's downlink cells."""
        net = self.net
        nodes = sorted(net.nodes)
        if not nodes:
            return
        sf = net.schedule.n_cells
        probes_per_sf = PROBE_SHARE * len(net.schedule.indices("D"))
        gap = max(1, round(sf / probes_per_sf))
        timeout = 2 * sf  # MetricsTester's timeout
        turn = 0
        for _ in range(net.slots_for(seconds)):
            if net.asn % gap == 0:
                address = nodes[turn % len(nodes)]
                turn += 1
                sent = self.pending.get(address)
                if sent is None or net.asn - sent > timeout:
                    self.send_probe(address)
            net.step()


@dataclass
class Result:
    formation_p95_s: list[float] = field(default_factory=list)
    formation_p100_s: list[float] = field(default_factory=list)
    rtt_ms: list[float] = field(default_factory=list)
    pdr: list[float] = field(default_factory=list)
    incomplete: int = 0


def run_one(n: int, seed: int, measure_s: float, result: Result) -> None:
    net = Network.for_nodes(n, seed=seed, links=StaticLinks(REFERENCE_PDR, REFERENCE_PDR))
    edge = EdgeProbe(net)
    net.run(net.slots_for(FORMATION_CAP_S), until=lambda x: len(edge.first_join) == n)
    if len(edge.first_join) < n:
        result.incomplete += 1
    if edge.first_join:
        t0 = min(edge.first_join.values())
        offsets = sorted(net.seconds(asn - t0) for asn in edge.first_join.values())
        result.formation_p95_s.append(nearest_rank(offsets, 95))
        result.formation_p100_s.append(nearest_rank(offsets, 100))
    edge.measure(measure_s)
    result.rtt_ms.extend(net.seconds(s) * 1000 for s in edge.rtt_slots)
    sent = sum(node.uplink.sent for node in net.nodes.values())
    received = sum(node.uplink.received for node in net.nodes.values())
    if sent:
        result.pdr.append(received / sent)


def _mean(values: list[float]) -> str:
    return f"{statistics.mean(values):.2f}" if values else "-"


@click.command()
@click.option("--n", "sizes", default="10,40,60,100", show_default=True, help="Node counts.")
@click.option("--reps", default=20, show_default=True, help="Seeds per node count.")
@click.option("--measure-s", default=10.0, show_default=True, help="Probing time after formation.")
@click.option("--seed", default=0, show_default=True, help="First seed.")
def main(sizes: str, reps: int, measure_s: float, seed: int):
    """Run each node count on its smallest schedule and print it against the hardware reference."""
    started = time.monotonic()
    rows = []
    for n in [int(s) for s in sizes.split(",")]:
        result = Result()
        for rep in range(reps):
            run_one(n, seed + rep, measure_s, result)
        schedule = Network.for_nodes(0).fw.schedule_for(n).name
        rtt = sorted(result.rtt_ms)
        rows.append(
            (
                f"{n:>4} {schedule:<7}",
                f"{_mean(result.formation_p95_s):>6} {REFERENCE_FORMATION_P95_S.get(n, '-'):>6}",
                f"{_mean(result.formation_p100_s):>6}",
                f"{(nearest_rank(rtt, 50) if rtt else 0):>6.0f} {REFERENCE_RTT_P50_MS[schedule]:>6}",
                f"{100 * statistics.mean(result.pdr or [0]):>6.1f} {100 * REFERENCE_PDR:>6.1f}",
                f"{result.incomplete:>4}",
            )
        )
    click.echo("   N sched    form95s    hw form100s  rtt50ms    hw    pdr%    hw  dnf")
    for row in rows:
        click.echo("  ".join(row))
    click.echo(
        f"\n{reps} seeds per N, every link at {100 * REFERENCE_PDR:.1f}% PDR; {measure_s:.0f} s probing each; "
        f"RTT is wire time (no UART or host); {time.monotonic() - started:.1f} s wall."
    )


if __name__ == "__main__":
    main()
