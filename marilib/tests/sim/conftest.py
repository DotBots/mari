import pytest

from marilib.mari_protocol import Frame
from marilib.model import EdgeEvent, GatewayInfo, NodeInfoEdge
from marilib.sim.app import SyntheticApp
from marilib.sim.links import PerfectLinks, StaticLinks
from marilib.sim.network import Network

# The reference campaign's single-attempt PDR at 10 m, for tests that need losses
LOSSY = StaticLinks(0.943, 0.943)


class Uart:
    """Every event the simulated gateway put on its UART, decoded with marilib's classes."""

    def __init__(self, net: Network):
        self.net = net
        self.events: list[tuple[int, EdgeEvent, object]] = []
        net.on_uart = self.record

    def record(self, data: bytes) -> None:
        event = EdgeEvent(data[0])
        if event == EdgeEvent.GATEWAY_INFO:
            body = GatewayInfo().from_bytes(data[1:])
        elif event == EdgeEvent.NODE_DATA:
            body = Frame().from_bytes(data[1:])
        else:
            body = NodeInfoEdge().from_bytes(data[1:]).address
        self.events.append((self.net.asn, event, body))

    def of(self, event: EdgeEvent) -> list:
        return [(asn, body) for asn, e, body in self.events if e == event]


def perfect_network(n: int, schedule: str = "tiny", **kwargs) -> Network:
    net = Network(schedule, links=PerfectLinks(), **kwargs)
    for _ in range(n):
        net.add_node(SyntheticApp())
    return net


@pytest.fixture
def uart():
    return Uart
