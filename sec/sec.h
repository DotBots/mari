#ifndef __SEC_H
#define __SEC_H

/**
 * @defgroup    sec      security
 * @ingroup     security
 * @brief       Security module
 *
 * @{
 * @file
 * @author Geovane Fedrecheski <geovane.fedrecheski@inria.fr>
 * @copyright Inria, 2025-now
 * @}
 */

#include <stdint.h>
#include <stdlib.h>
#include <nrf.h>
#include <stdbool.h>

#if defined(NRF52840_XXAA)
#include "lakers.h"
#include "lakers_shared.h"
#include "lakers_ead_authz.h"
#endif

typedef enum {
    EDHOC_NONE = 1,
    EDHOC_M1_PENDING,
    EDHOC_M1_READY,
    EDHOC_M1_SENT,
    EDHOC_ERROR = 255,
} mr_edhoc_state_t;

void    mr_sec_init(void);
void    mr_sec_edhoc_set_state(mr_edhoc_state_t state);
int8_t  mr_sec_edhoc_init(void);
uint8_t mr_sec_edhoc_prepare_m1(uint8_t *msg_1, uint8_t *msg1_len);
bool    mr_sec_edhoc_is_m1_ready(void);
size_t  mr_sec_edhoc_set_ready_message(uint8_t *buffer);

void mr_sec_edhoc_event_loop(void);

#endif  // __SEC_H
