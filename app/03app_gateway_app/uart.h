#ifndef __UART_H
#define __UART_H

/**
 * @defgroup    bsp_uart    UART
 * @ingroup     bsp
 * @brief       Control the UART peripheral
 *
 * @{
 * @file
 * @author Alexandre Abadie <alexandre.abadie@inria.fr>
 * @copyright Inria, 2022
 * @}
 */

#include <stdint.h>
#include <stdlib.h>
#include "mr_gpio.h"

//=========================== defines ==========================================

#define MR_UART_IRQ_PRIORITY (0)

typedef uint8_t uart_t;  ///< UART peripheral index

typedef void (*uart_rx_cb_t)(uint8_t data);  ///< Callback function prototype, it is called on each byte received

//=========================== public ===========================================

/**
 * @brief Initialize the UART interface
 *
 * @param[in] uart      UART peripheral to use
 * @param[in] rx_pin    pointer to RX pin
 * @param[in] tx_pin    pointer to TX pin
 * @param[in] baudrate  Baudrate in bauds
 * @param[in] callback  callback function called on each received byte
 */
void mr_uart_init(uart_t uart, const mr_gpio_t *rx_pin, const mr_gpio_t *tx_pin, uint32_t baudrate, uart_rx_cb_t callback);

/**
 * @brief   Write data on UART interface
 *
 * @param[in]   uart        UART interface to use
 * @param[in]   buffer      Buffer to write
 * @param[in]   length      Length of the buffer
 */
void mr_uart_write(uart_t uart, uint8_t *buffer, size_t length);

/**
 * @brief   Check if UART TX is busy
 *
 * @param[in]   uart        UART interface to check
 *
 * @return true if TX is in progress, false otherwise
 */
bool mr_uart_tx_busy(uart_t uart);

#endif
