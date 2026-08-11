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

#include "models.h"
#include "scheduler.h"

//=========================== defines ==========================================

#define MARI_PROTOCOL_VERSION 4

#define MARI_NET_ID_PATTERN_ANY 0
#define MARI_NET_ID_DEFAULT     1

//=========================== prototypes =======================================

size_t mr_build_packet_data(uint8_t                *buffer,
                            uint64_t                dst,
                            uint8_t                *data,
                            size_t                  data_len,
                            const mari_tx_config_t *cfg);

size_t mr_build_packet_join_request(uint8_t *buffer, uint64_t dst);

size_t mr_build_packet_join_response(uint8_t *buffer, uint64_t dst);

size_t mr_build_packet_keepalive(uint8_t *buffer, uint64_t dst);

size_t mr_build_packet_beacon(uint8_t *buffer, uint16_t net_id, uint64_t asn, uint8_t remaining_capacity, uint8_t active_schedule_id);

size_t mr_build_uart_packet_gateway_info(uint8_t *buffer, const mr_gateway_uart_stats_t *uart_stats);

#endif
