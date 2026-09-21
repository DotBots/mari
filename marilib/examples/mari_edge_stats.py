import sys
import threading
import time

import click
from marilib.logger import MetricsLogger
from marilib.mari_protocol import MARI_BROADCAST_ADDRESS, Frame, DefaultPayload, DefaultPayloadType
from marilib.marilib_edge import MarilibEdge
from marilib.model import SCHEDULES, EdgeEvent, GatewayInfo, MariNode, TestState
from marilib.serial_uart import get_default_port
from marilib.tui_edge import MarilibTUIEdge
from marilib.communication_adapter import mqtt_options_from_env
from marilib.communication_adapter import SerialAdapter, MQTTAdapter


def probe_interval_for_slotframes(mari: MarilibEdge, every: int) -> float | None:
    """Seconds between probes to a node, for one probe every `every` slotframes.

    The slotframe is the network's own clock - latency, queue depth and the
    probe timeout are all measured in it - so a cadence expressed in slotframes
    samples every schedule at the same rate relative to its own dynamics, which
    is what makes latency comparable across them.

    It also holds the probe's share of downlink nearly constant without being
    asked to. That share is N / (every * d_down), and N/d_down is 5.00, 4.40,
    4.13, 4.64 for tiny, medium, big and huge - the schedules give each node an
    uplink slot and scale downlink alongside, so the ratio barely moves. The
    share is therefore about 4.6/every on any of them: ~23% at every=20, ~15%
    at 30. Below about 15 the probe stream starts crowding out the traffic it
    is supposed to be measuring.

    Returns None until the gateway has reported a schedule we know.
    """
    schedule = SCHEDULES.get(mari.gateway.info.schedule_id)
    if schedule is None or every <= 0:
        return None
    return every * float(schedule["sf_duration"]) / 1000.0


class LoadTester(threading.Thread):
    """Generates filler downlink traffic up to a target link occupancy.

    `--load` is the share of the gateway's downlink capacity the link should
    carry in total, metrics probes included. Probes are unicast downlink
    packets competing for the same D slots as the filler, so the filler rate
    is the target minus the probe rate. Without that subtraction, asking for
    75% on the huge schedule puts 90% on the link, which measures the knee
    rather than a loaded network.
    """

    def __init__(
        self,
        mari: MarilibEdge,
        test_state: TestState,
        stop_event: threading.Event,
        probe_interval: float = 0.0,
    ):
        super().__init__(daemon=True)
        self.mari = mari
        self.test_state = test_state
        self._stop_event = stop_event
        self.probe_interval = probe_interval
        self._warned_over_budget = False

    def run(self):
        while not self._stop_event.is_set():
            delay = self.compute_delay()
            if delay is None:
                # Gateway schedule not known yet, or the probe stream already
                # fills the budget. Re-check shortly: both can change as the
                # gateway reports in and as nodes join or leave.
                self._stop_event.wait(0.1)
                continue

            with self.mari.lock:
                nodes_exist = bool(self.mari.gateway.nodes)

            if nodes_exist:
                self.mari.send_frame(
                    MARI_BROADCAST_ADDRESS,
                    DefaultPayload(type_=DefaultPayloadType.METRICS_LOAD).with_filler_bytes(180),
                )
            self._stop_event.wait(delay)

    def probe_rate(self) -> float:
        """Nominal probe packets/s: one per node per probe interval.

        Nominal, not measured: a probe that times out is retransmitted (up to
        MAX_PROBE_RETRIES), so under heavy loss the real probe rate is higher
        and the link carries more than the requested share. That regime is
        visible in the log as a non-zero pending-probe count and an effective
        latency pinned at two slotframes.
        """
        if self.probe_interval <= 0:
            return 0.0
        with self.mari.lock:
            node_count = len(self.mari.gateway.nodes)
        return node_count / self.probe_interval

    def compute_delay(self) -> float | None:
        """Seconds between filler packets, or None if none should be sent.

        Recomputed per packet rather than latched at startup: the probe stream
        scales with the node count, so a rate fixed during formation would
        overshoot the target once the network filled up.
        """
        if self.test_state.load == 0:
            return None
        max_rate = self.mari.get_max_downlink_rate()
        if max_rate == 0:
            return None  # gateway schedule not reported yet

        self.test_state.rate = int(max_rate)
        target_pps = max_rate * (self.test_state.load / 100.0)
        filler_pps = target_pps - self.probe_rate()

        if filler_pps <= 0:
            if not self._warned_over_budget:
                self._warned_over_budget = True
                sys.stderr.write(
                    f"Warning: metrics probes alone offer "
                    f"{self.probe_rate() / max_rate:.0%} of downlink capacity, at or above the "
                    f"requested {self.test_state.load}%. Sending no filler traffic; either raise "
                    f"--load or raise --metrics-probe-interval.\n"
                )
            return None

        self._warned_over_budget = False
        return 1.0 / filler_pps


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
