import csv
import json

import pytest
from click.testing import CliRunner
from rich.console import Console

from marilib.marilib_edge import MarilibEdge
from marilib.sim import Network, SimulatedGatewayAdapter
from marilib.sim.cli import main
from marilib.sim.events import Event, check_events, parse_events
from marilib.sim.links import PerfectLinks
from marilib.sim.runner import Session, Storms
from marilib.tui_edge import MarilibTUIEdge

EVENTS = """
[[event]]
at = 10.0
action = "remove"
node = 3

[[event]]
at = 15.0
action = "add"
node = 3

[[event]]
at = 20.0
action = "move"
node = 5
distance = 12.0
"""


def run_cli(*args, events: str | None = None, tmp_path=None):
    if events is not None:
        path = tmp_path / "events.toml"
        path.write_text(events)
        args = (*args, "--events", str(path))
    return CliRunner().invoke(main, ["run", *args])


def test_fast_run_reports_the_scripted_leave_rejoin_and_move(tmp_path):
    result = run_cli(
        "--nodes",
        "20",
        "--duration",
        "30",
        "--pacing",
        "fast",
        "--summary",
        events=EVENTS,
        tmp_path=tmp_path,
    )
    assert result.exit_code == 0, result.output
    assert "20/20 nodes joined" in result.output
    assert "remove node 3: gateway saw it leave at" in result.output
    assert "add node 3: joined at" in result.output
    assert "move node 5 to 12.0 m: uplink PDR" in result.output
    node_3 = next(line.split() for line in result.output.splitlines() if line.split()[:1] == ["3"])
    assert node_3[2:4] == ["2", "1"]  # joins, left


def test_same_seed_and_events_give_the_same_summary(tmp_path):
    args = ("--nodes", "12", "--duration", "20", "--pacing", "fast", "--summary", "--seed", "7")
    first = run_cli(*args, events=EVENTS, tmp_path=tmp_path)
    second = run_cli(*args, events=EVENTS, tmp_path=tmp_path)
    # drop the wall-clock line
    assert first.output.split("\n", 1)[1] == second.output.split("\n", 1)[1]


def test_a_move_reaches_the_link_model_as_the_nodes_position():
    class Recording(PerfectLinks):
        seen: dict[float, set] = {}

        def pdr(self, link):
            if link.node == 0x1005:
                self.seen.setdefault(link.node_pos[0], set()).add(link.asn)
            return 1.0

    net = Network.for_nodes(20, seed=1, links=Recording())
    session = Session(net, events=[Event(20.0, "move", 5, (12.0, 0.0))])
    session.run(30)
    moved_at = session.fired[0].asn
    assert max(net.links.seen[12.0]) > moved_at
    assert max(asn for x, asns in net.links.seen.items() if x != 12.0 for asn in asns) < moved_at


def test_fast_runs_measure_rtt_from_gateway_stamps():
    net = Network.for_nodes(5, seed=2)
    session = Session(net, probe_interval_s=0.5)
    session.run(10)
    assert all(session.recorder.rtt_slots[address] for address in net.nodes)
    assert all(
        0 < s < 2 * net.schedule.n_cells for v in session.recorder.rtt_slots.values() for s in v
    )


@pytest.mark.parametrize(
    "args, error",
    [
        (("--pacing", "fast"), "--pacing fast needs --duration"),
        (("--schedule", "nope", "--duration", "1", "--pacing", "fast"), "no mari schedule 'nope'"),
        (("--duration", "1", "--pacing", "fast", "--load", "50"), "need --pacing realtime"),
        (("--duration", "1", "--pacing", "fast", "--log-dir", "x"), "need --pacing realtime"),
        (("--duration", "1", "--pacing", "fast", "-i", "1", "-k", "20"), "give -i or -k, not both"),
    ],
)
def test_bad_arguments_are_usage_errors(args, error):
    result = run_cli(*args)
    assert result.exit_code == 2
    assert error in result.output


def test_events_on_a_node_that_is_off_are_rejected(tmp_path):
    result = run_cli(
        "--nodes", "2", "--duration", "1", "--pacing", "fast", events=EVENTS, tmp_path=tmp_path
    )
    assert result.exit_code == 2
    assert "node 3 is not on" in result.output


@pytest.mark.parametrize(
    "entry, error",
    [
        ({"at": 1, "action": "teleport", "node": 1}, "action must be one of"),
        ({"at": 1, "action": "move", "node": 1}, "'move' needs"),
        ({"at": 1, "action": "remove", "node": 1, "distance": 3}, "takes no position"),
        ({"at": 1, "action": "add"}, "missing 'node'"),
        ({"at": 1, "action": "add", "node": 1, "where": 2}, "unknown key(s) where"),
        ({"at": 1, "action": "halt", "node": 1}, "takes only 'at'"),
    ],
)
def test_malformed_events_are_rejected(entry, error):
    with pytest.raises(ValueError, match=error.replace("(", r"\(").replace(")", r"\)")):
        parse_events({"event": [entry]})


def test_events_are_sorted_and_checked_in_time_order():
    events = parse_events(
        {
            "event": [
                {"at": 5, "action": "add", "node": 2},
                {"at": 1, "action": "remove", "node": 2},
            ]
        }
    )
    assert [e.action for e in events] == ["remove", "add"]
    check_events(events, n_nodes=2)
    with pytest.raises(ValueError, match="already on"):
        check_events(events[1:], n_nodes=2)


def test_a_second_halt_before_a_reset_is_rejected():
    events = parse_events({"event": [{"at": 1, "action": "halt"}, {"at": 2, "action": "halt"}]})
    with pytest.raises(ValueError, match="already halted"):
        check_events(events, n_nodes=1)


def test_storms_reform_the_whole_network_each_time():
    net = Network.for_nodes(10, seed=3)
    session = Session(net, storms=Storms(cycles=3))
    session.form(quiet_s=3, settle_s=90)
    assert net.joined_count == 10
    session.run()
    assert [storm.nodes for storm in session.storm_results] == [10, 10, 10]
    resets = [storm.reset_asn for storm in session.storm_results]
    assert all(b - a >= net.slots_for(6 + 3) for a, b in zip(resets, resets[1:]))


def test_fast_storms_are_summarized_and_reproducible():
    args = ("--nodes", "10", "--pacing", "fast", "--storms", "2", "--summary", "--seed", "4")
    first, second = run_cli(*args), run_cli(*args)
    assert first.exit_code == 0, first.output
    assert "storm  reset_s nodes joins  span_s" in first.output
    assert first.output.split("\n", 1)[1] == second.output.split("\n", 1)[1]


def _read_csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def test_a_realtime_run_writes_the_bench_logs(tmp_path):
    result = run_cli(
        "--nodes",
        "3",
        "--duration",
        "4",
        "--headless",
        "--log-dir",
        str(tmp_path),
        "-k",
        "20",
        "--load",
        "50",
    )
    assert result.exit_code == 0, result.output
    (run_dir,) = tmp_path.glob("run_*")
    joins = [
        row for row in _read_csv(run_dir / "log_events.csv") if row["event_name"] == "NODE_JOINED"
    ]
    assert len({row["node_address"] for row in joins}) == 3
    (nodes_csv,) = run_dir.glob("node_metrics_*.csv")
    assert {row["node_address"] for row in _read_csv(nodes_csv)} == {
        f"0x{0x1000 + i:016X}" for i in (1, 2, 3)
    }
    with open(run_dir / "metrics_setup.csv", newline="") as f:
        setup = dict(row for row in csv.reader(f))
    assert setup["load_percent"] == "50"
    assert setup["probe_every_slotframes"] == "20"
    assert setup["schedule_name"] == "tiny"
    assert setup["sim_seed"] == "0"


def test_realtime_storms_leave_run_meta_beside_the_logs(tmp_path):
    result = run_cli(
        "--nodes",
        "3",
        "--storms",
        "1",
        "--hold",
        "0.5",
        "--quiet",
        "0.5",
        "--headless",
        "--log-dir",
        str(tmp_path),
    )
    assert result.exit_code == 0, result.output
    (run_dir,) = tmp_path.glob("run_*")
    meta = json.loads((run_dir / "run_meta.json").read_text())
    assert (meta["cycles"], meta["fleet"], meta["hold_s"]) == (1, 3, 0.5)
    joins = [
        row for row in _read_csv(run_dir / "log_events.csv") if row["event_name"] == "NODE_JOINED"
    ]
    assert (
        len({row["node_address"] for row in joins}) == 3
    )  # the storm's rejoins, not the formation before it


def test_mari_edge_tui_renders_the_simulated_gateway():
    net = Network.for_nodes(4, seed=1)
    edge = MarilibEdge(
        lambda *_: None, serial_interface=SimulatedGatewayAdapter(net, realtime=False)
    )
    try:
        net.run(net.slots_for(2))
        edge.update()
        tui, console = MarilibTUIEdge(), Console(width=200, record=True)
        console.print(tui.create_header_panel(edge), tui.create_nodes_panel(edge))
        text = console.export_text()
        assert "simulator:tiny" in text
        assert "0x0000000000001004" in text
    finally:
        edge.close()


def test_verify_is_a_subcommand():
    result = CliRunner().invoke(main, ["verify", "--help"])
    assert result.exit_code == 0
    assert "smallest schedule" in result.output
