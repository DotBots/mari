from __future__ import annotations
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from rich.columns import Columns
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from marilib.model import SCHEDULES, MariNode, TestState
from marilib.tui import MarilibTUI

if TYPE_CHECKING:
    from marilib.marilib_edge import MarilibEdge

# Per-node TX-queue depth (in slotframes) above which we color the
# value yellow (mild contention) or red (real saturation). Same
# thresholds apply to the network-wide warning in the header.
QUEUE_DEPTH_WARN_SF = 2.0
QUEUE_DEPTH_BAD_SF = 4.0

# Per-node RSSI (dBm) thresholds for coloring the link-quality value.
# RSSI is negative; more negative = weaker. At or below BAD we color the
# value red (poor link), between WARN and BAD yellow, above WARN plain.
RSSI_WARN_DBM = -60  # weaker than this -> yellow
RSSI_BAD_DBM = -70  # weaker than this -> red


def _rssi_cell(dbm: "float | None") -> str:
    """Format an RSSI value (dBm) with color: red for weak links
    (<= RSSI_BAD_DBM), yellow for marginal (<= RSSI_WARN_DBM), plain
    otherwise. Returns '...' when no sample is available."""
    if dbm is None:
        return "..."
    val = f"{dbm:.0f}"
    if dbm <= RSSI_BAD_DBM:
        return f"[red]{val}[/red]"
    if dbm <= RSSI_WARN_DBM:
        return f"[yellow]{val}[/yellow]"
    return val


class MarilibTUIEdge(MarilibTUI):
    """A Text-based User Interface for MarilibEdge."""

    def __init__(
        self,
        max_tables=3,
        re_render_max_freq=0.2,
        test_state: TestState | None = None,
    ):
        self.console = Console()
        self.live = Live(console=self.console, auto_refresh=False, transient=True)
        self.live.start()
        self.max_tables = max_tables
        self.re_render_max_freq = re_render_max_freq
        self.last_render_time = datetime.now()
        self.test_state = test_state

    def get_max_rows(self) -> int:
        """Calculate maximum rows based on terminal height."""
        terminal_height = self.console.height
        available_height = terminal_height - 10 - 2 - 2 - 1 - 2
        return max(2, available_height)

    def render(self, mari: "MarilibEdge"):
        """Render the TUI layout."""
        with mari.lock:
            if datetime.now() - self.last_render_time < timedelta(seconds=self.re_render_max_freq):
                return
            self.last_render_time = datetime.now()
            layout = Layout()
            layout.split(
                Layout(self.create_header_panel(mari), size=12),
                Layout(self.create_nodes_panel(mari)),
            )
            self.live.update(layout, refresh=True)

    def create_header_panel(self, mari: "MarilibEdge") -> Panel:
        """Create the header panel with gateway and network stats."""
        status = Text()

        # UART Status Line
        status.append("UART: ", style="bold cyan")
        status.append(
            "connected" if mari.serial_connected else "disconnected",
            style="bold green" if mari.serial_connected else "bold red",
        )
        if mari.serial_connected:
            status.append(
                f" via {mari.serial_interface.port} at {mari.serial_interface.baudrate} baud "
            )
        secs = int((datetime.now() - mari.last_received_serial_data_ts).total_seconds())
        status.append(
            f"(last: {secs}s ago)",
            style="bold green" if secs <= 1 else "bold red",
        )

        status.append("  |  ")

        # MQTT Status Line
        status.append("MQTT: ", style="bold cyan")
        if mari.uses_mqtt:
            status.append(
                "connected" if mari.mqtt_connected else "disconnected",
                style="bold green" if mari.mqtt_connected else "bold red",
            )
            if mari.mqtt_connected:
                status.append(f" to {mari.mqtt_interface.host}:{mari.mqtt_interface.port} ")
            mqtt_secs = int((datetime.now() - mari.last_received_mqtt_data_ts).total_seconds())
            status.append(
                f"(last: {mqtt_secs}s ago)",
                style="bold green" if mqtt_secs <= 1 else "bold red",
            )
        else:
            status.append("disabled", style="bold yellow")

        status.append("\n\nGateway:  ", style="bold cyan")
        status.append(f"0x{mari.gateway.info.address:016X}  |  ")
        status.append("Network ID: ", style="bold cyan")
        status.append(f"0x{mari.gateway.info.network_id:04X}  |  ")
        status.append("ASN: ", style="bold cyan")
        status.append(f"{mari.gateway.info.asn:020d}")

        status.append("\n\n")
        status.append("Schedule: ", style="bold cyan")
        status.append(f"#{mari.gateway.info.schedule_id} {mari.gateway.info.schedule_name} |  ")
        status.append(f"{len(mari.gateway.nodes)} / {mari.gateway.info.max_nodes} nodes  |  ")
        status.append(mari.gateway.info.repr_schedule_cells_with_colors())

        # --- Latency and PDR Display ---
        avg_rtt_latency = mari.gateway.stats_avg_latency_roundtrip_node_edge_ms()
        avg_effective_latency = mari.gateway.stats_avg_effective_latency_ms()
        pending_probes = mari.gateway.stats_pending_probe_count()
        has_latency_info = avg_rtt_latency > 0 or avg_effective_latency > 0

        # Check if we have PDR info by looking at the gateway averages
        avg_uart_pdr_up = mari.gateway.stats_avg_pdr_uplink_uart()
        avg_uart_pdr_down = mari.gateway.stats_avg_pdr_downlink_uart()
        has_uart_pdr_info = avg_uart_pdr_up > 0 or avg_uart_pdr_down > 0

        avg_radio_pdr_down = mari.gateway.stats_avg_pdr_downlink_radio()
        avg_radio_pdr_up = mari.gateway.stats_avg_pdr_uplink_radio()
        has_radio_pdr_info = avg_radio_pdr_down > 0 or avg_radio_pdr_up > 0

        radio_pdr_info = (
            f"  Radio ↓ {avg_radio_pdr_down:.1%} ↑ {avg_radio_pdr_up:.1%}"
            if has_radio_pdr_info
            else ""
        )
        uart_pdr_info = (
            f"  UART ↓ {avg_uart_pdr_down:.1%} ↑ {avg_uart_pdr_up:.1%}" if has_uart_pdr_info else ""
        )

        if has_latency_info or has_uart_pdr_info or has_radio_pdr_info:
            status.append("\n\n")

        # Display Latency
        if has_latency_info:
            status.append("Latency:  ", style="bold yellow")
            status.append(f"RTT Avg: {avg_rtt_latency:.1f} ms")
            status.append(f"  |  Effective Avg: {avg_effective_latency:.1f} ms")
            if pending_probes:
                status.append(f"  |  Pending: {pending_probes}")
            # Network-wide latency breakdown. Four legs sum ≈ host RTT;
            # see MetricsProbePayload and MariNode docstrings for what
            # each term covers. Red on the largest term so the eye
            # lands on the bottleneck.
            host_ms = mari.gateway.stats_avg_host_overhead_ms()
            dl_ms = mari.gateway.stats_avg_downlink_half_ms()
            app_ms = mari.gateway.stats_avg_node_processing_ms()
            ul_ms = mari.gateway.stats_avg_uplink_half_ms()
            if dl_ms or app_ms or ul_ms:
                worst = max(host_ms, dl_ms, app_ms, ul_ms)

                def fmt(v):
                    s = f"{v:.0f}"
                    return f"[red]{s}[/red]" if v == worst and worst > 0 else s

                # Text.append doesn't parse markup (only Table cells do);
                # build a Text via from_markup so the [red] tags render.
                status.append_text(
                    Text.from_markup(
                        f" (host: {fmt(host_ms)}  dl: {fmt(dl_ms)}  app: {fmt(app_ms)}  ul: {fmt(ul_ms)} ms)"
                    )
                )

            # Saturation warning. Flag the worst node's TX-queue depth in
            # slotframes — if any node's queue is multiple slotframes deep,
            # the operator should see it without scanning the per-node
            # table. Thresholds match the per-node coloring above.
            sched_h = SCHEDULES.get(mari.gateway.info.schedule_id)
            sf_dur_h = float(sched_h["sf_duration"]) if sched_h else 0.0
            if sf_dur_h > 0 and mari.gateway.nodes:
                worst_q = max(
                    (n.stats_queue_depth_slotframes(sf_dur_h) for n in mari.gateway.nodes),
                    default=0.0,
                )
                if worst_q >= QUEUE_DEPTH_BAD_SF:
                    status.append_text(
                        Text.from_markup(
                            f"  [red bold]⚠ TX queue saturated (worst {worst_q:.1f} sf)[/]"
                        )
                    )
                elif worst_q >= QUEUE_DEPTH_WARN_SF:
                    status.append_text(
                        Text.from_markup(
                            f"  [yellow]⚠ TX queue contended (worst {worst_q:.1f} sf)[/]"
                        )
                    )

        # Display PDR — match the bold-yellow style of Latency/Stats so
        # all three metric labels look consistent.
        if has_uart_pdr_info or has_radio_pdr_info:
            status.append("  |  ")
            status.append("PDR:", style="bold yellow")
            status.append(f"{radio_pdr_info}{uart_pdr_info}")

        status.append("\n\nStats:  ", style="bold yellow")
        if self.test_state and self.test_state.load > 0 and self.test_state.rate > 0:
            status.append(
                "Test load: ",
            )
            status.append(f"{self.test_state.load}% of {self.test_state.rate} pps")
            status.append("  |  ")

        # The probe cadence, and the share of downlink it actually costs. The
        # measured rate counts retries, so it runs above the nominal
        # nodes/interval exactly when probes are timing out - the case where the
        # link carries more than was asked for and the latency series is being
        # censored.
        if self.test_state and self.test_state.probe_interval > 0 and self.test_state.rate > 0:
            measured = mari.metrics_tester.probe_rate_hz() if mari.metrics_tester else 0.0
            nominal = len(mari.gateway.nodes) / self.test_state.probe_interval
            over = nominal > 0 and measured > nominal * 1.25
            status.append("Probe: ")
            if self.test_state.probe_every:
                status.append(
                    f"{self.test_state.probe_every} sf = {self.test_state.probe_interval:.1f}s"
                )
            else:
                status.append(f"{self.test_state.probe_interval:.1f}s")
            status.append(" / ")
            status.append(
                f"{measured:.1f} pps = {100.0 * measured / self.test_state.rate:.0f}% of downlink",
                style="yellow" if over else "",
            )
            if over:
                status.append(" (retrying)", style="yellow")
            status.append("  |  ")

        # Raw link saturation, both directions, from frames actually counted -
        # independent of what --load or --probe-every were set to. The settings
        # say what was asked for; this says what the link is carrying.
        #
        # Downlink capacity is d_down slots per slotframe. Uplink capacity is
        # one slot per *connected* node per slotframe (the C schedules carry
        # exactly max_nodes U cells), so the denominator follows the node count:
        # unassigned U slots are idle by construction and including them would
        # hide node-level saturation behind an empty schedule.
        schedule = SCHEDULES.get(mari.gateway.info.schedule_id)
        node_count = len(mari.gateway.nodes)
        if schedule and schedule["sf_duration"]:
            window = 10
            sf_s = float(schedule["sf_duration"]) / 1000.0
            dl_pps = mari.gateway.stats.sent_count(window) / window
            ul_pps = mari.gateway.stats.received_count(window) / window
            dl_cap = float(schedule["d_down"]) / sf_s
            ul_cap = node_count / sf_s if node_count else 0.0

            def _sat(pps, cap):
                if cap <= 0:
                    return "n/a", ""
                frac = pps / cap
                style = "red" if frac > 0.95 else ("yellow" if frac > 0.8 else "")
                return f"{pps:.1f}/{cap:.1f} = {frac:.0%}", style

            dl_txt, dl_style = _sat(dl_pps, dl_cap)
            ul_txt, ul_style = _sat(ul_pps, ul_cap)
            status.append("Link: ")
            status.append("DL ")
            status.append(dl_txt, style=dl_style)
            status.append("  UL ")
            status.append(ul_txt, style=ul_style)
            status.append("  |  ")

        stats = mari.gateway.stats
        status.append(f"Frames TX: {stats.sent_count(include_test_packets=True)}  |  ")
        status.append(f"Frames RX: {stats.received_count(include_test_packets=True)}  |  ")
        status.append(f"TX/s: {stats.sent_count(1, include_test_packets=True)}  |  ")
        status.append(f"RX/s: {stats.received_count(1, include_test_packets=True)}")

        # UART link health. The host counts what it wrote, the gateway counts
        # what arrived, and the error counters say where anything missing went.
        # All are cumulative since the gateway booted.
        info = mari.gateway.info
        host = mari.serial_interface.stats
        errors = (
            info.uart_rx_hdlc_err
            + info.uart_rx_hw_overrun
            + info.uart_rx_hw_framing
            + info.uart_rx_hw_break
            + info.uart_rx_slot_full
            + info.uart_tx_queue_drop
            + info.ipc_u2r_lost
            + info.ipc_r2u_lost
        )
        status.append("\n")
        status.append(f"UART: host TX {host.write_calls} frames / {host.write_bytes} B  |  ")
        status.append(f"gw RX {info.uart_rx_frames_ok} frames / {info.uart_rx_bytes} B  |  ")
        status.append("errors ")
        status.append(f"{errors}", style="red" if errors else "green")
        if errors:
            status.append(
                f" (hdlc {info.uart_rx_hdlc_err}, overrun {info.uart_rx_hw_overrun}, "
                f"framing {info.uart_rx_hw_framing}, break {info.uart_rx_hw_break}, "
                f"slot-full {info.uart_rx_slot_full}, txq {info.uart_tx_queue_drop}, "
                f"ipc {info.ipc_u2r_lost}/{info.ipc_r2u_lost})"
            )

        return Panel(
            status,
            title=f"[bold]MariEdge running since {mari.started_ts.strftime('%Y-%m-%d %H:%M:%S')}",
            border_style="blue",
        )

    def create_nodes_table(
        self,
        nodes: list[MariNode],
        title="",
        sf_duration_ms: float = 0.0,
    ) -> Table:
        table = Table(
            show_header=True,
            header_style="bold cyan",
            border_style="blue",
            padding=(0, 1),
            title=title,
        )
        table.add_column("Node Address", style="cyan")
        table.add_column("TX", justify="right")
        table.add_column("/s", justify="right")
        table.add_column("RX", justify="right")
        table.add_column("/s", justify="right")
        table.add_column("Radio ↓ PDR | RSSI", justify="center")
        table.add_column("Radio ↑ PDR | RSSI", justify="center")
        table.add_column("UART PDR ↓ | ↑", justify="center")
        # Latency breakdown and Q (sf) are explained in the
        # "Reading the TUI" section of README.md.
        table.add_column("Latency | host/dl/app/ul (ms)", justify="center")
        table.add_column("Effective (ms)", justify="right")
        table.add_column("Q (sf)", justify="right")

        for node in nodes:
            # Combined latency cell: total host RTT on the left, then
            # the four-leg breakdown (host overhead, dl, app, ul) on
            # the right. host overhead = host RTT - wire RTT (UART
            # round-trip + Python parse); dl/app/ul are the wire-side
            # ASN-decomposed legs. Sum of the four ≈ total host RTT.
            # When no probe samples exist yet, show "...". When the
            # host RTT is known but the firmware hasn't populated the
            # wire ASNs yet, show the total with "?" for the legs.
            host_rtt = node.stats_avg_latency_roundtrip_node_edge_ms()
            dl_ms = node.stats_avg_downlink_half_ms()
            app_ms = node.stats_avg_node_processing_ms()
            ul_ms = node.stats_avg_uplink_half_ms()
            host_ms = node.stats_avg_host_overhead_ms()
            wire_available = (dl_ms or app_ms or ul_ms) and host_rtt > 0
            if wire_available:
                # Color the largest of the four legs red so the eye
                # lands on the bottleneck. Ties resolve to the latest
                # in host/dl/app/ul order.
                worst = max(host_ms, dl_ms, app_ms, ul_ms)
                parts = []
                for label_val in [host_ms, dl_ms, app_ms, ul_ms]:
                    s = f"{label_val:.0f}"
                    if label_val == worst and worst > 0:
                        s = f"[red]{s}[/red]"
                    parts.append(s)
                lat_str = f"{host_rtt:.0f} | {parts[0]} / {parts[1]} / {parts[2]} / {parts[3]}"
            elif host_rtt > 0:
                lat_str = f"{host_rtt:.0f} | ? / ? / ? / ?"
            else:
                lat_str = "..."

            effective_ms = node.stats_avg_effective_latency_ms()
            if effective_ms > 0:
                effective_str = f"{effective_ms:.0f}"
                if node.stats_pending_probe_count():
                    effective_str += "*"
            else:
                effective_str = "..."

            # Node TX-queue depth in slotframes (1 U slot per node per
            # slotframe → ul_ms / sf_duration_ms ≈ packets queued ahead
            # of the probe response). Needs the gateway's slotframe
            # duration, passed in from create_nodes_panel.
            if sf_duration_ms > 0 and ul_ms > 0:
                q_sf = node.stats_queue_depth_slotframes(sf_duration_ms)
                if q_sf >= QUEUE_DEPTH_BAD_SF:
                    q_str = f"[red]{q_sf:.1f}[/red]"
                elif q_sf >= QUEUE_DEPTH_WARN_SF:
                    q_str = f"[yellow]{q_sf:.1f}[/yellow]"
                else:
                    q_str = f"{q_sf:.1f}"
            else:
                q_str = "..."
            # PDR Downlink with color coding
            if node.stats_pdr_downlink_radio() > 0:
                if node.stats_pdr_downlink_radio() > 0.9:
                    pdr_down_str = f"[white]{node.stats_pdr_downlink_radio():>4.0%}[/white]"
                elif node.stats_pdr_downlink_radio() > 0.8:
                    pdr_down_str = f"[yellow]{node.stats_pdr_downlink_radio():>4.0%}[/yellow]"
                else:
                    pdr_down_str = f"[red]{node.stats_pdr_downlink_radio():>4.0%}[/red]"
            else:
                pdr_down_str = "..."

            # PDR Uplink with color coding
            if node.stats_pdr_uplink_radio() > 0:
                if node.stats_pdr_uplink_radio() > 0.9:
                    pdr_up_str = f"[white]{node.stats_pdr_uplink_radio():>4.0%}[/white]"
                elif node.stats_pdr_uplink_radio() > 0.8:
                    pdr_up_str = f"[yellow]{node.stats_pdr_uplink_radio():>4.0%}[/yellow]"
                else:
                    pdr_up_str = f"[red]{node.stats_pdr_uplink_radio():>4.0%}[/red]"
            else:
                pdr_up_str = "..."

            # PDR UART Up / Down with color coding
            if node.stats_pdr_downlink_uart() > 0:
                if node.stats_pdr_downlink_uart() > 0.9:
                    pdr_down_gw_edge_str = f"[white]{node.stats_pdr_downlink_uart():>4.0%}[/white]"
                elif node.stats_pdr_downlink_uart() > 0.8:
                    pdr_down_gw_edge_str = (
                        f"[yellow]{node.stats_pdr_downlink_uart():>4.0%}[/yellow]"
                    )
                else:
                    pdr_down_gw_edge_str = f"[red]{node.stats_pdr_downlink_uart():>4.0%}[/red]"
            else:
                pdr_down_gw_edge_str = "..."

            if node.stats_pdr_uplink_uart() > 0:
                if node.stats_pdr_uplink_uart() > 0.9:
                    pdr_up_gw_edge_str = f"[white]{node.stats_pdr_uplink_uart():>4.0%}[/white]"
                elif node.stats_pdr_uplink_uart() > 0.8:
                    pdr_up_gw_edge_str = f"[yellow]{node.stats_pdr_uplink_uart():>4.0%}[/yellow]"
                else:
                    pdr_up_gw_edge_str = f"[red]{node.stats_pdr_uplink_uart():>4.0%}[/red]"
            else:
                pdr_up_gw_edge_str = "..."

            rssi_node_str = _rssi_cell(node.stats_rssi_node_dbm())
            rssi_gw_str = _rssi_cell(node.stats_rssi_gw_dbm())

            table.add_row(
                f"0x{node.address:016X}",
                str(node.stats.sent_count(include_test_packets=True)),
                str(node.stats.sent_count(1, include_test_packets=True)),
                str(node.stats.received_count(include_test_packets=True)),
                str(node.stats.received_count(1, include_test_packets=True)),
                f"{pdr_down_str} | {rssi_node_str} dBm",
                f"{pdr_up_str} | {rssi_gw_str} dBm",
                f"{pdr_down_gw_edge_str} | {pdr_up_gw_edge_str}",
                lat_str,
                effective_str,
                q_str,
            )
        return table

    def create_nodes_panel(self, mari: "MarilibEdge") -> Panel:
        """Create the panel that contains the nodes table."""
        nodes = mari.gateway.nodes
        sched = SCHEDULES.get(mari.gateway.info.schedule_id)
        sf_duration_ms = float(sched["sf_duration"]) if sched else 0.0
        max_rows = self.get_max_rows()
        max_displayable_nodes = self.max_tables * max_rows
        nodes_to_display = nodes[:max_displayable_nodes]
        remaining_nodes = max(0, len(nodes) - max_displayable_nodes)
        tables = []
        current_table_nodes = []
        for i, node in enumerate(nodes_to_display):
            current_table_nodes.append(node)
            if len(current_table_nodes) == max_rows or i == len(nodes_to_display) - 1:
                title = f"Nodes {i - len(current_table_nodes) + 2}-{i + 1}"
                tables.append(self.create_nodes_table(current_table_nodes, title, sf_duration_ms))
                current_table_nodes = []
                if len(tables) >= self.max_tables:
                    break
        if len(tables) > 1:
            content = Columns(tables, equal=True, expand=True)
        else:
            content = tables[0] if tables else Table()
        if remaining_nodes > 0:
            panel_content = Group(
                content,
                Text(
                    f"\n(...and {remaining_nodes} more nodes)",
                    style="bold yellow",
                ),
            )
        else:
            panel_content = content
        return Panel(
            panel_content,
            title="[bold]Connected Nodes",
            border_style="blue",
        )

    def close(self):
        """Clean up the live display."""
        self.live.stop()
        print("")
