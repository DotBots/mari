"""The slot engine: one gateway, N nodes, a shared medium and a virtual ASN clock.

Each `step` is one slot, processed in the order `new_slot_synced` in
firmware/mari/mac.c runs it: timeouts first, then the cell's radio activity.
Timeouts are kept on a heap rather than polled per node, so a slot costs only
what its cell involves.
"""

from __future__ import annotations

import heapq
import math
import random
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

from marilib.mari_protocol import MARI_BROADCAST_ADDRESS, MARI_NET_ID_DEFAULT, Frame, Header
from marilib.model import EdgeEvent
from marilib.protocol import PacketType
from marilib.sim.app import STATUS_PERIOD_S, HandoverInfo, NodeApp, RxInfo, SyntheticApp
from marilib.sim.links import DOWN, UP, Link, LinkModel, PerfectLinks
from marilib.sim.firmware import (
    BEACON,
    DOWNLINK,
    SHARED_UPLINK,
    Firmware,
    Schedule,
    load_firmware,
)
from marilib.sim.gateway import SimGateway
from marilib.sim.node import DisconnectReason, MariQueue, NodeState, SimNode
from marilib.sim.trace import Trace

DEFAULT_GATEWAY_ADDRESS = 0x0001
DEFAULT_NODE_ADDRESS_BASE = 0x1000


@dataclass(frozen=True)
class AirFrame:
    """A frame on the medium. `cell` is the join response's cell id."""

    type: PacketType
    src: int
    dst: int
    payload: bytes = b""
    next_proto: int = 0
    cell: int = 0


class Network:
    def __init__(
        self,
        schedule: Schedule | str | int,
        *,
        seed: int = 0,
        links: LinkModel | None = None,
        gateway_position: tuple[float, float, float] = (0.0, 0.0, 0.0),
        firmware: Firmware | None = None,
        trace: Trace | None = None,
        network_id: int = MARI_NET_ID_DEFAULT,
        gateway_address: int = DEFAULT_GATEWAY_ADDRESS,
    ):
        self.fw = firmware or load_firmware()
        self.schedule = schedule if isinstance(schedule, Schedule) else self.fw.schedule(schedule)
        self.seed = seed
        self.rng = random.Random(f"{seed}:network")
        self.links = links if links is not None else PerfectLinks()
        self.gateway_position = gateway_position
        self.trace = trace
        self.network_id = network_id
        self.asn = 0

        fw = self.fw
        self.slot_us = fw.slot_us
        self.leave_slots = self.schedule.n_cells * fw["MARI_MAX_SLOTFRAMES_NO_RX_LEAVE"]
        self.scan_slots = fw["MARI_SCAN_MAX_SLOTS"]
        self.join_response_slots = fw["MARI_JOINING_STATE_TIMEOUT"] // self.slot_us + 1
        self.synced_timeout_slots = fw["MARI_JOIN_TIMEOUT_SINCE_SYNCED"] // self.slot_us + 1
        self.n_regular = fw["MARI_N_BLE_REGULAR_CHANNELS"]
        self.n_adv = fw["MARI_N_BLE_ADVERTISING_CHANNELS"]
        self.beacon_hopping = fw.flags["MARI_ENABLE_BEACON_HOPPING"]

        self.gateway = SimGateway(
            address=gateway_address,
            network_id=network_id,
            schedule=self.schedule,
            queue=MariQueue(fw["MARI_PACKET_QUEUE_SIZE"]),
            gateway_info_version=fw["MARI_GATEWAY_INFO_VERSION"],
        )
        self.nodes: dict[int, SimNode] = {}
        # Ordered sets (dicts) so iteration order, and with it the RNG draw
        # order, is fixed by the order nodes were added.
        self._scanning: dict[int, SimNode] = {}
        self._listening: dict[int, SimNode] = {}
        self._owners: dict[int, list[SimNode]] = {}  # node-side view of each U cell

        self._timers: list[tuple[int, int, str, int, int]] = []
        self._timer_seq = 0
        self._gateway_timer_armed: set[int] = set()

        self.on_uart: Callable[[bytes], None] | None = None
        self._edge_inbox: deque[bytes] = deque()
        self._edge_lock = threading.Lock()
        self.realtime_lag_slots = 0
        self.realtime_max_lag_s = 0.0
        self._reset_pending = False

    # ------------------------------------------------------------ building --

    @classmethod
    def for_nodes(
        cls,
        n_nodes: int,
        *,
        schedule: Schedule | str | int | None = None,
        distance_m: float = 2.0,
        status_period_s: float | None = STATUS_PERIOD_S,
        addresses: list[int] | None = None,
        **kwargs,
    ) -> "Network":
        """N stock nodes on a circle around the gateway, on the smallest schedule that fits.

        The first nodes take `addresses`, in order; the rest are numbered from
        DEFAULT_NODE_ADDRESS_BASE.
        """
        addresses = list(addresses or [])
        if len(addresses) > n_nodes:
            raise ValueError(f"{len(addresses)} addresses for {n_nodes} nodes")
        firmware = kwargs.pop("firmware", None) or load_firmware()
        net = cls(schedule or firmware.schedule_for(n_nodes), firmware=firmware, **kwargs)
        period = round(status_period_s * 1e6 / net.slot_us) if status_period_s else 0
        for i in range(n_nodes):
            angle = 2 * math.pi * i / max(n_nodes, 1)
            position = (
                round(distance_m * math.cos(angle), 9),
                round(distance_m * math.sin(angle), 9),
            )
            address = addresses[i] if i < len(addresses) else DEFAULT_NODE_ADDRESS_BASE + i + 1
            app = SyntheticApp(
                position=position,
                period_slots=period,
                rng=random.Random(f"{net.seed}:app:{address}"),
            )
            net.add_node(app, address=address)
        return net

    def add_node(self, app: NodeApp, *, address: int | None = None, start_asn: int | None = None):
        """Power a node on. `start_asn` defaults to a scan already under way when the gateway boots.

        A start before the current ASN means the node was scanning before then:
        its first scan ends somewhere within one scan duration from now.
        """
        if address is None:
            address = DEFAULT_NODE_ADDRESS_BASE + len(self.nodes) + 1
        if address in self.nodes:
            raise ValueError(f"node 0x{address:x} already exists")
        rng = random.Random(f"{self.seed}:node:{address}")
        node = SimNode(address, app, rng, MariQueue(self.fw["MARI_PACKET_QUEUE_SIZE"]))
        self.nodes[address] = node
        if start_asn is None:
            start_asn = self.asn - rng.randrange(self.scan_slots)
            node.scan_round = rng.randrange(self.n_adv)
        self._arm(max(start_asn, self.asn), "boot", node, start_asn)
        return node

    def remove_node(self, address: int) -> None:
        """Power a node off. The gateway notices only through its own timeout."""
        node = self.nodes.pop(address)
        self._forget(node)
        self._set_state(node, NodeState.OFF)

    # ------------------------------------------------------------- gateway --

    @property
    def gateway_asn(self) -> int:
        """The ASN the gateway reports and its nodes count, which a reset restarts from 0."""
        return self.asn - self.gateway.asn_base

    def halt_gateway(self) -> None:
        """Stop both gateway cores, as storm runs do over SWD: no beacons, no reception, no UART."""
        self.gateway.halted = True
        self._emit("gw_halted")

    def reset_gateway(self) -> None:
        """Reboot the gateway with empty tables. It comes back at the next slotframe boundary."""
        self.gateway.halted = True
        self._reset_pending = True

    def _reboot_gateway(self) -> None:
        self._reset_pending = False
        self.gateway.reboot(self.asn)
        self._timers = [t for t in self._timers if t[2] != "gw_timeout"]
        heapq.heapify(self._timers)
        self._gateway_timer_armed.clear()
        self._emit("gw_reset")

    # ---------------------------------------------------------------- edge --

    def edge_tx(self, data: bytes) -> None:
        """Bytes from the edge, as the gateway's UART would receive them. Thread-safe."""
        with self._edge_lock:
            self._edge_inbox.append(bytes(data))

    def _uart(self, data: bytes) -> None:
        if self.on_uart is not None:
            self.on_uart(data)

    def _drain_edge(self) -> None:
        with self._edge_lock:
            inbox, self._edge_inbox = self._edge_inbox, deque()
        for data in inbox:
            if self.gateway.halted:
                self._emit("uart_drop", len=len(data), why="halted")
                continue
            self.gateway.uart_rx_frames += 1
            self.gateway.uart_rx_bytes += len(data)
            # 03app_gateway_net: only NODE_DATA longer than a header is accepted
            if data[0] != EdgeEvent.NODE_DATA or len(data) <= 1 + Header().size:
                self._emit("uart_drop", len=len(data))
                continue
            frame = Frame().from_bytes(data[1:])
            frame.header.source = self.gateway.address
            frame.header.network_id = self.network_id
            self.gateway.stamp_tx_probe(frame, self.gateway_asn)
            air = AirFrame(
                PacketType.DATA,
                self.gateway.address,
                frame.header.destination,
                frame.payload,
                frame.header.next_proto,
            )
            if not self.gateway.queue.add(air):
                self._emit("queue_wrap", node="gw")

    # ----------------------------------------------------------------- run --

    def step(self) -> None:
        asn = self.asn
        index = asn % self.schedule.n_cells
        if index == 0 and self._reset_pending:
            self._reboot_gateway()
        self._drain_edge()
        if index == 0 and not self.gateway.halted:
            self._uart(self.gateway.uart_gateway_info(self.gateway_asn, self.slot_us))
        self._fire_timers(asn)
        cell = self.schedule.cells[index]
        if cell.type == BEACON:
            self._beacon_cell(asn, index, cell.channel_offset)
        elif cell.type == SHARED_UPLINK:
            self._shared_uplink_cell(asn, index)
        elif cell.type == DOWNLINK:
            self._downlink_cell(asn, index)
        else:
            self._uplink_cell(asn, index)
        self.asn += 1

    def run(
        self,
        slots: int | None = None,
        *,
        until: Callable[["Network"], bool] | None = None,
        realtime: bool = False,
        stop: threading.Event | None = None,
    ) -> int:
        """Step until `slots` have passed, `until` holds, or `stop` is set. Returns slots run.

        With `realtime`, slots are paced to the wall clock; a slot that starts
        late is counted in `realtime_lag_slots` rather than skipped.
        """
        start_asn, start_wall = self.asn, time.monotonic()
        slot_s = self.slot_us / 1e6
        while slots is None or self.asn - start_asn < slots:
            if until is not None and until(self):
                break
            if stop is not None and stop.is_set():
                break
            if realtime:
                due = start_wall + (self.asn - start_asn) * slot_s
                ahead = due - time.monotonic()
                if ahead > 0:
                    time.sleep(ahead)
                elif ahead < -slot_s:
                    self.realtime_lag_slots += 1
                    self.realtime_max_lag_s = max(self.realtime_max_lag_s, -ahead)
            self.step()
        return self.asn - start_asn

    def seconds(self, slots: int) -> float:
        return slots * self.slot_us / 1e6

    def slots_for(self, seconds: float) -> int:
        return round(seconds * 1e6 / self.slot_us)

    @property
    def joined_count(self) -> int:
        return len(set(self.gateway.joined))

    # -------------------------------------------------------------- timers --

    def _arm(self, due: int, kind: str, node: SimNode | None, token: int = 0) -> None:
        self._timer_seq += 1
        address = node.address if node is not None else token
        heapq.heappush(self._timers, (due, self._timer_seq, kind, address, token))

    def _fire_timers(self, asn: int) -> None:
        timers = self._timers
        while timers and timers[0][0] <= asn:
            _, _, kind, address, token = heapq.heappop(timers)
            if kind == "gw_timeout":
                if not self.gateway.halted:
                    self._gateway_timeout(asn, cell_index=token)
                continue
            node = self.nodes.get(address)
            if node is None:
                continue
            if kind == "boot":
                if node.state == NodeState.OFF:
                    self._start_scan(node, token)
            elif kind == "synced_timeout":
                if token == node.sync_epoch and node.state in (NodeState.SYNCED, NodeState.JOINING):
                    self._give_up_joining(node, asn, "synced_timeout")
            elif node.epoch != token:
                continue  # armed in an earlier state; superseded
            elif kind == "scan_end":
                self._end_scan(node, asn)
            elif kind == "leave":
                self._check_leave(node, asn)
            elif kind == "join_timeout":
                self._failed_join(node, asn)

    # --------------------------------------------------------------- state --

    def _set_state(self, node: SimNode, state: NodeState, **fields) -> None:
        previous = node.state
        node.state = state
        node.epoch += 1
        self._emit(
            "state", node=node.address, **{"from": previous.name, "to": state.name}, **fields
        )

    def _forget(self, node: SimNode) -> None:
        self._scanning.pop(node.address, None)
        self._listening.pop(node.address, None)
        self._release_cell(node)

    def _release_cell(self, node: SimNode) -> None:
        if node.cell is not None:
            self._owners[node.cell].remove(node)
            node.cell = None

    def _start_scan(self, node: SimNode, start_asn: int) -> None:
        self._forget(node)
        node.scan_channel = self.n_regular + (
            node.scan_round % self.n_adv if self.beacon_hopping else 0
        )
        node.scan_round += 1
        node.scan_end_asn = start_asn + self.scan_slots
        node.scan_beacon_capacity = None
        self._set_state(node, NodeState.SCANNING, channel=node.scan_channel)
        self._scanning[node.address] = node
        self._arm(max(node.scan_end_asn, self.asn), "scan_end", node, node.epoch)

    def _end_scan(self, node: SimNode, asn: int) -> None:
        if not node.scan_beacon_capacity:
            self._start_scan(node, asn)
            return
        # sync_to_gateway + mr_assoc_node_handle_synced; slots start at the next boundary
        del self._scanning[node.address]
        node.sync_asn = asn
        node.sync_epoch += 1
        node.active_from = asn + 1
        node.remaining_capacity = node.scan_beacon_capacity
        node.init_backoff(self.fw["MARI_BACKOFF_N_MIN"])
        self._set_state(node, NodeState.SYNCED, backoff=node.backoff_time)
        self._listening[node.address] = node
        self._arm(asn + self.synced_timeout_slots, "synced_timeout", node, node.sync_epoch)

    def _give_up_joining(self, node: SimNode, asn: int, why: str) -> None:
        node.reset_backoff()
        self._set_state(node, NodeState.IDLE, why=why)
        self._start_scan(node, asn)

    def _failed_join(self, node: SimNode, asn: int) -> None:
        """mr_assoc_node_handle_failed_join: back off and retry, or rescan when stuck or full."""
        if node.remaining_capacity == 0:
            self._give_up_joining(node, asn, "gateway_full")
            return
        node.register_collision_backoff(
            self.fw["MARI_BACKOFF_N_MIN"], self.fw["MARI_BACKOFF_N_MAX"]
        )
        if node.consecutive_max_backoff >= self.fw["MARI_BACKOFF_MAX_STREAK"]:
            self._give_up_joining(node, asn, "backoff_streak")
            return
        self._set_state(node, NodeState.SYNCED, backoff=node.backoff_time, n=node.backoff_n)

    def _joined(self, node: SimNode, cell: int, asn: int) -> None:
        """mr_assoc_node_handle_joined."""
        if node.handover_pending:
            node.handover = HandoverInfo(
                duration_ms=min(
                    round((asn - node.handover_start_asn) * self.slot_us / 1000), 0xFFFF
                ),
                seq=(node.handover.seq + 1) & 0xFF,
                reason=node.handover_reason,
            )
            node.handover_pending = False
        node.queue.reset()
        while node.app.next_uplink(asn) is not None:
            pass  # produced while not joined: lost with the queue reset
        node.reset_backoff()
        node.last_rx_asn = asn
        node.pending_disconnect = 0
        node.cell = cell
        self._owners.setdefault(cell, []).append(node)
        node.joins += 1
        if node.first_joined_asn is None:
            node.first_joined_asn = asn
        self._set_state(node, NodeState.JOINED, cell=cell)
        self._arm(asn + self.leave_slots + 1, "leave", node, node.epoch)

    def _check_leave(self, node: SimNode, asn: int) -> None:
        """mr_assoc_node_should_leave, evaluated only when it could have become true."""
        if node.pending_disconnect:
            self._disconnect(node, asn, node.pending_disconnect)
        elif asn - node.last_rx_asn > self.leave_slots:
            self._disconnect(node, asn, DisconnectReason.PEER_LOST_TIMEOUT)
        else:
            self._arm(node.last_rx_asn + self.leave_slots + 1, "leave", node, node.epoch)

    def _disconnect(self, node: SimNode, asn: int, reason: int) -> None:
        if not node.handover_pending:
            node.handover_pending = True
            node.handover_start_asn = asn
            node.handover_reason = int(reason)
        self._release_cell(node)
        self._set_state(node, NodeState.IDLE, why=DisconnectReason(reason).name)
        self._start_scan(node, asn)

    # --------------------------------------------------------------- cells --

    def _beacon_channel(self, channel_offset: int) -> int:
        return self.n_regular + (channel_offset % self.n_adv if self.beacon_hopping else 0)

    def _link(self, node: SimNode, direction: str, channel: int) -> Link:
        x, y = node.app.position_m()
        return Link(
            node.address,
            self.gateway.address,
            direction,
            self.asn,
            channel,
            (x, y, 0.0),
            self.gateway_position,
        )

    def _data_channel(self, index: int) -> int:
        # scheduler.c mr_scheduler_get_channel, RFC 7554: (ASN + offset) mod nFreq
        return (self.gateway_asn + self.schedule.cells[index].channel_offset) % self.n_regular

    def _received(self, link: Link, node: SimNode) -> bool:
        return node.rng.random() < self.links.pdr(link)

    def _beacon_cell(self, asn: int, index: int, channel_offset: int) -> None:
        gw = self.gateway
        if gw.halted:
            return
        self.gateway.set_usage(index, True)
        capacity = gw.remaining_capacity
        channel = self._beacon_channel(channel_offset)
        members = set(gw.joined)
        for node in self._scanning.values():
            if (
                node.scan_channel == channel
                and capacity > 0
                and self._received(self._link(node, DOWN, channel), node)
            ):
                node.scan_beacon_capacity = capacity
        for node in self._listening.values():
            if node.active_from > asn or not self._received(self._link(node, DOWN, channel), node):
                continue
            node.remaining_capacity = capacity
            if node.state == NodeState.JOINED:
                if node.address not in members:
                    if not node.pending_disconnect:
                        node.pending_disconnect = DisconnectReason.PEER_LOST_BLOOM
                        self._arm(asn + 1, "leave", node, node.epoch)
                else:
                    node.last_rx_asn = asn

    def _shared_uplink_cell(self, asn: int, index: int) -> None:
        senders = []
        for node in self._listening.values():
            if node.active_from > asn or node.state != NodeState.SYNCED:
                continue
            if node.backoff_time > 0:
                node.backoff_time -= 1
            if node.backoff_time == 0:
                senders.append(node)
        for node in senders:
            node.joining_asn = asn
            self._set_state(node, NodeState.JOINING)
            self._arm(asn + self.join_response_slots, "join_timeout", node, node.epoch)
        if self.gateway.halted:
            for node in senders:
                self._emit("tx", node=node.address, cell=index, kind="S", ok=False, why="halted")
            return
        self.gateway.set_usage(index, bool(senders))
        if len(senders) > 1:
            for node in senders:
                self._emit("tx", node=node.address, cell=index, kind="S", ok=False, why="collision")
            return
        for node in senders:
            ok = self._received(self._link(node, UP, self._data_channel(index)), node)
            self._emit("tx", node=node.address, cell=index, kind="S", ok=ok, **_lost(ok))
            if ok:
                self._gateway_join_request(node.address, asn)

    def _gateway_join_request(self, address: int, asn: int) -> None:
        gw = self.gateway
        cell, new = gw.assign_cell(address, asn)
        if cell is None:
            self._emit("gateway_full", node=address)
            return
        gw.join_response = (address, cell)
        if new:
            self._arm_gateway_timeout(cell, asn)
        gw.probes.add(address)
        self._emit("gw_joined", node=address, cell=cell)
        self._uart(gw.uart_node_event(EdgeEvent.NODE_JOINED, address))

    def _arm_gateway_timeout(self, cell: int, last_asn: int) -> None:
        if cell not in self._gateway_timer_armed:
            self._gateway_timer_armed.add(cell)
            self._arm(last_asn + self.leave_slots + 1, "gw_timeout", None, cell)

    def _gateway_timeout(self, asn: int, cell_index: int) -> None:
        """mr_assoc_gateway_clear_old_nodes, for one cell."""
        self._gateway_timer_armed.discard(cell_index)
        entry = self.gateway.cells[cell_index]
        node, last = entry
        if not node:
            return
        if asn - last <= self.leave_slots:
            self._arm_gateway_timeout(cell_index, last)
            return
        entry[0], entry[1] = 0, 0
        if self.gateway.cell_of(node) is None:
            self.gateway.probes.clear(node)
        self._emit("gw_left", node=node, cell=cell_index)
        self._uart(self.gateway.uart_node_event(EdgeEvent.NODE_LEFT, node))

    def _downlink_cell(self, asn: int, index: int) -> None:
        gw = self.gateway
        if gw.halted:
            return
        if gw.join_response is not None:
            address, cell = gw.join_response
            gw.join_response = None
            frame = AirFrame(PacketType.JOIN_RESPONSE, gw.address, address, cell=cell)
        else:
            frame = gw.queue.pop()
        gw.set_usage(index, frame is not None)
        if frame is None:
            return
        if frame.type == PacketType.JOIN_RESPONSE:
            node = self._listening.get(frame.dst)
            if node is None or node.active_from > asn:
                return
            link = self._link(node, DOWN, self._data_channel(index))
            ok = self._received(link, node)
            self._emit("tx", node=node.address, cell=index, kind="D", ok=ok, join=True, **_lost(ok))
            if ok and node.state == NodeState.JOINING:
                self._joined(node, frame.cell, asn)
            return
        if frame.dst == MARI_BROADCAST_ADDRESS:
            receivers = [n for n in self._listening.values() if n.state == NodeState.JOINED]
        else:
            node = self._listening.get(frame.dst)
            receivers = [node] if node is not None and node.state == NodeState.JOINED else []
        for node in receivers:
            if node.active_from > asn:
                continue
            node.downlink.sent += 1
            link = self._link(node, DOWN, self._data_channel(index))
            ok = self._received(link, node)
            self._emit("tx", node=node.address, cell=index, kind="D", ok=ok, **_lost(ok))
            if not ok:
                continue
            node.downlink.received += 1
            node.last_rx_asn = asn
            rssi = self.links.rssi_dbm(link)
            rx = RxInfo(asn, rssi, node.handover, self.gateway_asn)
            node.app.on_downlink(self._to_frame(frame), rx)

    def _uplink_cell(self, asn: int, index: int) -> None:
        senders = [n for n in self._owners.get(index, ()) if n.active_from <= asn]
        gw = self.gateway
        frames = []
        for node in senders:
            while (uplink := node.app.next_uplink(asn)) is not None:
                payload, next_proto = (
                    uplink if isinstance(uplink, tuple) else (uplink, node.app.next_proto)
                )
                air = AirFrame(PacketType.DATA, node.address, gw.address, payload, next_proto)
                if not node.queue.add(air):
                    self._emit("queue_wrap", node=node.address)
            frame = node.queue.pop()
            if frame is None and self.fw["MARI_AUTO_UPLINK_KEEPALIVE"]:
                frame = AirFrame(PacketType.KEEP_ALIVE, node.address, gw.address)
            if frame is not None:
                frames.append((node, frame))
        if gw.halted:
            for node, frame in frames:
                node.uplink.sent += frame.type == PacketType.DATA
                self._emit("tx", node=node.address, cell=index, kind="U", ok=False, why="halted")
            return
        gw.set_usage(index, bool(frames))
        if len(frames) > 1:
            for node, _ in frames:
                self._emit("tx", node=node.address, cell=index, kind="U", ok=False, why="collision")
            return
        for node, frame in frames:
            is_data = frame.type == PacketType.DATA
            if is_data:
                node.uplink.sent += 1
            link = self._link(node, UP, self._data_channel(index))
            ok = self._received(link, node)
            self._emit("tx", node=node.address, cell=index, kind="U", ok=ok, **_lost(ok))
            if not ok or gw.cell_of(node.address) is None:
                continue  # the gateway only listens to nodes it holds a cell for
            gw.keep_alive(node.address, asn)
            if is_data:
                node.uplink.received += 1
                mari_frame = self._to_frame(frame)
                gw.stamp_rx_probe(mari_frame, self.gateway_asn, self.links.rssi_dbm(link))
                self._uart(gw.uart_data(mari_frame))
            else:
                self._uart(gw.uart_node_event(EdgeEvent.NODE_KEEP_ALIVE, node.address))

    def _to_frame(self, frame: AirFrame) -> Frame:
        header = Header(
            type_=frame.type,
            network_id=self.network_id,
            destination=frame.dst,
            source=frame.src,
            next_proto=frame.next_proto,
        )
        return Frame(header, frame.payload)

    def _emit(self, ev: str, **fields) -> None:
        if self.trace is None:
            return
        if isinstance(fields.get("node"), int):
            fields["node"] = f"0x{fields['node']:x}"
        self.trace.emit(self.asn, ev, **fields)


def _lost(ok: bool) -> dict:
    return {} if ok else {"why": "lost"}
