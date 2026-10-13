"""Drive a network for a while, apply scripted events, and summarize what happened.

The summary is computed from the simulation's own slot clock, so a run paced
to the wall clock and a fast run of the same seed and events report alike.
"""

from __future__ import annotations

import random
import threading
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import IO

from marilib.mari_protocol import Frame, Header, MetricsProbePayload
from marilib.model import EdgeEvent
from marilib.sim.app import STATUS_PERIOD_S, SyntheticApp, is_probe
from marilib.sim.events import Event
from marilib.sim.network import DEFAULT_NODE_ADDRESS_BASE, Network
from marilib.sim.trace import Trace


class Recorder(Trace):
    """A trace that also counts what the summary needs: uplink attempts, gateway joins and leaves."""

    def __init__(self, stream: IO[str] | None = None):
        super().__init__(stream)
        self.clear()

    def clear(self) -> None:
        # Uplink cell transmissions, data and keep-alive, collisions and halted-gateway slots excluded
        self.attempts: Counter[int] = Counter()
        self.delivered: Counter[int] = Counter()
        self.joined_at: dict[int, list[int]] = defaultdict(list)
        self.left_at: dict[int, list[int]] = defaultdict(list)
        self.rtt_slots: dict[int, list[int]] = defaultdict(list)
        self.last_join_asn: int | None = None

    def emit(self, asn: int, ev: str, **fields) -> None:
        super().emit(asn, ev, **fields)
        if (
            ev == "tx"
            and fields["kind"] == "U"
            and fields.get("why") not in ("collision", "halted")
        ):
            node = int(fields["node"], 16)
            self.attempts[node] += 1
            self.delivered[node] += fields["ok"]
        elif ev == "gw_joined":
            self.joined_at[int(fields["node"], 16)].append(asn)
            self.last_join_asn = asn
        elif ev == "gw_left":
            self.left_at[int(fields["node"], 16)].append(asn)

    def on_uart(self, data: bytes) -> None:
        """Gateway-to-gateway probe round trips, from the ASNs the gateway stamps on the way."""
        if data[0] != EdgeEvent.NODE_DATA:
            return
        frame = Frame().from_bytes(data[1:])
        if is_probe(frame.payload):
            probe = MetricsProbePayload().from_bytes(frame.payload)
            if probe.gw_tx_enqueued_asn:
                self.rtt_slots[frame.header.source].append(
                    probe.gw_rx_asn - probe.gw_tx_enqueued_asn
                )


@dataclass(frozen=True)
class Storms:
    """Join storms as storm_run.py makes them on the bench: halt the gateway, reset it, let the network re-form.

    A storm has finished once no join has arrived for `quiet_s`, or `settle_s`
    after the reset, whichever comes first.
    """

    cycles: int
    hold_s: float = 6.0
    quiet_s: float = 3.0
    settle_s: float = 90.0


@dataclass(frozen=True)
class Storm:
    reset_asn: int
    nodes: int  # distinct nodes that joined
    joins: int
    span_slots: int  # first join to last join


@dataclass(frozen=True)
class FiredEvent:
    event: Event
    asn: int
    attempts: int
    delivered: int


class Session:
    """A network plus its event script, probing and recorder. `run` may be called from any thread."""

    def __init__(
        self,
        net: Network,
        *,
        events: list[Event] = (),
        probe_interval_s: float = 0.0,
        default_distance_m: float = 2.0,
        trace_stream: IO[str] | None = None,
        storms: Storms | None = None,
    ):
        self.net = net
        self.recorder = Recorder(trace_stream)
        net.trace = self.recorder
        self.start_asn = net.asn
        self.pending = list(events)
        self.fired: list[FiredEvent] = []
        self.default_distance_m = default_distance_m
        self.probe_gap_slots = net.slots_for(probe_interval_s) if probe_interval_s else 0
        self._next_probe_asn = net.asn
        self._probe_turn = 0
        self.storms = storms
        self.storm_results: list[Storm] = []
        self._storm_hold_until: int | None = None
        self._storm_since = net.asn
        self._storm_first = True
        self.tap_uart()

    def tap_uart(self) -> None:
        """Put the recorder in front of whatever consumes the UART now (an adapter, or nothing)."""
        downstream = self.net.on_uart

        def on_uart(data: bytes) -> None:
            self.recorder.on_uart(data)
            if downstream is not None:
                downstream(data)

        self.net.on_uart = on_uart

    def run(
        self,
        duration_s: float | None = None,
        *,
        realtime: bool = False,
        stop: threading.Event | None = None,
    ) -> int:
        slots = self.net.slots_for(duration_s) if duration_s is not None else None
        return self.net.run(slots, until=self._tick, realtime=realtime, stop=stop)

    def form(self, quiet_s: float, settle_s: float) -> None:
        """Run as fast as possible until joins stop, then start the run's clock and counters from there.

        Bench runs begin with the network already formed, so a run that is
        compared with one should too.
        """
        net, rec, start = self.net, self.recorder, self.net.asn

        def formed(net: Network) -> bool:
            last = rec.last_join_asn
            if last is not None and net.asn - last >= net.slots_for(quiet_s):
                return True
            return net.asn - start >= net.slots_for(settle_s)

        net.run(until=formed)
        rec.clear()
        self.start_asn = self._storm_since = self._next_probe_asn = net.asn

    def _tick(self, net: Network) -> bool:
        while self.pending and net.slots_for(self.pending[0].at_s) + self.start_asn <= net.asn:
            self._apply(self.pending.pop(0))
        if self.probe_gap_slots:
            self._probe()
        return self.storms is not None and self._storm_tick()

    def _storm_tick(self) -> bool:
        """Advance the halt / reset / re-form cycle. True once the last storm has settled."""
        net, rec, plan = self.net, self.recorder, self.storms
        if self._storm_hold_until is not None:
            if net.asn >= self._storm_hold_until:
                self._storm_hold_until = None
                net.reset_gateway()
                self._storm_since = net.asn
            return False
        last = rec.last_join_asn
        joined_since = last is not None and last >= self._storm_since
        quiet = net.slots_for(plan.quiet_s)
        if self._storm_first:
            done = net.asn - max(self._storm_since, last or 0) >= quiet
        else:
            done = (joined_since and net.asn - last >= quiet) or (
                net.asn - self._storm_since >= net.slots_for(plan.settle_s)
            )
        if not done:
            return False
        if not self._storm_first:
            self.storm_results.append(self._storm_result())
        self._storm_first = False
        if len(self.storm_results) >= plan.cycles:
            return True
        net.halt_gateway()
        self._storm_hold_until = net.asn + net.slots_for(plan.hold_s)
        return False

    def _storm_result(self) -> Storm:
        since = self._storm_since
        joins = [
            (asn, node)
            for node, asns in self.recorder.joined_at.items()
            for asn in asns
            if asn >= since
        ]
        times = sorted(asn for asn, _ in joins)
        return Storm(
            reset_asn=since,
            nodes=len({node for _, node in joins}),
            joins=len(joins),
            span_slots=times[-1] - times[0] if times else 0,
        )

    def _apply(self, event: Event) -> None:
        net, rec = self.net, self.recorder
        if event.action == "halt":
            self.fired.append(FiredEvent(event, net.asn, 0, 0))
            net.halt_gateway()
            return
        if event.action == "reset":
            self.fired.append(FiredEvent(event, net.asn, 0, 0))
            net.reset_gateway()
            return
        address = event.address
        self.fired.append(FiredEvent(event, net.asn, rec.attempts[address], rec.delivered[address]))
        if event.action == "remove":
            net.remove_node(address)
        elif event.action == "move":
            net.nodes[address].app.position = event.position
        else:
            period = round(STATUS_PERIOD_S * 1e6 / net.slot_us)
            app = SyntheticApp(
                position=event.position or (self.default_distance_m, 0.0),
                period_slots=period,
                rng=random.Random(f"{net.seed}:app:{address}"),
            )
            net.add_node(app, address=address, start_asn=net.asn)

    def _probe(self) -> None:
        """One probe per joined node per interval, spread evenly so the gateway queue never bursts."""
        net = self.net
        if net.asn < self._next_probe_asn:
            return
        joined = sorted(set(net.gateway.joined))
        if not joined:
            self._next_probe_asn = net.asn + self.probe_gap_slots
            return
        address = joined[self._probe_turn % len(joined)]
        self._probe_turn += 1
        payload = MetricsProbePayload(edge_tx_ts_us=net.asn).to_bytes()
        frame = Frame(Header(destination=address), bytes(payload))
        net.edge_tx(EdgeEvent.to_bytes(EdgeEvent.NODE_DATA) + frame.to_bytes())
        self._next_probe_asn = net.asn + max(1, self.probe_gap_slots // len(joined))

    # ------------------------------------------------------------- summary --

    def seconds(self, asn: int) -> float:
        return self.net.seconds(asn - self.start_asn)

    def summary(self) -> str:
        net, rec = self.net, self.recorder
        addresses = sorted(set(net.nodes) | set(rec.attempts) | set(rec.joined_at))
        powered = set(net.nodes)
        joined_now = set(net.gateway.joined)
        lines = [
            f"{'node':>16} {'dist_m':>6} {'joins':>5} {'left':>4} {'first_join_s':>12} "
            f"{'uplink_pdr%':>11} {'rtt_p50_ms':>10}"
        ]
        for address in addresses:
            node = net.nodes.get(address)
            joins = rec.joined_at.get(address, [])
            lines.append(
                f"{_label(address):>16} "
                f"{(f'{node.distance_m():.1f}' if node else 'off'):>6} "
                f"{len(joins):>5} {len(rec.left_at.get(address, [])):>4} "
                f"{(f'{self.seconds(joins[0]):.2f}' if joins else '-'):>12} "
                f"{_pct(rec.delivered[address], rec.attempts[address]):>11} "
                f"{self._rtt_p50(rec.rtt_slots.get(address, [])):>10}"
            )
        all_rtt = [s for slots in rec.rtt_slots.values() for s in slots]
        lines.append(
            f"{'all':>16} {'':>6} {sum(map(len, rec.joined_at.values())):>5} "
            f"{sum(map(len, rec.left_at.values())):>4} {'':>12} "
            f"{_pct(sum(rec.delivered.values()), sum(rec.attempts.values())):>11} "
            f"{self._rtt_p50(all_rtt):>10}"
        )
        header = (
            f"{len(joined_now & powered)}/{len(powered)} nodes joined at "
            f"{self.seconds(net.asn):.1f} s, schedule {net.schedule.name}, seed {net.seed}"
        )
        lines = [header, "", *lines]
        if self.fired:
            lines += ["", "events"] + [f"  {self._describe(f)}" for f in self.fired]
        if self.storm_results:
            lines += ["", f"{'storm':>6} {'reset_s':>8} {'nodes':>5} {'joins':>5} {'span_s':>7}"]
            for i, storm in enumerate(self.storm_results, 1):
                lines.append(
                    f"{i:>6} {self.seconds(storm.reset_asn):>8.1f} {storm.nodes:>5} {storm.joins:>5} "
                    f"{net.seconds(storm.span_slots):>7.2f}"
                )
        return "\n".join(lines)

    def _rtt_p50(self, slots: list[int]) -> str:
        if not slots:
            return "-"
        return f"{self.net.seconds(sorted(slots)[(len(slots) - 1) // 2]) * 1000:.0f}"

    def _describe(self, fired: FiredEvent) -> str:
        event, rec = fired.event, self.recorder
        text = f"{self.seconds(fired.asn):6.1f} s  {event.describe()}"
        if event.node is None:
            return text
        address = event.address
        if event.action == "remove":
            left = [a for a in rec.left_at.get(address, []) if a >= fired.asn]
            seen = (
                f"gateway saw it leave at {self.seconds(left[0]):.1f} s"
                if left
                else "gateway never saw it leave"
            )
            return f"{text}: {seen}"
        if event.action == "add":
            joined = [a for a in rec.joined_at.get(address, []) if a >= fired.asn]
            seen = f"joined at {self.seconds(joined[0]):.1f} s" if joined else "never joined"
            return f"{text}: {seen}"
        after = next(
            (f for f in self.fired if f.event.node == event.node and f.asn > fired.asn), None
        )
        attempts = (after.attempts if after else rec.attempts[address]) - fired.attempts
        delivered = (after.delivered if after else rec.delivered[address]) - fired.delivered
        before = self._before(fired)
        return f"{text}: uplink PDR {_pct(*before)}% before, {_pct(delivered, attempts)}% after"

    def _before(self, fired: FiredEvent) -> tuple[int, int]:
        earlier = [f for f in self.fired if f.event.node == fired.event.node and f.asn < fired.asn]
        base = (earlier[-1].delivered, earlier[-1].attempts) if earlier else (0, 0)
        return fired.delivered - base[0], fired.attempts - base[1]


def _pct(delivered: int, attempts: int) -> str:
    return f"{100 * delivered / attempts:.1f}" if attempts else "-"


def _label(address: int) -> str:
    """A numbered node (Network.for_nodes, events) by its number, any other by its address."""
    number = address - DEFAULT_NODE_ADDRESS_BASE
    return str(number) if 0 < number < 0x1000 else f"{address:X}"
