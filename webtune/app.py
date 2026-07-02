#!/usr/bin/env python3
# ============================================================================
#  AIR-AM  -  שרת בורר התדרים (web tuner)
# ----------------------------------------------------------------------------
#  ממשק וובי לבחירת תדר (פריסטים + תדר חופשי). בכל בחירה:
#   1. כותב קובץ הגדרות חדש ל-rtl_airband עם התדר הנבחר.
#   2. מפעיל מחדש את שירות rtl_airband.
#   3. הדפדפן מנגן את הסטרים מ-Icecast (mountpoint קבוע: live.mp3).
#
#  מיועד לרשת פרטית מהימנה בלבד. רץ כמשתמש לא-root (airam) עם sudoers ממוקד
#  ל-restart בלבד; אימות PIN אופציונלי (AIRAM_PIN), כבוי כברירת מחדל.
# ============================================================================
import collections
import csv
import io
import json
import logging
import os
import re
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, request, jsonify, send_from_directory, abort

import adsb   # מסלול פעיל + אינדיקציית GPS מנתוני ADS-B (thread נפרד)

# stdout => journald (השירות רץ תחת systemd); journalctl -u airam-web מציג הכל
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("airam")

# --- קבועים ---------------------------------------------------------------
CONFIG_PATH = Path("/etc/rtl_airband/airband.conf")
STATE_PATH = Path("/var/lib/airam/state.json")
MOUNT = "live.mp3"          # שם ה-stream הקבוע ב-Icecast
ICECAST_PORT = 8000
SOURCE_PW = "airam"         # חייבת להיות זהה ל-SOURCE_PW ב-install.sh (נכתבת ל-Icecast שם)
SAMPLE_RATE = 2.56          # Msps - ערוץ יחיד, חלון צר מספיק
DC_OFFSET = 0.3             # MHz - מזיזים את centerfreq מהתדר כדי להתרחק מ-spike ה-DC
# רווח SDRplay (מודל legacy של SoapySDRPlay3): שני אלמנטים נפרדים, וקטן יותר = רווח גדול יותר.
#   IFGR - הפחתת רווח בתדר הביניים, 20–59 dB.
#   RFGR - מצב ה-LNA (הפחתת רווח RF), 0–9 (לא-לינארי, ~7dB לצעד).
# כש-AGC כבוי כותבים gain = "IFGR=..,RFGR=.."; כש-AGC דלוק משמיטים את gain => AGC חומרתי.
IFGR_MIN, IFGR_MAX = 20, 59
RFGR_MIN, RFGR_MAX = 0, 9
IF_GAIN_DEFAULT = 40            # IFGR - אמצע הטווח, בטוח מפני עומס יתר
RF_GAIN_DEFAULT = 4            # RFGR - מצב LNA בינוני
OVERLOAD_DBFS = -3.0          # סף "עומס יתר": אות ערוץ קרוב ל-full scale של ה-ADC
SQUELCH_MODES = {"auto", "open", "manual"}
SNR_MIN, SNR_MAX = 0.0, 60.0   # dB - תחום clamp ל-SNR ידני
SNR_DEFAULT = 9.0              # ≈ סף ה-auto הפנימי של rtl_airband (~9.54 dB)
STATS_PATH = Path("/run/rtl_airband_stats.txt")   # tmpfs - בלי שחיקת SD
STATS_MAX_AGE = 5.0            # rtl_airband כותב כל ~1 שנייה; ~5 כתיבות => סובל ג'יטר אך עדיין מזהה restart

# --- ACARS (מצב משולב: SDR אחד בהחלפה) ------------------------------------
# מצב ACARS עוצר את rtl_airband (קול) ומריץ acarsdec על תדרי ה-ACARS. SDR אחד
# => רק צרכן אחד בכל רגע (Conflicts ב-unit מבטיח זאת). acarsdec שולח כל הודעה
# מפוענחת כ-JSON ב-UDP ל-listener כאן, וה-UI מושך אותן מ-/api/acars.
ACARS_SERVICE = "airam-acars"
ACARS_ENV_PATH = Path("/etc/airam/acars.env")
ACARS_UDP_HOST = "127.0.0.1"
ACARS_UDP_PORT = 5556                 # חייב להתאים ל-ACARS_UDP ב-acars.env
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
ACARS_GAIN_DEFAULT = -10              # ‎-10 => AGC (מוסכמת acarsdec)
ACARS_RATEMULT_DEFAULT = 160          # 160 => 2.0 MS/s (חלון ±1MHz)
ACARS_MAX_CHANNELS = 8               # מגבלת acarsdec — עד 8 ערוצים בו-זמנית
ACARS_WINDOW_MHZ = 1.9               # span מרבי בחלון דגימה אחד (2.0MS/s, עם שוליים)
ACARS_BUF_MAX = 500                   # הודעות אחרונות בזיכרון (נטענות לקליינט בעלייה, היום בלבד)
_FREQ_RE = re.compile(r"^\d{2,3}\.\d{1,3}$")   # ולידציית תדר ACARS (MHz) לפני כתיבה ל-env

# התמדה: כל הודעה מפוענחת נכתבת ל-acars.jsonl (כמו activity.jsonl) => שורדת restart.
# קורא ב-/api/acars/export ובטעינה הראשונית; thread ה-listener הוא הכותב היחיד.
ACARS_LOG_PATH = Path("/var/lib/airam/acars.jsonl")
ACARS_LOG_KEEP = 5000                 # retention על הדיסק (זנב נשמר; ייצוא לניתוח)

# מילון labels נפוץ של ACARS (best-effort, חלקי בכוונה — הלא-מוכרים נופלים ל-"Label X").
# ערך = (תיאור עברי, קבוצה). הקבוצה קובעת צבע badge ב-UI ואת עמודת category בייצוא:
#   position(ירוק) · clearance(כחול) · oooi(ענבר) · tech(אפור) · comm(אפור) · text(ברירת מחדל)
ACARS_LABELS = {
    "Q0": ("בדיקת קישור (link test)", "comm"),
    "_d": ("אישור קישור (link ack)", "comm"),
    "SA": ("ניהול מדיה (media advisory)", "comm"),
    "SQ": ("Squitter תחנת קרקע (SQ)", "comm"),
    "15": ("דיווח מיקום (label 15)", "position"),
    "54": ("מעבר לערוץ קול (voice go-ahead)", "comm"),
    ":;": ("כוונון תדר אוטומטי (autotune)", "comm"),
    "H1": ("הודעת מערכת/חברה (H1)", "text"),
    "5Z": ("שירות חברה (airline)", "text"),
    "5V": ("זמינות VHF (link mgmt)", "comm"),
    "C1": ("הודעת חברה (C1)", "text"),
    "3L": ("נתוני ULD/מטען (3L)", "tech"),
    "A4": ("הודעת לו\"ז (FSM)", "comm"),
    "WX": ("בקשת מזג אוויר (WX)", "comm"),
    "RA": ("תקשורת אוויר/קרקע", "text"),
    "RB": ("תקשורת אוויר/קרקע", "text"),
    "QA": ("OOOI · יציאה (Out)", "oooi"),
    "QB": ("OOOI · המראה (Off)", "oooi"),
    "QC": ("OOOI · נחיתה (On)", "oooi"),
    "QD": ("OOOI · חניה (In)", "oooi"),
    "80": ("OOOI · דוח OFFRP/INRP (80)", "oooi"),
    "A9": ("ATIS · מידע שדה (A9)", "comm"),
    "B9": ("בקשת אישור ATC", "clearance"),
    "BA": ("אישור ATC (clearance)", "clearance"),
}

# כיוון ההודעה (best-effort, חלקי בכוונה — כמו ACARS_LABELS): downlink = מטוס→קרקע
# (דיווח/בקשה מהמטוס), uplink = קרקע→מטוס (אישור/הודעת חברה אל המטוס). רק labels שאנו
# בטוחים בהם; השאר נופלים ל-heuristic של header או ל-None (לא מנחשים).
_ACARS_DIR_BY_LABEL = {
    "H1": "downlink", "5Z": "downlink", "C1": "downlink",
    "QA": "downlink", "QB": "downlink", "QC": "downlink", "QD": "downlink",
    "80": "downlink",   # דוח OOOI (OFFRP/INRP) מהמטוס
    "Q0": "downlink",   # link test ממטוס
    "B9": "downlink",   # בקשת אישור מהמטוס
    "3L": "downlink",   # נתוני ULD/מטען מהמטוס
    "WX": "downlink",   # בקשת METAR לשדות גיבוי מהמטוס
    "SA": "downlink",   # media advisory — המטוס מדווח על מצב הקישורים שלו
    "15": "downlink",   # דיווח מיקום מהמטוס
    "BA": "uplink",     # מתן אישור מהקרקע אל המטוס
    "A9": "uplink",     # ATIS משודר מהקרקע
    "A4": "uplink",     # FSM / הודעת לוח-זמנים מהקרקע
    "SQ": "uplink",     # squitter של תחנת הקרקע (תוקן: בעבר downlink בטעות)
    "54": "uplink",     # voice go-ahead — הוראת קרקע לעבור לערוץ קול
    ":;": "uplink",     # autotune — הוראת קרקע למקלט לעבור תדר
}
# header ניתוב של תחנת קרקע בתחילת הטקסט (למשל ‎.ATSXCXA או ‎/TLVATYA) => uplink.
# שמרני: דורש ‎. או ‎/ בתחילת השורה ואחריו מזהה תחנה אותיות-גדולות/ספרות.
_UPLINK_HEADER_RE = re.compile(r"^[./][A-Z][A-Z0-9]{3,7}\b")

# הקלטות: rtl_airband כותב קובץ MP3 לכל שידור (split_on_transmission) בשם
# <REC_BASENAME>_YYYYMMDD_HHMMSS_<Hz>.mp3 (.tmp בזמן כתיבה, rename בסגירה
# ~0.5ש' אחרי שהסקוולץ' נסגר). קובץ שהסתיים = אירוע ביומן השידורים.
REC_DIR = Path("/var/lib/airam/recordings")
REC_BASENAME = "airam"         # filename_template ב-config וגם עוגן הפרסור של השמות
REC_BYTES_PER_SEC = 6000       # CBR 48kbps (ה-patch ב-install.sh) => הערכת משך מגודל
REC_MAX_FILES = 200            # retention
REC_MAX_BYTES = 100 * 1024 * 1024
ACTIVITY_PATH = Path("/var/lib/airam/activity.jsonl")
ACTIVITY_KEEP = 500            # היומן שורד את מחיקת הקבצים (retention) - רק בלי נגינה
ACTIVITY_RETURN = 50
WATCH_INTERVAL = 10.0          # שניות בין סריקות של תיקיית ההקלטות

# תמלול ATC (אופציונלי): whisper.cpp מקומי. לכל הקלטה שמסתיימת נכתב קובץ-צד
# <file>.mp3.txt עם הטקסט. פעיל רק אם AIRAM_TRANSCRIBE=1 וגם הבינארי+המודל קיימים
# (install.sh בונה אותם רק עם INSTALL_WHISPER=1) => התקנות קיימות לא מושפעות.
TRANSCRIBE = os.environ.get("AIRAM_TRANSCRIBE", "").strip().lower() in ("1", "true", "yes", "on")
WHISPER_BIN = os.environ.get("AIRAM_WHISPER_BIN", "/usr/local/bin/whisper-cli")
WHISPER_MODEL = os.environ.get("AIRAM_WHISPER_MODEL", "/opt/airam/models/ggml-base.en.bin")
WHISPER_LANG = os.environ.get("AIRAM_WHISPER_LANG", "en")   # ATC בישראל = אנגלית
TRANSCRIBE_TIMEOUT = 120.0     # שניות לקובץ בודד (המרה + תמלול)
# רמז הקשר => מטה את המודל לפרזיולוגיית ATC ושמות מקומיים (משפר דיוק משמעותית)
WHISPER_PROMPT = ("Air traffic control radio between pilots and Ben Gurion / Tel Aviv "
                  "tower, ground, approach. Phrases: cleared for takeoff, line up and wait, "
                  "taxi to runway, hold short, contact tower, squawk, climb, descend, "
                  "heading, knots, QNH, wind, runway 03 12 21 26 30.")

APP_DIR = Path(__file__).resolve().parent


def _read_version():
    # VERSION יושב בשורש המאגר (פיתוח) או לצד app.py (ב-Pi: install.sh מעתיק אותו)
    for p in (APP_DIR / "VERSION", APP_DIR.parent / "VERSION"):
        try:
            return p.read_text().strip()
        except OSError:
            continue
    return "dev"


VERSION = _read_version()
app = Flask(__name__, static_folder=str(APP_DIR / "static"))

# כיוונון אחד בכל רגע: שני POST-ים מקבילים => שני restart שלובים זה בזה
TUNE_LOCK = threading.Lock()

# הרצה כמשתמש לא-root (חיזוק אבטחה): ה-restart עובר דרך sudoers ממוקד.
# כ-root אין צורך ב-sudo => פריסות ישנות (טרם re-install) ממשיכות לעבוד.
SUDO = [] if os.geteuid() == 0 else ["sudo", "-n"]

# אימות אופציונלי: פעיל אך ורק אם AIRAM_PIN הוגדר ב-environment של השירות.
# לא הוגדר => אפס שינוי בחוויה ("בלי סיסמאות" כברירת מחדל).
AIRAM_PIN = os.environ.get("AIRAM_PIN", "").strip()


@app.before_request
def _guard():
    """הגנות קלות על בקשות משנות-מצב (POST/PUT/DELETE):
      1. CSRF / DNS-rebinding: אם נשלח Origin/Referer הוא חייב להתאים ל-Host.
      2. אימות אופציונלי: אם AIRAM_PIN הוגדר, נדרש header X-AIRAM-PIN תואם.
    בקשות GET (סטרים/מדדים/health/activity/airspace/metar/power) לא מושפעות."""
    if request.method not in ("POST", "PUT", "DELETE"):
        return None
    origin = request.headers.get("Origin") or request.headers.get("Referer")
    if origin and urlparse(origin).netloc != request.host:
        return jsonify(ok=False, error="מקור הבקשה לא תואם (Origin)"), 403
    if AIRAM_PIN and request.headers.get("X-AIRAM-PIN", "") != AIRAM_PIN:
        return jsonify(ok=False, error="נדרש PIN", auth=True), 401
    return None

# פריסטים של נתב"ג / TMA - רק זריעה ראשונית; מרגע עריכה בממשק האמת היא
# /var/lib/airam/presets.json (נטען בכל בקשה - הקובץ זעיר והעריכה נדירה)
DEFAULT_PRESETS = [
    {"name": "מגדל (Tower)",     "freq": 134.600},
    {"name": "ATIS",             "freq": 132.500, "sq": "open"},  # רציף => תמיד פתוח
    {"name": "קרקע מזרח",        "freq": 129.200},
    {"name": "גישה/המראה",       "freq": 120.500},
    {"name": "Tel Aviv Control", "freq": 121.400},
    {"name": "קרקע מערב",        "freq": 118.050},
    {"name": "מסירה (Delivery)", "freq": 121.950},
    {"name": "Guard (חירום)",    "freq": 121.500},
]
PRESETS_PATH = Path("/var/lib/airam/presets.json")
PRESETS_MAX = 30


def _validate_presets(lst):
    """(ok, cleaned) - מנרמל ומאמת רשימת פריסטים מהלקוח/מהדיסק."""
    if not isinstance(lst, list) or len(lst) > PRESETS_MAX:
        return False, None
    out = []
    for p in lst:
        if not isinstance(p, dict):
            return False, None
        name = str(p.get("name", "")).strip()
        try:
            freq = float(p.get("freq"))
        except (TypeError, ValueError):
            return False, None
        if not name or len(name) > 40 or not (0.1 <= freq <= 1999.5):
            return False, None
        item = {"name": name, "freq": round(freq, 4)}
        sq = p.get("sq")
        if sq is not None:
            sq = str(sq).lower()
            if sq not in SQUELCH_MODES:
                return False, None
            item["sq"] = sq
        out.append(item)
    return True, out


def load_presets():
    try:
        ok, cleaned = _validate_presets(json.loads(PRESETS_PATH.read_text()))
        if ok:
            return cleaned
    except Exception:
        pass   # אין קובץ / פגום => ברירת המחדל (הקובץ נכתב רק בעריכה הראשונה)
    return [dict(p) for p in DEFAULT_PRESETS]

DEFAULT_STATE = {"freq": 132.500, "mod": "am", "agc": True,
                 "if_gain": IF_GAIN_DEFAULT, "rf_gain": RF_GAIN_DEFAULT,
                 "squelch_mode": "open", "squelch_snr": SNR_DEFAULT,  # ברירת מחדל ATIS => תמיד פתוח
                 "app_mode": "voice",  # "voice" (rtl_airband) | "acars" (acarsdec) | "off" (standby)
                 "acars_freqs": ACARS_FREQS_DEFAULT}


# --- שורת ה-squelch: מקור אמת יחיד -----------------------------------------
def _squelch_line(squelch_mode, squelch_snr):
    """מחזיר את שורת ה-squelch (או None) לכל מצב. שנה כאן בלבד.
      auto   -> None  (ללא שורה => squelch אוטומטי, ~9.54 dB מעל הרעש)
      open   -> תמיד פתוח (ל-ATIS / שידור רציף)
      manual -> סף SNR ידני ב-dB
    תמיד squelch_snr_threshold (לא dBFS) => בלתי תלוי ב-gain/AGC, ואף פעם לא שני
    הפרמטרים יחד.
    """
    if squelch_mode == "manual":
        return f"        squelch_snr_threshold = {float(squelch_snr):.1f};"
    if squelch_mode == "open":
        return "        squelch_snr_threshold = 0;"   # 0 = תמיד פתוח
    return None  # auto


# --- בניית קובץ ההגדרות ל-rtl_airband ------------------------------------
def render_config(freq, mod, agc, if_gain, rf_gain, squelch_mode="auto", squelch_snr=SNR_DEFAULT):
    f = float(freq)
    lines = [
        "# נוצר אוטומטית ע\"י AIR-AM web tuner. שינויים ידניים נדרסים בכל כיוונון.",
        "localtime = true;   # חותמות הזמן בשמות קובצי ההקלטה בזמן מקומי",
        f'stats_filepath = "{STATS_PATH}";   # מדדי RF (signal/noise) ל-/api/metrics',
        "devices:",
        "(",
        "  {",
        '    type = "soapysdr";',
        '    device_string = "driver=sdrplay";',
    ]
    if not agc:
        # רווח ידני => שני אלמנטים. הגדרת gain מבטלת אוטומטית את ה-AGC בדרייבר.
        lines.append(f'    gain = "IFGR={int(if_gain)},RFGR={int(rf_gain)}";')  # אחרת AGC אוטומטי
    lines += [
        f"    sample_rate = {SAMPLE_RATE};",
        '    mode = "multichannel";',
        f"    centerfreq = {f + DC_OFFSET:.4f};",   # מוסט מהערוץ כדי להימנע מ-spike ה-DC
        "    channels:",
        "    (",
        "      {",
        f"        freq = {f:.4f};",
        f'        modulation = "{mod}";',
    ]
    sq = _squelch_line(squelch_mode, squelch_snr)
    if sq is not None:
        lines.append(sq)
    record = squelch_mode != "open"   # "פתוח" (ATIS) => הסקוולץ' לא נסגר לעולם
    lines += [
        "        outputs:",
        "        (",
        "          {",
        '            type = "icecast";',
        '            server = "127.0.0.1";',
        f"            port = {ICECAST_PORT};",
        f'            mountpoint = "{MOUNT}";',
        f'            name = "AIR-AM {f:.3f}";',
        '            username = "source";',
        f'            password = "{SOURCE_PW}";',
        "          }" + ("," if record else ""),
    ]
    if record:
        lines += [
            "          {",
            '            type = "file";',
            f'            directory = "{REC_DIR}";',
            f'            filename_template = "{REC_BASENAME}";',
            "            split_on_transmission = true;   # קובץ MP3 נפרד לכל שידור",
            "            include_freq = true;            # התדר (Hz) בשם הקובץ",
            "          }",
        ]
    lines += [
        "        );",
        "      }",
        "    );",
        "  }",
        ");",
        "",
    ]
    return "\n".join(lines)


def _atomic_write(path, text):
    """כתיבה אטומית (tmp + rename): rtl_airband יכול לעלות בכל רגע
    (Restart=always / udev) ואסור שיקרא קובץ חצי-כתוב."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def write_config(freq, mod, agc, if_gain, rf_gain, squelch_mode="auto", squelch_snr=SNR_DEFAULT):
    _atomic_write(CONFIG_PATH, render_config(freq, mod, agc, if_gain, rf_gain, squelch_mode, squelch_snr))


def load_state():
    try:
        st = json.loads(STATE_PATH.read_text())
        return {**DEFAULT_STATE, **st}
    except Exception:
        return dict(DEFAULT_STATE)


def save_state(st):
    _atomic_write(STATE_PATH, json.dumps(st))


# --- הפעלה מחדש מאומתת + רולבק --------------------------------------------
def _sdr_present():
    """בדיקת USB מהירה (vendor 1df7 = SDRplay) בלי לפתוח את המכשיר."""
    try:
        return subprocess.run(["lsusb", "-d", "1df7:"],
                              capture_output=True, timeout=5).returncode == 0
    except Exception:
        return True   # אין lsusb / ספק => מניחים שמחובר (עדיף רולבק מיותר מאף-פעם)


def _journal_tail(service="rtl_airband", lines=8):
    return subprocess.run(["journalctl", "-u", service, "-n", str(lines), "--no-pager"],
                          capture_output=True, text=True).stdout


def _restart_and_verify():
    """מפעיל מחדש את rtl_airband ומוודא שנשאר חי.
    מחזיר (error, detail, sdr_down): ‏sdr_down=True כשה-restart נתקע על המתנה
    ל-SDR — במצב הזה גם רולבק נדון לאותו כישלון ואין טעם לנסות אותו.
    ה-restart עצמו יכול לחסום עד ~30 שניות (airam-wait-sdrplay) כשה-SDR מנותק."""
    try:
        r = subprocess.run([*SUDO, "systemctl", "restart", "rtl_airband"],
                           capture_output=True, text=True, timeout=45)
    except subprocess.TimeoutExpired:
        return "ה-restart נתקע — בדוק שה-SDR מחובר", None, True
    if r.returncode != 0:
        # המסלול הנפוץ כשה-SDR מנותק: airam-wait-sdrplay ממצה 30 ניסיונות
        # וה-restart נכשל עם rc!=0 (לא timeout) => מזהים לפי נוכחות ה-USB.
        return (r.stderr or "restart failed").strip(), _journal_tail(), not _sdr_present()
    # restart מחזיר 0 כשהשירות עלה, אבל rtl_airband יכול לקרוס על config רע
    # גם ~2 שניות אחרי העלייה => פולינג (לא בדיקה בודדת שמפספסת קריסה מאוחרת).
    for _ in range(7):
        time.sleep(0.5)
        try:
            chk = subprocess.run(["systemctl", "is-active", "rtl_airband"],
                                 capture_output=True, text=True, timeout=5)
        except subprocess.TimeoutExpired:
            continue   # systemctl תקוע => מדלגים על הבדיקה הזו, לא תוקעים את הבקשה
        if chk.stdout.strip() != "active":
            return "rtl_airband נכשל לעלות — בדוק תדר/חיבור SDR", _journal_tail(), False
    return None, None, False


def _rollback(prev):
    """כיוונון נכשל => משחזרים את ההגדרות האחרונות שעבדו ומרימים מחדש (best-effort)."""
    log.warning("rollback to %.3f MHz", prev["freq"])
    try:
        write_config(prev["freq"], prev["mod"], prev["agc"], prev["if_gain"],
                     prev["rf_gain"], prev["squelch_mode"], prev["squelch_snr"])
        subprocess.run([*SUDO, "systemctl", "restart", "rtl_airband"],
                       capture_output=True, text=True, timeout=45)
    except Exception:
        pass


# --- ACARS: listener, ring-buffer, ומעבר מצב -----------------------------
_acars_lock = threading.Lock()
_acars_msgs = collections.deque(maxlen=ACARS_BUF_MAX)
_acars_seq = 0                 # מזהה רץ גלובלי (cursor ל-UI: "תן לי הודעות חדשות מ-id")


def _scan_latlon(obj):
    """סורק רקורסיבית מבנה libacars אחר זוג lat/lon תקין (ADS-C/CPDLC). מחזיר
    (lat, lon) או None. הגנתי לשינויי סכמה בין גרסאות — מזהה לפי שם המפתח, לא מבנה."""
    lat = lon = None

    def walk(o):
        nonlocal lat, lon
        if lat is not None and lon is not None:
            return
        if isinstance(o, dict):
            for k, v in o.items():
                kl = str(k).lower()
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    if lat is None and kl in ("lat", "latitude"):
                        lat = float(v)
                    elif lon is None and kl in ("lon", "lng", "long", "longitude"):
                        lon = float(v)
                else:
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(obj)
    if lat is None or lon is None:
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return None                           # 0/0 = "אין מיקום" טיפוסי, לא מרכז האוקיינוס
    return round(lat, 5), round(lon, 5)


# מיקום בפורמט ARINC קומפקטי בטקסט חופשי. שני פורמטים נתמכים:
# 1. עם נקודה עשרונית (ואופציונלית פסיק בין lat ל-lon): N3206.0,E03450.0 או N3206.0 E03450.0
# 2. ספרה עשרונית ללא נקודה (DDMMf / DDDMMf): N32042E034560 = N 32°04.2' E 034°56.0'
# שמרני בכוונה — [0-5]\d אוכף דקות 00–59 => כמעט בלי false positives ממרצפי-ספרות מקריים.
_TEXT_POS_RE = re.compile(
    r"([NS])\s?(\d{2})([0-5]\d)\.(\d{1,3})[,\s]?([EW])\s?(\d{3})([0-5]\d)\.(\d{1,3})")
# פורמט קומפקטי ללא נקודה: N32042E034560 — ספרת עשרון מחוברת ישירות אחרי הדקות.
# מנסים אחרי הפורמט עם נקודה (עדיפות נמוכה) כי הוא מדויק פחות.
_TEXT_POS_COMPACT_RE = re.compile(
    r"([NS])(\d{2})([0-5]\d)(\d)([EW])(\d{3})([0-5]\d)(\d)")
# הערה: פורמט ה-login של LLBG (`02XSTLVLLBG03200N03452E...`) *אינו* מחולץ —
# ה-DDMM שם הוא נ"צ ה*שדה* (reference של נתב"ג שמשותף לכל מטוס שמתחבר), לא מיקום
# המטוס. חילוצו הדביק 📍 מטעה על כל הודעת login. ראה CHANGELOG 1.7.1.

# /.POS/ = תגובת מטוס ל-REQPOS (position request מהקרקע). פורמט מבני לחלוטין:
# /.POS/TS{HHMMSS},{DDMMYY}{N}{DD}{MMf}{E}{DDD}{MMf},,{t},{?},{WPT},{ETA_WPT},,{fuel},,{spd},{alt}
# lat/lon: DD+MMf = מעלות + דקות-עם-עשרון (3 ספרות: MM*10+f). דוגמה: 006 = 00.6'
# הפורמט אמין גם עם error (פרוטוקול מבני, לא heuristic) ⇒ נחלץ לפני שמירת error guard.
_POS_REPORT_RE = re.compile(
    r"/\.POS/TS\d{6},\d{6}"        # TS timestamp + date (6 digits each)
    r"([NS])(\d{2})(\d{3})"         # lat: NS, 2-digit deg, 3-digit MMf
    r"([EW])(\d{3})(\d{3})"         # lon: EW, 3-digit deg, 3-digit MMf
    r",,\d{6},\d+,"                 # gap fields (time2, unknown)
    r"([A-Z][A-Z0-9]{1,7})"        # next waypoint (2–8 chars)
    r",(\d{6})"                     # ETA to waypoint (HHMMSS)
    r"(?:,,[^,]*,,[^,]*,([A-Z0-9]{2,6}))?"  # optional: fuel,,spd,{FL/alt code}
)


def _text_latlon(text):
    """heuristic שמרני לחילוץ מיקום מטקסט חופשי (פורמט ARINC קומפקטי). מחזיר (lat, lon)
    או None. מכוון לדיוק על פני כיסוי => מחזיר רק כשהתבנית מלאה וברורה."""
    if not text:
        return None

    def _parse(groups, compact=False):
        try:
            ns, la_d, la_m, la_f, ew, lo_d, lo_m, lo_f = groups
            if compact:
                lat = int(la_d) + (int(la_m) + int(la_f) / 10) / 60
                lon = int(lo_d) + (int(lo_m) + int(lo_f) / 10) / 60
            else:
                lat = int(la_d) + float(la_m + "." + (la_f or "0")) / 60
                lon = int(lo_d) + float(lo_m + "." + (lo_f or "0")) / 60
        except (ValueError, TypeError):
            return None
        if ns == "S":
            lat = -lat
        if ew == "W":
            lon = -lon
        if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
            return None
        return round(lat, 5), round(lon, 5)

    m = _TEXT_POS_RE.search(text)
    if m:
        return _parse(m.groups())
    m = _TEXT_POS_COMPACT_RE.search(text)
    if m:
        return _parse(m.groups(), compact=True)
    return None


def _ddmmf(deg, mmf):
    """מעלות + דקות-עם-עשרון-מחובר (MMf: 3 ספרות — דקות (2) + עשרון (1),
    006 = 00.6', 539 = 53.9') => מעלות עשרוניות. משותף ל-/.POS/ ול-label 15."""
    m = int(mmf)
    return int(deg) + (m // 10 + (m % 10) / 10) / 60


def _parse_pos_report(text):
    """מחלץ נ\"צ + waypoint + ETA מהודעת /.POS/ (תגובה ל-REQPOS מהקרקע).
    מחזיר (lat, lon, decoded_str) או None.
    אמין גם עם acarsdec error כי הפורמט מבני — אין heuristic על טקסט חופשי."""
    if not text or "/.POS/" not in text:
        return None
    m = _POS_REPORT_RE.search(text)
    if not m:
        return None
    ns, la_d, la_mf, ew, lo_d, lo_mf, wpt, eta, alt = m.groups()
    try:
        lat, lon = _ddmmf(la_d, la_mf), _ddmmf(lo_d, lo_mf)
    except (ValueError, TypeError):
        return None
    if ns == "S":
        lat = -lat
    if ew == "W":
        lon = -lon
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return None
    parts = [f"WPT {wpt}"]
    if eta and len(eta) == 6:
        parts.append(f"ETA {eta[:2]}:{eta[2:4]}z")
    if alt:
        parts.append(alt)
    return round(lat, 5), round(lon, 5), " · ".join(parts)


def _libacars_decode(obj):
    """(kind, text) ממבנה libacars: kind ל-badge ('CPDLC'/'ADS-C'/'ARINC-622'),
    ו-text קצר קריא (CPDLC clearance וכו') אם נמצא. הגנתי לשינויי סכמה."""
    blob = json.dumps(obj, ensure_ascii=False).lower()
    kind = ("CPDLC" if "cpdlc" in blob
            else "ADS-C" if ("adsc" in blob or "ads-c" in blob)
            else "ARINC-622")
    texts = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if (isinstance(v, str) and len(v.strip()) > 3
                        and any(t in str(k).lower() for t in ("text", "msg", "message"))):
                    texts.append(v.strip())
                else:
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(obj)
    text = " · ".join(dict.fromkeys(texts))[:300] or None   # dedup בשמירת סדר
    return kind, text


def _acars_direction(label, text):
    """heuristic שמרני לכיוון ההודעה: 'uplink' (קרקע→מטוס) / 'downlink' (מטוס→קרקע) / None.
    label מוכר קודם (אמין), אחרת header ניתוב בטקסט => uplink. None כשלא חד-משמעי (לא מנחשים)."""
    d = _ACARS_DIR_BY_LABEL.get(label)
    if d:
        return d
    if isinstance(text, str) and _UPLINK_HEADER_RE.match(text.lstrip()):
        return "uplink"
    return None


_ATIS_WIND_RE = re.compile(r"(\d{3})/(\d{2,3}KT)|WIND\s+(\d+)/(\d+)")
_ATIS_RWY_RE = re.compile(r"R(?:WY|/W)\s?(\d{1,2}[LRC]?)", re.IGNORECASE)
_ATIS_QNH_RE = re.compile(r"Q(?:NH\s?)?(\d{4})")
_ACTYPE_RE = re.compile(r"\b(B7[3-9]\d|A[23][0-9]\d|E[17][0-9]\d|CRJ\d|AT[57]\d)\b")
# זוגות OUT/OFF/ON/IN + זמן (HHMM עם/בלי :) — \b לפני הכותרת, בלי \b אחריה כי הזמן
# עלול להיות צמוד (OUT1420). IN לא בתחילת מילה לפני ספרה אבל הפורמטים בשטח לא מופרדים.
_OOOI_PAIR_RE = re.compile(r"\b(OUT|OFF|ON|IN)\s?(\d{2}[:.]\d{2}|\d{4})", re.IGNORECASE)

# WX (בקשות מזג אוויר): מחלץ קודי ICAO מארבע אותיות. מסנן מילות-מפתח שאינן שדות תעופה.
_WX_ICAO_RE = re.compile(r"\b([A-Z]{4})\b")
_WX_NON_AIRPORT = frozenset({
    "METAR", "SPECI", "SIGMET", "PIREP", "ATIS", "CAVOK", "NOSIG", "TEMPO",
    "BECMG", "PROB", "FROM", "TILL", "WIND", "GUST", "SHRA", "TSRA", "ACFT",
    "ACARS", "UPDT", "REQU", "RESP",
})
_HOME_AIRPORT = "LLBG"


def _parse_atis(text):
    """Best-effort: מחלץ wind/runway/QNH מטקסט A9 (ATIS). מחזיר string קצר או None."""
    if not text:
        return None
    parts = []
    m = _ATIS_RWY_RE.search(text)
    if m:
        parts.append(f"מסלול {m.group(1)}")
    m = _ATIS_WIND_RE.search(text)
    if m:
        wind = (m.group(1) + "/" + m.group(2)) if m.group(1) else (m.group(3) + "/" + m.group(4))
        parts.append(f"רוח {wind}")
    m = _ATIS_QNH_RE.search(text)
    if m:
        parts.append(f"QNH {m.group(1)}")
    return " · ".join(parts) if parts else None


def _parse_oooi_80(text):
    """Best-effort: מחלץ זמני OUT/OFF/ON/IN מהודעות OFFRP/INRP (label 80)."""
    if not text:
        return None
    pairs = []
    for m in _OOOI_PAIR_RE.finditer(text):
        k = m.group(1).upper()
        t = m.group(2).replace(".", "").replace(":", "")
        if len(t) == 4:
            t = t[:2] + ":" + t[2:]
        pairs.append(f"{k} {t}")
    return " · ".join(pairs) if pairs else None


def _extract_actype(label, text):
    """Best-effort: מחלץ סוג מטוס (למשל B738, A320) מטקסט H1/C1. מחזיר string או None."""
    if label not in ("H1", "C1") or not text:
        return None
    m = _ACTYPE_RE.search(text)
    return m.group(1) if m else None


def _parse_wx_alternates(text):
    """מחלץ שדות alternate מהודעת WX (בקשת METAR לשדות גיבוי).
    שני קודי ICAO+ שאינם LLBG = תכנון alternate פעיל. מחזיר decoded קצר או None."""
    if not text:
        return None
    seen: set = set()
    codes = []
    for c in _WX_ICAO_RE.findall(text):
        if c in seen or c in _WX_NON_AIRPORT or c == _HOME_AIRPORT:
            continue
        seen.add(c)
        codes.append(c)
    if len(codes) >= 2:
        return "ALTERNATE: " + " · ".join(codes)
    if codes:
        return f"WX: {codes[0]}"
    return None


# --- חבילת פענוח עמוק: SA / H1 / FPN / label 15 / SQ / autotune -------------
# כל ה-parsers מופעלים *רק* לפי label (dispatch ב-_normalize_acars) => אין סיכון
# false-positive בין labels; בתוך ה-label — regex מעוגן-תחילה ושמרני.

# media advisory (label SA): '0' + E/L (established/lost) + אות מדיה + HHMMSS +
# רשימת מדיות זמינות. דוגמה: 0EV093425VS = קישור VHF נוצר ב-09:34:25, זמין VHF+SATCOM.
_SA_MEDIA = {"V": "VHF", "S": "SATCOM", "H": "HF", "G": "GlobalStar", "C": "Iridium",
             "2": "VDL-M2", "X": "Inmarsat", "I": "Iridium", "T": "טלפוני"}
_SA_RE = re.compile(r"^0([EL])([VSHGCX2IT])([0-2]\d)([0-5]\d)([0-5]\d)([VSHGCX2IT]*)")

# H1 sub-label: '#' + מזהה מקור בן 2 תווים בתחילת הטקסט (#DF = מקליט, #M1 = FMC...).
_H1_SUB_RE = re.compile(r"^#([A-Z][A-Z0-9])")
_H1_SUBLABELS = {
    "DF": "מקליט נתונים (DFDAU)", "M1": "מחשב ניהול טיסה (FMC)",
    "M2": "FMC 2", "M3": "FMC 3", "CF": "מערכת תחזוקה (CFDS)",
    "EC": "בקר מנוע (EEC)", "EI": "דיווח מנוע", "WO": "תצפית מז\"א",
    "PS": "דיווח מיקום", "S1": "בקשת קרקע (S1)",
}
_H1_POS_RE = re.compile(r".{0,2}POS")     # POS מיד אחרי ההדר (עם block char אופציונלי)

# ‎/FPN/ = תוכנית טיסה בתוך H1: ‏:DA: יציאה, ‏:AA: יעד, ‏:F: נקודות מופרדות '..'
# (לכל נקודה עשוי להיצמד נ"צ אחרי פסיק — נחתך).
_FPN_DA_RE = re.compile(r":DA:([A-Z]{4})")
_FPN_AA_RE = re.compile(r":AA:([A-Z]{4})")
_FPN_F_RE = re.compile(r":F:([A-Z0-9.,]+)")
_FPN_MAX_WPTS = 8

# דיווח מיקום קלאסי (label 15): '(2' + NS+DD+MMf + EW+DDD+MMf. אותו קידוד דקות
# של /.POS/ (_ddmmf). דקות נאכפות [0-5]\d => כמעט בלי false positives.
_L15_RE = re.compile(r"^\(2([NS])(\d{2})([0-5]\d\d)([EW])(\d{3})([0-5]\d\d)")

# squitter תחנת קרקע (label SQ): '0' + version + 2 אותיות + IATA(3) + ICAO(4) +
# נ"צ התחנה + אות מדיה + תדר kHz + '/'. דוגמה: 02XSTLVLLBG03200N03452EV136975/
_SQ_RE = re.compile(r"^0\d[A-Z]{2}([A-Z]{3})([A-Z]{4})")
_SQ_FREQ_RE = re.compile(r"[A-Z](\d{6})/")

# autotune (label ':;'): הוראת קרקע למקלט לעבור תדר — 6 ספרות kHz בתחום ה-air band.
_AUTOTUNE_RE = re.compile(r"\b(1[23]\d{4})\b")


def _parse_sa_media(text):
    """media advisory (SA): איזה קישור נוצר/אבד, מתי, ואילו מדיות זמינות.
    פורמט שדות-תו-בודד מעוגן. מחזיר string קצר או None."""
    if not text:
        return None
    m = _SA_RE.match(text.strip())
    if not m:
        return None
    ev, media, hh, mm, ss, avail = m.groups()
    parts = [f"קישור {_SA_MEDIA.get(media, media)} " + ("נוצר" if ev == "E" else "אבד"),
             f"{hh}:{mm}:{ss}z"]
    if avail:
        names = [_SA_MEDIA.get(c, c) for c in dict.fromkeys(avail)]   # dedup בשמירת סדר
        parts.append("זמין: " + "·".join(names))
    return " · ".join(parts)


def _parse_fpn(text):
    """‎/FPN/ (תוכנית טיסה ב-H1): יציאה→יעד + רשימת waypoints. מחזיר string או None."""
    idx = text.find("/FPN/")
    if idx < 0:
        return None
    seg = text[idx:]
    parts = []
    da, aa = _FPN_DA_RE.search(seg), _FPN_AA_RE.search(seg)
    if da and aa:
        parts.append(f"{da.group(1)}→{aa.group(1)}")
    elif aa:
        parts.append(f"יעד {aa.group(1)}")
    m = _FPN_F_RE.search(seg)
    if m:
        wpts = []
        for tok in m.group(1).split(".."):
            name = tok.split(",")[0].strip()          # חיתוך נ"צ צמוד (PURLA,N32016...)
            if 2 <= len(name) <= 8 and name[0].isalpha():
                wpts.append(name)
        if wpts:
            shown = " ".join(wpts[:_FPN_MAX_WPTS])
            if len(wpts) > _FPN_MAX_WPTS:
                shown += f" (+{len(wpts) - _FPN_MAX_WPTS})"
            parts.append(shown)
    return "תוכנית טיסה " + " · ".join(parts) if parts else None


def _parse_h1(text):
    """H1: זיהוי מקור ההודעה לפי sub-label (#DF/#M1/...) + פענוח /FPN/ אם קיים.
    מחזיר string קצר או None (H1 בלי הדר '#' => אין מה להסיק, לא מנחשים)."""
    if not text:
        return None
    text = text.lstrip()
    parts = []
    m = _H1_SUB_RE.match(text)
    if m:
        sub = m.group(1)
        desc = _H1_SUBLABELS.get(sub)
        if desc is None and sub[0] == "T" and sub[1].isdigit():
            desc = "מסוף תא (cabin terminal)"
        if desc:
            parts.append(desc)
        if _H1_POS_RE.match(text[m.end():]):
            parts.append("דיווח מיקום")
    fpn = _parse_fpn(text)
    if fpn:
        parts.append(fpn)
    return " · ".join(parts) if parts else None


def _parse_label15(text):
    """נ\"צ מדיווח מיקום קלאסי (label 15). פורמט מעוגן-מבני (כמו /.POS/) =>
    אמין גם עם error>0. מחזיר (lat, lon) או None."""
    if not text:
        return None
    m = _L15_RE.match(text.lstrip())
    if not m:
        return None
    ns, la_d, la_mf, ew, lo_d, lo_mf = m.groups()
    lat, lon = _ddmmf(la_d, la_mf), _ddmmf(lo_d, lo_mf)
    if ns == "S":
        lat = -lat
    if ew == "W":
        lon = -lon
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return None
    return round(lat, 5), round(lon, 5)


def _parse_sq(text):
    """squitter תחנת קרקע (SQ): מזהה תחנה (IATA+ICAO) ותדר. *בלי* חילוץ נ"צ —
    ה-DDMM בהודעה הוא מיקום התחנה, לא המטוס (לקח 1.7.1)."""
    if not text:
        return None
    m = _SQ_RE.match(text.strip())
    if not m:
        return None
    iata, icao = m.groups()
    parts = [f"תחנת קרקע {iata} ({icao})"]
    fm = _SQ_FREQ_RE.search(text)
    if fm:
        khz = int(fm.group(1))
        if 118000 <= khz <= 137000:
            parts.append(f"{khz / 1000:.3f}MHz")
    return " · ".join(parts)


def _parse_autotune(text):
    """label ':;' — הוראת קרקע למקלט ה-ACARS לעבור תדר (kHz בטקסט)."""
    if not text:
        return None
    m = _AUTOTUNE_RE.search(text)
    if not m:
        return None
    khz = int(m.group(1))
    if not (118000 <= khz <= 137000):
        return None
    return f"כוונון אוטומטי ל-{khz / 1000:.3f}MHz"


def _normalize_acars(m):
    """מצמצם הודעת acarsdec JSON לשדות שה-UI מציג, בפורמט *אחיד* לכל סוגי ההודעות:
    קטגוריה קריאה (label => תיאור), קבוצה (לצבע), ומיקום (lat/lon) כשזמין. עמיד
    לשדות חסרים (הרבה הודעות ACARS הן ACK ריק בלי tail/flight/text)."""
    def g(*keys):
        for k in keys:
            v = m.get(k)
            if v not in (None, ""):
                return v
        return None

    text = g("text")
    if isinstance(text, str):
        text = text.replace("\r\n", "\n").replace("\r", "\n").strip()

    label = g("label")
    desc, group = ACARS_LABELS.get(label, (None, "text")) if label else (None, "comm")
    category = desc or (f"Label {label}" if label else "הודעה")

    # פענוח ARINC-622 (libacars): kind => badge וקבוצה, וטקסט קריא אם יש.
    lat = lon = pos_src = decoded = None
    libacars = m.get("libacars")
    if libacars:
        kind, dtext = _libacars_decode(libacars)
        category, decoded = kind, dtext
        group = "clearance" if kind == "CPDLC" else "position" if kind == "ADS-C" else group
        pos = _scan_latlon(libacars)
        if pos:
            lat, lon, pos_src = pos[0], pos[1], "adsc"

    # /.POS/ = תגובת REQPOS: פרוטוקול מבני (לא heuristic) => אמין גם עם error.
    # נחלץ לפני בדיקת error כי ספרה שהתהפכה ב-prefix שגרם ל-error לא פוגמת את הקואורדינטה.
    if lat is None and text:
        pos = _parse_pos_report(text)
        if pos:
            lat, lon, pos_src = pos[0], pos[1], "pos-report"
            if decoded is None and pos[2]:
                decoded = pos[2]                  # WPT · ETA · alt code

    # label 15 (דיווח מיקום קלאסי): פורמט מעוגן-מבני כמו /.POS/ => לפני שומר ה-error.
    if lat is None and label == "15" and text:
        pos = _parse_label15(text)
        if pos:
            lat, lon, pos_src = pos[0], pos[1], "label15"

    # נפילה: מיקום מקודד בטקסט חופשי — אבל *רק* מ-frame נקי. acarsdec error>0 = ביטים
    # שתוקנו/לא-תוקנו; ספרה אחת שהתהפכה בקואורדינטה => מטוס במקום שגוי על המפה. ADS-C
    # (libacars) לעיל מוגן-CRC ולכן נשמר גם עם error; ה-heuristic הטקסטואלי לא — לכן מגודר.
    if lat is None and not m.get("error"):
        pos = _text_latlon(text)
        if pos:
            lat, lon, pos_src = pos[0], pos[1], "text"

    if lat is not None:
        group = "position"                    # יש מיקום => תמיד ירוק (קבוצת position)

    # פענוח מבנה label-ספציפי (רק אם libacars לא סיפק decoded כבר)
    if decoded is None:
        if label == "80":
            decoded = _parse_oooi_80(text)
        elif label == "A9":
            decoded = _parse_atis(text)
        elif label == "WX":
            decoded = _parse_wx_alternates(text)
        elif label == "SA":
            decoded = _parse_sa_media(text)
        elif label == "H1":
            decoded = _parse_h1(text)
        elif label == "SQ":
            decoded = _parse_sq(text)
        elif label == ":;":
            decoded = _parse_autotune(text)

    return {
        "t": g("timestamp") or time.time(),   # epoch seconds (float) מ-acarsdec (חסר => עכשיו)
        "freq": g("freq"),                    # MHz
        "level": g("level"),                  # dBFS
        "label": label,
        "category": category,                 # תיאור קריא אחיד (label/ARINC-622)
        "group": group,                       # קבוצה לצבע ב-UI / עמודה בייצוא
        "tail": g("tail", "registration"),
        "flight": g("flight", "fid"),
        "mode": g("mode"),
        "msgno": g("msgno"),
        "dir": _acars_direction(label, text),  # "uplink" | "downlink" | None (best-effort)
        "lat": lat,
        "lon": lon,
        "pos_src": pos_src,                   # "adsc" | "text" | None
        "decoded": decoded,                   # טקסט מפוענח קצר (CPDLC/ATIS/OOOI וכו') או None
        "text": text,
        "error": m.get("error"),
        "actype": _extract_actype(label, text),  # סוג מטוס best-effort (H1/C1) או None
    }


def _append_acars_log(rec):
    """מוסיף הודעה מנורמלת ל-acars.jsonl (append; thread ה-listener הוא הכותב היחיד).
    נכשל בשקט (דיסק מלא וכו') => הפיד החי ממשיך לפעול."""
    try:
        ACARS_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with ACARS_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        log.exception("acars log append")


def _trim_acars_log():
    """קיצוץ ל-ACARS_LOG_KEEP שורות (rewrite אטומי). נקרא מדי פעם מ-thread ה-listener
    (הכותב היחיד => אין מרוץ). קוראים (ייצוא) סובלים שורה אחרונה חלקית."""
    try:
        lines = ACARS_LOG_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    if len(lines) > ACARS_LOG_KEEP:
        _atomic_write(ACARS_LOG_PATH, "\n".join(lines[-ACARS_LOG_KEEP:]) + "\n")


def _today_start():
    """epoch של חצות מקומי (שעון ה-Pi) של היום. רצפת-זמן ל"היום בלבד": מסננת את
    טעינת ההיסטוריה ואת /api/acars => סשן חדש לא מוצף בתעבורת ימים קודמים.
    ההיסטוריה המלאה בדיסק (acars.jsonl) נשמרת וזמינה בייצוא וב-?all=1."""
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def _load_acars_history():
    """טוען את זנב acars.jsonl ל-ring buffer בעלייה => הודעות *היום* שורדות restart,
    ממוינות לפי זמן (t עולה) עם id רץ. נקרא *לפני* הפעלת thread ה-listener (אין מרוץ).
    רק הודעות מהיום נטענות לזיכרון (ההיסטוריה המלאה נשמרת בדיסק)."""
    global _acars_seq
    try:
        lines = ACARS_LOG_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    recs = []
    for ln in lines[-ACARS_BUF_MAX:]:
        try:
            recs.append(json.loads(ln))
        except ValueError:
            continue                          # שורה פגומה (כתיבה חלקית) => דילוג
    floor = _today_start()
    recs = [r for r in recs if (r.get("t") or 0) >= floor]   # היום בלבד (הדיסק נשמר)
    recs.sort(key=lambda r: r.get("t") or 0)
    with _acars_lock:
        for r in recs:
            _acars_seq += 1
            r["id"] = _acars_seq
            _acars_msgs.append(r)
    if recs:
        log.info("ACARS: נטענו %d הודעות מההיסטוריה", len(recs))


def _acars_listener():
    """thread רקע: מאזין ל-UDP מ-acarsdec (-j), שומר ל-acars.jsonl, ומכניס ל-ring
    buffer. רץ תמיד (גם במצב קול) — פשוט לא יגיעו דאטהגרמות כש-acarsdec כבוי."""
    global _acars_seq
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((ACARS_UDP_HOST, ACARS_UDP_PORT))
    except OSError:
        log.warning("ACARS listener: port %d busy - /api/acars יחזיר ריק", ACARS_UDP_PORT)
        return
    seen = 0
    # dedup: (tail, label, text[:80]) → (timestamp, rec_dict). מונע כפילות מ-ACARS retries
    # (כש-ground station לא שולח ACK, המטוס שולח שוב — עד 7 פעמים ב-APU fault של OO-ACF).
    # retry_count מצטבר על הכרטיס המקורי בזיכרון; ה-JSONL נשמר נקי מחזרות.
    _dedup: dict = {}
    while True:
        try:
            data, _ = sock.recvfrom(65535)
        except OSError:
            continue
        try:
            msg = json.loads(data.decode("utf-8", "replace"))
        except (ValueError, UnicodeError):
            continue                          # דאטהגרם לא-JSON => מתעלמים
        rec = _normalize_acars(msg)

        # בדיקת dedup: רק להודעות עם tail+text (ACK ריקים אינם מוחזרים)
        tail, label, text = rec.get("tail"), rec.get("label"), rec.get("text") or ""
        ts = rec.get("t") or time.time()
        if tail and text:
            dedup_key = (tail, label, text[:80])
            prev_ts, prev_rec = _dedup.get(dedup_key, (0, None))
            if prev_rec is not None and ts - prev_ts < 90:
                # prev_rec חי גם ב-_acars_msgs שקוראים ממנו routes => מוטציה רק תחת הנעילה
                with _acars_lock:
                    prev_rec["retry_count"] = prev_rec.get("retry_count", 1) + 1
                continue                      # retry — לא מוסיפים כרטיס חדש
            _dedup[dedup_key] = (ts, rec)
            if len(_dedup) > 500:             # ניקוי ערכים ישנים (מניעת דליפת זיכרון)
                cutoff = ts - 90
                for k in [k for k, (t, _) in _dedup.items() if t < cutoff]:
                    del _dedup[k]

        _append_acars_log(rec)                # התמדה לפני הקצאת id הזמני (הקובץ נקי מ-id)
        with _acars_lock:
            _acars_seq += 1
            rec["id"] = _acars_seq
            _acars_msgs.append(rec)
        seen += 1
        if seen % 200 == 0:                   # קיצוץ תקופתי (הכותב היחיד)
            _trim_acars_log()


def _is_active(service):
    """is-active הוא קריאת-קריאה => לא דורש sudo (עובד לכל משתמש)."""
    try:
        r = subprocess.run(["systemctl", "is-active", service],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() == "active"
    except Exception:
        return False


def _sysctl(action, service, timeout=45):
    """systemctl פעולה משנת-מצב => דרך SUDO (sudoers ממוקד מתיר בדיוק את
    הפעולות האלה ל-airam: restart/stop rtl_airband, restart/stop airam-acars)."""
    return subprocess.run([*SUDO, "systemctl", action, service],
                          capture_output=True, text=True, timeout=timeout)


def _sanitize_freqs(freqs):
    """מסנן רשימת תדרי ACARS לערכים תקינים (MHz). נכתבים ל-env => חובה לוודא
    שאין הזרקה: רק ספרות ונקודה (אף ש-systemd מנתח בבטחה, שמירה על קלט נקי)."""
    out = [str(f).strip() for f in (freqs or []) if _FREQ_RE.match(str(f).strip())]
    return out or list(ACARS_FREQS_DEFAULT)


def _acars_window_error(freqs):
    """בודק שרשימת תדרי ACARS חוקית לחלון דגימה *אחד* של acarsdec: עד
    ACARS_MAX_CHANNELS ערוצים, וכולם בתוך span של ACARS_WINDOW_MHZ (חלון ~2MHz).
    מחזיר הודעת שגיאה (str) או None אם תקין. טהורה => נבדקת בלי חומרה."""
    vals = []
    for f in freqs or []:
        try:
            vals.append(float(f))
        except (TypeError, ValueError):
            continue
    if not vals:
        return "לא נבחרו תדרי ACARS תקינים"
    if len(vals) > ACARS_MAX_CHANNELS:
        return "acarsdec תומך עד %d ערוצים (נבחרו %d)" % (ACARS_MAX_CHANNELS, len(vals))
    span = max(vals) - min(vals)
    if span > ACARS_WINDOW_MHZ + 1e-9:
        return ("התדרים מרוחקים מדי לחלון דגימה אחד (טווח %.3fMHz, מקסימום %sMHz) — "
                "בחר בנק תדרים אחר" % (span, ACARS_WINDOW_MHZ))
    return None


def write_acars_env(freqs, gain=ACARS_GAIN_DEFAULT, ratemult=ACARS_RATEMULT_DEFAULT):
    """כותב /etc/airam/acars.env בפורמט EnvironmentFile של systemd. הערך של
    ACARS_FREQS *לא* מצוטט: systemd לוקח את שארית השורה (כולל רווחים) כערך,
    וב-ExecStart ‎$ACARS_FREQS (ללא סוגריים) מתפצל בחזרה למספר ארגומנטים."""
    text = "\n".join([
        "# נכתב אוטומטית ע\"י AIR-AM web tuner (מצב ACARS). שינויים ידניים נדרסים.",
        f"ACARS_FREQS={' '.join(_sanitize_freqs(freqs))}",
        f"ACARS_GAIN={int(gain)}",
        f"ACARS_RATEMULT={int(ratemult)}",
        f"ACARS_UDP={ACARS_UDP_HOST}:{ACARS_UDP_PORT}",
        "",
    ])
    _atomic_write(ACARS_ENV_PATH, text)


def _enter_acars(freqs):
    """עוצר את rtl_airband ומריץ acarsdec. מחזיר (error, detail).
    Conflicts ב-unit עוצר ממילא את rtl_airband, אבל עוצרים מפורשות תחילה כדי
    לשחרר את ה-SDR לפני ש-acarsdec פותח אותו (מונע מרוץ על המכשיר)."""
    try:
        _sysctl("stop", "rtl_airband", timeout=30)
    except Exception:
        pass
    write_acars_env(freqs)
    try:
        r = _sysctl("restart", ACARS_SERVICE, timeout=45)
    except subprocess.TimeoutExpired:
        return "הפעלת ACARS נתקעה — בדוק שה-SDR מחובר", None
    if r.returncode != 0:
        return (r.stderr or "acarsdec failed").strip(), _journal_tail(ACARS_SERVICE)
    # כמו ב-rtl_airband: השירות יכול לעלות ואז לקרוס => פולינג ולא בדיקה בודדת
    for _ in range(7):
        time.sleep(0.5)
        if not _is_active(ACARS_SERVICE):
            return "acarsdec נכשל לעלות — בדוק journalctl -u airam-acars", _journal_tail(ACARS_SERVICE)
    return None, None


def _enter_standby():
    """מצב כיבוי (standby): עוצר את *שני* צרכני ה-SDR (rtl_airband + acarsdec) =>
    משחרר את ה-RSP1B ליישום SDR אחר, בעוד airam-web/הדף נשארים פעילים. את
    sdrplay.service משאירים חי בכוונה: ה-API daemon הוא המתווך שמאפשר לאפליקציית
    SDRplay אחרת להתחבר מיד — וגם ה-sudoers ממילא אינו מתיר לעצור אותו.
    מחזיר (error, detail). serialized תחת TUNE_LOCK ע"י הקורא."""
    for svc in (ACARS_SERVICE, "rtl_airband"):
        try:
            _sysctl("stop", svc, timeout=30)
        except Exception:
            pass
    for _ in range(7):
        time.sleep(0.3)
        if not _is_active(ACARS_SERVICE) and not _is_active("rtl_airband"):
            return None, None
    return "כיבוי המקלט נכשל — שירות עדיין פעיל", _journal_tail("rtl_airband")


# --- נתיבים ----------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/live.m3u")
def live_playlist():
    """Playlist המצביע על סטרים ה-Icecast. פתיחה בנגן שמע חיצוני (VLC וכו')
    מנגנת ברקע בצורה חסינה, ללא תלות בדפדפן."""
    host = request.host.split(":", 1)[0]          # רק ה-hostname, בלי פורט ה-web
    url = f"http://{host}:{ICECAST_PORT}/{MOUNT}"
    body = "#EXTM3U\n#EXTINF:-1,AIR-AM live\n" + url + "\n"
    return app.response_class(body, mimetype="audio/x-mpegurl")


@app.route("/stream")
def stream_proxy():
    """Reverse-proxy לסטרים ה-Icecast, same-origin => עובד גם בדף HTTPS בלי
    mixed-content. נחוץ כשהדף מוגש ב-HTTPS (למשל מאחורי 'tailscale serve'):
    סטרים HTTP ישיר מ-Icecast היה נחסם. ב-HTTP/LAN הנגן ניגש ל-Icecast ישירות."""
    upstream = f"http://127.0.0.1:{ICECAST_PORT}/{MOUNT}"
    try:
        up = urllib.request.urlopen(upstream, timeout=10)   # noqa: S310 (לוקאלהוסט בלבד)
    except Exception:
        abort(502)

    def gen():
        try:
            while True:
                chunk = up.read(8192)
                if not chunk:
                    break
                yield chunk
        finally:
            up.close()

    resp = app.response_class(gen(), mimetype="audio/mpeg")
    resp.headers["Cache-Control"] = "no-store"
    resp.direct_passthrough = True   # בלי באפורינג של Werkzeug => latency נמוך
    return resp


# נכסי PWA המוגשים מהשורש (לא מ-/static): ה-service worker *חייב* להיות מהשורש
# כדי שה-scope שלו יכסה את כל האתר, וה-manifest/אייקונים נוחים בשורש לצדו.
_ROOT_ASSETS = {
    "manifest.webmanifest": "application/manifest+json",
    "sw.js": "text/javascript",
    "icon-192.png": "image/png",
    "icon-512.png": "image/png",
    "apple-touch-icon.png": "image/png",
}


@app.route("/<path:fname>")
def root_asset(fname):
    mimetype = _ROOT_ASSETS.get(fname)
    if mimetype is None:
        abort(404)
    resp = send_from_directory(app.static_folder, fname, mimetype=mimetype)
    if fname == "sw.js":
        resp.headers["Service-Worker-Allowed"] = "/"   # scope לכל האתר
        resp.headers["Cache-Control"] = "no-cache"      # עדכון UI נקלט מיד
    return resp


@app.route("/api/state")
def api_state():
    st = load_state()
    # מקור-אמת למצב = המציאות (השירות), לא רק ה-state השמור: אחרי reboot רק
    # rtl_airband עולה (acars לא enabled), אז state ישן "acars" => מתוקן ל-voice.
    # תלת-מצבי: acars פעיל => acars; rtl_airband פעיל => voice; שניהם כבויים *ו*-state
    # מסומן off => standby מכוון (לא שורד reboot: rtl_airband enabled וחוזר לעלות).
    if _is_active(ACARS_SERVICE):
        st["app_mode"] = "acars"
    elif _is_active("rtl_airband"):
        st["app_mode"] = "voice"
    elif st.get("app_mode") == "off":
        st["app_mode"] = "off"
    else:
        st["app_mode"] = "voice"          # בזמן עליית שירותים / מצב לא ידוע => ברירת מחדל
    st.update(presets=load_presets(), mount=MOUNT, port=ICECAST_PORT, version=VERSION,
              acars_banks=ACARS_BANKS)
    return jsonify(st)


@app.route("/api/presets", methods=["GET", "PUT"])
def api_presets():
    """PUT מחליף את הרשימה כולה - העריכה בממשק היא על הסט המלא, אין צורך ב-CRUD."""
    if request.method == "GET":
        return jsonify(ok=True, presets=load_presets())
    data = request.get_json(silent=True)
    ok, cleaned = _validate_presets(data)
    if not ok:
        return jsonify(ok=False, error="רשימת פריסטים לא תקינה", presets=load_presets()), 400
    _atomic_write(PRESETS_PATH, json.dumps(cleaned, ensure_ascii=False))
    log.info("presets updated (%d items, from %s)", len(cleaned), request.remote_addr)
    return jsonify(ok=True, presets=cleaned)


@app.route("/api/health")
def api_health():
    """סטטוס המערכת — מאפשר ל-UI להבדיל בין "אין שידור" ל"משהו נפל"."""
    services = {}
    for svc in ("rtl_airband", "icecast2", "sdrplay", "airam-acars"):
        try:
            r = subprocess.run(["systemctl", "is-active", svc],
                               capture_output=True, text=True, timeout=5)
            services[svc] = (r.stdout.strip() or "unknown")
        except Exception:
            services[svc] = "unknown"
    try:
        stats_age = round(time.time() - STATS_PATH.stat().st_mtime, 1)
    except OSError:
        stats_age = None     # עוד לא נכתב (rtl_airband לא עלה / זה עתה הופעל)
    # תקין בשני המצבים: קול (rtl_airband+icecast) או ACARS (airam-acars) — אחרת
    # מצב ACARS (שבו rtl_airband מכוון מבחירה) היה נראה כתקלה.
    voice_ok = services["rtl_airband"] == "active" and services["icecast2"] == "active"
    acars_ok = services["airam-acars"] == "active"
    # standby מכוון: שני הצרכנים כבויים ו-state מסומן off => תקין, *לא* תקלה (אחרת
    # מצב הכיבוי שביקש המשתמש היה נראה כקריסה). sdrplay נשאר active במפה.
    off_ok = (load_state().get("app_mode") == "off"
              and services["rtl_airband"] != "active"
              and services["airam-acars"] != "active")
    mode = "acars" if acars_ok else "voice" if voice_ok else "off" if off_ok else "voice"
    return jsonify(ok=(voice_ok or acars_ok or off_ok), app_mode=mode,
                   services=services, sdr_present=_sdr_present(), stats_age=stats_age)


# שורת מדד בקובץ ה-stats של rtl_airband (פורמט Prometheus):
#   channel_dbfs_signal_level{freq="132.500"}	-42.3
# ה-label freq מאותר בתוך הסוגריים בנפרד => עמיד לשינוי סדר/הוספת labels ב-upstream.
_METRIC_RE = re.compile(r'^(\w+)\{([^}]*)\}\s+(-?[0-9.]+)')
_FREQ_LABEL_RE = re.compile(r'(?:^|[,{\s])freq="([0-9.]+)"')


def parse_stats(text, want_freq):
    """מחלץ {metric: value} לשורות שה-label freq שלהן תואם (MHz בפורמט 3 ספרות)."""
    vals = {}
    for line in text.splitlines():
        m = _METRIC_RE.match(line)
        if not m:
            continue
        fl = _FREQ_LABEL_RE.search(m.group(2))
        if fl and fl.group(1) == want_freq:
            vals[m.group(1)] = float(m.group(3))
    return vals


# --- יומן שידורים והקלטות ---------------------------------------------------
_REC_NAME_RE = re.compile(rf"^{re.escape(REC_BASENAME)}_\d{{8}}_\d{{6}}_(\d+)\.mp3$")


def _rec_freq_mhz(name):
    """airam_20260611_203455_134600000.mp3 => 134.600 (MHz). אחר => None."""
    m = _REC_NAME_RE.match(name)
    return round(int(m.group(1)) / 1e6, 3) if m else None


def _append_activity(rows):
    """append + קיצוץ. הקובץ מוגבל (מאות שורות) => קריאה מלאה זולה, וכתיבה
    אטומית כדי ש-/api/activity לא יקרא קובץ חצי-כתוב."""
    try:
        lines = ACTIVITY_PATH.read_text().splitlines()
    except OSError:
        lines = []
    lines += [json.dumps(r, ensure_ascii=False) for r in rows]
    if len(lines) > ACTIVITY_KEEP * 2:   # קיצוץ בהיסטרזיס - לא משכתבים בכל append
        lines = lines[-ACTIVITY_KEEP:]
    _atomic_write(ACTIVITY_PATH, "\n".join(lines) + "\n")


def _last_logged_ts():
    """ה-ts האחרון ביומן - ממנו ממשיכים אחרי restart (בלי לרשום כפולים)."""
    try:
        for ln in reversed(ACTIVITY_PATH.read_text().splitlines()):
            try:
                return float(json.loads(ln)["ts"])
            except (ValueError, KeyError, TypeError):
                continue
    except OSError:
        pass
    return 0.0


def _transcript_path(mp3):
    """קובץ-צד התמלול לצד ההקלטה: airam_....mp3 => airam_....mp3.txt."""
    return mp3.parent / (mp3.name + ".txt")


def _transcribe_file(mp3):
    """ממיר MP3 ל-WAV 16kHz מונו (ffmpeg) ומריץ whisper.cpp. מחזיר טקסט או None.
    כל כשל (ffmpeg/whisper/timeout) מטופל בשקט => לולאת הרקע ממשיכה."""
    wav = mp3.parent / (mp3.name + ".wav.tmp")
    try:
        subprocess.run(["ffmpeg", "-nostdin", "-y", "-i", str(mp3),
                        "-ar", "16000", "-ac", "1", str(wav)],
                       capture_output=True, timeout=TRANSCRIBE_TIMEOUT, check=True)
        out = subprocess.run([WHISPER_BIN, "-m", WHISPER_MODEL, "-f", str(wav),
                              "-l", WHISPER_LANG, "-nt", "--prompt", WHISPER_PROMPT],
                             capture_output=True, text=True,
                             timeout=TRANSCRIBE_TIMEOUT, check=True)
        return " ".join(out.stdout.split()).strip() or None
    except Exception:
        log.exception("transcribe %s", mp3.name)
        return None
    finally:
        try:
            wav.unlink()
        except OSError:
            pass


def _transcribe_worker():
    """לולאת רקע: מתמלל הקלטות שעוד אין להן קובץ-צד .txt (חדש=>ישן).
    כותב גם תמלול ריק => לא מנסים שוב את אותו קובץ בלולאה הבאה."""
    if not (Path(WHISPER_BIN).exists() and Path(WHISPER_MODEL).exists()):
        log.warning("transcription on, but whisper missing (%s / %s) - מדלג",
                    WHISPER_BIN, WHISPER_MODEL)
        return
    log.info("transcription worker started (model=%s)", WHISPER_MODEL)
    while True:
        try:
            recs = sorted(REC_DIR.glob("*.mp3"),
                          key=lambda p: p.stat().st_mtime, reverse=True)
            for mp3 in recs:
                txt = _transcript_path(mp3)
                if txt.exists():
                    continue
                _atomic_write(txt, (_transcribe_file(mp3) or "") + "\n")
        except Exception:
            log.exception("transcribe worker")
        time.sleep(WATCH_INTERVAL)


def _sweep_recordings():
    """retention: עד REC_MAX_FILES / REC_MAX_BYTES (חדש=>ישן), ו-.tmp נטושים
    (שידור שנקטע בקריסה משאיר .tmp שלעולם לא ייסגר ל-mp3). קובץ-צד התמלול
    (.txt) נמחק יחד עם ההקלטה שלו."""
    try:
        recs = sorted(REC_DIR.glob("*.mp3"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return
    total = 0
    for i, p in enumerate(recs):
        try:
            total += p.stat().st_size
            if i >= REC_MAX_FILES or total > REC_MAX_BYTES:
                p.unlink()
                _transcript_path(p).unlink(missing_ok=True)
        except OSError:
            pass
    now = time.time()
    for p in REC_DIR.glob("*.tmp"):
        try:
            if now - p.stat().st_mtime > 3600:
                p.unlink()
        except OSError:
            pass


def _scan_new_recordings(last_seen):
    """(rows, newest) - הקלטות שה-mtime שלהן מאוחר מ-last_seen, חדש=>ישן לפי mtime.
    ה-ts מעוגל *לפני* ההשוואה - אותו עיגול שנכתב ליומן (ושחוזר מ-_last_logged_ts)
    => סריקה חוזרת אחרי restart לא תייצר שורות כפולות."""
    rows, newest = [], last_seen
    try:
        recs = sorted(REC_DIR.glob("*.mp3"), key=lambda p: p.stat().st_mtime)
    except OSError:
        recs = []
    for p in recs:
        try:
            stat = p.stat()
        except OSError:
            continue   # נמחק בינתיים (retention) => מדלגים
        ts = round(stat.st_mtime, 1)
        if ts > last_seen:
            rows.append({"ts": ts, "freq": _rec_freq_mhz(p.name), "file": p.name,
                         "dur": round(stat.st_size / REC_BYTES_PER_SEC, 1)})
            newest = max(newest, ts)
    return rows, newest


def _activity_watcher():
    """לולאת רקע: הקלטה חדשה שהסתיימה => שורה ביומן; ואז retention.
    בעלייה ממשיכים מה-ts האחרון שנרשם => הקלטות מהזמן שהשרת היה כבוי נקלטות."""
    last_seen = _last_logged_ts()
    while True:
        try:
            rows, newest = _scan_new_recordings(last_seen)
            if rows:
                _append_activity(rows)
                last_seen = newest   # מקדמים רק אחרי כתיבה מוצלחת => כישלון append לא מאבד אירועים
            _sweep_recordings()
        except Exception:
            log.exception("activity watcher")
        time.sleep(WATCH_INTERVAL)


@app.route("/api/activity")
def api_activity():
    """אירועי השידור האחרונים, חדש=>ישן. exists=False כשההקלטה כבר נמחקה ב-retention."""
    try:
        lines = ACTIVITY_PATH.read_text().splitlines()
    except OSError:
        lines = []
    events = []
    for ln in reversed(lines):
        if len(events) >= ACTIVITY_RETURN:
            break
        try:
            ev = json.loads(ln)
        except ValueError:
            continue
        ev["exists"] = bool(ev.get("file")) and (REC_DIR / ev["file"]).is_file()
        ev["text"] = None
        if ev.get("file"):
            try:
                ev["text"] = (REC_DIR / (ev["file"] + ".txt")).read_text().strip() or None
            except OSError:
                pass   # אין תמלול (כבוי, עדיין מעובד, או נמחק) => None
        events.append(ev)
    return jsonify(ok=True, events=events)


@app.route("/recordings/<name>")
def recordings(name):
    # send_from_directory חוסם path traversal; ‏<name> (לא <path:>) חוסם תתי-תיקיות
    return send_from_directory(str(REC_DIR), name)


# --- METAR נתב"ג --------------------------------------------------------------
METAR_URL = "https://aviationweather.gov/api/data/metar?ids=LLBG"
METAR_TTL = 300.0              # ה-METAR מתעדכן ~כל חצי שעה; 5 דקות cache מנומס
_metar = {"checked": 0.0, "fetched": 0.0, "text": None}
_METAR_LOCK = threading.Lock()


@app.route("/api/metar")
def api_metar():
    """METAR גולמי של LLBG. כשל (אין אינטרנט) => מחזירים את האחרון שיש + גילו,
    וה-UI מחליט; אין retry לפני שעבר ה-TTL כדי לא להציק ל-API הציבורי."""
    now = time.time()
    # תופסים את ה-slot מתחת לנעילה (רק thread אחד מביא), אבל מבצעים את ה-fetch
    # *מחוץ* לנעילה => בקשות /api/metar מקבילות לא נחסמות 5 שניות על ה-HTTP.
    with _METAR_LOCK:
        do_fetch = now - _metar["checked"] > METAR_TTL
        if do_fetch:
            _metar["checked"] = now
    if do_fetch:
        try:
            req = urllib.request.Request(METAR_URL, headers={"User-Agent": "AIR-AM tuner"})
            with urllib.request.urlopen(req, timeout=5) as r:
                text = r.read().decode("utf-8", "replace").strip()
            if text:
                with _METAR_LOCK:
                    _metar.update(fetched=now, text=text)
        except Exception:
            pass   # שומרים את הישן; age בתשובה חושף שהוא לא טרי
    with _METAR_LOCK:
        text = _metar["text"]
        age = round(now - _metar["fetched"], 1) if text else None
    return jsonify(ok=True, metar=text, age=age)


@app.route("/api/metrics")
def api_metrics():
    """מדדי RF חיים לתדר הנוכחי. rtl_airband מרענן את הקובץ כל ~1 שנייה."""
    try:
        age = time.time() - STATS_PATH.stat().st_mtime
        text = STATS_PATH.read_text()
    except OSError:
        return jsonify(ok=True, fresh=False)   # עוד לא נכתב (אחרי restart/אתחול)

    want = f"{load_state()['freq']:.3f}"       # מדדים מתויגים freq=MHz ב-3 ספרות
    vals = parse_stats(text, want)

    sig = vals.get("channel_dbfs_signal_level")
    noise = vals.get("channel_dbfs_noise_level")
    snr = round(sig - noise, 1) if (sig is not None and noise is not None) else None
    # עומס יתר: רמת האות בערוץ מתקרבת ל-full scale (0 dBFS) => ה-ADC/רווח רווי.
    overload = sig is not None and sig >= OVERLOAD_DBFS
    return jsonify(ok=True, fresh=(age <= STATS_MAX_AGE and snr is not None),
                   age=round(age, 1), signal=sig, noise=noise, snr=snr,
                   overload=overload, overload_dbfs=OVERLOAD_DBFS,
                   squelch_opens=vals.get("channel_squelch_counter"))


@app.route("/api/airspace")
def api_airspace():
    """מסלול נחיתות/המראות פעיל ומצב GPS, מנותחים מ-ADS-B (ראה adsb.py).
    קורא snapshot בזיכרון בלבד - אף פעם לא חוסם ואף פעם לא 500."""
    return jsonify(adsb.snapshot())


def _vcgencmd(*args):
    """מריץ vcgencmd ומחזיר stdout (או None אם לא Pi / לא מותקן / נכשל)."""
    try:
        r = subprocess.run(["vcgencmd", *args], capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


@app.route("/api/power")
def api_power():
    """מצב אספקת המתח ל-Pi (שימושי במיוחד עם סוללה ניידת):
      get_throttled  -> דגלי undervoltage/throttling (כל דגמי Pi)
      pmic_read_adc  -> מתח כניסה 5V בפועל (Pi 5 בלבד)
      measure_temp   -> טמפ' ליבה
    ביטים של get_throttled: 0=under-volt עכשיו · 2=throttled עכשיו ·
    16=under-volt קרה מאז אתחול · 18=throttling קרה."""
    out = _vcgencmd("get_throttled")
    if out is None:
        return jsonify(ok=False)   # אין vcgencmd (לא Pi / חסר) => הממשק מסתיר את החיווי

    flags = 0
    m = re.search(r"0x([0-9a-fA-F]+)", out)
    if m:
        flags = int(m.group(1), 16)

    volts_in = None
    adc = _vcgencmd("pmic_read_adc")          # Pi 5 בלבד
    if adc:
        mv = re.search(r"EXT5V_V\s+volt\([^)]*\)=([0-9.]+)", adc)
        if mv:
            volts_in = round(float(mv.group(1)), 2)

    temp = None
    mt = re.search(r"=([0-9.]+)", _vcgencmd("measure_temp") or "")
    if mt:
        temp = round(float(mt.group(1)), 1)

    return jsonify(ok=True, throttled=hex(flags),
                   undervolt_now=bool(flags & 0x1),
                   throttle_now=bool(flags & 0x4),
                   undervolt_ever=bool(flags & 0x10000),
                   throttle_ever=bool(flags & 0x40000),
                   volts_in=volts_in, temp=temp)


def _parse_tune(data):
    """מנקה/מאמת פרמטרי כיוונון קולי. מחזיר (params, error). תדר נכתב כ-float
    מפורמט => ללא סיכון הזרקה."""
    try:
        freq = float(data.get("freq"))
    except (TypeError, ValueError):
        return None, "תדר לא תקין"
    if not (0.1 <= freq <= 1999.5):   # מרווח עבור DC_OFFSET (centerfreq <= 2000)
        return None, "תדר מחוץ לטווח (0.1–1999.5 MHz)"

    mod = "nfm" if str(data.get("mod", "am")).lower() == "nfm" else "am"
    agc_raw = data.get("agc", True)   # עמיד גם ל-"false" טקסטואלי (curl), לא רק bool
    agc = agc_raw if isinstance(agc_raw, bool) else str(agc_raw).lower() not in ("false", "0", "off", "no")
    try:
        if_gain = max(IFGR_MIN, min(IFGR_MAX, int(data.get("if_gain", IF_GAIN_DEFAULT))))
    except (TypeError, ValueError):
        if_gain = IF_GAIN_DEFAULT
    try:
        rf_gain = max(RFGR_MIN, min(RFGR_MAX, int(data.get("rf_gain", RF_GAIN_DEFAULT))))
    except (TypeError, ValueError):
        rf_gain = RF_GAIN_DEFAULT

    squelch_mode = str(data.get("squelch_mode", "auto")).lower()
    if squelch_mode not in SQUELCH_MODES:
        squelch_mode = "auto"
    try:
        squelch_snr = float(data.get("squelch_snr", SNR_DEFAULT))
    except (TypeError, ValueError):
        squelch_snr = SNR_DEFAULT
    squelch_snr = max(SNR_MIN, min(SNR_MAX, squelch_snr))

    return {"freq": freq, "mod": mod, "agc": agc, "if_gain": if_gain, "rf_gain": rf_gain,
            "squelch_mode": squelch_mode, "squelch_snr": squelch_snr}, None


def _voice_tune(params):
    """מכוונן קול (rtl_airband). מבטיח יציאה ממצב ACARS תחילה (משחרר את ה-SDR).
    מחזיר (payload, http_status). serialized תחת TUNE_LOCK."""
    if not TUNE_LOCK.acquire(blocking=False):
        # state בתשובה => ה-UI מיישר את התצוגה האופטימית חזרה למציאות
        return {"ok": False, "error": "כיוונון אחר מתבצע כרגע — המתן שנייה ונסה שוב",
                "state": load_state()}, 409
    try:
        prev = load_state()   # ההגדרות האחרונות שעבדו, לרולבק במקרה כישלון
        # מצב משולב: אם acarsdec רץ הוא מחזיק את ה-SDR => עוצרים מפורשות לפני
        # שמרימים את rtl_airband (Conflicts גיבוי, אבל זה משחרר את המכשיר מיד).
        if _is_active(ACARS_SERVICE):
            try:
                _sysctl("stop", ACARS_SERVICE, timeout=30)
            except Exception:
                pass
        new_state = {**params, "app_mode": "voice",
                     "acars_freqs": prev.get("acars_freqs", ACARS_FREQS_DEFAULT)}
        log.info("tune %.3f MHz mod=%s agc=%s if_gain=%d rf_gain=%d squelch=%s snr=%.1f (from %s)",
                 params["freq"], params["mod"], params["agc"], params["if_gain"],
                 params["rf_gain"], params["squelch_mode"], params["squelch_snr"], request.remote_addr)
        write_config(params["freq"], params["mod"], params["agc"], params["if_gain"],
                     params["rf_gain"], params["squelch_mode"], params["squelch_snr"])

        err, detail, sdr_down = _restart_and_verify()
        if err:
            log.warning("tune %.3f MHz failed: %s (sdr_down=%s)", params["freq"], err, sdr_down)
            if sdr_down:
                # ה-SDR מנותק: רולבק ייתקע באותה המתנה בדיוק, אז מדלגים עליו.
                # הקונפיג החדש נשאר על הדיסק וייקלט כשהמכשיר יחובר (udev מרים
                # את השירותים) => שומרים state תואם לדיסק, לא את הקודם.
                save_state(new_state)
                return {"ok": False, "detail": detail, "state": new_state,
                        "error": err + " — התדר יוחל אוטומטית כשה-SDR יחובר"}, 500
            _rollback(prev)   # config רע => לא משאירים את השירות בלולאת קריסה
            return {"ok": False, "error": err + " (חזרתי לתדר הקודם)",
                    "detail": detail, "state": {**prev, "app_mode": "voice"}}, 500

        # נשמר רק אחרי שאומת שהשירות חי => state תמיד משקף הגדרות שעובדות
        save_state(new_state)
        return {"ok": True, **new_state}, 200
    finally:
        TUNE_LOCK.release()


@app.route("/api/tune", methods=["POST"])
def api_tune():
    # בלי force=True: מחייב Content-Type: application/json => דפדפן זר (CSRF) לא
    # יכול לשלוח טופס text/plain שמכוונן את הרדיו (כמו ב-/api/presets).
    data = request.get_json(silent=True) or {}
    params, err = _parse_tune(data)
    if err:
        return jsonify(ok=False, error=err), 400
    payload, status = _voice_tune(params)
    return jsonify(payload), status


def _acars_adsb():
    """העשרת ADS-B לזנבות שבזיכרון ה-ACARS (היתוך לפי רישום מנורמל). קריאת
    snapshot בזיכרון בלבד — אין רשת בנתיב הבקשה, אין אינטרנט => dict ריק."""
    with _acars_lock:
        regs = {adsb.norm_reg(m.get("tail")) for m in _acars_msgs if m.get("tail")}
    regs.discard(None)
    return adsb.aircraft_snapshot(regs) if regs else {}


@app.route("/api/acars")
def api_acars():
    """הודעות ACARS אחרונות. ?since=<id> => רק חדשות מאותו cursor (פולינג יעיל).
    כברירת מחדל מוחזרות רק הודעות *היום* (שעון ה-Pi) => סשן חדש לא מוצף בתעבורת
    ימים קודמים. ?all=1 => כל מה שבזיכרון; ההיסטוריה המלאה תמיד זמינה בייצוא."""
    try:
        since = int(request.args.get("since", 0))
    except (TypeError, ValueError):
        since = 0
    show_all = request.args.get("all") in ("1", "true", "yes")
    floor = 0 if show_all else _today_start()
    with _acars_lock:
        # עותקים (לא references): jsonify מסדרל אחרי שחרור הנעילה, ו-retry_count
        # עלול להתעדכן ע"י ה-listener באמצע האיטרציה של ה-encoder
        msgs = [dict(m) for m in _acars_msgs
                if m["id"] > since and (m.get("t") or 0) >= floor]
        cursor = _acars_seq
    return jsonify(ok=True, active=_is_active(ACARS_SERVICE),
                   freqs=load_state().get("acars_freqs", ACARS_FREQS_DEFAULT),
                   cursor=cursor, messages=msgs, adsb=_acars_adsb())


ACARS_EXPORT_COLS = ["time_iso", "timestamp", "freq", "level", "mode", "label",
                     "category", "group", "dir", "tail", "flight", "actype", "msgno", "error",
                     "lat", "lon", "pos_src", "text"]


def _read_acars_log():
    """כל ההודעות מ-acars.jsonl, ממוינות לפי זמן (t עולה). סובל שורות פגומות
    (כתיבה חלקית של ההודעה האחרונה בזמן הקריאה)."""
    try:
        lines = ACARS_LOG_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    out.sort(key=lambda r: r.get("t") or 0)
    return out


@app.route("/api/acars/export")
def api_acars_export():
    """ייצוא כל הודעות ה-ACARS השמורות לקובץ מסודר (לניתוח offline).
    ?format=csv (ברירת מחדל) | json. GET => בלי PIN (כמו שאר ה-GET)."""
    fmt = (request.args.get("format") or "csv").lower()
    recs = _read_acars_log()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    if fmt == "json":
        resp = app.response_class(json.dumps(recs, ensure_ascii=False, indent=1),
                                  mimetype="application/json")
        fname = f"airam-acars-{stamp}.json"
    else:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(ACARS_EXPORT_COLS)
        for r in recs:
            t = r.get("t")
            iso = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else ""
            txt = (r.get("text") or "").replace("\r", " ").replace("\n", " ")
            w.writerow([iso, t, r.get("freq"), r.get("level"), r.get("mode"),
                        r.get("label"), r.get("category"), r.get("group"), r.get("dir"),
                        r.get("tail"), r.get("flight"), r.get("actype"), r.get("msgno"), r.get("error"),
                        r.get("lat"), r.get("lon"), r.get("pos_src"), txt])
        # BOM => Excel מזהה UTF-8 ומציג עברית (category) נכון
        resp = app.response_class("﻿" + buf.getvalue(),
                                  mimetype="text/csv; charset=utf-8")
        fname = f"airam-acars-{stamp}.csv"
    resp.headers["Content-Disposition"] = f'attachment; filename="{fname}"'
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/mode", methods=["POST"])
def api_mode():
    """מעבר בין מצב קול (rtl_airband) למצב ACARS (acarsdec). SDR אחד בהחלפה.
    POST => עובר דרך _guard (Origin + PIN אופציונלי), כמו /api/tune."""
    data = request.get_json(silent=True) or {}
    mode = str(data.get("mode", "")).lower()

    if mode == "acars":
        if not TUNE_LOCK.acquire(blocking=False):
            return jsonify(ok=False, error="פעולה אחרת מתבצעת — נסה שוב",
                           state=load_state()), 409
        try:
            st = load_state()
            freqs = _sanitize_freqs(data.get("freqs") or st.get("acars_freqs"))
            werr = _acars_window_error(freqs)        # חייב להיכנס בחלון דגימה אחד
            if werr:
                return jsonify(ok=False, error=werr, state=load_state()), 400
            log.info("mode -> ACARS freqs=%s (from %s)", freqs, request.remote_addr)
            err, detail = _enter_acars(freqs)
            if err:
                log.warning("enter ACARS failed: %s", err)
                # נכשל => מנסים לחזור לקול האחרון כדי לא להשאיר SDR בלי צרכן
                try:
                    write_config(st["freq"], st["mod"], st["agc"], st["if_gain"],
                                 st["rf_gain"], st["squelch_mode"], st["squelch_snr"])
                    _sysctl("restart", "rtl_airband", timeout=45)
                except Exception:
                    pass
                return jsonify(ok=False, error=err, detail=detail,
                               state={**st, "app_mode": "voice"}), 500
            new_state = {**st, "app_mode": "acars", "acars_freqs": freqs}
            save_state(new_state)
            return jsonify(ok=True, app_mode="acars", acars_freqs=freqs)
        finally:
            TUNE_LOCK.release()

    if mode == "off":
        # כיבוי (standby): עוצר את שני צרכני ה-SDR ומשחרר את ה-RSP1B ליישום אחר.
        # airam-web/הדף נשארים פעילים => אפשר להדליק שוב מה-UI בכל רגע.
        if not TUNE_LOCK.acquire(blocking=False):
            return jsonify(ok=False, error="פעולה אחרת מתבצעת — נסה שוב",
                           state=load_state()), 409
        try:
            log.info("mode -> OFF (standby) (from %s)", request.remote_addr)
            err, detail = _enter_standby()
            if err:
                log.warning("enter standby failed: %s", err)
                return jsonify(ok=False, error=err, detail=detail, state=load_state()), 500
            new_state = {**load_state(), "app_mode": "off"}
            save_state(new_state)
            return jsonify(ok=True, app_mode="off")
        finally:
            TUNE_LOCK.release()

    if mode == "voice":
        # חוזר לקול עם ההגדרות השמורות האחרונות (כולל התדר האחרון שהאזנו לו)
        st = load_state()
        params, perr = _parse_tune(data if "freq" in data else st)
        if perr:   # state פגום => נופלים לברירת מחדל
            params, _ = _parse_tune(DEFAULT_STATE)
        payload, status = _voice_tune(params)
        return jsonify(payload), status

    return jsonify(ok=False, error="mode לא תקין (voice/acars)"), 400


if __name__ == "__main__":
    # ודא קובץ הגדרות עדכני: חסר => כותבים; קיים בלי תכונה שהממשק מסתמך עליה
    # (שדרוג מגרסה ישנה: stats_filepath למדדי RF, localtime להקלטות) =>
    # משכתבים ומרימים את rtl_airband פעם אחת כדי שהתכונות יפעלו.
    try:
        _cur = CONFIG_PATH.read_text()
    except OSError:
        _cur = None
    if _cur is None or "stats_filepath" not in _cur or "localtime" not in _cur:
        st = load_state()
        write_config(st["freq"], st["mod"], st["agc"], st["if_gain"],
                     st["rf_gain"], st["squelch_mode"], st["squelch_snr"])
        if _cur is not None:   # שדרוג: השירות כבר רץ עם ההגדרות הישנות
            try:
                subprocess.run([*SUDO, "systemctl", "restart", "rtl_airband"],
                               capture_output=True, timeout=60)
            except Exception:
                pass
    REC_DIR.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=_activity_watcher, daemon=True).start()
    _load_acars_history()                                           # היסטוריית ACARS שורדת restart (לפני ה-listener)
    threading.Thread(target=_acars_listener, daemon=True).start()   # פיד UDP מ-acarsdec (שקט במצב קול)
    if TRANSCRIBE:   # תמלול ATC אופציונלי - דמון נפרד (לא חוסם את היומן/retention)
        threading.Thread(target=_transcribe_worker, daemon=True).start()
    adsb.start()   # רק כשרצים כשרת (לא בזמן import) - דמון, לא מעכב עלייה
    # threaded: סטרים /stream הוא חיבור ארוך-טווח => חייב לא לחסום בקשות אחרות
    app.run(host="0.0.0.0", port=8080, threaded=True)
