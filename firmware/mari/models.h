#ifndef __MODELS_H
#define __MODELS_H

/**
 * @defgroup    models      common models
 * @ingroup     mari
 * @brief       Common models used in the mari protocol
 *
 * @{
 * @file
 * @author Geovane Fedrecheski <geovane.fedrecheski@inria.fr>
 * @copyright Inria, 2024-now
 * @}
 */

#include <stdint.h>
#include <stdlib.h>
#include <nrf.h>
#include <stdbool.h>

#include "bloom.h"

//=========================== defines =========================================

#define MARI_N_BLE_REGULAR_CHANNELS     37
#define MARI_N_BLE_ADVERTISING_CHANNELS 3

// #ifndef MARI_FIXED_CHANNEL
#define MARI_FIXED_CHANNEL      0   // to hardcode the channel, use a valid value other than 0
#define MARI_FIXED_SCAN_CHANNEL 37  // to hardcode the channel, use a valid value other than 0
// #endif

// Beacon/scan channel hopping. When defined, the gateway transmits each beacon
// cell on its own BLE advertising channel (cell offset 0 -> 37, 1 -> 38,
// 2 -> 39), synced nodes listen on the matching channel, and a scanning node
// rotates its listen channel one per scan round - giving the beacon/join path
// the frequency diversity the data slots already have. Comment out to keep all
// beacons and scanning on MARI_FIXED_SCAN_CHANNEL (A/B comparison).
#define MARI_ENABLE_BEACON_HOPPING

#define MARI_N_CELLS_MAX 149

#define MARI_ENABLE_BACKGROUND_SCAN 1

#define MARI_PACKET_MAX_SIZE 255

#define MARI_STATS_SCHED_USAGE_SIZE 4  // supports schedules with up to 256 cells

//=========================== types ============================================

// -------- types sent over the air --------

typedef enum {
    MARI_PACKET_BEACON        = 1,
    MARI_PACKET_JOIN_REQUEST  = 2,
    MARI_PACKET_JOIN_RESPONSE = 4,
    MARI_PACKET_KEEPALIVE     = 8,
    MARI_PACKET_DATA          = 16,
} mr_packet_type_t;

// Upper-layer protocol multiplex (EtherType / PPP Protocol / IPv6 Next
// Header analogue). Meaningful when type == MARI_PACKET_DATA; receivers
// dispatch the payload to the owner of the indicated namespace. For
// MARI_PACKET_{BEACON,JOIN_*,KEEPALIVE} the field is always
// MARI_NEXT_PROTO_MARI_INTERNAL — those frames are mari-managed by
// definition.
//
typedef enum {
    MARI_NEXT_PROTO_RESERVED = 0x00,  // null catcher (zeroed / uninitialized memory)

    MARI_NEXT_PROTO_MARI_INTERNAL = 0x01,  // mari's own control + metrics
    // 0x02..0x09 reserved for mari extensions

    MARI_NEXT_PROTO_SWARMIT_TESTBED = 0x10,  // SwarmIT testbed protocol
    MARI_NEXT_PROTO_DOTBOT_APP      = 0x11,  // DotBot application protocol
    // 0x12..0x39 reserved for custom app protocols

    MARI_NEXT_PROTO_IPV4 = 0xA0,  // IPv4 packet (RFC 791)
    MARI_NEXT_PROTO_IPV6 = 0xA1,  // IPv6 packet (RFC 8200)
    // 0xA2..0xFD reserved for standardized network protocols

    MARI_NEXT_PROTO_EXPERIMENTAL = 0xFE,  // experimental / private use
    MARI_NEXT_PROTO_UNKNOWN      = 0xFF,  // unclassified upper-layer protocol; default when next_proto unset
} mr_next_proto_t;

// Per-frame send configuration. Passed by pointer to the sender API so
// new fields can be added without breaking call sites — pattern borrowed
// from Linux sendmsg(2), Zephyr net_pkt, OpenThread otMessageSettings.
// Future fields likely to land here: priority, retry policy, TTL in
// slotframes, request for link-layer security, …
typedef struct {
    mr_next_proto_t next_proto;
} mari_tx_config_t;

// Default config for mari's own internal traffic (metrics, control). Other
// consumers (DotBot apps, SwarmIT, IP stacks) define their own mari_tx_config_t
// constants per the high-nibble category partition above.
static const mari_tx_config_t MARI_TX_INTERNAL = {
    .next_proto = MARI_NEXT_PROTO_MARI_INTERNAL,
};

// general packet header
typedef struct __attribute__((packed)) {
    uint8_t          version;
    mr_packet_type_t type;
    uint16_t         network_id;
    uint64_t         dst;
    uint64_t         src;
    mr_next_proto_t  next_proto;
} mr_packet_header_t;

// beacon packet
typedef struct __attribute__((packed)) {
    uint8_t          version;
    mr_packet_type_t type;
    uint16_t         network_id;
    uint64_t         asn;
    uint64_t         src;
    uint8_t          remaining_capacity;
    uint8_t          active_schedule_id;
    uint8_t          bloom_filter[MARI_BLOOM_M_BYTES];
} mr_beacon_packet_header_t;

// -------- types used internally --------

typedef enum {
    MARI_GATEWAY = 'G',
    MARI_NODE    = 'D',
} mr_node_type_t;

typedef enum {
    MARI_NEW_PACKET = 1,
    MARI_CONNECTED,
    MARI_DISCONNECTED,
    MARI_NODE_JOINED,
    MARI_NODE_LEFT,
    MARI_KEEPALIVE,
    MARI_ERROR,
} mr_event_t;

typedef enum {
    MARI_NONE              = 0,
    MARI_HANDOVER          = 1,
    MARI_OUT_OF_SYNC       = 2,
    MARI_PEER_LOST         = 3,  // deprecated
    MARI_GATEWAY_FULL      = 4,
    MARI_PEER_LOST_TIMEOUT = 5,
    MARI_PEER_LOST_BLOOM   = 6,
    MARI_HANDOVER_FAILED   = 7,
} mr_event_tag_t;

// Node-measured handover outage, reported upstream in the metrics probe (see
// mari_node_get_handover_info). Tracked by the association module on the node's
// free-running timer, so it is free of ASN re-sync, uplink quantization, and
// the gateway leave-timeout.
typedef struct {
    uint16_t duration_ms;  ///< last outage duration in ms, saturating at 0xFFFF
    uint8_t  seq;          ///< +1 per completed handover; lets the host dedup
    uint8_t  reason;       ///< mr_event_tag_t cause of the last handover
} mr_handover_info_t;

typedef struct {
    uint8_t             len;
    mr_packet_header_t *header;
    uint8_t            *payload;
    uint8_t             payload_len;
} mari_packet_t;

typedef struct {
    union {
        mari_packet_t new_packet;
        struct {
            uint64_t node_id;
        } node_info;
        struct {
            uint64_t gateway_id;
        } gateway_info;
    } data;
    mr_event_tag_t tag;
} mr_event_data_t;

typedef enum {
    MARI_RADIO_ACTION_SLEEP = 'S',
    MARI_RADIO_ACTION_RX    = 'R',
    MARI_RADIO_ACTION_TX    = 'T',
} mr_radio_action_t;

typedef enum {
    SLOT_TYPE_BEACON        = 'B',
    SLOT_TYPE_SHARED_UPLINK = 'S',
    SLOT_TYPE_DOWNLINK      = 'D',
    SLOT_TYPE_UPLINK        = 'U',
} slot_type_t;

typedef struct {
    mr_radio_action_t radio_action;
    uint8_t           channel;
    slot_type_t       type;
} mr_slot_info_t;

typedef struct {
    slot_type_t type;
    uint8_t     channel_offset;
    uint64_t    assigned_node_id;
    uint64_t    last_received_asn;  ///< ASN marking the last time the node was heard from
    uint64_t    bloom_h1;           ///< H1 hash of the node ID, used to compute the bloom filter
    uint64_t    bloom_h2;           ///< H2 hash of the node ID, used to compute the bloom filter
} cell_t;

typedef struct {
    uint8_t id;                       // unique identifier for the schedule
    uint8_t max_nodes;                // maximum number of nodes that can be scheduled, equivalent to the number of uplink slot_durations
    uint8_t backoff_n_min;            // unused; the MAC uses MARI_BACKOFF_N_MIN in association.c
    uint8_t backoff_n_max;            // unused; the MAC uses MARI_BACKOFF_N_MAX in association.c
    size_t  n_cells;                  // number of cells in this schedule
    cell_t  cells[MARI_N_CELLS_MAX];  // cells in this schedule. NOTE(FIXME?): the first 3 cells must be beacons
} schedule_t;

typedef struct {
    uint8_t  channel;
    int8_t   rssi;
    uint32_t start_ts;
    uint32_t end_ts;
    uint64_t asn;
    bool     to_me;
    uint8_t  packet[MARI_PACKET_MAX_SIZE];
    uint8_t  packet_len;
} mr_received_packet_t;

// -------- types used for UART --------

typedef enum {
    MARI_EDGE_NODE_JOINED  = 1,
    MARI_EDGE_NODE_LEFT    = 2,
    MARI_EDGE_DATA         = 3,
    MARI_EDGE_KEEPALIVE    = 4,
    MARI_EDGE_GATEWAY_INFO = 5,
} mr_gateway_edge_type_t;

// Cumulative gateway-side counters for the host UART link and for the two
// inter-core mailboxes. They only reset on reboot, so a host reads rates by
// differencing consecutive gateway_info packets. Every field is owned by
// exactly one core, which is the producer on the ring it reports: the app core
// owns all of them except ipc_r2u_lost, since only the net core is in a
// position to observe a message it could not hand over.
typedef struct __attribute__((packed)) {
    uint32_t uart_rx_bytes;       ///< Bytes the UARTE moved from the wire into RAM
    uint32_t uart_rx_frames_ok;   ///< HDLC frames that passed the FCS check
    uint32_t uart_rx_hdlc_err;    ///< HDLC frames the decoder rejected
    uint32_t uart_rx_hw_overrun;  ///< ERRORSRC.OVERRUN: the internal RX FIFO dropped a byte
    uint32_t uart_rx_hw_framing;  ///< ERRORSRC.FRAMING
    uint32_t uart_rx_hw_break;    ///< ERRORSRC.BREAK
    uint32_t uart_rx_slot_full;   ///< DMA buffers discarded because the consumer had not freed a slot
    uint32_t uart_tx_queue_drop;  ///< Uplink frames dropped because the TX queue was full
    uint32_t ipc_u2r_lost;        ///< Downlink messages lost between the app and the net core
    uint32_t ipc_r2u_lost;        ///< Uplink messages lost between the net and the app core
} mr_gateway_uart_stats_t;

// uart packet for gateway info
typedef struct __attribute__((packed)) {
    uint8_t                 version;
    uint64_t                device_id;
    uint16_t                net_id;
    uint16_t                schedule_id;
    uint64_t                sched_usage[MARI_STATS_SCHED_USAGE_SIZE];
    uint64_t                asn;
    uint32_t                timer;
    mr_gateway_uart_stats_t uart_stats;
} mr_uart_packet_gateway_info_t;

// -------- types used for metrics collection --------

typedef enum {
    MARI_PAYLOAD_TYPE_METRICS_PROBE = 0x9C,
} mr_metrics_payload_type_t;

typedef struct __attribute__((packed)) {
    mr_metrics_payload_type_t type;  ///< Payload type (1 byte)

    uint64_t cloud_tx_ts_us;        ///< Cloud transmit timestamp in microseconds (8 bytes)
    uint64_t cloud_rx_ts_us;        ///< Cloud receive timestamp in microseconds (8 bytes)
    uint32_t cloud_tx_count;        ///< Cloud transmit counter (4 bytes)
    uint32_t cloud_rx_count;        ///< Cloud receive counter (4 bytes)
    uint64_t edge_tx_ts_us;         ///< Edge transmit timestamp in microseconds (8 bytes)
    uint64_t edge_rx_ts_us;         ///< Edge receive timestamp in microseconds (8 bytes)
    uint32_t edge_tx_count;         ///< Edge transmit counter (4 bytes)
    uint32_t edge_rx_count;         ///< Edge receive counter (4 bytes)
    uint32_t gw_tx_count;           ///< Gateway transmit counter (4 bytes)
    uint32_t gw_rx_count;           ///< Gateway receive counter (4 bytes)
    uint64_t gw_rx_asn;             ///< Gateway receive ASN (8 bytes)
    uint64_t gw_tx_enqueued_asn;    ///< Gateway TX enqueued ASN (8 bytes)
    uint64_t gw_tx_dequeued_asn;    ///< Gateway TX dequeued ASN (8 bytes)
    uint32_t node_rx_count;         ///< Node receive counter (4 bytes)
    uint32_t node_tx_count;         ///< Node transmit counter (4 bytes)
    uint64_t node_rx_asn;           ///< Node receive ASN (8 bytes)
    uint64_t node_tx_enqueued_asn;  ///< Node TX enqueued ASN (8 bytes)
    uint64_t node_tx_dequeued_asn;  ///< Node TX dequeued ASN (8 bytes)
    int8_t   rssi_at_node;          ///< RSSI at node in dBm (1 byte, signed)
    int8_t   rssi_at_gw;            ///< RSSI at gateway in dBm (1 byte, signed)

    uint16_t node_handover_duration_ms;  ///< Node-measured last handover duration in ms, saturating at 0xFFFF (2 bytes)
    uint8_t  node_handover_seq;          ///< Node handover counter, +1 per completed handover; lets the host dedup (1 byte)
    uint8_t  node_handover_reason;       ///< mr_event_tag_t reason for the last handover (1 byte)
} mr_metrics_payload_t;

//=========================== callbacks =======================================

typedef void (*mr_event_cb_t)(mr_event_t event, mr_event_data_t event_data);

#endif  // __MODELS_H
