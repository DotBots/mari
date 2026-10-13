import io
import math
import os

import pytest

from marilib.model import EdgeEvent
from marilib.sim.network import Network
from marilib.sim.node import NodeState
from marilib.sim.trace import Trace

from .conftest import LOSSY, Uart

# The plan's full check is 1000 seeds; the default keeps `pytest` quick.
INVARIANT_RUNS = int(os.environ.get("MARI_SIM_INVARIANT_RUNS", "8"))


def run_checked(seed: int, n: int = 102, seconds: float = 8.0):
    net = Network.for_nodes(n, seed=seed, links=LOSSY)
    uart = Uart(net)
    schedule = net.schedule
    for _ in range(net.slots_for(seconds)):
        index = net.asn % schedule.n_cells
        if schedule.cells[index].type == "U":
            owners = net._owners.get(index, [])
            assert all(o.state == NodeState.JOINED for o in owners)
            holder = net.gateway.cells[index][0]
            assert sum(o.address == holder for o in owners) <= 1
        net.step()
        assert net.joined_count <= schedule.max_nodes
    return net, uart


@pytest.mark.parametrize("seed", range(INVARIANT_RUNS))
def test_no_shared_cells_and_no_data_before_join(seed):
    # Not "no data after NODE_LEFT": the firmware can emit NODE_LEFT for a node
    # it still holds in a second cell, see test_mac.py.
    _, uart = run_checked(seed)
    joined = set()
    for _, event, body in uart.events:
        if event == EdgeEvent.NODE_JOINED:
            joined.add(body)
        elif event == EdgeEvent.NODE_DATA:
            assert body.header.source in joined


def test_pdr_does_not_depend_on_the_node_count():
    rates = {}
    for n in (5, 100):
        net = Network.for_nodes(n, seed=11, links=LOSSY, status_period_s=0.05)
        net.run(net.slots_for(40 if n == 5 else 8))
        sent = sum(x.uplink.sent for x in net.nodes.values())
        received = sum(x.uplink.received for x in net.nodes.values())
        rates[n] = (received / sent, sent)
    (p_small, n_small), (p_large, n_large) = rates[5], rates[100]
    pooled = (p_small * n_small + p_large * n_large) / (n_small + n_large)
    sigma = math.sqrt(pooled * (1 - pooled) * (1 / n_small + 1 / n_large))
    assert abs(p_small - p_large) < 4 * sigma, rates


def trace_of(seed: int) -> str:
    stream = io.StringIO()
    net = Network.for_nodes(30, seed=seed, trace=Trace(stream))
    net.run(net.slots_for(3))
    return stream.getvalue()


def test_same_seed_same_trace():
    assert trace_of(5) == trace_of(5)


def test_different_seed_different_trace():
    assert trace_of(5) != trace_of(6)


def test_trace_lines_carry_asn_and_event():
    first = trace_of(1).splitlines()[0]
    assert first.startswith('{"asn": ')
    assert '"ev": ' in first
