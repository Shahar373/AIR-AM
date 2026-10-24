/*
 * airam-rnnoise — מסנן סטרימינג להפחתת רעש (RNNoise v0.2, המודל המובנה).
 *
 * קלט:  f32le מונו 48kHz ב-stdin.   פלט: f32le מונו 48kHz ב-stdout.
 * חוליה אמצעית בשרשרת של scripts/airam-denoise (ffmpeg | airam-rnnoise | ffmpeg).
 * ר' docs/denoise-design.md.
 *
 * למה לא rnnoise_demo הרשמי (נבדק, לא הנחה):
 *   1. fread()+feof() — קריאה חלקית מצינור (תקינה לגמרי) נחשבת אצלו לסוף פריים
 *      ומזיזה את כל הזרם; כאן read_full משלים פריים מלא.
 *   2. אין fflush — הפלט יושב ב-buffer של stdio (4KB+) => השהיה מיותרת בשידור חי.
 *   3. ההמרה float->short בלי הגבלה: דגימה מעל 32767 *מתגלגלת* לערך הפוך
 *      (נצפה בניסוי — 550 קפיצות בקטע אחד). כאן הפלט נחתך ל-±1.0.
 *
 * RNNoise עובד בסקאלת int16 (±32768) גם כשהוא מקבל float — לכן ההכפלה/החלוקה.
 * יציאה: EOF בקלט / כשל כתיבה (SIGPIPE ממורד הזרם) => יוצאים, וכל השרשרת
 * יוצאת איתנו (systemd מאתחל את היחידה כולה — אין "חצי צינור" תקוע).
 */
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include "rnnoise.h"

#define FRAME 480   /* rnnoise_get_frame_size() ב-v0.2 — נבדק בעלייה */

static int read_full(float *buf, size_t n) {
    size_t got = 0;
    while (got < n) {
        size_t r = fread(buf + got, sizeof(float), n - got, stdin);
        if (r == 0)
            return 0;   /* EOF/שגיאה — פריים חלקי בסוף הזרם נזרק */
        got += r;
    }
    return 1;
}

int main(void) {
    float x[FRAME];
    if (rnnoise_get_frame_size() != FRAME) {
        fprintf(stderr, "airam-rnnoise: frame size %d != %d\n", rnnoise_get_frame_size(), FRAME);
        return 2;
    }
    DenoiseState *st = rnnoise_create(NULL);   /* NULL = המודל המובנה (rnnoise_data.c) */
    if (!st)
        return 1;
    while (read_full(x, FRAME)) {
        for (int i = 0; i < FRAME; i++)
            x[i] = isfinite(x[i]) ? x[i] * 32768.0f : 0.0f;
        rnnoise_process_frame(st, x, x);
        for (int i = 0; i < FRAME; i++) {
            float v = x[i] / 32768.0f;
            x[i] = v > 1.0f ? 1.0f : (v < -1.0f ? -1.0f : v);
        }
        if (fwrite(x, sizeof(float), FRAME, stdout) != FRAME || fflush(stdout) != 0)
            break;
    }
    rnnoise_destroy(st);
    return 0;
}
