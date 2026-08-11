/**
 * @file
 * @ingroup     app
 *
 * @brief       Mari Gateway application (radio side)
 *
 * @author Geovane Fedrecheski <geovane.fedrecheski@inria.fr>
 * @author Alexandre Abadie <alexandre.abadie@inria.fr>
 *
 * @copyright Inria, 2025-now
 */
#include <nrf.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "ipc.h"

#include "mr_device.h"
#include "mr_timer_hf.h"
#include "mr_radio.h"
#include "mac.h"
#include "association.h"
#include "scheduler.h"
#include "mari.h"
#include "packet.h"
#include "models.h"

#include "metrics.h"

//=========================== defines ==========================================

#define MARI_APP_NET_CONFIG_START_ADDRESS (0x0103f800)  // start of the last page (2KB) of the flash (0x01000000 + 0x00040000 - 0x800)
#define MARI_APP_CONFIG_MAGIC_VALUE       (0x5753524D)  // "SWRM"

#define MARI_APP_TIMER_DEV 1

typedef struct {
    mr_event_t      mari_event;
    mr_event_data_t mari_event_data;
    bool            mari_event_ready;
    bool            uart_to_radio_packet_ready;
    bool            to_uart_gateway_loop_ready;
    uint32_t        tx_count;
    uint32_t        rx_count;
    uint8_t         ipc_u2r_seq;  ///< last uart_to_radio sequence number this core consumed
} gateway_vars_t;

typedef struct __attribute__((packed)) {
    uint32_t magic;       // to detect if config is valid (must equal MARI_APP_CONFIG_MAGIC_VALUE)
    uint32_t has_net_id;  // 1 if net_id is provisioned; otherwise fall back to the default
    uint32_t net_id;      // Mari network ID, meaningful only when has_net_id == 1
} mari_app_config_t;

//=========================== variables ========================================

gateway_vars_t _app_vars = { 0 };

extern schedule_t schedule_tiny, schedule_medium, schedule_big, schedule_huge;
schedule_t       *schedule_app = &schedule_huge;

volatile __attribute__((section(".shared_data"))) ipc_shared_data_t ipc_shared_data;

static void _mari_event_callback(mr_event_t event, mr_event_data_t event_data) {
    _app_vars.mari_event = event;
    memcpy(&_app_vars.mari_event_data, &event_data, sizeof(mr_event_data_t));
    _app_vars.mari_event_ready = true;
}

static void _to_uart_gateway_loop(void) {
    _app_vars.to_uart_gateway_loop_ready = true;
}

static uint16_t _net_id(void) {
    const mari_app_config_t *cfg = (const mari_app_config_t *)MARI_APP_NET_CONFIG_START_ADDRESS;

    if (cfg->magic != MARI_APP_CONFIG_MAGIC_VALUE || cfg->has_net_id != 1) {
        // No network config found, use default network ID
        return MARI_NET_ID_DEFAULT;
    }
    return (uint16_t)(cfg->net_id & 0xFFFFu);
}

static void _init_ipc(void) {
    NRF_IPC_NS->INTENSET                            = (1 << IPC_CHAN_UART_TO_RADIO);
    NRF_IPC_NS->SEND_CNF[IPC_CHAN_RADIO_TO_UART]    = (1 << IPC_CHAN_RADIO_TO_UART);
    NRF_IPC_NS->RECEIVE_CNF[IPC_CHAN_UART_TO_RADIO] = (1 << IPC_CHAN_UART_TO_RADIO);

    NVIC_EnableIRQ(IPC_IRQn);
    NVIC_ClearPendingIRQ(IPC_IRQn);
    NVIC_SetPriority(IPC_IRQn, IPC_IRQ_PRIORITY);
}

int main(void) {
    printf("Hello Mari Gateway Net Core %016llX\n", mr_device_id());
    mr_timer_hf_init(MARI_APP_TIMER_DEV);
    _init_ipc();

    mari_init(MARI_GATEWAY, _net_id(), schedule_app, &_mari_event_callback);

    // NOTE: to send the stats every slotframe, we need to use the duration of the slotframe
    mr_timer_hf_set_periodic_us(MARI_APP_TIMER_DEV, 3, mr_scheduler_get_duration_us(), &_to_uart_gateway_loop);

    // Unlock the application core
    ipc_shared_data.net_ready = true;

    while (1) {
        __WFE();

        if (_app_vars.mari_event_ready) {
            _app_vars.mari_event_ready = false;

            mr_event_t      event      = _app_vars.mari_event;
            mr_event_data_t event_data = _app_vars.mari_event_data;

            uint32_t now_ts_s     = mr_timer_hf_now(MARI_APP_TIMER_DEV) / 1000 / 1000;
            bool     send_to_uart = false;
            switch (event) {
                case MARI_NEW_PACKET:
                {
                    // handle metrics probe
                    if (metrics_is_probe(event_data.data.new_packet.payload, event_data.data.new_packet.payload_len)) {
                        metrics_handle_rx_probe(event_data.data.new_packet.header->src, event_data.data.new_packet.payload);
                    }

                    ipc_shared_data.radio_to_uart_len = event_data.data.new_packet.len + 1;
                    ipc_shared_data.radio_to_uart[0]  = MARI_EDGE_DATA;
                    memcpy((void *)ipc_shared_data.radio_to_uart + 1, event_data.data.new_packet.header, event_data.data.new_packet.len);
                    send_to_uart = true;
                    break;
                }
                case MARI_KEEPALIVE:
                    ipc_shared_data.radio_to_uart_len = 1 + sizeof(uint64_t);
                    ipc_shared_data.radio_to_uart[0]  = MARI_EDGE_KEEPALIVE;
                    memcpy((void *)ipc_shared_data.radio_to_uart + 1, &event_data.data.node_info.node_id, sizeof(uint64_t));
                    send_to_uart = true;
                    break;
                case MARI_NODE_JOINED:
                    printf("%d New node joined: %016llX  (%d nodes connected)\n", now_ts_s, event_data.data.node_info.node_id, mari_gateway_count_nodes());
                    metrics_add_node(event_data.data.node_info.node_id);
                    ipc_shared_data.radio_to_uart_len = 1 + sizeof(uint64_t);
                    ipc_shared_data.radio_to_uart[0]  = MARI_EDGE_NODE_JOINED;
                    memcpy((void *)ipc_shared_data.radio_to_uart + 1, &event_data.data.node_info.node_id, sizeof(uint64_t));
                    send_to_uart = true;
                    break;
                case MARI_NODE_LEFT:
                    printf("%d Node left: %016llX, reason: %u  (%d nodes connected)\n", now_ts_s, event_data.data.node_info.node_id, event_data.tag, mari_gateway_count_nodes());
                    metrics_clear_node(event_data.data.node_info.node_id);
                    ipc_shared_data.radio_to_uart_len = 1 + sizeof(uint64_t);
                    ipc_shared_data.radio_to_uart[0]  = MARI_EDGE_NODE_LEFT;
                    memcpy((void *)ipc_shared_data.radio_to_uart + 1, &event_data.data.node_info.node_id, sizeof(uint64_t));
                    send_to_uart = true;
                    break;
                case MARI_ERROR:
                    printf("Error, reason: %u\n", event_data.tag);
                    break;
                default:
                    break;
            }

            if (send_to_uart) {
                ipc_shared_data.radio_to_uart_seq++;
                NRF_IPC_NS->TASKS_SEND[IPC_CHAN_RADIO_TO_UART] = 1;
            }
        }

        if (_app_vars.uart_to_radio_packet_ready) {
            _app_vars.uart_to_radio_packet_ready = false;

            // This loop, not the IPC handler, is the consumer of
            // uart_to_radio_tx, so the gap has to be measured here: two IPC
            // signals arriving between two passes leave a single buffer
            // holding only the second message.
            uint8_t seq = ipc_shared_data.uart_to_radio_seq;
            ipc_shared_data.stats.ipc_u2r_lost += (uint8_t)(seq - _app_vars.ipc_u2r_seq) - 1;
            _app_vars.ipc_u2r_seq = seq;

            uint8_t packet_type = ipc_shared_data.uart_to_radio_tx[0];
            if (packet_type != MARI_EDGE_DATA) {
                printf("Invalid UART packet type: %02X\n", packet_type);
                continue;
            }

            uint8_t *mari_frame     = (uint8_t *)ipc_shared_data.uart_to_radio_tx + 1;
            uint8_t  mari_frame_len = ipc_shared_data.uart_to_radio_len - 1;

            mr_packet_header_t *header = (mr_packet_header_t *)mari_frame;
            header->src                = mr_device_id();
            header->network_id         = mr_assoc_get_network_id();

            // handle metrics probe
            uint8_t *payload     = mari_frame + sizeof(mr_packet_header_t);
            uint8_t  payload_len = mari_frame_len - sizeof(mr_packet_header_t);
            if (metrics_is_probe(payload, payload_len)) {
                metrics_handle_tx_probe(header->dst, payload);
            }

            mari_tx(mari_frame, mari_frame_len);
        }

        if (_app_vars.to_uart_gateway_loop_ready) {
            _app_vars.to_uart_gateway_loop_ready = false;
            ipc_shared_data.radio_to_uart[0]     = MARI_EDGE_GATEWAY_INFO;
            size_t len                           = mr_build_uart_packet_gateway_info(
                (uint8_t *)(ipc_shared_data.radio_to_uart + 1),
                (const mr_gateway_uart_stats_t *)&ipc_shared_data.stats);
            ipc_shared_data.radio_to_uart_len = 1 + len;
            ipc_shared_data.radio_to_uart_seq++;
            NRF_IPC_NS->TASKS_SEND[IPC_CHAN_RADIO_TO_UART] = 1;
        }

        // best to keep this at the end of the main loop
        mari_event_loop();
    }
}

void IPC_IRQHandler(void) {
    if (NRF_IPC_NS->EVENTS_RECEIVE[IPC_CHAN_UART_TO_RADIO]) {
        NRF_IPC_NS->EVENTS_RECEIVE[IPC_CHAN_UART_TO_RADIO] = 0;
        _app_vars.uart_to_radio_packet_ready               = true;
    }
}
