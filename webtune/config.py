# ============================================================================
#  AIR-AM — הגדרות ונתוני-ברירת-מחדל טהורים (בנקי תדרים, רווחים, מסנני ערוץ, מנועי סינון)
# ----------------------------------------------------------------------------
#  הוצא מ-app.py (שלב ג2) כדי ש-appstate (DEFAULT_STATE) לא ייצור ייבוא מעגלי. **נתונים בלבד**: בלי
#  Flask/state/systemd/IO. ⚠ הקוראים ניגשים כ-`config.<שם>` (לא `from config import`), כמו שאר המודולים.
# ============================================================================

# המנועים — רשימה סגורה, ורק מה שעבר את מעבדת ההאזנה העיוורת על הקלטות אמיתיות
# (docs/audio-comfort-design.md, 13.11.2026). RNNoise 100% + נורמליזציה (v2.29.0)
# נפסל שם ("לא טבעי" ב-3/3) ולכן אינו אופציה. הפרמטרים עצמם בסקריפט.
DENOISE_ENGINES = {
    "rnmix": {"label": "RNNoise עדין"},   # 50% גולמי מיושר — airam-rnnoise --mix
    "speex": {"label": "Speex"},          # תקרת הנחתה 15dB — airam-speexdn
}
DENOISE_ENGINE_DEFAULT = "rnmix"    # כבר מותקן אצל כולם (שלב 4e) — Speex דורש שלב 4f

# מסנן הערוץ (docs/voice-channel-design.md). ⚠ הערוץ *אינו* "בין FFT ברוחב 5kHz":
# rtl_airband מכפיל כל בלוק בחלון Blackman-Harris של 7 איברים ולוקח את עוצמת
# הבין, והחלון הזה רחב בכמה בינים. נמדד (rtl_airband v5.2.0 אמיתי, IQ סינתטי):
# ב-fft 512 התגובה שטוחה עד 4kHz, אבל שכן חזק ב-8.33/12.5kHz *עובר* (‎+3/+1.8dB
# מעל האות). fft גדול יותר = בין צר יותר = פחות רעש ושכנים, ומעט פחות גבוהים.
# ‏"bandwidth" (lowpass I/Q של rtl_airband) לבדו **מסוכן**: עם שכן חזק ב-8.33kHz
# הוא השתיק את הערוץ כולו (הסקוולץ' מודד אז את האות המסונן) — לכן רק יחד עם 1024.
# ⚠ fft_size קטן מ-256 לא מותר ב-rtl_airband, וקצב דגימה שאינו כפולה שלמה של
# 16kHz (למשל 1Msps) משבש את השמע (נמדד) — לכן הקצב לא משתנה כאן.
CHAN_FILTERS = {
    "standard": {"fft": 512, "bandwidth": 0},      # ההתנהגות ההיסטורית
    "narrow": {"fft": 1024, "bandwidth": 0},       # נמדד: רעש ‎-4.6dB, שכן 25kHz ‎-4.4dB, ‎-1.4dB ב-3kHz
    "narrow8k": {"fft": 1024, "bandwidth": 8000},  # נמדד: שכנים ‎-37dB, רעש ‎-6.3dB, ‎-2.6dB ב-3kHz
}
CHAN_FILTER_DEFAULT = "standard"

# רווח SDRplay (מודל legacy של SoapySDRPlay3): שני אלמנטים נפרדים, וקטן יותר = רווח גדול יותר.
#   IFGR - הפחתת רווח בתדר הביניים, 20–59 dB.
#   RFGR - מצב ה-LNA (הפחתת רווח RF), 0–9 (לא-לינארי, ~7dB לצעד).
# כש-AGC כבוי כותבים gain = "IFGR=..,RFGR=.."; כש-AGC דלוק משמיטים את gain => AGC חומרתי.
IFGR_MIN, IFGR_MAX = 20, 59
RFGR_MIN, RFGR_MAX = 0, 9
IF_GAIN_DEFAULT = 40            # IFGR - אמצע הטווח, בטוח מפני עומס יתר
RF_GAIN_DEFAULT = 4            # RFGR - מצב LNA בינוני

SNR_MIN, SNR_MAX = 0.0, 60.0   # dB - תחום clamp ל-SNR ידני
SNR_DEFAULT = 9.0              # ≈ סף ה-auto הפנימי של rtl_airband (~9.54 dB)

# בנקי תדרי ACARS: כל בנק נכנס בחלון דגימה *אחד* של acarsdec (≤ ACARS_WINDOW_MHZ).
# העיקרון: acarsdec מפענח עד 8 ערוצים, וכולם חייבים ליפול בתוך חלון ~2MHz (chooseFc
# בוחר center שמכסה את כולם). צביר 131.x וצביר 136.x רחוקים ~5MHz => *לעולם* לא בחלון
# אחד => בנקים נפרדים להחלפה (כמו מתג קול/ACARS). הצבא ומטוסי התדלוק האמריקאים
# (KC-135/KC-46) אינם משתמשים בתדר ACARS צבאי נפרד — הם פלטפורמות אזרחיות מותאמות
# על רשת ARINC/SITA, ובפועל מופיעים על 131.550 (הראשי העולמי) ועל צביר אירופה.
ACARS_BANKS = [
    {"id": "eu131", "name": "אירופה + עולמי (131)",
     "freqs": ["130.450", "131.425", "131.525", "131.550", "131.725", "131.825", "131.850"]},
    {"id": "band136", "name": "אזור 136",
     "freqs": ["136.700", "136.750", "136.800", "136.850", "136.900", "136.925", "136.975"]},
]
ACARS_FREQS_DEFAULT = ACARS_BANKS[0]["freqs"]   # בנק ברירת המחדל (131.x מורחב, span 1.4MHz)

# בנקי תדרי VDL2: כל התדרים בצביר 136.7–137.0 (span ‏250kHz) => תמיד חלון דגימה אחד.
# 136.975 הוא ה-CSC (Common Signalling Channel) העולמי — כמעט כל התעבורה באזורנו שם;
# 4 הערוצים המשניים (אירופה) מפוענחים בו-זמנית בחינם. בנק CSC-בלבד = fallback ל-CPU.
VDL2_BANKS = [
    {"id": "eu_csc", "name": "עולמי + אירופה (CSC+4)",
     "freqs": ["136.725", "136.775", "136.825", "136.875", "136.975"]},
    {"id": "csc", "name": "CSC בלבד (136.975)", "freqs": ["136.975"]},
]
VDL2_FREQS_DEFAULT = VDL2_BANKS[0]["freqs"]

# דגל --spectrum (options.c/web.c, אומת מהמקור) פותח שני endpoints נוספים ב-
# dashboard: GET /api/spectrum?ch=N&bins=N (מערך mags_db + mixer/AFC) ו-
# /api/constellation. **זה האבחון היחיד שמבחין בין "אין RF בכלל" ל"יש RF, לא
# נעול"**: ebno/lock לבדם מראים "אין נעילה" גם כשהאנטנה מנותקת וגם כשהיא
# מכוונת ב-5° שגיאה. רצפת רעש שמזנקת ~20-30dB כשה-LNA מוזן היא הראיה הישירה
# היחידה שהשרשרת RF חיה בכלל (ר' §12 — משווים מול מדידה של המשתמש, לא מול סף
# מומצא). המחיר: **אפס CPU רציף** — web_get_spectrum_by_channel קורא את מצב
# הדמודולטור הקיים (jaero_pmsk_get_spectrum), אין ring buffer ואין FFT מתמשך;
# העבודה מתרחשת רק כשה-UI מבקש. ⚠ המחיר האמיתי הוא אבטחתי: הדגל מוסיף גם
# GET /api/tune?ch=N&hz=X (משנה-מצב!) לאותו פורט לא-מאומת שנקשר ל-INADDR_ANY
# (ר' SATCOM_WEB_PORT למעלה ו-§9) — לכן זה משתנה-מצב שניתן לכבות, ולא קבוע.
# רווח ידני: ‏inmarsat-sniffer מקבל ‎--sdrplay-gain=N ומטפל בו כך (sdrplay.c,
# אומת מהמקור — הציטוט חשוב כי ההתנהגות **לא** מקבילה לזו של הקול):
#     if (sdrplay_gain_val >= 0) {
#         int grdb = sdrplay_gain_val;
#         if (grdb < 20) grdb = 20;  if (grdb > 59) grdb = 59;
#         chp->tunerParams.gain.gRdB = grdb;
#         chp->tunerParams.gain.LNAstate = 0;          /* ← לא ניתן לשליטה */
#         chp->ctrlParams.agc.enable = sdrplay_api_AGC_DISABLE;
#     } else {
#         chp->ctrlParams.agc.enable  = sdrplay_api_AGC_5HZ;
#         chp->ctrlParams.agc.setPoint_dBfs = -30;
#     }
# שתי מסקנות מעשיות:
# (1) **הטווח 20–59 זהה ל-IFGR של הקול** (IFGR_MIN/IFGR_MAX) — אותה סמנטיקה
#     הפוכה בדיוק: הערך הוא *הפחתה*, קטן=רווח גדול. לכן משתמשים באותם קבועים.
# (2) **אין שליטה ב-RFGR/LNAstate כמו בקול** — במצב ידני הכלי מקבע LNAstate=0,
#     כלומר **רווח RF מקסימלי**. זה לא חיסרון ל-SATCOM אלא בדיוק מה שרוצים
#     לאות לוויין חלש, וזו הסיבה שרווח ידני יכול לעזור דווקא כשה-AGC לא:
#     ה-AGC מכוון ל-setpoint של ‎-30dBfs על *כל* מה שבחלון, כך שאנרגיה חזקה
#     מחוץ לפס (סלולר סמוך ל-L-band — בדיוק מה שה-SAW של ה-LNA נועד לחתוך)
#     יכולה לגרום לו להוריד רווח ולהחניק את הנשא של הלוויין. לכן זו אופציה,
#     לא ברירת מחדל: AGC נשאר ברירת המחדל (None), והידני הוא כלי לשטח.
SATCOM_GAIN_DEFAULT = None            # None = AGC של הדרייבר (‎5Hz, setpoint ‎-30dBfs)

# "בנקים" של satcom = לוויינים (geostationary), לא צבירי-תדרים כמו ACARS/VDL2 —
# כל "בנק" הוא לוויין יחיד (freqs בן-איבר-יחיד עם דגל ה---satellite=). זה מאפשר
# ל-UI לעשות שימוש חוזר במנגנון בורר-הבנקים הקיים כבורר-לוויין, בלי קוד מיוחד.
# דגלי הלוויין ושמותיהם מאומתים מ-`inmarsat-sniffer --list-satellites` (ר'
# docs/satcom-feasibility.md §2). AF1 (Alphasat, +25.0E) ברירת המחדל ל-ישראל.
SATCOM_BANKS = [
    {"id": "AF1", "name": "Alphasat · EMEA (25°E)", "freqs": ["AF1"]},
    {"id": "4F3", "name": "I-4 F3 · אמריקה (98°W)", "freqs": ["4F3"]},
    {"id": "3F5", "name": "I-3 F5 · אטלנטי (54°W)", "freqs": ["3F5"]},
    {"id": "F1", "name": "I-6 F1 · אוק' הודי/שקט (83°E)", "freqs": ["F1"]},
]
SATCOM_SATELLITES = {b["id"] for b in SATCOM_BANKS}  # דגלי --satellite= תקינים
SATCOM_FREQS_DEFAULT = SATCOM_BANKS[0]["freqs"]      # ["AF1"] — Alphasat, ל-EMEA/ישראל

DISCONNECT_DROP_DB = 10.0
