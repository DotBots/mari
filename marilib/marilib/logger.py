import csv
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import IO, List, Dict

from marilib.model import MariGateway, MariNode

# Counters carried in every gateway_info, named as they are in
# mr_gateway_uart_stats_t (firmware/mari/models.h).
GATEWAY_UART_STAT_COLUMNS = [
    "uart_rx_bytes",
    "uart_rx_frames_ok",
    "uart_rx_hdlc_err",
    "uart_rx_hw_overrun",
    "uart_rx_hw_framing",
    "uart_rx_hw_break",
    "uart_rx_slot_full",
    "uart_tx_queue_drop",
    "ipc_u2r_lost",
    "ipc_r2u_lost",
]

# The host's own view of the same hop, from SerialAdapterStats.
HOST_UART_STAT_COLUMNS = [
    "host_write_calls",
    "host_write_bytes",
    "host_write_errors",
    "host_rx_frames_ok",
    "host_rx_hdlc_err",
]


@dataclass
class MetricsLogger:
    """
    A metrics logger that saves statistics to CSV files with log rotation.
    """

    log_dir_base: str = "logs"
    rotation_interval_minutes: int = 1440  # 1 day
    log_interval_seconds: float = 1.0
    last_log_time: Dict[int, datetime] = field(default_factory=dict)

    def __post_init__(self):
        """
        Initializes the logger with rotation and setup logging capabilities.
        """
        try:
            self.rotation_interval = timedelta(minutes=self.rotation_interval_minutes)

            self.start_time = datetime.now()
            self.run_timestamp = self.start_time.strftime("%Y%m%d_%H%M%S")
            self.log_dir = os.path.join(self.log_dir_base, f"run_{self.run_timestamp}")
            os.makedirs(self.log_dir, exist_ok=True)

            self._gateway_file: IO[str] | None = None
            self._nodes_file: IO[str] | None = None
            self._events_file: IO[str] | None = None
            self._gateway_writer = None
            self._nodes_writer = None
            self._events_writer = None
            self.segment_start_time: datetime | None = None

            # Open events log file
            events_path = os.path.join(self.log_dir, "log_events.csv")
            self._events_file = open(events_path, "w", newline="", encoding="utf-8")
            self._events_writer = csv.writer(self._events_file)
            self._events_writer.writerow(
                ["timestamp", "gateway_address", "node_address", "event_name", "event_tag"]
            )

            self._open_new_segment()
            self.active = True

        except (IOError, OSError) as e:
            print(f"Error: Failed to initialize logger: {e}")
            self.active = False

    def log_setup_parameters(self, params: Dict[str, any] | None):
        """Creates and writes test setup parameters to metrics_setup.csv.

        Rewrites the file on every call. Callers enrich the parameter dict as
        facts arrive - the schedule name is only knowable once the first
        GATEWAY_INFO lands, well after the logger is constructed - and a
        write-once guard here silently dropped every one of those late fields.
        """
        if not params:
            return

        setup_path = os.path.join(self.log_dir, "metrics_setup.csv")
        with open(setup_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["param", "value"])
            writer.writerow(["start_time", self.start_time.isoformat()])
            for key, value in params.items():
                writer.writerow([key, value])

    def _open_new_segment(self):
        self._close_segment_files()

        self.segment_start_time = datetime.now()
        segment_ts = self.segment_start_time.strftime("%H%M%S")

        gateway_path = os.path.join(self.log_dir, f"gateway_metrics_{segment_ts}.csv")
        nodes_path = os.path.join(self.log_dir, f"node_metrics_{segment_ts}.csv")

        self._gateway_file = open(gateway_path, "w", newline="", encoding="utf-8")
        self._gateway_writer = csv.writer(self._gateway_file)
        gateway_header = [
            "timestamp",
            "gateway_address",
            "schedule_id",
            "connected_nodes",
            # "tx_total",
            # "rx_total",
            # "tx_rate_1s",
            # "rx_rate_1s",
            "avg_latency_ms",
            "avg_effective_latency_ms",
            "pending_probes",
            "avg_pdr_downlink_radio",
            "avg_pdr_uplink_radio",
            "latest_node_tx_count",
            "latest_node_rx_count",
            "latest_gw_tx_count",
            "latest_gw_rx_count",
            # Cumulative UART / inter-core counters, gateway side (from
            # gateway_info) then host side (from the serial adapter). They only
            # reset on reboot, so read them as differences between rows. Any
            # non-zero error counter localizes a loss on the host-to-gateway
            # hop, which nothing else in this file can see.
            *GATEWAY_UART_STAT_COLUMNS,
            *HOST_UART_STAT_COLUMNS,
        ]
        self._gateway_writer.writerow(gateway_header)

        self._nodes_file = open(nodes_path, "w", newline="", encoding="utf-8")
        self._nodes_writer = csv.writer(self._nodes_file)
        nodes_header = [
            "timestamp",
            "gateway_address",
            "node_address",
            "is_alive",
            # "tx_total",
            # "rx_total",
            # "tx_rate_1s",
            # "rx_rate_1s",
            "success_rate_30s",
            "success_rate_total",
            "pdr_downlink",
            "pdr_uplink",
            "radio_pdr_downlink",
            "radio_pdr_uplink",
            # A rolling window over the last MARI_PROBE_STATS_MAX_LEN (10)
            # probes, not a cumulative figure: the ratio is taken between the
            # oldest and newest entries of a bounded deque. Its resolution is
            # therefore ~1/10 per node, enough to show that several percent are
            # being lost and never enough to certify three nines. For an exact
            # figure over any window, pool the raw *_count columns below.
            "uart_pdr_downlink",
            "uart_pdr_uplink",
            "rssi_node_dbm",
            "rssi_gw_dbm",
            "avg_latency_edge_ms",
            "avg_effective_latency_ms",
            "pending_probes",
            "avg_latency_cloud_ms",
            "last_latency_edge_ms",
            "last_latency_cloud_ms",
            # Raw counters and ASN split from the node's latest probe, one
            # sample per probe rather than a rolling average. Rows are written
            # every log_interval_seconds, so consecutive rows repeat the same
            # probe until a new one lands: dedup on edge_rx_count. The six
            # counters make PDR exact over any window (and per node), which
            # the rolling radio_pdr_* columns above cannot give.
            "gw_tx_count",
            "gw_rx_count",
            "node_tx_count",
            "node_rx_count",
            "edge_tx_count",
            "edge_rx_count",
            "last_downlink_half_ms",
            "last_node_processing_ms",
            "last_uplink_half_ms",
            "last_wire_rtt_ms",
        ]
        self._nodes_writer.writerow(nodes_header)

    def _check_for_rotation(self):
        if datetime.now() - self.segment_start_time >= self.rotation_interval:
            self._open_new_segment()

    def _log_common(self):
        if not self.active:
            return False
        self._check_for_rotation()
        return True

    def log_periodic_metrics(
        self, gateway: MariGateway, nodes: List[MariNode], host_stats: Dict[str, int] | None = None
    ):
        last_log_time = self.last_log_time.get(gateway.info.address, self.segment_start_time)
        if datetime.now() - last_log_time >= timedelta(seconds=self.log_interval_seconds):
            self.log_gateway_metrics(gateway, host_stats)
            self.log_all_nodes_metrics(nodes)
            self.last_log_time[gateway.info.address] = datetime.now()
            # Flush at the sampling rate, as log_events.csv already does. Two
            # reasons: a run becomes readable while it is still going, and a
            # process that dies without running close() still leaves its data
            # behind instead of an empty file.
            for f in (self._gateway_file, self._nodes_file):
                if f and not f.closed:
                    f.flush()

    def log_gateway_metrics(self, gateway: MariGateway, host_stats: Dict[str, int] | None = None):
        if not self._log_common() or self._gateway_writer is None:
            return

        host_stats = host_stats or {}
        timestamp = datetime.now().isoformat()
        row = [
            timestamp,
            f"0x{gateway.info.address:016X}",
            gateway.info.schedule_id,
            len(gateway.nodes),
            # gateway.stats.sent_count(include_test_packets=False),
            # gateway.stats.received_count(include_test_packets=False),
            # gateway.stats.sent_count(1, include_test_packets=False),
            # gateway.stats.received_count(1, include_test_packets=False),
            f"{gateway.stats_avg_latency_roundtrip_node_edge_ms():.2f}",
            f"{gateway.stats_avg_effective_latency_ms():.2f}",
            gateway.stats_pending_probe_count(),
            f"{gateway.stats_avg_pdr_downlink_radio():.2f}",
            f"{gateway.stats_avg_pdr_uplink_radio():.2f}",
            gateway.stats_latest_node_tx_count(),
            gateway.stats_latest_node_rx_count(),
            gateway.stats_latest_gw_tx_count(),
            gateway.stats_latest_gw_rx_count(),
            *(getattr(gateway.info, name, "") for name in GATEWAY_UART_STAT_COLUMNS),
            *(host_stats.get(name.removeprefix("host_"), "") for name in HOST_UART_STAT_COLUMNS),
        ]
        self._gateway_writer.writerow(row)

    def _safe_fraction(self, value: float | None) -> float:
        """Normalizes invalid PDR values to a stable 0..1 range for CSV export."""
        if value is None:
            return 0.0
        try:
            v = float(value)
        except (TypeError, ValueError):
            return 0.0
        return 0.0 if v < 0 else min(v, 1.0)

    def _latest_probe_fields(self, node: MariNode) -> list:
        """Raw counters + ASN-decomposed latency from the node's latest probe.

        Blank (not 0) when the node has no probe yet, so "no sample" stays
        distinguishable from a genuine zero counter.
        """
        probe = node.probe_stats_latest
        if probe is None:
            return [""] * 10
        return [
            probe.gw_tx_count,
            probe.gw_rx_count,
            probe.node_tx_count,
            probe.node_rx_count,
            probe.edge_tx_count,
            probe.edge_rx_count,
            f"{probe.downlink_half_ms():.2f}",
            f"{probe.node_processing_ms():.2f}",
            f"{probe.uplink_half_ms():.2f}",
            f"{probe.wire_rtt_ms():.2f}",
        ]

    def log_all_nodes_metrics(self, nodes: List[MariNode]):
        """Writes metrics for all nodes, handling rotation."""
        if not self._log_common() or self._nodes_writer is None:
            return

        timestamp = datetime.now().isoformat()
        for node in nodes:
            row = (
                [
                    timestamp,
                    f"0x{node.gateway_address:016X}",
                    f"0x{node.address:016X}",
                    node.is_alive,
                    # node.stats.sent_count(include_test_packets=False),
                    # node.stats.received_count(include_test_packets=False),
                    # node.stats.sent_count(1, include_test_packets=False),
                    # node.stats.received_count(1, include_test_packets=False),
                    f"{node.stats.success_rate(30):.2%}",
                    f"{node.stats.success_rate():.2%}",
                    f"{node.pdr_downlink:.2%}",
                    f"{node.pdr_uplink:.2%}",
                    f"{self._safe_fraction(node.stats_pdr_downlink_radio()):.2%}",
                    f"{self._safe_fraction(node.stats_pdr_uplink_radio()):.2%}",
                    f"{self._safe_fraction(node.stats_pdr_downlink_uart()):.2%}",
                    f"{self._safe_fraction(node.stats_pdr_uplink_uart()):.2%}",
                    node.stats_rssi_node_dbm(),
                    node.stats_rssi_gw_dbm(),
                    f"{node.stats_avg_latency_roundtrip_node_edge_ms():.2f}",
                    f"{node.stats_avg_effective_latency_ms():.2f}",
                    node.stats_pending_probe_count(),
                    f"{node.stats_avg_latency_roundtrip_node_edge_ms():.2f}",  # FIXME!: should use cloud option
                    f"{node.stats_latest_latency_roundtrip_node_edge_ms():.2f}",
                    f"{node.stats_latest_latency_roundtrip_node_edge_ms():.2f}",  # FIXME!: should use cloud option
                ]
                + self._latest_probe_fields(node)
            )
            self._nodes_writer.writerow(row)

    def log_event(
        self, gateway_address: int, node_address: int, event_name: str, event_tag: str = ""
    ):
        """Logs an event to the events log file."""
        if not self.active or self._events_writer is None:
            return

        timestamp = datetime.now().isoformat()
        row = [
            timestamp,
            f"0x{gateway_address:016X}",
            f"0x{node_address:016X}",
            event_name,
            event_tag,
        ]
        self._events_writer.writerow(row)
        if self._events_file:
            self._events_file.flush()

    def _close_segment_files(self):
        if self._gateway_file and not self._gateway_file.closed:
            self._gateway_file.close()
        if self._nodes_file and not self._nodes_file.closed:
            self._nodes_file.close()

    def close(self):
        if not self.active:
            return

        self._close_segment_files()
        if self._events_file and not self._events_file.closed:
            self._events_file.close()
        print(f"\nMetrics saved to: {self.log_dir}")
        self.active = False
