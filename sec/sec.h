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

#include "lakers.h"
#include "lakers_shared.h"
#include "lakers_ead_authz.h"

typedef enum {
    EDHOC_M1_SENT,
} mr_edhoc_state_t;

void    mr_sec_init(void);
void    mr_sec_edhoc_set_state(mr_edhoc_state_t state);
int8_t  mr_sec_edhoc_init(void);
uint8_t mr_sec_edhoc_prepare_m1(uint8_t *msg_1, uint8_t *msg1_len);

#endif  // __SEC_H
