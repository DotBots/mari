/**
 * @file
 * @ingroup     mari_assoc
 *
 * @brief       Association procedure for the mari protocol
 *
 * @author Geovane Fedrecheski <geovane.fedrecheski@inria.fr>
 *
 * @copyright Inria, 2025
 */

#include <nrf.h>
#include <stdint.h>
#include <stdio.h>
#include <stdbool.h>

#include "mr_device.h"
#include "mr_radio.h"
#include "mr_timer_hf.h"
#include "mr_rng.h"
#include "association.h"
#include "scan.h"
#include "mac.h"
#include "packet.h"
#include "mari.h"
#include "scheduler.h"
#include "bloom.h"
#include "sec.h"
#include "queue.h"

//=========================== debug ============================================

#ifndef DEBUG  // FIXME: remove before merge. Just to make VS Code enable code behind `#ifdef DEBUG`
#define DEBUG
#endif

#ifdef DEBUG
#include "mr_gpio.h"  // for debugging
// the 4 LEDs of the nRF52840-DK or the nRF5340-DK
#ifdef NRF52840_XXAA
mr_gpio_t led0 = { .port = 0, .pin = 13 };
mr_gpio_t led1 = { .port = 0, .pin = 14 };
mr_gpio_t led2 = { .port = 0, .pin = 15 };
mr_gpio_t led3 = { .port = 0, .pin = 16 };
#else
mr_gpio_t led0 = { .port = 0, .pin = 28 };
mr_gpio_t led1 = { .port = 0, .pin = 29 };
mr_gpio_t led2 = { .port = 0, .pin = 30 };
mr_gpio_t led3 = { .port = 0, .pin = 31 };
#endif
#define DEBUG_GPIO_TOGGLE(pin) mr_gpio_toggle(pin)
#define DEBUG_GPIO_SET(pin)    mr_gpio_set(pin)
#define DEBUG_GPIO_CLEAR(pin)  mr_gpio_clear(pin)
#else
// No-op when DEBUG is not defined
#define DEBUG_GPIO_TOGGLE(pin) ((void)0))
#define DEBUG_GPIO_SET(pin) ((void)0))
#define DEBUG_GPIO_CLEAR(pin) ((void)0))
#endif  // DEBUG

//=========================== defines =========================================

#define MARI_BACKOFF_N_MIN 5
#define MARI_BACKOFF_N_MAX 9

#define MARI_JOIN_TIMEOUT_SINCE_SYNCED (1000 * 1000 * 5)  // 5 seconds. after this time, go back to scanning. NOTE: have it be based on slotframe size?

// after this amount of time, consider that a join request failed (very likely due to a collision during the shared uplink slot)
// currently set to 2 slot durations -- enough when the schedule always have a shared-uplink followed by a downlink,
// and the gateway prioritizes join responses over all other downstream packets
#define MARI_JOINING_STATE_TIMEOUT ((MARI_WHOLE_SLOT_DURATION * (2 - 1)) + (MARI_WHOLE_SLOT_DURATION / 2))  // apply a half-slot duration just so that the timeout happens before the slot boundary

typedef struct {
    mr_assoc_state_t state;
    mr_event_cb_t    mari_event_callback;
    uint32_t         last_state_change_ts;  ///< Last time the state changed
    uint16_t         network_id;            ///< If gateway, puts it in the beacon packet. If node, uses it to filter beacons (0 means accept any network)

    // node
    uint32_t       last_received_from_gateway_asn;  ///< Last received packet when in joined state
    int16_t        backoff_n;
    uint8_t        backoff_random_time;                ///< Number of slots to wait before re-trying to join
    uint32_t       join_response_timeout_ts;           ///< Time when the node will give up joining
    uint16_t       synced_gateway_remaining_capacity;  ///< Number of nodes that my gateway can still accept
    mr_event_tag_t is_pending_disconnect;              ///< Whether the node is pending a disconnect
} assoc_vars_t;

//=========================== variables =======================================

assoc_vars_t assoc_vars = { 0 };

//=========================== prototypes ======================================

//=========================== public ==========================================

void mr_assoc_init(uint16_t net_id, mr_event_cb_t event_callback) {
#ifdef DEBUG
    mr_gpio_init(&led0, MR_GPIO_OUT);
    mr_gpio_init(&led1, MR_GPIO_OUT);
    mr_gpio_init(&led2, MR_GPIO_OUT);
    mr_gpio_init(&led3, MR_GPIO_OUT);
    // remember: the LEDs are active low
#endif

    assoc_vars.network_id          = net_id;
    assoc_vars.mari_event_callback = event_callback;
    mr_assoc_set_state(JOIN_STATE_IDLE);

    // init backoff things
    mr_rng_init();
    mr_assoc_node_reset_backoff();
}

inline void mr_assoc_set_state(mr_assoc_state_t state) {
    assoc_vars.state                = state;
    assoc_vars.last_state_change_ts = mr_timer_hf_now(MARI_TIMER_DEV);

#ifdef DEBUG
    DEBUG_GPIO_SET(&led0);
    DEBUG_GPIO_SET(&led1);
    DEBUG_GPIO_SET(&led2);
    DEBUG_GPIO_SET(&led3);
    switch (state) {
        case JOIN_STATE_IDLE:
            // DEBUG_GPIO_CLEAR(&pin1);
            break;
        case JOIN_STATE_SCANNING:
            // DEBUG_GPIO_SET(&pin1);
            DEBUG_GPIO_CLEAR(&led0);
            break;
        case JOIN_STATE_SYNCED:
            // DEBUG_GPIO_CLEAR(&pin1);
            DEBUG_GPIO_CLEAR(&led1);
            break;
        case JOIN_STATE_JOINING:
            DEBUG_GPIO_CLEAR(&led2);
            break;
        case JOIN_STATE_JOINED:
            DEBUG_GPIO_CLEAR(&led3);
            break;
        default:
            break;
    }
#endif
}

mr_assoc_state_t mr_assoc_get_state(void) {
    return assoc_vars.state;
}

bool mr_assoc_is_joined(void) {
    return assoc_vars.state == JOIN_STATE_JOINED;
}

uint16_t mr_assoc_get_network_id(void) {
    if (mari_get_node_type() == MARI_GATEWAY) {
        return assoc_vars.network_id;
    } else {
        return mr_mac_get_synced_network_id();
    }
}

// ------------ node functions ------------

void mr_assoc_node_handle_synced(void) {
    mr_assoc_set_state(JOIN_STATE_SYNCED);
    mr_assoc_node_reset_backoff();
    mr_sec_edhoc_set_state(EDHOC_M1_PENDING);
}

bool mr_assoc_node_ready_to_join(void) {
    bool assoc_ready = assoc_vars.state == JOIN_STATE_SYNCED && assoc_vars.backoff_random_time == 0;
    bool sec_ready   = mr_sec_edhoc_is_m1_ready();
    return assoc_ready && sec_ready;
}

void mr_assoc_node_start_joining(void) {
    uint32_t now_ts                     = mr_timer_hf_now(MARI_TIMER_DEV);
    assoc_vars.join_response_timeout_ts = now_ts + MARI_JOINING_STATE_TIMEOUT;
    mr_queue_set_join_request(mr_mac_get_synced_gateway());
    mr_assoc_set_state(JOIN_STATE_JOINING);
}

void mr_assoc_node_handle_joined(uint64_t gateway_id) {
    mr_assoc_set_state(JOIN_STATE_JOINED);
    mr_event_data_t event_data = { .data.gateway_info.gateway_id = gateway_id };
    assoc_vars.mari_event_callback(MARI_CONNECTED, event_data);
    assoc_vars.is_pending_disconnect = MARI_NONE;        // reset the pending disconnect flag
    mr_assoc_node_keep_gateway_alive(mr_mac_get_asn());  // initialize the gateway's keep-alive
    mr_assoc_node_reset_backoff();
}

bool mr_assoc_node_handle_failed_join(void) {
    if (assoc_vars.synced_gateway_remaining_capacity > 0) {
        mr_assoc_set_state(JOIN_STATE_SYNCED);
        mr_assoc_node_register_collision_backoff();
        mr_queue_set_join_request(mr_mac_get_synced_gateway());  // put a join request packet back on queue
        return true;
    } else {
        // no more capacity, go back to scanning
        mr_assoc_node_handle_give_up_joining();
        return false;
    }
}

void mr_assoc_node_handle_give_up_joining(void) {
    mr_assoc_set_state(JOIN_STATE_IDLE);
    mr_assoc_node_reset_backoff();
}

bool mr_assoc_node_too_long_waiting_for_join_response(void) {
    // joining state timeout is computed since the time the node sent a join request
    if (assoc_vars.state != JOIN_STATE_JOINING) {
        // can only reach timeout when in synced or joining state
        return false;
    }

    uint32_t now_ts = mr_timer_hf_now(MARI_TIMER_DEV);
    return now_ts > assoc_vars.join_response_timeout_ts;
}

bool mr_assoc_node_too_long_synced_without_joining(void) {
    // timeout is computed since the time the node synced with the gateway
    // this encompasses potentially many join attempts, including time spent waiting due to backoff
    if (assoc_vars.state != JOIN_STATE_SYNCED && assoc_vars.state != JOIN_STATE_JOINING) {
        // can only reach join timeout when in synced or joining state
        return false;
    }

    uint32_t now_ts    = mr_timer_hf_now(MARI_TIMER_DEV);
    uint32_t synced_ts = mr_mac_get_synced_ts();
    return now_ts - synced_ts > MARI_JOIN_TIMEOUT_SINCE_SYNCED;
}

void mr_assoc_node_reset_backoff(void) {
    assoc_vars.backoff_n           = -1;
    assoc_vars.backoff_random_time = 0;
}

void mr_assoc_node_tick_backoff(void) {
    if (assoc_vars.backoff_random_time > 0) {
        assoc_vars.backoff_random_time--;
    }
}

void mr_assoc_node_register_collision_backoff(void) {
    if (assoc_vars.backoff_n == -1) {
        // initialize backoff
        assoc_vars.backoff_n = MARI_BACKOFF_N_MIN;
    } else {
        // increment the n in [0, 2^n - 1], but only if n is less than the max
        uint8_t new_n        = assoc_vars.backoff_n + 1;
        assoc_vars.backoff_n = new_n < MARI_BACKOFF_N_MAX ? new_n : MARI_BACKOFF_N_MAX;
    }
    // choose a random number from [0, 2^n - 1] and set it as the backoff time
    uint16_t max = (1 << assoc_vars.backoff_n) - 1;

    // read 2 bytes from the RNG
    uint8_t raw_low, raw_high;
    mr_rng_read(&raw_low);
    mr_rng_read(&raw_high);
    // combine the two bytes into a 16-bit number (we need 16 bits because MARI_BACKOFF_N_MAX > 8)
    uint16_t raw = ((uint16_t)raw_high << 8) | (uint16_t)raw_low;

    // now, make sure random number is in the interval [0, max]
    // using modulo does not give perfect uniformity,
    // but it is much faster than an exhaustive search, and good enough for our purpose
    assoc_vars.backoff_random_time = (raw % (max + 1));
}

bool mr_assoc_node_should_leave(uint32_t asn) {
    if (assoc_vars.state != JOIN_STATE_JOINED) {
        // can only lose the gateway when already joined
        return false;
    }

    if (assoc_vars.is_pending_disconnect != MARI_NONE) {
        // anything other than MARI_NONE means that the node is pending a disconnect
        return true;
    }

    bool gateway_is_lost = (asn - assoc_vars.last_received_from_gateway_asn) > mr_scheduler_get_active_schedule_slot_count() * MARI_MAX_SLOTFRAMES_NO_RX_LEAVE;
    if (gateway_is_lost) {
        // too long since last received from the gateway, consider it lost
        assoc_vars.is_pending_disconnect = MARI_PEER_LOST_TIMEOUT;
        return true;
    }

    return false;
}

void mr_assoc_node_keep_gateway_alive(uint64_t asn) {
    assoc_vars.last_received_from_gateway_asn = asn;
}

void mr_assoc_node_handle_disconnect(void) {
    mr_assoc_set_state(JOIN_STATE_IDLE);
    mr_scheduler_node_deassign_myself_from_schedule();
    mr_event_data_t event_data = {
        .data.gateway_info.gateway_id = mr_mac_get_synced_gateway(),
        .tag                          = assoc_vars.is_pending_disconnect
    };
    assoc_vars.mari_event_callback(MARI_DISCONNECTED, event_data);
}

bool mr_assoc_node_matches_network_id(uint16_t network_id) {
    if (assoc_vars.network_id == MARI_NET_ID_PATTERN_ANY) {
        // accept any network id
        return true;
    }
    // in the future, some prefix logic could be added here
    return assoc_vars.network_id == network_id;
}

// ------------ gateway functions ---------

bool mr_assoc_gateway_node_is_joined(uint64_t node_id) {
    schedule_t *schedule = mr_scheduler_get_active_schedule_ptr();
    for (size_t i = 0; i < schedule->n_cells; i++) {
        if (schedule->cells[i].type != SLOT_TYPE_UPLINK) {
            // we only care about uplink cells
            continue;
        }
        if (schedule->cells[i].assigned_node_id == node_id) {
            // this node is assigned to a cell, so it is joined
            return true;
        }
    }
    return false;
}

bool mr_assoc_gateway_keep_node_alive(uint64_t node_id, uint64_t asn) {
    // save the asn of the last packet received from a certain node_id
    schedule_t *schedule = mr_scheduler_get_active_schedule_ptr();
    for (size_t i = 0; i < schedule->n_cells; i++) {
        if (schedule->cells[i].type != SLOT_TYPE_UPLINK) {
            // we only care about uplink cells
            continue;
        }
        if (schedule->cells[i].assigned_node_id == node_id) {
            // save the asn so we know this node is alive
            schedule->cells[i].last_received_asn = asn;
        }
    }
    // should never reach here
    return false;
}

void mr_assoc_gateway_clear_old_nodes(uint64_t asn) {
    // clear all nodes that have not been heard from in the last N asn
    // also deassign the cells from the scheduler
    uint64_t max_asn_old = mr_scheduler_get_active_schedule_slot_count() * MARI_MAX_SLOTFRAMES_NO_RX_LEAVE;

    schedule_t *schedule = mr_scheduler_get_active_schedule_ptr();
    for (size_t i = 0; i < schedule->n_cells; i++) {
        if (schedule->cells[i].type != SLOT_TYPE_UPLINK) {
            // we only care about uplink cells
            continue;
        }
        cell_t *cell = &schedule->cells[i];
        if (cell->assigned_node_id != 0 && asn - cell->last_received_asn > max_asn_old) {
            mr_event_data_t event_data = (mr_event_data_t){ .data.node_info.node_id = cell->assigned_node_id, .tag = MARI_PEER_LOST_TIMEOUT };
            // inform the scheduler
            mr_scheduler_gateway_decrease_nodes_counter();
            // clear the cell
            cell->assigned_node_id  = NULL;
            cell->last_received_asn = 0;
            // inform the application
            assoc_vars.mari_event_callback(MARI_NODE_LEFT, event_data);
        }
    }
}

// ------------ packet handlers -------

void mr_assoc_handle_beacon(uint8_t *packet, uint8_t length, uint8_t channel, uint32_t ts) {
    (void)length;

    if (packet[1] != MARI_PACKET_BEACON) {
        return;
    }

    // now that we know it's a beacon packet, parse and process it
    mr_beacon_packet_header_t *beacon = (mr_beacon_packet_header_t *)packet;

    if (beacon->version != MARI_PROTOCOL_VERSION) {
        // ignore packet with different protocol version
        return;
    }

    if (!mr_assoc_node_matches_network_id(beacon->network_id)) {
        // ignore packet with non-matching network id
        return;
    }

    bool from_my_gateway = beacon->src == mr_mac_get_synced_gateway();
    if (from_my_gateway && mr_assoc_is_joined()) {
        bool still_joined = mr_bloom_node_contains(mr_device_id(), packet + sizeof(mr_beacon_packet_header_t));
        if (!still_joined) {
            // node no longer joined to this gateway, so need to leave
            assoc_vars.is_pending_disconnect = MARI_PEER_LOST_BLOOM;
            return;
        }

        mr_assoc_node_keep_gateway_alive(mr_mac_get_asn());
    }

    if (from_my_gateway && assoc_vars.state >= JOIN_STATE_SYNCED) {
        // save the remaining capacity of my gateway
        assoc_vars.synced_gateway_remaining_capacity = beacon->remaining_capacity;
    }

    if (beacon->remaining_capacity == 0) {  // TODO: what if I am joined to this gateway? add a check for it.
        // this gateway is full, ignore it
        return;
    }

    // save this scan info
    mr_scan_add(*beacon, mr_radio_rssi(), channel, ts, 0);  // asn not used anymore during scan

    return;
}

//=========================== callbacks =======================================

//=========================== private =========================================
