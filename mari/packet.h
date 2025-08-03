#ifndef __PACKET_H
#define __PACKET_H

/**
 * @ingroup     mari
 * @brief       Packet format and building functions
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

//=========================== defines ==========================================

#define MARI_PROTOCOL_VERSION 2

#define MARI_NET_ID_PATTERN_ANY 0
#define MARI_NET_ID_DEFAULT     1

//=========================== variables ========================================

typedef enum {
    MARI_PACKET_BEACON        = 1,
    MARI_PACKET_JOIN_REQUEST  = 2,
    MARI_PACKET_JOIN_RESPONSE = 4,
    MARI_PACKET_KEEPALIVE     = 8,
    MARI_PACKET_DATA          = 16,
} mr_packet_type_t;

typedef struct __attribute__((packed)) {
    int8_t rssi;
} mr_packet_statistics_t;

// general packet header
typedef struct __attribute__((packed)) {
    uint8_t                version;
    mr_packet_type_t       type;
    uint16_t               network_id;
    uint64_t               dst;
    uint64_t               src;
    mr_packet_statistics_t stats;
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
} mr_beacon_packet_header_t;

typedef enum {
    MARI_EDGE_NODE_JOINED  = 1,
    MARI_EDGE_NODE_LEFT    = 2,
    MARI_EDGE_DATA         = 3,
    MARI_EDGE_KEEPALIVE    = 4,
    MARI_EDGE_GATEWAY_INFO = 5,
} mr_gateway_edge_type_t;

//=========================== prototypes =======================================

size_t mr_build_packet_data(uint8_t *buffer, uint64_t dst, uint8_t *data, size_t data_len);

size_t mr_build_packet_join_request(uint8_t *buffer, uint64_t dst);
size_t mr_build_packet_join_request_with_m1(uint8_t *buffer, uint64_t dst);

size_t mr_build_packet_join_response(uint8_t *buffer, uint64_t dst);

size_t mr_build_packet_keepalive(uint8_t *buffer, uint64_t dst);

size_t mr_build_packet_beacon(uint8_t *buffer, uint16_t net_id, uint64_t asn, uint8_t remaining_capacity, uint8_t active_schedule_id);

size_t mr_build_uart_packet_gateway_info(uint8_t *buffer);

#endif
