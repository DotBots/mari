import sys
import threading
import time

import click
from marilib.logger import MetricsLogger
from marilib.mari_protocol import MARI_BROADCAST_ADDRESS, Frame, DefaultPayload
from marilib.marilib_edge import MarilibEdge
from marilib.metrics import LoadTester, probe_interval_for_slotframes
from marilib.model import EdgeEvent, GatewayInfo, MariNode, TestState
from marilib.serial_uart import get_default_port
from marilib.tui_edge import MarilibTUIEdge
from marilib.communication_adapter import mqtt_options_from_env
from marilib.communication_adapter import SerialAdapter, MQTTAdapter


def on_event(event: EdgeEvent, event_data: MariNode | Frame | GatewayInfo):
    """An event handler for the application."""
    pass


@click.command()
@click.option(
    "--port",
    "-p",
    type=str,
    default=get_default_port(),
    show_default=True,
    help="Serial port to use (e.g., /dev/ttyACM0)",
)
@click.option(
    "--mqtt-host",
    "-m",
    type=str,
    default="",
    show_default=True,
    help="MQTT broker to use (default: empty, no cloud)",
)
@click.option(
    "--load",
    type=int,
    default=0,
    show_default=True,
    help=(
        "Target downlink occupancy in percent (0-100), metrics probes INCLUDED. "
        "The filler rate is this target minus the probe rate, so --load 75 puts 75% "
        "on the link rather than 75% plus whatever the probes add. 0 disables filler."
    ),
)
@click.option(
    "--send-periodic",
    "-s",
    type=float,
    default=0,
    show_default=True,
    help="Send periodic packet every N seconds (0 = disabled)",
)
@click.option(
    "--metrics-probe-interval",
    "-i",
    type=float,
    default=None,
    help=(
        "Seconds between probes to the same node, overriding --probe-every. "
        "Wall-clock rather than slotframes: use it to reproduce a fixed "
        "cadence across schedules, e.g. -i 1 for the 2025 campaign's setting."
    ),
)
@click.option(
    "--probe-every",
    "-k",
    type=int,
    default=20,
    show_default=True,
    help=(
        "Probe each node once every K slotframes. The slotframe is the "
        "network's own clock, so one K samples every schedule at the same rate "
        "relative to its own dynamics and costs a near-constant ~4.6/K of "
        "downlink capacity on any of them (~23% at K=20). Overridden by -i."
    ),
)
@click.option(
    "--log-dir",
    default="logs",
    show_default=True,
    help="Directory to save metric log files.",
    type=click.Path(),
)
def main(
    port: str | None,
    mqtt_host: str,
    load: int,
    send_periodic: float,
    probe_every: int,
    metrics_probe_interval: float | None,
    log_dir: str,
):
    if not (0 <= load <= 100):
        sys.stderr.write("Error: --load must be between 0 and 100.\n")
        return

    # -k is the default knob, in the network's own clock; -i overrides it with
    # wall-clock seconds. Two knobs for one setting, high level and low.
    test_state = TestState(load=load, probe_every=0 if metrics_probe_interval else probe_every)

    logger = MetricsLogger(log_dir_base=log_dir, rotation_interval_minutes=1440)

    mari = MarilibEdge(
        on_event,
        serial_interface=SerialAdapter(port),
        mqtt_interface=(
            MQTTAdapter.from_url(mqtt_host, is_edge=True, **mqtt_options_from_env())
            if mqtt_host
            else None
        ),
        logger=logger,
        main_file=__file__,
        tui=MarilibTUIEdge(test_state=test_state),
        # Placeholder cadence: the real one is derived below, once the gateway
        # has reported its schedule. A zero here would leave the tester thread
        # unstarted, and set_interval cannot start it afterwards.
        metrics_probe_period=metrics_probe_interval or 10.0,
    )

    # Record the knobs that define the scenario, so a run folder is
    # self-describing and does not depend on how its directory was named.
    # metrics_probe_interval_s is filled in once the cadence is known.
    mari.setup_params.update(
        {
            "load_percent": load,
            "probe_every_slotframes": probe_every if metrics_probe_interval is None else "",
            "send_periodic_s": send_periodic,
        }
    )
    if metrics_probe_interval is not None:
        mari.setup_params["metrics_probe_interval_s"] = round(metrics_probe_interval, 3)
        test_state.probe_interval = metrics_probe_interval
    logger.log_setup_parameters(mari.setup_params)

    stop_event = threading.Event()

    load_tester = LoadTester(
        mari, test_state, stop_event, probe_interval=metrics_probe_interval or 0.0
    )
    if load > 0:
        load_tester.start()

    try:
        if send_periodic > 0:
            normal_traffic_interval = send_periodic
            last_normal_send_time = 0

        while not stop_event.is_set():
            current_time = time.monotonic()

            mari.update()

            # Derive the probe cadence from the gateway's schedule the moment it
            # is known. Done here rather than by blocking at startup: GATEWAY_INFO
            # arrives asynchronously over serial and can take longer than any
            # deadline worth failing a run over. Until then the tester runs at the
            # placeholder rate, which costs nothing - it skips every cycle while no
            # node has joined, and a node cannot join a gateway that has not
            # beaconed its schedule.
            # Downlink capacity, for the TUI's probe-share readout. Set here
            # rather than only in LoadTester: at --load 0 the load tester never
            # starts, and the probe budget is exactly what you want to see in
            # that case.
            test_state.rate = int(mari.get_max_downlink_rate())

            if metrics_probe_interval is None:
                derived = probe_interval_for_slotframes(mari, probe_every)
                if derived is not None:
                    metrics_probe_interval = derived
                    mari.metrics_tester.set_interval(derived)
                    load_tester.probe_interval = derived
                    test_state.probe_interval = derived
                    mari.setup_params["metrics_probe_interval_s"] = round(derived, 3)
                    logger.log_setup_parameters(mari.setup_params)

            mari.render_tui()

            if (
                send_periodic > 0
                and current_time - last_normal_send_time >= normal_traffic_interval
            ):
                if mari.nodes:
                    mari.send_frame(MARI_BROADCAST_ADDRESS, DefaultPayload().to_bytes())
                last_normal_send_time = current_time

            time.sleep(1)

    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        mari.metrics_test_disable()
        if load_tester.is_alive():
            load_tester.join()
        mari.close_tui()
        mari.logger.close()


if __name__ == "__main__":
    main()
