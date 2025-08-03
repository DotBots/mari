/**
 * @file
 * @ingroup     sec
 *
 * @brief       Mock security functions
 *
 * @note        This is kind of silly, but I need it so it also compiles
 *              for the nRF5340 DK.
 *
 * @author Geovane Fedrecheski <geovane.fedrecheski@inria.fr>
 *
 * @copyright Inria, 2025
 */

#include <stdint.h>
#include <stdbool.h>
#include <stdlib.h>

#include "sec.h"

void mr_sec_init(void) {
}

void mr_sec_edhoc_set_state(mr_edhoc_state_t state) {
    (void)state;
}

int8_t mr_sec_edhoc_init(void) {
    return 0;
}

uint8_t mr_sec_edhoc_prepare_m1(uint8_t *msg_1, uint8_t *msg1_len) {
    (void)msg_1;
    (void)msg1_len;
    return 0;
}

bool mr_sec_edhoc_is_m1_ready(void) {
    return false;
}

size_t mr_sec_edhoc_set_ready_message(uint8_t *buffer) {
    (void)buffer;
    return 0;
}

void mr_sec_edhoc_event_loop(void) {
}
