/* RNNoise *מזויף* לבדיקות (tests/test_denoise_tools.py) — לא חלק מההתקנה.
 * חותמות זהות ל-include/rnnoise.h של RNNoise v0.2, כדי ש-tools/airam-rnnoise.c
 * ייבנה מולו בלי שינוי. "המסנן" כאן הוא השהיה טהורה של STUB_DELAY דגימות —
 * מה שמאפשר לבדוק את קו ההשהיה והערבוב בלי המודל האמיתי. */
typedef struct DenoiseState DenoiseState;
typedef struct RNNModel RNNModel;
int rnnoise_get_frame_size(void);
DenoiseState *rnnoise_create(RNNModel *model);
void rnnoise_destroy(DenoiseState *st);
float rnnoise_process_frame(DenoiseState *st, float *out, const float *in);
