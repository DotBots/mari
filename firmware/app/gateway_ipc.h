#ifndef __GATEWAY_IPC_H
#define __GATEWAY_IPC_H

/**
 * @defgroup    gateway_ipc Gateway inter-core mailboxes
 * @ingroup     app
 * @brief       Shared-RAM message rings between the gateway's two cores
 *
 * Included by both 03app_gateway_app and 03app_gateway_net, which is the point:
 * the layout is a shared-memory ABI and one declaration is the only way the two
 * cores are guaranteed to agree on it.
 *
 * The name is deliberately not ipc.h. drv/mr_ipc.h declares a different
 * ipc_shared_data_t for the RNG channel, and swarmit's netcore build has that
 * directory on its include path, where two headers with the same name would be
 * resolved by include order alone.
 *
 * @{
 * @file
 * @author Alexandre Abadie <alexandre.abadie@inria.fr>
 * @copyright Inria, 2023
 * @}
 */

#include <nrf.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "models.h"

#if defined(NRF_APPLICATION)
#define NRF_MUTEX NRF_MUTEX_NS
#elif defined(NRF_NETWORK)
#define NRF_MUTEX NRF_APPMUTEX_NS
#endif

#define IPC_IRQ_PRIORITY (1)

typedef enum {
    IPC_CHAN_RADIO_TO_UART = 0,  ///< Channel used for radio RX events
    IPC_CHAN_UART_TO_RADIO = 1,  ///< Channel used for radio RX events
} ipc_channels_t;

/// A message is one edge-event type byte followed by a mari packet, so the
/// largest one is a full-size packet plus that byte.
#define GATEWAY_IPC_MSG_MAX_SIZE (MARI_PACKET_MAX_SIZE + 1)
#define GATEWAY_IPC_SLOT_COUNT   (4)

typedef struct {
    uint32_t len;
    uint8_t  data[GATEWAY_IPC_MSG_MAX_SIZE];
} gateway_ipc_msg_t;

/**
 * @brief Single-producer single-consumer message ring in shared RAM
 *
 * The hardware IPC block is a doorbell, not a queue: it raises an interrupt on
 * the other core, and the message itself travels through here. Ringing it twice
 * before the other core answers is what used to cost a message.
 *
 * `head` is written only by the producing core and `tail` only by the consuming
 * one, so neither needs a lock. Both are free-running counters rather than
 * wrapped indices, which is what keeps full (`head - tail == SLOT_COUNT`)
 * distinguishable from empty (`head == tail`). Every field is naturally
 * aligned and both cores build with the same ABI, so the layout matches without
 * packing.
 */
typedef struct {
    gateway_ipc_msg_t slots[GATEWAY_IPC_SLOT_COUNT];
    volatile uint32_t head;     ///< messages written, producer only
    volatile uint32_t tail;     ///< messages read, consumer only
    volatile uint32_t dropped;  ///< messages the producer had nowhere to put
} gateway_ipc_ring_t;

typedef struct {
    bool                    net_ready;      ///< Network core is ready
    gateway_ipc_ring_t      radio_to_uart;  ///< net core -> app core
    gateway_ipc_ring_t      uart_to_radio;  ///< app core -> net core
    mr_gateway_uart_stats_t stats;          ///< Counters reported to the host in gateway_info
} ipc_shared_data_t;

/**
 * @brief Copy a message into the ring
 *
 * Never blocks and never overwrites an unread message: a full ring, an empty
 * message or one too large for a slot is counted in `dropped` and refused.
 *
 * @return true if the message was accepted
 */
static inline bool gateway_ipc_push(volatile gateway_ipc_ring_t *ring, const uint8_t *data, size_t len) {
    if (len == 0 || len > GATEWAY_IPC_MSG_MAX_SIZE ||
        (ring->head - ring->tail) >= GATEWAY_IPC_SLOT_COUNT) {
        ring->dropped++;
        return false;
    }

    volatile gateway_ipc_msg_t *slot = &ring->slots[ring->head % GATEWAY_IPC_SLOT_COUNT];
    for (size_t i = 0; i < len; i++) {
        slot->data[i] = data[i];
    }
    slot->len = (uint32_t)len;

    // The other core must see the slot before it sees the head that publishes it
    __DMB();
    ring->head++;
    return true;
}

/**
 * @brief The oldest unread message, or NULL when the ring is empty
 *
 * Valid until gateway_ipc_pop releases it.
 */
static inline volatile gateway_ipc_msg_t *gateway_ipc_peek(volatile gateway_ipc_ring_t *ring) {
    if (ring->head == ring->tail) {
        return NULL;
    }
    __DMB();
    return &ring->slots[ring->tail % GATEWAY_IPC_SLOT_COUNT];
}

/**
 * @brief Release the message gateway_ipc_peek returned, freeing its slot
 */
static inline void gateway_ipc_pop(volatile gateway_ipc_ring_t *ring) {
    // Finish reading the slot before the producer is told it is free
    __DMB();
    ring->tail++;
}

/**
 * @brief Lock the mutex, blocks until the mutex is locked
 */
static inline void mutex_lock(void) {
    while (NRF_MUTEX->MUTEX[0]) {}
}

/**
 * @brief Unlock the mutex, has no effect if the mutex is already unlocked
 */
static inline void mutex_unlock(void) {
    NRF_MUTEX->MUTEX[0] = 0;
}

#endif
