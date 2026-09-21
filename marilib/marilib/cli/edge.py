import time

import click
from marilib.logger import MetricsLogger
from marilib.mari_protocol import Frame, MARI_BROADCAST_ADDRESS, DefaultPayload
from marilib.model import EdgeEvent, MariNode
from marilib.communication_adapter import SerialAdapter, MQTTAdapter, mqtt_options_from_env
from marilib.serial_uart import get_default_port
from marilib.tui_edge import MarilibTUIEdge
from marilib.marilib_edge import MarilibEdge


def on_event(event: EdgeEvent, event_data: MariNode | Frame):
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
    "--mqtt-url",
    "-m",
    type=str,
    default=None,
    help="MQTT broker to use (default: None, no cloud). Credentials come from MARI_MQTT_USER / MARI_MQTT_PASS; MARI_MQTT_INSECURE=1 skips the broker certificate check.",
)
@click.option(
    "--metrics-probe-interval",
    "-i",
    type=float,
    default=0,
    help="How often to send a metrics probe in seconds (default: 0, no metrics)",
)
@click.option(
    "--log-dir",
    default="logs",
    show_default=True,
    help="Directory to save metric log files.",
    type=click.Path(),
)
def main(port: str | None, mqtt_url: str, metrics_probe_interval: float, log_dir: str):
    """A basic example of using the MarilibEdge library."""

    logger = MetricsLogger(
        log_dir_base=log_dir, rotation_interval_minutes=1440, log_interval_seconds=1.0
    )

    mari = MarilibEdge(
        on_event,
        serial_interface=SerialAdapter(port),
        mqtt_interface=(
            MQTTAdapter.from_url(mqtt_url, is_edge=True, **mqtt_options_from_env()) if mqtt_url else None
        ),
        logger=logger,
        tui=MarilibTUIEdge(),
        main_file=__file__,
        metrics_probe_period=metrics_probe_interval,  # use a less frequent probe to interfere less with the main traffic
    )

    # Same as mari_edge_stats.py: keep the run folder self-describing, so a
    # measurement is identifiable from its own data rather than its directory
    # name. No load_percent here - this CLI has no load generator.
    mari.setup_params["metrics_probe_interval_s"] = metrics_probe_interval
    logger.log_setup_parameters(mari.setup_params)

    try:
        while True:
            mari.update()
            if not mari.uses_mqtt and mari.nodes:
                mari.send_frame(MARI_BROADCAST_ADDRESS, DefaultPayload().to_bytes())
            mari.render_tui()
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        mari.close_tui()
        mari.logger.close()


if __name__ == "__main__":
    main()
