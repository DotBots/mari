/**
 * @file
 * @ingroup     net_mac
 *
 * @brief       Lower MAC driver for Mari
 *
 * @author Geovane Fedrecheski <geovane.fedrecheski@inria.fr>
 *
 * @copyright Inria, 2025
 */

#include <arm_cmse.h>
#include <nrf.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>

#include "mari.h"
#include "mac.h"
#include "queue.h"
#include "scan.h"
#include "scheduler.h"
#include "association.h"
#include "mr_radio.h"
#include "mr_timer_hf.h"
#include "packet.h"
#include "mr_device.h"

//=========================== debug ============================================

#ifndef DEBUG  // NOTE: This is just to make VS Code enable code behind `#ifdef DEBUG`
#define DEBUG
#endif

#ifdef DEBUG
#include "mr_gpio.h"  // for debugging
// pins connected to logic analyzer, variable names reflect the channel number
mr_gpio_t pin0 = { .port = 1, .pin = 2 };
mr_gpio_t pin1 = { .port = 1, .pin = 3 };
mr_gpio_t pin2 = { .port = 1, .pin = 4 };
mr_gpio_t pin3 = { .port = 1, .pin = 5 };
#define DEBUG_GPIO_TOGGLE(pin) mr_gpio_toggle(pin)
#define DEBUG_GPIO_SET(pin)    mr_gpio_set(pin)
#define DEBUG_GPIO_CLEAR(pin)  mr_gpio_clear(pin)
#define DEBUG_GPIO_SPIIKE(pin) \
    mr_gpio_set(pin);          \
    mr_gpio_clear(pin);
#else
// No-op when DEBUG is not defined
#define DEBUG_GPIO_TOGGLE(pin) ((void)0)
#define DEBUG_GPIO_SET(pin)    ((void)0)
#define DEBUG_GPIO_CLEAR(pin)  ((void)0)
#define DEBUG_GPIO_SPIIKE(pin) ((void)0)
#endif  // DEBUG

//=========================== defines ==========================================

typedef enum {
    // common
    STATE_SLEEP,

    // transmitter
    STATE_TX_OFFSET = 21,
    STATE_TX_DATA   = 22,

    // receiver
    STATE_RX_OFFSET      = 31,
    STATE_RX_DATA_LISTEN = 32,
    STATE_RX_DATA        = 33,

} mr_mac_state_t;

typedef struct {
    uint64_t device_id;  ///< Device ID

    mr_mac_state_t state;              ///< State within the slot
    uint32_t       start_slot_ts;      ///< Timestamp of the start of the slot
    uint64_t       asn;                ///< Absolute slot number
    mr_slot_info_t current_slot_info;  ///< Information about the current slot

    mr_event_cb_t mari_event_callback;  ///< Function pointer, stores the application callback

    mr_received_packet_t received_packet;  ///< Last received packet

    bool     is_scanning;           ///< Whether the node is scanning for gateways
    uint32_t scan_started_ts;       ///< Timestamp of the start of the scan
    uint32_t scan_expected_end_ts;  ///< Timestamp of the expected end of the scan
    uint32_t current_scan_item_ts;  ///< Timestamp of the current scan item
    uint8_t  scan_channel_idx;      ///< Rotating index into the advertising channels for cold scan
    uint8_t  current_scan_channel;  ///< Advertising channel currently being listened on (for beacon bookkeeping)

    bool is_bg_scanning;           ///< Whether the node is scanning for gateways in the background
    bool bg_scan_sleep_next_slot;  ///< Whether the next slot is a sleep slot

    ///< This timestamp keeps track of a full handover scan, that is, a scan that
    ///< Potentially spans multiple background scans (the bg scans are typically very short)
    uint32_t full_bg_scan_started_ts;
    uint32_t full_bg_scan_expected_end_ts;  ///< Timestamp of the expected end of the full handover scan

    uint64_t synced_gateway;     ///< ID of the gateway the node is synchronized with
    uint16_t synced_network_id;  ///< Network ID of the gateway the node is synchronized with
    uint32_t synced_ts;          ///< Timestamp of the last synchronization

    int8_t synced_gateway_rssi_ewma;  ///< Smoothed rssi of packets received from the synced gateway; serving side of the handover hysteresis check
} mac_vars_t;

//=========================== variables ========================================

mac_vars_t mac_vars = { 0 };

mr_slot_durations_t slot_durations = {
    .tx_offset = MARI_TS_TX_OFFSET,
    .tx_max    = MARI_PACKET_TOA_WITH_PADDING,

    .rx_guard  = MARI_RX_GUARD_TIME,
    .rx_offset = MARI_TS_TX_OFFSET - MARI_RX_GUARD_TIME,
    .rx_max    = MARI_RX_GUARD_TIME + MARI_PACKET_TOA_WITH_PADDING,  // same as rx_guard + tx_max

    .end_guard = MARI_END_GUARD_TIME,

    .whole_slot = MARI_WHOLE_SLOT_DURATION,
};

//=========================== prototypes =======================================

static inline void set_slot_state(mr_mac_state_t state);

static void new_slot_synced(void);
static void end_slot(void);
static void node_back_to_scanning(void);
static void disable_radio_and_intra_slot_timers(void);

static void activity_ti1(void);
static void activity_ti2(void);
static void activity_tie1(void);
static void activity_ti3(void);

static void activity_ri1(void);
static void activity_ri2(void);
static void activity_ri3(uint32_t ts);
static void activity_rie1(void);
static void activity_ri4(uint32_t ts);
static void activity_rie2(void);

static void fix_drift(uint32_t ts);

static void start_scan(void);
static void end_scan(void);
static void handle_scan_and_trigger_association(uint32_t now_ts);
static void activity_scan_start_frame(uint32_t ts);
static void activity_scan_end_frame(uint32_t ts);
static bool sync_to_gateway(uint32_t now_ts, mr_channel_info_t *selected_gateway, uint32_t handover_time_correction_us);

static void start_or_continue_background_scan(void);
static void end_background_scan(void);
static void handle_bg_scan_and_trigger_handover(uint32_t now_ts);

static void isr_mac_radio_start_frame(uint32_t ts);
static void isr_mac_radio_end_frame(uint32_t ts);

//=========================== public ===========================================

void mr_mac_init(mr_event_cb_t event_callback) {
#ifdef DEBUG
    mr_gpio_init(&pin0, MR_GPIO_OUT);
    mr_gpio_init(&pin1, MR_GPIO_OUT);
    mr_gpio_init(&pin2, MR_GPIO_OUT);
    mr_gpio_init(&pin3, MR_GPIO_OUT);
#endif

    // initialize the radio
    mr_radio_init(&isr_mac_radio_start_frame, &isr_mac_radio_end_frame, MR_RADIO_BLE_2MBit);

    // node stuff
    mac_vars.device_id = mr_device_id();

    // synchronization stuff
    mac_vars.asn = 0;

    // application callback
    mac_vars.mari_event_callback = event_callback;

    // begin the slot
    set_slot_state(STATE_SLEEP);

    if (mari_get_node_type() == MARI_GATEWAY) {
        mac_vars.start_slot_ts = mr_timer_hf_now(MARI_TIMER_DEV);
        mr_assoc_set_state(JOIN_STATE_JOINED);
        mr_timer_hf_set_periodic_us(
            MARI_TIMER_DEV,
            MARI_TIMER_INTER_SLOT_CHANNEL,
            slot_durations.whole_slot,
            &new_slot_synced);
    } else {
        start_scan();
    }
}

uint64_t mr_mac_get_asn(void) {
    return mac_vars.asn;
}

uint32_t mr_mac_get_tiner_value(void) {
    return mr_timer_hf_now(MARI_TIMER_DEV);
}

uint64_t mr_mac_get_synced_ts(void) {
    return mac_vars.synced_ts;
}

uint64_t mr_mac_get_synced_gateway(void) {
    return mac_vars.synced_gateway;
}

uint16_t mr_mac_get_synced_network_id(void) {
    return mac_vars.synced_network_id;
}

inline bool mr_mac_node_is_synced(void) {
    return mac_vars.synced_gateway != 0;
}

//=========================== private ==========================================

static void set_slot_state(mr_mac_state_t state) {
    mac_vars.state = state;

    switch (state) {
        case STATE_RX_DATA_LISTEN:
        case STATE_TX_DATA:
        case STATE_RX_DATA:
            // DEBUG_GPIO_SET(&pin1);
            break;
        case STATE_SLEEP:
            // DEBUG_GPIO_CLEAR(&pin1);
            DEBUG_GPIO_CLEAR(&pin2);  // pin2 might be SET in case a packet had just started to arrive, so set it low again
            break;
        default:
            break;
    }
}

// --------------------- start/end synced slots -----------

static void new_slot_synced(void) {
    mac_vars.start_slot_ts = mr_timer_hf_now(MARI_TIMER_DEV);
    DEBUG_GPIO_SET(&pin0);
    DEBUG_GPIO_CLEAR(&pin0);  // debug: show that a new slot started

    // perform timeout checks
    if (mari_get_node_type() == MARI_GATEWAY) {
        // too long without receiving a packet from certain nodes? disconnect them
        mr_assoc_gateway_clear_old_nodes(mac_vars.asn);
    } else if (mari_get_node_type() == MARI_NODE) {
        if (mr_assoc_node_should_leave(mac_vars.asn)) {
            // assoc module determined that the node should leave, so disconnect and back to scanning
            mr_assoc_node_handle_pending_disconnect();
            node_back_to_scanning();
            return;
        }
        if (mr_assoc_node_too_long_synced_without_joining()) {
            // too long synced without being able to join? give up and go back to scanning
            mr_assoc_node_handle_give_up_joining();
            node_back_to_scanning();
            return;
        }
        if (mr_assoc_node_too_long_waiting_for_join_response()) {
            // too long without receiving a join response? notify the association module which will backfoff
            bool keep_trying_to_join = mr_assoc_node_handle_failed_join();
            if (!keep_trying_to_join) {
                node_back_to_scanning();
                return;
            }
        }
    }

    mac_vars.current_slot_info = mr_scheduler_tick(mac_vars.asn++);

    if (mac_vars.current_slot_info.radio_action == MARI_RADIO_ACTION_TX) {
        activity_ti1();
    } else if (mac_vars.current_slot_info.radio_action == MARI_RADIO_ACTION_RX) {
        activity_ri1();
    } else if (mac_vars.current_slot_info.radio_action == MARI_RADIO_ACTION_SLEEP) {
        mr_scheduler_stats_register_used_slot(false);
        // check if we should use this slot for background scan
        if (MARI_ENABLE_BACKGROUND_SCAN && mari_get_node_type() == MARI_NODE && mr_assoc_is_joined()) {
            start_or_continue_background_scan();
        } else {
            set_slot_state(STATE_SLEEP);
            end_slot();
        }
    }
}

static void node_clear_synced_info(void) {
    mac_vars.synced_gateway               = 0;
    mac_vars.synced_network_id            = 0;
    mac_vars.synced_ts                    = 0;
    mac_vars.asn                          = 0;
    mac_vars.is_scanning                  = false;
    mac_vars.is_bg_scanning               = false;
    mac_vars.full_bg_scan_started_ts      = 0;
    mac_vars.full_bg_scan_expected_end_ts = 0;
}

static void node_back_to_scanning(void) {
    node_clear_synced_info();
    set_slot_state(STATE_SLEEP);
    end_slot();
    start_scan();
}

static void end_slot(void) {
    if (mari_get_node_type() == MARI_NODE && !mr_mac_node_is_synced()) {
        // not synced, so we are not in a slot
        return;
    }
    disable_radio_and_intra_slot_timers();
}

static void disable_radio_and_intra_slot_timers(void) {
    mr_radio_disable();

    // NOTE: clean all timers
    mr_timer_hf_cancel(MARI_TIMER_DEV, MARI_TIMER_CHANNEL_1);
    mr_timer_hf_cancel(MARI_TIMER_DEV, MARI_TIMER_CHANNEL_2);
    mr_timer_hf_cancel(MARI_TIMER_DEV, MARI_TIMER_CHANNEL_3);
}

// --------------------- start/end scan -------------------

static void start_scan(void) {
    mac_vars.scan_started_ts      = mr_timer_hf_now(MARI_TIMER_DEV);
    mac_vars.scan_expected_end_ts = mac_vars.scan_started_ts + MARI_SCAN_MAX_DURATION;
    DEBUG_GPIO_SET(&pin0);  // debug: show that a new scan started
    mac_vars.is_scanning = true;
    mr_assoc_set_state(JOIN_STATE_SCANNING);

    // end_scan will be called when the scan is over
    mr_timer_hf_set_oneshot_with_ref_us(
        MARI_TIMER_DEV,
        MARI_TIMER_INTER_SLOT_CHANNEL,
        mac_vars.scan_started_ts,
        MARI_SCAN_MAX_DURATION,  // scan during a certain amount of slots
        &end_scan);

    // mac_vars.assoc_info = mr_assoc_get_info(); // NOTE: why this?

    set_slot_state(STATE_RX_DATA_LISTEN);
    mr_radio_disable();
#ifdef MARI_ENABLE_BEACON_HOPPING
    // one advertising channel per scan round; a round that finds nothing retries
    // on the next channel, so a faded channel is escaped within a round or two
    mac_vars.current_scan_channel = MARI_N_BLE_REGULAR_CHANNELS + (mac_vars.scan_channel_idx % MARI_N_BLE_ADVERTISING_CHANNELS);
    mac_vars.scan_channel_idx++;
#else
    mac_vars.current_scan_channel = MARI_FIXED_SCAN_CHANNEL;
#endif
    mr_radio_set_channel(mac_vars.current_scan_channel);
    mr_radio_rx();
}

static void end_scan(void) {
    uint32_t now_ts = mr_timer_hf_now(MARI_TIMER_DEV);

    mac_vars.is_scanning = false;
    DEBUG_GPIO_CLEAR(&pin0);  // debug: show that the scan is over
    set_slot_state(STATE_SLEEP);
    disable_radio_and_intra_slot_timers();

    handle_scan_and_trigger_association(now_ts);
}

// --------------------- start/end background scan --------

static void start_or_continue_background_scan(void) {
    // 1. prepare timestamps and and arm timer
    if (!mac_vars.is_bg_scanning) {
        mac_vars.scan_started_ts      = mac_vars.start_slot_ts;  // reuse the slot start time as reference
        mac_vars.scan_expected_end_ts = mac_vars.scan_started_ts + MARI_BG_SCAN_DURATION;
    }

    if (mac_vars.full_bg_scan_started_ts == 0) {
        mac_vars.full_bg_scan_started_ts      = mac_vars.start_slot_ts;  // reuse the slot start time as reference
        uint32_t handover_full_scan_duration  = mr_scheduler_get_duration_us();
        mac_vars.full_bg_scan_expected_end_ts = mac_vars.full_bg_scan_started_ts + handover_full_scan_duration;
    }

    // check and save whether the next slot is a potential sleep slot
    cell_t next_slot                  = mr_scheduler_node_peek_slot(mac_vars.asn);  // remember: the asn was already incremented at new_slot_synced
    bool   next_uplink_is_sleep_slot  = next_slot.type == SLOT_TYPE_UPLINK && next_slot.assigned_node_id != mr_device_id();
    bool   next_slot_is_shared_uplink = next_slot.type == SLOT_TYPE_SHARED_UPLINK;
    mac_vars.bg_scan_sleep_next_slot  = next_uplink_is_sleep_slot || next_slot_is_shared_uplink;

    // end_background_scan will be called to check if the background scan should be stopped
    mr_timer_hf_set_oneshot_with_ref_us(
        MARI_TIMER_DEV,
        MARI_TIMER_CHANNEL_1,    // remember that the inter-slot timer is already being used for the slot
        mac_vars.start_slot_ts,  // in this case, we use the slot start time as reference because we are synced
        MARI_BG_SCAN_DURATION,   // scan for some time during this slot
        &end_background_scan);

    // 2. turn on the radio, in case it was off (bg scan might be already running since the last slot)
    if (!mac_vars.is_bg_scanning) {
        set_slot_state(STATE_RX_DATA_LISTEN);
        mr_radio_disable();
#ifdef MARI_ENABLE_BEACON_HOPPING
        // rotate the listen channel once per slotframe, so a full handover scan
        // (which spans one slotframe) sweeps all advertising channels over
        // successive slotframes and finds neighbours on any non-faded channel
        mac_vars.current_scan_channel = MARI_N_BLE_REGULAR_CHANNELS + (mr_scheduler_get_slotframe_counter() % MARI_N_BLE_ADVERTISING_CHANNELS);
#else
        mac_vars.current_scan_channel = MARI_FIXED_SCAN_CHANNEL;
#endif
        mr_radio_set_channel(mac_vars.current_scan_channel);
        mr_radio_rx();
    }
    mac_vars.is_bg_scanning = true;
}

static void end_background_scan(void) {
    uint32_t now_ts = mr_timer_hf_now(MARI_TIMER_DEV);

    if (!mac_vars.bg_scan_sleep_next_slot) {
        // if next slot is not sleep, stop the background scan and check if there is an alternative gateway to join
        mac_vars.is_bg_scanning = false;
        set_slot_state(STATE_SLEEP);
        disable_radio_and_intra_slot_timers();

        if (now_ts > mac_vars.full_bg_scan_expected_end_ts) {
            // the full handover scan is over, so handle the scan results and may trigger a handover
            handle_bg_scan_and_trigger_handover(now_ts);
            // independent of whether a handover was triggered, reset the full handover scan timestamps
            mac_vars.full_bg_scan_started_ts      = 0;
            mac_vars.full_bg_scan_expected_end_ts = 0;
        }
    }
    // otherwise, do nothing, and the background scan will continue through the next slot
}

// --------------------- tx activities --------------------

static void activity_ti1(void) {
    // ti1: arm tx timers and prepare the radio for tx
    // called by: function new_slot_synced
    set_slot_state(STATE_TX_OFFSET);

    // before arming the timers, check if there is a packet to send
    uint8_t packet[MARI_PACKET_MAX_SIZE];
    uint8_t packet_len = mr_queue_next_packet(mac_vars.current_slot_info.type, packet);

    if (packet_len == 0) {
        // nothing to tx
        mr_scheduler_stats_register_used_slot(false);

        // check if we should use this slot for background scan
        if (MARI_ENABLE_BACKGROUND_SCAN && mari_get_node_type() == MARI_NODE && mr_assoc_is_joined()) {
            start_or_continue_background_scan();
            return;
        }

        // if we are not going to use this slot, go to sleep
        set_slot_state(STATE_SLEEP);
        end_slot();
        return;
    }
    mr_scheduler_stats_register_used_slot(true);

    // arm the timers
    mr_timer_hf_set_oneshot_with_ref_diff_us(  // TODO: use PPI instead
        MARI_TIMER_DEV,
        MARI_TIMER_CHANNEL_1,
        mac_vars.start_slot_ts,
        slot_durations.tx_offset,
        &activity_ti2);

    mr_timer_hf_set_oneshot_with_ref_diff_us(
        MARI_TIMER_DEV,
        MARI_TIMER_CHANNEL_2,
        mac_vars.start_slot_ts,
        slot_durations.tx_offset + slot_durations.tx_max,
        &activity_tie1);

    // prepare the radio for tx
    mr_radio_disable();
    mr_radio_set_channel(mac_vars.current_slot_info.channel);
    mr_radio_tx_prepare(packet, packet_len);
}

static void activity_ti2(void) {
    // ti2: tx actually begins
    // called by: timer isr
    set_slot_state(STATE_TX_DATA);

    // FIXME: replace this call with a direct PPI connection, i.e., TsTxOffset expires -> radio tx
    mr_radio_tx_dispatch();
}

static void activity_tie1(void) {
    // tte1: something went wrong, stayed in tx for too long, abort
    // called by: timer isr
    set_slot_state(STATE_SLEEP);

    end_slot();
}

static void activity_ti3(void) {
    // ti3: all fine, finished tx, cancel error timers and go to sleep
    // called by: radio isr
    set_slot_state(STATE_SLEEP);

    // cancel tte1 timer
    mr_timer_hf_cancel(MARI_TIMER_DEV, MARI_TIMER_CHANNEL_2);

    end_slot();
}

// --------------------- rx activities --------------

// just write the placeholders for ri1

static void activity_ri1(void) {
    // ri1: arm rx timers and prepare the radio for rx
    // called by: function new_slot_synced
    set_slot_state(STATE_RX_OFFSET);

    mr_timer_hf_set_oneshot_with_ref_diff_us(  // TODO: use PPI instead
        MARI_TIMER_DEV,
        MARI_TIMER_CHANNEL_1,
        mac_vars.start_slot_ts,
        slot_durations.rx_offset,
        &activity_ri2);

    mr_timer_hf_set_oneshot_with_ref_diff_us(
        MARI_TIMER_DEV,
        MARI_TIMER_CHANNEL_2,
        mac_vars.start_slot_ts,
        slot_durations.tx_offset + slot_durations.rx_guard,
        &activity_rie1);

    mr_timer_hf_set_oneshot_with_ref_diff_us(
        MARI_TIMER_DEV,
        MARI_TIMER_CHANNEL_3,
        mac_vars.start_slot_ts,
        slot_durations.rx_offset + slot_durations.rx_max,
        &activity_rie2);
}

static void activity_ri2(void) {
    // ri2: rx actually begins
    // called by: timer isr
    set_slot_state(STATE_RX_DATA_LISTEN);

    mr_radio_disable();
    mr_radio_set_channel(mac_vars.current_slot_info.channel);
    mr_radio_rx();
}

static void activity_ri3(uint32_t ts) {
    // ri3: a packet started to arrive
    // called by: radio isr
    set_slot_state(STATE_RX_DATA);

    mr_scheduler_stats_register_used_slot(true);

    // cancel timer for rx_guard
    mr_timer_hf_cancel(MARI_TIMER_DEV, MARI_TIMER_CHANNEL_2);

    mac_vars.received_packet.start_ts = ts;
}

static void activity_rie1(void) {
    // rie1: didn't receive start of packet before rx_guard, abort
    // called by: timer isr
    set_slot_state(STATE_SLEEP);

    mr_scheduler_stats_register_used_slot(false);

    // cancel timer for rx_max (rie2)
    mr_timer_hf_cancel(MARI_TIMER_DEV, MARI_TIMER_CHANNEL_3);

    end_slot();
}

static void activity_ri4(uint32_t ts) {
    // ri4: all fine, finished rx, cancel error timers and go to sleep
    // called by: radio isr
    set_slot_state(STATE_SLEEP);

    // cancel timer for rx_max (rie2)
    mr_timer_hf_cancel(MARI_TIMER_DEV, MARI_TIMER_CHANNEL_3);

    if (!mr_radio_pending_rx_read()) {
        // no packet received
        end_slot();
        return;
    }

    mr_radio_get_rx_packet(mac_vars.received_packet.packet, &mac_vars.received_packet.packet_len);

    mr_packet_header_t *header = (mr_packet_header_t *)mac_vars.received_packet.packet;

    if (header->version != MARI_PROTOCOL_VERSION) {
        end_slot();
        return;
    }

    if (mari_get_node_type() == MARI_NODE && mr_assoc_is_joined() && header->src == mac_vars.synced_gateway) {
        // only fix drift if the packet comes from the gateway we are synced to
        // NOTE: this should ideally be done at ri3 (when the packet starts), but we don't have the id there.
        //       could use use the physical BLE address for that?
        fix_drift(mac_vars.received_packet.start_ts);
    }

    // store info about the received packet
    mac_vars.received_packet.channel = mac_vars.current_slot_info.channel;
    mac_vars.received_packet.rssi    = mr_radio_rssi();
    mac_vars.received_packet.end_ts  = ts;
    mac_vars.received_packet.asn     = mac_vars.asn;

    if (mari_get_node_type() == MARI_NODE && header->src == mac_vars.synced_gateway) {
        // smooth the serving gateway's rssi for the handover hysteresis check:
        // a single sample can sit in a per-channel fade, and received_packet
        // alone may hold a packet from another source (e.g. a foreign beacon)
        mac_vars.synced_gateway_rssi_ewma += (int8_t)((mac_vars.received_packet.rssi - mac_vars.synced_gateway_rssi_ewma) >> MARI_HANDOVER_RSSI_EWMA_SHIFT);
    }

    mr_handle_packet(mac_vars.received_packet.packet, mac_vars.received_packet.packet_len);

    end_slot();
}

static void activity_rie2(void) {
    // rie2: something went wrong, stayed in rx for too long, abort
    // called by: timer isr
    set_slot_state(STATE_SLEEP);

    end_slot();
}

static void fix_drift(uint32_t ts) {
    DEBUG_GPIO_SPIIKE(&pin1);
    uint32_t time_cpu_periph = 59;  // got this value by looking at the logic analyzer

    uint32_t expected_ts     = mac_vars.start_slot_ts + slot_durations.tx_offset + time_cpu_periph;
    int32_t  clock_drift     = ts - expected_ts;
    uint32_t abs_clock_drift = abs(clock_drift);

    if (abs_clock_drift < 100) {
        // drift is acceptable
        // adjust the slot reference
        mr_timer_hf_adjust_periodic_us(
            MARI_TIMER_DEV,
            MARI_TIMER_INTER_SLOT_CHANNEL,
            clock_drift);
    } else {
        // drift is too high, need to re-sync
        // FIXME: use `mr_assoc_node_handle_immediate_disconnect` instead
        mr_event_data_t event_data = { .data.gateway_info.gateway_id = mac_vars.synced_gateway, .tag = MARI_OUT_OF_SYNC };
        mac_vars.mari_event_callback(MARI_DISCONNECTED, event_data);
        mr_assoc_set_state(JOIN_STATE_IDLE);
        set_slot_state(STATE_SLEEP);
        end_slot();
        start_scan();
    }
}

// --------------------- handover --------------------

static bool select_gateway_for_handover(uint32_t now_ts, mr_channel_info_t *selected_gateway) {
    if (now_ts - mac_vars.synced_ts < MARI_HANDOVER_MIN_INTERVAL) {
        // just recently performed a synchronization, will not try again so soon
        return false;
    }

    // select over the full advertising-channel sweep (the bg scan listens on one
    // channel per slotframe), so the decision averages a candidate across all
    // channels instead of seeing only the last burst's single-channel sample
    uint32_t sweep_duration_us = MARI_N_BLE_ADVERTISING_CHANNELS * mr_scheduler_get_duration_us();
    // clamp: right after the timer starts (or wraps) now_ts can be smaller than
    // the sweep duration, and the unsigned underflow would discard every entry
    uint32_t sweep_started_ts = now_ts > sweep_duration_us ? now_ts - sweep_duration_us : 0;
    if (!mr_scan_select(selected_gateway, sweep_started_ts, now_ts)) {
        // no gateway found, do nothing
        return false;
    }

    if (selected_gateway->beacon.src == mac_vars.synced_gateway) {
        // should not happen, but just in case: already synced to this gateway, ignore it
        return false;
    }

    if (selected_gateway->rssi < (mac_vars.synced_gateway_rssi_ewma + MARI_HANDOVER_RSSI_HYSTERESIS)) {
        // the new gateway is not strong enough, ignore it
        return false;
    }

    return true;
}

static void handle_bg_scan_and_trigger_handover(uint32_t now_ts) {
    mr_channel_info_t selected_gateway = { 0 };
    if (!select_gateway_for_handover(now_ts, &selected_gateway)) {
        // no handover, stop here
        return;
    }

    // debug: show that a handover is going to happen
    DEBUG_GPIO_SET(&pin3);
    DEBUG_GPIO_CLEAR(&pin3);

    // a handover is going to happen, have the association module handle the disconnection event
    mr_assoc_node_handle_immediate_disconnect(MARI_HANDOVER);
    // during handover, we don't want the inter slot timer to tick again before we finish sync, so just set if far away in the future
    mr_timer_hf_set_periodic_us(
        MARI_TIMER_DEV,
        MARI_TIMER_INTER_SLOT_CHANNEL,
        slot_durations.whole_slot << 4,  // 16 slots in the future
        &new_slot_synced);

    uint32_t handover_time_correction_us = 206;  // magic number: measured using the logic analyzer
    if (sync_to_gateway(now_ts, &selected_gateway, handover_time_correction_us)) {
        // found a gateway and synchronized to it
        mr_assoc_node_handle_synced();
    } else {
        // failed to synchronize to a gateway, back to scanning
        mr_assoc_node_handle_immediate_disconnect(MARI_HANDOVER_FAILED);
        node_back_to_scanning();
        return;
    }
}

// --------------------- scan activities ------------------

static void handle_scan_and_trigger_association(uint32_t now_ts) {
    mr_channel_info_t selected_gateway = { 0 };
    if (!mr_scan_select(&selected_gateway, mac_vars.scan_started_ts, now_ts)) {
        // no gateway found, back to scanning
        start_scan();
        return;
    }

    if (sync_to_gateway(now_ts, &selected_gateway, 0)) {
        // successfully synchronized to a gateway
        mr_assoc_node_handle_synced();
    } else {
        // failed to synchronize to a gateway, back to scanning
        start_scan();
    }
}

static void activity_scan_dispatch_new_schedule(void) {
    mr_timer_hf_set_periodic_us(
        MARI_TIMER_DEV,
        MARI_TIMER_INTER_SLOT_CHANNEL,
        slot_durations.whole_slot,
        &new_slot_synced);
}

static bool sync_to_gateway(uint32_t now_ts, mr_channel_info_t *selected_gateway, uint32_t handover_time_correction_us) {
    if (!mr_scheduler_set_schedule(selected_gateway->beacon.active_schedule_id)) {
        // schedule not found, a new scan will begin again via new_scan
        return false;
    }

    mac_vars.synced_gateway    = selected_gateway->beacon.src;
    mac_vars.synced_network_id = selected_gateway->beacon.network_id;
    mac_vars.synced_ts         = now_ts;
    // seed the serving-side smoothing from the (cross-channel averaged) rssi
    // that won the selection; refined on every packet received from the gateway
    mac_vars.synced_gateway_rssi_ewma = selected_gateway->rssi;

    // the selected gateway may have been scanned a few slot_durations ago, so we need to account for that difference
    // NOTE: this assumes that the slot duration is the same for gateways and nodes
    uint32_t time_since_beacon      = now_ts - selected_gateway->timestamp;
    uint64_t asn_count_since_beacon = (time_since_beacon / slot_durations.whole_slot) + 1;  // +1 because we are inside the current slot
    uint64_t time_into_gateway_slot = time_since_beacon % slot_durations.whole_slot;

    uint64_t time_to_skip_one_slot = 0;
    if (time_into_gateway_slot > slot_durations.whole_slot / 2) {
        // too close to the next slot, skip this one
        asn_count_since_beacon++;
        time_to_skip_one_slot = slot_durations.whole_slot;
    }

    uint64_t time_cpu_and_toa = 541;  // magic number: measured using the logic analyzer
    time_cpu_and_toa += handover_time_correction_us;

    uint32_t time_dispatch_new_schedule = ((slot_durations.whole_slot - time_into_gateway_slot) + time_to_skip_one_slot) - time_cpu_and_toa;
    mr_timer_hf_set_oneshot_us(
        MARI_TIMER_DEV,
        MARI_TIMER_CHANNEL_1,
        time_dispatch_new_schedule,
        &activity_scan_dispatch_new_schedule);

    // set the asn to match the gateway's
    mac_vars.asn = selected_gateway->beacon.asn + asn_count_since_beacon;

    return true;
}

static void activity_scan_start_frame(uint32_t ts) {
    set_slot_state(STATE_RX_DATA);
    mac_vars.current_scan_item_ts = ts;

    // NOTE: should probably set an error timer here, in case the end event doesn't happen
}

static void activity_scan_end_frame(uint32_t end_frame_ts) {
    uint8_t packet[MARI_PACKET_MAX_SIZE];
    uint8_t packet_len;
    mr_radio_get_rx_packet(packet, &packet_len);

    mr_assoc_handle_beacon(packet, packet_len, mac_vars.current_scan_channel, mac_vars.current_scan_item_ts);

    // if there is still enough time before end of scan, re-enable the radio
    bool still_time_for_rx_scan    = mac_vars.is_scanning && (end_frame_ts + MARI_BEACON_TOA_WITH_PADDING < mac_vars.scan_expected_end_ts);
    bool still_time_for_rx_bg_scan = mr_assoc_is_joined() && mac_vars.is_bg_scanning && mac_vars.bg_scan_sleep_next_slot;
    if (still_time_for_rx_scan || still_time_for_rx_bg_scan) {
        // re-enable the radio, if there still time to scan more (conditions for normal / bg scan)
        set_slot_state(STATE_RX_DATA_LISTEN);
        // we cannot call rx immediately, because this runs in isr context/
        // and it might interfere with `if (NRF_RADIO->EVENTS_DISABLED)` in RADIO_IRQHandler
        mr_timer_hf_set_oneshot_with_ref_us(
            MARI_TIMER_DEV,
            MARI_TIMER_CHANNEL_2,
            end_frame_ts,
            20,  // arbitrary value, just to give some time for the radio to turn off
            &mr_radio_rx);
    } else {
        set_slot_state(STATE_SLEEP);
    }
}

// --------------------- tx/rx activities ------------

// --------------------- radio ---------------------
static void isr_mac_radio_start_frame(uint32_t ts) {
    DEBUG_GPIO_SET(&pin2);
    if (mac_vars.is_scanning || mac_vars.is_bg_scanning) {
        activity_scan_start_frame(ts);
        return;
    }

    switch (mac_vars.state) {
        case STATE_RX_DATA_LISTEN:
            activity_ri3(ts);
            break;
        default:
            break;
    }
}

static void isr_mac_radio_end_frame(uint32_t ts) {
    DEBUG_GPIO_CLEAR(&pin2);

    if (mac_vars.is_scanning || mac_vars.is_bg_scanning) {
        activity_scan_end_frame(ts);
        return;
    }

    switch (mac_vars.state) {
        case STATE_TX_DATA:
            activity_ti3();
            break;
        case STATE_RX_DATA:
            activity_ri4(ts);
            break;
        default:
            break;
    }
}
