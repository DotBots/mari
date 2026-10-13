"""A slot-level model of one mari gateway and its nodes, behind marilib's gateway adapter.

net = Network.for_nodes(20, seed=1)
edge = MarilibEdge(on_event, serial_interface=SimulatedGatewayAdapter(net))
"""

from marilib.sim.adapter import SimulatedGatewayAdapter
from marilib.sim.app import NodeApp, RxInfo, SyntheticApp
from marilib.sim.links import Link, LinkModel, PerfectLinks, StaticLinks
from marilib.sim.firmware import load_firmware
from marilib.sim.network import Network
from marilib.sim.trace import Trace

__all__ = [
    "Link",
    "LinkModel",
    "Network",
    "NodeApp",
    "PerfectLinks",
    "RxInfo",
    "SimulatedGatewayAdapter",
    "StaticLinks",
    "SyntheticApp",
    "Trace",
    "load_firmware",
]
