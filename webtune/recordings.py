# ============================================================================
#  AIR-AM — הקלטות, ★ שמורות, תמלול (whisper) ושחזור-סשן (הצד של הקבצים)
# ----------------------------------------------------------------------------
#  הוצא מ-app.py (שלב ג2). כל מה שכאן עובד על *קבצים בדיסק* — אין תלות ב-Flask, ב-state
#  או ב-systemd (נבדק מכנית: הסגור הטרנזיטיבי של הפונקציות כאן לא נוגע בהם). נתיבי ה-HTTP
#  נשארו ב-app.py (`/api/recordings/*`, `/api/sessions*`, `/api/activity`, `/api/transcribe`),
#  וכך גם כל קריאה ל-`load_state` — `transcribe_worker` מקבל אותם דרך `get_cfg()`.
#  המדיניות (CLAUDE.md §5/§12 + docs/lessons-learned.md): ★ = *מיקום הקובץ* (`saved/`), לא
#  רשומה; תמלול = sidecar עם `state` מפורש ושורד restart; `_session_clip_paths` נפרד
#  מ-`_iter_recordings` בכוונה.
#  ⚠ הקוראים ניגשים כ-`recordings.<שם>` (לא `from recordings import`) — כך החלפה בבדיקה
#  (`monkeypatch.setattr(recordings, "REC_DIR", ...)`) משפיעה גם על הקוד שנקרא מ-app.py.
# ============================================================================
import json
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path

import storage

log = logging.getLogger("airam")

# הקלטות: rtl_airband כותב קובץ MP3 לכל שידור (split_on_transmission) בשם
# <REC_BASENAME>_YYYYMMDD_HHMMSS_<Hz>.mp3 (.tmp בזמן כתיבה, rename בסגירה
# ~0.5ש' אחרי שהסקוולץ' נסגר). קובץ שהסתיים = אירוע ביומן השידורים.
REC_DIR = Path("/var/lib/airam/recordings")
REC_BASENAME = "airam"         # filename_template ב-config וגם עוגן הפרסור של השמות
# הערוץ השני של קול כפול כותב airam2_… — אותה תיקייה, אותו יומן, ★/תמלול/סשנים בלי שינוי,
# אבל היומן יודע מאיזה מקלט השידור (`ch`). גם שני תדרים זהים לא היו מתנגשים בשם.
REC_BASENAME2 = REC_BASENAME + "2"
REC_BYTES_PER_SEC = 6000       # CBR 48kbps (ה-patch ב-install.sh) => הערכת משך מגודל
# retention (הקלטות *לא* מסומנות בלבד — ר' _sweep_recordings). ⚠ 500 ולא 200:
# 200 שידורים ≈ 17 דק' אודיו בלבד בתדר עמוס — פחות מהחלון הרטרואקטיבי שכפתור
# "שמור סשן" (docs/session-replay-design.md) מבטיח. 500 מוסיף ~15MB בלבד
# (ר' §5.5 שם) ונותן ~5 שעות שעון — תנאי מקדים לפיצ'ר, לא קשור לתוכנו.
REC_MAX_FILES = 500
REC_MAX_BYTES = 100 * 1024 * 1024
# הקלטות שמורות (★) — **תת-תיקייה, לא רשימה בקובץ צד.**
# ⚠ זו הייתה טעות עיצוב שתוקנה: הגרסה הראשונה ניהלה `starred.json` עם רשימת
# פטורים, ו-`_sweep_recordings` קרא אותה כדי לדעת מה לא למחוק. כל מנגנון-הצללה
# כזה יוצר משפחה שלמה של כשלים: קובץ פגום ⇒ fail-open ⇒ **בדיוק ההקלטות
# המוגנות נמחקות** (הוכח); מרוץ בין הסימון לסריקה ⇒ השרת מאשר "נשמר" והקובץ
# נמחק (הוכח); read-modify-write בלי נעילה ⇒ איבוד עדכונים ועקיפת מכסה (הוכח).
# ‏`REC_DIR.glob("*.mp3")` **אינו רקורסיבי**, ולכן העברה ל-`saved/` נותנת את
# הפטור מ-retention ב*אפס* שורות לוגיקה, ו-`os.replace` הוא אטומי — אין מה
# לסנכרן ואין מה לאבד. כל שינוי כאן: אל תחזיר מאגר-מצב מקביל לקבצים עצמם.
SAVED_DIRNAME = "saved"
# תקרה על השמורות — לא כדי "לנהל" אותן אלא כדי שסימון לא ימלא כרטיס SD בשקט.
# ⚠ כשהתקרה מגיעה *מסרבים לסמן* ולא מוחקים ותיקה: מחיקת קובץ שהמשתמש הגן
# עליו במפורש היא בדיוק מה שהפיצ'ר נועד למנוע.
REC_STAR_MAX_FILES = 100
REC_STAR_MAX_BYTES = 100 * 1024 * 1024

WATCH_INTERVAL = 10.0          # שניות בין סריקות של תיקיית ההקלטות

# --- שחזור-סשן (שלב 2, docs/session-replay-design.md) -----------------------
# תיקייה נפרדת מ-recordings/saved/ בכוונה: סשן הוא יחידה מורכבת (קליפים +
# מסלולי ADS-B + מטא-דאטה), לא הקלטה בודדת. `_sweep_recordings` לא רואה אותה
# מאותה סיבה בדיוק כמו saved/ — glob לא רקורסיבי.
SESSIONS_DIR = Path("/var/lib/airam/sessions")
SESSION_CLIPS_DIRNAME = "clips"
# מזהה = תאריך-שעה קריא לאדם (YYYYMMDD-HHMM), עם סיומת מספרית בהתנגשות —
# ולכן זו גם ההגנה מפני path traversal בראוטים עם <id>: אין בו נקודות/לוכסנים.
SESSION_ID_RE = re.compile(r"^\d{8}-\d{4}(-\d+)?$")
# ⚠ אין קבוע נפרד ל"כמה דקות אפשר לבקש" — נשען על adsb.TRACK_BUFFER_MIN
# ישירות (בזמן הבקשה), לא על עותק מקומי שיכול להתפצל ממנו בשינוי עתידי.

# --- תמלול ATC (whisper.cpp מקומי) -------------------------------------------
# מודל ההפעלה **היברידי** (ר' §5/§12 ב-CLAUDE.md): לפי דרישה (כפתור ליד כל
# שידור) + אוטומטי לכל הקלטה *מסומנת בכוכב* + מתג אופציונלי "תמלל הכול".
# ⚠ הזמינות נבדקת **חיה בכל מחזור** ולא פעם אחת בעלייה: קודם ה-thread עשה
# return כשהבינארי חסר, ולכן התקנת whisper אחרי העלייה לא הורגשה עד restart.
# ⚠ `sudo INSTALL_WHISPER=1` ולא `INSTALL_WHISPER=1 sudo`: ל-sudo ב-Debian יש
# `env_reset` כברירת מחדל ו-INSTALL_WHISPER אינו ב-env_keep => הצורה ההפוכה
# בולעת את המשתנה והסקריפט מדלג על ההתקנה **בשקט**. מחרוזת אחת, כאן, כי היא
# מוצגת ב-UI כהוראת-פעולה והעתקה עיוורת ממנה חייבת לעבוד.
INSTALL_WHISPER_HINT = "sudo INSTALL_WHISPER=1 ./install.sh"
WHISPER_BIN = os.environ.get("AIRAM_WHISPER_BIN", "/usr/local/bin/whisper-cli")
# שני מודלים, לא אחד — כי שפת התמלול ניתנת לבחירה מה-UI:
#   en => ggml-small.en (אנגלית-בלבד; מדויק יותר באנגלית ממודל רב-לשוני באותו גודל)
#   he => ggml-small     (רב-לשוני; היחיד שמסוגל לעברית בכלל)
# ‏ATC בנתב"ג הוא אנגלית, ולכן אנגלית נשארת ברירת המחדל ומקבלת את המודל הטוב לה.
# ⚠ מודל `.en` **אינו יכול** לתמלל עברית — לא "פחות טוב", אלא לא נתמך מהבנייה.
# לכן `_whisper_model(lang)` מחזיר None לעברית כשרק מודל אנגלי מותקן, והממשק
# אומר זאת במפורש במקום להפיק ג'יבריש (§12: לא ממציאים, וגם לא מסתירים).
WHISPER_MODEL_DIR = Path(os.environ.get("AIRAM_WHISPER_MODEL_DIR", "/opt/airam/models"))
# לכל שפה: רשימת מועמדים לפי סדר העדפה. ההתקנות הוותיקות הורידו base.en בלבד,
# ולכן הוא נשאר בסוף שרשרת האנגלית — שדרוג בלי הורדה מחדש עדיין עובד.
WHISPER_MODELS = {
    "en": ("ggml-small.en.bin", "ggml-small.bin", "ggml-base.en.bin", "ggml-base.bin"),
    "he": ("ggml-small.bin", "ggml-base.bin"),
}
TX_LANGS = ("en", "he")
TX_LANG_DEFAULT = os.environ.get("AIRAM_WHISPER_LANG", "en")
if TX_LANG_DEFAULT not in TX_LANGS:
    TX_LANG_DEFAULT = "en"
# ⚠ פחות מכל הליבות **וגם** nice: ל-Pi 5 יש 4 ליבות, ו-3 מהן ב-100% אינן
# "נסיגה" — הן 75% מהמעבד. הגבלת ה-threads לבדה לא נותנת לרדיו עדיפות; מה
# שנותן אותה הוא התזמון. `nice -n 19` מבטיח ש-rtl_airband/acarsdec יקבלו מעבד
# ברגע שהם צריכים אותו, ולכן התמלול באמת נסוג ולא רק "תופס פחות".
WHISPER_THREADS = os.environ.get("AIRAM_WHISPER_THREADS", "3")
WHISPER_NICE = "19"
TRANSCRIBE_TIMEOUT = 300.0     # שניות לקובץ בודד (המרה + תמלול). small איטי מ-base פי ~3
TX_MIN_SEC = 0.7               # קטע קצר מזה = לחיצת סקוולץ', לא דיבור => לא מתמללים
TX_MAX_FAILS = 3               # ניסיונות לפני דילוג (מגן מלולאת whisper אינסופית — ר' _transcribe_worker)
# רמז הקשר => מטה את המודל לפרזיולוגיית ATC ושמות מקומיים (משפר דיוק משמעותית).
# ⚠ רק לאנגלית: רמז אנגלי על תמלול עברי מטה את המודל לשפה הלא-נכונה.
WHISPER_PROMPT = ("Air traffic control radio between pilots and Ben Gurion / Tel Aviv "
                  "tower, ground, approach. Phrases: cleared for takeoff, line up and wait, "
                  "taxi to runway, hold short, contact tower, squawk, climb, descend, "
                  "heading, knots, QNH, wind, runway 03 12 21 26 30.")
# ⚠ **אין רשימת "ביטויי הזיה".** הייתה כזו, והיא הוסרה אחרי שהוכח שהיא אוכלת
# תשדורות ATC לגיטימיות: הנרמול מחק ספרות, ולכן `"Thank you, 385"` (מסירת תדר
# שגרתית במגדל), `"Okay, 03"` ו-`"Ok."` סוננו — והוצגו למשתמש כ"סונן כהזיה",
# טענה שאין לנו שום בסיס לה. זו בדיוק ההמצאה ש-§12 אוסר, רק בכיוון ההפוך:
# לא ערך מומצא אלא *פסילה* מומצאת. ‏ATC מורכב כמעט כולו מביטויים קצרים
# וסטנדרטיים, ולכן כל blocklist מילולי כאן פוגע בתוכן אמיתי. מה שנשאר הוא
# ‏TX_MIN_SEC — סינון לפי *גודל הקובץ*, שהוא עובדה מדידה ולא ניחוש-תוכן.
# פלט של whisper מוצג כפי שהוא; המשתמש שומע את ההקלטה וקובע בעצמו.


_REC_NAME_RE = re.compile(rf"^{re.escape(REC_BASENAME)}(?:2)?_\d{{8}}_\d{{6}}_(\d+)\.mp3$")


def _rec_freq_mhz(name):
    """airam_20260611_203455_134600000.mp3 => 134.600 (MHz). אחר => None."""
    m = _REC_NAME_RE.match(name)
    return round(int(m.group(1)) / 1e6, 3) if m else None


# --- הקלטות שמורות (★) ------------------------------------------------------
# ר' הערת SAVED_DIRNAME: הפטור מ-retention הוא *מיקום הקובץ*, לא רשומה במאגר.
_STAR_LOCK = threading.Lock()   # מסדר סימונים מקבילים (Flask threaded=True)


def _saved_dir():
    return REC_DIR / SAVED_DIRNAME


def _rec_path(name):
    """הנתיב לקובץ הקלטה — בתיקייה החיה או בשמורות. None אם איננו."""
    for p in (REC_DIR / name, _saved_dir() / name):
        if p.is_file():
            return p
    return None


def _is_saved(name):
    return (_saved_dir() / name).is_file()


def _rec_event(p):
    """רשומת אירוע יומן הנגזרת **מהקובץ עצמו**. מקור-אמת יחיד: משמש גם את
    _scan_new_recordings (יומן חי) וגם את ?starred=1 (שמורות) => אין שתי
    גרסאות של אותה שורה שיכולות להיפרד."""
    st = p.stat()
    return {"ts": round(st.st_mtime, 1), "freq": _rec_freq_mhz(p.name),
            "file": p.name, "dur": round(st.st_size / REC_BYTES_PER_SEC, 1),
            "ch": 2 if p.name.startswith(REC_BASENAME2 + "_") else 1}


def _saved_usage():
    """(count, bytes) של תיקיית השמורות — לאכיפת התקרה."""
    count = total = 0
    try:
        entries = list(_saved_dir().glob("*.mp3"))
    except OSError:
        return 0, 0
    for p in entries:
        try:
            total += p.stat().st_size
            count += 1
        except OSError:
            pass
    return count, total


# --- תמלול ATC --------------------------------------------------------------
# ⚠ ה-sidecar הוא JSON ולא טקסט-חשוף, כי טקסט לבד לא יכול לבטא את ההבדל בין
# "לא ניסינו", "ניסינו ולא יצא כלום" ו-"ניסינו ונכשלנו" — בדיוק ההבחנה ש-§12
# מחייב, ושבגללה הפיצ'ר הישן נראה למשתמש כאילו הוא פשוט לא קיים (שורה בלי
# טקסט, זהה לחלוטין בארבעת המצבים). קובץ .txt ישן עדיין נקרא (תאימות לאחור).
def _transcript_path(mp3):
    """ה-sidecar הישן (טקסט בלבד): airam_....mp3 => airam_....mp3.txt.
    נשמר לקריאה בלבד — התקנות שכבר תמללו לא מאבדות את מה שיש להן."""
    return mp3.parent / (mp3.name + ".txt")


def _tx_path(mp3):
    """ה-sidecar הנוכחי: airam_....mp3 => airam_....mp3.tx.json."""
    return mp3.parent / (mp3.name + ".tx.json")


def _tx_sidecars(mp3):
    """שני ה-sidecars של הקלטה (חדש+ישן) — למחיקה משותפת ב-retention."""
    return (_tx_path(mp3), _transcript_path(mp3))


def _read_tx(mp3):
    """מצב התמלול של הקלטה, כמילון מוכן ל-API:
      {"state": "ok"|"empty"|"failed"|"pending"|"none", "text": str|None, ...}
    ‏'none' = מעולם לא ניסינו (אין sidecar) — **לא** אותו דבר כמו 'empty'.
    ⚠ כל קריאת קובץ כאן חייבת לתפוס גם ValueError: קובץ .txt ישן עם בייט
    UTF-8 פגום זרק UnicodeDecodeError (תת-מחלקה של ValueError) שטיפס עד
    ‏/api/activity והפיל אותו ב-500 **כל 15 שניות** (הוכח). errors='replace'
    מבטיח שגם תוכן פגום יוצג ולא יפיל את היומן כולו."""
    try:
        d = json.loads(_tx_path(mp3).read_text(errors="replace"))
        if isinstance(d, dict) and d.get("state"):
            return d
    except (OSError, ValueError):
        pass
    try:   # תאימות לאחור: sidecar טקסט ישן. ריק שם = "נוסה ולא יצא" (empty).
        old = _transcript_path(mp3).read_text(errors="replace").strip()
        return {"state": "ok" if old else "empty", "text": old or None}
    except (OSError, ValueError):
        return {"state": "none", "text": None}


def _write_tx(mp3, state, text=None, err=None, lang=None):
    rec = {"state": state, "text": text}
    if not mp3.parent.is_dir():
        # ‏storage.atomic_write יוצר תיקיות-אב: סשן שנמחק בזמן שה-worker מתמלל קליפ
        # שלו היה "קם לתחייה" כתיקייה עם sidecar בודד ובלי meta.json.
        return rec
    if err:
        rec["err"] = err
    if lang:
        rec["lang"] = lang
    storage.atomic_write(_tx_path(mp3), json.dumps(rec, ensure_ascii=False))
    return rec


def _whisper_model(lang="en"):
    """נתיב המודל לשפה המבוקשת, או None אם אין מתאים.
    ⚠ עברית דורשת מודל **רב-לשוני**: `ggml-small.en` לא "פחות טוב" בעברית —
    הוא לא תומך בה מהבנייה. מחזירים None ומדווחים, במקום להריץ ולקבל ג'יבריש."""
    for name in WHISPER_MODELS.get(lang, ()):
        p = WHISPER_MODEL_DIR / name
        if p.exists():
            return str(p)
    return None


def _whisper_ready(lang="en"):
    """(bin_ok, model_path) — נבדק **חי** בכל שימוש, לא פעם אחת בעלייה."""
    return Path(WHISPER_BIN).exists(), _whisper_model(lang)


# מה מתמלל *ברגע זה* — כדי ש-ה-UI יבחין בין "רץ עכשיו" ל"ממתין בתור".
# ⚠ זה כל מה שנשאר בזיכרון. התור עצמו הוא **sidecar עם state="pending"** על
# הדיסק, ולא רשימה בזיכרון כמו בגרסה הקודמת: restart של airam-web איבד שם
# בקשות ממתינות, והשורה חזרה להיראות "לא ניסינו" — בדיוק הכשל ש-§12 מדבר
# עליו, בקוד שנכתב כדי לתקן אותו.
_TX_BUSY = {"file": None}
_TX_LOCK = threading.Lock()
_TX_FAILS = {}                  # שם קובץ -> כשלונות רצופים בכתיבת ה-sidecar


def _tx_busy_file():
    with _TX_LOCK:
        return _TX_BUSY["file"]


def _tx_queue_len():
    """כמה הקלטות מסומנות pending על הדיסק (כולל זו שרצה כרגע)."""
    n = 0
    for d in (REC_DIR, _saved_dir()):
        try:
            entries = list(d.glob("*.mp3.tx.json"))
        except OSError:
            continue
        for p in entries:
            try:
                if json.loads(p.read_text(errors="replace")).get("state") == "pending":
                    n += 1
            except (OSError, ValueError):
                pass
    return n


def _transcribe_file(mp3, lang="en"):
    """ממיר MP3 ל-WAV 16kHz מונו (ffmpeg) ומריץ whisper.cpp.
    מחזיר (state, text, err) — ולא רק טקסט/None: המידע *למה* אין טקסט הוא
    בדיוק מה שהמשתמש היה צריך ולא קיבל (§12)."""
    model = _whisper_model(lang)
    if not model or not Path(WHISPER_BIN).exists():
        return "failed", None, ("אין מודל תמלול לעברית — נדרש מודל רב-לשוני"
                                if lang == "he" else "whisper לא מותקן")
    wav = mp3.parent / (mp3.name + ".wav.tmp")
    # nice: התמלול חייב להיסוג מפני הרדיו. ר' הערת WHISPER_NICE.
    cmd = ["nice", "-n", WHISPER_NICE, WHISPER_BIN, "-m", model, "-f", str(wav),
           "-l", lang, "-nt", "-t", str(WHISPER_THREADS)]
    if lang == "en":               # רמז ATC אנגלי — לא מטים בו תמלול עברי
        cmd += ["--prompt", WHISPER_PROMPT]
    try:
        # ⚠ `-f wav` **חובה**: ffmpeg בוחר מכל (muxer) לפי *סיומת שם הקובץ*, לא
        # לפי תוכן. שם הקובץ הזמני מסתיים ב-`.tmp` (`<name>.mp3.wav.tmp` —
        # ר' `wav` למעלה), כך שבלי הדגל ffmpeg נכשל תמיד עם "Unable to choose
        # an output format" — על *כל* הקלטה, בלי קשר לתוכן שלה (נצפה בשטח:
        # ‏rc=234 מ-ffmpeg, לא מ-whisper-cli כפי שההודעה "Error opening..."
        # הטעתה להניח בהתחלה). `-f wav` עוקף לגמרי את הניחוש-לפי-סיומת.
        subprocess.run(["nice", "-n", WHISPER_NICE, "ffmpeg", "-nostdin", "-y",
                        "-i", str(mp3), "-ar", "16000", "-ac", "1", "-f", "wav",
                        str(wav)],
                       capture_output=True, timeout=TRANSCRIBE_TIMEOUT, check=True)
        out = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=TRANSCRIBE_TIMEOUT, check=True)
    except subprocess.TimeoutExpired:
        log.warning("transcribe %s — timeout אחרי %.0f שניות", mp3.name, TRANSCRIBE_TIMEOUT)
        return "failed", None, f"חריגת זמן ({TRANSCRIBE_TIMEOUT:.0f}ש')"
    except FileNotFoundError as e:
        return "failed", None, f"כלי חסר: {e.filename or e}"
    except subprocess.CalledProcessError as e:
        err = (e.stderr or b"")
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
        log.warning("transcribe %s נכשל (rc=%s)", mp3.name, e.returncode)
        return "failed", None, " ".join(err.split())[-200:] or f"rc={e.returncode}"
    except Exception as e:
        log.exception("transcribe %s", mp3.name)
        return "failed", None, str(e)[:200]
    finally:
        try:
            wav.unlink()
        except OSError:
            pass
    # הפלט מוצג כפי שהוא — אין סינון-תוכן (ר' הערת WHISPER_PROMPT למעלה).
    text = " ".join(out.stdout.split()).strip()
    return ("ok", text, None) if text else ("empty", None, None)


def _iter_recordings():
    """כל ההקלטות (חיות + שמורות), חדש=>ישן. ⚠ ה-key עמיד לכשל stat על פריט
    בודד: הגרסה הקודמת עטפה את כל ה-sorted ב-try/except והחזירה — כך ש-symlink
    שבור אחד היה מבטל את התמלול (ואת ה-retention) **לגמרי ובשקט**."""
    out = []
    for d in (REC_DIR, _saved_dir()):
        try:
            out += list(d.glob("*.mp3"))
        except OSError:
            continue

    def mtime(p):
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0
    return sorted(out, key=mtime, reverse=True)


def _tx_untouched(p):
    """אין sidecar כלל (לא ניסינו) — לא כולל 'pending' שכבר ממתין."""
    return not _tx_path(p).exists() and not _transcript_path(p).exists()


def _session_clip_paths():
    """קליפי האודיו של *כל* הסשנים השמורים (`sessions/<id>/clips/*.mp3`).
    ⚠ **בכוונה לא חלק מ-`_iter_recordings()`** — זו לא החמצה: `_iter_recordings`
    משמש גם את `api_sessions` כדי לבחור אילו הקלטות להעביר לסשן *חדש*, ולכן
    הוספת קליפי-סשן לשם הייתה גורמת לשמירת סשן חדש **לגנוב קליפים מסשנים
    קיימים**. הפרדה מפורשת במקום שיתוף מפתה.
    הסיבה שהפונקציה קיימת בכלל: קליפ ש*הועבר* לסשן יצא מ-`_iter_recordings`,
    ולכן `_tx_next_target` לא היה מוצא אותו לעולם — כלומר שמירת סשן הוציאה
    את ההקלטות שלו מתור התמלול לצמיתות, ו"התמלול תחת הנגן" היה ריק תמיד."""
    out = []
    try:
        dirs = sorted(SESSIONS_DIR.iterdir())
    except OSError:
        return out
    for d in dirs:
        if not SESSION_ID_RE.match(d.name):
            continue
        try:
            out += list((d / SESSION_CLIPS_DIRNAME).glob("*.mp3"))
        except OSError:
            continue
    return out


def _tx_next_target(auto):
    """ההקלטה הבאה לתמלול, לפי סדר העדיפויות:
      1. `state="pending"` (המשתמש לחץ 📝 ומחכה — שורד restart)
      2. הקלטות שמורות (★) **וקליפים בסשן שמור** שלא נוגעו
      3. הכול (רק אם auto)
    הקלטה עם sidecar קיים (כולל 'נכשל') לא נבחרת שוב לבד — ניסיון חוזר הוא
    תמיד פעולה מפורשת של המשתמש, שנרשמת כ-pending.
    ⚠ קליפי-סשן באותה עדיפות כמו ★ **מאותו נימוק בדיוק**: שניהם תוכן שהמשתמש
    הגן עליו במפורש (לחץ ★ / לחץ "שמור סשן"), בניגוד להקלטה חולפת ביומן."""
    recs = _iter_recordings()
    sess = _session_clip_paths()
    for p in recs + sess:
        if _TX_FAILS.get(p.name, 0) >= TX_MAX_FAILS:
            continue              # poison: הכתיבה נכשלת שוב ושוב (דיסק מלא)
        if _read_tx(p).get("state") == "pending":
            return p
    for p in recs:                # שמורות: מתומללות אוטומטית תמיד
        if (_TX_FAILS.get(p.name, 0) < TX_MAX_FAILS
                and _is_saved(p.name) and _tx_untouched(p)):
            return p
    for p in sess:                # קליפי סשן: אותה עדיפות, ר' ה-docstring
        if _TX_FAILS.get(p.name, 0) < TX_MAX_FAILS and _tx_untouched(p):
            return p
    if not auto:
        return None
    for p in recs:
        if _TX_FAILS.get(p.name, 0) < TX_MAX_FAILS and _tx_untouched(p):
            return p
    return None


def _sweep_recordings():
    """retention: עד REC_MAX_FILES / REC_MAX_BYTES (חדש=>ישן), ו-.tmp נטושים
    (שידור שנקטע בקריסה משאיר .tmp שלעולם לא ייסגר ל-mp3). קובצי-הצד של התמלול
    נמחקים יחד עם ההקלטה שלהם.
    ⚠ הקלטות שמורות (★) לא מטופלות כאן **בכלל** — הן יושבות ב-`saved/`,
    ו-`glob("*.mp3")` אינו רקורסיבי. זה כל מנגנון הפטור: אין רשימה לקרוא,
    אין מה לסנכרן, ואין מצב שבו קובץ פגום גורם למחיקת מה שהמשתמש שמר."""
    def mtime(p):
        # ⚠ עמיד לכשל על פריט בודד: כשה-key היה `p.stat().st_mtime` והכול
        # עטוף ב-try/except חיצוני, symlink שבור/EACCES על קובץ אחד ביטל את
        # ה-retention **כולו** — הכרטיס היה מתמלא בשקט (הוכח).
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0
    try:
        recs = sorted(REC_DIR.glob("*.mp3"), key=mtime, reverse=True)
    except OSError:
        return
    total = kept = 0
    for p in recs:
        try:
            total += p.stat().st_size
            kept += 1
            if kept > REC_MAX_FILES or total > REC_MAX_BYTES:
                p.unlink()
                for s in _tx_sidecars(p):
                    s.unlink(missing_ok=True)
        except OSError:
            pass
    now = time.time()
    for p in REC_DIR.glob("*.tmp"):
        try:
            if now - p.stat().st_mtime > 3600:
                p.unlink()
        except OSError:
            pass
    # sidecar יתום (בלי mp3 תואם) — יכול להיווצר כשה-thread המתמלל (ריצה עצמאית,
    # ר' _transcribe_worker) מסיים לתמלל mp3 שנגזם ע"י הרצה מקבילה/קודמת של
    # הפונקציה הזו בדיוק לפני שהתמלול הספיק לכתוב; הלולאה למעלה מוחקת sidecar רק
    # יחד עם ה-.mp3 שעדיין ברשימה, ולא רואה קובץ שכבר נעדר ממנה.
    for d in (REC_DIR, _saved_dir()):
        for pat, strip in (("*.txt", ".txt"), ("*.tx.json", ".tx.json")):
            try:
                orphans = list(d.glob(pat))
            except OSError:
                continue
            for p in orphans:
                try:
                    if not (p.parent / p.name[:-len(strip)]).exists():
                        p.unlink()
                except OSError:
                    pass


def _scan_new_recordings(last_seen):
    """(rows, newest) - הקלטות שה-mtime שלהן מאוחר מ-last_seen, חדש=>ישן לפי mtime.
    ה-ts מעוגל *לפני* ההשוואה - אותו עיגול שנכתב ליומן (ושחוזר מ-_last_logged_ts)
    => סריקה חוזרת אחרי restart לא תייצר שורות כפולות."""
    rows, newest = [], last_seen
    def mtime(p):
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0            # עמיד לכשל על פריט בודד — ר' _sweep_recordings
    try:
        recs = sorted(REC_DIR.glob("*.mp3"), key=mtime)
    except OSError:
        recs = []
    for p in recs:
        try:
            ev = _rec_event(p)    # מקור-אמת יחיד לשורה (משותף עם ?starred=1)
        except OSError:
            continue   # נמחק בינתיים (retention) => מדלגים
        if ev["ts"] > last_seen:
            rows.append(ev)
            newest = max(newest, ev["ts"])
    return rows, newest


def _decorate_event(ev):
    """מוסיף לאירוע יומן את שדות ההקלטה: קיום, שמירה (★), ומצב התמלול.
    ⚠ `tx.state` הוא הלב של תיקון התמלול: 'none' (לא ניסינו) / 'pending'
    (ממתין או מתמלל כרגע) / 'ok' / 'empty' (נוסה, אין דיבור) / 'failed'
    (נוסה ונכשל) הם חמישה מצבים שנראו למשתמש זהים לחלוטין קודם — שורה בלי
    טקסט. `text` נשמר לתאימות לאחור עם קליינטים ישנים."""
    name = str(ev.get("file") or "")
    mp3 = _rec_path(name) if name else None
    ev["exists"] = mp3 is not None
    ev["starred"] = bool(name) and _is_saved(name)
    tx = dict(_read_tx(mp3)) if mp3 else {"state": "none", "text": None}
    if tx.get("state") == "pending":
        tx["running"] = (name == _tx_busy_file())   # "מתמלל" מול "ממתין בתור"
    ev["tx"] = tx
    ev["text"] = tx.get("text")
    return ev


def _move_recording(src, dst_dir):
    """מעביר הקלטה + קובצי-הצד שלה. ⚠ `os.replace` אטומי בתוך אותו filesystem
    => אין רגע שבו הקובץ לא קיים באף צד, ואין מה לסנכרן עם רשומת-מצב."""
    dst_dir.mkdir(parents=True, exist_ok=True)
    for s in _tx_sidecars(src):
        if s.exists():
            try:
                os.replace(s, dst_dir / s.name)
            except OSError:
                pass          # ה-sidecar אינו קריטי; ה-mp3 הוא מה שחשוב
    os.replace(src, dst_dir / src.name)


def _new_session_id():
    """מזהה קריא-לאדם (YYYYMMDD-HHMM), עם סיומת מספרית בהתנגשות (שני שמירות
    באותה דקה) — ראו SESSION_ID_RE להסבר למה זה גם ההגנה מפני path traversal."""
    base = time.strftime("%Y%m%d-%H%M")
    if not (SESSIONS_DIR / base).exists():
        return base
    n = 2
    while (SESSIONS_DIR / f"{base}-{n}").exists():
        n += 1
    return f"{base}-{n}"


def _session_dir(session_id):
    """תיקיית הסשן, או None אם המזהה לא תקין (path traversal) או שהסשן לא קיים."""
    if not SESSION_ID_RE.match(session_id or ""):
        return None
    d = SESSIONS_DIR / session_id
    return d if d.is_dir() else None

def transcribe_worker(get_cfg):
    """לולאת רקע יחידה (whisper לוקח את ה-CPU => לא מקבילים אותו).
    ⚠ ה-thread **לא מת** כשwhisper חסר: הוא ממשיך לישון ולבדוק זמינות, כך
    שהתקנת whisper בזמן ריצה נתפסת בלי restart לשירות.
    `get_cfg()` ⇒ `(lang, auto)` — נקרא בכל מחזור (המודול לא מכיר את ה-state;
    ה-wrapper ב-app.py מזריק אותו)."""
    warned = False
    while True:
        try:
            lang, auto = get_cfg()
            bin_ok, model = _whisper_ready(lang)
            if not (bin_ok and model):
                if not warned:
                    log.info("תמלול: אין כלי/מודל זמין (%s, שפה=%s) — ממתין; "
                             "להתקנה: %s", WHISPER_BIN, lang, INSTALL_WHISPER_HINT)
                    warned = True
                time.sleep(WATCH_INTERVAL)
                continue
            if warned:
                log.info("תמלול: whisper זוהה (model=%s)", Path(model).name)
                warned = False
            mp3 = _tx_next_target(bool(auto))
            if mp3 is None:
                time.sleep(WATCH_INTERVAL)
                continue
            with _TX_LOCK:
                _TX_BUSY["file"] = mp3.name
            try:
                try:
                    size = mp3.stat().st_size
                except OSError:
                    continue          # נמחק בינתיים (retention)
                # קטע קצר מדי = פתיחת סקוולץ' בלי דיבור. מדווחים 'empty' במפורש
                # (ולא "לא ניסינו") כדי שלא ננסה אותו שוב בכל מחזור.
                if size < TX_MIN_SEC * REC_BYTES_PER_SEC:
                    _write_tx(mp3, "empty", err="קצר מדי לתמלול")
                else:
                    state, text, err = _transcribe_file(mp3, lang)
                    _write_tx(mp3, state, text=text, err=err, lang=lang)
                _TX_FAILS.pop(mp3.name, None)
            except OSError as e:
                # ⚠ כתיבת ה-sidecar נכשלה (ENOSPC — כרטיס SD מלא). בלי המונה
                # הזה, _tx_next_target היה בוחר *את אותו קובץ* בכל מחזור
                # ומריץ עליו את whisper לנצח, בדיוק כשהמערכת כבר במצוקה (הוכח).
                _TX_FAILS[mp3.name] = _TX_FAILS.get(mp3.name, 0) + 1
                log.warning("תמלול: כתיבת התמלול של %s נכשלה (%s) — ניסיון %d/%d",
                            mp3.name, e, _TX_FAILS[mp3.name], TX_MAX_FAILS)
                time.sleep(WATCH_INTERVAL)
            finally:
                with _TX_LOCK:
                    _TX_BUSY["file"] = None
            continue                  # יש עוד עבודה? נטפל בה מיד, בלי sleep
        except Exception:
            log.exception("transcribe worker")
        time.sleep(WATCH_INTERVAL)
