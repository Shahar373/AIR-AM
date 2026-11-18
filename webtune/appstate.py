# ============================================================================
#  AIR-AM — המצב השמור (state.json): טעינה, שמירה ומיזוג אטומי
# ----------------------------------------------------------------------------
#  הוצא מ-app.py (שלב ג2). אין תלות ב-Flask/systemd: קובץ JSON + `storage.atomic_write` + `config` (נתוני ברירת מחדל).
#  המדיניות (CLAUDE.md §5/§12): קובץ חסר ≠ קובץ פגום (עותק `.corrupt`, פעם אחת לאירוע); כל שמירה אחרי פעולה ארוכה
#  עוברת `_merge_state` (טוען טרי וממזג רק את מפתחות הקורא) — לא `save_state({**st, ...})` מ-snapshot ישן.
#  ⚠ הקוראים ניגשים כ-`appstate.<שם>` (לא `from appstate import`). השם `appstate` ולא `state`: `state` משתנה
#  מקומי נפוץ ב-app.py והיה מסתיר את המודול (כמו שקרה עם `recordings`).
# ============================================================================
import json
import logging
import threading
from pathlib import Path

import config
import recordings
import storage

log = logging.getLogger("airam")

STATE_PATH = Path("/var/lib/airam/state.json")


DEFAULT_STATE = {"freq": 132.500, "mod": "am", "agc": True,
                 "if_gain": config.IF_GAIN_DEFAULT, "rf_gain": config.RF_GAIN_DEFAULT,
                 "squelch_mode": "open", "squelch_snr": config.SNR_DEFAULT,  # ברירת מחדל ATIS => תמיד פתוח
                 # "voice" (rtl_airband) | "acars" (acarsdec) | "vdl2" (dumpvdl2) |
                 # "satcom" (inmarsat-sniffer) | "off" (standby).
                 # ברירת המחדל ניטרלית (off): אין "מצב ראשי" — התקנה טרייה נוחתת במסך
                 # הבית והמשתמש בוחר מצב. המצב הנבחר שורד reboot (משוחזר ע"י _boot_restore).
                 "app_mode": "off",
                 "acars_freqs": config.ACARS_FREQS_DEFAULT,
                 "vdl2_freqs": config.VDL2_FREQS_DEFAULT,
                 "satcom_freqs": config.SATCOM_FREQS_DEFAULT,
                 # True = bias-T של ה-RSP1B מזין את ה-LNA (ברירת המחדל ההיסטורית).
                 # False = המשתמש מזין את ה-LNA ממקור חיצוני (ר' _enter_satcom).
                 "satcom_bias_tee": True,
                 # True (ברירת מחדל) = מדלגים על דמודולטורי ה-C-channel — חוסך
                 # ~50% CPU ובקושי עולה במידע (ר' §12 ב-CLAUDE.md ו-write_satcom_env).
                 "satcom_skip_c": True,
                 # True (ברירת מחדל) = --spectrum פעיל => GET /api/satcom/spectrum
                 # עובד. זה כלי האבחון היחיד שמראה אם יש RF בכלל (ר' הערת
                 # SATCOM_SPECTRUM_BINS). דולק כברירת מחדל כי בלי נעילה המצב חסר
                 # ערך ממילא, ועלות ה-CPU היא אפס; ניתן לכיבוי מי שמעדיף לא לחשוף
                 # את GET /api/tune של הכלי ברשת המקומית (§9).
                 "satcom_spectrum": True,
                 # None = AGC (ברירת המחדל); int 20..59 = gRdB ידני (*הפחתה*,
                 # קטן=רווח גדול — כמו if_gain של הקול). ר' SATCOM_GAIN_DEFAULT.
                 "satcom_gain": config.SATCOM_GAIN_DEFAULT,
                 # בסיס כיול למד השדה: {"noise": dBFS, "freq": MHz, "ts": epoch} או None.
                 # נמדד תמיד תחת אותם תנאים קבועים (AGC, /api/antenna/check) => בר-השוואה
                 # לעצמו לאורך זמן, בלי תלות באיזה מצב פעיל עכשיו. לעולם לא ממציאים
                 # אותו — בלי כיול מפורש של המשתמש, אין פסק דין (ר' §12).
                 "signal_baseline": None,
                 # מתי המשתמש ראה לאחרונה את דוח הסשן (epoch) — None עד /api/session/ack
                 # הראשון. /api/session נופל ל"שעה אחורה" כשזה חסר (התקנה טרייה/שדרוג),
                 # לא לכל ההיסטוריה.
                 "last_session_view_at": None,
                 # תמלול *כל* הקלטה אוטומטית ברקע. ⚠ ברירת המחדל **כבויה**:
                 # מי שמתקין whisper (INSTALL_WHISPER=1) לא בהכרח רוצה שהוא ירוץ
                 # על כל הקלטה — תמלול לפי דרישה (📝) ושל הקלטות שמורות (★)
                 # עובדים תמיד בלי המתג הזה. המתג ב-UI הוא מקור-האמת היחיד.
                 "transcribe_auto": False,
                 # שפת התמלול: "en" (ATC בנתב"ג) או "he". דורש מודל רב-לשוני
                 # לעברית — ר' WHISPER_MODELS ו-_whisper_model.
                 "transcribe_lang": recordings.TX_LANG_DEFAULT,
                 # תחנת הקליטה להצלבת ADS-B/אופק הרדיו: {"lat","lon","alt_m","name",
                 # "source","set_at"} או None. ⚠ אין ברירת מחדל (לא נתב"ג): AIR-AM נייד,
                 # ומיקום מומצא היה מייצר אופק שגוי בשקט — בלי תחנה אין פסק-דין BLOS.
                 "station": None,
                 # סינון רעשים חי לסטרים /clean.mp3. מתג גלובלי; כבוי כברירת מחדל.
                 # ר' _denoise_sync. denoise_engine — מתוך DENOISE_ENGINES.
                 "denoise": False,
                 "denoise_engine": config.DENOISE_ENGINE_DEFAULT,
                 # מסנן הערוץ של הקול (CHAN_FILTERS) — ברירת המחדל = ההתנהגות ההיסטורית.
                 "chan_filter": config.CHAN_FILTER_DEFAULT,
                 # מסנן notch לשידורי FM/MW ב-RSP1B (rfnotch_ctrl של SoapySDRPlay3).
                 # ⚠ ההנחתה שלו בתחום 118–137MHz לא נמדדה — מתג, לא ברירת מחדל.
                 "fm_notch": False,
                 # serial של המקלט הראשי (docs/dual-sdr-feasibility.md §3.1). None = כמו תמיד:
                 # "driver=sdrplay" בלי serial ⇒ המקלט הראשון שה-API מחזיר — נכון רק כשיש
                 # מקלט אחד. עם שניים מחוברים בלי בחירה, איזה מהם נפתח נקבע במקרה.
                 "sdr_primary": None,
                 # המקלט המשני (קול + ACARS / קול כפול).
                 "sdr_secondary": None,
                 # הערוץ השני של "קול כפול" (על המשני). squelch/מסנן/notch — כמו הראשי; רווח — תמיד
                 # AGC (מקלט אחר, ואין לו פקדי רווח משלו בממשק).
                 "voice2_freq": 134.600, "voice2_mod": "am"}


_state_corrupt_warned = False   # חד-פעמי לאירוע פגימה, לא לכל קריאה — ר' load_state


def load_state():
    """קורא את המצב השמור, ממוזג על ברירות המחדל (שדות חדשים בשדרוג נכנסים לבד).
    ⚠ מבחין בין *קובץ חסר* (תקין לגמרי — התקנה טרייה) לבין *קובץ פגום*: פגימות
    מתרחשת בעיקר בכיבוי פתאומי (ר' storage.atomic_write), ובלי לוג המשתמש היה מאבד
    תדר/gain/בנקים/satcom_bias_tee/scan_plan בשקט מוחלט ונוחת בברירות מחדל בלי
    להבין למה. שומרים עותק .corrupt לאבחון. ⚠ הלוג+הכתיבה חד-פעמיים לאירוע פגימה
    (flag גלובלי, מתאפס בקריאה תקינה הבאה) ולא לכל קריאה — הפונקציה נקראת גם
    מראוטים בתדירות גבוהה (כולל /api/metrics ב-polling), ובלי ה-flag קובץ פגום
    יחיד היה מייצר ספאם ללוג ודריסה חוזרת של .corrupt בכל בקשה."""
    global _state_corrupt_warned
    try:
        raw = STATE_PATH.read_text()
    except FileNotFoundError:
        return dict(DEFAULT_STATE)         # התקנה טרייה — לא אירוע
    except OSError as e:
        log.warning("קריאת state נכשלה (%s) — ברירות מחדל", e)
        return dict(DEFAULT_STATE)
    try:
        st = json.loads(raw)
    except ValueError as e:
        if not _state_corrupt_warned:
            log.warning("state.json פגום (%s) — נופלים לברירות מחדל; עותק נשמר ב-%s.corrupt",
                        e, STATE_PATH.name)
            try:
                STATE_PATH.with_suffix(STATE_PATH.suffix + ".corrupt").write_text(raw)
            except OSError:
                pass                       # אבחון בלבד — לא שווה להיכשל בגללו
            _state_corrupt_warned = True
        return dict(DEFAULT_STATE)
    if not isinstance(st, dict):           # JSON תקין אך לא אובייקט (למשל "null")
        if not _state_corrupt_warned:
            log.warning("state.json אינו אובייקט (%s) — ברירות מחדל", type(st).__name__)
            _state_corrupt_warned = True
        return dict(DEFAULT_STATE)
    _state_corrupt_warned = False          # התאוששנו — אירוע פגימה עתידי יתועד שוב
    return {**DEFAULT_STATE, **st}


def _reset_state_corrupt_warned():
    """מאפס את ה-flag. נחוץ לבדיקות (מצב גלובלי דולף בין בדיקות שמשאירות
    state.json פגום — בלי איפוס, בדיקה הבאה שמצפה ללוג הייתה מדוכאת בשקט)."""
    global _state_corrupt_warned
    _state_corrupt_warned = False


def save_state(st):
    storage.atomic_write(STATE_PATH, json.dumps(st))


_STATE_LOCK = threading.Lock()


def _merge_state(changes):
    """read-modify-write של state.json: טוען טרי, ממזג רק את המפתחות של הקורא
    ושומר. מחזיר את ה-state החדש.
    ⚠ מעבר מצב/כיוונון מחזיק snapshot לאורך restart של 3–45ש'; שמירת
    {**st, ...} מאותו snapshot דרסה שינוי מקביל (למשל "תמלל הכול" או בחירת
    שפה באמצע מעבר) — lost update שקט. כל שמירה אחרי פעולה ארוכה עוברת כאן."""
    with _STATE_LOCK:
        st = {**load_state(), **changes}
        save_state(st)
        return st
