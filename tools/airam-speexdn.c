/*
 * airam-speexdn — מסנן סטרימינג להפחתת רעש (SpeexDSP preprocess, קלאסי, בלי מודל).
 *
 * קלט:  f32le מונו 16kHz ב-stdin.   פלט: f32le מונו 16kHz ב-stdout.
 * חוליה אמצעית בשרשרת של scripts/airam-denoise כש-AIRAM_DENOISE_ENGINE=speex.
 * ר' docs/audio-comfort-design.md.
 *
 *   airam-speexdn            # תקרת הנחתה 15dB (ברירת המחדל — זו שעברה במעבדה)
 *   airam-speexdn -8         # תקרה אחרת (dB שלילי, ‎-1..-60)
 *
 * למה Speex: עובד ב-16kHz (הקצב של rtl_airband) — בלי העלאה ל-48k כמו RNNoise —
 * ויש לו תקרת הנחתה מובנית (NOISE_SUPPRESS), כך ששום רכיב לא נמחק לגמרי.
 * במעבדת ההאזנה (13.11.2026) תקרה של 15dB עברה; 8dB לא הוריד מספיק רעש.
 * רק DENOISE — AGC/VAD/DEREVERB כבויים: AGC הוא בדיוק סוג הנורמליזציה שנפסל
 * בגרסה הקודמת, ו-VAD היה מאפס קטעים שהוא "חושב" שאין בהם דיבור.
 *
 * ⚠ ההמרות float↔int16 זהות בדיוק ל-speex_denoise ב-scripts/airam-listening-lab
 * (‏×32767 עם חיתוך, ÷32768) — בדיקה ב-CI משווה פלט בייט-בבייט, כדי שמה שנשמע
 * במעבדה הוא מה שרץ במוצר.
 * יציאה: EOF בקלט / כשל כתיבה (SIGPIPE ממורד הזרם) => יוצאים, וכל השרשרת איתנו.
 */
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <speex/speex_preprocess.h>

#define RATE 16000
#define FRAME 320          /* 20ms */

static int read_full(float *buf, size_t n) {
    size_t got = 0;
    while (got < n) {
        size_t r = fread(buf + got, sizeof(float), n - got, stdin);
        if (r == 0)
            return 0;      /* EOF/שגיאה — פריים חלקי בסוף הזרם נזרק */
        got += r;
    }
    return 1;
}

int main(int argc, char **argv) {
    int suppress = -15;
    if (argc > 2)
        goto bad;
    if (argc == 2) {
        char *end = NULL;
        long v = strtol(argv[1], &end, 10);
        if (*end || v > -1 || v < -60)
            goto bad;
        suppress = (int)v;
    }
    SpeexPreprocessState *st = speex_preprocess_state_init(FRAME, RATE);
    if (!st)
        return 1;
    int on = 1, off = 0;
    speex_preprocess_ctl(st, SPEEX_PREPROCESS_SET_DENOISE, &on);
    speex_preprocess_ctl(st, SPEEX_PREPROCESS_SET_AGC, &off);
    speex_preprocess_ctl(st, SPEEX_PREPROCESS_SET_VAD, &off);
    speex_preprocess_ctl(st, SPEEX_PREPROCESS_SET_DEREVERB, &off);
    speex_preprocess_ctl(st, SPEEX_PREPROCESS_SET_NOISE_SUPPRESS, &suppress);

    float x[FRAME];
    spx_int16_t s[FRAME];
    while (read_full(x, FRAME)) {
        for (int i = 0; i < FRAME; i++) {
            /* double ולא float: כמו Python במעבדה (אחרת עיגול שונה ⇒ פלט שונה בבייט) */
            double v = isfinite(x[i]) ? (double)x[i] * 32767.0 : 0.0;
            /* (int) קוטם לכיוון אפס — כמו int() ב-Python */
            int q = v >= 32767.0 ? 32767 : (v <= -32768.0 ? -32768 : (int)v);
            s[i] = (spx_int16_t)q;
        }
        speex_preprocess_run(st, s);
        for (int i = 0; i < FRAME; i++)
            x[i] = s[i] / 32768.0f;
        if (fwrite(x, sizeof(float), FRAME, stdout) != FRAME || fflush(stdout) != 0)
            break;
    }
    speex_preprocess_state_destroy(st);
    return 0;
bad:
    fprintf(stderr, "usage: airam-speexdn [SUPPRESS_DB (-1..-60, default -15)]\n");
    return 2;
}
