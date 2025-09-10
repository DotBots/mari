/**
 * @file
 * @ingroup     app
 *
 * @brief       Mari Gateway application (uart side)
 *
 * @author Geovane Fedrecheski <geovane.fedrecheski@inria.fr>
 * @author Alexandre Abadie <alexandre.abadie@inria.fr>
 *
 * @copyright Inria, 2025-now
 */
#include <nrf.h>
#include <stdio.h>

#include "ipc.h"

#include "mr_clock.h"
#include "mr_device.h"
#include "hdlc.h"
#include "uart.h"

//=========================== defines ==========================================

#define MR_UART_INDEX    (1)          ///< Index of UART peripheral to use
#define MR_UART_BAUDRATE (1000000UL)  ///< UART baudrate used by the gateway

typedef struct {
    bool    mari_frame_received;
    bool    uart_byte_received;
    uint8_t uart_byte;
    uint8_t hdlc_encode_buffer[1024];  // Should be large enough
} gateway_app_vars_t;

// UART RX and TX pins
static const mr_gpio_t _mr_uart_tx_pin = { .port = 1, .pin = 1 };
static const mr_gpio_t _mr_uart_rx_pin = { .port = 1, .pin = 0 };

static gateway_app_vars_t                                           _app_vars = { 0 };
volatile __attribute__((section(".shared_data"))) ipc_shared_data_t ipc_shared_data;

static void _setup_debug_pins(void) {
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

static void _configure_ram_non_secure(uint8_t start_region, size_t length) {
    for (uint8_t region = start_region; region < start_region + length; region++) {
        NRF_SPU_S->RAMREGION[region].PERM = (SPU_RAMREGION_PERM_READ_Enable << SPU_RAMREGION_PERM_READ_Pos |
                                             SPU_RAMREGION_PERM_WRITE_Enable << SPU_RAMREGION_PERM_WRITE_Pos |
                                             SPU_RAMREGION_PERM_EXECUTE_Enable << SPU_RAMREGION_PERM_EXECUTE_Pos |
                                             SPU_RAMREGION_PERM_SECATTR_Non_Secure << SPU_RAMREGION_PERM_SECATTR_Pos);
    }
}

static void _init_ipc(void) {
    NRF_IPC_S->INTENSET                            = (1 << IPC_CHAN_RADIO_TO_UART);
    NRF_IPC_S->SEND_CNF[IPC_CHAN_UART_TO_RADIO]    = (1 << IPC_CHAN_UART_TO_RADIO);
    NRF_IPC_S->RECEIVE_CNF[IPC_CHAN_RADIO_TO_UART] = (1 << IPC_CHAN_RADIO_TO_UART);

    NVIC_EnableIRQ(IPC_IRQn);
    NVIC_ClearPendingIRQ(IPC_IRQn);
    NVIC_SetPriority(IPC_IRQn, IPC_IRQ_PRIORITY);
}

static void _release_network_core(void) {
    // Do nothing if network core is already started and ready
    if (!NRF_RESET_S->NETWORK.FORCEOFF && ipc_shared_data.net_ready) {
        return;
    } else if (!NRF_RESET_S->NETWORK.FORCEOFF) {
        ipc_shared_data.net_ready = false;
    }

    NRF_RESET_S->NETWORK.FORCEOFF = (RESET_NETWORK_FORCEOFF_FORCEOFF_Release << RESET_NETWORK_FORCEOFF_FORCEOFF_Pos);

    // add an extra delay to ensure the network core is released
    // NOTE: this is very hacky, but since this only happens once, we don't want to consume another timer
    for (uint32_t i = 0; i < 500000; i++) {
        __NOP();
    }

    while (!ipc_shared_data.net_ready) {}
}

static void _uart_callback(uint8_t byte) {
    _app_vars.uart_byte          = byte;
    _app_vars.uart_byte_received = true;
}

int main(void) {
    printf("Hello Mari Gateway App Core (UART) %016llX\n", mr_device_id());

    _setup_debug_pins();

    _configure_ram_non_secure(2, 1);

    // Enable HFCLK with external 32MHz oscillator
    mr_hfclk_init();

    // APPMUTEX (address at 0x41030000 => periph ID is 48)
    NRF_SPU_S->PERIPHID[48].PERM = (SPU_PERIPHID_PERM_SECUREMAPPING_UserSelectable << SPU_PERIPHID_PERM_SECUREMAPPING_Pos |
                                    SPU_PERIPHID_PERM_SECATTR_NonSecure << SPU_PERIPHID_PERM_SECATTR_Pos |
                                    SPU_PERIPHID_PERM_PRESENT_IsPresent << SPU_PERIPHID_PERM_PRESENT_Pos);

    _init_ipc();
    mr_uart_init(MR_UART_INDEX, &_mr_uart_rx_pin, &_mr_uart_tx_pin, MR_UART_BAUDRATE, &_uart_callback);

    _release_network_core();
    // this is a bit hacky -- sometimes it does not work without this
    NRF_RESET_S->NETWORK.FORCEOFF = 0;

    while (1) {
        __WFE();

        if (_app_vars.uart_byte_received) {
            _app_vars.uart_byte_received = false;
            mr_hdlc_state_t hdlc_state   = mr_hdlc_rx_byte(_app_vars.uart_byte);
            switch ((uint8_t)hdlc_state) {
                case MR_HDLC_STATE_IDLE:
                case MR_HDLC_STATE_RECEIVING:
                case MR_HDLC_STATE_ERROR:
                    break;
                case MR_HDLC_STATE_READY:
                {
                    mutex_lock();
                    size_t msg_len                    = mr_hdlc_decode((uint8_t *)ipc_shared_data.uart_to_radio);
                    ipc_shared_data.uart_to_radio_len = msg_len;
                    mutex_unlock();
                    if (msg_len) {
                        NRF_IPC_S->TASKS_SEND[IPC_CHAN_UART_TO_RADIO] = 1;
                    }
                } break;
                default:
                    break;
            }
        }

        if (_app_vars.mari_frame_received) {
            _app_vars.mari_frame_received = false;
            mutex_lock();
            size_t frame_len              = mr_hdlc_encode((uint8_t *)ipc_shared_data.radio_to_uart, ipc_shared_data.radio_to_uart_len, _app_vars.hdlc_encode_buffer);
            mutex_unlock();
            mr_uart_write(MR_UART_INDEX, _app_vars.hdlc_encode_buffer, frame_len);
        }
    }
}

void IPC_IRQHandler(void) {
    if (NRF_IPC_S->EVENTS_RECEIVE[IPC_CHAN_RADIO_TO_UART]) {
        NRF_IPC_S->EVENTS_RECEIVE[IPC_CHAN_RADIO_TO_UART] = 0;
        _app_vars.mari_frame_received                     = true;
    }
}
