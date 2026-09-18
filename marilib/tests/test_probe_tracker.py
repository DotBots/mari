import threading
from unittest.mock import MagicMock

import pytest

from marilib.mari_protocol import Frame, Header, MetricsProbePayload
from marilib.metrics import MetricsTester
from marilib.model import SCHEDULES, GatewayInfo, MariGateway, MariNode
from marilib.probe_tracker import MAX_PROBE_RETRIES, PendingProbe


def _make_tester(interval: float = 1.0, schedule_id: int = 6) -> tuple[MetricsTester, MagicMock]:
    marilib = MagicMock()
    marilib.lock = threading.Lock()
    marilib.gateway = MariGateway()
    marilib.gateway.set_info(
        GatewayInfo(address=0x1000, network_id=0x0001, schedule_id=schedule_id)
    )
    tester = MetricsTester(marilib, interval=interval)
    return tester, marilib


def _add_test_node(marilib: MagicMock, address: int = 0xABCD) -> MariNode:
    return marilib.gateway.add_node(address)


def _probe_response_payload(edge_tx_ts_us: int) -> bytes:
    payload = MetricsProbePayload(edge_tx_ts_us=edge_tx_ts_us)
    return payload.to_bytes()


def test_register_pending_on_send():
    tester, marilib = _make_tester()
    node = _add_test_node(marilib)
    marilib.send_probe.return_value = 123456

    tester._transmit_probe(node)

    assert 123456 in node.sent_probe_packets
    pending = node.sent_probe_packets[123456]
    assert pending.sent_at_us == 123456
    assert pending.retry_count == 0
    marilib.send_probe.assert_called_once()


def test_matching_response_records_effective_and_rtt():
    tester, marilib = _make_tester()
    node = _add_test_node(marilib)
    node.probe_tracker.register_pending(
        PendingProbe(edge_tx_ts_us=1000, node_address=node.address, sent_at_us=1000)
    )

    frame = Frame(
        header=Header(source=node.address),
        payload=_probe_response_payload(1000),
    )
    payload = tester.handle_response_edge(frame, rx_ts_us=51000)

    assert payload is not None
    assert node.stats_pending_probe_count() == 0
    assert node.stats_avg_effective_latency_ms() == pytest.approx(50.0)
    assert len(node.probe_stats) == 1
    assert node.probe_stats_latest.latency_roundtrip_node_edge_ms() == pytest.approx(50.0)


def test_unmatched_response_ignored_for_rtt_stats():
    """An unmatched (late) reply stays out of the edge's own RTT stats, but is
    still stamped and returned so the frame forwarded to the cloud carries a
    usable edge_rx_ts_us instead of 0."""
    tester, marilib = _make_tester()
    node = _add_test_node(marilib)

    frame = Frame(
        header=Header(source=node.address),
        payload=_probe_response_payload(9999),
    )
    payload = tester.handle_response_edge(frame, rx_ts_us=60000)

    assert payload is not None
    assert payload.edge_rx_ts_us == 60000
    assert payload.latency_roundtrip_node_edge_ms() == pytest.approx(50.001)
    assert node.stats_avg_effective_latency_ms() == 0.0
    assert len(node.probe_stats) == 0


def test_timeout_is_two_slotframes():
    tiny, _ = _make_tester(interval=1.0, schedule_id=6)
    assert tiny._probe_timeout_ms() == pytest.approx(2 * SCHEDULES[6]["sf_duration"])

    huge, _ = _make_tester(interval=1.0, schedule_id=1)
    assert huge._probe_timeout_ms() == pytest.approx(2 * SCHEDULES[1]["sf_duration"])


def test_timeout_disabled_without_schedule():
    tester, marilib = _make_tester(schedule_id=99)
    assert tester._probe_timeout_ms() is None


def test_timeout_records_effective_latency_and_retransmits():
    tester, marilib = _make_tester(schedule_id=6)
    node = _add_test_node(marilib)
    node.probe_tracker.register_pending(
        PendingProbe(edge_tx_ts_us=1000, node_address=node.address, sent_at_us=0)
    )
    marilib.send_probe.return_value = 2000

    tester.check_timeouts()

    assert node.stats_avg_effective_latency_ms() == pytest.approx(2 * SCHEDULES[6]["sf_duration"])
    assert 1000 not in node.sent_probe_packets
    assert 2000 in node.sent_probe_packets
    assert node.sent_probe_packets[2000].retry_count == 1
    marilib.send_probe.assert_called_once()


def test_timeout_stops_retrying_after_max_retries():
    tester, marilib = _make_tester(schedule_id=6)
    node = _add_test_node(marilib)
    node.probe_tracker.register_pending(
        PendingProbe(
            edge_tx_ts_us=1000,
            node_address=node.address,
            sent_at_us=0,
            retry_count=MAX_PROBE_RETRIES,
        )
    )

    tester.check_timeouts()

    assert node.stats_pending_probe_count() == 0
    marilib.send_probe.assert_not_called()


def test_effective_latency_higher_than_rtt_when_packets_lost():
    tester, marilib = _make_tester(schedule_id=6)
    node = _add_test_node(marilib)

    node.probe_tracker.record_effective_sample(29.31)
    node.probe_tracker.record_effective_sample(29.31)

    tx_us = 1_000_000
    rx_us = 1_025_000
    frame = Frame(
        header=Header(source=node.address),
        payload=_probe_response_payload(tx_us),
    )
    node.probe_tracker.register_pending(
        PendingProbe(edge_tx_ts_us=tx_us, node_address=node.address, sent_at_us=tx_us)
    )
    tester.handle_response_edge(frame, rx_ts_us=rx_us)

    assert node.stats_avg_latency_roundtrip_node_edge_ms() == pytest.approx(25.0)
    assert node.stats_avg_effective_latency_ms() > node.stats_avg_latency_roundtrip_node_edge_ms()


def test_send_skipped_when_probe_already_pending():
    tester, marilib = _make_tester()
    node = _add_test_node(marilib)
    node.probe_tracker.register_pending(
        PendingProbe(edge_tx_ts_us=42, node_address=node.address, sent_at_us=42)
    )

    tester._send_edge_probe(node)

    marilib.send_probe.assert_not_called()


def test_probe_tracker_avg_ignores_empty_samples():
    from marilib.probe_tracker import ProbeTracker

    tracker = ProbeTracker()
    assert tracker.stats_avg_effective_latency_ms() == 0.0
    tracker.record_effective_sample(10.0)
    tracker.record_effective_sample(30.0)
    assert tracker.stats_avg_effective_latency_ms() == pytest.approx(20.0)
