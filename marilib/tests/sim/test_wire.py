import time

from marilib.marilib_edge import MarilibEdge
from marilib.mari_protocol import MARI_GATEWAY_INFO_VERSION, MARI_PROTOCOL_VERSION
from marilib.model import EdgeEvent, GatewayInfo
from marilib.protocol import PacketType
from marilib.sim.adapter import SimulatedGatewayAdapter
from marilib.sim.network import Network

from .conftest import LOSSY, Uart


def test_every_uart_event_parses_with_marilib():
    net = Network.for_nodes(12, seed=2, links=LOSSY)
    uart = Uart(net)  # raises on anything marilib cannot decode
    net.run(net.slots_for(6))
    kinds = {event for _, event, _ in uart.events}
    assert kinds == {
        EdgeEvent.GATEWAY_INFO,
        EdgeEvent.NODE_JOINED,
        EdgeEvent.NODE_DATA,
        EdgeEvent.NODE_KEEP_ALIVE,
    }
    for _, frame in uart.of(EdgeEvent.NODE_DATA):
        assert frame.header.version == MARI_PROTOCOL_VERSION
        assert frame.header.type_ == PacketType.DATA
        assert frame.header.destination == net.gateway.address


def test_gateway_info_once_per_slotframe():
    net = Network.for_nodes(3, seed=0)
    uart = Uart(net)
    net.run(net.schedule.n_cells * 5)
    infos = uart.of(EdgeEvent.GATEWAY_INFO)
    assert [asn for asn, _ in infos] == [i * net.schedule.n_cells for i in range(5)]
    info: GatewayInfo = infos[-1][1]
    assert info.version == MARI_GATEWAY_INFO_VERSION
    assert info.schedule_id == net.schedule.id
    assert info.schedule_name == net.schedule.name
    assert info.asn == infos[-1][0]


def test_schedule_usage_bitmap_reads_back_through_marilib():
    net = Network.for_nodes(3, seed=0)
    uart = Uart(net)
    net.run(net.slots_for(3))
    info = uart.of(EdgeEvent.GATEWAY_INFO)[-1][1]
    usage = info.repr_schedule_stats()
    assert len(usage) == net.schedule.n_cells
    assert usage[:3] == "111"  # beacons always go out


def test_stock_edge_sees_the_simulated_network():
    net = Network.for_nodes(8, seed=4)
    edge = MarilibEdge(
        lambda *_: None, serial_interface=SimulatedGatewayAdapter(net, realtime=False)
    )
    try:
        net.run(net.slots_for(5))
        assert len(edge.nodes) == 8
        assert edge.gateway.info.schedule_name == "tiny"
        assert edge.gateway.info.address == net.gateway.address
        assert edge.gateway.stats.received_count() > 0
    finally:
        edge.close()


def test_malformed_edge_input_is_dropped():
    net = Network.for_nodes(1, seed=0)
    net.edge_tx(bytes([EdgeEvent.GATEWAY_INFO, 0, 0]))
    net.edge_tx(bytes([EdgeEvent.NODE_DATA, 1, 2]))
    net.run(2)
    assert len(net.gateway.queue) == 0


def test_stock_edge_measures_live_rtt_and_pdr_in_realtime():
    net = Network.for_nodes(5, seed=1)
    edge = MarilibEdge(
        lambda *_: None,
        serial_interface=SimulatedGatewayAdapter(net, realtime=True),
        metrics_probe_period=0.3,
    )
    try:
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            time.sleep(0.2)
            if all(len(n.probe_stats) >= 2 for n in edge.nodes) and len(edge.nodes) == 5:
                break
        assert len(edge.nodes) == 5
        assert edge.gateway.stats_avg_latency_roundtrip_node_edge_ms() > 0
        assert 0 < edge.gateway.stats_avg_pdr_uplink_radio() <= 1
    finally:
        edge.close()
