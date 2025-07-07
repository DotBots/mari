#ifndef __IPC_H
#define __IPC_H

/**
 * @defgroup    bsp_ipc Inter-Processor Communication
 * @ingroup     bsp
 * @brief       Control the IPC peripheral (nRF53 only)
 *
 * @{
 * @file
 * @author Alexandre Abadie <alexandre.abadie@inria.fr>
 * @copyright Inria, 2023
 * @}
 */

#include <nrf.h>
#include <stdbool.h>
#include <stdint.h>

#if defined(NRF_APPLICATION)
#define NRF_MUTEX NRF_MUTEX_NS
#elif defined(NRF_NETWORK)
#define NRF_MUTEX NRF_APPMUTEX_NS
#endif

#define IPC_IRQ_PRIORITY (0)

typedef enum {
    IPC_CHAN_RADIO_TO_UART = 0,  ///< Channel used for radio RX events
    IPC_CHAN_UART_TO_RADIO = 1,  ///< Channel used for radio RX events
} ipc_channels_t;

typedef struct __attribute__((packed)) {
    bool    net_ready;                    ///< Network core is ready
    bool    net_ack;                      ///< Network core acked the latest request
    uint8_t radio_to_uart[UINT8_MAX];     ///< Data received from the network core
    uint8_t radio_to_uart_len;            ///< Length of the data received from the network core
    uint8_t uart_to_radio_tx[UINT8_MAX];  ///< Data to send to the network
    uint8_t uart_to_radio_len;            ///< Length of the data to send to the network core
} ipc_shared_data_t;

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
