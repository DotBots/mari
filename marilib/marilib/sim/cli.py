"""mari-sim: run a simulated mari network, live under the mari-edge TUI or as fast as it goes."""

from __future__ import annotations

import json
import threading
import time

import click

from marilib.model import SCHEDULES
from marilib.sim.events import check_events, load_events
from marilib.sim.links import StaticLinks
from marilib.sim.network import Network
from marilib.sim.runner import Session, Storms
from marilib.sim.verify import main as verify


@click.group()
def main():
    """Simulate a mari gateway and its nodes, slot by slot."""


@main.command()
@click.option(
    "--nodes",
    "-n",
    "n_nodes",
    type=click.IntRange(min=0),
    default=None,
    help="Nodes powered at start. [default: the robots in --links, else 10]",
)
@click.option(
    "--schedule",
    "-s",
    default="auto",
    show_default=True,
    help="Schedule name, or auto for the smallest one that fits --nodes.",
)
@click.option("--seed", default=0, show_default=True, help="Same seed and events, same run.")
@click.option(
    "--pdr",
    type=click.FloatRange(0, 100),
    default=100.0,
    show_default=True,
    help="Uplink and downlink PDR in percent, for every link --links does not set.",
)
@click.option(
    "--links",
    "links_path",
    type=click.Path(exists=True, dir_okay=False),
    help="Per-robot PDRs, in the PyDotBot simulator's world-file format (see marilib.sim.links).",
)
@click.option(
    "--duration",
    type=float,
    default=None,
    help="Simulated seconds. Default: until the storms are done, or until Ctrl-C.",
)
@click.option(
    "--pacing",
    type=click.Choice(["realtime", "fast"]),
    default="realtime",
    show_default=True,
    help="realtime runs the stock MarilibEdge on the wall clock; fast runs headless as fast as it can.",
)
@click.option(
    "--headless", is_flag=True, help="With realtime pacing, run without the mari-edge TUI."
)
@click.option(
    "--metrics-probe-interval",
    "-i",
    type=float,
    default=None,
    help="Seconds between probes to the same node; 0 disables RTT measurement. [default: 1.0]",
)
@click.option(
    "--probe-every",
    "-k",
    type=int,
    default=None,
    help="Probe each node once every K slotframes, as mari_edge_stats.py does. Exclusive with -i.",
)
@click.option(
    "--load",
    type=click.IntRange(0, 100),
    default=0,
    show_default=True,
    help="Target downlink occupancy in percent, probes included, as mari_edge_stats.py --load. Realtime only.",
)
@click.option(
    "--log-dir",
    type=click.Path(file_okay=False),
    default=None,
    help="Write the MetricsLogger run_* folder here, as mari-edge does. Realtime only.",
)
@click.option(
    "--storms",
    type=click.IntRange(min=1),
    default=None,
    help="Join storms to run, as storm_run.py does: halt the gateway, reset it, let the network re-form.",
)
@click.option(
    "--hold", default=6.0, show_default=True, help="Seconds each storm holds the gateway halted."
)
@click.option(
    "--quiet",
    default=3.0,
    show_default=True,
    help="A storm is over once no join has arrived for this long.",
)
@click.option(
    "--settle",
    default=90.0,
    show_default=True,
    help="Seconds after a reset at which a storm is over regardless.",
)
@click.option(
    "--events",
    "events_path",
    type=click.Path(exists=True, dir_okay=False),
    help="TOML file of node add / remove / move and gateway halt / reset events (see marilib.sim.events).",
)
@click.option(
    "--trace", "trace_file", type=click.File("w"), help="Write the JSON-lines event trace here."
)
@click.option(
    "--summary", is_flag=True, help="Print per-node PDR, RTT and event outcomes at the end."
)
def run(
    n_nodes, schedule, seed, pdr, links_path, duration, pacing, headless, metrics_probe_interval, probe_every,
    load, log_dir, storms, hold, quiet, settle, events_path, trace_file, summary,
):  # fmt: skip
    """Run a network of --nodes stock nodes around one gateway."""
    fast = pacing == "fast"
    if fast and duration is None and storms is None:
        raise click.UsageError("--pacing fast needs --duration or --storms")
    if fast and (load or log_dir):
        raise click.UsageError(
            "--load and --log-dir need --pacing realtime: they run in the stock MarilibEdge"
        )
    if metrics_probe_interval is not None and probe_every is not None:
        raise click.UsageError("give -i or -k, not both")
    if metrics_probe_interval is not None and metrics_probe_interval < 0:
        raise click.BadParameter("must be >= 0", param_hint="--metrics-probe-interval")
    if probe_every is not None and probe_every < 1:
        raise click.BadParameter("must be >= 1", param_hint="--probe-every")
    try:
        links = (
            StaticLinks.from_file(links_path) if links_path else StaticLinks(pdr / 100, pdr / 100)
        )
    except ValueError as error:
        raise click.BadParameter(str(error), param_hint="--links") from None
    if links_path and pdr != 100.0:
        raise click.UsageError(
            "give --pdr or --links, not both: the file's [network] sets the defaults"
        )
    addresses = links.addresses
    if n_nodes is None:
        n_nodes = len(addresses) or 10
    if n_nodes < len(addresses):
        raise click.BadParameter(f"--links lists {len(addresses)} robots", param_hint="--nodes")
    if events_path and addresses:
        raise click.UsageError(
            "--events numbers nodes from 1, so it cannot be combined with robots in --links yet"
        )
    try:
        events = load_events(events_path) if events_path else []
        check_events(events, n_nodes)
    except ValueError as error:
        raise click.BadParameter(str(error), param_hint="--events") from None
    try:
        net = Network.for_nodes(
            n_nodes,
            schedule=None if schedule == "auto" else schedule,
            seed=seed,
            links=links,
            addresses=addresses,
        )
    except KeyError as error:
        raise click.BadParameter(error.args[0], param_hint="--schedule") from None

    if probe_every is not None:
        probe_interval_s = probe_every * float(SCHEDULES[net.schedule.id]["sf_duration"]) / 1000.0
    else:
        probe_interval_s = 1.0 if metrics_probe_interval is None else metrics_probe_interval

    storm_plan = Storms(storms, hold, quiet, settle) if storms else None
    session = Session(
        net,
        events=events,
        probe_interval_s=probe_interval_s if fast else 0.0,
        trace_stream=trace_file,
        storms=storm_plan,
    )
    if storm_plan:
        session.form(quiet, settle)
    started = time.monotonic()
    if fast:
        session.run(duration)
    else:
        setup = {
            "load_percent": load,
            "probe_every_slotframes": probe_every if probe_every is not None else "",
            "metrics_probe_interval_s": round(probe_interval_s, 3),
            "send_periodic_s": 0,
            "sim_nodes": n_nodes,
            "sim_seed": seed,
            "sim_uplink_pdr": round(100 * links.uplink, 2),
            "sim_downlink_pdr": round(100 * links.downlink, 2),
            "sim_links_file": links_path or "",
        }
        run_dir = _run_with_edge(
            session, duration, probe_interval_s, tui=not headless, log_dir=log_dir, load=load,
            probe_every=probe_every or 0, setup=setup,
        )  # fmt: skip
        if run_dir and storm_plan:
            _write_run_meta(run_dir, n_nodes, net.schedule.name, load, storm_plan)
    wall = time.monotonic() - started
    session.recorder.close()

    click.echo(
        f"{session.seconds(net.asn):.1f} s simulated in {wall:.1f} s ({pacing}), "
        f"{net.joined_count}/{len(net.nodes)} nodes joined"
    )
    if net.realtime_lag_slots:
        click.echo(
            f"{net.realtime_lag_slots} slots started late against the wall clock, "
            f"the worst by {net.realtime_max_lag_s * 1000:.1f} ms"
        )
    if summary:
        click.echo("\n" + session.summary())


def _run_with_edge(
    session: Session,
    duration: float | None,
    probe_interval_s: float,
    *,
    tui: bool,
    log_dir: str | None,
    load: int,
    probe_every: int,
    setup: dict,
) -> str | None:
    """The stock MarilibEdge on the simulated UART, driven as mari_edge_stats.py drives it on the bench.

    The network runs on its own thread. Returns the logger's run folder, if any.
    """
    from marilib.logger import MetricsLogger
    from marilib.marilib_edge import MarilibEdge
    from marilib.metrics import LoadTester
    from marilib.model import TestState
    from marilib.sim.adapter import SimulatedGatewayAdapter
    from marilib.tui_edge import MarilibTUIEdge

    test_state = TestState(load=load, probe_every=probe_every, probe_interval=probe_interval_s)
    logger = (
        MetricsLogger(log_dir_base=log_dir, rotation_interval_minutes=1440) if log_dir else None
    )
    adapter = SimulatedGatewayAdapter(session.net, realtime=False)
    edge = MarilibEdge(
        lambda *_: None,
        serial_interface=adapter,
        logger=logger,
        tui=MarilibTUIEdge(test_state=test_state) if tui else None,
        main_file=__file__,
        metrics_probe_period=probe_interval_s,
    )
    edge.setup_params.update(setup)
    if logger:
        logger.log_setup_parameters(edge.setup_params)
    session.tap_uart()
    stop = threading.Event()
    load_tester = LoadTester(edge, test_state, stop, probe_interval=probe_interval_s)
    if load:
        load_tester.start()
    thread = threading.Thread(
        target=session.run,
        args=(duration,),
        kwargs={"realtime": True, "stop": stop},
        name="mari-sim",
        daemon=True,
    )
    thread.start()
    try:
        while thread.is_alive():
            edge.update()
            test_state.rate = int(edge.get_max_downlink_rate())
            edge.render_tui()
            thread.join(timeout=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        thread.join()
        if load_tester.is_alive():
            load_tester.join()
        edge.close_tui()
        edge.close()
        if logger:
            logger.close()
    return logger.log_dir if logger else None


def _write_run_meta(run_dir: str, fleet: int, schedule: str, load: int, storms: Storms) -> None:
    """The run_meta.json storm_run.py leaves beside a storm run, so the same readers find it."""
    meta = {
        "fleet": fleet,
        "expect": None,
        "cycles": storms.cycles,
        "schedule": schedule,
        "load_percent": load,
        "hold_s": storms.hold_s,
        "quiet_s": storms.quiet_s,
        "settle_s": storms.settle_s,
    }
    with open(f"{run_dir}/run_meta.json", "w") as f:
        json.dump(meta, f, indent=2)


main.add_command(verify, name="verify")


if __name__ == "__main__":
    main()
