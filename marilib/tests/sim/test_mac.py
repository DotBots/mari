import random

from marilib.mari_protocol import (
    MARI_BROADCAST_ADDRESS,
    Frame,
    Header,
    MetricsProbePayload,
    NextProto,
)
from marilib.model import EdgeEvent
from marilib.sim.app import SyntheticApp
from marilib.sim.links import PerfectLinks
from marilib.sim.network import Network
from marilib.sim.node import MariQueue, NodeState, SimNode
from marilib.sim.trace import Trace

from .conftest import Uart, perfect_network


def edge_frame(dst: int, payload: bytes) -> bytes:
    return (
        EdgeEvent.to_bytes(EdgeEvent.NODE_DATA) + Frame(Header(destination=dst), payload).to_bytes()
    )


def joined(net: Network) -> int:
    return net.run(
        net.slots_for(10),
        until=lambda x: all(n.state == NodeState.JOINED for n in x.nodes.values()),
    )


def test_queue_wraps_to_empty_after_size_adds():
    queue = MariQueue(4)
    assert all(queue.add(i) for i in range(3))
    assert len(queue) == 3
    assert queue.add(3) is False  # the fourth add makes the ring read empty
    assert queue.pop() is None


def test_backoff_window_grows_to_the_max_and_counts_the_streak():
    node = SimNode(1, SyntheticApp(), random.Random(0), MariQueue(32))
    node.init_backoff(4)
    assert node.backoff_n == 4 and 0 <= node.backoff_time < 16
    windows = []
    for _ in range(4):
        node.register_collision_backoff(4, 6)
        windows.append((node.backoff_n, node.consecutive_max_backoff))
        assert 0 <= node.backoff_time < 1 << node.backoff_n
    assert windows == [(5, 0), (6, 1), (6, 2), (6, 3)]


def test_a_lone_node_joins_the_first_uplink_cell():
    net = perfect_network(1)
    uart = Uart(net)
    joined(net)
    node = next(iter(net.nodes.values()))
    assert node.state == NodeState.JOINED
    assert node.cell == net.schedule.indices("U")[0]
    assert [body for _, body in uart.of(EdgeEvent.NODE_JOINED)] == [node.address]


def test_a_full_gateway_stops_accepting_joins():
    net = perfect_network(11, "tiny")
    net.run(net.slots_for(10))
    assert net.joined_count == 10
    assert sorted(n.state for n in net.nodes.values()).count(NodeState.JOINED) == 10


def test_a_node_powered_off_is_dropped_after_the_leave_window():
    net = perfect_network(2)
    uart = Uart(net)
    joined(net)
    node = next(iter(net.nodes))
    net.run(net.schedule.n_cells)
    last_heard = net.gateway.cells[net.gateway.cell_of(node)][1]
    net.remove_node(node)
    net.run(2 * net.leave_slots)
    left = uart.of(EdgeEvent.NODE_LEFT)
    assert [body for _, body in left] == [node]
    assert left[0][0] == last_heard + net.leave_slots + 1
    assert net.joined_count == 1


def test_a_node_the_gateway_forgot_leaves_on_the_next_beacon_and_rejoins():
    net = perfect_network(1, trace=Trace(keep=True))
    joined(net)
    node = next(iter(net.nodes.values()))
    entry = net.gateway.cells[node.cell]
    entry[0] = entry[1] = 0  # the gateway timed it out while the node still thinks it is joined
    net.run(net.slots_for(3), until=lambda x: node.joins == 2)
    reasons = [e.get("why") for e in net.trace.events if e["ev"] == "state" and e["to"] == "IDLE"]
    assert reasons == ["PEER_LOST_BLOOM"]
    assert node.state == NodeState.JOINED


def test_unicast_and_broadcast_downlinks_reach_the_app():
    net = perfect_network(3)
    joined(net)
    first, *rest = net.nodes.values()
    net.edge_tx(edge_frame(first.address, b"\x01hello"))
    net.edge_tx(edge_frame(MARI_BROADCAST_ADDRESS, b"\x01all"))
    net.run(net.schedule.n_cells * 2)
    assert first.app.received == 2
    assert [n.app.received for n in rest] == [1, 1]


def test_a_probe_round_trip_is_stamped_in_slot_order():
    net = perfect_network(1)
    uart = Uart(net)
    joined(net)
    node = next(iter(net.nodes.values()))
    net.edge_tx(edge_frame(node.address, bytes(MetricsProbePayload(edge_tx_ts_us=7).to_bytes())))
    net.run(net.schedule.n_cells * 3)
    replies = [f for _, f in uart.of(EdgeEvent.NODE_DATA) if f.payload[0] == 0x9C]
    assert len(replies) == 1
    probe = MetricsProbePayload().from_bytes(replies[0].payload)
    assert probe.edge_tx_ts_us == 7
    assert 0 < probe.gw_tx_enqueued_asn <= probe.node_rx_asn < probe.gw_rx_asn
    assert (probe.gw_tx_count, probe.node_rx_count, probe.node_tx_count, probe.gw_rx_count) == (
        1,
        1,
        1,
        1,
    )
    assert net.schedule.cells[probe.node_rx_asn % net.schedule.n_cells].type == "D"
    assert probe.gw_rx_asn % net.schedule.n_cells == node.cell


def test_nodes_only_send_data_in_their_own_cell():
    trace = Trace(keep=True)
    net = Network.for_nodes(10, seed=3, trace=trace)
    net.run(net.slots_for(5))
    cells = {}
    for event in trace.events:
        if event["ev"] == "state" and event["to"] == "JOINED":
            cells[event["node"]] = event["cell"]
        if event["ev"] == "tx" and event["kind"] == "U":
            assert event["cell"] == cells[event["node"]]


def test_status_and_probe_replies_leave_in_queue_order():
    app = SyntheticApp(period_slots=10)
    app._next_due = 5
    app._pending.append((3, b"probe-3"))
    app._pending.append((8, b"probe-8"))
    out = []
    while (payload := app.next_uplink(9)) is not None:
        out.append(payload)
    assert out == [b"probe-3", app.payload, b"probe-8"]


def test_an_uplink_paired_with_a_next_proto_is_sent_under_it():
    class TwoProtoApp(SyntheticApp):
        def next_uplink(self, asn):
            if self._pending:
                return self._pending.popleft()

    net = Network("tiny", links=PerfectLinks())
    app = TwoProtoApp(next_proto=NextProto.DOTBOT_APP)
    net.add_node(app, start_asn=0)
    uart = Uart(net)
    joined(net)
    app._pending.extend([b"app", (b"internal", NextProto.MARI_INTERNAL)])
    net.run(net.schedule.n_cells * 3)
    sent = [(f.payload, f.header.next_proto) for _, f in uart.of(EdgeEvent.NODE_DATA)]
    assert sent == [(b"app", NextProto.DOTBOT_APP), (b"internal", NextProto.MARI_INTERNAL)]


def test_uplinks_made_before_joining_are_dropped():
    net = Network("tiny", links=PerfectLinks())
    app = SyntheticApp(period_slots=1)
    net.add_node(app, start_asn=0)
    uart = Uart(net)
    joined(net)
    net.run(net.schedule.n_cells)
    # one status per slot was produced while scanning; none of it is delivered
    assert len(uart.of(EdgeEvent.NODE_DATA)) <= net.schedule.n_cells


def test_a_rejoin_takes_the_first_free_cell_even_while_still_held():
    """Firmware behavior: the gateway can hold one node in two cells, then time one out.

    The stale cell then raises NODE_LEFT for a node that is still joined.
    """
    net = perfect_network(0)
    gw = net.gateway
    first, second = net.schedule.indices("U")[:2]
    assert gw.assign_cell(0xA, 0) == (first, True)
    assert gw.assign_cell(0xB, 0) == (second, True)
    gw.cells[first][0] = 0  # 0xA timed out
    assert gw.assign_cell(0xB, 10) == (first, True)
    assert [c for c, (node, _) in gw.cells.items() if node == 0xB] == [first, second]


def test_a_halted_gateway_goes_silent_and_a_reset_restarts_it_empty_at_asn_0():
    net = perfect_network(3)
    uart = Uart(net)
    net.run(net.slots_for(2))
    assert net.joined_count == 3
    net.halt_gateway()
    halted_at = net.asn
    net.run(net.slots_for(3))
    assert not [asn for asn, _ in uart.of(EdgeEvent.GATEWAY_INFO) if asn >= halted_at]
    assert not any(node.state == NodeState.JOINED for node in net.nodes.values())
    assert not uart.of(EdgeEvent.NODE_LEFT)  # a halted gateway reports nothing

    net.reset_gateway()
    reset_at = net.asn
    net.run(net.slots_for(2))
    infos = [info for asn, info in uart.of(EdgeEvent.GATEWAY_INFO) if asn >= reset_at]
    assert infos[0].asn == 0
    assert len({address for asn, address in uart.of(EdgeEvent.NODE_JOINED) if asn >= reset_at}) == 3
    assert net.joined_count == 3
