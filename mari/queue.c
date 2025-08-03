/**
 * @file
 * @ingroup     queue
 *
 * @brief       Packet queue management
 *
 * @author Geovane Fedrecheski <geovane.fedrecheski@inria.fr>
 *
 * @copyright Inria, 2024
 */

#include <nrf.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>

#include "packet.h"
#include "mac.h"
#include "scheduler.h"
#include "association.h"
#include "bloom.h"
#include "queue.h"
#include "sec.h"
#include "mari.h"

//=========================== defines ==========================================

typedef struct {
    uint8_t length;
    uint8_t buffer[MARI_PACKET_MAX_SIZE];
} mr_packet_t;

typedef struct {
    uint8_t     current;  ///< Current position in the queue
    uint8_t     last;     ///< Position of the last item added in the queue
    mr_packet_t packets[MARI_PACKET_QUEUE_SIZE];
} mari_packet_queue_t;

typedef struct {
    mari_packet_queue_t packet_queue;
    mr_packet_t         join_packet;
} queue_vars_t;

//=========================== variables ========================================

static queue_vars_t queue_vars = { 0 };

//=========================== prototypes =======================================

//=========================== public ===========================================

uint8_t mr_queue_next_packet(slot_type_t slot_type, uint8_t *packet) {
    uint8_t len = 0;

    if (mari_get_node_type() == MARI_GATEWAY) {
        if (slot_type == SLOT_TYPE_BEACON) {
            // prepare a beacon packet with current asn, remaining capacity and active schedule id
            len = mr_build_packet_beacon(
                packet,
                mr_assoc_get_network_id(),
                mr_mac_get_asn(),
                mr_scheduler_gateway_remaining_capacity(),
                mr_scheduler_get_active_schedule_id());
            if (mr_bloom_gateway_is_available()) {
                len += mr_bloom_gateway_copy(packet + sizeof(mr_beacon_packet_header_t));
            }
        } else if (slot_type == SLOT_TYPE_DOWNLINK) {
            if (mr_queue_has_join_packet()) {
                len = mr_queue_get_join_packet(packet);
            } else {
                // load a packet from the queue, if any is available
                len = mr_queue_peek(packet);
                if (len) {
                    // actually pop the packet from the queue
                    mr_queue_pop();
                }
            }
        }
    } else if (mari_get_node_type() == MARI_NODE) {
        if (slot_type == SLOT_TYPE_SHARED_UPLINK) {
            if (!mr_assoc_is_joined() && mr_assoc_node_ready_to_join()) {
                mr_assoc_node_start_joining();
                len = mr_queue_get_join_packet(packet);
            }
        } else if (slot_type == SLOT_TYPE_UPLINK) {
            // load a packet from the queue, if any is available
            len = mr_queue_peek(packet);
            if (len) {
                // actually pop the packet from the queue
                mr_queue_pop();
            } else if (MARI_AUTO_UPLINK_KEEPALIVE) {
                // send a keepalive packet
                len = mr_build_packet_keepalive(packet, mr_mac_get_synced_gateway());
            }
        }
    }

    return len;
}

void mr_queue_add(uint8_t *packet, uint8_t length) {
    // enqueue for transmission
    memcpy(queue_vars.packet_queue.packets[queue_vars.packet_queue.last].buffer, packet, length);
    queue_vars.packet_queue.packets[queue_vars.packet_queue.last].length = length;
    // increment the `last` index
    queue_vars.packet_queue.last = (queue_vars.packet_queue.last + 1) % MARI_PACKET_QUEUE_SIZE;
}

uint8_t mr_queue_peek(uint8_t *packet) {
    if (queue_vars.packet_queue.current == queue_vars.packet_queue.last) {
        return 0;
    }

    memcpy(packet, queue_vars.packet_queue.packets[queue_vars.packet_queue.current].buffer, queue_vars.packet_queue.packets[queue_vars.packet_queue.current].length);
    // do not increment the `current` index here, as this is just a peek
    return queue_vars.packet_queue.packets[queue_vars.packet_queue.current].length;
}

bool mr_queue_pop(void) {
    if (queue_vars.packet_queue.current == queue_vars.packet_queue.last) {
        return false;
    } else {
        // increment the `current` index
        queue_vars.packet_queue.current = (queue_vars.packet_queue.current + 1) % MARI_PACKET_QUEUE_SIZE;
        return true;
    }
}

void mr_queue_set_join_request(uint64_t node_id) {
    queue_vars.join_packet.length = mr_build_packet_join_request_with_m1(queue_vars.join_packet.buffer, node_id);
}

void mr_queue_set_join_response(uint64_t node_id, uint8_t assigned_cell_id) {
    uint8_t len                          = mr_build_packet_join_response(queue_vars.join_packet.buffer, node_id);
    queue_vars.join_packet.buffer[len++] = assigned_cell_id;
    queue_vars.join_packet.length        = len;
}

bool mr_queue_has_join_packet(void) {
    return queue_vars.join_packet.length > 0;
}

// if used by the node, gets it a join request packet
// if used by the gateway, gets it a join response packet
uint8_t mr_queue_get_join_packet(uint8_t *packet) {
    memcpy(packet, queue_vars.join_packet.buffer, queue_vars.join_packet.length);
    uint8_t len = queue_vars.join_packet.length;

    // clear the join request
    queue_vars.join_packet.length = 0;

    return len;
}
