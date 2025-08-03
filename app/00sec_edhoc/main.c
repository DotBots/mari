#include <nrf.h>
#include <stdio.h>
#include <stdbool.h>

#include "mr_timer_hf.h"
#include "sec.h"

//=========================== defines ==========================================

#define MARI_APP_TIMER_DEV 1

typedef struct {
    uint8_t m1[MAX_MESSAGE_SIZE_LEN];
    uint8_t m1_len;
} edhoc_vars_t;

//=========================== variables ========================================

edhoc_vars_t node_vars = { 0 };

//=========================== prototypes =======================================

//=========================== main =============================================

int main(void) {
    printf("\nHello Mari Node\n");
    int8_t   res = 0;
    uint32_t ts;

    mr_timer_hf_init(MARI_APP_TIMER_DEV);

    mr_sec_init();

    while (1) {
        ts  = mr_timer_hf_now(MARI_APP_TIMER_DEV);
        res = mr_sec_edhoc_init();
        printf("init time: %u\n", mr_timer_hf_now(MARI_APP_TIMER_DEV) - ts);
        if (res != 0) {
            printf("Error sec: %d\n", res);
        }

        ts  = mr_timer_hf_now(MARI_APP_TIMER_DEV);
        res = mr_sec_edhoc_prepare_m1(node_vars.m1, &node_vars.m1_len);
        printf("m1 time: %u\n", mr_timer_hf_now(MARI_APP_TIMER_DEV) - ts);
        if (res != 0) {
            printf("Error sec: %d\n", res);
        }

        __SEV();
        __WFE();
        __WFE();
    }
}
