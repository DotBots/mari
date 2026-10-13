import math

import pytest

from marilib.sim.links import DOWN, UP, Link, StaticLinks
from marilib.sim.network import Network
from marilib.sim.runner import Session


def link(node: int, direction: str) -> Link:
    return Link(node, 1, direction, 0, 0, (2.0, 0.0, 0.0), (0.0, 0.0, 0.0))


def test_a_node_override_wins_per_direction():
    links = StaticLinks(0.98, 0.97, nodes={7: (0.5, None)})
    assert links.pdr(link(7, UP)) == 0.5
    assert links.pdr(link(7, DOWN)) == 0.97
    assert links.pdr(link(8, UP)) == 0.98


def test_a_pdr_outside_the_unit_interval_is_refused():
    with pytest.raises(ValueError, match="node 0x7"):
        StaticLinks(nodes={7: (1.2, None)})


def test_the_file_is_the_world_file_in_percent(tmp_path):
    path = tmp_path / "links.toml"
    path.write_text(
        "[network]\nuplink_pdr = 98\ndownlink_pdr = 96\n\n"
        '[[dotbots]]\naddress = "BADCAFE111111111"\nuplink_pdr = 91\n\n'
        '[[dotbots]]\naddress = "DEADBEEF22222222"\n'
    )
    links = StaticLinks.from_file(path)
    assert (links.uplink, links.downlink) == (0.98, 0.96)
    assert links.addresses == [0xBADCAFE111111111, 0xDEADBEEF22222222]
    assert links.pdr(link(0xBADCAFE111111111, UP)) == pytest.approx(0.91)
    assert links.pdr(link(0xDEADBEEF22222222, DOWN)) == pytest.approx(0.96)


def test_pdr_alone_sets_both_directions(tmp_path):
    path = tmp_path / "links.toml"
    path.write_text("[network]\npdr = 90\n")
    links = StaticLinks.from_file(path)
    assert (links.uplink, links.downlink) == (0.9, 0.9)


@pytest.mark.parametrize(
    "text, message",
    [
        ("[network]\nuplink_pdr = 120\n", "network.uplink_pdr"),
        ("[[dotbots]]\nuplink_pdr = 90\n", r"dotbots\[0\]: no address"),
        ('[[dotbots]]\naddress = "A1"\n\n[[dotbots]]\naddress = "a1"\n', "listed twice"),
        ('[[dotbots]]\naddress = "A1"\ndownlink_pdr = "high"\n', r"dotbots\[0\].downlink_pdr"),
    ],
)
def test_a_bad_file_names_the_key(tmp_path, text, message):
    path = tmp_path / "links.toml"
    path.write_text(text)
    with pytest.raises(ValueError, match=message):
        StaticLinks.from_file(path)


def test_the_engine_delivers_each_nodes_configured_pdr():
    weak, strong = 0x1001, 0x1002
    links = StaticLinks(1.0, 1.0, nodes={weak: (0.7, 0.8)})
    net = Network.for_nodes(2, seed=4, links=links, status_period_s=0.01)
    Session(net, probe_interval_s=0.05).run(120)  # probes give each node downlink traffic
    for address, (up, down) in [(weak, (0.7, 0.8)), (strong, (1.0, 1.0))]:
        node = net.nodes[address]
        for stats, expected in [(node.uplink, up), (node.downlink, down)]:
            if expected == 1.0:
                assert stats.received == stats.sent
                continue
            sigma = math.sqrt(expected * (1 - expected) / stats.sent)
            assert abs(stats.received / stats.sent - expected) < 4 * sigma, (hex(address), stats)


def test_the_first_nodes_take_the_given_addresses():
    net = Network.for_nodes(3, addresses=[0xBADCAFE111111111])
    assert list(net.nodes) == [0xBADCAFE111111111, 0x1002, 0x1003]
