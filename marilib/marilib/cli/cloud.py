import time

import click
from marilib.mari_protocol import MARI_BROADCAST_ADDRESS, MARI_NET_ID_DEFAULT, DefaultPayload, Frame
from marilib.marilib_cloud import MarilibCloud
from marilib.model import EdgeEvent, GatewayInfo, MariNode
from marilib.communication_adapter import MQTTAdapter, mqtt_options_from_env
from marilib.tui_cloud import MarilibTUICloud
from marilib.logger import MetricsLogger


def on_event(event: EdgeEvent, event_data: MariNode | Frame | GatewayInfo):
    """An event handler for the application."""
    pass


@click.command()
@click.option(
    "--mqtt-url",
    "-m",
    type=str,
    default="mqtt://localhost:1883",
    show_default=True,
    help="MQTT broker to use. Credentials come from MARI_MQTT_USER / MARI_MQTT_PASS; MARI_MQTT_INSECURE=1 skips the broker certificate check.",
)
@click.option(
    "--network-id",
    "-n",
    type=lambda x: int(x, 16),
    default=MARI_NET_ID_DEFAULT,
    help=f"Network ID to use [default: 0x{MARI_NET_ID_DEFAULT:04X}]",
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
    "--log-dir",
    default="logs",
    show_default=True,
    help="Directory to save metric log files.",
    type=click.Path(),
)
@click.option(
    "--max-gateways-tui",
    "-g",
    default=5,
    show_default=True,
    help="Maximum number of gateways to display in the TUI.",
    type=int,
)
def main(mqtt_url: str, network_id: int, send_periodic: float, log_dir: str, max_gateways_tui: int):
    """A basic example of using the MariLibCloud library."""

    mari = MarilibCloud(
        on_event,
        mqtt_interface=MQTTAdapter.from_url(mqtt_url, is_edge=False, **mqtt_options_from_env()),
        logger=MetricsLogger(
            log_dir_base=log_dir, rotation_interval_minutes=1440, log_interval_seconds=1.0
        ),
        network_id=network_id,
        tui=MarilibTUICloud(max_tables=max_gateways_tui),
        main_file=__file__,
    )

    try:
        if send_periodic > 0:
            normal_traffic_interval = send_periodic
            last_normal_send_time = 0

        while True:
            current_time = time.monotonic()

            mari.update()
            if (
                send_periodic > 0
                and current_time - last_normal_send_time >= normal_traffic_interval
            ):
                if mari.nodes:
                    mari.send_frame(MARI_BROADCAST_ADDRESS, DefaultPayload().to_bytes())
                last_normal_send_time = current_time

            mari.render_tui()
            time.sleep(1)

    except KeyboardInterrupt:
        pass
    finally:
        mari.close_tui()
        mari.logger.close()


if __name__ == "__main__":
    main()
