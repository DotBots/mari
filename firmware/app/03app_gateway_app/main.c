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
#include <string.h>

#include "gateway_ipc.h"

#include "mr_clock.h"
#include "mr_device.h"
#include "hdlc.h"
#include "uart.h"

//=========================== defines ==========================================

#define MR_UART_INDEX    (1)          ///< Index of UART peripheral to use
#define MR_UART_BAUDRATE (1000000UL)  ///< UART baudrate used by the gateway

#define TX_QUEUE_SIZE 4

typedef struct {
    uint8_t buffer[256];
    size_t  length;
} tx_frame_t;

typedef struct {
    uint8_t hdlc_encode_buffer[1024];  // Should be large enough
    uint8_t hdlc_decode_buffer[1024];  // Sized so mr_hdlc_decode can never overrun it
    bool    tx_pending;                // Flag for deferred TX (legacy)
    size_t  tx_frame_len;              // Length of frame to transmit

    // TX queue
    tx_frame_t tx_queue[TX_QUEUE_SIZE];
    uint8_t    tx_queue_head;
    uint8_t    tx_queue_tail;
    uint8_t    tx_queue_count;

    mr_hdlc_state_t hdlc_state;  ///< decoder state after the last byte fed to it
} gateway_app_vars_t;

/**
 * @brief Diagnostic counters owned by this core
 *
 * Kept apart from the operational state above because nothing reads them to
 * decide anything: they are incremented on the way past and mirrored into
 * shared RAM for the network core to put in gateway_info. Adding one here
 * cannot change how the gateway behaves.
 */
typedef struct {
    uint32_t rx_frames_ok;   ///< frames decoded with a valid checksum
    uint32_t rx_hdlc_err;    ///< torn or corrupted frames, one per damaged frame
    uint32_t tx_queue_drop;  ///< frames refused because the TX queue was full
} gateway_app_dbg_vars_t;

// UART RX and TX pins
static const mr_gpio_t _mr_uart_tx_pin = { .port = 1, .pin = 1 };
static const mr_gpio_t _mr_uart_rx_pin = { .port = 1, .pin = 0 };

static gateway_app_vars_t                                           _app_vars = { 0 };
static gateway_app_dbg_vars_t                                       _dbg_vars = { 0 };
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

// TX queue management functions
static bool _tx_queue_is_empty(void) {
    return _app_vars.tx_queue_count == 0;
}

static bool _tx_queue_is_full(void) {
    return _app_vars.tx_queue_count >= TX_QUEUE_SIZE;
}

static bool _tx_queue_enqueue(const uint8_t *data, size_t length) {
    if (_tx_queue_is_full() || length > sizeof(_app_vars.tx_queue[0].buffer)) {
        return false;  // Queue full or frame too large
    }

    memcpy(_app_vars.tx_queue[_app_vars.tx_queue_head].buffer, data, length);
    _app_vars.tx_queue[_app_vars.tx_queue_head].length = length;
    _app_vars.tx_queue_head                            = (_app_vars.tx_queue_head + 1) % TX_QUEUE_SIZE;
    _app_vars.tx_queue_count++;
    return true;
}

static bool _tx_queue_dequeue(uint8_t *data, size_t *length) {
    if (_tx_queue_is_empty()) {
        return false;
    }

    *length = _app_vars.tx_queue[_app_vars.tx_queue_tail].length;
    memcpy(data, _app_vars.tx_queue[_app_vars.tx_queue_tail].buffer, *length);
    _app_vars.tx_queue_tail = (_app_vars.tx_queue_tail + 1) % TX_QUEUE_SIZE;
    _app_vars.tx_queue_count--;
    return true;
}

// Called from the main loop, once per received DMA buffer, in arrival order.
//
// Every byte of every buffer goes through the decoder: frames arrive back to
// back and one buffer can carry several, or the tail of one and the head of
// the next. A frame is decoded the moment it is READY, before the next byte
// goes in, because an opening flag overwrites an unconsumed READY state.
static void _uart_callback(uint8_t *buffer, size_t length) {
    for (size_t i = 0; i < length; i++) {
        mr_hdlc_state_t previous   = _app_vars.hdlc_state;
        mr_hdlc_state_t hdlc_state = mr_hdlc_rx_byte(buffer[i]);
        _app_vars.hdlc_state       = hdlc_state;
        if (hdlc_state == MR_HDLC_STATE_READY) {
            size_t msg_len = mr_hdlc_decode(_app_vars.hdlc_decode_buffer);
            if (msg_len == 0) {
                continue;
            }
            _dbg_vars.rx_frames_ok++;
            // A frame too large for a mailbox slot, or one arriving with the
            // ring full, is refused and counted there rather than written past
            // the end of shared memory.
            if (gateway_ipc_push(&ipc_shared_data.uart_to_radio, _app_vars.hdlc_decode_buffer, msg_len)) {
                NRF_IPC_S->TASKS_SEND[IPC_CHAN_UART_TO_RADIO] = 1;
            }
        } else if (hdlc_state == MR_HDLC_STATE_ERROR && previous != MR_HDLC_STATE_ERROR) {
            // The decoder stays in ERROR, dropping bytes, until the next
            // opening flag; only the transition is one torn frame.
            _dbg_vars.rx_hdlc_err++;
        }
    }
}

// Copy this core's counters into shared RAM. The net core reads them there
// when it builds gateway_info, so they only need to be as fresh as the
// slotframe that carries them.
static void _publish_stats(void) {
    const mr_uart_rx_stats_t *uart_stats = mr_uart_rx_stats(MR_UART_INDEX);

    ipc_shared_data.stats.uart_rx_bytes      = uart_stats->rx_bytes;
    ipc_shared_data.stats.uart_rx_hw_overrun = uart_stats->hw_overrun;
    ipc_shared_data.stats.uart_rx_hw_framing = uart_stats->hw_framing;
    ipc_shared_data.stats.uart_rx_hw_break   = uart_stats->hw_break;
    ipc_shared_data.stats.uart_rx_frames_ok  = _dbg_vars.rx_frames_ok;
    ipc_shared_data.stats.uart_rx_hdlc_err   = _dbg_vars.rx_hdlc_err;
    ipc_shared_data.stats.uart_rx_slot_full  = uart_stats->rx_slot_full;
    ipc_shared_data.stats.uart_tx_queue_drop = _dbg_vars.tx_queue_drop;

    // This core is the producer on the downlink ring, so it is the one that
    // knows when a message could not be handed over.
    ipc_shared_data.stats.ipc_u2r_lost = ipc_shared_data.uart_to_radio.dropped;
}

int main(void) {
    printf("Hello Mari Gateway App Core (UART) %016llX\n", mr_device_id());

    _setup_debug_pins();

    // Enable HFCLK with external 32MHz oscillator
    mr_hfclk_init();

    _configure_ram_non_secure(2, 1);
    _init_ipc();
    mr_uart_init(MR_UART_INDEX, &_mr_uart_rx_pin, &_mr_uart_tx_pin, MR_UART_BAUDRATE, &_uart_callback);

    _release_network_core();
    // this is a bit hacky -- sometimes it does not work without this
    NRF_RESET_S->NETWORK.FORCEOFF = 0;

    while (1) {
        __WFE();

        _publish_stats();

        mr_uart_rx_process(MR_UART_INDEX);

        // Process queued TX frames when conditions are right
        if (!_tx_queue_is_empty() && !mr_uart_tx_busy(MR_UART_INDEX)) {
            uint8_t frame_data[256];
            size_t  frame_len;

            if (_tx_queue_dequeue(frame_data, &frame_len)) {
                _app_vars.tx_frame_len = mr_hdlc_encode(frame_data, frame_len, _app_vars.hdlc_encode_buffer);
                // mr_gpio_set(&pin_dbg_uart_write);
                mr_uart_write(MR_UART_INDEX, _app_vars.hdlc_encode_buffer, _app_vars.tx_frame_len);
                // mr_gpio_clear(&pin_dbg_uart_write);
            }
        }

        // Handle deferred TX when UART becomes available (keep for compatibility)
        if (_app_vars.tx_pending && !mr_uart_tx_busy(MR_UART_INDEX)) {
            _app_vars.tx_pending = false;
            // mr_gpio_set(&pin_dbg_uart_write);
            mr_uart_write(MR_UART_INDEX, _app_vars.hdlc_encode_buffer, _app_vars.tx_frame_len);
            // mr_gpio_clear(&pin_dbg_uart_write);
        }
    }
}

void IPC_IRQHandler(void) {
    if (NRF_IPC_S->EVENTS_RECEIVE[IPC_CHAN_RADIO_TO_UART]) {
        NRF_IPC_S->EVENTS_RECEIVE[IPC_CHAN_RADIO_TO_UART] = 0;

        // Drain every message the net core has queued, not just one: the
        // doorbell rings once per message but says nothing about how many are
        // waiting, and a message left behind delays the next wake.
        volatile gateway_ipc_msg_t *msg;
        while ((msg = gateway_ipc_peek(&ipc_shared_data.radio_to_uart)) != NULL) {
            if (!_tx_queue_enqueue((const uint8_t *)msg->data, msg->len)) {
                _dbg_vars.tx_queue_drop++;
            }
            gateway_ipc_pop(&ipc_shared_data.radio_to_uart);
        }
    }
}
