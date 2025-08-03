/**
 * @file
 * @ingroup     sec
 *
 * @brief       Security functions
 *
 * @author Geovane Fedrecheski <geovane.fedrecheski@inria.fr>
 *
 * @copyright Inria, 2025
 */

#include <stdint.h>
#include <stdlib.h>
#include <nrf.h>
#include <stdbool.h>
#include <string.h>

#include "sec.h"

#ifdef LAKERS_PSA
extern void mbedtls_memory_buffer_alloc_init(uint8_t *buf, size_t len);
#endif

//=========================== defines ==========================================

typedef struct {
    mr_edhoc_state_t state;
    int8_t edhoc_res;

    CredentialC    cred_i, fetched_cred_r;
    IdCred         id_cred_r;
    EdhocInitiator initiator;

    // used during execution of EDHOC
    EdhocMessageBuffer message_1;
    uint8_t            message_1_len;
    uint8_t            c_r;
    EdhocMessageBuffer message_2;
    EdhocMessageBuffer message_3;
    uint8_t            prk_out[SHA256_DIGEST_LEN];

    // used during execution of authz
    EadAuthzDevice   device;
    EadItemsC        ead_1, ead_2;
    BytesP256ElemLen authz_secret;
} sec_vars_t;

//=========================== variables ========================================

#define EDHOC_INITIATOR_INDEX 0

// -------- for EDHOC --------
static const uint8_t CRED_I[2][100] = {
    { 0xA2, 0x02, 0x70, 0x31, 0x32, 0x33, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39, 0x30, 0x61, 0x62, 0x63, 0x64, 0x65, 0x66, 0x08, 0xA1, 0x01, 0xA5, 0x01, 0x02, 0x02, 0x41, 0x01, 0x20, 0x01, 0x21, 0x58, 0x20, 0x52, 0x7C, 0x4D, 0x4C, 0x08, 0x9F, 0x9F, 0xE3, 0x33, 0x56, 0xAA, 0x97, 0xA1, 0xD6, 0x72, 0xDA, 0x32, 0xC1, 0x60, 0x08, 0x24, 0x4F, 0xEF, 0x37, 0xF0, 0x71, 0x54, 0xE0, 0x70, 0xE6, 0x6D, 0x1F, 0x22, 0x58, 0x20, 0x32, 0xE4, 0x6C, 0x45, 0xC4, 0xDD, 0xCB, 0x6D, 0x6C, 0x52, 0x4F, 0x37, 0x9D, 0x57, 0x15, 0x9D, 0x64, 0x2D, 0xD7, 0xF0, 0x27, 0x9C, 0x45, 0x50, 0xE3, 0x44, 0x48, 0xDA, 0xC4, 0x19, 0x53, 0x2C },
    { 0xa2, 0x02, 0x70, 0x31, 0x32, 0x33, 0x33, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39, 0x30, 0x61, 0x62, 0x63, 0x64, 0x65, 0x66, 0xa1, 0x01, 0xa5, 0x01, 0x02, 0x02, 0x41, 0x02, 0x20, 0x01, 0x21, 0x58, 0x20, 0xed, 0x47, 0xd7, 0xb6, 0xd0, 0x0c, 0x41, 0x4b, 0xa9, 0xfe, 0x1c, 0x9e, 0x6d, 0x2b, 0x07, 0x85, 0x45, 0x14, 0x36, 0x76, 0x6d, 0x5c, 0x0e, 0x65, 0xf3, 0xd7, 0xe3, 0x3b, 0x0d, 0x35, 0x4a, 0xd6, 0x22, 0x58, 0x20, 0x44, 0x3e, 0xda, 0x79, 0x2f, 0x81, 0x88, 0x44, 0xc8, 0x86, 0xbd, 0x1e, 0xc6, 0xfa, 0x0b, 0xd3, 0x61, 0xf8, 0xaa, 0xc9, 0xa8, 0xbc, 0xc2, 0x28, 0x65, 0x02, 0xaa, 0x9e, 0xb9, 0xea, 0xbb, 0xf4 },
};
static const BytesP256ElemLen I[2] = {
    { 0x1f, 0x7e, 0x4a, 0xe4, 0x29, 0x3a, 0x34, 0x8b, 0xf2, 0xb1, 0x36, 0x5c, 0xe0, 0x98, 0xaa, 0x49, 0xc2, 0x07, 0xbd, 0x1b, 0xa7, 0xdd, 0xde, 0xcd, 0xfa, 0xd6, 0x0c, 0xad, 0xe8, 0x2e, 0x9e, 0xf5 },
    { 0x3c, 0xa8, 0x54, 0xbf, 0xaa, 0x90, 0xda, 0x16, 0xe1, 0xa8, 0xfa, 0xcc, 0x0c, 0xd8, 0x34, 0x92, 0x7e, 0xc0, 0xb3, 0x19, 0x74, 0x8b, 0xb4, 0x79, 0xf1, 0x31, 0x6b, 0x8d, 0x38, 0x30, 0x74, 0xa8 },
};

// --------for EAD authz -----
static const uint8_t ID_U[2][4] = {
    { 0xa1, 0x04, 0x41, 0x01 },
    { 0xa1, 0x04, 0x41, 0x02 },
};
static const size_t           ID_U_LEN  = sizeof(ID_U[EDHOC_INITIATOR_INDEX]) / sizeof(ID_U[EDHOC_INITIATOR_INDEX][0]);
static const BytesP256ElemLen G_W       = { 0xFF, 0xA4, 0xF1, 0x02, 0x13, 0x40, 0x29, 0xB3, 0xB1, 0x56, 0x89, 0x0B, 0x88, 0xC9, 0xD9, 0x61, 0x95, 0x01, 0x19, 0x65, 0x74, 0x17, 0x4D, 0xCB, 0x68, 0xA0, 0x7D, 0xB0, 0x58, 0x8E, 0x4D, 0x41 };
static const uint8_t          LOC_W[]   = "http://localhost:18000";
static const uint8_t          LOC_W_LEN = (sizeof(LOC_W) / sizeof(LOC_W[0])) - 1;  // -1 to discard the \0 at the end
static const uint8_t          SS        = 2;

// -------- crypto backend -----
#ifdef LAKERS_PSA
uint8_t mbedtls_buffer[4096 * 2] = { 0 };
#endif

sec_vars_t sec_vars = { 0 };

//=========================== prototypes =======================================

//=========================== public ===========================================

void mr_sec_init(void) {
#ifdef LAKERS_PSA
    mbedtls_memory_buffer_alloc_init(mbedtls_buffer, 4096 * 2);
#endif
}

void mr_sec_edhoc_set_state(mr_edhoc_state_t state) {
    sec_vars.state = state;
}

int8_t mr_sec_edhoc_init(void) {
    int8_t res;

    res = credential_new(&sec_vars.cred_i, CRED_I[EDHOC_INITIATOR_INDEX], sizeof(CRED_I[EDHOC_INITIATOR_INDEX]) / sizeof(CRED_I[EDHOC_INITIATOR_INDEX][0]));
    if (res != 0) {
        return res;
    }

    res = initiator_new(&sec_vars.initiator);
    if (res != 0) {
        return res;
    }

    res = authz_device_new(&sec_vars.device, ID_U[EDHOC_INITIATOR_INDEX], ID_U_LEN, &G_W, LOC_W, LOC_W_LEN);
    if (res != 0) {
        return res;
    }

    return 0;
}

uint8_t mr_sec_edhoc_prepare_m1(uint8_t *msg_1, uint8_t *msg1_len) {
    // prepare message_1 and ead_1
    int8_t res = initiator_compute_ephemeral_secret(&sec_vars.initiator, &G_W, &sec_vars.authz_secret);
    if (res != 0) {
        return res;
    }

    // FIXME: re-add EAD item preparation
    res = authz_device_prepare_ead_1(&sec_vars.device, &sec_vars.authz_secret, SS, &sec_vars.ead_1);
    if (res != 0) {
        return res;
    };

    res = initiator_prepare_message_1(&sec_vars.initiator, NULL, &sec_vars.ead_1, &sec_vars.message_1);
    // res = initiator_prepare_message_1(&sec_vars.initiator, NULL, NULL, &sec_vars.message_1);
    if (res != 0) {
        return res;
    }

    // save h_message_1 for later
    memcpy(sec_vars.device.wait_ead2.h_message_1, sec_vars.initiator.wait_m2.h_message_1, SHA256_DIGEST_LEN);

    // copy message_1 out
    memcpy(msg_1, sec_vars.message_1.content, sec_vars.message_1.len);
    *msg1_len = sec_vars.message_1.len;

    return 0;
}

bool mr_sec_edhoc_is_m1_ready(void) {
    return sec_vars.state == EDHOC_M1_READY;
}

size_t mr_sec_edhoc_set_ready_message(uint8_t *buffer) {
    if (sec_vars.state != EDHOC_M1_READY) {
        return 0;
    }

    memcpy(buffer, sec_vars.message_1.content, sec_vars.message_1_len);
    return sec_vars.message_1_len;
}

void mr_sec_edhoc_event_loop(void) {
    sec_vars.edhoc_res = 0;
    switch (sec_vars.state) {
        case EDHOC_M1_PENDING: {
            sec_vars.edhoc_res = mr_sec_edhoc_init();
            if (sec_vars.edhoc_res != 0) {
                break;
            }
            sec_vars.edhoc_res = mr_sec_edhoc_prepare_m1(sec_vars.message_1.content, &sec_vars.message_1_len);
            if (sec_vars.edhoc_res != 0) {
                break;
            }
            mr_sec_edhoc_set_state(EDHOC_M1_READY);
            break;
        }
        default:
            break;
    }

    if (sec_vars.edhoc_res != 0) {
        sec_vars.state = EDHOC_ERROR;
        return;
    }
}
