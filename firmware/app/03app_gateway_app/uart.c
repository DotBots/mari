/**
 * @file
 * @ingroup bsp_uart
 *
 * @brief  nRF52833-specific definition of the "uart" bsp module.
 *
 * @author Alexandre Abadie <alexandre.abadie@inria.fr>
 *
 * @copyright Inria, 2022
 */

#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <stdbool.h>
#include <nrf.h>
#include <nrf_peripherals.h>

#include "mr_gpio.h"
#include "uart.h"

//=========================== defines ==========================================

#if defined(NRF5340_XXAA) && defined(NRF_APPLICATION)
#define NRF_POWER      (NRF_POWER_S)
#define NRF_UART_TIMER (NRF_TIMER2_S)
#define TIMER_CC_NUM   TIMER2_CC_NUM
#define TIMER_IRQ      TIMER2_IRQn
#if defined(NRF_TRUSTZONE_NONSECURE)
#define NRF_UART_DPPIC (NRF_DPPIC_NS)
#else
#define NRF_UART_DPPIC (NRF_DPPIC_S)
#endif
#elif defined(NRF5340_XXAA) && defined(NRF_NETWORK)
#define NRF_POWER      (NRF_POWER_NS)
#define NRF_UART_TIMER (NRF_TIMER2_NS)
#define TIMER_CC_NUM   TIMER2_CC_NUM
#define TIMER_IRQ      TIMER2_IRQn
#define NRF_UART_DPPIC (NRF_DPPIC_NS)
#else
#error "the gateway UART RX path needs the nRF5340 UARTE and DPPI"
#endif

#define MR_UARTE_CHUNK_SIZE (64U)

// Reception rotates through a small set of EasyDMA buffers. The hardware
// shortcut ENDRX_STARTRX begins the next transfer the instant one ends, and
// RXD.PTR is double-buffered (nRF5340 PS 7.38.3), so the pointer for transfer
// N+1 is written while N is still filling: no software step sits between two
// buffers, and no byte arrives with the receiver stopped.
//
// A slot is staged only while fewer than UART_RX_SLOT_COUNT are spoken for, so
// from a drained start every slot fills before any byte is discarded: the main
// loop can fall behind by UART_RX_SLOT_COUNT * UART_RX_SLOT_SIZE, 2.56 ms of
// line-rate input at 1 Mbps. The worst-case main-loop pass is well under that;
// uart_rx_slot_full is the counter that says otherwise.
#define UART_RX_SLOT_SIZE    (64U)
#define UART_RX_SLOT_COUNT   (4U)
#define UART_RX_SLOT_NONE    (0xFFU)   ///< the transfer is going to the discard buffer
#define UART_RX_FLUSH_SIZE   (8U)      ///< FLUSHRX needs RXD.MAXCNT > 4 (PS 7.38.3)
#define UART_RX_IDLE_US      (200U)    ///< line quiet for this long -> flush the partial slot
#define UART_RX_EVENT_SPIN   (10000U)  ///< bound on waiting for a UARTE event, ~ms at 128 MHz
#define UART_RX_DPPI_CHANNEL (0U)

typedef struct {
    NRF_UARTE_Type *p;
    IRQn_Type       irq;
} uart_conf_t;

typedef struct {
    uint8_t  data[UART_RX_SLOT_SIZE];
    uint16_t len;
} uart_rx_slot_t;

typedef struct {
    uart_rx_slot_t     rx_slots[UART_RX_SLOT_COUNT];   ///< the DMA buffer rotation
    uint8_t            rx_discard[UART_RX_SLOT_SIZE];  ///< absorbs bytes while no slot is free
    uint8_t            rx_flush[UART_RX_FLUSH_SIZE];   ///< FLUSHRX target for the RX FIFO residue
    uint8_t            rx_fill;                        ///< slot the DMA is filling, UART_RX_SLOT_NONE for the discard buffer
    uint8_t            rx_next;                        ///< slot staged in RXD.PTR, UART_RX_SLOT_NONE for the discard buffer
    uint8_t            rx_stage;                       ///< ring index of the next slot to hand to the DMA
    uint8_t            rx_read;                        ///< ring index the consumer reads next
    volatile uint32_t  rx_produced;                    ///< slots filled, written by the ISR only
    volatile uint32_t  rx_consumed;                    ///< slots drained, written by mr_uart_rx_process only
    uart_rx_cb_t       callback;                       ///< pointer to the callback function
    uint8_t           *tx_buffer;                      ///< current TX buffer
    size_t             tx_length;                      ///< total bytes to transmit
    size_t             tx_pos;                         ///< current position in TX buffer
    bool               tx_busy;                        ///< flag indicating TX is in progress
    mr_uart_rx_stats_t rx_stats;                       ///< cumulative RX counters
} uart_vars_t;

//=========================== variables ========================================

static const uart_conf_t _devs[UARTE_COUNT] = {
#if defined(NRF5340_XXAA) && defined(NRF_APPLICATION)
    {
#if defined(NRF_TRUSTZONE_NONSECURE)
        .p = NRF_UARTE0_NS,
#else
        .p = NRF_UARTE0_S,
#endif
        .irq = SERIAL0_IRQn,
    },
    {
#if defined(NRF_TRUSTZONE_NONSECURE)
        .p = NRF_UARTE1_NS,
#else
        .p = NRF_UARTE1_S,
#endif
        .irq = SERIAL1_IRQn,
    },
    {
#if defined(NRF_TRUSTZONE_NONSECURE)
        .p = NRF_UARTE2_NS,
#else
        .p = NRF_UARTE2_S,
#endif
        .irq = SERIAL2_IRQn,
    },
    {
#if defined(NRF_TRUSTZONE_NONSECURE)
        .p = NRF_UARTE3_NS,
#else
        .p = NRF_UARTE3_S,
#endif
        .irq = SERIAL3_IRQn,
    },
#elif defined(NRF5340_XXAA) && defined(NRF_NETWORK)
    {
        .p   = NRF_UARTE0_NS,
        .irq = SERIAL0_IRQn,
    },
#else
    {
        .p   = NRF_UARTE0,
        .irq = UARTE0_UART0_IRQn,
    },
    {
        .p   = NRF_UARTE1,
        .irq = UARTE1_IRQn,
    },
#endif
};

static uart_vars_t _uart_vars[UARTE_COUNT] = { 0 };  ///< variable handling the UART context
static uart_t      _uart_global_index      = 0;      ///< needed for the timer interrupt handler

//=========================== prototypes =======================================

static void _rx_setup(uart_t uart);
static void _rx_stage_next(uart_t uart);
static void _rx_flush_partial_slot(uart_t uart);

//=========================== private ==========================================

static uint32_t _rx_buffer_of(uart_vars_t *vars, uint8_t slot) {
    return (slot == UART_RX_SLOT_NONE) ? (uint32_t)vars->rx_discard
                                       : (uint32_t)vars->rx_slots[slot].data;
}

/// Spin until a UARTE event is raised, then clear it. Every caller waits on an
/// event the peripheral has already been told to produce, so the bound is only
/// there to keep a hardware surprise from wedging an interrupt handler.
static bool _rx_wait_event(volatile uint32_t *event) {
    for (uint32_t spin = 0; spin < UART_RX_EVENT_SPIN; spin++) {
        if (*event) {
            *event = 0;
            (void)*event;  // read back so the clear has landed before we move on
            return true;
        }
    }
    return false;
}

/// Choose the buffer the next transfer will use and point RXD.PTR at it.
///
/// Safe to call while a transfer is in flight: RXD.PTR is double-buffered, so
/// once STARTRX has latched it (RXSTARTED) the register belongs to the next
/// transfer and the current one is unaffected.
static void _rx_stage_next(uart_t uart) {
    uart_vars_t *vars = &_uart_vars[uart];

    uint32_t in_use = (vars->rx_produced - vars->rx_consumed) +
                      ((vars->rx_fill != UART_RX_SLOT_NONE) ? 1U : 0U);
    if (in_use < UART_RX_SLOT_COUNT) {
        vars->rx_next  = vars->rx_stage;
        vars->rx_stage = (uint8_t)((vars->rx_stage + 1U) % UART_RX_SLOT_COUNT);
    } else {
        // Nowhere to put the bytes. Point the DMA at the discard buffer rather
        // than stopping: a stopped receiver loses the bytes anyway and costs a
        // restart, and this way the loss is counted at ENDRX.
        vars->rx_next = UART_RX_SLOT_NONE;
    }

    _devs[uart].p->RXD.PTR    = _rx_buffer_of(vars, vars->rx_next);
    _devs[uart].p->RXD.MAXCNT = UART_RX_SLOT_SIZE;
}

/// Start reception into rx_fill, leaving rx_next staged behind it.
static void _rx_start(uart_t uart) {
    uart_vars_t *vars = &_uart_vars[uart];

    _devs[uart].p->RXD.PTR          = _rx_buffer_of(vars, vars->rx_fill);
    _devs[uart].p->RXD.MAXCNT       = UART_RX_SLOT_SIZE;
    _devs[uart].p->EVENTS_RXSTARTED = 0;
    _devs[uart].p->EVENTS_ENDRX     = 0;
    _devs[uart].p->SHORTS           = (UARTE_SHORTS_ENDRX_STARTRX_Enabled << UARTE_SHORTS_ENDRX_STARTRX_Pos);
    _devs[uart].p->TASKS_STARTRX    = 1;

    _rx_wait_event(&_devs[uart].p->EVENTS_RXSTARTED);
    _devs[uart].p->RXD.PTR    = _rx_buffer_of(vars, vars->rx_next);
    _devs[uart].p->RXD.MAXCNT = UART_RX_SLOT_SIZE;
}

static void _rx_setup(uart_t uart) {
    uart_vars_t *vars = &_uart_vars[uart];

    vars->rx_stage    = 0;
    vars->rx_read     = 0;
    vars->rx_produced = 0;
    vars->rx_consumed = 0;
    vars->rx_fill     = UART_RX_SLOT_NONE;

    _rx_stage_next(uart);  // picks slot 0
    vars->rx_fill = vars->rx_next;
    vars->rx_next = UART_RX_SLOT_NONE;
    _rx_stage_next(uart);  // picks slot 1
    _rx_start(uart);
}

/// Deliver a slot that stopped short of full because the line went quiet.
///
/// Runs at the same interrupt priority as the UARTE handler, which is what
/// makes it safe to poll ENDRX and RXTO here: neither handler can preempt the
/// other, so the events cannot be consumed mid-sequence. The receiver is only
/// stopped for the few microseconds of the sequence, and it stops after the
/// line has already been idle for UART_RX_IDLE_US.
static void _rx_flush_partial_slot(uart_t uart) {
    uart_vars_t    *vars = &_uart_vars[uart];
    NRF_UARTE_Type *p    = _devs[uart].p;

    // PS figure 238 draws the forced stop with the shortcut off. Left on, the
    // explicit ENDRX would restart reception into the staged buffer while the
    // stop and flush are still running.
    p->SHORTS = 0;

    p->EVENTS_ENDRX = 0;
    p->EVENTS_RXTO  = 0;
    p->TASKS_STOPRX = 1;

    uint32_t received = 0;
    if (_rx_wait_event(&p->EVENTS_ENDRX)) {
        received = p->RXD.AMOUNT;
    }
    _rx_wait_event(&p->EVENTS_RXTO);

    // The internal RX FIFO can still hold bytes that belong to the transfer
    // that just ended. They only reach RAM via FLUSHRX, into a buffer that is
    // not the one already holding data.
    uint32_t flushed = 0;
    p->RXD.PTR       = (uint32_t)vars->rx_flush;
    p->RXD.MAXCNT    = UART_RX_FLUSH_SIZE;
    p->EVENTS_ENDRX  = 0;
    p->TASKS_FLUSHRX = 1;
    if (_rx_wait_event(&p->EVENTS_ENDRX)) {
        flushed = p->RXD.AMOUNT;
    }

    vars->rx_stats.rx_bytes += received + flushed;

    if (vars->rx_fill == UART_RX_SLOT_NONE) {
        if (received + flushed > 0) {
            vars->rx_stats.rx_slot_full++;
        }
        vars->rx_fill = vars->rx_next;
        _rx_stage_next(uart);
    } else {
        uart_rx_slot_t *slot = &vars->rx_slots[vars->rx_fill];
        slot->len            = (uint16_t)received;
        for (uint32_t i = 0; i < flushed && slot->len < UART_RX_SLOT_SIZE; i++) {
            slot->data[slot->len++] = vars->rx_flush[i];
        }
        if (slot->len > 0) {
            vars->rx_produced++;
            vars->rx_fill = vars->rx_next;
            _rx_stage_next(uart);
        }
        // An empty slot keeps its place in the rotation: reception resumes
        // into it and the buffer staged behind it is still the right one.
    }

    _rx_start(uart);
}

//=========================== public ===========================================

void mr_uart_init(uart_t uart, const mr_gpio_t *rx_pin, const mr_gpio_t *tx_pin, uint32_t baudrate, uart_rx_cb_t callback) {
    _uart_global_index = uart;

#if defined(NRF5340_XXAA)
    if (baudrate > 460800) {
        // On nrf53 configure constant latency mode for better performances with high baudrates
        NRF_POWER->TASKS_CONSTLAT = 1;
    }
#endif

    // configure UART pins (RX as input, TX as output);
    mr_gpio_init(rx_pin, MR_GPIO_IN_PU);
    mr_gpio_init(tx_pin, MR_GPIO_OUT);

    // configure UART
    _devs[uart].p->CONFIG   = 0;
    _devs[uart].p->PSEL.RXD = (rx_pin->port << UARTE_PSEL_RXD_PORT_Pos) |
                              (rx_pin->pin << UARTE_PSEL_RXD_PIN_Pos) |
                              (UARTE_PSEL_RXD_CONNECT_Connected << UARTE_PSEL_RXD_CONNECT_Pos);
    _devs[uart].p->PSEL.TXD = (tx_pin->port << UARTE_PSEL_TXD_PORT_Pos) |
                              (tx_pin->pin << UARTE_PSEL_TXD_PIN_Pos) |
                              (UARTE_PSEL_TXD_CONNECT_Connected << UARTE_PSEL_TXD_CONNECT_Pos);
    _devs[uart].p->PSEL.RTS = 0xffffffff;  // pin disconnected
    _devs[uart].p->PSEL.CTS = 0xffffffff;  // pin disconnected

    // configure baudrate
    switch (baudrate) {
        case 1200:
            _devs[uart].p->BAUDRATE = (UARTE_BAUDRATE_BAUDRATE_Baud1200 << UARTE_BAUDRATE_BAUDRATE_Pos);
            break;
        case 9600:
            _devs[uart].p->BAUDRATE = (UARTE_BAUDRATE_BAUDRATE_Baud9600 << UARTE_BAUDRATE_BAUDRATE_Pos);
            break;
        case 14400:
            _devs[uart].p->BAUDRATE = (UARTE_BAUDRATE_BAUDRATE_Baud14400 << UARTE_BAUDRATE_BAUDRATE_Pos);
            break;
        case 19200:
            _devs[uart].p->BAUDRATE = (UARTE_BAUDRATE_BAUDRATE_Baud19200 << UARTE_BAUDRATE_BAUDRATE_Pos);
            break;
        case 28800:
            _devs[uart].p->BAUDRATE = (UARTE_BAUDRATE_BAUDRATE_Baud28800 << UARTE_BAUDRATE_BAUDRATE_Pos);
            break;
        case 31250:
            _devs[uart].p->BAUDRATE = (UARTE_BAUDRATE_BAUDRATE_Baud31250 << UARTE_BAUDRATE_BAUDRATE_Pos);
            break;
        case 38400:
            _devs[uart].p->BAUDRATE = (UARTE_BAUDRATE_BAUDRATE_Baud38400 << UARTE_BAUDRATE_BAUDRATE_Pos);
            break;
        case 56000:
            _devs[uart].p->BAUDRATE = (UARTE_BAUDRATE_BAUDRATE_Baud56000 << UARTE_BAUDRATE_BAUDRATE_Pos);
            break;
        case 57600:
            _devs[uart].p->BAUDRATE = (UARTE_BAUDRATE_BAUDRATE_Baud57600 << UARTE_BAUDRATE_BAUDRATE_Pos);
            break;
        case 76800:
            _devs[uart].p->BAUDRATE = (UARTE_BAUDRATE_BAUDRATE_Baud76800 << UARTE_BAUDRATE_BAUDRATE_Pos);
            break;
        case 115200:
            _devs[uart].p->BAUDRATE = (UARTE_BAUDRATE_BAUDRATE_Baud115200 << UARTE_BAUDRATE_BAUDRATE_Pos);
            break;
        case 230400:
            _devs[uart].p->BAUDRATE = (UARTE_BAUDRATE_BAUDRATE_Baud230400 << UARTE_BAUDRATE_BAUDRATE_Pos);
            break;
        case 250000:
            _devs[uart].p->BAUDRATE = (UARTE_BAUDRATE_BAUDRATE_Baud250000 << UARTE_BAUDRATE_BAUDRATE_Pos);
            break;
        case 460800:
            _devs[uart].p->BAUDRATE = (UARTE_BAUDRATE_BAUDRATE_Baud460800 << UARTE_BAUDRATE_BAUDRATE_Pos);
            break;
        case 921600:
            _devs[uart].p->BAUDRATE = (UARTE_BAUDRATE_BAUDRATE_Baud921600 << UARTE_BAUDRATE_BAUDRATE_Pos);
            break;
        case 1000000:
            _devs[uart].p->BAUDRATE = (UARTE_BAUDRATE_BAUDRATE_Baud1M << UARTE_BAUDRATE_BAUDRATE_Pos);
            break;
        default:
            // error, return without enabling UART
            return;
    }

    _devs[uart].p->ENABLE = (UARTE_ENABLE_ENABLE_Enabled << UARTE_ENABLE_ENABLE_Pos);

    if (callback) {
        // configure the UART for RX

        _uart_vars[uart].callback = callback;

        // setup the RX interrupt. ERROR is enabled so that a byte the internal
        // RX FIFO had to drop shows up as ERRORSRC.OVERRUN instead of being
        // invisible.
        _devs[uart].p->ERRORSRC = _devs[uart].p->ERRORSRC;
        _devs[uart].p->INTENSET = (UARTE_INTENSET_ENDRX_Enabled << UARTE_INTENSET_ENDRX_Pos) |
                                  (UARTE_INTENSET_ERROR_Enabled << UARTE_INTENSET_ERROR_Pos);

        NVIC_EnableIRQ(_devs[uart].irq);
        NVIC_SetPriority(_devs[uart].irq, MR_UART_IRQ_PRIORITY);
        NVIC_ClearPendingIRQ(_devs[uart].irq);

        // The idle-line timer. Every received byte clears and restarts it over
        // DPPI, so it only reaches its compare value when the line has been
        // quiet for UART_RX_IDLE_US - which is when a slot that stopped short
        // of full needs delivering. Stopping on compare keeps it silent while
        // the line stays idle.
        NRF_UART_TIMER->TASKS_STOP           = 1;
        NRF_UART_TIMER->TASKS_CLEAR          = 1;
        NRF_UART_TIMER->PRESCALER            = 4;  // Run TIMER at 1MHz
        NRF_UART_TIMER->BITMODE              = (TIMER_BITMODE_BITMODE_32Bit << TIMER_BITMODE_BITMODE_Pos);
        NRF_UART_TIMER->CC[TIMER_CC_NUM - 1] = UART_RX_IDLE_US;
        NRF_UART_TIMER->SHORTS               = (1 << (TIMER_SHORTS_COMPARE0_STOP_Pos + TIMER_CC_NUM - 1));
        NRF_UART_TIMER->INTENSET             = (1 << (TIMER_INTENSET_COMPARE0_Pos + TIMER_CC_NUM - 1));

        // The flush sequence polls UARTE events that the UART handler also
        // consumes, so the two must not preempt each other: same priority.
        NVIC_SetPriority(TIMER_IRQ, MR_UART_IRQ_PRIORITY);
        NVIC_EnableIRQ(TIMER_IRQ);

        _devs[uart].p->PUBLISH_RXDRDY = (UART_RX_DPPI_CHANNEL << UARTE_PUBLISH_RXDRDY_CHIDX_Pos) |
                                        (UARTE_PUBLISH_RXDRDY_EN_Enabled << UARTE_PUBLISH_RXDRDY_EN_Pos);
        NRF_UART_TIMER->SUBSCRIBE_CLEAR = (UART_RX_DPPI_CHANNEL << TIMER_SUBSCRIBE_CLEAR_CHIDX_Pos) |
                                          (TIMER_SUBSCRIBE_CLEAR_EN_Enabled << TIMER_SUBSCRIBE_CLEAR_EN_Pos);
        NRF_UART_TIMER->SUBSCRIBE_START = (UART_RX_DPPI_CHANNEL << TIMER_SUBSCRIBE_START_CHIDX_Pos) |
                                          (TIMER_SUBSCRIBE_START_EN_Enabled << TIMER_SUBSCRIBE_START_EN_Pos);
        NRF_UART_DPPIC->CHENSET = (1UL << UART_RX_DPPI_CHANNEL);

        _rx_setup(uart);
    }
}

void mr_uart_write(uart_t uart, uint8_t *buffer, size_t length) {
    // Don't start new TX if one is already in progress
    if (_uart_vars[uart].tx_busy) {
        return;
    }

    // Store TX state
    _uart_vars[uart].tx_buffer = buffer;
    _uart_vars[uart].tx_length = length;
    _uart_vars[uart].tx_pos    = 0;
    _uart_vars[uart].tx_busy   = true;

    // Enable TX interrupt
    _devs[uart].p->INTENSET |= (UARTE_INTENSET_ENDTX_Enabled << UARTE_INTENSET_ENDTX_Pos);

    // Start first chunk
    _devs[uart].p->EVENTS_ENDTX  = 0;
    _devs[uart].p->TXD.PTR       = (uint32_t)&buffer[0];
    size_t chunk_size            = (length > MR_UARTE_CHUNK_SIZE) ? MR_UARTE_CHUNK_SIZE : length;
    _devs[uart].p->TXD.MAXCNT    = chunk_size;
    _devs[uart].p->TASKS_STARTTX = 1;
}

bool mr_uart_tx_busy(uart_t uart) {
    return _uart_vars[uart].tx_busy;
}

const mr_uart_rx_stats_t *mr_uart_rx_stats(uart_t uart) {
    return &_uart_vars[uart].rx_stats;
}

void mr_uart_rx_process(uart_t uart) {
    uart_vars_t *vars = &_uart_vars[uart];

    while (vars->rx_produced != vars->rx_consumed) {
        uart_rx_slot_t *slot = &vars->rx_slots[vars->rx_read];
        if (vars->callback && slot->len > 0) {
            vars->callback(slot->data, slot->len);
        }
        vars->rx_read = (uint8_t)((vars->rx_read + 1U) % UART_RX_SLOT_COUNT);
        vars->rx_consumed++;
    }
}

//=========================== interrupts =======================================

static void _uart_isr(uart_t uart) {

    // a byte was lost or corrupted on the wire; ERRORSRC latches the cause
    if (_devs[uart].p->EVENTS_ERROR) {
        _devs[uart].p->EVENTS_ERROR = 0;
        uint32_t errorsrc           = _devs[uart].p->ERRORSRC;
        _devs[uart].p->ERRORSRC     = errorsrc;  // write-one-to-clear
        if (errorsrc & (UARTE_ERRORSRC_OVERRUN_Present << UARTE_ERRORSRC_OVERRUN_Pos)) {
            _uart_vars[uart].rx_stats.hw_overrun++;
        }
        if (errorsrc & (UARTE_ERRORSRC_FRAMING_Present << UARTE_ERRORSRC_FRAMING_Pos)) {
            _uart_vars[uart].rx_stats.hw_framing++;
        }
        if (errorsrc & (UARTE_ERRORSRC_BREAK_Present << UARTE_ERRORSRC_BREAK_Pos)) {
            _uart_vars[uart].rx_stats.hw_break++;
        }
        // ERRORSRC.PARITY has no counter: CONFIG leaves parity excluded, so the
        // bit cannot be set.
    }

    // a buffer is full. The ENDRX_STARTRX shortcut has already begun the next
    // transfer into the buffer staged at the previous boundary, so this
    // handler only does bookkeeping and stages the one after that.
    if (_devs[uart].p->EVENTS_ENDRX) {
        _devs[uart].p->EVENTS_ENDRX = 0;
        (void)_devs[uart].p->EVENTS_ENDRX;

        uart_vars_t *vars   = &_uart_vars[uart];
        uint32_t     amount = _devs[uart].p->RXD.AMOUNT;
        vars->rx_stats.rx_bytes += amount;

        if (vars->rx_fill == UART_RX_SLOT_NONE) {
            if (amount > 0) {
                vars->rx_stats.rx_slot_full++;
            }
        } else {
            vars->rx_slots[vars->rx_fill].len = (uint16_t)amount;
            vars->rx_produced++;
        }

        vars->rx_fill = vars->rx_next;
        // RXSTARTED for the transfer the shortcut just began. It is raised
        // before this handler can run, so the wait is a formality that makes
        // the double-buffer contract explicit rather than assumed.
        _rx_wait_event(&_devs[uart].p->EVENTS_RXSTARTED);
        _rx_stage_next(uart);
    }

    // check if the interrupt was caused by TX completion
    if (_devs[uart].p->EVENTS_ENDTX) {
        _devs[uart].p->EVENTS_ENDTX = 0;

        // Update position
        _uart_vars[uart].tx_pos += MR_UARTE_CHUNK_SIZE;

        // Check if more chunks need to be sent
        if (_uart_vars[uart].tx_pos < _uart_vars[uart].tx_length) {
            // Send next chunk
            size_t remaining  = _uart_vars[uart].tx_length - _uart_vars[uart].tx_pos;
            size_t chunk_size = (remaining > MR_UARTE_CHUNK_SIZE) ? MR_UARTE_CHUNK_SIZE : remaining;

            _devs[uart].p->TXD.PTR       = (uint32_t)&_uart_vars[uart].tx_buffer[_uart_vars[uart].tx_pos];
            _devs[uart].p->TXD.MAXCNT    = chunk_size;
            _devs[uart].p->TASKS_STARTTX = 1;
        } else {
            // TX complete
            _uart_vars[uart].tx_busy = false;
            // Disable TX interrupt
            _devs[uart].p->INTENCLR = (UARTE_INTENCLR_ENDTX_Clear << UARTE_INTENCLR_ENDTX_Pos);
        }
    }
};

#if defined(NRF5340_XXAA)
void SERIAL0_IRQHandler(void) {
    _uart_isr(0);
}

#if defined(NRF5340_XXAA_APPLICATION)
void SERIAL1_IRQHandler(void) {
    _uart_isr(1);
}

void SERIAL2_IRQHandler(void) {
    _uart_isr(2);
}

void SERIAL3_IRQHandler(void) {
    _uart_isr(3);
}
#endif  // NRF5340_XXAA_APPLICATION

#else  // NRF5340_XXAA
void UARTE0_UART0_IRQHandler(void) {
    _uart_isr(0);
}

void UARTE1_IRQHandler(void) {
    _uart_isr(1);
}
#endif

void TIMER2_IRQHandler(void) {
    if (NRF_UART_TIMER->EVENTS_COMPARE[TIMER_CC_NUM - 1]) {
        NRF_UART_TIMER->EVENTS_COMPARE[TIMER_CC_NUM - 1] = 0;
        (void)NRF_UART_TIMER->EVENTS_COMPARE[TIMER_CC_NUM - 1];

        // No byte has arrived for UART_RX_IDLE_US, so a frame that ended part
        // way through a buffer is sitting there with no ENDRX coming.
        _rx_flush_partial_slot(_uart_global_index);
    }
}
