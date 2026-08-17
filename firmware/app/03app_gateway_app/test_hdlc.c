/**
 * @file
 * @ingroup drv_hdlc
 *
 * @brief  Host test for the gateway's HDLC decoder.
 *
 * hdlc.c needs nothing but stdint and stdbool, so it compiles and runs on a
 * development machine. Build and run from this directory:
 *
 *     cc -o test_hdlc test_hdlc.c hdlc.c && ./test_hdlc
 *
 * The vectors in test_hdlc_vectors.h are produced by marilib's hdlc_encode
 * (see test_hdlc_vectors.py), so the two implementations are checked against
 * each other rather than against a second copy of the same logic.
 *
 * Each vector is replayed at several chunk sizes. The gateway receives into
 * rotating DMA buffers whose boundaries fall wherever the wire puts them, so
 * "the same stream cut in different places decodes to the same frames" is the
 * property that matters.
 */

#include <stdio.h>
#include <string.h>

#include "hdlc.h"
#include "test_hdlc_vectors.h"

#define MAX_FRAMES     16
#define MAX_FRAME_SIZE 1024

typedef struct {
    uint8_t frames[MAX_FRAMES][MAX_FRAME_SIZE];
    size_t  frame_lens[MAX_FRAMES];
    size_t  frame_count;
    size_t  errors;
    size_t  overflow;
} decode_result_t;

/**
 * Replay a stream the way the gateway does: one callback per chunk, every byte
 * of every chunk fed to the decoder, and a READY frame decoded before the next
 * byte goes in - an opening flag overwrites an unconsumed READY state.
 */
static void _replay(const uint8_t *stream, size_t stream_len, size_t chunk, decode_result_t *out) {
    memset(out, 0, sizeof(*out));
    mr_hdlc_reset();
    mr_hdlc_state_t previous = MR_HDLC_STATE_IDLE;

    for (size_t offset = 0; offset < stream_len; offset += chunk) {
        size_t len = (stream_len - offset < chunk) ? stream_len - offset : chunk;
        for (size_t i = 0; i < len; i++) {
            mr_hdlc_state_t state = mr_hdlc_rx_byte(stream[offset + i]);
            mr_hdlc_state_t was   = previous;
            previous              = state;
            if (state == MR_HDLC_STATE_READY) {
                uint8_t decoded[MAX_FRAME_SIZE];
                size_t  decoded_len = mr_hdlc_decode(decoded);
                if (decoded_len == 0) {
                    continue;
                }
                if (out->frame_count >= MAX_FRAMES) {
                    out->overflow++;
                    continue;
                }
                memcpy(out->frames[out->frame_count], decoded, decoded_len);
                out->frame_lens[out->frame_count] = decoded_len;
                out->frame_count++;
            } else if (state == MR_HDLC_STATE_ERROR && was != MR_HDLC_STATE_ERROR) {
                // The decoder stays in ERROR until the next opening flag, so
                // only the transition is one torn frame.
                out->errors++;
            }
        }
    }
}

static int _failures = 0;

static void _fail(const char *vector, size_t chunk, const char *what) {
    printf("FAIL  %-46s chunk %-4zu %s\n", vector, chunk, what);
    _failures++;
}

static void _check(const hdlc_vector_t *vec, size_t chunk) {
    decode_result_t result;
    _replay(vec->stream, vec->stream_len, chunk, &result);

    if (result.overflow) {
        _fail(vec->name, chunk, "more frames decoded than the harness can hold");
        return;
    }
    if (result.frame_count != vec->frame_count) {
        char detail[128];
        snprintf(detail, sizeof(detail), "decoded %zu frames, expected %zu",
                 result.frame_count, vec->frame_count);
        _fail(vec->name, chunk, detail);
        return;
    }
    if (result.errors != vec->expected_errors) {
        char detail[128];
        snprintf(detail, sizeof(detail), "saw %zu decode errors, expected %zu",
                 result.errors, vec->expected_errors);
        _fail(vec->name, chunk, detail);
        return;
    }
    for (size_t i = 0; i < vec->frame_count; i++) {
        if (result.frame_lens[i] != vec->frame_lens[i]) {
            char detail[128];
            snprintf(detail, sizeof(detail), "frame %zu is %zu bytes, expected %zu",
                     i, result.frame_lens[i], vec->frame_lens[i]);
            _fail(vec->name, chunk, detail);
            return;
        }
        if (memcmp(result.frames[i], vec->frames[i], vec->frame_lens[i]) != 0) {
            char detail[128];
            snprintf(detail, sizeof(detail), "frame %zu does not match the encoder's input", i);
            _fail(vec->name, chunk, detail);
            return;
        }
    }
}

/**
 * Encode a payload with this same file's encoder and decode it back.
 *
 * The decode vectors come from marilib, so nothing else here exercises
 * mr_hdlc_encode. It carries the uplink, and the largest thing it has to carry
 * is a full-size mari packet plus its type byte.
 */
static void _check_encode_roundtrip(size_t payload_len, uint8_t fill_kind) {
    uint8_t payload[MAX_FRAME_SIZE];
    uint8_t frame[MAX_FRAME_SIZE * 2];
    char    name[64];

    for (size_t i = 0; i < payload_len; i++) {
        // fill_kind 1 is every byte an HDLC flag or escape, the worst case for
        // the encoder because each one becomes two bytes on the wire
        payload[i] = fill_kind ? ((i & 1) ? 0x7E : 0x7D) : (uint8_t)(i * 7 + 3);
    }

    size_t frame_len = mr_hdlc_encode(payload, payload_len, frame);
    snprintf(name, sizeof(name), "encode round trip, %zu bytes, fill %u",
             payload_len, (unsigned)fill_kind);

    decode_result_t result;
    _replay(frame, frame_len, 64, &result);

    if (result.frame_count != 1 || result.errors != 0) {
        _fail(name, 64, "did not decode back to exactly one clean frame");
        return;
    }
    if (result.frame_lens[0] != payload_len ||
        memcmp(result.frames[0], payload, payload_len) != 0) {
        _fail(name, 64, "decoded payload differs from the encoder's input");
    }
}

int main(void) {
    // 1 and 3 tear frames at arbitrary places; 64 is the gateway's DMA buffer
    // size, so it is the split the wire actually produces; 4096 delivers every
    // stream whole.
    const size_t chunks[]  = { 1, 3, 7, 64, 65, 4096 };
    const size_t vec_count = sizeof(HDLC_VECTORS) / sizeof(HDLC_VECTORS[0]);
    size_t       run_count = 0;

    for (size_t v = 0; v < vec_count; v++) {
        for (size_t c = 0; c < sizeof(chunks) / sizeof(chunks[0]); c++) {
            _check(&HDLC_VECTORS[v], chunks[c]);
            run_count++;
        }
    }

    // 256 is GATEWAY_IPC_MSG_MAX_SIZE, the largest message the uplink carries
    const size_t sizes[] = { 1, 2, 64, 145, 254, 255, 256 };
    for (size_t s = 0; s < sizeof(sizes) / sizeof(sizes[0]); s++) {
        for (uint8_t fill = 0; fill < 2; fill++) {
            _check_encode_roundtrip(sizes[s], fill);
            run_count++;
        }
    }

    if (_failures) {
        printf("\n%d of %zu checks failed\n", _failures, run_count);
        return 1;
    }
    printf("%zu checks passed (%zu decode vectors x %zu chunk sizes, plus encode round trips)\n",
           run_count, vec_count, sizeof(chunks) / sizeof(chunks[0]));
    return 0;
}
