/*
 * airam-rnnoise — מסנן סטרימינג להפחתת רעש (RNNoise v0.2, המודל המובנה).
 *
 * קלט:  f32le מונו 48kHz ב-stdin.   פלט: f32le מונו 48kHz ב-stdout.
 * חוליה אמצעית בשרשרת של scripts/airam-denoise (ffmpeg | airam-rnnoise | ffmpeg).
 * ר' docs/denoise-design.md ו-docs/audio-comfort-design.md.
 *
 *   airam-rnnoise                          # 100% מסונן (ההתנהגות המקורית)
 *   airam-rnnoise --mix 0.5 --delay 959    # 50% גולמי מיושר + 50% מסונן
 *
 * --mix A: חלק האות הגולמי בפלט (0 ≤ A < 1). פלט = A·גולמי + (1−A)·מסונן ⇒ שום
 *   רכיב לא מונחת ביותר מ-20·log10(A) — הברה שהמסנן טעה בה נחלשת, לא נעלמת.
 *   במעבדת ההאזנה (13.11.2026) 100% נשמע "רובוטי" בכל ההקלטות; 50% עבר.
 * --delay N: בכמה דגימות הפלט של RNNoise מאחר אחרי הקלט. הגולמי מושהה באותו
 *   N לפני הערבוב — בלי זה הערבוב הוא מסנן מסרק. נמדד ע"י scripts/airam-listening-lab
 *   (קורלציה צולבת על הקלטות אמיתיות). שגיאה של דגימה-שתיים אינה מורגשת:
 *   החריץ הראשון של מסנן המסרק נופל ב-24kHz/e, מעל ה-8kHz שנשארים אחרי ה-16k.
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
#include <string.h>
#include "rnnoise.h"

#define FRAME 480        /* rnnoise_get_frame_size() ב-v0.2 — נבדק בעלייה */
#define MAX_DELAY 48000  /* שנייה — הרבה מעל כל השהיה סבירה של RNNoise */

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

static int usage(void) {
    fprintf(stderr, "usage: airam-rnnoise [--mix A (0<=A<1)] [--delay N (0..%d)]\n", MAX_DELAY);
    return 2;
}

int main(int argc, char **argv) {
    float x[FRAME], dry[FRAME];
    float mix = 0.0f;
    long delay = 0;
    for (int i = 1; i < argc; i++) {
        char *end = NULL;
        if (!strcmp(argv[i], "--mix") && i + 1 < argc) {
            mix = strtof(argv[++i], &end);
            if (*end || !(mix >= 0.0f && mix < 1.0f))
                return usage();
        } else if (!strcmp(argv[i], "--delay") && i + 1 < argc) {
            delay = strtol(argv[++i], &end, 10);
            if (*end || delay < 0 || delay > MAX_DELAY)
                return usage();
        } else {
            return usage();
        }
    }
    if (rnnoise_get_frame_size() != FRAME) {
        fprintf(stderr, "airam-rnnoise: frame size %d != %d\n", rnnoise_get_frame_size(), FRAME);
        return 2;
    }
    /* קו השהיה לגולמי: ring של delay דגימות (רק כשמערבבים) */
    float *ring = NULL;
    long rpos = 0;
    if (mix > 0.0f && delay > 0) {
        ring = calloc((size_t)delay, sizeof(float));
        if (!ring)
            return 1;
    }
    DenoiseState *st = rnnoise_create(NULL);   /* NULL = המודל המובנה (rnnoise_data.c) */
    if (!st)
        return 1;
    const float wet_k = 1.0f - mix;
    while (read_full(x, FRAME)) {
        for (int i = 0; i < FRAME; i++) {
            if (!isfinite(x[i]))
                x[i] = 0.0f;
            dry[i] = x[i];
            x[i] *= 32768.0f;
        }
        rnnoise_process_frame(st, x, x);
        for (int i = 0; i < FRAME; i++) {
            float v = x[i] / 32768.0f;
            if (mix > 0.0f) {
                float d = dry[i];
                if (ring) {               /* d = הגולמי של לפני delay דגימות */
                    float old = ring[rpos];
                    ring[rpos] = d;
                    rpos = (rpos + 1) % delay;
                    d = old;
                }
                v = mix * d + wet_k * v;
            }
            x[i] = v > 1.0f ? 1.0f : (v < -1.0f ? -1.0f : v);
        }
        if (fwrite(x, sizeof(float), FRAME, stdout) != FRAME || fflush(stdout) != 0)
            break;
    }
    rnnoise_destroy(st);
    free(ring);
    return 0;
}
