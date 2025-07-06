/**
 * @file
 * @ingroup     app
 *
 * @brief       Mira Gateway application (uart side)
 *
 * @author Geovane Fedrecheski <geovane.fedrecheski@inria.fr>
 * @author Alexandre Abadie <alexandre.abadie@inria.fr>
 *
 * @copyright Inria, 2025-now
 */
#include <nrf.h>
#include <stdio.h>

#include "mr_device.h"
#include "mr_gpio.h"
#include "mr_ipc.h"
#include "mira.h"
#include "tz.h"

//=========================== defines ==========================================

#define MIRA_APP_TIMER_DEV 1

typedef struct {
    bool dummy;
} gateway_app_vars_t;

//=========================== variables ========================================

gateway_app_vars_t gateway_app_vars = { 0 };

extern volatile __attribute__((section(".shared_data"))) mr_ipc_shared_data_t ipc_shared_data;

//=========================== prototypes =======================================

void setup_debug_pins(void);
void app_mira_node_tx(const uint8_t *packet, uint8_t length);

//=========================== main =============================================

int main(void) {
    printf("Hello Mira Gateway App Core (UART) %016llX\n", mr_device_id());

    setup_debug_pins();

    mr_timer_hf_init(MIRA_APP_TIMER_DEV);

    mr_timer_hf_set_periodic_us(MIRA_APP_TIMER_DEV, 2, mr_scheduler_get_duration_us(), &mira_event_loop);

    // TODO: communicate with the network core via IPC, and make sure we start the network core

    // Start the network core
    release_network_core();

    // APPMUTEX (address at 0x41030000 => periph ID is 48)
    tz_configure_periph_non_secure(NRF_APPLICATION_PERIPH_ID_MUTEX);

    // Initialize TDMA client drv in the net-core
    mr_ipc_network_call(MR_IPC_MIRA_INIT_REQ);

    // TODO: communicate with an external device via UART (e.g. a computer or raspberry pi)

    while (1) {
        __SEV();
        __WFE();
        __WFE();
    }
}

//=========================== callbacks ========================================

void mira_event_callback(mr_event_t event, mr_event_data_t event_data) {
    switch (event) {
        case MIRA_NEW_PACKET:
        {
            break;
        }
        case MIRA_NODE_JOINED:
            printf("New node joined: %016llX  (%d nodes connected)\n", event_data.data.node_info.node_id, mira_gateway_count_nodes());
            break;
        case MIRA_NODE_LEFT:
            printf("Node left: %016llX, reason: %u  (%d nodes connected)\n", event_data.data.node_info.node_id, event_data.tag, mira_gateway_count_nodes());
            break;
        case MIRA_ERROR:
            printf("Error, reason: %u\n", event_data.tag);
            break;
        default:
            break;
    }
}

//=========================== private ========================================

void app_mira_node_tx(const uint8_t *packet, uint8_t length) {
    ipc_shared_data.tx_pdu.length = length;
    memcpy((void *)ipc_shared_data.tx_pdu.buffer, packet, length);
    ipc_network_call(MR_IPC_MIRA_NODE_TX_REQ);
}

void setup_debug_pins(void) {
    // Assign P0.28 to P0.31 to the network core (for debugging association.c via LEDs)
    NRF_P0_S->PIN_CNF[28] = GPIO_PIN_CNF_MCUSEL_NetworkMCU << GPIO_PIN_CNF_MCUSEL_Pos;
    NRF_P0_S->PIN_CNF[29] = GPIO_PIN_CNF_MCUSEL_NetworkMCU << GPIO_PIN_CNF_MCUSEL_Pos;
    NRF_P0_S->PIN_CNF[30] = GPIO_PIN_CNF_MCUSEL_NetworkMCU << GPIO_PIN_CNF_MCUSEL_Pos;
    NRF_P0_S->PIN_CNF[31] = GPIO_PIN_CNF_MCUSEL_NetworkMCU << GPIO_PIN_CNF_MCUSEL_Pos;

    // Assign P1.02 to P1.05 to the network core (for debugging mac.c via logic analyzer)
    NRF_P1_S->PIN_CNF[2] = GPIO_PIN_CNF_MCUSEL_NetworkMCU << GPIO_PIN_CNF_MCUSEL_Pos;
    NRF_P1_S->PIN_CNF[3] = GPIO_PIN_CNF_MCUSEL_NetworkMCU << GPIO_PIN_CNF_MCUSEL_Pos;
    NRF_P1_S->PIN_CNF[4] = GPIO_PIN_CNF_MCUSEL_NetworkMCU << GPIO_PIN_CNF_MCUSEL_Pos;
    NRF_P1_S->PIN_CNF[5] = GPIO_PIN_CNF_MCUSEL_NetworkMCU << GPIO_PIN_CNF_MCUSEL_Pos;

    // Configure all GPIOs as non secure
    NRF_SPU_S->GPIOPORT[0].PERM = 0;
    NRF_SPU_S->GPIOPORT[1].PERM = 0;
}
