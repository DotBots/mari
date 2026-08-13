import dataclasses
from dataclasses import dataclass
from enum import IntEnum

from marilib.protocol import Packet, PacketFieldMetadata, PacketType

MARI_PROTOCOL_VERSION = 3
# The gateway_info UART struct versions independently of the radio format.
MARI_GATEWAY_INFO_VERSION = 1
MARI_BROADCAST_ADDRESS = 0xFFFFFFFFFFFFFFFF
MARI_NET_ID_DEFAULT = 0x0001

# Mari slot duration in ms. From fedrecheski26mari.pdf §3.4 and Table 1:
# all four shipped schedules have ~1.7 ms slots (huge: 256.88 ms /
# 149 slots = 1.7240 ms; the others come out to the same value within
# rounding). Used to convert ASN deltas in MetricsProbePayload to ms.
MARI_SLOT_DURATION_MS = 1.7240


class DefaultPayloadType(IntEnum):
    APPLICATION_DATA = 0x01
    METRICS_REQUEST = 0x90
    METRICS_RESPONSE = 0x91
    METRICS_LOAD = 0x92
    METRICS_PROBE = 0x9C

    def as_bytes(self) -> bytes:
        return bytes([self.value])


class HandoverReason(IntEnum):
    """Reason a node left its gateway, mirror of mr_event_tag_t in
    firmware/mari/models.h. Reported by the node in the metrics probe so a
    handover can be classified as a seamless background-scan switch vs a real
    outage (lost gateway / drift)."""

    NONE = 0
    HANDOVER = 1  # background-scan handover (found the new gateway before leaving)
    OUT_OF_SYNC = 2  # drift resync
    PEER_LOST = 3  # deprecated
    GATEWAY_FULL = 4
    PEER_LOST_TIMEOUT = 5  # missed too many slotframes from the gateway
    PEER_LOST_BLOOM = 6  # gateway beacon no longer lists this node
    HANDOVER_FAILED = 7


class NextProto(IntEnum):
    """Upper-layer protocol multiplex for the mari packet header.

    Mirror of mr_next_proto_t in firmware/mari/models.h. Meaningful when
    PacketType is DATA; for BEACON / JOIN_* / KEEPALIVE the field is
    always MARI_INTERNAL.

    Numeric layout — ranges grouped by who owns the protocol::

        0x00         RESERVED — null catcher (zeroed / uninitialized memory)
        0x01..0x09   mari link-layer internal (metrics, control, ...)
        0x0A..0x0F   unallocated (future mari)
        0x10..0x39   swarm-application / custom protocols
        0x3A..0x9F   unallocated (future categories)
        0xA0..0xFD   standardized network protocols (IPv4, IPv6, ARP, ...)
        0xFE         experimental / private use
        0xFF         UNKNOWN — unclassified; default when next_proto unset
    """

    RESERVED = 0x00  # null catcher (zeroed / uninitialized memory)
    MARI_INTERNAL = 0x01  # mari's own control + metrics
    SWARMIT_TESTBED = 0x10  # SwarmIT testbed protocol
    DOTBOT_APP = 0x11  # DotBot application protocol
    IPV4 = 0xA0  # IPv4 packet (RFC 791)
    IPV6 = 0xA1  # IPv6 packet (RFC 8200)
    EXPERIMENTAL = 0xFE  # experimental / private use
    UNKNOWN = 0xFF  # unclassified; default when next_proto unset


@dataclass
class DefaultPayload(Packet):
    metadata: list[PacketFieldMetadata] = dataclasses.field(
        default_factory=lambda: [
            PacketFieldMetadata(name="type", length=1),
        ]
    )
    type_: DefaultPayloadType = DefaultPayloadType.APPLICATION_DATA

    def with_filler_bytes(self, length: int) -> bytes:
        return self.to_bytes() + bytes([0xF1] * length)


@dataclass
class MetricsProbePayload(Packet):
    metadata: list[PacketFieldMetadata] = dataclasses.field(
        default_factory=lambda: [
            PacketFieldMetadata(name="type", length=1),
            PacketFieldMetadata(name="cloud_tx_ts_us", length=8),
            PacketFieldMetadata(name="cloud_rx_ts_us", length=8),
            PacketFieldMetadata(name="cloud_tx_count", length=4),
            PacketFieldMetadata(name="cloud_rx_count", length=4),
            PacketFieldMetadata(name="edge_tx_ts_us", length=8),
            PacketFieldMetadata(name="edge_rx_ts_us", length=8),
            PacketFieldMetadata(name="edge_tx_count", length=4),
            PacketFieldMetadata(name="edge_rx_count", length=4),
            PacketFieldMetadata(name="gw_tx_count", length=4),
            PacketFieldMetadata(name="gw_rx_count", length=4),
            PacketFieldMetadata(name="gw_rx_asn", length=8),
            PacketFieldMetadata(name="gw_tx_enqueued_asn", length=8),
            PacketFieldMetadata(name="gw_tx_dequeued_asn", length=8),
            PacketFieldMetadata(name="node_rx_count", length=4),
            PacketFieldMetadata(name="node_tx_count", length=4),
            PacketFieldMetadata(name="node_rx_asn", length=8),
            PacketFieldMetadata(name="node_tx_enqueued_asn", length=8),
            PacketFieldMetadata(name="node_tx_dequeued_asn", length=8),
            PacketFieldMetadata(name="rssi_at_node", length=1),
            PacketFieldMetadata(name="rssi_at_gw", length=1),
            PacketFieldMetadata(name="node_handover_duration_ms", length=2),
            PacketFieldMetadata(name="node_handover_seq", length=1),
            PacketFieldMetadata(name="node_handover_reason", length=1),
        ]
    )
    type_: DefaultPayloadType = DefaultPayloadType.METRICS_PROBE
    cloud_tx_ts_us: int = 0
    cloud_rx_ts_us: int = 0
    cloud_tx_count: int = 0
    cloud_rx_count: int = 0
    edge_tx_ts_us: int = 0
    edge_rx_ts_us: int = 0
    edge_tx_count: int = 0
    edge_rx_count: int = 0
    gw_tx_count: int = 0
    gw_rx_count: int = 0
    gw_rx_asn: int = 0
    gw_tx_enqueued_asn: int = 0
    gw_tx_dequeued_asn: int = 0
    node_rx_count: int = 0
    node_tx_count: int = 0
    node_rx_asn: int = 0
    node_tx_enqueued_asn: int = 0
    node_tx_dequeued_asn: int = 0
    rssi_at_node: int = 0
    rssi_at_gw: int = 0
    node_handover_duration_ms: int = 0
    node_handover_seq: int = 0
    node_handover_reason: int = 0

    @property
    def packet_length(self) -> int:
        return sum(field.length for field in self.metadata)

    @property
    def asn(self) -> int:
        """ASN at reception back from the network"""
        return self.gw_rx_asn

    def latency_roundtrip_node_edge_ms(self) -> float:
        return (self.edge_rx_ts_us - self.edge_tx_ts_us) / 1000.0

    def latency_roundtrip_node_cloud_ms(self) -> float:
        return (self.cloud_rx_ts_us - self.cloud_tx_ts_us) / 1000.0

    # ----------------------- ASN-decomposed latency -----------------------
    # The probe carries ASN snapshots taken at the gateway and the node.
    # Multiplied by MARI_SLOT_DURATION_MS, ASN deltas tell us where the
    # RTT went in slot-time:
    #
    #   dl half = node_rx_asn - gw_tx_enqueued_asn
    #             (gateway downlink-queue wait + 1 radio slot)
    #   app     = node_tx_enqueued_asn - node_rx_asn
    #             (swarmit netcore main-loop turnaround)
    #   ul half = gw_rx_asn - node_tx_enqueued_asn
    #             (node uplink-queue wait + 1 radio slot)
    #
    # Note: gw_tx_dequeued_asn and node_tx_dequeued_asn are declared in
    # mari/models.h but never written by the firmware, so we can't isolate
    # the queue wait from the radio-slot fire — both are folded into the
    # 'dl half' and 'ul half' figures. See docs/mari-protocol-summary.md.

    def _asn_delta_ms(self, end_asn: int, start_asn: int) -> float:
        """Convert end-start ASN to ms. Returns 0.0 for missing samples
        (any ASN == 0) or non-monotonic pairs (end <= start, which can
        happen on the first probe before counters have rolled in)."""
        if not end_asn or not start_asn or end_asn <= start_asn:
            return 0.0
        return (end_asn - start_asn) * MARI_SLOT_DURATION_MS

    def downlink_half_ms(self) -> float:
        """Gateway-enqueue to node-receive. Includes the gateway's
        downlink-queue wait plus one radio slot."""
        return self._asn_delta_ms(self.node_rx_asn, self.gw_tx_enqueued_asn)

    def node_processing_ms(self) -> float:
        """Node app-side turnaround: node-receive to node-enqueue. In
        the swarmit firmware, this is the time the netcore main loop
        takes to process the probe and queue the response."""
        return self._asn_delta_ms(self.node_tx_enqueued_asn, self.node_rx_asn)

    def uplink_half_ms(self) -> float:
        """Node-enqueue to gateway-receive. Includes the node's
        uplink-queue wait plus one radio slot. This is where queue
        backpressure at the node will show up."""
        return self._asn_delta_ms(self.gw_rx_asn, self.node_tx_enqueued_asn)

    def wire_rtt_ms(self) -> float:
        """Total wire round-trip in slot-time = dl + app + ul. Should be
        close to host-measured RTT minus UART + Python overhead (~15-30 ms)."""
        return self.downlink_half_ms() + self.node_processing_ms() + self.uplink_half_ms()

    def pdr_saturated(self, count_a: int, count_b: int) -> float:
        if count_b == 0:
            return 0.0
        pdr = count_a / count_b
        if pdr > 1.0:
            return 0.0
        return pdr

    def pdr_uplink_radio(self, probe_stats_start_epoch=None) -> float:
        if probe_stats_start_epoch is None:
            # if no epoch is provided, use the current values
            gw_rx_count = self.gw_rx_count
            node_tx_count = self.node_tx_count
        else:
            # if epoch is provided, subtract the epoch values from the current values
            if probe_stats_start_epoch.asn == 0:
                return 0
            gw_rx_count = self.gw_rx_count - probe_stats_start_epoch.gw_rx_count
            node_tx_count = self.node_tx_count - probe_stats_start_epoch.node_tx_count
        if node_tx_count <= 0:
            return 0
        return self.pdr_saturated(gw_rx_count, node_tx_count)

    def pdr_downlink_radio(self, probe_stats_start_epoch=None) -> float:
        if probe_stats_start_epoch is None:
            # if no epoch is provided, use the current values
            gw_tx_count = self.gw_tx_count
            node_rx_count = self.node_rx_count
        else:
            # if epoch is provided, subtract the epoch values from the current values
            if probe_stats_start_epoch.asn == 0:
                return 0
            gw_tx_count = self.gw_tx_count - probe_stats_start_epoch.gw_tx_count
            node_rx_count = self.node_rx_count - probe_stats_start_epoch.node_rx_count
        return self.pdr_saturated(node_rx_count, gw_tx_count)

    def pdr_uplink_uart(self, probe_stats_start_epoch=None) -> float:
        if probe_stats_start_epoch is None:
            # if no epoch is provided, just wait a bit
            return -1
        # if epoch is provided, subtract the epoch values from the current values
        if probe_stats_start_epoch.asn == 0:
            return 0
        # if a packet was received at the gatweway, it should also be received at the edge (otherwise, it's a loss)
        gw_rx_count = self.gw_rx_count - probe_stats_start_epoch.gw_rx_count
        edge_rx_count = self.edge_rx_count - probe_stats_start_epoch.edge_rx_count
        return self.pdr_saturated(edge_rx_count, gw_rx_count)

    def pdr_downlink_uart(self, probe_stats_start_epoch=None) -> float:
        if probe_stats_start_epoch is None:
            # if no epoch is provided, just wait a bit
            return -1
        # if epoch is provided, subtract the epoch values from the current values
        if probe_stats_start_epoch.asn == 0:
            return 0
        # if a packet was sent at the edge, it should also be sent at the gateway (otherwise, it's a loss)
        gw_tx_count = self.gw_tx_count - probe_stats_start_epoch.gw_tx_count
        edge_tx_count = self.edge_tx_count - probe_stats_start_epoch.edge_tx_count
        return self.pdr_saturated(gw_tx_count, edge_tx_count)

    def rssi_at_node_dbm(self) -> int:
        if self.rssi_at_node > 127:
            return self.rssi_at_node - 255
        return self.rssi_at_node

    def rssi_at_gw_dbm(self) -> int:
        if self.rssi_at_gw > 127:
            return self.rssi_at_gw - 255
        return self.rssi_at_gw

    def __repr__(self):
        rep = dataclasses.asdict(self)
        rep.pop("metadata", None)
        return f"{rep}"


@dataclass
class MetricsRequestPayload(Packet):
    metadata: list[PacketFieldMetadata] = dataclasses.field(
        default_factory=lambda: [
            PacketFieldMetadata(name="type", length=1),
            PacketFieldMetadata(name="timestamp_us", length=8),
        ]
    )
    type_: DefaultPayloadType = DefaultPayloadType.METRICS_REQUEST
    timestamp_us: int = 0


@dataclass
class MetricsResponsePayload(Packet):
    metadata: list[PacketFieldMetadata] = dataclasses.field(
        default_factory=lambda: [
            PacketFieldMetadata(name="type", length=1),
            PacketFieldMetadata(name="timestamp_us", length=8),
            PacketFieldMetadata(name="rx_count", length=4),
            PacketFieldMetadata(name="tx_count", length=4),
        ]
    )
    type_: DefaultPayloadType = DefaultPayloadType.METRICS_RESPONSE
    timestamp_us: int = 0
    rx_count: int = 0
    tx_count: int = 0


@dataclass
class Header(Packet):
    """Dataclass that holds MAC header fields."""

    metadata: list[PacketFieldMetadata] = dataclasses.field(
        default_factory=lambda: [
            PacketFieldMetadata(name="version", disp="ver.", length=1),
            PacketFieldMetadata(name="type_", disp="type", length=1),
            PacketFieldMetadata(name="network_id", disp="net", length=2),
            PacketFieldMetadata(name="destination", disp="dst", length=8),
            PacketFieldMetadata(name="source", disp="src", length=8),
            PacketFieldMetadata(name="next_proto", disp="proto", length=1),
        ]
    )
    version: int = MARI_PROTOCOL_VERSION
    type_: int = PacketType.DATA
    network_id: int = MARI_NET_ID_DEFAULT
    destination: int = MARI_BROADCAST_ADDRESS
    source: int = 0x0000000000000000
    next_proto: int = NextProto.MARI_INTERNAL

    def __repr__(self):
        type_ = PacketType(self.type_).name
        try:
            next_proto_name = NextProto(self.next_proto).name
        except ValueError:
            next_proto_name = f"0x{self.next_proto:02x}"
        return (
            f"Header(version={self.version}, type_={type_}, "
            f"network_id=0x{self.network_id:04x}, "
            f"destination=0x{self.destination:016x}, "
            f"source=0x{self.source:016x}, next_proto={next_proto_name})"
        )


@dataclass
class Frame:
    """Data class that holds a payload packet."""

    header: Header = None
    payload: bytes = b""

    def from_bytes(self, bytes_):
        self.header = Header().from_bytes(bytes_[0:21])
        if len(bytes_) > 21:
            self.payload = bytes_[21:]
        return self

    def to_bytes(self, byteorder="little") -> bytes:
        return self.header.to_bytes(byteorder) + self.payload

    @property
    def is_test_packet(self) -> bool:
        """Returns True if either the payload is a metrics response, request, or load test packet."""
        return (
            self.payload.startswith(DefaultPayloadType.METRICS_RESPONSE.as_bytes())
            or self.payload.startswith(DefaultPayloadType.METRICS_REQUEST.as_bytes())
            or self.payload.startswith(DefaultPayloadType.METRICS_LOAD.as_bytes())
            or self.payload.startswith(DefaultPayloadType.METRICS_PROBE.as_bytes())
        )

    @property
    def is_load_test_packet(self) -> bool:
        return self.payload.startswith(DefaultPayloadType.METRICS_LOAD.as_bytes())

    def __repr__(self):
        header_no_metadata = dataclasses.replace(self.header, metadata=[])
        return f"Frame(header={header_no_metadata}, payload={self.payload})"
