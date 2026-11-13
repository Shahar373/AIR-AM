/* ר' rnnoise.h כאן — השהיה טהורה של STUB_DELAY דגימות במקום הפחתת רעש. */
#include <stdlib.h>
#include <string.h>
#include "rnnoise.h"

#ifndef STUB_DELAY
#define STUB_DELAY 959
#endif

struct DenoiseState { float hist[STUB_DELAY + 480]; };

int rnnoise_get_frame_size(void) { return 480; }
DenoiseState *rnnoise_create(RNNModel *model) { (void)model; return calloc(1, sizeof(DenoiseState)); }
void rnnoise_destroy(DenoiseState *st) { free(st); }

float rnnoise_process_frame(DenoiseState *st, float *out, const float *in) {
    float tmp[480];
    memcpy(tmp, in, sizeof tmp);
    /* hist = [STUB_DELAY דגימות קודמות][הפריים הנוכחי] */
    memcpy(st->hist + STUB_DELAY, tmp, sizeof tmp);
    memcpy(out, st->hist, sizeof tmp);
    memmove(st->hist, st->hist + 480, STUB_DELAY * sizeof(float));
    return 0.0f;
}
