"""Tests for the gateway_info wire format.

The vectors here are built the way the firmware builds the struct, from
mr_uart_packet_gateway_info_t in firmware/mari/models.h, so a change on either
side that is not mirrored on the other fails here rather than on the bench.
"""

import struct

import pytest

from marilib.mari_protocol import MARI_GATEWAY_INFO_VERSION
from marilib.model import GatewayInfo

# struct layout, little-endian, packed:
#   uint8_t  version
#   uint64_t device_id
#   uint16_t net_id
#   uint16_t schedule_id
#   uint64_t sched_usage[4]
#   uint64_t asn
#   uint32_t timer
#   uint32_t uart_stats[10]
GATEWAY_INFO_STRUCT = "<BQHH4QQI10I"
GATEWAY_INFO_SIZE = 97

UART_STAT_NAMES = [
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


def build_gateway_info(
    version=MARI_GATEWAY_INFO_VERSION,
    device_id=0x1122334455667788,
    net_id=0x0001,
    schedule_id=1,
    sched_usage=(0, 0, 0, 0),
    asn=0,
    timer=0,
    uart_stats=(0,) * 10,
) -> bytes:
    """Encode a gateway_info exactly as the firmware memcpys it onto the wire."""
    return struct.pack(
        GATEWAY_INFO_STRUCT,
        version,
        device_id,
        net_id,
        schedule_id,
        *sched_usage,
        asn,
        timer,
        *uart_stats,
    )


def test_struct_layout_matches_declared_size():
    assert struct.calcsize(GATEWAY_INFO_STRUCT) == GATEWAY_INFO_SIZE
    assert GatewayInfo().size == GATEWAY_INFO_SIZE


def test_roundtrip_all_fields():
    stats = tuple(range(1, 11))
    payload = build_gateway_info(
        device_id=0x1122334455667788,
        net_id=0xABCD,
        schedule_id=3,
        sched_usage=(0x1111111111111111, 0x2222222222222222, 0x3, 0x4),
        asn=0xDEADBEEF,
        timer=0x01020304,
        uart_stats=stats,
    )
    info = GatewayInfo().from_bytes(payload)

    assert info.version == MARI_GATEWAY_INFO_VERSION
    assert info.address == 0x1122334455667788
    assert info.network_id == 0xABCD
    assert info.schedule_id == 3
    assert info.asn == 0xDEADBEEF
    assert info.timer == 0x01020304
    for name, expected in zip(UART_STAT_NAMES, stats):
        assert getattr(info, name) == expected, name


def test_roundtrip_sched_usage_is_read_at_the_right_offset():
    """schedule_id is a uint16_t on the wire.

    Reading it as one byte shifts sched_usage by one, which the schedule
    rendering used to compensate for by dropping the first eight bits.
    """
    info = GatewayInfo().from_bytes(build_gateway_info(schedule_id=1, sched_usage=(0xFF, 0, 0, 0)))
    assert info.schedule_id == 1
    # 0x00000000000000FF as the first uint64 of a 256-bit little-endian value
    assert info.schedule_stats == 0xFF


def test_schedule_cell_usage_bit_n_is_cell_n():
    """Cell n of the schedule is bit n of the sched_usage bitmap."""
    # cells 0 and 5 in use, nothing else
    info = GatewayInfo().from_bytes(
        build_gateway_info(schedule_id=1, sched_usage=((1 << 0) | (1 << 5), 0, 0, 0))
    )
    bits = info.repr_schedule_stats()
    assert bits[0] == "1"
    assert bits[5] == "1"
    assert set(bits[1:5]) == {"0"}
    assert set(bits[6:]) == {"0"}


def test_short_payload_is_rejected():
    """A payload one field short must raise, not parse into garbage."""
    payload = build_gateway_info()[:-4]
    with pytest.raises(ValueError, match="expected 97"):
        GatewayInfo().from_bytes(payload)


def test_old_layout_payload_is_rejected():
    """The pre-counter gateway_info was 56 bytes and had no version field.

    Left unchecked the base parser would consume its first 56 bytes as if they
    were the new layout and read every field from the wrong offset.
    """
    old_layout = struct.pack("<QHH4QQI", 0x1122334455667788, 1, 1, 0, 0, 0, 0, 0, 0)
    assert len(old_layout) == 56
    with pytest.raises(ValueError, match="expected 97"):
        GatewayInfo().from_bytes(old_layout)


def test_longer_payload_is_rejected():
    """Extra trailing bytes mean the sender has fields we do not know about."""
    with pytest.raises(ValueError, match="expected 97"):
        GatewayInfo().from_bytes(build_gateway_info() + b"\x00\x00\x00\x00")


def test_to_bytes_round_trips_through_from_bytes():
    info = GatewayInfo().from_bytes(build_gateway_info(uart_stats=tuple(range(10, 20))))
    assert info.to_bytes() == build_gateway_info(uart_stats=tuple(range(10, 20)))
