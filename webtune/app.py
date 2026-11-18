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
import base64
import binascii
import collections
import csv
import gzip
import hashlib
import heapq
import hmac
import io
import itertools
import json
import logging
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, request, jsonify, send_from_directory, send_file, abort

import experiment   # רשם ניסוי RF וניסוי הכיול האוטומטי (הוצא בשלב ג2)
import diag   # יומן בדיקה וייצוא לוגים — הניתוח הטהור (פענוח journald, בדיקות, summary.md)
import acars_parse   # נרמול ופענוח ACARS/VDL2/SATCOM (הוצא בשלב ג2)
import appstate   # המצב השמור: load/save/_merge_state (הוצא בשלב ג2)
import config   # הגדרות ונתוני-ברירת-מחדל טהורים (הוצא בשלב ג2)
import recordings   # הקלטות, ★, תמלול, סשנים — קבצים בדיסק (הוצא בשלב ג2)
import storage   # אחסון: כתיבה אטומית + JSONL (הוצא מכאן בשלב ג2)
import adsb   # מסלול פעיל + אינדיקציית GPS מנתוני ADS-B (thread נפרד)
import xcheck   # הצלבת הודעות מול ADS-B + אופק רדיו (פונקציות טהורות)

# stdout => journald (השירות רץ תחת systemd); journalctl -u airam-web מציג הכל
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("airam")

# --- קבועים ---------------------------------------------------------------
CONFIG_PATH = Path("/etc/rtl_airband/airband.conf")
MOUNT = "live.mp3"          # שם ה-stream הקבוע ב-Icecast
ICECAST_PORT = 8000
SOURCE_PW = "airam"         # חייבת להיות זהה ל-SOURCE_PW ב-install.sh (נכתבת ל-Icecast שם)
# סינון רעשים חי (docs/denoise-design.md): rtl_airband שולח עותק PCM ב-UDP
# (udp_stream — *תמיד* בקונפיג, ר' render_config) => airam-denoise (מנוע נבחר)
# => mount נוסף ב-Icecast. /live.mp3, ההקלטות והתמלול לא עוברים דרכו כלל.
DENOISE_SERVICE = "airam-denoise"
DENOISE_UDP_PORT = 5559     # חייב להיות זהה ל-UDP_PORT ב-scripts/airam-denoise
DENOISE_MOUNT = "clean.mp3"  # חייב להיות זהה ל-MOUNT ב-scripts/airam-denoise + ל-<mount> ב-install.sh
DENOISE_SCRIPT = Path("/usr/local/bin/airam-denoise")
DENOISE_RNNOISE = Path("/usr/local/bin/airam-rnnoise")
DENOISE_SPEEXDN = Path("/usr/local/bin/airam-speexdn")
DENOISE_ENV_PATH = Path("/etc/airam/denoise.env")   # EnvironmentFile של airam-denoise.service
DENOISE_INSTALL_HINT = "cd ~/AIR-AM && git pull && sudo ./install.sh"
SAMPLE_RATE = 2.56          # Msps - ערוץ יחיד, חלון צר מספיק
DC_OFFSET = 0.3             # MHz - מזיזים את centerfreq מהתדר כדי להתרחק מ-spike ה-DC
OVERLOAD_DBFS = -3.0          # סף "עומס יתר": אות ערוץ קרוב ל-full scale של ה-ADC
SQUELCH_MODES = {"auto", "open", "manual"}
STATS_PATH = Path("/run/rtl_airband_stats.txt")   # tmpfs - בלי שחיקת SD
STATS_MAX_AGE = 5.0            # rtl_airband כותב כל ~1 שנייה; ~5 כתיבות => סובל ג'יטר אך עדיין מזהה restart

# --- מד שדה מאוחד + בדיקת אנטנה (ר' docs/field-station-roadmap.md) ---------
# ב-ACARS/VDL2 אין מד אות רציף כמו ב-קול (rtl_airband): acarsdec/dumpvdl2 לא
# חושפים רצפת רעש כששקט, רק בתוך הודעה מפוענחת. "בדיקת אנטנה" עוקפת את זה
# ע"י מעבר זמני לקול (AGC, סקוולץ' פתוח) על התדר המבוקש ומדידה אמיתית.
ANTENNA_CHECK_SAMPLE_SEC = 3.0   # פולינג עד שדגימה טרייה לתדר הזה מופיעה ב-stats
SIGNAL_LAST_MSG_MAX_AGE = 300.0  # מעל זה "הודעה אחרונה" מסומנת לא-טרייה (5 דק') — לא נעלמת, רק מסומנת
# ירידה (dB) ברצפת הרעש מתחת לבסיס שהמשתמש כייל, שנחשבת חריגה. *לא* סף
# "איכות אות" מומצא (§12 ב-CLAUDE.md אוסר את זה) — פסק הדין תמיד מול הבסיס
# של המשתמש עצמו, לעולם לא מול ערך מוחלט שניחשנו.
# ⚠ **הנחה שטרם אומתה, לא "תצפית פיזיקלית"** (כך היא תועדה קודם — בלי מדידה
# אחת מאחוריה). ניתוח מהמקור (rtl_airband v5.2.0 + SoapySDRPlay3, ר'
# docs/antenna-calibration-experiment.md) מראה שהיא תלוית-תנאים: ב-VHF רעש-
# הסביבה אינו "גבוה בהרבה" מרעש-הפנים בשטח שקט (ירידה צפויה 3–8dB גם ברווח
# קבוע), ותחת AGC — כשיש נשא חזק בחלון ה-1.536MHz (ATIS של נתב"ג) — ה-AGC
# מעלה רווח אחרי הניתוק ומוחק חלק/את כל הירידה. הניסוי המתועד שם (עם רשם
# ה-RF למטה) יכריע; עד אז הקבוע נשאר כפי שהוא ולא "מתוקן" לפי ניחוש.


# --- ACARS (מצב משולב: SDR אחד בהחלפה) ------------------------------------
# מצב ACARS עוצר את rtl_airband (קול) ומריץ acarsdec על תדרי ה-ACARS. SDR אחד
# => רק צרכן אחד בכל רגע (Conflicts ב-unit מבטיח זאת). acarsdec שולח כל הודעה
# מפוענחת כ-JSON ב-UDP ל-listener כאן, וה-UI מושך אותן מ-/api/acars.
ACARS_SERVICE = "airam-acars"
# מצב "קול + ACARS" (docs/dual-sdr-feasibility.md §6): acarsdec על המקלט ה*משני*, לצד
# rtl_airband על הראשי. יחידה נפרדת (Conflicts מול airam-acars/vdl2/satcom — *לא* מול
# rtl_airband), env משלה, אותו פורט UDP ⇒ ה-listener, הפיד והארכיון לא משתנים.
ACARS2_SERVICE = "airam-acars2"
ACARS2_ENV_PATH = Path("/etc/airam/acars2.env")
# פתיחה מדורגת: ACARS עולה רק אחרי שהקול *סיים* לעלות. על ה-Pi (§7.8) rtl_airband צורך
# ליבה שלמה ~9ש' בעלייה, ושני צרכנים שעולים יחד הפילו power bank של 5V/3A.
VOICE_STARTUP_WAIT_SEC = 30   # עד מתי מחכים לכתיבת ה-stats הראשונה של rtl_airband
# המרווח עצמו ביחידה (ExecStartPre=/bin/sleep 10 ב-airam-acars2.service) — כך הוא חל גם כש-
# systemd מאתחל את שנינו יחד (PartOf=sdrplay), לא רק כשאנחנו מתזמנים. כאן רק מחכים ל-stats.
# מצב "קול כפול" (docs/dual-sdr-feasibility.md §8): rtl_airband *שני* על המקלט המשני — תהליך לכל
# מקלט (תהליך אחד עם שניים נכשל ב-sdrplay_api_Fail, §7.6). קונפיג, stats ו-mount משלו; ההקלטות
# לאותה תיקייה (include_freq ⇒ שמות שונים), כך שיומן השידורים/★/תמלול חלים על שני הערוצים בחינם.
VOICE2_SERVICE = "rtl_airband2"
VOICE2_CONFIG_PATH = Path("/etc/rtl_airband/airband2.conf")
STATS2_PATH = Path("/run/rtl_airband2_stats.txt")
MOUNT2 = "live2.mp3"
# מצב "קול + VDL2": dumpvdl2 על המשני, אותו פורט UDP ⇒ אותו listener/פיד/ארכיון של VDL2.
VDL2B_SERVICE = "airam-vdl2-2"
VDL2B_ENV_PATH = Path("/etc/airam/vdl2-2.env")
# מצבים מורכבים: קול על הראשי + צרכן אחד על המשני. *לא* ב-MODE_SERVICE (מצב, לא שירות) ולא scannable.
COMPOSITE_SECONDARY = {"voice_acars": ACARS2_SERVICE, "voice2": VOICE2_SERVICE,
                       "voice_vdl2": VDL2B_SERVICE}
SECONDARY_SERVICES = tuple(COMPOSITE_SECONDARY.values())
COMPOSITE_LABEL = {"voice_acars": "קול + ACARS", "voice2": "קול כפול", "voice_vdl2": "קול + VDL2"}
# מצב מורכב שהמשני שלו הוא מפענח דאטה: (מפתח התדרים ב-state, ברירת מחדל)
COMPOSITE_DATA = {"voice_acars": ("acars_freqs", None), "voice_vdl2": ("vdl2_freqs", None)}
ACARS_ENV_PATH = Path("/etc/airam/acars.env")
# מיפוי תפקידי המקלטים (docs/dual-sdr-feasibility.md): SDR_PRIMARY=<serial>. נקרא ע"י
# scripts/airam-wait-sdrplay ב-sed עם תבנית מעוגנת — *לא* source (§9).
SDR_ENV_PATH = Path("/etc/airam/sdr.env")
# serial של SDRplay: אותיות/ספרות בלבד (נצפה: 240404AF60, 230405A498). מעוגן — ערך שלא
# עובר לא נכתב לשום env/קונפיג (נכנס ל-device_string של תהליך root).
_SERIAL_RE = re.compile(r"^[0-9A-Za-z]{4,32}$")
ACARS_UDP_HOST = "127.0.0.1"
ACARS_UDP_PORT = 5556                 # חייב להתאים ל-ACARS_UDP ב-acars.env
ACARS_GAIN_DEFAULT = -10              # ‎-10 => AGC (מוסכמת acarsdec)
ACARS_RATEMULT_DEFAULT = 160          # 160 => 2.0 MS/s (חלון ±1MHz)
ACARS_MAX_CHANNELS = 8               # מגבלת acarsdec — עד 8 ערוצים בו-זמנית
ACARS_WINDOW_MHZ = 1.9               # span מרבי בחלון דגימה אחד (2.0MS/s, עם שוליים)
ACARS_BUF_MAX = 500                   # הודעות אחרונות בזיכרון (נטענות לקליינט בעלייה, היום בלבד)
# ⚠ לא רק "ולידציית איכות" — זו גבול-האבטחה היחיד נגד הזרקת ארגומנטים: ה-
# ExecStart-ים ב-systemd/*.service משתמשים במפורש ב-$ACARS_FREQS/$VDL2_FREQS
# *בלי* מרכאות (כדי שיתפצלו למספר ארגומנטים — כל תדר כארגומנט נפרד, פיצ'ר
# systemd מכוון), אז כל תו רווח בערך הזה הופך לגבול-ארגומנט חדש בתהליך root.
# ‎_FREQ_RE מעוגן (^...$), ספרות+נקודה בלבד — ערך שלא עובר לא נכתב ל-env בכלל
# (מסונן מהרשימה, לא "מנוקה"). אם אי-פעם משנים את זה, לוודא שהתבנית עדיין
# שוללת רווחים/מקפים-מובילים/מטא-תווים — אחרת airam עם גישת-כתיבה כלשהי
# ל-/etc/airam/*.env (למשל RCE בערוץ אחר לגמרי) יכול להזריק דגלים לתהליך root.
_FREQ_RE = re.compile(r"^\d{2,3}\.\d{1,3}$")   # ולידציית תדר ACARS (MHz) לפני כתיבה ל-env

# התמדה: כל הודעה מפוענחת נכתבת ל-acars.jsonl (כמו activity.jsonl) => שורדת restart.
# קורא ב-/api/acars/export ובטעינה הראשונית; thread ה-listener הוא הכותב היחיד.
ACARS_LOG_PATH = Path("/var/lib/airam/acars.jsonl")
ACARS_LOG_KEEP = 5000                 # retention על הדיסק (זנב נשמר; ייצוא לניתוח)


# --- VDL2 (מצב שלישי: SDR אחד בהחלפה) --------------------------------------
# VDL Mode 2 (D8PSK, 31.5kbps) הוא הדור הבא של דאטה-לינק: רוב התעבורה בו היא
# ACARS-over-AVLC (אותן הודעות ACARS => כל הפרסרים הקיימים חלים), והשאר ATN/X.25
# (CPDLC/ADS-C) ו-XID (ניהול קישור). dumpvdl2 שולח כל פריים מפוענח כ-JSON ב-UDP
# ל-listener כאן (כמו acarsdec), וה-UI מושך מ-/api/vdl2. CHANGELOG ‏1.10.0 קבע
# ש-CPDLC לא קיים על ACARS VHF באזורנו — הוא רץ על VDL2; המצב הזה סוגר את הפער.
VDL2_SERVICE = "airam-vdl2"
VDL2_ENV_PATH = Path("/etc/airam/vdl2.env")
VDL2_UDP_PORT = 5557                  # חייב להתאים ל-port ב-airam-vdl2.service (host: ACARS_UDP_HOST)
VDL2_MAX_CHANNELS = 8                 # תקרה שפויה (dumpvdl2 מוגבל CPU, לא ערוצים)
VDL2_WINDOW_MHZ = 1.9                 # SoapySDR של dumpvdl2 דוגם 2.1MS/s => ~2MHz עם שוליים
VDL2_BUF_MAX = 500                    # הודעות אחרונות בזיכרון (כמו ACARS)
VDL2_LOG_PATH = Path("/var/lib/airam/vdl2.jsonl")
VDL2_LOG_KEEP = 5000                  # retention על הדיסק (זנב נשמר; ייצוא לניתוח)
# סינון רעש בצד המפענח: בלי supervisory (RR וכו'), ‏ACK ריקים, ‏GSIF squitters (כל
# כמה שניות מכל תחנת קרקע — היו מציפים את הפיד), ‏x25 control ו-keepalives של הרשת.
# נשארים: acars (התוכן העיקרי), x25 data (CPDLC/ADS-C), xid (אירועי logon, קצב נמוך).
VDL2_MSG_FILTER = "all,-avlc_s,-acars_nodata,-gsif,-x25_control,-idrp_keepalive,-esis"

# --- SATCOM (מצב רביעי: ACARS דרך לוויין Inmarsat, L-band) -------------------
# תעבורת ACARS מעל אוקיינוסים/אזורים בלי כיסוי VHF עוברת דרך לוויין Inmarsat
# Classic Aero. inmarsat-sniffer (alphafox02) מפענח את זה מה-RSP1B (אנטנת
# L-band+LNA נפרדת, מוחלפת *ידנית* מול אנטנת ה-airband — ר' README/docs) ושולח
# JSON ל-UDP; שדה isu.acars מסונתז ל-dict בסגנון acarsdec ומוזרם דרך
# acars_parse._normalize_acars, בדיוק כמו מסלול A של VDL2 (ר' acars_parse._normalize_satcom).
# הלוויין (לא "תדרים") הוא הפרמטר הנבחר: geostationary => כיוון אנטנה חד-פעמי,
# אין "בנקים" כמו ACARS/VDL2. לכן satcom_freqs (בשם, לסימטריה עם acars/vdl2 ב-
# /api/mode) הוא רשימה בת-איבר-יחיד עם דגל הלוויין (למשל ["AF1"] = Alphasat).
SATCOM_SERVICE = "airam-satcom"
SATCOM_ENV_PATH = Path("/etc/airam/satcom.env")
SATCOM_UDP_PORT = 5558                # חייב להתאים ל-SATCOM_UDP ב-satcom.env
# דגל --web[=PORT] המובנה של inmarsat-sniffer (options.c/web.c, אומת מהמקור)
# מרים dashboard HTTP עצמאי עם GET /api/state: total_acars/feed_drops/channels
# [{ch,baud,msgs,age,mse,ebno,lock}] — lock=נעילת דמודולטור *גם* באפס הודעות
# מפוענחות. זו האבחנה שחסרה בין "אין אנטנה"/"לא מכוון"/"תקין, שקט כרגע".
# ⚠ web.c קושר ל-INADDR_ANY (לא ניתן להגבלה ל-loopback דרך הכלי עצמו — נבדק
# במקור) => הפורט עצמו נגיש ברשת המקומית, לא רק מ-127.0.0.1. GET /api/satcom/
# health עושה proxy מקומי (כמו /stream) כדי ש-_guard/PIN יישארו שער אחיד
# לממשק, אבל זה *לא* מבטל את חשיפת הפורט הגולמי ברשת — אותה קטגוריית סיכון
# כמו Icecast (8000, גם הוא בלי אימות, מיועד לרשת פרטית מהימנה בלבד — §9).
SATCOM_WEB_PORT = 8888
SATCOM_HEALTH_TIMEOUT = 2.0           # שניות — נקרא ב-polling, חייב להיות מהיר
SATCOM_SPECTRUM_BINS = 256            # ברירת מחדל לבקשת ספקטרום (web.c: 32..1024)
SATCOM_SPECTRUM_TIMEOUT = 3.0         # מעט יותר מ-health: מערך גדול יותר
SATCOM_LOG_TAIL_LINES = 40            # GET /api/satcom/log — מספיק לשורות הפתיחה
# רווח ברירת מחדל = AGC (ריק, כמו ACARS/VDL2). לרווח ידני מעבירים gRdB ל-
# write_satcom_env => --sdrplay-gain (הפחתה, קטן=רווח גדול). לא --soapy-gain (ר' שם).
SATCOM_BUF_MAX = 500                  # הודעות אחרונות בזיכרון (כמו ACARS/VDL2)
SATCOM_LOG_PATH = Path("/var/lib/airam/satcom.jsonl")
SATCOM_LOG_KEEP = 5000                # retention על הדיסק (זנב נשמר; ייצוא לניתוח)

ACTIVITY_PATH = Path("/var/lib/airam/activity.jsonl")
ACTIVITY_KEEP = 500            # היומן שורד את מחיקת הקבצים (retention) - רק בלי נגינה
ACTIVITY_RETURN = 50


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

# ⚠ הגנה נגד ניחוש PIN: בלי rate-limit, PIN בן 4 ספרות (הדוגמה במסמכים) הוא
# מרחב של 10,000 ערכים — לקוח ברשת המקומית (או דף DNS-rebinding, ר' _guard)
# יכול למצות אותו תוך שניות. לא חוסמים IP לגמרי (DHCP/NAT הופכים חסימה קבועה
# לפגיעה במשתמש לגיטימי) — רק מאטים משמעותית ניסיונות חוזרים מאותו מקור.
_PIN_FAIL_LOCK = threading.Lock()
_pin_fails = {}              # remote_addr -> (count, window_start_epoch)
PIN_RATE_WINDOW_SEC = 60.0
PIN_RATE_MAX_ATTEMPTS = 5    # מעבר לזה — הבקשה נדחית בלי לבדוק PIN עד סוף החלון
PIN_RATE_DELAY_SEC = 1.0     # השהיה נוספת על הדחייה (מייקרת גם ניסיון בודד)
_PIN_FAILS_MAX = 256         # מעליו מגזמים חלונות שפג תוקפם (ר' _pin_prune)


def _pin_rate_limited(ip):
    now = time.time()
    with _PIN_FAIL_LOCK:
        _pin_prune(now)
        count, start = _pin_fails.get(ip, (0, now))
        if now - start > PIN_RATE_WINDOW_SEC:
            count, start = 0, now
        return count >= PIN_RATE_MAX_ATTEMPTS


def _pin_prune(now):
    """גיזום חלונות שפג תוקפם. ⚠ נקרא תחת _PIN_FAIL_LOCK בלבד. בלעדיו המילון
    גדל לצמיתות: הערכים נמחקים רק בהצלחה (_pin_record_success), כך שכל IP
    שנכשל ולא הצליח אף פעם נשאר בזיכרון עד אתחול (DHCP/NAT מייצרים כתובות
    חדשות לאורך זמן)."""
    if len(_pin_fails) < _PIN_FAILS_MAX:
        return
    for ip in [k for k, (_c, start) in _pin_fails.items() if now - start > PIN_RATE_WINDOW_SEC]:
        _pin_fails.pop(ip, None)


def _pin_record_fail(ip):
    now = time.time()
    with _PIN_FAIL_LOCK:
        _pin_prune(now)
        count, start = _pin_fails.get(ip, (0, now))
        if now - start > PIN_RATE_WINDOW_SEC:
            count, start = 0, now
        _pin_fails[ip] = (count + 1, start)


def _pin_record_success(ip):
    with _PIN_FAIL_LOCK:
        _pin_fails.pop(ip, None)


@app.before_request
def _guard():
    """הגנות קלות על בקשות משנות-מצב (POST/PUT/DELETE):
      1. CSRF / DNS-rebinding: אם נשלח Origin/Referer הוא חייב להתאים ל-Host.
      2. אימות אופציונלי: אם AIRAM_PIN הוגדר, נדרש header X-AIRAM-PIN תואם —
         השוואה בזמן-קבוע (hmac.compare_digest, לא ==) + rate-limit רך לפי IP.
    בקשות GET (סטרים/מדדים/health/activity/airspace/metar/power) לא מושפעות."""
    if request.method not in ("POST", "PUT", "DELETE"):
        return None
    origin = request.headers.get("Origin") or request.headers.get("Referer")
    if origin and urlparse(origin).netloc != request.host:
        return jsonify(ok=False, error="מקור הבקשה לא תואם (Origin)"), 403
    if AIRAM_PIN:
        ip = request.remote_addr or "unknown"
        if _pin_rate_limited(ip):
            # ⚠ דוחים *בלי לבדוק את ה-PIN בכלל*, לא רק משהים. הגרסה הקודמת
            # השהתה שנייה ואז בדקה בכל זאת — וזה לא מגביל קצב: Flask רץ
            # threaded=True, כך שההשהיה מתרחשת בכל thread במקביל. תוקף שפותח
            # 100 חיבורים בו-זמנית היה ממצה מרחב של 4 ספרות בכ-100 שניות,
            # ולא ב-"~2.7 שעות" שההערה כאן הבטיחה. דחייה מוחלטת לאורך החלון
            # חוסמת את המקביליות: 5 ניסיונות ל-60 שניות, ולא משנה כמה חיבורים.
            # החסימה מוגבלת-בזמן (מתפוגגת לבד) => DHCP/NAT לא נענשים לצמיתות.
            time.sleep(PIN_RATE_DELAY_SEC)
            return jsonify(ok=False, auth=True,
                           error="יותר מדי ניסיונות PIN — המתן דקה ונסה שוב"), 429
        supplied = request.headers.get("X-AIRAM-PIN", "")
        if not hmac.compare_digest(supplied, AIRAM_PIN):
            _pin_record_fail(ip)
            return jsonify(ok=False, error="נדרש PIN", auth=True), 401
        _pin_record_success(ip)
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
def render_config(freq, mod, agc, if_gain, rf_gain, squelch_mode="auto", squelch_snr=config.SNR_DEFAULT,
                  chan_filter=config.CHAN_FILTER_DEFAULT, fm_notch=False, serial=None,
                  stats_path=None, mount=None, denoise_udp=True, rec_template=None):
    """קונפיג rtl_airband. ברירות המחדל = הערוץ הראשי. הערוץ השני (קול כפול) מעביר stats_path/mount
    משלו ו-denoise_udp=False: הסינון מאזין לפורט אחד, ושני שולחים אליו היו מערבבים שני תדרים."""
    stats_path = stats_path or STATS_PATH
    mount = mount or MOUNT
    rec_template = rec_template or recordings.REC_BASENAME
    f = float(freq)
    cf = config.CHAN_FILTERS.get(chan_filter, config.CHAN_FILTERS[config.CHAN_FILTER_DEFAULT])
    # rfnotch_ctrl: הגדרת SoapySDRPlay3 שעוברת דרך ה-device_string (נקראת ע"י writeSetting
    # בבנאי, *לפני* שהזרם עולה — ב-RSP1B היא rsp1aParams.rfNotchEnable). ערך קבוע, לא קלט.
    dev = "driver=sdrplay" + _serial_arg(serial) + (",rfnotch_ctrl=true" if fm_notch else "")
    lines = [
        "# נוצר אוטומטית ע\"י AIR-AM web tuner. שינויים ידניים נדרסים בכל כיוונון.",
        "localtime = true;   # חותמות הזמן בשמות קובצי ההקלטה בזמן מקומי",
        f'stats_filepath = "{stats_path}";   # מדדי RF (signal/noise) ל-/api/metrics',
        f"fft_size = {cf['fft']};   # מסנן הערוץ: {chan_filter if chan_filter in config.CHAN_FILTERS else config.CHAN_FILTER_DEFAULT}",
        "devices:",
        "(",
        "  {",
        '    type = "soapysdr";',
        f'    device_string = "{dev}";',
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
    if cf["bandwidth"]:
        lines.append(f"        bandwidth = {int(cf['bandwidth'])};")
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
        f'            mountpoint = "{mount}";',
        f'            name = "AIR-AM {f:.3f}";',
        '            username = "source";',
        f'            password = "{SOURCE_PW}";',
        "          }" + ("," if (denoise_udp or record) else ""),
    ]
    if denoise_udp:
        lines += [
        # עותק PCM לסינון הרעשים (airam-denoise) — *תמיד*, גם כשהמתג כבוי:
        # הדלקה/כיבוי לא מאתחלים את הרדיו ולא מפצלים הקלטה. sendto לא-חוסם
        # שמתעלם מכשל (udp_stream.cpp) => בלי מאזין זה ~64KB/s לשום מקום.
        # ⚠ כשל באתחול הפלט הזה מפיל את rtl_airband כולו (rtl_airband.cpp:
        # "aborting") — לכן כתובת ופורט *מספריים* בלבד: בלי DNS, ו-connect
        # של UDP בלופבק מצליח גם בלי שאיש מאזין. continuous => אפסים גם
        # בסקוולץ' סגור, אחרת Icecast מנתק את /clean.mp3 אחרי source-timeout.
        "          {",
        '            type = "udp_stream";',
        '            dest_address = "127.0.0.1";',
        f"            dest_port = {DENOISE_UDP_PORT};",
        "            continuous = true;",
        "          }" + ("," if record else ""),
        ]
    if record:
        lines += [
            "          {",
            '            type = "file";',
            f'            directory = "{recordings.REC_DIR}";',
            f'            filename_template = "{rec_template}";',
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


# תבנית קובצי ה-tmp של storage.atomic_write (‎<שם>.<סיומת>.tmp<pid>-<tid>) — לניקוי
# יתומים בעלייה. ניתוק חשמל *בין* הכתיבה ל-rename משאיר קובץ כזה מאחור.
_TMP_GLOB = "*.tmp*"
_TMP_ORPHAN_AGE_SEC = 3600     # שעה: זהיר בהרבה ממשך כתיבה אמיתי (מילישניות)


def _cleanup_orphan_tmp(dirs=None):
    """מוחק קובצי tmp יתומים שנשארו מכתיבה שנקטעה (ניתוק חשמל באמצע
    storage.atomic_write). ⚠ נקרא *רק בעלייה* ורק על קבצים ישנים מ-_TMP_ORPHAN_AGE_SEC:
    ה-pid בשם חוזר על עצמו במערכת, ולכן אי אפשר להסיק ממנו בבטחה שהתהליך מת —
    מחיקת tmp של כתיבה *חיה* תפיל אותה. מחזיר את מספר הקבצים שנמחקו (ללוג)."""
    removed = 0
    now = time.time()
    default_dirs = (appstate.STATE_PATH.parent, CONFIG_PATH.parent, ACARS_ENV_PATH.parent,
                    recordings.REC_DIR, recordings._saved_dir())
    for d in (dirs if dirs is not None else default_dirs):
        try:
            candidates = list(Path(d).glob(_TMP_GLOB))
        except OSError:
            continue
        for p in candidates:
            try:
                if now - p.stat().st_mtime < _TMP_ORPHAN_AGE_SEC:
                    continue          # יכול להיות כתיבה חיה של מופע אחר
                p.unlink()
                removed += 1
            except OSError:
                continue              # נמחק בינתיים / אין הרשאה — לא מעניין
    return removed


def write_config(freq, mod, agc, if_gain, rf_gain, squelch_mode="auto", squelch_snr=config.SNR_DEFAULT,
                 chan_filter=config.CHAN_FILTER_DEFAULT, fm_notch=False):
    storage.atomic_write(CONFIG_PATH, render_config(freq, mod, agc, if_gain, rf_gain, squelch_mode, squelch_snr,
                                             chan_filter, fm_notch, serial=_primary_serial()))


def _voice2_params(st):
    """(freq2, mod2) תקינים מה-state — ערך פגום ⇒ ברירת המחדל."""
    try:
        f2 = float(st.get("voice2_freq"))
        if not (0.1 <= f2 <= 1999.5):
            raise ValueError
    except (TypeError, ValueError):
        f2 = appstate.DEFAULT_STATE["voice2_freq"]
    m2 = "nfm" if str(st.get("voice2_mod", "am")).lower() == "nfm" else "am"
    return f2, m2


def write_voice2_config(st=None):
    """קונפיג הערוץ השני (קול כפול) על המקלט המשני. נכתב לפני כל הפעלה — כמו acars2.env.
    squelch/מסנן/notch מהראשי, רווח AGC, בלי פלט UDP לסינון (ר' render_config)."""
    st = st if st is not None else appstate.load_state()
    f2, m2 = _voice2_params(st)
    sq_mode = str(st.get("squelch_mode", "auto")).lower()
    if sq_mode not in SQUELCH_MODES:
        sq_mode = "auto"
    try:
        sq_snr = max(config.SNR_MIN, min(config.SNR_MAX, float(st.get("squelch_snr", config.SNR_DEFAULT))))
    except (TypeError, ValueError):
        sq_snr = config.SNR_DEFAULT
    cf = st.get("chan_filter", config.CHAN_FILTER_DEFAULT)
    if cf not in config.CHAN_FILTERS:
        cf = config.CHAN_FILTER_DEFAULT
    storage.atomic_write(VOICE2_CONFIG_PATH, render_config(
        f2, m2, True, config.IF_GAIN_DEFAULT, config.RF_GAIN_DEFAULT, sq_mode, sq_snr, cf,
        bool(st.get("fm_notch")), serial=_secondary_serial(),
        stats_path=STATS2_PATH, mount=MOUNT2, denoise_udp=False, rec_template=recordings.REC_BASENAME2))


# --- תפקידי המקלטים (docs/dual-sdr-feasibility.md §3.1) -----------------------
def _sanitize_serial(v):
    """serial תקין או None — לעולם לא "מנוקה" חלקית (נכנס ל-device_string של root)."""
    if isinstance(v, str) and _SERIAL_RE.match(v.strip()):
        return v.strip()
    return None


def _primary_serial():
    """ה-serial של המקלט הראשי, או None ("כל מקלט" — ההתנהגות של מקלט יחיד)."""
    try:
        return _sanitize_serial(appstate.load_state().get("sdr_primary"))
    except Exception:
        return None


def _secondary_serial():
    """ה-serial של המקלט המשני (מצב קול + ACARS), או None."""
    try:
        return _sanitize_serial(appstate.load_state().get("sdr_secondary"))
    except Exception:
        return None


def _roles_error(mode="voice_acars"):
    """None כשמוגדרים ראשי ומשני שונים — תנאי לכל מצב של שני מקלטים. אחרת הודעה למשתמש."""
    p, s2 = _primary_serial(), _secondary_serial()
    if not p or not s2:
        return (COMPOSITE_LABEL.get(mode, "המצב") +
                " דורש שני מקלטים: בחר ראשי ומשני במסך הבית (🔍 זהה מקלטים)")
    if p == s2:
        return "הראשי והמשני הם אותו מקלט — בחר שניים שונים"
    return None


def _serial_arg(serial, sep=",serial="):
    """החלק שמתווסף ל-"driver=sdrplay" (או ל-"sdrplay" של inmarsat-sniffer, sep="-")."""
    s = _sanitize_serial(serial)
    return f"{sep}{s}" if s else ""


def write_sdr_env(primary, secondary):
    """/etc/airam/sdr.env — מה ש-airam-wait-sdrplay מחכה לו (serial ספציפי, לא "כל מקלט")."""
    text = "\n".join([
        "# נכתב אוטומטית ע\"י AIR-AM (תפקידי המקלטים). שינויים ידניים נדרסים.",
        "SDR_PRIMARY=" + (_sanitize_serial(primary) or ""),
        "SDR_SECONDARY=" + (_sanitize_serial(secondary) or ""),
        "",
    ])
    storage.atomic_write(SDR_ENV_PATH, text)


# --- הפעלה מחדש מאומתת + רולבק --------------------------------------------
def _sdr_present():
    """בדיקת USB מהירה (vendor 1df7 = SDRplay) בלי לפתוח את המכשיר."""
    try:
        return subprocess.run(["lsusb", "-d", "1df7:"],
                              capture_output=True, timeout=5).returncode == 0
    except Exception:
        return True   # אין lsusb / ספק => מניחים שמחובר (עדיף רולבק מיותר מאף-פעם)


def _serial_on_api(serial):
    """True/False — ה-API מציע מקלט עם ה-serial הזה; None כשאין SoapySDRUtil / timeout.
    ⚠ מקלט שתהליך כלשהו מחזיק *לא* מופיע (§5, "חיווי SDR") — לכן נשאל רק על תפקיד
    שהצרכן שלו לא רץ כרגע (מסלולי כשל / reconcile)."""
    try:
        r = subprocess.run(["SoapySDRUtil", f"--find=driver=sdrplay,serial={serial}"],
                           capture_output=True, text=True, timeout=12)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return any(d["serial"] == serial for d in _parse_sdr_find(r.stdout))


def _role_present(role="primary"):
    """האם המקלט של התפקיד (primary/secondary) מחובר. עם מקלט אחד או בלי קיבוע — בדיוק
    _sdr_present (כמו תמיד). עם שני תפקידים קבועים ופחות משני מקלטים ב-USB, "יש SDRplay"
    לא אומר *איזה* — אז שואלים את ה-API על ה-serial. בלעדי זה, ראשי שנותק בזמן שהמשני
    מחובר נחשב "נוכח": כיוונון/אתחול/סריקה נפלו ל-off במקום לחכות לו, וה-reconcile
    החזיק את TUNE_LOCK עד 45ש' בכל דקה בניסיון להרים אותו (ביקורת 17.11.2026)."""
    if not _sdr_present():
        return False
    p, s2 = _primary_serial(), _secondary_serial()
    serial = p if role == "primary" else s2
    if not serial or not (p and s2):
        return True
    _present, _desc, count = _sdr_usb_full()
    if count is None or count >= 2:
        return True
    found = _serial_on_api(serial)
    return True if found is None else found


def _journal_tail(service="rtl_airband", lines=8):
    """זנב יומן לאבחון כישלון. timeout קצר וחובה: נקראת מתוך _enter_*/
    _restart_and_verify/_enter_standby *בזמן* שהקורא מחזיק TUNE_LOCK — journalctl
    תקוע (journald לא מגיב, בדיוק הרגע שהשירותים מתנהגים לא תקין) בלי timeout
    היה תוקע את התהליך הזה לנצח ונועל את כל שינויי המצב/כיוונון/בדיקת האנטנה
    העתידיים מאחורי TUNE_LOCK שאף פעם לא משתחרר."""
    try:
        return subprocess.run(["journalctl", "-u", service, "-n", str(lines), "--no-pager"],
                              capture_output=True, text=True, timeout=5).stdout
    except subprocess.TimeoutExpired:
        return ""


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
        return (r.stderr or "restart failed").strip(), _journal_tail(), not _role_present("primary")
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
    """כיוונון נכשל => משחזרים את ההגדרות האחרונות שעבדו ומרימים מחדש.
    מחזיר True אם השחזור הצליח (rtl_airband חי) — רולבק שנכשל מטופל אצל הקורא
    בנפילה ל-off (לא משאירים שירות בלולאת קריסה ולא מעמידים פנים שהקול חזר)."""
    log.warning("rollback to %.3f MHz", prev["freq"])
    try:
        write_config(prev["freq"], prev["mod"], prev["agc"], prev["if_gain"],
                     prev["rf_gain"], prev["squelch_mode"], prev["squelch_snr"],
                     prev.get("chan_filter", config.CHAN_FILTER_DEFAULT), bool(prev.get("fm_notch")))
        subprocess.run([*SUDO, "systemctl", "restart", "rtl_airband"],
                       capture_output=True, text=True, timeout=45)
    except Exception:
        return False
    for _ in range(3):
        time.sleep(0.5)
        if not _is_active("rtl_airband"):
            return False
    return True


# --- ring-buffers של שלושת הפידים (הלוגיקה: _FEEDS, למטה) ----------------------
_acars_lock = threading.Lock()
_acars_msgs = collections.deque(maxlen=ACARS_BUF_MAX)
_acars_seq = 0                 # מזהה רץ גלובלי (cursor ל-UI: "תן לי הודעות חדשות מ-id")

# VDL2 — ring-buffer נפרד (אותה תבנית)
_vdl2_lock = threading.Lock()
_vdl2_msgs = collections.deque(maxlen=VDL2_BUF_MAX)
_vdl2_seq = 0                  # cursor נפרד ל-/api/vdl2
_vdl2_drop_count = 0           # פריימים לא-מזוהים (סכמה לא תואמת) — נחשף בלוג תקופתי

# SATCOM — ring-buffer נפרד (אותה תבנית)
_satcom_lock = threading.Lock()
_satcom_msgs = collections.deque(maxlen=SATCOM_BUF_MAX)
_satcom_seq = 0                # cursor נפרד ל-/api/satcom
_satcom_drop_count = 0         # הודעות לא-מזוהות (סכמה לא תואמת) — נחשף בלוג תקופתי


def _append_acars_log(rec):
    storage.append_jsonl_log(ACARS_LOG_PATH, rec)


def _trim_acars_log():
    storage.trim_jsonl_log(ACARS_LOG_PATH, ACARS_LOG_KEEP)


def _today_start():
    """epoch של חצות מקומי (שעון ה-Pi) של היום. רצפת-זמן ל"היום בלבד": מסננת את
    טעינת ההיסטוריה ואת /api/acars => סשן חדש לא מוצף בתעבורת ימים קודמים.
    ההיסטוריה המלאה בדיסק (acars.jsonl) נשמרת וזמינה בייצוא וב-?all=1."""
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def _day_bounds(date_str):
    """גבולות היום המקומי [start, end) עבור מחרוזת 'YYYY-MM-DD' (לארכיון החיפוש),
    או None אם הפורמט לא תקין. עצמאי מ-_today_start — משמש לקריאה מהדיסק, לא
    לרצפת "היום בלבד" של הפיד החי.
    ⚠ end מחושב עם mktime על tm_mday+1 (לא start+86400): ישראל עוברת שעון קיץ/חורף
    (ימים של 23/25 שעות) — mktime עם isdst=-1 מנרמל את tm_mday+1 (גם 32 וכו') ומחשב
    מחדש DST ליום החדש, כך שהגבול תמיד חצות-אמיתי, לא +24h קבוע."""
    try:
        lt = time.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        return None
    start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    end = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + 1, 0, 0, 0, 0, 0, -1))
    return start, end


# --- שלושת פידי הדאטה (ACARS / VDL2 / SATCOM): מימוש אחד -------------------
# אותו מחזור חיים לשלושתם: UDP מהמפענח ⇒ נרמול ⇒ dedup של שידורים חוזרים ⇒ jsonl ⇒ ring
# buffer עם id רץ ⇒ תור ההצלבה; וטעינת "היום" מהדיסק בעלייה. עד v2.37 זה היה כתוב שלוש פעמים.
# ⚠ ערכי המפרט הם *שמות* של משתנים גלובליים, ונפתרים בזמן הקריאה (_fv): הבדיקות מחליפות
# app.ACARS_LOG_PATH / app.ACARS_UDP_PORT / app._acars_seq = 0 ישירות, וכך זה ממשיך לעבוד.
_FEEDS = {
    "acars": {"label": "ACARS", "port": "ACARS_UDP_PORT", "log": "ACARS_LOG_PATH", "keep": "ACARS_LOG_KEEP",
              "buf": "ACARS_BUF_MAX", "lock": "_acars_lock", "msgs": "_acars_msgs", "seq": "_acars_seq",
              "drop": None, "normalize": "acars_parse._normalize_acars", "ident": ("tail",),
              "services": ("ACARS_SERVICE", "ACARS2_SERVICE"), "freqs_key": "acars_freqs",
              "freqs_default": "config.ACARS_FREQS_DEFAULT", "adsb": "_acars_adsb"},
    "vdl2": {"label": "VDL2", "port": "VDL2_UDP_PORT", "log": "VDL2_LOG_PATH", "keep": "VDL2_LOG_KEEP",
             "buf": "VDL2_BUF_MAX", "lock": "_vdl2_lock", "msgs": "_vdl2_msgs", "seq": "_vdl2_seq",
             "drop": "_vdl2_drop_count", "normalize": "acars_parse._normalize_vdl2", "ident": ("tail", "icao"),
             "services": ("VDL2_SERVICE", "VDL2B_SERVICE"), "freqs_key": "vdl2_freqs",
             "freqs_default": "config.VDL2_FREQS_DEFAULT", "adsb": "_vdl2_adsb"},
    # SATCOM: זהות = tail בלבד (רוב ה-ACARS הלוויני נושא רישום); בלי שדה adsb בתשובה
    "satcom": {"label": "SATCOM", "port": "SATCOM_UDP_PORT", "log": "SATCOM_LOG_PATH", "keep": "SATCOM_LOG_KEEP",
               "buf": "SATCOM_BUF_MAX", "lock": "_satcom_lock", "msgs": "_satcom_msgs", "seq": "_satcom_seq",
               "drop": "_satcom_drop_count", "normalize": "acars_parse._normalize_satcom", "ident": ("tail",),
               "services": ("SATCOM_SERVICE",), "freqs_key": "satcom_freqs",
               "freqs_default": "config.SATCOM_FREQS_DEFAULT", "adsb": None},
}
FEED_DEDUP_SEC = 90      # שידור חוזר (אין ACK מהקרקע — המטוס שולח שוב, עד 7 פעמים) ⇒ retry_count, לא כרטיס
FEED_DEDUP_MAX = 500     # מעל זה מנקים ערכים ישנים ממפת ה-dedup (מניעת דליפת זיכרון)
FEED_TRIM_EVERY = 200    # קיצוץ תקופתי של ה-jsonl (ה-listener הוא הכותב היחיד)


def _fv(feed, key):
    """ערך מהמפרט של פיד, נפתר מ-globals() *עכשיו* (ר' ההערה מעל _FEEDS). שם מנוקד
    ("acars_parse._normalize_acars") ⇒ מאפיין של מודול — גם הוא נפתר בזמן הקריאה."""
    name = _FEEDS[feed][key]
    if "." in name:
        mod, attr = name.split(".", 1)
        return getattr(globals()[mod], attr)
    return globals()[name]


def _feed_push(feed, rec):
    """id רץ + הכנסה ל-ring buffer. ⚠ רק תחת הנעילה של הפיד."""
    name = _FEEDS[feed]["seq"]
    globals()[name] += 1
    rec["id"] = globals()[name]
    _fv(feed, "msgs").append(rec)


def _load_feed_history(feed):
    """טוען את זנב ה-jsonl ל-ring buffer בעלייה => הודעות *היום* שורדות restart, ממוינות
    לפי זמן (t עולה) עם id רץ. נקרא *לפני* הפעלת thread ה-listener (אין מרוץ). רק הודעות
    מהיום נטענות לזיכרון (ההיסטוריה המלאה נשמרת בדיסק)."""
    try:
        lines = storage.read_log_lines(_fv(feed, "log"))
    except OSError:
        return
    recs = storage.jsonl_records(lines[-_fv(feed, "buf"):])
    floor = _today_start()
    recs = [r for r in recs if (r.get("t") or 0) >= floor]   # היום בלבד (הדיסק נשמר)
    recs.sort(key=lambda r: r.get("t") or 0)
    with _fv(feed, "lock"):
        for r in recs:
            _feed_push(feed, r)
    if recs:
        log.info("%s: נטענו %d הודעות מההיסטוריה", _FEEDS[feed]["label"], len(recs))


def _feed_listener(feed):
    """thread רקע: מאזין ל-UDP מהמפענח, שומר ל-jsonl ומכניס ל-ring buffer. רץ תמיד (גם
    כשהמצב אחר) — פשוט לא מגיעות דאטהגרמות כשהמפענח כבוי."""
    spec = _FEEDS[feed]
    label = spec["label"]
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    port = _fv(feed, "port")
    try:
        sock.bind((ACARS_UDP_HOST, port))
    except OSError:
        log.warning("%s listener: port %d busy - /api/%s יחזיר ריק", label, port, feed)
        return
    normalize = _fv(feed, "normalize")
    seen = 0
    # dedup: (זהות, label, text[:80]) → (timestamp, rec). retry_count מצטבר על הכרטיס המקורי
    # בזיכרון; ה-jsonl נשמר נקי מחזרות.
    dedup: dict = {}
    while True:
        try:
            data, _ = sock.recvfrom(65535)
        except OSError:
            continue
        try:
            msg = json.loads(data.decode("utf-8", "replace"))
        except (ValueError, UnicodeError):
            continue                          # דאטהגרם לא-JSON => מתעלמים
        try:
            rec = normalize(msg)
        except Exception:
            # שדה עם טיפוס בלתי-צפוי (label כרשימה, level כמחרוזת וכו') לא יפיל את
            # ה-thread לצמיתות — הפיד ימשיך לזרום להודעות הבאות.
            log.exception("%s: נרמול נכשל על דאטהגרם — מדולג", label)
            continue
        if rec is None:
            # לא בר-הצגה (סכמה לא תואמת וכו') — לוג תקופתי, כדי להבדיל "אין תעבורה"
            # מ"המפענח שינה סכמה" בלי לקרוא קוד.
            if spec["drop"]:
                globals()[spec["drop"]] += 1
                n = globals()[spec["drop"]]
                if n % 200 == 1:
                    log.warning("%s: הודעה לא זוהתה (סכמה לא תואמת?) — %d עד כה", label, n)
            continue

        ident = next((rec.get(k) for k in spec["ident"] if rec.get(k)), None)
        text = rec.get("text") or ""
        ts = rec.get("t") or time.time()
        if ident and text:                    # רק הודעות עם זהות+טקסט (ACK ריקים אינם מוחזרים)
            key = (ident, rec.get("label"), text[:80])
            prev_ts, prev_rec = dedup.get(key, (0, None))
            if prev_rec is not None and ts - prev_ts < FEED_DEDUP_SEC:
                # prev_rec חי גם ב-buffer שקוראים ממנו routes => מוטציה רק תחת הנעילה
                with _fv(feed, "lock"):
                    prev_rec["retry_count"] = prev_rec.get("retry_count", 1) + 1
                continue
            dedup[key] = (ts, rec)
            if len(dedup) > FEED_DEDUP_MAX:
                cutoff = ts - FEED_DEDUP_SEC
                for k in [k for k, (t0, _) in dedup.items() if t0 < cutoff]:
                    del dedup[k]

        storage.append_jsonl_log(_fv(feed, "log"), rec)   # התמדה לפני הקצאת id (הקובץ נקי מ-id)
        with _fv(feed, "lock"):
            _feed_push(feed, rec)
        _xc_submit(feed, rec)                 # הצלבת ADS-B — תור לא-חוסם, thread נפרד
        seen += 1
        if seen % FEED_TRIM_EVERY == 0:
            storage.trim_jsonl_log(_fv(feed, "log"), _fv(feed, "keep"))


def _feed_response(feed):
    """הלב של /api/acars, /api/vdl2, /api/satcom. ?since=<id> => רק חדשות מאותו cursor.
    כברירת מחדל רק הודעות *היום* (שעון ה-Pi) => סשן חדש לא מוצף בתעבורת ימים קודמים;
    ?all=1 => כל מה שבזיכרון. ?day=YYYY-MM-DD => ארכיון: קורא מהדיסק (לא מהזיכרון) את
    כל הודעות אותו יום מקומי — מצב סטטי, בלי cursor/adsb, עצמאי מהפיד החי."""
    spec = _FEEDS[feed]
    day = request.args.get("day")
    if day:
        bounds = _day_bounds(day)
        if bounds is None:
            return jsonify(ok=False, error="תאריך לא תקין (פורמט: YYYY-MM-DD)"), 400
        start, end = bounds
        msgs = [r for r in storage.read_jsonl_log(_fv(feed, "log")) if start <= (r.get("t") or 0) < end]
        return jsonify(ok=True, day=day, messages=msgs)
    try:
        since = int(request.args.get("since", 0))
    except (TypeError, ValueError):
        since = 0
    show_all = request.args.get("all") in ("1", "true", "yes")
    floor = 0 if show_all else _today_start()
    with _fv(feed, "lock"):
        # עותקים (לא references): jsonify מסדרל אחרי שחרור הנעילה, ו-retry_count עלול
        # להתעדכן ע"י ה-listener באמצע האיטרציה של ה-encoder
        msgs = [dict(m) for m in _fv(feed, "msgs") if m["id"] > since and (m.get("t") or 0) >= floor]
        cursor = _fv(feed, "seq")
    extra = {"adsb": _fv(feed, "adsb")()} if spec["adsb"] else {}
    return jsonify(ok=True, active=any(_is_active(globals()[n]) for n in spec["services"]),
                   freqs=appstate.load_state().get(spec["freqs_key"], _fv(feed, "freqs_default")),
                   cursor=cursor, messages=msgs, **extra, **_xc_feed_payload(feed))


def _load_acars_history():
    _load_feed_history("acars")


def _acars_listener():
    _feed_listener("acars")


def _append_vdl2_log(rec):
    storage.append_jsonl_log(VDL2_LOG_PATH, rec)


def _trim_vdl2_log():
    storage.trim_jsonl_log(VDL2_LOG_PATH, VDL2_LOG_KEEP)


def _load_vdl2_history():
    _load_feed_history("vdl2")


def _vdl2_listener():
    _feed_listener("vdl2")


def _append_satcom_log(rec):
    storage.append_jsonl_log(SATCOM_LOG_PATH, rec)


def _trim_satcom_log():
    storage.trim_jsonl_log(SATCOM_LOG_PATH, SATCOM_LOG_KEEP)


def _load_satcom_history():
    _load_feed_history("satcom")


def _satcom_listener():
    _feed_listener("satcom")


HEALTH_SERVICES = ("rtl_airband", "icecast2", "sdrplay",
                   "airam-acars", "airam-vdl2", "airam-satcom", "airam-acars2",
                   VOICE2_SERVICE, VDL2B_SERVICE, DENOISE_SERVICE)   # לחיווי בלבד — לא משפיע על ok (ר' api_health)


def _services_status(names):
    """סטטוס של כמה יחידות ב-fork *אחד*. `systemctl is-active a b c` מקבל רשימת
    יחידות ומדפיס שורה לכל אחת, לפי הסדר שנשלח.
    ⚠ למה זה משנה: /api/health נקרא בפולינג כל 10 שניות מכל טלפון שפתוח, וקודם
    פתח שישה תהליכים בכל קריאה => ~2,160 forks בשעה, בפרויקט שחוסך 50% CPU
    בדמודולטור (skip_c) בדיוק כדי לשרוד על ספק כוח שולי בשטח. ניטור עצמי לא
    אמור להיות הצרכן הגדול בתקציב.
    ‏rc!=0 הוא המצב *הרגיל* כאן (הוא רק מציין שלא כל היחידות active) — ולכן
    קוראים את stdout ולא בודקים returncode. שורה חסרה => "unknown", כמו קודם."""
    try:
        r = subprocess.run(["systemctl", "is-active", *names],
                           capture_output=True, text=True, timeout=5)
        lines = r.stdout.splitlines()
    except Exception:
        lines = []
    return {svc: (lines[i].strip() if i < len(lines) else "") or "unknown"
            for i, svc in enumerate(names)}


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
    הפעולות האלה ל-airam: restart/stop של rtl_airband / airam-acars / airam-vdl2 /
    airam-satcom, reset-failed של airam-satcom בלבד — ר' _enter_satcom — ו-start/stop
    של airam-denoise, ר' _denoise_sync)."""
    return subprocess.run([*SUDO, "systemctl", action, service],
                          capture_output=True, text=True, timeout=timeout)


def _sanitize_freqs(freqs, default=None):
    """מסנן רשימת תדרים לערכים תקינים (MHz). נכתבים ל-env => חובה לוודא
    שאין הזרקה: רק ספרות ונקודה (אף ש-systemd מנתח בבטחה, שמירה על קלט נקי).
    ‏default => רשימת הנפילה כשלא נשאר כלום (ברירת מחדל: תדרי ה-ACARS)."""
    out = [str(f).strip() for f in (freqs or []) if _FREQ_RE.match(str(f).strip())]
    return out or list(default if default is not None else config.ACARS_FREQS_DEFAULT)


def _window_error(freqs, max_channels, window_mhz, decoder):
    """בודק שרשימת תדרים חוקית לחלון דגימה *אחד* של המפענח: עד max_channels
    ערוצים, וכולם בתוך span של window_mhz. מחזיר הודעת שגיאה (str) או None אם
    תקין. טהורה => נבדקת בלי חומרה. משותפת ל-ACARS (acarsdec) ול-VDL2 (dumpvdl2)."""
    vals = []
    for f in freqs or []:
        try:
            vals.append(float(f))
        except (TypeError, ValueError):
            continue
    if not vals:
        return "לא נבחרו תדרים תקינים"
    if len(vals) > max_channels:
        return "%s תומך עד %d ערוצים (נבחרו %d)" % (decoder, max_channels, len(vals))
    span = max(vals) - min(vals)
    if span > window_mhz + 1e-9:
        return ("התדרים מרוחקים מדי לחלון דגימה אחד (טווח %.3fMHz, מקסימום %sMHz) — "
                "בחר בנק תדרים אחר" % (span, window_mhz))
    return None


def _acars_window_error(freqs):
    return _window_error(freqs, ACARS_MAX_CHANNELS, ACARS_WINDOW_MHZ, "acarsdec")


def _vdl2_window_error(freqs):
    return _window_error(freqs, VDL2_MAX_CHANNELS, VDL2_WINDOW_MHZ, "dumpvdl2")


# ⚠ אותו גבול-אבטחה כמו _FREQ_RE (ר' ההערה שם) — אלפאנומרי בלבד, בלי מקף
# מוביל/רווח, לפני כתיבה ל-SATCOM_SATELLITE ב-env שנקרא ע"י $-לא-מצוטט ב-ExecStart.
_SAT_RE = re.compile(r"^[A-Z0-9]{2,4}$")   # פורמט דגל לוויין (טוקן קצר) לפני כתיבה ל-env


def _sanitize_satellite(freqs, default=None):
    """כמו _sanitize_freqs: מסנן *פורמט* בלבד (טוקן אלפאנומרי קצר) לפני כתיבה
    ל-env, לא "לוויין מוכר" — זו אחריות _satcom_window_error (בדיוק כמו ש-
    _sanitize_freqs לא בודק שהתדר בבנק תקין, רק שהוא תדר). ההפרדה הזו קריטית:
    "XYZ" (פורמט תקין, לוויין לא-קיים) חייב לעבור הלאה ל-window_error ולקבל
    400 מסודר — לא ליפול בשקט לברירת המחדל (בניגוד לג'אנק אמיתי כמו "$(reboot)").
    מחזיר תמיד רשימה בת-איבר-יחיד (geostationary => לוויין אחד, לא בנק)."""
    out = [str(f).strip().upper() for f in (freqs or [])
           if _SAT_RE.match(str(f).strip().upper())]
    return out[:1] or list(default if default is not None else config.SATCOM_FREQS_DEFAULT)


def _sanitize_satcom_gain(value, default=None):
    """מנרמל את בחירת הרווח ל-`None` (AGC) או ל-int בתחום IFGR_MIN..IFGR_MAX.

    שלוש כניסות שונות, שלוש משמעויות (חשוב לא לבלבל ביניהן):
      • `None`/`""`/`"agc"` => AGC מפורש (הבחירה המכוונת "תן לדרייבר לנהל").
      • מספר => gRdB ידני, נחתך לתחום. **חותכים ולא דוחים** כי הכלי עצמו
        חותך בדיוק לאותו תחום (sdrplay.c) — 400 כאן היה מציג למשתמש שגיאה
        על ערך שהחומרה מקבלת בשקט, וזו הבחנה בלי הבדל.
      • ג'אנק (מחרוזת לא-מספרית, dict) => `default` — אותו דפוס בדיוק כמו
        `_sanitize_freqs`/`_sanitize_satellite`: פורמט לא-תקין לא מפיל בקשה,
        הוא נופל לבחירה השמורה.
    """
    if value is None or (isinstance(value, str) and value.strip().lower() in ("", "agc", "auto")):
        return None
    try:
        return max(config.IFGR_MIN, min(config.IFGR_MAX, int(float(value))))
    except (TypeError, ValueError):
        return default


def _satcom_window_error(freqs):
    """ולידציה מקבילה ל-_window_error/_vdl2_window_error, אך ללוויין ולא לחלון
    דגימה: /api/mode הגנרי מצפה לפונקציה בחתימה (freqs) -> error|None, כדי
    ש-satcom ישתלב באותו זנב גנרי כמו acars/vdl2 (ר' api_mode)."""
    vals = [str(f).strip().upper() for f in (freqs or [])]
    if not vals:
        return "לא נבחר לוויין"
    if len(vals) > 1:
        return "ניתן לבחור לוויין אחד בלבד (geostationary — לא בנק ערוצים)"
    if vals[0] not in config.SATCOM_SATELLITES:
        return "לוויין לא מוכר: %s (אפשרויות: %s)" % (vals[0], ", ".join(sorted(config.SATCOM_SATELLITES)))
    return None


def write_acars2_env(freqs, gain=ACARS_GAIN_DEFAULT, ratemult=ACARS_RATEMULT_DEFAULT):
    """/etc/airam/acars2.env — אותו פורמט בדיוק, על המקלט המשני (מצב קול + ACARS)."""
    write_acars_env(freqs, gain, ratemult, path=ACARS2_ENV_PATH, serial=_secondary_serial())


def write_acars_env(freqs, gain=ACARS_GAIN_DEFAULT, ratemult=ACARS_RATEMULT_DEFAULT,
                    path=None, serial=None):
    """כותב /etc/airam/acars.env בפורמט EnvironmentFile של systemd. הערך של
    ACARS_FREQS *לא* מצוטט: systemd לוקח את שארית השורה (כולל רווחים) כערך,
    וב-ExecStart ‎$ACARS_FREQS (ללא סוגריים) מתפצל בחזרה למספר ארגומנטים."""
    text = "\n".join([
        "# נכתב אוטומטית ע\"י AIR-AM web tuner (מצב ACARS). שינויים ידניים נדרסים.",
        f"ACARS_FREQS={' '.join(_sanitize_freqs(freqs))}",
        f"ACARS_GAIN={int(gain)}",
        f"ACARS_RATEMULT={int(ratemult)}",
        f"ACARS_UDP={ACARS_UDP_HOST}:{ACARS_UDP_PORT}",
        # ‎-d driver=sdrplay${ACARS_SERIAL} ב-unit — ריק = כל מקלט (מקלט יחיד)
        "ACARS_SERIAL=" + _serial_arg(serial if path else _primary_serial()),
        "",
    ])
    storage.atomic_write(path or ACARS_ENV_PATH, text)


def write_vdl2_env(freqs, ifgr=None, rfgr=None, path=None, serial=None):
    """כותב /etc/airam/vdl2.env בפורמט EnvironmentFile של systemd. שים לב:
    ‏dumpvdl2 מקבל תדרים ב-*Hz* — ההמרה מ-MHz (הפורמט של state/UI) נעשית רק כאן.
    ‏VDL2_GAIN מכיל את הדגל *כולו* (או ריק): ‏$VDL2_GAIN לא-מצוטט ב-ExecStart נעלם
    לגמרי כשהערך ריק => ברירת המחדל היא AGC של הדרייבר (כמו rtl_airband בלי שורת gain).
    האפליקציה כותבת רק מחרוזת ריקה או ints מפורמטים => אין משטח הזרקה."""
    mhz = _sanitize_freqs(freqs, config.VDL2_FREQS_DEFAULT)
    hz = " ".join(str(int(round(float(f) * 1e6))) for f in mhz)
    gain = ""
    if ifgr is not None and rfgr is not None:
        gain = "--soapy-gain IFGR=%d,RFGR=%d" % (int(ifgr), int(rfgr))
    text = "\n".join([
        "# נכתב אוטומטית ע\"י AIR-AM web tuner (מצב VDL2). שינויים ידניים נדרסים.",
        "# התדרים ב-Hz (dumpvdl2), בעוד ה-state/UI עובדים ב-MHz.",
        f"VDL2_FREQS={hz}",
        f"VDL2_GAIN={gain}",
        f"VDL2_MSG_FILTER={VDL2_MSG_FILTER}",
        "VDL2_SERIAL=" + _serial_arg(serial if path else _primary_serial()),
        "",
    ])
    storage.atomic_write(path or VDL2_ENV_PATH, text)


def write_satcom_env(freqs, gain=None, bias_tee=True, skip_c=True, spectrum=True):
    """כותב /etc/airam/satcom.env בפורמט EnvironmentFile של systemd.
    ‏freqs כאן הוא רשימה בת-איבר-יחיד עם דגל הלוויין (למשל ["AF1"]) — geostationary
    => אין "ערוצים"/בנק לבחור כמו ACARS/VDL2 (ר' הערה ליד SATCOM_FREQS_DEFAULT).
    ‏SATCOM_GAIN מכיל את דגל הרווח *כולו* (או ריק) — כמו VDL2_GAIN. ⚠ המפענח
    ‏inmarsat-sniffer בדרייבר ה-SDRplay הנייטיבי (‎-i sdrplay) קורא את הרווח מ-
    ‎--sdrplay-gain (gRdB — *הפחתה*, קטן=רווח גדול, כמו IFGR), *לא* מ---soapy-gain
    (שהוא לדרייבר ה-SoapySDR הגנרי בלבד — אומת מ-sdrplay.c/options.c במקור). ריק
    => AGC של הדרייבר (ברירת המחדל, כמו ACARS/VDL2). ‏SATCOM_BIAS_TEE מכיל את הדגל
    ‎-B (או ריק): ‏$SATCOM_BIAS_TEE לא-מצוטט ב-ExecStart נעלם כשריק => bias-T כבוי.
    ⚠ bias-T חייב להיות דולק *רק* במצב satcom (מזין את ה-LNA שצמוד לאנטנת ה-L-band,
    לא לאנטנת ה-airband) — satcom.env לא נטען כלל במצבי VHF אחרים, מובטח מבנית.
    ‏SATCOM_WEB_PORT קבוע (לא תלוי-בחירת-משתמש) — משמש את ה---web= ב-ExecStart
    ואת GET /api/satcom/health (ר' SATCOM_WEB_PORT למעלה).
    ‏SATCOM_SKIP_C מכיל את הדגל ‎--skip-c-channel (או ריק) — ר' §12: מדלג על
    ששת דמודולטורי ה-OQPSK 8400 (C-channels) מתוך 12 הערוצים של Alphasat.
    ברירת המחדל **דולקת** כי AIR-AM צורך ACARS בלבד וה-C-channels כמעט לא
    נושאים אותו, בעוד שהם הדמודולטורים היקרים ביותר (‎~50% CPU לפי המקור).
    ‏SATCOM_SPECTRUM מכיל את הדגל ‎--spectrum (או ריק) — פותח את
    GET /api/spectrum בלוח האבחון של הכלי, שממנו GET /api/satcom/spectrum
    שואב. **האבחון היחיד שמבחין "אין RF" מ"יש RF בלי נעילה"** (ר' ההערה ליד
    SATCOM_SPECTRUM_BINS). דולק כברירת מחדל; עלות CPU רציפה אפס."""
    sats = _sanitize_satellite(freqs)
    gain_flag = "--sdrplay-gain=%d" % int(gain) if gain is not None else ""
    text = "\n".join([
        "# נכתב אוטומטית ע\"י AIR-AM web tuner (מצב SATCOM). שינויים ידניים נדרסים.",
        f"SATCOM_SATELLITE={sats[0]}",
        f"SATCOM_GAIN={gain_flag}",
        "SATCOM_BIAS_TEE=" + ("-B" if bias_tee else ""),
        "SATCOM_SKIP_C=" + ("--skip-c-channel" if skip_c else ""),
        "SATCOM_SPECTRUM=" + ("--spectrum" if spectrum else ""),
        f"SATCOM_UDP={ACARS_UDP_HOST}:{SATCOM_UDP_PORT}",
        f"SATCOM_WEB_PORT={SATCOM_WEB_PORT}",
        # ‎-i sdrplay${SATCOM_SERIAL} — הפורמט של inmarsat-sniffer הוא sdrplay-<serial>
        "SATCOM_SERIAL=" + _serial_arg(_primary_serial(), sep="-"),
        "",
    ])
    storage.atomic_write(SATCOM_ENV_PATH, text)


def _stop_services(services, timeout=30):
    """עצירה best-effort, לפי הסדר שנשלח. Conflicts ביחידות עוצר ממילא — עוצרים מפורשות
    קודם כדי לשחרר את ה-SDR לפני שהצרכן הבא פותח אותו (מונע מרוץ על המכשיר)."""
    for svc in services:
        try:
            _sysctl("stop", svc, timeout=timeout)
        except Exception:
            pass


def _restart_verified(service, stuck_msg, failed_msg, default_err):
    """restart לצרכן SDR + אימות שהוא *נשאר* למעלה. מחזיר (error, detail).
    השירות יכול לעלות ואז לקרוס (SDR תפוס/חסר) ⇒ פולינג ~3.5ש' ולא בדיקה בודדת.
    ⚠ לא לבלבל עם _restart_and_verify של הקול (רולבק, SDR נוכח) — נפרד בכוונה."""
    try:
        r = _sysctl("restart", service, timeout=45)
    except subprocess.TimeoutExpired:
        return stuck_msg, None
    if r.returncode != 0:
        return (r.stderr or default_err).strip(), _journal_tail(service)
    for _ in range(7):
        time.sleep(0.5)
        if not _is_active(service):
            return failed_msg, _journal_tail(service)
    return None, None


def _enter_vdl2(freqs):
    """עוצר את שני צרכני ה-SDR האחרים ומריץ dumpvdl2. מחזיר (error, detail).
    Conflicts ב-unit עוצר אותם ממילא, אבל עוצרים מפורשות תחילה כדי לשחרר את
    ה-SDR לפני ש-dumpvdl2 פותח אותו (מונע מרוץ על המכשיר)."""
    _stop_services(("rtl_airband", ACARS_SERVICE, *SECONDARY_SERVICES, SATCOM_SERVICE))
    write_vdl2_env(freqs)
    return _restart_verified(VDL2_SERVICE, "הפעלת VDL2 נתקעה — בדוק שה-SDR מחובר",
                             "dumpvdl2 נכשל לעלות — בדוק journalctl -u airam-vdl2", "dumpvdl2 failed")


def _enter_acars(freqs):
    """עוצר את שאר צרכני ה-SDR ומריץ acarsdec. מחזיר (error, detail).
    Conflicts ב-unit עוצר אותם ממילא, אבל עוצרים מפורשות תחילה כדי לשחרר את
    ה-SDR לפני ש-acarsdec פותח אותו (מונע מרוץ על המכשיר)."""
    _stop_services(("rtl_airband", *SECONDARY_SERVICES, VDL2_SERVICE, SATCOM_SERVICE))
    write_acars_env(freqs)
    return _restart_verified(ACARS_SERVICE, "הפעלת ACARS נתקעה — בדוק שה-SDR מחובר",
                             "acarsdec נכשל לעלות — בדוק journalctl -u airam-acars", "acarsdec failed")


def _enter_satcom(freqs, bias_tee=True, skip_c=True, spectrum=True, gain=None):
    """עוצר את שלושת צרכני ה-SDR האחרים ומריץ inmarsat-sniffer. מחזיר
    (error, detail). Conflicts ב-unit עוצר אותם ממילא, אבל עוצרים מפורשות
    תחילה כדי לשחרר את ה-SDR לפני ש-inmarsat-sniffer פותח אותו (מונע מרוץ על
    המכשיר) — כמו _enter_acars/_enter_vdl2. ⚠ הכניסה למצב הזה *לא* מחליפה את
    האנטנה הפיזית (VHF airband <-> L-band) — זו פעולה ידנית של המשתמש; ה-UI
    מציג באנר-הוראה בכניסה/יציאה (ר' docs/satcom-feasibility.md §3).
    ‏bias_tee=False למי שמזין את ה-LNA ממקור חיצוני (USB power bank + DC
    injector) — ‏RSP1B bias-T מוגבל ל-‎100mA, ותוספת הצריכה של ה-LNA + עליית
    ה-CPU של inmarsat-sniffer בו-זמנית עלולה לדחוף ספק שולי (למשל power bank
    נייד) מעבר לתקרה. ⚠ אסור להזין משני מקורות בו-זמנית (הזרמה הדדית אפשרית)
    — המשתמש אחראי לוודא שרק אחד מהם דולק בפועל.
    ‏skip_c=True (ברירת מחדל) מוריד את דמודולטורי ה-C-channel — הצד השני של
    אותו תקציב חשמל, אבל דרך ה-CPU במקום דרך ה-bias-T (ר' §12/write_satcom_env).
    ‏spectrum=True (ברירת מחדל) מפעיל את ‎--spectrum => GET /api/satcom/spectrum
    זמין (אבחון "יש RF בכלל?" — ר' SATCOM_SPECTRUM_BINS).
    ‏gain=None (ברירת מחדל) => AGC של הדרייבר; int 20..59 => gRdB ידני עם
    LNAstate מקובע ל-0 (רווח RF מקסימלי) — ר' SATCOM_GAIN_DEFAULT למה זה
    דווקא *עוזר* לאות לוויין חלש כשה-AGC נחנק מאנרגיה מחוץ לפס."""
    _stop_services(("rtl_airband", ACARS_SERVICE, *SECONDARY_SERVICES, VDL2_SERVICE))
    write_satcom_env(freqs, gain=gain, bias_tee=bias_tee, skip_c=skip_c, spectrum=spectrum)
    try:
        # airam-satcom.service (בשונה משאר צרכני ה-SDR) מוגדר עם StartLimitBurst
        # סופי — הגנה מפני קריסה חוזרת שמדליקה מחדש bias-T ללא פיקוח (ר' ההערה
        # ביחידה). אם התקרה הופעלה מקריסה קודמת, restart רגיל ייכשל עד
        # reset-failed; best-effort, לא תלוי הצלחה (no-op תקין כשלא היה כשל).
        _sysctl("reset-failed", SATCOM_SERVICE, timeout=10)
    except Exception:
        pass
    return _restart_verified(SATCOM_SERVICE, "הפעלת SATCOM נתקעה — בדוק שה-SDR מחובר",
                             "inmarsat-sniffer נכשל לעלות — בדוק journalctl -u airam-satcom",
                             "inmarsat-sniffer failed")


def _enter_standby():
    """מצב כיבוי (standby): עוצר את *ארבעת* צרכני ה-SDR (rtl_airband + acarsdec +
    dumpvdl2 + inmarsat-sniffer) => משחרר את ה-RSP1B ליישום SDR אחר, בעוד
    airam-web/הדף נשארים פעילים. את sdrplay.service משאירים חי בכוונה: ה-API
    daemon הוא המתווך שמאפשר לאפליקציית SDRplay אחרת להתחבר מיד — וגם ה-sudoers
    ממילא אינו מתיר לעצור אותו. מחזיר (error, detail). serialized תחת TUNE_LOCK
    ע"י הקורא."""
    consumers = (ACARS_SERVICE, *SECONDARY_SERVICES, VDL2_SERVICE, SATCOM_SERVICE, "rtl_airband")
    _stop_services(consumers)
    stuck = []
    for _ in range(7):
        time.sleep(0.3)
        stuck = [svc for svc in consumers if _is_active(svc)]
        if not stuck:
            return None, None
    # journal של השירות שבאמת עדיין פעיל (לא rtl_airband קשיח) — קריטי כש-satcom
    # הוא התקוע: אבחון שגוי בדיוק כשהכי חשוב לדעת מה לא נעצר (bias-T עדיין דלוק).
    return "כיבוי המקלט נכשל — שירות עדיין פעיל", _journal_tail(stuck[0])


# --- רגיסטרי מצבים: קול/ACARS/VDL2/SATCOM שווי-מעמד, off ניטרלי -------------
# תפיסת ההפעלה: ה-SDR הוא משאב, ארבעת המצבים הם "אפליקציות" שוות-מעמד שמתחרות
# עליו, ו-airam-web הוא המתזמר. אין "מצב ראשי" ואין fallback לקול — כישלון
# כניסה למצב נופל ל-off (standby) עם שגיאה ברורה.
MODE_SERVICE = {"voice": "rtl_airband", "acars": ACARS_SERVICE, "vdl2": VDL2_SERVICE,
                "satcom": SATCOM_SERVICE}


def _live_mode():
    """המצב שרץ בפועל (לפי השירותים), או None כשאף צרכן לא פעיל.
    קול נבדק ראשון: Conflicts ב-systemd מבטיח בלעדיות הדדית, אז אם rtl_airband
    פעיל אין טעם לבדוק את השאר — חוסך קריאות systemctl במצב הנפוץ."""
    for m in ("voice", "vdl2", "acars", "satcom"):
        if _is_active(MODE_SERVICE[m]):
            # קול + ACARS: rtl_airband על הראשי *וגם* acarsdec על המשני. רק אחד מהם ⇒
            # "voice" (ACARS נפל) — api_state/health מסמנים את זה כתקלה מול המצב השמור.
            if m == "voice":
                for cm, svc in COMPOSITE_SECONDARY.items():
                    if _is_active(svc):
                        return cm
            return m
    return None


def _enter_voice(params, keep_secondary=False):
    """כניסה סימטרית לקול (peer של _enter_acars/_enter_vdl2/_enter_satcom): עוצר
    את צרכני הדאטה, כותב את קונפיג rtl_airband ומרים עם אימות.
    מחזיר (error, detail, sdr_down) — כמו _restart_and_verify."""
    # אם acarsdec/dumpvdl2/inmarsat-sniffer רץ הוא מחזיק את ה-SDR => עוצרים
    # מפורשות לפני שמרימים את rtl_airband (Conflicts גיבוי, אבל זה משחרר את
    # המכשיר מיד).
    # keep_secondary: כיוונון קול בתוך "קול + ACARS" — acarsdec על המקלט השני ממשיך.
    _stop_services([svc for svc in (ACARS_SERVICE, VDL2_SERVICE, SATCOM_SERVICE) if _is_active(svc)])
    # ⚠ צרכני המשני נעצרים *בלי* תנאי is-active: אין להם Conflicts מול rtl_airband, אז זה
    # המקום היחיד שעוצר אותם — ומשני ב-"activating" (השער / sleep 10 / RestartSec) היה
    # שורד את היציאה מהמצב הכפול, עולה אחר כך, ומציג מצב כפול "תקין" שהמשתמש עזב.
    if not keep_secondary:
        _stop_services(SECONDARY_SERVICES)
    # chan_filter/fm_notch אופציונליים: בדיקת האנטנה והניסוי (_probe_params/_exp_voice_params)
    # לא מעבירים אותם ⇒ רצים תמיד בתנאים הקבועים (standard, בלי notch) — בר-השוואה לבסיס.
    write_config(params["freq"], params["mod"], params["agc"], params["if_gain"],
                 params["rf_gain"], params["squelch_mode"], params["squelch_snr"],
                 params.get("chan_filter", config.CHAN_FILTER_DEFAULT), bool(params.get("fm_notch")))
    res = _restart_and_verify()
    if res[0] is None:
        # אחרי ש-rtl_airband אומת בלבד, וב-best-effort: כשל בסינון לעולם לא
        # מכשיל כניסה לקול (ר' _denoise_sync). מכסה כיוונון, מעבר מצב, סריקה,
        # שחזור באתחול, reconcile ושחזור אחרי בדיקת אנטנה — כולם עוברים כאן.
        _denoise_after_voice()
    return res


def _wait_voice_settled(since):
    """פתיחה מדורגת: מחכה ש-rtl_airband יכתוב stats *אחרי* `since` (סיים לעלות); המרווח
    הנוסף יושב ביחידה (ExecStartPre=/bin/sleep 10). לא נכשל — אחרי VOICE_STARTUP_WAIT_SEC ממשיכים בכל מקרה (ה-stats
    הם סימן, לא תנאי; הקול כבר אומת ע"י _restart_and_verify)."""
    end = time.monotonic() + VOICE_STARTUP_WAIT_SEC
    while time.monotonic() < end:
        try:
            if STATS_PATH.stat().st_mtime >= since:
                break
        except OSError:
            pass
        time.sleep(0.5)


def _start_acars2(freqs):
    """acarsdec על המקלט המשני. מחזיר (error, detail)."""
    write_acars2_env(freqs)
    return _restart_verified(ACARS2_SERVICE, "הפעלת ACARS על המקלט המשני נתקעה — בדוק שהוא מחובר",
                             "ACARS על המקלט המשני נכשל לעלות — בדוק journalctl -u " + ACARS2_SERVICE,
                             "acarsdec failed")


def _start_voice2(st=None):
    """rtl_airband שני על המקלט המשני (קול כפול). מחזיר (error, detail)."""
    write_voice2_config(st)
    return _restart_verified(VOICE2_SERVICE, "הפעלת הערוץ השני נתקעה — בדוק שהמקלט המשני מחובר",
                             "הערוץ השני נכשל לעלות — בדוק תדר/חיבור, journalctl -u " + VOICE2_SERVICE,
                             "rtl_airband2 failed")


def _start_vdl2b(freqs):
    """dumpvdl2 על המקלט המשני (קול + VDL2). מחזיר (error, detail)."""
    write_vdl2_env(freqs, path=VDL2B_ENV_PATH, serial=_secondary_serial())
    return _restart_verified(VDL2B_SERVICE, "הפעלת VDL2 על המקלט המשני נתקעה — בדוק שהוא מחובר",
                             "VDL2 על המקלט המשני נכשל לעלות — בדוק journalctl -u " + VDL2B_SERVICE,
                             "dumpvdl2 failed")


def _start_secondary(mode, st):
    """מרים את צרכן המשני של מצב מורכב. מחזיר (error, detail). בלי ראשי+משני תקינים —
    מסרב (env/קונפיג בלי serial = "כל מקלט" = עלול לפתוח את הראשי)."""
    rerr = _roles_error(mode)
    if rerr:
        return rerr, None
    if mode == "voice2":
        return _start_voice2(st)
    if mode == "voice_vdl2":
        return _start_vdl2b(st.get("vdl2_freqs"))
    return _start_acars2(st.get("acars_freqs"))


def _enter_composite(mode, params, st):
    """מצב של שני מקלטים: rtl_airband על הראשי, ואחריו — לא במקביל — הצרכן של המשני
    (acarsdec ב-voice_acars, rtl_airband שני ב-voice2). מחזיר (error, detail, sdr_down).
    ⚠ הסדר הוא הבטיחות החשמלית (§7.8): שני rtl_airband שעלו יחד הפילו power bank 5V/3A;
    קודם הקול עולה ומתייצב (stats חדש), והיחידה של המשני מחכה עוד 10ש' בעצמה."""
    rerr = _roles_error(mode)
    if rerr:
        return rerr, None, False
    _stop_services((ACARS_SERVICE, *SECONDARY_SERVICES, VDL2_SERVICE, SATCOM_SERVICE))
    since = time.time()
    err, detail, sdr_down = _enter_voice(params, keep_secondary=True)
    if err:
        return err, detail, sdr_down
    # ⚠ stats שהתהליך *הקודם* כתב רגע לפני שנהרג (מעבר מקול) הם "אחרי since" ⇒ הדירוג
    # החשמלי היה מסתיים מיד. זמן ההפעלה של התהליך הנוכחי מ-systemd מבדיל ביניהם.
    start = _rtl_airband_start_wall()
    _wait_voice_settled(max(since, start) if start else since)
    err, detail = _start_secondary(mode, st)
    if err:
        try:
            _sysctl("stop", COMPOSITE_SECONDARY[mode], timeout=30)
        except Exception:
            pass
        return err, detail, False
    return None, None, False


def _composite_secondary_only(mode, st):
    """True כשהמצב המורכב השמור הוא `mode` והקול על הראשי חי — אז שינוי בצד המשני (בנק ACARS,
    תדר שני) מאתחל *רק* את המשני. כניסה מלאה הייתה עוצרת ומרימה גם את הקול: שמע נקטע ל-~10ש'
    בגלל החלפת תדרי דאטה. מצב אחר (כולל קול רגיל) ⇒ False — כניסה מלאה כרגיל."""
    return st.get("app_mode") == mode and _is_active("rtl_airband")


def _secondary_starting_or_up(mode):
    """המשני של מצב מורכב פעיל *או בדרך* (activating: השער, ה-sleep המדורג, RestartSec).
    systemd כבר מטפל בו ⇒ אין להרים אותו שוב (restart היה חוסם עד 45ש' תחת TUNE_LOCK ומפיל
    את הקול לעוד מחזור פתיחה)."""
    st = _services_status([COMPOSITE_SECONDARY[mode]]).get(COMPOSITE_SECONDARY[mode], "")
    return st in ("active", "activating", "reloading")


def _enter_voice_acars(params, freqs):
    """מצב "קול + ACARS" (שני מקלטים) — ר' _enter_composite."""
    return _enter_composite("voice_acars", params, {"acars_freqs": freqs})


def _enter_voice2(params, st=None):
    """מצב "קול כפול" (שני מקלטים) — ר' _enter_composite."""
    return _enter_composite("voice2", params, st if st is not None else appstate.load_state())


def _enter_voice_vdl2(params, freqs):
    """מצב "קול + VDL2" (שני מקלטים) — ר' _enter_composite."""
    return _enter_composite("voice_vdl2", params, {"vdl2_freqs": freqs})


def _enter_composite_saved(mode, params, st):
    """כניסה מלאה למצב מורכב לפי ה-state השמור (אתחול / reconcile)."""
    if mode == "voice_acars":
        return _enter_voice_acars(params, st.get("acars_freqs"))
    if mode == "voice_vdl2":
        return _enter_voice_vdl2(params, st.get("vdl2_freqs"))
    return _enter_voice2(params, st)


# --- סינון רעשים חי: airam-denoise (docs/denoise-design.md) -----------------
# *לא* צרכן SDR ולא מצב: שירות צד שקורא עותק UDP של השמע ומשדר /clean.mp3.
# airam-web מיישר אותו לכוונה (מתג גלובלי ב-state) — הוא רץ ⇔ המתג דלוק +
# הקול חי + הרכיבים מותקנים. PartOf=rtl_airband ביחידה עוצר/מאתחל אותו יחד
# עם הרדיו, כך שכאן צריך רק *להפעיל* (ולעצור בכיבוי המתג).
# ⚠ החוסן לא תלוי בקוד הזה: Icecast מוגדר עם fallback-mount (install.sh שלב 5),
# כך שמי שמבקש /clean.mp3 כשהסינון לא חי מקבל /live.mp3 — ומי שמאזין כשהסינון
# קורס מועבר אליו בלי ניתוק (נבדק). הקוד כאן אחראי ל*חיווי הכן* (§12): ה-UI
# אומר "מסונן" רק כשה-mount באמת חי — אחרת המשתמש שומע גולמי ויודע את זה.
_DENOISE_LOCK = threading.Lock()   # start/stop מקבילים (POST + reconcile + כניסה לקול)
ICECAST_STATUS_TTL_SEC = 2.0       # /api/denoise ו-/api/health בפולינג מכל טלפון פתוח
_ice_status = {"t": 0.0, "mounts": None}
_ICE_STATUS_LOCK = threading.Lock()


def _denoise_engine(st=None):
    """המנוע השמור, תמיד מתוך הרשימה הסגורה (state ישן/פגום ⇒ ברירת המחדל)."""
    e = (st if st is not None else appstate.load_state()).get("denoise_engine")
    return e if e in config.DENOISE_ENGINES else config.DENOISE_ENGINE_DEFAULT


def _denoise_missing(engine=None):
    """רכיבים חסרים לסינון במנוע הנתון (ברירת מחדל: השמור). רשימה ריקה = מותקן.
    נבדק *חי* בכל קריאה ולא פעם אחת בעלייה — אותו לקח כמו whisper (§12): התקנה
    מאוחרת חייבת להיות מורגשת בלי restart, ו"לא מותקן" חייב להיות מדווח."""
    if engine not in config.DENOISE_ENGINES:
        engine = _denoise_engine()
    missing = [] if shutil.which("ffmpeg") else ["ffmpeg"]
    binary = DENOISE_RNNOISE if engine == "rnmix" else DENOISE_SPEEXDN
    missing += [p.name for p in (DENOISE_SCRIPT, binary) if not os.access(p, os.X_OK)]
    return missing


def write_denoise_env(engine):
    """/etc/airam/denoise.env — EnvironmentFile של airam-denoise.service (systemd,
    לא shell). הערך תמיד מהרשימה הסגורה; הסקריפט גם הוא מסרב לכל דבר אחר."""
    if engine not in config.DENOISE_ENGINES:
        engine = config.DENOISE_ENGINE_DEFAULT
    storage.atomic_write(DENOISE_ENV_PATH, "# נכתב אוטומטית ע\"י AIR-AM (סינון רעשים). שינויים ידניים נדרסים.\n"
                                    f"AIRAM_DENOISE_ENGINE={engine}\n")


def _icecast_live_mounts():
    """קבוצת ה-mounts ש*באמת* משדרים כרגע לפי status-json.xsl של Icecast, או None
    כשאין תשובה. ⚠ mount שהמקור שלו מת נשאר ברשימה כרשומה בלי stream_start
    (נצפה ב-Icecast 2.4.4 כשהיו לו מאזינים שהועברו ל-fallback) — לכן "חי" =
    יש stream_start, לא "מופיע ברשימה". cache קצר: נקרא מכל טלפון בפולינג."""
    now = time.monotonic()
    with _ICE_STATUS_LOCK:
        if now - _ice_status["t"] < ICECAST_STATUS_TTL_SEC:
            return _ice_status["mounts"]
    mounts = None
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{ICECAST_PORT}/status-json.xsl",  # noqa: S310
                                    timeout=2) as r:
            src = json.loads(r.read(256 * 1024)).get("icestats", {}).get("source")
        src = src if isinstance(src, list) else ([src] if isinstance(src, dict) else [])
        mounts = {str(s.get("listenurl", "")).rsplit("/", 1)[-1]
                  for s in src if isinstance(s, dict) and s.get("stream_start")}
    except Exception:
        mounts = None                      # Icecast למטה / JSON שבור — "לא ידוע", לא "כבוי"
    with _ICE_STATUS_LOCK:
        _ice_status.update(t=now, mounts=mounts)
    return mounts


def _denoise_state(enabled, missing, voice_on, service, live):
    """פסק-דין יחיד לחיווי — ה-UI לא מנחש. הערכים:
      off         — המתג כבוי (שומעים גולמי, כמבוקש)
      unavailable — דלוק אבל לא מותקן (שומעים גולמי; install_hint בתשובה)
      idle        — דלוק, אבל הקול לא פעיל — אין מה לסנן
      live        — /clean.mp3 משדר (שומעים מסונן)
      starting    — השירות עולה, ה-mount עוד לא חי (שומעים גולמי בינתיים)
      failed      — השירות לא רץ למרות שאמור (שומעים גולמי)
      unknown     — השירות רץ אבל Icecast לא ענה — לא טוענים "מסונן" בלי ראיה"""
    if not enabled:
        return "off"
    if missing:
        return "unavailable"
    if not voice_on:
        return "idle"
    if live is True:
        return "live"
    if service in ("active", "activating", "reloading"):
        return "unknown" if live is None else "starting"
    return "failed"


def _denoise_status(services=None):
    """מצב הסינון ל-/api/denoise ול-/api/health. services — מפה מוכנה מ-
    _services_status (‏/api/health כבר מחזיק אותה => בלי fork נוסף)."""
    st = appstate.load_state()
    enabled = bool(st.get("denoise"))
    engine = _denoise_engine(st)
    missing = _denoise_missing(engine)
    if services is None:
        services = _services_status(("rtl_airband", DENOISE_SERVICE))
    voice_on = services.get("rtl_airband") == "active"
    service = services.get(DENOISE_SERVICE, "unknown")
    # שאילתת Icecast רק כשיש בה טעם — המתג כבוי הוא המצב הנפוץ
    live = None
    if enabled and voice_on and not missing:
        mounts = _icecast_live_mounts()
        live = None if mounts is None else (DENOISE_MOUNT in mounts)
    engines = []
    for eid, e in config.DENOISE_ENGINES.items():
        m = missing if eid == engine else _denoise_missing(eid)
        engines.append({"id": eid, "label": e["label"], "available": not m, "missing": m})
    return {"enabled": enabled, "engine": engine, "engines": engines,
            "available": not missing, "missing": missing,
            "install_hint": DENOISE_INSTALL_HINT if missing else None,
            "state": _denoise_state(enabled, missing, voice_on, service, live),
            "service": service, "live": live, "mount": DENOISE_MOUNT}


def _denoise_sync():
    """מיישר את airam-denoise לכוונה: רץ ⇔ המתג דלוק + rtl_airband חי + מותקן.
    idempotent, בטוח לקריאה מכל מקום, **לעולם לא זורק** — ולעולם לא נוגע
    ברדיו: גם כשל מלא כאן משאיר את /live.mp3 ואת ההקלטות בדיוק כמו שהיו.
    לא מחזיק TUNE_LOCK (נקרא גם מתוך _enter_voice שכבר מחזיק אותו)."""
    try:
        st = appstate.load_state()
        engine = _denoise_engine(st)
        want = (bool(st.get("denoise")) and not _denoise_missing(engine)
                and _is_active("rtl_airband"))
        with _DENOISE_LOCK:
            cur = _services_status((DENOISE_SERVICE,))[DENOISE_SERVICE]
            running = cur in ("active", "activating", "reloading")
            if want and not running:
                # המנוע נקרא מה-env רק בהפעלה => כותבים אותו ממש לפני (אטומי)
                write_denoise_env(engine)
                r = _sysctl("start", DENOISE_SERVICE, timeout=15)
                if r is not None and getattr(r, "returncode", 0) != 0:
                    log.warning("denoise: start נכשל: %s", (r.stderr or "").strip()[:200])
            elif running and not want:
                _sysctl("stop", DENOISE_SERVICE, timeout=15)
    except Exception:
        log.exception("denoise sync נכשל (מתעלמים — הרדיו לא מושפע)")


def _denoise_after_voice():
    """וו אחרי כניסה מוצלחת לקול. בלי מתג דלוק — אפס פעולות (אף fork): הנתיב
    הזה רץ בכל כיוונון, והמתג הכבוי הוא ברירת המחדל."""
    try:
        if appstate.load_state().get("denoise"):
            _denoise_sync()
    except Exception:
        log.exception("denoise after voice נכשל (מתעלמים)")


def _fail_to_off(st, err, detail, log_prefix):
    """כישלון כניסה למצב => נפילה ל-off (standby) — לעולם לא fallback לקול.
    עוצר את כל הצרכנים (best-effort), שומר state עם off + prev_mode, ומחזיר
    (payload, 500) בחוזה שה-UI מכיר: app_mode/state תמיד off => נחיתה במסך הבית."""
    log.warning("%s failed: %s — falling to standby", log_prefix, err)
    try:
        _enter_standby()   # שגיאה משנית לא מעניינת — ממילא מדווחים על המקורית
    except Exception:
        pass
    new_state = appstate._merge_state({"app_mode": "off", "prev_mode": st.get("app_mode", "off")})
    return {"ok": False, "error": err, "detail": detail,
            "app_mode": "off", "state": new_state}, 500


# --- מצב סריקה/סבב: מחזור אוטומטי בין המצבים לפי לוח זמנים ------------------
# "רגל" (leg) = {"mode": voice/acars/vdl2, "dwell_sec": int, "freqs": [...]?}.
# thread נפרד מסתובב בין הרגלים; נועל TUNE_LOCK רק בזמן מעבר (לא בזמן ההמתנה)
# => עצירה/מעבר מצב ידני של המשתמש מתערבים כמעט מיד, לא ממתינים לרגל שלמה.
# כשל ברגל => דילוג לבאה כמעט מיד; כשל של *כל* הרגלים ברצף (סבב שלם בלי אף
# הצלחה) => נופל ל-off, בדיוק כמו כשל כניסה לכל מצב אחר (אין fallback לקול).
SCAN_DWELL_MIN, SCAN_DWELL_MAX = 10, 3600   # שניות — הגנה מפני ערכים אבסורדיים
SCAN_LEGS_MAX = 8                            # הגנה מפני לוחות ענק
SCAN_WINDOW_RECHECK_SEC = 30   # אחרי סבב שלם בלי אף רגל בחלון שעות — לפני שבודקים שוב
_HHMM_RE = re.compile(r'^([01]\d|2[0-3]):[0-5]\d$')   # "HH:MM" (24h) לחלון שעות פר-רגל


def _leg_active_now(leg):
    """האם הרגל בחלון השעות שלה כרגע (שעון מקומי). בלי active_from/active_to
    בכלל => תמיד פעילה. תומך בחלון שחוצה חצות (למשל 22:00–06:00).
    from==to => חלון של 24 שעות (תמיד פעילה) — לא "אף פעם"; זו הכוונה הסבירה
    של משתמש שממלא את אותה שעה בשני השדות, לא לוח ריק בשקט."""
    frm, to = leg.get("active_from"), leg.get("active_to")
    if not frm or not to:
        return True
    now = time.localtime()
    cur = now.tm_hour * 60 + now.tm_min
    fh, fm = (int(x) for x in frm.split(":"))
    th, tm = (int(x) for x in to.split(":"))
    f, t = fh * 60 + fm, th * 60 + tm
    if f == t:
        return True
    return (f <= cur < t) if f <= t else (cur >= f or cur < t)

_scan_lock = threading.Lock()      # מגן על _scan_thread/_scan_thread_stop/_scan_status
_scan_thread = None
_scan_thread_stop = None           # Event של ה-thread *הפעיל* הנוכחי (לא גלובלי משותף —
                                    # כל thread מקבל Event משלו, כדי שסבב חדש לא "יבטל" ישן)
_scan_status = {"idx": -1, "leg": None, "next_switch_at": None, "plan": [],
                "wait": None}   # "window" (אף רגל לא בחלון — לא תקלה) / "sdr" (ה-SDR לא ב-USB — תקלה)


def _validate_scan_plan(raw):
    """מוודא לוח סריקה: רשימה לא-ריקה (עד SCAN_LEGS_MAX) של רגלים תקינים —
    מצב voice/acars/vdl2 + dwell_sec בטווח סביר + (ל-acars/vdl2) תדרים תקינים
    שנכנסים בחלון דגימה אחד + (אופציונלי) חלון שעות "HH:MM"-"HH:MM" — שניהם
    חייבים להיות תקינים ביחד, אחרת הרגל (וכל הלוח) נדחים. מחזיר לוח מנורמל או
    None אם לא תקין."""
    if not isinstance(raw, list) or not (1 <= len(raw) <= SCAN_LEGS_MAX):
        return None
    plan = []
    for leg in raw:
        if not isinstance(leg, dict):
            return None
        mode = leg.get("mode")
        if mode not in ("voice", "acars", "vdl2"):
            return None
        try:
            dwell = int(leg.get("dwell_sec"))
        except (TypeError, ValueError):
            return None
        if not (SCAN_DWELL_MIN <= dwell <= SCAN_DWELL_MAX):
            return None
        clean = {"mode": mode, "dwell_sec": dwell}
        frm, to = leg.get("active_from"), leg.get("active_to")
        if frm or to:
            if not (isinstance(frm, str) and isinstance(to, str)
                    and _HHMM_RE.match(frm) and _HHMM_RE.match(to)):
                return None
            clean["active_from"], clean["active_to"] = frm, to
        if mode in ("acars", "vdl2") and leg.get("freqs"):
            default = config.ACARS_FREQS_DEFAULT if mode == "acars" else config.VDL2_FREQS_DEFAULT
            wcheck = _acars_window_error if mode == "acars" else _vdl2_window_error
            freqs = _sanitize_freqs(leg.get("freqs"), default)
            if wcheck(freqs):
                return None
            clean["freqs"] = freqs
        plan.append(clean)
    return plan


def _scan_enter_leg(leg):
    """נכנס לרגל בודדת (מצב+תדרים/כיוונון). *לא* נועל TUNE_LOCK — הקורא אחראי
    (עקבי עם _enter_voice/_enter_acars/_enter_vdl2). מחזיר (error, detail)."""
    mode = leg["mode"]
    if mode == "voice":
        params, perr = _parse_tune(appstate.load_state())
        if perr:
            params, _ = _parse_tune(appstate.DEFAULT_STATE)
        err, detail, _sdr_down = _enter_voice(params)
        return err, detail
    key = "acars_freqs" if mode == "acars" else "vdl2_freqs"
    default = config.ACARS_FREQS_DEFAULT if mode == "acars" else config.VDL2_FREQS_DEFAULT
    enter = _enter_acars if mode == "acars" else _enter_vdl2
    freqs = leg.get("freqs") or appstate.load_state().get(key) or default
    return enter(freqs)


def _scan_stop_thread():
    """עוצר את thread הסריקה הפעיל (אם יש) ומחכה שיסיים. אין-אופ אם לא רץ סבב.
    לא נועל TUNE_LOCK — ה-thread עצמו מחזיק אותו רק לזמן קצר בכל מעבר רגל.
    מאפס את _scan_status כשבאמת עצרנו thread — אחרת /api/scan מחזיר לרגע רגל/
    ספירה-לאחור של סבב שכבר בוטל (הקורא ב-api_mode עומד להחליף אותם מיד, אבל
    בין הבקשות הבאות ה-status לא צריך להישאר "מזוהם")."""
    global _scan_thread, _scan_thread_stop
    with _scan_lock:
        thread, stop_evt = _scan_thread, _scan_thread_stop
        _scan_thread = _scan_thread_stop = None
        if stop_evt:
            stop_evt.set()
    if thread and thread.is_alive():
        thread.join(timeout=15)
    if thread:
        with _scan_lock:
            _scan_status.update(wait=None, idx=-1, leg=None, next_switch_at=None)


def _scan_loop(stop_evt, plan, start_idx, first_dwell, consumer_active=False):
    """עטיפה ל-_scan_loop_body: חריגה לא-צפויה (למשל OSError מ-write_config/
    save_state כשהדיסק מלא) הייתה הורגת את ה-thread בשקט, והרגל האחרונה הייתה
    ממשיכה לרוץ לנצח. כאן היא נרשמת, ו-api_state/api_health מדווחים תקלה כי
    ה-thread כבר לא חי (_scan_thread_alive)."""
    try:
        _scan_loop_body(stop_evt, plan, start_idx, first_dwell, consumer_active)
    except Exception:
        log.exception("scan: ה-thread נפל — הסבב נעצר")
        with _scan_lock:
            _scan_status.update(wait=None, idx=-1, leg=None, next_switch_at=None)


def _scan_thread_alive():
    with _scan_lock:
        return _scan_thread is not None and _scan_thread.is_alive()


def _scan_loop_body(stop_evt, plan, start_idx, first_dwell, consumer_active=False):
    """thread: ממתין first_dwell על הרגל שכבר הוכנסה (start_idx-1), ואז מסתובב
    בין שאר רגלי הלוח עד עצירה. stop_evt ייחודי-לקריאה-הזו (לא גלובלי) => סבב
    חדש שמתחיל אחר-כך לא "מבטל בטעות" thread ישן שעדיין מסיים את היציאה.
    consumer_active = האם צרכן SDR רץ בפועל כשה-thread מתחיל (True כשהתחלנו
    ברגל שהוכנסה כבר ע"י _scan_activate; False כשאף רגל לא הייתה בחלון וה-SDR
    נשאר כבוי). רגל מחוץ לחלון השעות שלה מדולגת מיד (לא כשל); סבב שלם בלי אף
    רגל בחלון => מכבים את הצרכן הרץ (אם יש) ומחכים SCAN_WINDOW_RECHECK_SEC לפני
    שבודקים שוב (לא busy-loop) — כך שהחלון-שנסגר-באמצע-סבב באמת משתיק את ה-SDR,
    לא רק את החיווי ב-UI. רגל שזהה בדיוק לרגל שכבר רצה (מצב+תדרים) לא נכנסת
    מחדש — נמנעים מ-restart מיותר של השירות כשהלוח מכיל רק רגל אחת (או רגל
    שחוזרת על עצמה) עם dwell קצר."""
    idx = start_idx
    remaining = first_dwell
    consecutive_fail = 0
    consecutive_skip = 0    # רגלים רצופות שנעדרו-מחלון-שעות — לא כשל, רק "לא עכשיו"
    last_entered = plan[(start_idx - 1) % len(plan)] if consumer_active else None
    while not stop_evt.is_set():
        while remaining > 0 and not stop_evt.is_set():
            step = min(1.0, remaining)
            # ⚠ stop_evt.wait ולא time.sleep: ההמתנה נקטעת *מיידית* כשמבקשים
            # לעצור, במקום להשלים עד שנייה שלמה של שינה לפני שבודקים שוב. זה
            # מקצר מעבר-מצב/עצירת-סריקה שהמשתמש מחכה לו בפועל, ובנוסף הופך את
            # ההמתנה לדטרמיניסטית בבדיקות — no_sleep ממקף את `time.sleep`
            # *הגלובלי* (app.time הוא מודול time עצמו), כך שבדיקה שהסתמכה עליו
            # ניטרלה בטעות גם את ה-sleep של עצמה והפכה תלוית-מזל (ר' §12).
            if stop_evt.wait(step):
                break
            remaining -= step
        if stop_evt.is_set():
            break
        leg = plan[idx % len(plan)]
        if not _leg_active_now(leg):
            consecutive_skip += 1
            idx += 1
            with _scan_lock:
                _scan_status.update(wait="window", idx=-1, leg=None, next_switch_at=None)
            if consecutive_skip >= len(plan):
                # סבב שלם בלי אף רגל בחלון => אין מה לשדר עכשיו, מכבים בפועל
                # (לא רק מסתירים מה-UI) — אחרת הרגל האחרונה שרצה ממשיכה לשדר
                # כל עוד אף רגל אחרת לא נכנסת בפועל. תחת TUNE_LOCK כמו כל שינוי
                # חומרה אחר (כמו _scan_enter_leg למטה) — בלעדיו זה יכול לרוץ
                # בו-זמנית עם /api/antenna/check ולכבות את הצרכן שהבדיקה מודדת.
                if consumer_active:
                    if TUNE_LOCK.acquire(timeout=5):
                        try:
                            log.info("scan: אף רגל לא בחלון השעות — מכבה את הצרכן הפעיל")
                            _enter_standby()
                        finally:
                            TUNE_LOCK.release()
                        consumer_active = False
                        last_entered = None
                    # אחרת: הנעילה תפוסה כרגע (בדיקת אנטנה/מעבר אחר) — לא נוגעים
                    # בצרכן הפעם; consumer_active נשאר True וה-recheck הבא ינסה שוב.
                remaining = SCAN_WINDOW_RECHECK_SEC   # אף רגל לא בחלון — לא רודפים בלולאה
                consecutive_skip = 0
            else:
                remaining = 0   # עוד רגלים לבדוק באותו סבב — ממשיכים מיד
            continue
        consecutive_skip = 0
        if not _sdr_present():
            # ⚠ ניתוק USB באמצע סבב (או אתחול בלי SDR): קודם כל הרגלים נכשלו
            # בזו אחר זו והסריקה נפלה ל-off לצמיתות (ה-reconcile לא מטפל ב-scan)
            # — נגיעה בכבל עצרה את התחנה עד התערבות ידנית. ממתינים ל-SDR כמו
            # לחלון שעות, ו-health מדווח תקלה בינתיים (רגל אמורה לרוץ ואף צרכן
            # לא פעיל). **לא מנסים להיכנס בזמן ההמתנה:** כל ניסיון חוסם עד ~45ש'
            # תחת TUNE_LOCK (השער airam-wait-sdrplay), ומעבר-מצב מה-UI היה מקבל
            # 409 רוב הזמן. עוצרים צרכן שרץ, או יחידה שנשארה בלולאת
            # Restart=always אחרי כניסה שנכשלה.
            if (consumer_active or consecutive_fail) and TUNE_LOCK.acquire(timeout=5):
                try:
                    log.warning("scan: ה-SDR לא נוכח — עוצר וממתין לו (לא נופל ל-off)")
                    _enter_standby()
                finally:
                    TUNE_LOCK.release()
                consumer_active = False
                last_entered = None
                consecutive_fail = 0
            with _scan_lock:
                _scan_status.update(wait="sdr", idx=-1, leg=None, next_switch_at=None)
            remaining = SCAN_WINDOW_RECHECK_SEC
            continue
        # ‏same דורש גם שהצרכן עדיין חי: _mode_reconcile_loop מדלג על scan, כך
        # שבלוח עם רגל יחידה צרכן שקרס (למשל קריסת sdrplay שלא הופצה) היה נשאר
        # מת עד סוף החלון בלי שום ניסיון כניסה מחדש.
        same = (last_entered is not None and last_entered["mode"] == leg["mode"]
                and last_entered.get("freqs") == leg.get("freqs")
                and _is_active(MODE_SERVICE[leg["mode"]]))
        if same:
            # אותה רגל בדיוק כבר רצה (מצב+תדרים) — אין טעם ב-restart של השירות,
            # רק מרעננים את הטיימר. חוסך נתק שמע/הקלטות כל dwell בלוח עם רגל
            # יחידה (או רגלים חוזרות) בעלת חלון שעות.
            with _scan_lock:
                _scan_status.update(wait=None, idx=idx % len(plan), leg=leg,
                                    next_switch_at=time.time() + leg["dwell_sec"])
            remaining = leg["dwell_sec"]
            idx += 1
            continue
        if not TUNE_LOCK.acquire(timeout=5):
            remaining = 1
            continue
        try:
            err, detail = _scan_enter_leg(leg)
        finally:
            TUNE_LOCK.release()
        if err:
            log.warning("scan: leg %d (%s) failed: %s", idx % len(plan), leg["mode"], err)
            consecutive_fail += 1
            if consecutive_fail >= len(plan) and not _role_present("primary"):
                # ה-SDR נעלם *תוך כדי* הסבב: זה ניתוק USB, לא כשל של הלוח —
                # הבדיקה בראש הלולאה תמתין לו (ר' שם). כשל אמיתי (SDR נוכח)
                # עדיין נופל ל-off למטה.
                idx += 1
                remaining = 0
                continue
            if consecutive_fail >= len(plan):
                log.warning("scan: כל הרגלים נכשלו בסבב — נופל ל-off")
                # תחת TUNE_LOCK עד סוף כתיבת ה-state (כולל) — לא רק סביב
                # _enter_standby: מעבר מצב מה-UI שנכנס בין הכיבוי לשמירה היה
                # נדרס ב-"off". השמירה עצמה דרך _merge_state (מפתחות בודדים).
                if not TUNE_LOCK.acquire(timeout=5):
                    log.warning("scan: לא ניתן היה לתפוס TUNE_LOCK לכיבוי — פעולה אחרת כנראה כבר משתלטת")
                    return
                try:
                    _enter_standby()
                    if stop_evt.is_set():
                        return   # מעבר מצב אחר כבר תפס פיקוד בינתיים — לא דורסים את ה-state שלו
                    appstate._merge_state({"app_mode": "off", "prev_mode": "scan"})
                finally:
                    TUNE_LOCK.release()
                with _scan_lock:
                    _scan_status.update(wait=None, idx=-1, leg=None, next_switch_at=None)
                return
            idx += 1
            remaining = 1     # מנסים את הבאה כמעט מיד — לא ממתינים dwell מלא אחרי כשל
            continue
        consecutive_fail = 0
        consumer_active = True
        last_entered = leg
        if stop_evt.is_set():
            return   # נעצרנו בדיוק אחרי כניסה מוצלחת — לא כותבים סטטוס של סבב שכבר בוטל
        with _scan_lock:
            _scan_status.update(wait=None, idx=idx % len(plan), leg=leg,
                                next_switch_at=time.time() + leg["dwell_sec"])
        remaining = leg["dwell_sec"]
        idx += 1


def _scan_activate(plan, wait_for_sdr=False):
    """מפעיל סבב סריקה: מוצא את הרגל הראשונה שבחלון השעות שלה כרגע (אם אין
    לאף רגל חלון — זו הרגל הראשונה, כרגיל) ונכנס אליה (הקורא מחזיק את
    TUNE_LOCK — עקבי עם שאר _enter_*). אם אף רגל לא בחלון כרגע — **לא כשל**:
    ה-SDR נשאר כבוי ומתחיל thread שממתין לחלון הבא (ר' _scan_loop).
    מחזיר (error, detail) — error רק על כשל אמיתי בכניסה לרגל.
    ⚠ עוצר קודם thread סריקה קיים (אם יש) — הקורא מחזיק TUNE_LOCK, אז זה בטוח.
    בלעדיו, קריאה כפולה עם אותו מצב שמור (למשל _boot_restore אחרי שהמשתמש כבר
    התחיל את אותו לוח scan בעצמו בזמן ההמתנה ל-SDR) הייתה דורסת את
    _scan_thread/_scan_thread_stop הגלובליים ומשאירה thread ישן שרץ ללא-הפניה
    ובלתי-ניתן-לעצירה יותר (שני threads מסתובבים בין רגליים בו-זמנית)."""
    global _scan_thread, _scan_thread_stop
    _scan_stop_thread()
    active_idx = next((i for i, leg in enumerate(plan) if _leg_active_now(leg)), None)
    if active_idx is None:
        # ⚠ עוצרים את הצרכן שכבר רץ (SATCOM/קול/...): ה-thread מתחיל עם
        # consumer_active=False ולכן לעולם לא היה עוצר אותו בעצמו. בלי זה,
        # לוח שכל רגליו מחוץ לחלון השאיר את SATCOM רץ עם bias-T דלוק עד
        # שנפתח חלון — בזמן שה-UI מציג "ממתין" ו-health מדווח תקין.
        err, detail = _enter_standby()
        if err:
            return err, detail
        stop_evt = threading.Event()
        thread = threading.Thread(target=_scan_loop, args=(stop_evt, plan, 0, 0, False), daemon=True)
        with _scan_lock:
            _scan_status.update(wait="window", idx=-1, leg=None, next_switch_at=None, plan=plan)
            _scan_thread, _scan_thread_stop = thread, stop_evt
        thread.start()
        return None, None
    err, detail = _scan_enter_leg(plan[active_idx])
    if err and wait_for_sdr and not _role_present("primary"):
        # שחזור באתחול בלי SDR (wait_for_sdr — רק מ-_boot_restore; כניסה ידנית
        # מה-UI ממשיכה לקבל שגיאה מיידית): במקום ליפול ל-off, מתחילים את ה-thread
        # כשהוא ממתין — הוא ינסה שוב את אותה רגל אחרי SCAN_WINDOW_RECHECK_SEC,
        # ו-_scan_loop עצמו ממתין כל עוד ה-SDR חסר. health מדווח תקלה בינתיים
        # (רגל אמורה לרוץ ואף צרכן לא פעיל). עוצרים קודם את היחידה שנכשלה —
        # אחרת Restart=always שלה היה ממשיך לנסות במקביל ל-thread.
        log.warning("scan: SDR לא נוכח — הסבב ימתין לו במקום ליפול ל-off")
        _enter_standby()
        stop_evt = threading.Event()
        thread = threading.Thread(target=_scan_loop,
                                  args=(stop_evt, plan, active_idx, SCAN_WINDOW_RECHECK_SEC, False),
                                  daemon=True)
        with _scan_lock:
            _scan_status.update(wait="sdr", idx=-1, leg=None, next_switch_at=None, plan=plan)
            _scan_thread, _scan_thread_stop = thread, stop_evt
        thread.start()
        return None, None
    if err:
        return err, detail
    stop_evt = threading.Event()
    thread = threading.Thread(target=_scan_loop,
                              args=(stop_evt, plan, active_idx + 1, plan[active_idx]["dwell_sec"], True),
                              daemon=True)
    with _scan_lock:
        _scan_status.update(wait=None, idx=active_idx, leg=plan[active_idx],
                            next_switch_at=time.time() + plan[active_idx]["dwell_sec"], plan=plan)
        _scan_thread, _scan_thread_stop = thread, stop_evt
    thread.start()
    return None, None


# --- נתיבים ----------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/live.m3u")
def live_playlist():
    """Playlist המצביע על סטרים ה-Icecast. פתיחה בנגן שמע חיצוני (VLC וכו')
    מנגנת ברקע בצורה חסינה, ללא תלות בדפדפן."""
    host = request.host.split(":", 1)[0]          # רק ה-hostname, בלי פורט ה-web
    # המתג גלובלי => גם הנגן החיצוני מקבל את המסונן כשהוא דלוק. בטוח גם כשהסינון
    # לא חי: Icecast מגיש את /live.mp3 למבקשי /clean.mp3 (fallback-mount).
    mount = DENOISE_MOUNT if appstate.load_state().get("denoise") else MOUNT
    url = f"http://{host}:{ICECAST_PORT}/{mount}"
    body = "#EXTM3U\n#EXTINF:-1,AIR-AM live\n" + url + "\n"
    return app.response_class(body, mimetype="audio/x-mpegurl")


@app.route("/stream")
def stream_proxy():
    """Reverse-proxy לסטרים ה-Icecast, same-origin => עובד גם בדף HTTPS בלי
    mixed-content. נחוץ כשהדף מוגש ב-HTTPS (למשל מאחורי 'tailscale serve'):
    סטרים HTTP ישיר מ-Icecast היה נחסם. ב-HTTP/LAN הנגן ניגש ל-Icecast ישירות."""
    # ?m=clean => הסטרים המסונן. רשימה סגורה (לא שם mount חופשי מהלקוח) — אין
    # דרך להפוך את ה-proxy לגישה לכל נתיב ב-Icecast המקומי. /clean.mp3 שאינו חי
    # מוגש ע"י Icecast מ-/live.mp3 (fallback-mount) => אין כאן מקרה קצה נוסף.
    mount = {"clean": DENOISE_MOUNT, "live2": MOUNT2}.get(request.args.get("m"), MOUNT)
    upstream = f"http://127.0.0.1:{ICECAST_PORT}/{mount}"
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
    st = appstate.load_state()
    # מקור-אמת למצב = המציאות (השירות הפעיל), ובאין צרכן פעיל — הכוונה השמורה.
    # אין ברירת-מחדל לקול: מצב שמור שאמור לרוץ אבל לא רץ מדווח כתקלה (mode_ok)
    # במקום להעמיד פנים שאנחנו בקול. _live_mode בודק את rtl_airband ראשון
    # (אופטימיזציית Conflicts — ראה שם).
    live = _live_mode()
    saved = st.get("app_mode", "off")
    if saved == "scan":
        # סריקה: "המצב" הוא scan עצמו (לא הרגל הנוכחית) — הרגל/הספירה לאחור
        # מגיעות מ-/api/scan. תקין כל עוד *איזשהו* צרכן פעיל (הרגל הנוכחית), *או*
        # שאף רגל לא אמורה לרוץ כרגע (כולן מחוץ לחלון השעות שלהן — "ממתין", לא תקלה).
        plan = st.get("scan_plan") or []
        any_due = any(_leg_active_now(leg) for leg in plan) if plan else True
        st["app_mode"] = "scan"
        # thread שמת (חריגה) = אף אחד לא מסובב את הרגלים — תקלה, גם אם צרכן עוד רץ
        st["mode_ok"] = _scan_thread_alive() and ((live is not None) or not any_due)
    elif saved in COMPOSITE_SECONDARY and live in ("voice", saved, None):
        # מצב של שני מקלטים: תקין רק כששני הצרכנים רצים. קול בלבד = הצרכן על המשני נפל.
        st["app_mode"] = saved
        st["mode_ok"] = live == saved
    else:
        st["app_mode"] = live or saved
        # mode_ok=False: המצב השמור אמור להריץ צרכן ואף אחד לא רץ (קריסה / עליית
        # מערכת / _boot_restore עוד בדרך). True גם ב-off — standby מכוון אינו תקלה.
        st["mode_ok"] = (live is not None) or (saved == "off")
    st["roles_ready"] = _roles_error() is None
    st["mount2"] = MOUNT2
    st.update(presets=load_presets(), mount=MOUNT, port=ICECAST_PORT, version=VERSION,
              denoise_mount=DENOISE_MOUNT, denoise_available=not _denoise_missing(),
              denoise_engine=_denoise_engine(st),
              acars_banks=config.ACARS_BANKS, vdl2_banks=config.VDL2_BANKS, satcom_banks=config.SATCOM_BANKS)
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
    storage.atomic_write(PRESETS_PATH, json.dumps(cleaned, ensure_ascii=False))
    log.info("presets updated (%d items, from %s)", len(cleaned), request.remote_addr)
    return jsonify(ok=True, presets=cleaned)


# --- חיווי SDR: מזוהה? פנוי? (GET /api/sdr) ---------------------------------
# שתי שאלות נפרדות, כל אחת עם מקור-אמת משלה:
#   "מזוהה" — ה-RSP נוכח ב-USB (lsusb, vendor 1df7), בלי לפתוח אותו.
#   "פנוי"  — ה-SDRplay API מוכן למסור אותו *עכשיו*. sdrplay_api_GetDevices לא
#             מחזיר מכשיר שלקוח אחר כבר בחר (SelectDevice) — אומת במקור של
#             SoapySDRPlay3: ‏findSDRPlay מוסיף ידנית את המכשירים שהתהליך *שלו*
#             תפס (SoapySDRPlay_getClaimedSerials), והערה ב-Settings.cpp מתארת
#             "probe for an absent or already-claimed device" כמקרה של "no
#             sdrplay device matches". כלומר USB נוכח + find ריק = מישהו אחר מחזיק.
# ה-probe הוא אותו `SoapySDRUtil --find` שהשער airam-wait-sdrplay כבר מריץ לפני כל
# צרכן: Open+LockDeviceApi+GetDevices+Unlock — בלי SelectDevice, כך שהוא לא תופס
# את המכשיר בעצמו. רץ רק כשאף צרכן שלנו לא פעיל (אחרת התשובה ידועה: שלנו),
# לא תחת TUNE_LOCK (מעבר-מצב באמצע), ועם cache קצר — כי יש בו fork וטעינת מודולים.
SDR_PROBE_TTL_SEC = 10.0
SDR_PROBE_TIMEOUT_SEC = 12
SDR_API_SERVICE = "sdrplay"
SDR_CONSUMERS = ("rtl_airband", ACARS_SERVICE, VDL2_SERVICE, SATCOM_SERVICE, *SECONDARY_SERVICES)
_SDR_CONSUMER_MODE = {**{svc: m for m, svc in MODE_SERVICE.items()},
                      **{svc: m for m, svc in COMPOSITE_SECONDARY.items()}}
# מצבי systemd שבהם הצרכן שלנו מחזיק (או מנסה להחזיק) את המכשיר: "activating"
# כולל לולאת auto-restart של Restart=always — ה-SDR לא פנוי לאחרים גם אז.
_SDR_HOLDING_STATES = ("active", "activating", "reloading", "deactivating")
# תוכנות SDR מוכרות, לפי /proc/<pid>/comm (15 תווים לכל היותר — לכן "inmarsat-sniffe").
# ⚠ רמז לפי שם בלבד, לא הוכחה: comm קריא לכל משתמש, אבל /proc/<pid>/maps של
# תהליך root לא קריא ל-airam, אז אין דרך לבדוק מי באמת טען את ה-API.
SDR_SUSPECT_NAMES = frozenset({
    "rtl_airband", "acarsdec", "dumpvdl2", "inmarsat-sniffe", "sdrpp", "SDRconnect",
    "CubicSDR", "gqrx", "SoapySDRServer", "rx_sdr", "rx_fm", "rx_tools", "dump1090",
    "dump1090-fa", "readsb", "jaero", "welle-cli", "GNURadio", "gnuradio-compan"})
_sdr_probe_cache = {"t": 0.0, "result": None}
_SDR_PROBE_LOCK = threading.Lock()


def _sdr_usb():
    """(present, desc): present=None כשאי אפשר לבדוק (אין lsusb) — לא ניחוש.
    ⚠ בכוונה שונה מ-_sdr_present שמניח True בכשל: שם זו החלטת רולבק, כאן זה
    חיווי למשתמש, ו"מזוהה" שלא נבדק הוא בדיוק ההמצאה ש-§12 אוסר."""
    present, desc, _n = _sdr_usb_full()
    return present, desc


def _sdr_usb_full():
    """(present, desc, count) — count = כמה מקלטי SDRplay ב-USB (None כשאין lsusb)."""
    try:
        r = subprocess.run(["lsusb", "-d", "1df7:"], capture_output=True, text=True, timeout=5)
    except Exception:
        return None, None, None
    if r.returncode != 0:
        return False, None, 0
    lines = [l for l in r.stdout.splitlines() if l.strip()]
    line = (lines or [""])[0]
    m = re.search(r"ID\s+([0-9a-fA-F]{4}:[0-9a-fA-F]{4})\s*(.*)", line)
    return True, ((m.group(2).strip() or m.group(1)) if m else line.strip() or None), len(lines) or 1


def _parse_sdr_find(text):
    """פלט SoapySDRUtil --find ⇒ [{serial, model, label}]. התווית של SoapySDRPlay3:
    "SDRplay Dev<N> <model> <serial>" (Registration.cpp)."""
    devs, cur = [], None
    for line in (text or "").splitlines():
        if line.startswith("Found device"):
            cur = {}
            devs.append(cur)
            continue
        m = re.match(r"\s+(\w+)\s*=\s*(.*?)\s*$", line)
        if m and cur is not None:
            cur[m.group(1)] = m.group(2)
    out = []
    for d in devs:
        serial = _sanitize_serial(d.get("serial"))
        if d.get("driver") != "sdrplay" or not serial:
            continue
        mm = re.match(r"SDRplay Dev\d+ (\S+) ", d.get("label", "") + " ")
        out.append({"serial": serial, "model": mm.group(1) if mm else None,
                    "label": d.get("label") or None})
    return out


def _sdr_probe_api():
    """שואל את ה-API אם יש מכשיר SDRplay פנוי. מחזיר dict עם result:
    found (+label) / none / api_error (+detail) / timeout / no_tool."""
    try:
        r = subprocess.run(["SoapySDRUtil", "--find=driver=sdrplay"],
                           capture_output=True, text=True, timeout=SDR_PROBE_TIMEOUT_SEC)
    except FileNotFoundError:
        return {"result": "no_tool"}
    except subprocess.TimeoutExpired:
        return {"result": "timeout"}
    except OSError as e:
        return {"result": "api_error", "detail": str(e)}
    out, err = r.stdout or "", r.stderr or ""
    # לא לפי returncode: הגרסה הארוזה ב-Debian עשויה להיות ישנה מזו שנבדקה;
    # הפלט "driver = sdrplay" הוא אותו תנאי בדיוק כמו ב-airam-wait-sdrplay.
    if "driver = sdrplay" in out:
        m = re.search(r"^\s*label\s*=\s*(.+)$", out, re.M)
        return {"result": "found", "label": m.group(1).strip() if m else None}
    # sdrplay_api_Open נכשל (daemon תקוע/לא עונה) — SoapySDRPlay3 רושם אותו ב-stderr
    # וזורק; enumerate תופס. זו תקלת API, לא "תפוס" — חייבים להבדיל ביניהם.
    if re.search(r"sdrplay_api_Open|ApiVersion|ServiceNotResponding", err):
        lines = [l.strip() for l in err.splitlines() if "sdrplay" in l.lower()]
        return {"result": "api_error", "detail": (lines[-1] if lines else err.strip())[:200]}
    return {"result": "none"}


def _sdr_suspects(proc="/proc"):
    """תהליכים שהשם שלהם תואם תוכנת SDR מוכרת (רמז בלבד, ר' SDR_SUSPECT_NAMES)."""
    out = []
    me = os.getpid()
    try:
        entries = os.listdir(proc)
    except OSError:
        return out
    for pid in entries:
        if not pid.isdigit() or int(pid) == me:
            continue
        try:
            with open(f"{proc}/{pid}/comm", encoding="utf-8", errors="replace") as f:
                name = f.read().strip()
        except OSError:
            continue                   # התהליך הסתיים בינתיים / hidepid
        if name in SDR_SUSPECT_NAMES:
            out.append({"pid": int(pid), "name": name})
    return sorted(out, key=lambda p: p["pid"])[:10]


def _sdr_holder(services):
    """הצרכן שלנו שמחזיק את ה-SDR כרגע: (mode, state) או (None, None)."""
    # מצב כפול קודם: rtl_airband ראשון ב-SDR_CONSUMERS, ובלי זה החיווי אמר "קול" גם כששני
    # המקלטים תפוסים (ACARS/ערוץ שני על המשני)
    if services.get("rtl_airband") in _SDR_HOLDING_STATES:
        for mode, svc in COMPOSITE_SECONDARY.items():
            if services.get(svc) in _SDR_HOLDING_STATES:
                return mode, services.get("rtl_airband")
    for svc in SDR_CONSUMERS:
        st = services.get(svc)
        if st in _SDR_HOLDING_STATES:
            return _SDR_CONSUMER_MODE.get(svc), st
    return None, None


def _sdr_status():
    """מצב ה-SDR למשתמש. state:
    missing — לא ב-USB · ours — צרכן של AIR-AM מחזיק בו · switching — מעבר-מצב
    באמצע · api_down — שירות sdrplay לא פעיל · api_error — ה-API לא עונה ·
    free — ה-API מציע אותו · busy — ב-USB אבל ה-API לא מציע: תוכנה אחרת מחזיקה ·
    unavailable — ה-API לא מציע, ואין lsusb לדעת אם הוא בכלל מחובר ·
    checking — probe ראשון עוד רץ (בקשה מקבילה) ·
    unknown — אין כלי לבדוק "פנוי" (SoapySDRUtil חסר) — לא ממציאים תשובה."""
    usb, desc, count = _sdr_usb_full()
    primary = _primary_serial()
    res = {"ok": True, "usb": usb, "usb_desc": desc, "state": None, "mode": None,
           "service_state": None, "api": None, "label": None, "detail": None,
           "suspects": [], "checked_age": None,
           # שני מקלטים בלי ראשי שנבחר ⇒ איזה מהם נפתח נקבע במקרה (§3.1 במסמך)
           "usb_count": count, "primary": primary, "secondary": _secondary_serial(),
           "roles_needed": bool(count and count > 1 and not primary)}
    if usb is False:
        res["state"] = "missing"
        return res
    services = _services_status((SDR_API_SERVICE, *SDR_CONSUMERS))
    res["api"] = services.get(SDR_API_SERVICE)
    mode, sst = _sdr_holder(services)
    if mode:
        res.update(state="ours", mode=mode, service_state=sst)
        return res
    if res["api"] != "active":
        res["state"] = "api_down"
        return res
    if TUNE_LOCK.locked():
        res["state"] = "switching"
        return res
    # probe אחד בכל רגע. טלפון שני שמגיע באמצע מקבל את התשובה האחרונה במקום
    # להמתין עד SDR_PROBE_TIMEOUT_SEC (ה-UI מוותר אחרי 8ש' — NET_TIMEOUT_GET).
    if _SDR_PROBE_LOCK.acquire(blocking=False):
        try:
            cached = _sdr_probe_cache["result"]
            if cached is None or time.monotonic() - _sdr_probe_cache["t"] >= SDR_PROBE_TTL_SEC:
                cached = _sdr_probe_api()
                _sdr_probe_cache.update(t=time.monotonic(), result=cached)
        finally:
            _SDR_PROBE_LOCK.release()
    else:
        cached = _sdr_probe_cache["result"]
        if cached is None:
            res["state"] = "checking"
            return res
    res["checked_age"] = round(time.monotonic() - _sdr_probe_cache["t"], 1)
    # צרכן שלנו עלה *בזמן* ה-probe (מעבר מצב מטלפון אחר) => "לא נמצא" שלו הוא
    # אנחנו, לא תוכנה זרה. בודקים שוב לפני שמאשימים מישהו.
    mode, sst = _sdr_holder(_services_status(SDR_CONSUMERS))
    if mode:
        res.update(state="ours", mode=mode, service_state=sst, checked_age=None)
        return res
    kind = cached["result"]
    if kind == "found":
        res.update(state="free", label=cached.get("label"))
    elif kind == "none":
        # בלי lsusb לא ידוע אם המכשיר בכלל מחובר — "לא מוצע" בלבד, לא "תפוס".
        res["state"] = "busy" if usb else "unavailable"
        res["suspects"] = _sdr_suspects()
    elif kind in ("api_error", "timeout"):
        res.update(state="api_error",
                   detail=cached.get("detail") or "ה-API לא ענה תוך %d ש׳" % SDR_PROBE_TIMEOUT_SEC)
    else:
        res["state"] = "unknown"
    return res


@app.route("/api/sdr")
def api_sdr():
    """חיווי SDR: מזוהה ב-USB? פנוי (ה-API מציע אותו)? ומי מחזיק בו אם לא."""
    return jsonify(_sdr_status())


@app.route("/api/sdr/devices")
def api_sdr_devices():
    """המקלטים שה-API מציע עכשיו + התפקידים השמורים. ⚠ מקלט שצרכן (שלנו או זר)
    מחזיק לא מופיע ב-find — לכן `held` מסמן שהרשימה חלקית, לא שהוא "לא קיים"."""
    st = appstate.load_state()
    held, _ = _sdr_holder(_services_status(SDR_CONSUMERS))
    try:
        r = subprocess.run(["SoapySDRUtil", "--find=driver=sdrplay"],
                           capture_output=True, text=True, timeout=SDR_PROBE_TIMEOUT_SEC)
        devices, err = _parse_sdr_find(r.stdout), None
    except FileNotFoundError:
        devices, err = [], "SoapySDRUtil לא מותקן"
    except (subprocess.TimeoutExpired, OSError):
        devices, err = [], "ה-API לא ענה"
    _usb, _desc, count = _sdr_usb_full()
    return jsonify({"ok": err is None, "error": err, "devices": devices, "usb_count": count,
                    "held_by": held,
                    "primary": _sanitize_serial(st.get("sdr_primary")),
                    "secondary": _sanitize_serial(st.get("sdr_secondary"))})


@app.route("/api/sdr/roles", methods=["POST"])
def api_sdr_roles():
    """{primary?, secondary?}: serial או null (=נקה). נכנס לתוקף בכניסה הבאה למצב —
    לא מאתחל את מה שרץ (לא נוגעים ברדיו מתחת לרגליים של מאזין)."""
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict) or not ({"primary", "secondary"} & set(data)):
        return jsonify({"ok": False, "error": "צריך primary ו/או secondary"}), 400
    changes = {}
    for key in ("primary", "secondary"):
        if key not in data:
            continue
        v = data[key]
        if v is None or v == "":
            changes["sdr_" + key] = None
            continue
        sv = _sanitize_serial(v)
        if not sv:
            return jsonify({"ok": False, "error": f"serial לא תקין: {key}"}), 400
        changes["sdr_" + key] = sv
    # ⚠ תחת TUNE_LOCK: כיוונון/רולבק/reconcile/tune2 קוראים את ה-serial *מחדש* בכל כתיבת
    # קונפיג. החלפת תפקידים באמצע מצב כפול הפנתה את הקונפיג של הראשי למקלט שה-acarsdec
    # (המשני) מחזיק ⇒ הכיוונון הבא והרולבק שלו נכשלו ⇒ off. גם שתי בקשות מקבילות יכלו
    # לשמור ראשי == משני (הבדיקה רצה על snapshot) — הנעילה מסדרת גם אותן.
    if not TUNE_LOCK.acquire(blocking=False):
        return jsonify({"ok": False, "error": "פעולה אחרת מתבצעת — נסה שוב בעוד רגע"}), 409
    try:
        st = appstate.load_state()
        if st.get("app_mode") in COMPOSITE_SECONDARY or any(_is_active(s2) for s2 in SECONDARY_SERVICES):
            return jsonify({"ok": False, "error": "מצב של שני מקלטים פעיל — עבור לקול רגיל או לכיבוי "
                            "לפני שינוי תפקידי המקלטים"}), 409
        prim = changes.get("sdr_primary", st.get("sdr_primary"))
        sec = changes.get("sdr_secondary", st.get("sdr_secondary"))
        if prim and sec and prim == sec:
            return jsonify({"ok": False, "error": "אותו מקלט לא יכול להיות גם ראשי וגם משני"}), 400
        new = appstate._merge_state(changes)
        try:
            write_sdr_env(new.get("sdr_primary"), new.get("sdr_secondary"))
        except OSError as e:
            log.warning("sdr.env write failed: %s", e)
    finally:
        TUNE_LOCK.release()
    _sdr_probe_cache.update(t=0.0, result=None)
    return jsonify({"ok": True, "primary": new.get("sdr_primary"),
                    "secondary": new.get("sdr_secondary"),
                    "applies": "במעבר המצב הבא"})


@app.route("/api/health")
def api_health():
    """סטטוס המערכת — מאפשר ל-UI להבדיל בין "אין שידור" ל"משהו נפל"."""
    return jsonify(_health_snapshot())


def _health_snapshot():
    """הלב של /api/health כ-dict — משותף גם ליומן הבדיקה (_diag_sample) ולייצוא."""
    services = _services_status(HEALTH_SERVICES)
    try:
        stats_age = round(time.time() - STATS_PATH.stat().st_mtime, 1)
    except OSError:
        stats_age = None     # עוד לא נכתב (rtl_airband לא עלה / זה עתה הופעל)
    # תקין בכל המצבים: קול (rtl_airband+icecast) / ACARS (airam-acars) / VDL2
    # (airam-vdl2) / SATCOM (airam-satcom) — אחרת מצב דאטה (שבו rtl_airband
    # מכובה מבחירה) היה נראה כתקלה.
    voice_ok = services["rtl_airband"] == "active" and services["icecast2"] == "active"
    acars_ok = services["airam-acars"] == "active"
    vdl2_ok = services["airam-vdl2"] == "active"
    satcom_ok = services["airam-satcom"] == "active"
    acars2_on = services.get("airam-acars2") == "active"
    voice2_on = services.get(VOICE2_SERVICE) == "active"
    vdl2b_on = services.get(VDL2B_SERVICE) == "active"
    # standby מכוון: כל הצרכנים כבויים ו-state מסומן off => תקין, *לא* תקלה (אחרת
    # מצב הכיבוי שביקש המשתמש היה נראה כקריסה). sdrplay נשאר active במפה.
    saved_state = appstate.load_state()
    saved = saved_state.get("app_mode", "off")
    off_ok = (saved == "off"
              and services["rtl_airband"] != "active"
              and services["airam-acars"] != "active"
              and services["airam-vdl2"] != "active"
              and services["airam-satcom"] != "active"
              and not acars2_on and not voice2_on and not vdl2b_on)
    # המצב נגזר מהשירות הפעיל, ובאין פעיל — מהכוונה השמורה (אין ברירת-מחדל לקול).
    # ok = בריאות המצב הנגזר בלבד: מצב שמור שלא רץ => ok=False (תקלה מדווחת),
    # למשל אחרי קריסת שירות או בזמן ש-_boot_restore עוד מחזיר את המצב.
    active = ("vdl2" if services["airam-vdl2"] == "active"
              else "acars" if services["airam-acars"] == "active"
              else "satcom" if services["airam-satcom"] == "active"
              else ("voice_acars" if acars2_on else "voice2" if voice2_on
                    else "voice_vdl2" if vdl2b_on else "voice")
              if services["rtl_airband"] == "active" else None)
    if saved == "scan":
        # סריקה: תקין כל עוד הרגל הנוכחית (איזשהו צרכן) פועלת, *או* שאף רגל לא
        # אמורה לרוץ כרגע (חלון שעות) — "ממתין" אינו תקלה.
        plan = saved_state.get("scan_plan") or []
        any_due = any(_leg_active_now(leg) for leg in plan) if plan else True
        mode, ok = "scan", _scan_thread_alive() and ((active is not None) or not any_due)
    elif saved in COMPOSITE_SECONDARY and active in ("voice", saved, None):
        mode, ok = saved, voice_ok and services.get(COMPOSITE_SECONDARY[saved]) == "active"
    else:
        mode = active or saved
        ok = (voice_ok if mode in ("voice", *COMPOSITE_SECONDARY) else acars_ok if mode == "acars"
              else vdl2_ok if mode == "vdl2" else satcom_ok if mode == "satcom" else off_ok)
    # הסינון לא נכנס ל-ok בכוונה: כשל בו לא פוגע ברדיו (Icecast מגיש /live.mp3
    # למבקשי /clean.mp3), ו"תקלה" אדומה במסך הבית בגללו הייתה שולחת לבדוק SDR.
    return dict(ok=ok, app_mode=mode,
                services=services, sdr_present=_sdr_present(), stats_age=stats_age,
                denoise=_denoise_status(services))


@app.route("/api/denoise", methods=["GET", "POST"])
def api_denoise():
    """סינון רעשים חי (docs/denoise-design.md, docs/audio-comfort-design.md). GET:
    מצב (ר' _denoise_state). POST {enabled?: bool, engine?: str} (לפחות אחד):
    מתג גלובלי ו/או מנוע (state) + יישור מיידי של השירות. לא נוגע ברדיו ולא
    ב-TUNE_LOCK — הפלט ל-UDP קיים בקונפיג תמיד, אז אין restart לרדיו. החלפת
    מנוע בזמן ריצה = stop+start ל-airam-denoise בלבד (Icecast מגיש את הגולמי
    בשנייה שבאמצע). דרך _guard (POST)."""
    if request.method == "POST":
        data = request.get_json(silent=True)
        if (not isinstance(data, dict) or not ({"enabled", "engine"} & data.keys())
                or ("enabled" in data and not isinstance(data["enabled"], bool))
                or ("engine" in data and data["engine"] not in config.DENOISE_ENGINES)):
            return jsonify(ok=False, error="נדרש {\"enabled\": true|false} ו/או {\"engine\": "
                           + "|".join(config.DENOISE_ENGINES) + "}"), 400
        st = appstate.load_state()
        enabled = data.get("enabled", bool(st.get("denoise")))
        engine = data.get("engine", _denoise_engine(st))
        missing = _denoise_missing(engine)
        if enabled and missing:
            # לא שומרים "דלוק" שלא יכול לעבוד — מחזירים את הפקודה שמתקנת (כמו whisper)
            return jsonify(ok=False, error="סינון הרעשים לא מותקן (חסר: " + ", ".join(missing) + ")",
                           **_denoise_status()), 501
        changed_engine = engine != _denoise_engine(st)
        appstate._merge_state({"denoise": enabled, "denoise_engine": engine})
        log.info("denoise %s engine=%s (from %s)", "on" if enabled else "off", engine,
                 request.remote_addr)
        if changed_engine:
            # המנוע נקרא רק בהפעלה — עוצרים, ו-_denoise_sync מפעיל עם ה-env החדש
            with _DENOISE_LOCK:
                if _services_status((DENOISE_SERVICE,))[DENOISE_SERVICE] in (
                        "active", "activating", "reloading"):
                    _sysctl("stop", DENOISE_SERVICE, timeout=15)
        _denoise_sync()
    return jsonify(ok=True, **_denoise_status())


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
            try:
                vals[m.group(1)] = float(m.group(3))
            except ValueError:
                # ⚠ התבנית (-?[0-9.]+) מקבלת גם מחרוזות שאינן מספר — "."‏, ".."‏,
                # "1.2.3" — ו-float עליהן זרק ValueError *לא-מטופל*. הקובץ יושב
                # ב-tmpfs ונכתב ~פעם בשנייה בזמן שאנחנו קוראים אותו, כך שקריאה
                # קרועה היא תרחיש אמיתי. הנפילה לא הייתה מקומית: parse_stats
                # מזין את /api/metrics (פולינג כל שנייה), את /api/signal, ואת
                # _sample_probe_stats — כלומר גם **בדיקת האנטנה** הייתה נכשלת.
                # מדלגים על המדד הפגום; השאר בשורה/בקובץ עדיין תקפים.
                continue
    return vals


# --- יומן שידורים והקלטות ---------------------------------------------------
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
    storage.atomic_write(ACTIVITY_PATH, "\n".join(lines) + "\n")


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


def _tx_status():
    """מצב מנגנון התמלול כולו — הבסיס לשורת הסטטוס ב-UI. ⚠ הערך שהופך את
    הפיצ'ר לגלוי: בלעדיו 'whisper לא מותקן' נראה בדיוק כמו 'אין מה לתמלל'."""
    st = appstate.load_state()
    lang = st.get("transcribe_lang") or recordings.TX_LANG_DEFAULT
    bin_ok = Path(recordings.WHISPER_BIN).exists()
    model = recordings._whisper_model(lang)
    langs = {ln: bool(recordings._whisper_model(ln)) for ln in recordings.TX_LANGS}
    return {"available": bool(bin_ok and model), "bin_ok": bin_ok,
            "model_name": Path(model).name if model else None,
            "lang": lang, "langs": langs,
            "auto": bool(st.get("transcribe_auto")),
            "queue": recordings._tx_queue_len(), "busy": recordings._tx_busy_file(),
            "install_hint": recordings.INSTALL_WHISPER_HINT}


def _transcribe_worker():
    """wrapper דק: הלולאה עצמה ב-recordings.transcribe_worker; כאן רק קריאת ה-state."""
    def cfg():
        st = appstate.load_state()
        return (st.get("transcribe_lang") or recordings.TX_LANG_DEFAULT,
                bool(st.get("transcribe_auto")))
    recordings.transcribe_worker(cfg)


def _activity_watcher():
    """לולאת רקע: הקלטה חדשה שהסתיימה => שורה ביומן; ואז retention.
    בעלייה ממשיכים מה-ts האחרון שנרשם => הקלטות מהזמן שהשרת היה כבוי נקלטות."""
    last_seen = _last_logged_ts()
    while True:
        try:
            rows, newest = recordings._scan_new_recordings(last_seen)
            if rows:
                _append_activity(rows)
                last_seen = newest   # מקדמים רק אחרי כתיבה מוצלחת => כישלון append לא מאבד אירועים
            recordings._sweep_recordings()
        except Exception:
            log.exception("activity watcher")
        time.sleep(recordings.WATCH_INTERVAL)


@app.route("/api/activity")
def api_activity():
    """אירועי השידור האחרונים, חדש=>ישן. exists=False כשההקלטה כבר נמחקה ב-retention.
    ‏?starred=1 => רק ההקלטות השמורות, **נסרקות מ-`saved/`** ולא מהיומן — כך הן
    נשארות נגישות גם אחרי שהשורה שלהן קוצצה מ-activity.jsonl (ACTIVITY_KEEP),
    בלי מאגר-מצב מקביל שאפשר לאבד/לפגום (ר' הערת SAVED_DIRNAME)."""
    count, used = recordings._saved_usage()
    meta = {"starred_count": count, "starred_max": recordings.REC_STAR_MAX_FILES,
            "starred_bytes": used, "starred_max_bytes": recordings.REC_STAR_MAX_BYTES}
    if request.args.get("starred") in ("1", "true", "yes"):
        events = []
        for p in recordings._iter_recordings():
            if not recordings._is_saved(p.name):
                continue
            try:
                events.append(recordings._decorate_event(recordings._rec_event(p)))
            except OSError:
                continue          # נעלם בין ה-glob ל-stat
        return jsonify(ok=True, events=events, starred_only=True, **meta)
    try:
        lines = ACTIVITY_PATH.read_text(errors="replace").splitlines()
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
        # ⚠ אותה משפחת באג כמו ב-storage.jsonl_records: שורת JSON תקין שאינה אובייקט
        # (‏null/מספר/מערך — שריד בלוק פגום אחרי כיבוי פתאומי) עברה את
        # ה-except ValueError ואז הפילה את הראוט ב-TypeError. ‏/api/activity
        # נקרא בפולינג כל 15 שניות במצב קול, כך שזה 500 חוזר ולא תקלה חד-פעמית.
        if not isinstance(ev, dict):
            continue
        events.append(recordings._decorate_event(ev))
    return jsonify(ok=True, events=events, **meta)


def _rec_name_arg():
    """(name, error_response) — שם הקלטה מגוף הבקשה, מאומת מול תבנית השם.
    ⚠ האימות מול _REC_NAME_RE הוא גם ההגנה מפני path traversal: השם חייב להיות
    בדיוק airam_<תאריך>_<שעה>_<Hz>.mp3, כך שאין בו לוכסנים/נקודות-נקודה בכלל."""
    data = request.get_json(silent=True) or {}
    name = str(data.get("file") or "")
    if not recordings._REC_NAME_RE.match(name):
        return None, (jsonify(ok=False, error="שם הקלטה לא תקין"), 400)
    return name, None


@app.route("/api/recordings/star", methods=["POST"])
def api_star():
    """שמירת הקלטה (★) / ביטול. שמורה = יושבת ב-`saved/` ולכן `_sweep_recordings`
    לא רואה אותה כלל.
    ⚠ הכול תחת `_STAR_LOCK`: בלי נעילה, בדיקת התקרה ופעולת ההעברה היו
    TOCTOU — 20 בקשות מקבילות קיבלו `ok:true` בזמן ש-2 בלבד נשמרו בפועל,
    כלומר 18 אישורים שקריים על קבצים שיימחקו (הוכח)."""
    name, err = _rec_name_arg()
    if err:
        return err
    want = bool((request.get_json(silent=True) or {}).get("starred", True))
    with recordings._STAR_LOCK:
        live, saved = recordings.REC_DIR / name, recordings._saved_dir() / name
        if not want:
            if saved.is_file():
                try:
                    recordings._move_recording(saved, recordings.REC_DIR)
                except OSError as e:
                    return jsonify(ok=False, error=f"ביטול השמירה נכשל: {e}"), 500
            count, used = recordings._saved_usage()
            return jsonify(ok=True, file=name, starred=False,
                           starred_count=count, starred_max=recordings.REC_STAR_MAX_FILES)
        if saved.is_file():
            count, used = recordings._saved_usage()          # כבר שמורה — idempotent
            return jsonify(ok=True, file=name, starred=True,
                           starred_count=count, starred_max=recordings.REC_STAR_MAX_FILES)
        if not live.is_file():
            return jsonify(ok=False, error="ההקלטה כבר לא קיימת"), 404
        count, used = recordings._saved_usage()
        try:
            size = live.stat().st_size
        except OSError:
            return jsonify(ok=False, error="ההקלטה כבר לא קיימת"), 404
        # ⚠ מסרבים, לא מוחקים ותיקה: המשתמש הגן על שתיהן במפורש, והבחירה מי
        # מהן להסיר היא שלו.
        if count >= recordings.REC_STAR_MAX_FILES or used + size > recordings.REC_STAR_MAX_BYTES:
            return jsonify(ok=False, starred_count=count, starred_max=recordings.REC_STAR_MAX_FILES,
                           error=(f"אין מקום לשמירה נוספת ({count}/{recordings.REC_STAR_MAX_FILES}) — "
                                  "בטל שמירה של הקלטה אחרת")), 409
        try:
            recordings._move_recording(live, recordings._saved_dir())
        except OSError as e:
            return jsonify(ok=False, error=f"השמירה נכשלה: {e}"), 500
        count, used = recordings._saved_usage()
    return jsonify(ok=True, file=name, starred=True,
                   starred_count=count, starred_max=recordings.REC_STAR_MAX_FILES)


@app.route("/api/recordings/transcribe", methods=["POST"])
def api_transcribe_one():
    """תמלול לפי דרישה של שידור בודד — הדרך המהירה לראות תמלול בלי להריץ את
    whisper על כל ההקלטות.
    ⚠ הבקשה נרשמת כ-sidecar `state="pending"` על הדיסק ולא בתור בזיכרון:
    restart ל-airam-web היה מאבד אותה, והשורה הייתה חוזרת להיראות
    "לא ניסינו" — בדיוק הכשל ש-§12 מדבר עליו."""
    name, err = _rec_name_arg()
    if err:
        return err
    mp3 = recordings._rec_path(name)
    if mp3 is None:
        return jsonify(ok=False, error="ההקלטה כבר לא קיימת"), 404
    data = request.get_json(silent=True) or {}
    lang = data.get("lang") or appstate.load_state().get("transcribe_lang") or recordings.TX_LANG_DEFAULT
    if lang not in recordings.TX_LANGS:
        return jsonify(ok=False, error="שפה לא נתמכת"), 400
    bin_ok, model = recordings._whisper_ready(lang)
    if not (bin_ok and model):
        return jsonify(ok=False, tx=_tx_status(),
                       error=(("אין מודל תמלול לעברית — נדרש מודל רב-לשוני. הרץ על ה-Pi: "
                               if lang == "he" else "תמלול לא מותקן — הרץ על ה-Pi: ")
                              + recordings.INSTALL_WHISPER_HINT)), 501
    cur = recordings._read_tx(mp3)
    if cur.get("state") == "pending":
        return jsonify(ok=True, file=name, tx=cur, queue=recordings._tx_queue_len())
    # לחיצה מפורשת של המשתמש היא **תמיד** ניסיון חוזר — גם על 'empty'.
    # ⚠ קודם `force` נשלח רק ב-'failed', ולכן לחיצה על שורת "לא זוהה דיבור"
    # החזירה את אותו sidecar ולא עשתה כלום: הכפתור נראה שבור.
    recordings._write_tx(mp3, "pending", lang=lang)
    recordings._TX_FAILS.pop(name, None)
    return jsonify(ok=True, file=name, queue=recordings._tx_queue_len(),
                   tx={"state": "pending", "text": None, "lang": lang})


@app.route("/api/transcribe", methods=["GET", "POST"])
def api_transcribe():
    """GET — מצב מנגנון התמלול (מותקן? איזה מודל? אילו שפות אפשריות?). זה מה
    שמאפשר ל-UI לומר 'לא מותקן, הנה הפקודה' במקום פשוט לא להציג כלום.
    ‏POST {auto?: bool, lang?: "en"|"he"} — נשמר ב-state ושורד reboot."""
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        if "auto" not in data and "lang" not in data:
            return jsonify(ok=False, error="חסר שדה auto או lang"), 400
        changes = {}
        if "auto" in data:
            changes["transcribe_auto"] = bool(data.get("auto"))
        if "lang" in data:
            if data.get("lang") not in recordings.TX_LANGS:
                return jsonify(ok=False, error="שפה לא נתמכת"), 400
            changes["transcribe_lang"] = data["lang"]
        appstate._merge_state(changes)
    return jsonify(ok=True, tx=_tx_status())


@app.route("/api/recordings/starred.zip")
def api_starred_zip():
    """ייצוא כל ההקלטות השמורות (+התמלולים) כ-ZIP אחד.
    ⚠ הסיבה שזה קיים: השמורות יושבות על כרטיס SD, וכרטיסי SD ב-Pi מתים.
    ‏ZIP_STORED ולא DEFLATED — MP3 כבר דחוס, ודחיסה חוזרת היא רק CPU על ה-Pi.
    נכתב לקובץ זמני ולא ל-BytesIO: עד REC_STAR_MAX_BYTES בזיכרון על מכונה עם
    4GB, בזמן שהרדיו רץ, זה בדיוק מה שאסור."""
    import zipfile
    files = sorted(recordings._saved_dir().glob("*.mp3")) if recordings._saved_dir().is_dir() else []
    if not files:
        return jsonify(ok=False, error="אין הקלטות שמורות לייצוא"), 404
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
            for p in files:
                try:
                    z.write(p, p.name)
                except OSError:
                    continue      # נעלם בינתיים — לא מפילים את הייצוא כולו
                tx = recordings._read_tx(p)
                if tx.get("text"):
                    z.writestr(p.name + ".txt", tx["text"] + "\n")
        tmp.close()
        resp = send_file(tmp.name, mimetype="application/zip", as_attachment=True,
                         download_name=f"airam-saved-{time.strftime('%Y%m%d')}.zip")
    except Exception:
        tmp.close()
        os.unlink(tmp.name)
        raise
    # הקובץ הזמני נמחק אחרי שהתשובה נשלחה במלואה (send_file משתמש ב-generator)
    resp.call_on_close(lambda: os.path.exists(tmp.name) and os.unlink(tmp.name))
    return resp


# --- שחזור-סשן (שלב 2, docs/session-replay-design.md §4.3/§8) ----------------

@app.route("/api/sessions", methods=["GET", "POST"])
def api_sessions():
    """GET — רשימת סשנים שמורים (חדש→ישן, ממוין לפי created_at ב-meta.json —
    לא לפי שם-תיקייה, כדי לא להיתקל בסדר-מחרוזות מוזר בהתנגשות ‏-2/-3).
    POST ‏{minutes, note?} — שומר את N הדקות האחרונות מה-buffer המתגלגל
    (adsb.read_track_slice) + את ההקלטות שבחלון הזמן הזה. דרך _guard."""
    if request.method == "GET":
        sessions = []
        if recordings.SESSIONS_DIR.is_dir():
            for d in recordings.SESSIONS_DIR.iterdir():
                if not d.is_dir() or not recordings.SESSION_ID_RE.match(d.name):
                    continue
                try:
                    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue          # תיקייה חצי-כתובה/פגומה — לא מפילים את הרשימה
                sessions.append(meta)
        sessions.sort(key=lambda m: m.get("created_at") or 0, reverse=True)
        return jsonify(ok=True, sessions=sessions)

    data = request.get_json(silent=True) or {}
    try:
        minutes = float(data.get("minutes", adsb.TRACK_BUFFER_MIN))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="minutes לא תקין"), 400
    if minutes <= 0:
        return jsonify(ok=False, error="minutes לא תקין"), 400
    # נחתך לגודל ה-buffer בפועל — אי אפשר לשמור מה שכבר נגזם מ-track.jsonl
    minutes = min(minutes, adsb.TRACK_BUFFER_MIN)
    note = str(data.get("note") or "").strip()[:200]

    t_end = time.time()
    t_start = t_end - minutes * 60
    rows = adsb.read_track_slice(t_start, t_end)
    if not rows:
        return jsonify(ok=False, error="אין נתוני מסלול בטווח המבוקש — "
                                        "ה-buffer ריק או שהחלון ישן מדי"), 400

    session_id = recordings._new_session_id()
    sdir = recordings.SESSIONS_DIR / session_id
    clips_dir = sdir / recordings.SESSION_CLIPS_DIRNAME
    clips_dir.mkdir(parents=True, exist_ok=True)

    # ⚠ המסלול נכתב *לפני* העברת הקליפים: קודם הקליפים הלא-שמורים הועברו
    # ראשונים, וכשל בכתיבת המסלול (דיסק מלא) הריץ rmtree על התיקייה — ומחק
    # לצמיתות הקלטות שכבר יצאו מהתיקייה החיה. כשל כאן עוד לא נגע באף הקלטה.
    track_path = sdir / "track.jsonl.gz"
    tmp = track_path.with_name(track_path.name + f".tmp{os.getpid()}")
    try:
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        os.replace(tmp, track_path)
    except OSError as e:
        shutil.rmtree(sdir, ignore_errors=True)
        return jsonify(ok=False, error=f"שמירת מסלול נכשלה: {e}"), 500

    # קליפים בחלון הזמן: שמורה (★) מ*עתיקים* (שומרת על ההגנה ב-saved/ גם),
    # לא-שמורה *מועברת* (משתמשת ב-_move_recording הקיים — משחררת מ-retention
    # החי, בדיוק כמו §12: "מיקום הקובץ הוא המצב", אפס לוגיקת-פטור חדשה).
    clips = []
    moved = []          # לא-שמורות שהועברו — מוחזרות לתיקייה החיה אם השמירה נכשלת
    skipped = 0
    for p in recordings._iter_recordings():
        try:
            mtime = p.stat().st_mtime
            if not (t_start <= mtime <= t_end):
                continue
            ev = recordings._rec_event(p)
        except OSError:
            continue
        if recordings._is_saved(p.name):
            try:
                shutil.copy2(p, clips_dir / p.name)
                for sc in recordings._tx_sidecars(p):
                    if sc.exists():
                        shutil.copy2(sc, clips_dir / sc.name)
            except OSError:
                skipped += 1
                continue
        else:
            try:
                recordings._move_recording(p, clips_dir)
            except OSError:
                skipped += 1
                continue
            moved.append(p.name)
        clips.append(ev)

    aircraft = sorted({ac[0] for row in rows for ac in row.get("ac", [])})
    gaps = [{"t": row["t"], "reason": row.get("gap"), "detail": row.get("detail")}
            for row in rows if "gap" in row]

    # ⚠ app_mode/freq הם תמונת-מצב *נוכחית* בזמן השמירה, לא היסטוריה מלאה:
    # אין ב-AIR-AM יומן מעברי-מצב (ר' docs/session-replay-design.md §4.3 —
    # `modes` שם היה תכנון-אידיאלי; לממש אותו דורש מנגנון חדש לגמרי, לא רק
    # קריאת state קיים). מתועד כאן כסטייה מכוונת, לא כשגיאה.
    st = appstate.load_state()
    meta = {
        "id": session_id,
        "t_start": t_start,
        "t_end": t_end,
        "created_at": time.time(),
        "note": note,
        "app_mode": st.get("app_mode"),
        "freq": st.get("freq"),
        "aircraft": aircraft,
        "clips": clips,
        "gaps": gaps,
        "counts": {"clips": len(clips), "aircraft": len(aircraft), "samples": len(rows)},
    }
    try:
        storage.atomic_write(sdir / "meta.json", json.dumps(meta, ensure_ascii=False, indent=2))
    except OSError as e:
        for name in moved:
            try:
                recordings._move_recording(clips_dir / name, recordings.REC_DIR)
            except OSError:
                log.warning("שמירת סשן: החזרת %s לתיקייה החיה נכשלה", name)
                return jsonify(ok=False, error=f"שמירת מטא-דאטה נכשלה: {e}; "
                               f"ההקלטות נשארו ב-{sdir}"), 500
        shutil.rmtree(sdir, ignore_errors=True)
        return jsonify(ok=False, error=f"שמירת מטא-דאטה נכשלה: {e}"), 500
    if skipped:
        # לא שקט: סשן שחסרים בו קליפים לא נראה "מלא" כשהוא לא
        return jsonify(ok=True, id=session_id, session=meta, skipped_clips=skipped)
    return jsonify(ok=True, id=session_id, session=meta)


@app.route("/api/sessions/<session_id>", methods=["GET", "DELETE"])
def api_session_detail(session_id):
    """GET — meta.json. DELETE — מחיקת הסשן כולו (קליפים+מסלול+מטא-דאטה).
    ⚠ DELETE על סשן ששמר קליפ *לא-שמור* לא מחזיר אותו לחיים — הוא נמחק
    יחד עם התיקייה, בדיוק כמו שמחיקת הקלטה רגילה בלתי-הפיכה."""
    sdir = recordings._session_dir(session_id)
    if sdir is None:
        return jsonify(ok=False, error="סשן לא נמצא"), 404
    if request.method == "DELETE":
        try:
            shutil.rmtree(sdir)
        except OSError as e:
            return jsonify(ok=False, error=f"מחיקה נכשלה: {e}"), 500
        return jsonify(ok=True, id=session_id)
    try:
        meta = json.loads((sdir / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return jsonify(ok=False, error="metadata של הסשן פגום"), 500
    # ⚠ התמלול נקרא **חי מה-sidecar**, ולא נשמר לתוך meta.json בזמן השמירה:
    # קליפ יכול להתמלל *אחרי* שהסשן נשמר (התור מבוסס-sidecar, ר' §12), וערך
    # שהוקפא בשמירה היה נשאר "אין תמלול" לנצח — בדיוק סוג ההקפאה שהפרויקט
    # למד להימנע ממנה (`starred.json`). meta.json נשאר כפי שנכתב.
    clips_dir = sdir / recordings.SESSION_CLIPS_DIRNAME
    for c in meta.get("clips") or []:
        if isinstance(c, dict) and c.get("file"):
            c["tx"] = recordings._read_tx(clips_dir / c["file"])
    return jsonify(ok=True, session=meta)


@app.route("/api/sessions/<session_id>/track")
def api_session_track(session_id):
    """מסלול ה-ADS-B של הסשן, מפוענח מ-track.jsonl.gz ל-JSON (שורות ac+gap
    כמו שהן ב-adsb.read_track_slice — ה-UI מקבל אותו סכימה משני המקורות)."""
    sdir = recordings._session_dir(session_id)
    if sdir is None:
        return jsonify(ok=False, error="סשן לא נמצא"), 404
    try:
        with gzip.open(sdir / "track.jsonl.gz", "rt", encoding="utf-8") as f:
            rows = [json.loads(ln) for ln in f if ln.strip()]
    except (OSError, ValueError) as e:
        return jsonify(ok=False, error=f"קריאת מסלול נכשלה: {e}"), 500
    return jsonify(ok=True, rows=rows)


@app.route("/api/sessions/<session_id>/clips/<name>")
def api_session_clip(session_id, name):
    """קליפ אודיו של סשן — route ייעודי (לא /recordings/<name>) כדי לא להוסיף
    עוד מסלול-חיפוש לכל בקשת הקלטה רגילה, ר' docs/session-replay-design.md §4.4."""
    sdir = recordings._session_dir(session_id)
    if sdir is None or not recordings._REC_NAME_RE.match(name):
        return jsonify(ok=False, error="לא נמצא"), 404
    return send_from_directory(str(sdir / recordings.SESSION_CLIPS_DIRNAME), name)


@app.route("/api/sessions/<session_id>/export.zip")
def api_session_export(session_id):
    """ייצוא סשן שלם (מטא-דאטה + מסלול + כל הקליפים) כ-ZIP — אותו דפוס בדיוק
    כמו api_starred_zip (ZIP_STORED, קובץ זמני ולא BytesIO, ניקוי ב-call_on_close)."""
    import zipfile
    sdir = recordings._session_dir(session_id)
    if sdir is None:
        return jsonify(ok=False, error="סשן לא נמצא"), 404
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
            meta_p = sdir / "meta.json"
            if meta_p.is_file():
                z.write(meta_p, "meta.json")
            track_p = sdir / "track.jsonl.gz"
            if track_p.is_file():
                z.write(track_p, "track.jsonl.gz")
            clips_dir = sdir / recordings.SESSION_CLIPS_DIRNAME
            if clips_dir.is_dir():
                for p in sorted(clips_dir.glob("*.mp3")):
                    try:
                        z.write(p, f"{recordings.SESSION_CLIPS_DIRNAME}/{p.name}")
                    except OSError:
                        continue
                    # התמלול לצד הקליפ, כמו ב-starred.zip — נקרא חי מה-sidecar
                    tx = recordings._read_tx(p)
                    if tx.get("text"):
                        z.writestr(f"{recordings.SESSION_CLIPS_DIRNAME}/{p.name}.txt", tx["text"] + "\n")
        tmp.close()
        resp = send_file(tmp.name, mimetype="application/zip", as_attachment=True,
                         download_name=f"airam-session-{session_id}.zip")
    except Exception:
        tmp.close()
        os.unlink(tmp.name)
        raise
    resp.call_on_close(lambda: os.path.exists(tmp.name) and os.unlink(tmp.name))
    return resp


@app.route("/recordings/<name>")
def recording_file(name):
    # send_from_directory חוסם path traversal; ‏<name> (לא <path:>) חוסם תתי-תיקיות.
    # שמורה יושבת ב-saved/ => מנסים שם כשאיננה בתיקייה החיה.
    d = recordings.REC_DIR if (recordings.REC_DIR / name).is_file() else recordings._saved_dir()
    return send_from_directory(str(d), name)


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


def _read_voice_metrics(ch=1):
    """מדדי RF רציפים לתדר הנוכחי מ-rtl_airband stats. מקור אמת יחיד לפענוח
    הקובץ — משותף ל-/api/metrics (תצוגת קול) ול-/api/signal (מד השדה המאוחד,
    מצב voice). rtl_airband מרענן את הקובץ כל ~1 שנייה. ch=2 — הערוץ השני (קול כפול)."""
    path = STATS2_PATH if ch == 2 else STATS_PATH
    try:
        age = time.time() - path.stat().st_mtime
        text = path.read_text()
    except OSError:
        return {"fresh": False, "age": None, "signal": None, "noise": None,
                "snr": None, "overload": False, "squelch_opens": None}

    st = appstate.load_state()
    want = f"{(_voice2_params(st)[0] if ch == 2 else st['freq']):.3f}"   # מתויגים freq=MHz ב-3 ספרות
    vals = parse_stats(text, want)

    sig = vals.get("channel_dbfs_signal_level")
    noise = vals.get("channel_dbfs_noise_level")
    snr = round(sig - noise, 1) if (sig is not None and noise is not None) else None
    # עומס יתר: רמת האות בערוץ מתקרבת ל-full scale (0 dBFS) => ה-ADC/רווח רווי.
    overload = sig is not None and sig >= OVERLOAD_DBFS
    return {"fresh": (age <= STATS_MAX_AGE and snr is not None), "age": round(age, 1),
            "signal": sig, "noise": noise, "snr": snr, "overload": overload,
            "squelch_opens": vals.get("channel_squelch_counter")}


@app.route("/api/metrics")
def api_metrics():
    """מדדי RF חיים לתדר הנוכחי. rtl_airband מרענן את הקובץ כל ~1 שנייה. ?ch=2 — הערוץ השני."""
    ch = 2 if request.args.get("ch") == "2" else 1
    return jsonify(ok=True, overload_dbfs=OVERLOAD_DBFS, ch=ch, **_read_voice_metrics(ch))


def _signal_verdict(noise, baseline):
    """פסק דין *רק* מול בסיס שהמשתמש כייל בעצמו — לעולם לא סף איכות מומצא
    (§12 ב-CLAUDE.md). ‏noise=None (אין מדידה נוכחית) => 'unknown'. בלי בסיס
    כלל => 'no_baseline' — לא ניחוש. אחרת משווים מול DISCONNECT_DROP_DB."""
    if noise is None:
        return "unknown"
    if not baseline or baseline.get("noise") is None:
        return "no_baseline"
    return "below_baseline" if (baseline["noise"] - noise) >= config.DISCONNECT_DROP_DB else "ok"


@app.route("/api/signal")
def api_signal():
    """מד שדה מאוחד: המדד הכי-טוב שקיים למצב שרץ *בפועל* כרגע (לא לכוונה
    השמורה — כמו _live_mode בכל מקום אחר), ופסק דין מול הבסיס שכויל
    ב-/api/antenna/check.
      voice  — מדידה רציפה (rtl_airband stats), verdict תקף רק תחת AGC
               (השוואה לבסיס שנמדד גם הוא תחת AGC — ר' DEFAULT_STATE).
      acars/vdl2 — level (+snr ב-VDL2 בלבד, לעולם לא ב-ACARS — ר' §12) *מההודעה
               האחרונה בזיכרון בלבד*: אין כאן מדידה רציפה של שקט (המפענחים
               לא חושפים רצפת רעש כששקט), ולכן אין verdict מול בסיס — רק
               בדיקת אנטנה יזומה (שמשתמשת בקול) מייצרת מדידה בת-השוואה.
      satcom — יש לו כלי ייעודי (/api/satcom/health); כאן רק מפנים אליו.
      off/אין מצב חי — kind="none"."""
    st = appstate.load_state()
    mode = _live_mode()
    baseline = st.get("signal_baseline")
    payload = {"ok": True, "mode": mode or "off"}

    if mode == "voice" or mode in COMPOSITE_SECONDARY:
        m = _read_voice_metrics()
        agc_ok = bool(st.get("agc", True))   # gain ידני => לא בר-השוואה לבסיס (§12: לא משווים תפוחים לתפוזים)
        payload.update(kind="continuous", fresh=m["fresh"], age=m["age"],
                       signal=m["signal"], noise=m["noise"], snr=m["snr"],
                       baseline=baseline,
                       verdict=_signal_verdict(m["noise"] if agc_ok else None, baseline))
    elif mode in ("acars", "vdl2"):
        lock = _acars_lock if mode == "acars" else _vdl2_lock
        buf = _acars_msgs if mode == "acars" else _vdl2_msgs
        with lock:
            last = dict(buf[-1]) if buf else None
        if last is None:
            payload.update(kind="last-message", fresh=False, age=None, signal=None,
                           noise=None, snr=None, baseline=None, verdict="unknown")
        else:
            age = max(0.0, time.time() - (last.get("t") or time.time()))
            payload.update(kind="last-message", fresh=(age <= SIGNAL_LAST_MSG_MAX_AGE),
                           age=round(age, 1), signal=last.get("level"), noise=None,
                           snr=last.get("snr"), baseline=None, verdict="unknown")
    elif mode == "satcom":
        payload.update(kind="satcom-panel", fresh=None, age=None, signal=None,
                       noise=None, snr=None, baseline=None, verdict="unknown")
    else:
        payload.update(kind="none", fresh=False, age=None, signal=None,
                       noise=None, snr=None, baseline=baseline, verdict="unknown")
    return jsonify(**payload)


def _sample_probe_stats(freq, timeout_sec):
    """דוגם רצפת רעש לתדר נתון בפולינג קצר, עד timeout_sec. בשונה מ-
    /api/metrics (שם קול כבר רץ ברציפות) — אחרי restart של rtl_airband לתדר
    הזה לוקח רגע עד שהוא מתפרסם ב-stats, אז דגימה בודדת עלולה לפספס.
    מחזיר {"signal","noise","snr"} או None אם לא הגיע דיווח טרי בזמן."""
    want = f"{freq:.3f}"
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            mtime = STATS_PATH.stat().st_mtime
            age = time.time() - mtime
            text = STATS_PATH.read_text()
        except OSError:
            mtime, age, text = None, None, ""
        if text:
            vals = parse_stats(text, want)
            noise = vals.get("channel_dbfs_noise_level")
            if age is not None and age <= STATS_MAX_AGE and noise is not None:
                sig = vals.get("channel_dbfs_signal_level")
                # stats_mtime: איזו כתיבה נקראה בפועל — הניסוי האוטומטי משווה אותו
                # לזמן ההפעלה של rtl_airband כדי לזהות קריאה של התהליך *הקודם*.
                return {"signal": sig, "noise": noise, "stats_mtime": round(mtime, 2),
                        "snr": round(sig - noise, 1) if sig is not None else None}
        time.sleep(0.3)
    return None


def _restore_after_probe(prev_state, prev_live):
    """משחזר את מה שבאמת רץ לפני בדיקת האנטנה הזמנית — בצד השרת, כדי שהבדיקה
    תישאר עסקה סינכרונית אחת ולא תלויה בפולינג הבא של הקליינט. לא נועל
    TUNE_LOCK (הקורא כבר מחזיק אותו, כמו כל _enter_* אחר) ולא כותב ל-state.json
    (זו לא בקשת מעבר-מצב — הכוונה השמורה לא השתנתה בכלל). best-effort: כישלון
    שחזור לא הופך את הבדיקה עצמה לכישלון, רק נרשם ללוג; המשתמש עדיין יכול
    להיכנס למצב מחדש ידנית, בדיוק כמו כל _enter_* אחר שנכשל.
    ⚠ תוך כדי סבב סריקה, prev_state["acars_freqs"/"vdl2_freqs"] הוא הבנק
    השמור ב-state.json — לא בהכרח תדרי הרגל שבאמת רצה (רגל עם freqs מפורשים
    לא נכתבת ל-state, ר' _scan_enter_leg). שחזור לפי prev_state היה מכוונן
    מחדש לבנק הלא-נכון, וה-thread הפעיל (last_entered) לא היה מבחין בכך
    (משווה מול הרגל שבלוח, לא מול מה שבאמת נכנס) => נשאר תקוע על בנק שגוי עד
    שהלוח מתקדם. משחזרים את הרגל הנוכחית עצמה כשסבב פעיל."""
    with _scan_lock:
        scan_active = _scan_thread is not None and _scan_thread.is_alive()
        scan_leg = dict(_scan_status["leg"]) if scan_active and _scan_status.get("leg") else None
    if scan_leg:
        err, detail = _scan_enter_leg(scan_leg)
        if err:
            log.warning("בדיקת אנטנה: שחזור רגל הסריקה הנוכחית (%s) נכשל: %s", scan_leg.get("mode"), err)
        return
    try:
        if prev_live == "acars":
            _enter_acars(prev_state.get("acars_freqs", config.ACARS_FREQS_DEFAULT))
        elif prev_live == "vdl2":
            _enter_vdl2(prev_state.get("vdl2_freqs", config.VDL2_FREQS_DEFAULT))
        elif prev_live == "satcom":
            _enter_satcom(prev_state.get("satcom_freqs", config.SATCOM_FREQS_DEFAULT),
                          bias_tee=prev_state.get("satcom_bias_tee", True),
                          skip_c=prev_state.get("satcom_skip_c", True),
                          spectrum=prev_state.get("satcom_spectrum", True),
                          gain=_sanitize_satcom_gain(prev_state.get("satcom_gain")))
        elif prev_live == "voice":
            _enter_voice({"freq": prev_state["freq"], "mod": prev_state["mod"],
                         "agc": prev_state["agc"], "if_gain": prev_state["if_gain"],
                         "rf_gain": prev_state["rf_gain"],
                         "squelch_mode": prev_state["squelch_mode"],
                         "squelch_snr": prev_state["squelch_snr"],
                         "chan_filter": prev_state.get("chan_filter", config.CHAN_FILTER_DEFAULT),
                         "fm_notch": bool(prev_state.get("fm_notch"))})
        else:
            _enter_standby()
    except Exception:
        log.warning("בדיקת אנטנה: שחזור המצב הקודם (%s) נכשל", prev_live, exc_info=True)


def _probe_params(freq):
    """תנאי המדידה של בדיקת האנטנה (AGC, סקוולץ' פתוח, AM) — מקור-אמת יחיד
    ל-/api/antenna/check ולניסוי האוטומטי, כדי שהניסוי יבדוק *את* המסלול של המוצר."""
    return {"freq": freq, "mod": "am", "agc": True, "if_gain": config.IF_GAIN_DEFAULT,
            "rf_gain": config.RF_GAIN_DEFAULT, "squelch_mode": "open", "squelch_snr": config.SNR_DEFAULT}


@app.route("/api/antenna/check", methods=["POST"])
def api_antenna_check():
    """בדיקת אנטנה בת ~3 שניות: נכנס זמנית לקול (AGC, סקוולץ' פתוח) בתדר
    המבוקש, דוגם רצפת רעש אמיתית, וחוזר למצב שהיה פעיל קודם. עוקף את המגבלה
    ש-acarsdec/dumpvdl2 לא חושפים רצפת רעש רציפה כששקט (§12) — הדרך היחידה
    לקבל מדידה בת-השוואה במצבים האלה. ‏calibrate=true שומר את התוצאה כבסיס
    ההשוואה (state['signal_baseline']) לפסקי-הדין העתידיים של /api/signal.
    serialized תחת TUNE_LOCK כמו כל שינוי חומרה אחר — אם כיוונון/מעבר מצב
    אחר כבר רץ, מחזיר 409 במקום לתקוע את שניהם."""
    data = request.get_json(silent=True) or {}
    prev = appstate.load_state()
    try:
        freq = float(data.get("freq"))
        if not (0.1 <= freq <= 1999.5):
            raise ValueError
    except (TypeError, ValueError):
        freq = prev.get("freq", appstate.DEFAULT_STATE["freq"])
    calibrate = bool(data.get("calibrate"))

    if not TUNE_LOCK.acquire(blocking=False):
        return jsonify(ok=False, error="פעולה אחרת מתבצעת כרגע — המתן שנייה ונסה שוב"), 409
    try:
        prev_live = _live_mode()
        if prev_live in COMPOSITE_SECONDARY:
            return jsonify(ok=False, error="לא זמין במצב " + COMPOSITE_LABEL[prev_live] +
                           " — עבור לקול בלבד ונסה שוב"), 409
        if prev_live == "satcom":
            # כמו _experiment_start: עם אנטנת L-band מחוברת המדידה ב-VHF חסרת
            # משמעות (ו-calibrate היה שומר אותה כבסיס), והמעבר מכבה ומדליק bias-T.
            return jsonify(ok=False, error="SATCOM פעיל — בדיקת אנטנה היא ל-VHF בלבד"), 409
        # ⚠ "כבר בקול" לא מספיק כדי לדלג על ההכנה: הדגימה חייבת להיעשות *באותם
        # תנאים* שבהם נמדד הבסיס, אחרת ההשוואה של _signal_verdict חסרת משמעות.
        # רצפת הרעש ב-dBFS נמדדת אחרי שרשרת הרווח => משתמש שיושב בקול עם רווח
        # ידני (IFGR שונה מברירת המחדל) היה מקבל רצפה שונה בעשרות dB מהבסיס
        # שנמדד תחת AGC, ו-verdict של "below_baseline" (= "האנטנה מנותקת!") בלי
        # שדבר באמת השתנה. זו בדיוק ההמצאה שעקרון §12 אוסר, רק בכיוון של
        # פסק-דין במקום ערך. לכן מדלגים רק כשהמצב החי *זהה* לתנאי הבדיקה.
        already_voice = (prev_live == "voice"
                         and abs(prev.get("freq", -999.0) - freq) < 5e-4
                         and prev.get("mod") == "am"
                         and bool(prev.get("agc")) is True
                         and prev.get("squelch_mode") == "open")
        if not already_voice:
            err, detail, _sdr_down = _enter_voice(_probe_params(freq))
            if err:
                # שלב ההכנה כבר עצר את הצרכן הקודם (peer של _enter_acars/_enter_vdl2)
                # לפני שקול עצמו נכשל לעלות => מנסים best-effort להחזיר את מה שהיה,
                # במקום להשאיר את ה-SDR תקוע חצי-מכובה בלי הסבר. לא נוגעים ב-state.json —
                # זו פעולת אבחון, לא בקשת מעבר-מצב.
                _restore_after_probe(prev, prev_live)
                return jsonify(ok=False, error="בדיקת האנטנה נכשלה: " + err, detail=detail), 500

        result = _sample_probe_stats(freq, ANTENNA_CHECK_SAMPLE_SEC)

        if not already_voice:
            _restore_after_probe(prev, prev_live)

        if result is None:
            experiment._rflog_event({"ev": "probe", "freq": freq, "calibrate": calibrate,
                          "already_voice": already_voice, "error": "no_fresh_stats"})
            return jsonify(ok=False, error="לא התקבלו מדדים מה-SDR בזמן — נסה שוב"), 504

        if calibrate:
            baseline = {"noise": result["noise"], "freq": freq, "ts": time.time()}
            appstate._merge_state({"signal_baseline": baseline})
        else:
            baseline = prev.get("signal_baseline")
        verdict = _signal_verdict(result["noise"], baseline)
        experiment._rflog_event({"ev": "probe", "freq": freq, "calibrate": calibrate,
                      "already_voice": already_voice, "noise": result["noise"],
                      "signal": result["signal"], "verdict": verdict,
                      "baseline_noise": (baseline or {}).get("noise")})
        return jsonify(ok=True, freq=freq, calibrated=calibrate, baseline=baseline,
                       verdict=verdict, **result)
    finally:
        TUNE_LOCK.release()


@app.route("/api/rflog", methods=["GET", "POST"])
def api_rflog():
    """GET: מצב הרשם. POST {active: bool}: הפעלה/כיבוי (idempotent). דרך _guard."""
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        if bool(data.get("active")):
            experiment._rflog_start()
        else:
            with experiment._exp_lock:
                exp_running = experiment._exp["running"]
            if exp_running:
                return jsonify(ok=False, error="הניסוי האוטומטי רץ ומשתמש ברשם — עצור את הניסוי קודם",
                               **experiment._rflog_status()), 409
            experiment._rflog_stop("user")
    return jsonify(ok=True, **experiment._rflog_status())


@app.route("/api/rflog/mark", methods=["POST"])
def api_rflog_mark():
    """{label} — סימון אירוע פיזי ("מחובר"/"מנותק"/"מסיים 50Ω"). 409 כשהרשם כבוי:
    סימון שלא נרשם לא יכול להיראות כאילו נרשם."""
    data = request.get_json(silent=True) or {}
    label = str(data.get("label") or "").strip()[:experiment.RFLOG_LABEL_MAX]
    if not label:
        return jsonify(ok=False, error="חסרה תווית"), 400
    if not experiment._rflog_event({"ev": "mark", "label": label}):
        return jsonify(ok=False, error="הרשם כבוי — הפעל אותו קודם"), 409
    with experiment._rflog_lock:
        experiment._rflog["marks"] += 1
    return jsonify(ok=True, label=label, **experiment._rflog_status())


@app.route("/api/rflog/export")
def api_rflog_export():
    if not experiment.RFLOG_PATH.exists():
        return jsonify(ok=False, error="אין הקלטה עדיין"), 404
    return send_file(str(experiment.RFLOG_PATH), mimetype="application/x-ndjson", as_attachment=True,
                     download_name=f"airam-rflog-{time.strftime('%Y%m%d-%H%M')}.jsonl")


# --- יומן בדיקה + ייצוא לוגים (webtune/diag.py — הניתוח הטהור) ---------------
# כשבודקים משהו (מצב חדש, כבל, ספק כוח) רוצים אחר כך קובץ *אחד* שעונה "האם הכול
# היה בסדר": מה המשתמש עשה (פעולות + סימונים), מה המערכת עשתה (דגימה כל 5ש' של
# המצב/השירותים/המתח), ומה היומנים אמרו (journald של כל היחידות + הקרנל). הדגימה
# רצה רק בזמן "בדיקה" פעילה; הייצוא עובד תמיד (חלון זמן). ⚠ אין בחבילה
# /etc/airam/airam.env (PIN, סודות OpenSky), ונ"צ התחנה מעוגל ל-~1 ק"מ.
DIAG_PATH = Path("/var/lib/airam/diag.jsonl")
DIAG_SAMPLE_SEC = 5
DIAG_MAX_SEC = 4 * 3600            # לא כותבים ל-SD לנצח אם שכחו לעצור
DIAG_ROTATE_BYTES = 5_000_000      # בהתחלת בדיקה: קובץ גדול מזה עובר ל-.prev
DIAG_LABEL_MAX = 80
DIAG_BODY_MAX = 300
DIAG_BODY_SKIP = ("trace_b64",)    # קובץ trace מיובא — גדול, ולא מעניין בציר הזמן
DIAG_EXPORT_DEFAULT_MIN = 60
DIAG_EXPORT_MAX_MIN = 24 * 60
DIAG_JOURNAL_MAX = 20000           # רשומות journald לכל שאילתה (שירותים / קרנל)
DIAG_MSG_SAMPLE = 20               # כמה הודעות אחרונות מכל מקור נכנסות לחבילה
DIAG_PRE_SEC = 60                  # חלון "מאז תחילת הבדיקה" מתחיל דקה לפניה
_diag_fsync = os.fsync
_diag_lock = threading.Lock()
_diag = {"active": False, "started_at": None, "until": None, "label": "", "samples": 0,
         "marks": 0, "requests": 0, "write_errors": 0, "thread": None, "stop": None,
         "last_run": None}


def _diag_units():
    return ("airam-web", SDR_API_SERVICE, *SDR_CONSUMERS, DENOISE_SERVICE, "icecast2")


def _diag_config_files():
    """הקונפיג שהאפליקציה כותבת (לא airam.env!). פונקציה ולא קבוע — הבדיקות מחליפות נתיבים."""
    return (CONFIG_PATH, VOICE2_CONFIG_PATH, ACARS_ENV_PATH, ACARS2_ENV_PATH, VDL2_ENV_PATH,
            VDL2B_ENV_PATH, SATCOM_ENV_PATH, SDR_ENV_PATH, DENOISE_ENV_PATH)


def _diag_cmd(args, timeout=15):
    """(stdout, stderr, rc) — או (None, שגיאה, None) כשהפקודה חסרה/נתקעה. לא זורק."""
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.stdout, r.stderr, r.returncode
    except FileNotFoundError:
        return None, f"{args[0]} לא מותקן", None
    except (subprocess.TimeoutExpired, OSError) as e:
        return None, f"{args[0]}: {e}", None


def _diag_write(obj):
    """append + fsync (כמו _rflog_write: תוצאת בדיקה, ו-power bank שקוטע הוא בדיוק
    מה שבודקים). לעולם לא זורק — כשל נספר ב-write_errors."""
    line = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
    with _diag_lock:
        try:
            DIAG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(DIAG_PATH, "a+b") as f:
                storage.append_line_on_boundary(f, line)
                f.flush()
                _diag_fsync(f.fileno())
        except OSError as e:
            _diag["write_errors"] += 1
            if _diag["write_errors"] in (1, 100):
                log.warning("יומן בדיקה: כתיבה ל-%s נכשלה: %s", DIAG_PATH, e)
            return False
    return True


def _meminfo_mb(key):
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith(key + ":"):
                return round(int(line.split()[1]) / 1024)
    except (OSError, ValueError, IndexError):
        pass
    return None


def _diag_sample():
    """דגימה אחת: המצב (אותו חישוב כמו /api/health), מתח/טמפ', עומס, זיכרון."""
    h = _health_snapshot()
    p = _power_cached() or {}
    try:
        load1 = round(os.getloadavg()[0], 2)
    except OSError:
        load1 = None
    return {"ev": "sample", "t": round(time.time(), 2), "mode": h["app_mode"], "ok": h["ok"],
            "services": h["services"], "sdr_present": h["sdr_present"], "stats_age": h["stats_age"],
            "denoise": (h.get("denoise") or {}).get("state"),
            "power": {k: p.get(k) for k in ("throttled", "undervolt_now", "throttle_now",
                                            "volts_in", "temp")} if p else None,
            "load1": load1, "mem_avail_mb": _meminfo_mb("MemAvailable")}


def _diag_stop(reason):
    """עוצר את הבדיקה (idempotent). לא עושה join כשנקרא מתוך ה-worker עצמו."""
    now = time.time()
    with _diag_lock:
        if not _diag["active"]:
            return False
        _diag["active"] = False
        evt, th = _diag["stop"], _diag["thread"]
        _diag["last_run"] = {"started_at": _diag["started_at"], "ended_at": now, "label": _diag["label"]}
    if evt:
        evt.set()
    _diag_write({"ev": "stop", "t": round(now, 2), "reason": reason})
    if th and th is not threading.current_thread():
        th.join(timeout=2)
    return True


def _diag_worker(stop_evt):
    while not stop_evt.is_set():
        with _diag_lock:
            until = _diag["until"]
        if until is not None and time.time() >= until:
            _diag_stop("timeout")
            return
        try:
            if _diag_write(_diag_sample()):
                with _diag_lock:
                    _diag["samples"] += 1
        except Exception:
            log.warning("יומן בדיקה: דגימה נכשלה", exc_info=True)   # לא מפילים את ה-thread
        stop_evt.wait(DIAG_SAMPLE_SEC)


def _diag_launch(started_at, label, first_event):
    evt = threading.Event()
    with _diag_lock:
        _diag.update(active=True, started_at=started_at, until=started_at + DIAG_MAX_SEC,
                     label=label, samples=0, marks=0, requests=0, write_errors=0,
                     stop=evt, thread=None, last_run=None)
    _diag_write(first_event)
    th = threading.Thread(target=_diag_worker, args=(evt,), name="diag", daemon=True)
    with _diag_lock:
        _diag["thread"] = th
    th.start()


def _diag_start(label):
    with _diag_lock:
        if _diag["active"]:
            return False
    try:
        if DIAG_PATH.exists() and DIAG_PATH.stat().st_size > DIAG_ROTATE_BYTES:
            os.replace(DIAG_PATH, DIAG_PATH.with_suffix(".jsonl.prev"))
    except OSError:
        pass
    now = time.time()
    _diag_launch(now, label, {"ev": "start", "t": round(now, 2), "label": label,
                              "version": VERSION, "max_sec": DIAG_MAX_SEC})
    log.info("יומן בדיקה: התחלה '%s' (from %s)", label, request.remote_addr if request else "-")
    return True


def _diag_event(obj):
    """אירוע (סימון / פעולה) — רק כשבדיקה פעילה. מחזיר אם נרשם."""
    with _diag_lock:
        active = _diag["active"]
    return active and _diag_write({"t": round(time.time(), 2), **obj})


def _diag_status():
    with _diag_lock:
        st = {k: _diag[k] for k in ("active", "started_at", "until", "label", "samples",
                                    "marks", "requests", "write_errors", "last_run")}
    st["remaining"] = max(0, round(st["until"] - time.time())) if st["active"] and st["until"] else None
    try:
        st["size"] = DIAG_PATH.stat().st_size
    except OSError:
        st["size"] = 0
    return st


def _diag_read_rows():
    try:
        return storage.jsonl_records(storage.read_log_lines(DIAG_PATH))
    except OSError:
        return []


def _diag_boot():
    """בעלייה: בדיקה שלא נעצרה (airam-web/ה-Pi אותחלו באמצעה — לרוב *בדיוק* מה
    שבודקים) ממשיכה, עם שורת resume; אחרת רק משחזרים את "הבדיקה האחרונה" לייצוא."""
    rows = _diag_read_rows()
    start = None
    for r in rows:
        if r.get("ev") == "start":
            start = r
    if not start:
        return
    after = [r for r in rows if (r.get("t") or 0) >= start.get("t", 0)]
    stopped = next((r for r in after if r.get("ev") == "stop"), None)
    t0 = start.get("t") or 0
    if stopped is None and time.time() < t0 + DIAG_MAX_SEC:
        _diag_launch(t0, start.get("label") or "", {"ev": "resume", "t": round(time.time(), 2),
                                                     "reason": "airam-web עלה מחדש", "version": VERSION})
        log.info("יומן בדיקה: ממשיך את '%s' אחרי עלייה", start.get("label"))
        return
    end = stopped["t"] if stopped else max((r.get("t") or 0) for r in after)
    with _diag_lock:
        _diag["last_run"] = {"started_at": t0, "ended_at": end, "label": start.get("label") or ""}


@app.before_request
def _diag_t0():
    request.environ["airam.t0"] = time.monotonic()


@app.after_request
def _diag_after_request(resp):
    """בזמן בדיקה: כל פעולה משנת-מצב מהטלפון נכנסת לציר הזמן — מה נשלח, מה חזר,
    ושגיאה אם הייתה. ⚠ לא כותבים headers (‏PIN), ו-trace מיובא מוחלף בגודלו."""
    try:
        if (request.method in ("POST", "PUT", "DELETE") and request.path.startswith("/api/")
                and not request.path.startswith("/api/diag")):
            with _diag_lock:
                active = _diag["active"]
            if active:
                body = request.get_json(silent=True)
                if isinstance(body, dict):
                    body = {k: (f"<{len(str(v))} תווים>" if k in DIAG_BODY_SKIP else v)
                            for k, v in body.items()}
                txt = json.dumps(body, ensure_ascii=False) if body is not None else ""
                err = None
                if resp.status_code >= 400 and resp.is_json:
                    err = (resp.get_json(silent=True) or {}).get("error")
                t0 = request.environ.get("airam.t0")
                if _diag_event({"ev": "request", "method": request.method, "path": request.path,
                                "status": resp.status_code, "body": txt[:DIAG_BODY_MAX],
                                "error": err, "ms": round((time.monotonic() - t0) * 1000) if t0 else None}):
                    with _diag_lock:
                        _diag["requests"] += 1
    except Exception:
        log.warning("יומן בדיקה: רישום בקשה נכשל", exc_info=True)   # לעולם לא מפילים את התשובה
    return resp


@app.route("/api/diag", methods=["GET", "POST"])
def api_diag():
    """GET: מצב יומן הבדיקה. POST {action: start|stop, label?}. דרך _guard."""
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        action = data.get("action")
        if action == "start":
            label = str(data.get("label") or "").strip()[:DIAG_LABEL_MAX]
            if not _diag_start(label):
                return jsonify(ok=False, error="בדיקה כבר רצה — עצור אותה קודם", **_diag_status()), 409
        elif action == "stop":
            _diag_stop("user")
        else:
            return jsonify(ok=False, error="נדרש action: start|stop"), 400
    return jsonify(ok=True, **_diag_status())


@app.route("/api/diag/mark", methods=["POST"])
def api_diag_mark():
    """{label} — "עכשיו ניתקתי את המשני" וכו'. 409 כשאין בדיקה: סימון שלא נרשם
    לא יכול להיראות כאילו נרשם."""
    data = request.get_json(silent=True) or {}
    label = str(data.get("label") or "").strip()[:DIAG_LABEL_MAX]
    if not label:
        return jsonify(ok=False, error="חסרה תווית"), 400
    if not _diag_event({"ev": "mark", "label": label}):
        return jsonify(ok=False, error="אין בדיקה פעילה — התחל בדיקה קודם"), 409
    with _diag_lock:
        _diag["marks"] += 1
    return jsonify(ok=True, mark=label, **_diag_status())


def _diag_window(args):
    """(start, end, test_info). ‏?minutes=N ⇒ N הדקות האחרונות; אחרת הבדיקה
    הפעילה/האחרונה (מדקה לפניה); ואם לא הייתה — DIAG_EXPORT_DEFAULT_MIN."""
    now = time.time()
    st = _diag_status()
    if not args.get("minutes"):
        if st["active"]:
            return st["started_at"] - DIAG_PRE_SEC, now, {"label": st["label"], "active": True}
        if st["last_run"]:
            lr = st["last_run"]
            return (lr["started_at"] - DIAG_PRE_SEC, min(now, lr["ended_at"] + DIAG_PRE_SEC),
                    {"label": lr["label"], "active": False})
    try:
        minutes = int(args.get("minutes") or DIAG_EXPORT_DEFAULT_MIN)
    except (TypeError, ValueError):
        minutes = DIAG_EXPORT_DEFAULT_MIN
    minutes = min(DIAG_EXPORT_MAX_MIN, max(1, minutes))
    return now - minutes * 60, now, None


def _diag_journal(start, end, match):
    """journald כ-JSON בחלון. מחזיר (recs, error) — error כשאין הרשאה/אין journalctl."""
    out, err, rc = _diag_cmd(["journalctl", "-o", "json", "--no-pager", "-n", str(DIAG_JOURNAL_MAX),
                              "--since", f"@{int(start)}", "--until", f"@{int(end) + 1}", *match])
    if out is None:
        return [], err
    note = None
    if err and re.search(r"insufficient permissions|not seeing messages", err, re.I):
        note = "אין הרשאה מלאה ליומן (המשתמש airam צריך להיות בקבוצה systemd-journal)"
    elif rc not in (0, None) and not out:
        note = (err or "").strip()[:200] or f"journalctl rc={rc}"
    return diag.parse_journal_json(out), note


def _diag_redacted_state():
    st = appstate.load_state()
    station = st.get("station")
    if isinstance(station, dict):
        st["station"] = {**station, **{k: round(station[k], 2) for k in ("lat", "lon")
                                       if isinstance(station.get(k), (int, float))}}
    return st


def _diag_boot_time():
    try:
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def _diag_system_text(units_show):
    L = []
    try:
        u = os.uname()
        L.append(f"uname: {u.sysname} {u.release} {u.machine}")
    except AttributeError:
        pass
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("PRETTY_NAME="):
                L.append("os: " + line.split("=", 1)[1].strip('"'))
    except OSError:
        pass
    L.append(f"python: {sys.version.split()[0]} · airam: {VERSION}")
    try:
        L.append(f"uptime: {float(Path('/proc/uptime').read_text().split()[0]) / 3600:.2f} שעות")
        L.append("loadavg: " + Path("/proc/loadavg").read_text().strip())
    except (OSError, ValueError, IndexError):
        pass
    L.append(f"memory: total={_meminfo_mb('MemTotal')}MB available={_meminfo_mb('MemAvailable')}MB")
    try:
        du = shutil.disk_usage(appstate.STATE_PATH.parent)
        L.append(f"disk {appstate.STATE_PATH.parent}: free={du.free // 2**20}MB / total={du.total // 2**20}MB")
    except OSError:
        pass
    for args in (["lsusb"], ["lsusb", "-t"]):
        out, err, _rc = _diag_cmd(args, timeout=5)
        L += ["", "$ " + " ".join(args), (out or err or "").rstrip()]
    L += ["", "$ systemctl show (יחידות)"]
    for unit, props in units_show.items():
        L.append(f"[{unit}] " + " ".join(f"{k}={v}" for k, v in props.items() if k != "Id"))
    return "\n".join(L) + "\n"


def _diag_collect(start, end, test):
    """אוסף הכול לחבילה. מחזיר (summary_md, files[(name, text, desc)]). לעולם לא
    זורק בגלל מקור בודד — כשל נרשם ב-collect_errors ומופיע בדוח."""
    d, files, errors = {"collect_errors": []}, [], []

    def guard(name, fn):
        try:
            return fn()
        except Exception as e:     # מקור אחד שנכשל לא מבטל את כל החבילה
            log.warning("ייצוא לוגים: %s נכשל", name, exc_info=True)
            errors.append(f"{name}: {e}")
            return None

    health = guard("health", _health_snapshot)
    d["health"] = health
    snapshot = {
        "version": VERSION, "created": time.time(), "window": [start, end], "health": health,
        "state": guard("state", _diag_redacted_state), "sdr": guard("sdr", _sdr_status),
        "power": guard("power", _read_power), "transcribe": guard("transcribe", _tx_status),
        "rflog": guard("rflog", experiment._rflog_status), "diag": guard("diag", _diag_status),
        "scan": guard("scan", lambda: {**_scan_status, "alive": _scan_thread_alive()}),
        "xcheck": guard("xcheck", lambda: dict(_xc_stats)),
        "adsb_net": guard("adsb", adsb.net_status),
    }
    d["sdr"], d["power"] = snapshot["sdr"], snapshot["power"]

    out, err, _rc = _diag_cmd(["systemctl", "show", "-p", "Id,ActiveState,SubState,Result,NRestarts,"
                               "ActiveEnterTimestamp,InactiveEnterTimestamp,ExecMainStatus",
                               *[u + ".service" for u in _diag_units()]])
    d["units"] = diag.parse_systemctl_show(out) if out is not None else {}
    if out is None:
        errors.append(f"systemctl show: {err}")
    snapshot["units"] = d["units"]

    match = [a for u in _diag_units() for a in ("-u", u)]
    recs, d["units_error"] = _diag_journal(start, end, match)
    d["unit_events"], unit_tl = diag.unit_events(recs)
    files.append(("logs/services.log", diag.format_journal(recs),
                  "כל היחידות יחד, לפי הזמן (journald)"))
    for unit in _diag_units():
        mine = [r for r in recs if r["unit"] == unit]
        if mine:
            files.append((f"logs/{unit}.log", diag.format_journal(mine), f"journald של {unit}"))
    krecs, d["kernel_error"] = _diag_journal(start, end, ["_TRANSPORT=kernel"])
    d["kernel_events"] = None if (d["kernel_error"] and not krecs) else diag.kernel_events(krecs)
    files.append(("logs/kernel.log", diag.format_journal(krecs), "הקרנל: מתח, USB, זיכרון"))

    rows = [r for r in _diag_read_rows() if start <= (r.get("t") or 0) <= end]
    d["samples"] = [r for r in rows if r.get("ev") == "sample"]
    files.append(("test-log.jsonl", "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                  "יומן הבדיקה: דגימה כל 5ש', סימונים ופעולות מהטלפון"))

    msgs, sample = {}, {}
    for src, path in (("acars", ACARS_LOG_PATH), ("vdl2", VDL2_LOG_PATH), ("satcom", SATCOM_LOG_PATH)):
        recs_m = [r for r in storage.read_jsonl_log(path) if start <= (r.get("t") or 0) <= end]
        msgs[src] = len(recs_m)
        sample[src] = recs_m[-DIAG_MSG_SAMPLE:]
    act = [r for r in storage.jsonl_records(guard("activity", lambda: storage.read_log_lines(ACTIVITY_PATH)
                                           if ACTIVITY_PATH.exists() else []) or [])
           if start <= (r.get("ts") or 0) <= end]
    msgs["שידורי קול"] = len(act)
    sample["activity"] = act[-DIAG_MSG_SAMPLE:]
    d["messages"] = msgs
    files.append(("messages.json", json.dumps({"counts": msgs, "last": sample}, ensure_ascii=False,
                                              indent=1, default=str), "ספירה + ההודעות האחרונות בחלון"))

    for p in _diag_config_files():
        try:
            files.append((f"config/{p.name}", p.read_text(errors="replace"), f"תוכן {p}"))
        except OSError:
            continue

    boot = _diag_boot_time()
    d["boot_time"], d["boot_in_window"] = boot, bool(boot and start <= boot <= end)
    files.append(("system.txt", guard("system", lambda: _diag_system_text(d["units"])) or "",
                  "מערכת: גרסאות, זיכרון, דיסק, USB, יחידות"))
    d["collect_errors"] = errors
    snapshot["collect_errors"] = errors
    files.append(("snapshot.json", json.dumps(snapshot, ensure_ascii=False, indent=1, default=str),
                  "המצב ברגע הייצוא: state, health, SDR, מתח, תמלול, סריקה, הצלבה"))

    checks = diag.analyze(d)
    timeline, dropped = diag.build_timeline(rows, unit_tl, d["kernel_events"])
    meta = {"version": VERSION, "start": start, "end": end, "created": time.time(),
            "app_mode": (health or {}).get("app_mode"), "boot_time": boot,
            "test_label": test["label"] if test else None,
            "test_active": bool(test and test["active"])}
    listing = [("summary.md", "הדוח הזה")] + [(n, desc) for n, _t, desc in files]
    return diag.render_summary(meta, checks, timeline, dropped, listing), files, checks


@app.route("/api/diag/export")
def api_diag_export():
    """ZIP אחד לבדיקה: summary.md (בדיקות + ציר זמן) ראשון, ואחריו היומנים המלאים.
    ‏?minutes=N — N הדקות האחרונות; בלי — הבדיקה הפעילה/האחרונה (ר' _diag_window)."""
    import zipfile
    start, end, test = _diag_window(request.args)
    summary, files, _checks = _diag_collect(start, end, test)
    stamp = time.strftime("%Y%m%d-%H%M")
    root = f"airam-diag-{stamp}"
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(f"{root}/summary.md", summary)
            for name, text, _desc in files:
                z.writestr(f"{root}/{name}", text)
        tmp.close()
        resp = send_file(tmp.name, mimetype="application/zip", as_attachment=True,
                         download_name=f"{root}.zip")
    except Exception:
        tmp.close()
        os.unlink(tmp.name)
        raise
    resp.call_on_close(lambda: os.path.exists(tmp.name) and os.unlink(tmp.name))
    return resp


@app.route("/api/diag/summary")
def api_diag_summary():
    """אותו ניתוח בלי ה-ZIP — לתצוגה בטלפון (רשימת הבדיקות + הטקסט המלא)."""
    start, end, test = _diag_window(request.args)
    summary, _files, checks = _diag_collect(start, end, test)
    return jsonify(ok=True, start=start, end=end, overall=diag.worst(c["level"] for c in checks),
                   checks=checks, summary=summary)


def _rtl_airband_start_wall():
    """זמן-קיר שבו התהליך הראשי הנוכחי של rtl_airband הופעל, או None.
    ExecMainStartTimestampMonotonic הוא CLOCK_MONOTONIC במיקרו-שניות — אותו שעון
    כמו time.monotonic() בלינוקס, כך שההמרה מדויקת בלי לפענח מחרוזת תאריך.
    ‏`systemctl show` קורא בלבד — לא דורש sudo."""
    try:
        r = subprocess.run(["systemctl", "show", "-p", "ExecMainStartTimestampMonotonic",
                            "--value", "rtl_airband"], capture_output=True, text=True, timeout=5)
        us = int((r.stdout or "0").strip() or 0)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    if us <= 0:
        return None
    return round(time.time() - (time.monotonic() - us / 1e6), 2)


@app.route("/api/experiment", methods=["GET", "POST"])
def api_experiment():
    """GET: מצב הניסוי (+ result כשהסתיים). POST {action}: start / confirm (הפעולה
    הפיזית בוצעה) / abort. דרך _guard (POST)."""
    if request.method == "GET":
        return jsonify(ok=True, **experiment._experiment_status())
    action = (request.get_json(silent=True) or {}).get("action")
    if action == "start":
        payload, code = experiment._experiment_start()
        return jsonify(**payload), code
    with experiment._exp_lock:
        running, stop_evt, confirm_evt, waiting = (experiment._exp["running"], experiment._exp["stop"],
                                                   experiment._exp["confirm"], experiment._exp["waiting"])
    if action == "confirm":
        if not running or not waiting:
            # ⚠ הסטטוס כבר מכיל error (של הריצה האחרונה) — מיזוג, לא kwargs כפולים
            return jsonify({**experiment._experiment_status(), "ok": False,
                            "error": "אין פעולה שממתינה לאישור כרגע"}), 409
        confirm_evt.set()
        return jsonify(ok=True, **experiment._experiment_status())
    if action == "abort":
        if running and stop_evt:
            stop_evt.set()
        return jsonify(ok=True, **experiment._experiment_status())
    return jsonify(ok=False, error="action לא מוכר"), 400


@app.route("/api/airspace")
def api_airspace():
    """מסלול נחיתות/המראות פעיל ומצב GPS, מנותחים מ-ADS-B (ראה adsb.py).
    קורא snapshot בזיכרון בלבד - אף פעם לא חוסם ואף פעם לא 500."""
    return jsonify(adsb.snapshot())


@app.route("/api/replay/buffer")
def api_replay_buffer():
    """מצב ה-buffer המתגלגל של מסלולי ADS-B (adsb.py, שלב 1 ב-
    docs/session-replay-design.md) — לפני POST /api/sessions (שלב 2, טרם
    מומש), כדי שה-UI יוכל לומר 'ניתן לשמור עד X דקות אחורה' ולהציג פערי-קליטה.
    ‏`clips_available` נבדק *כאן* ולא ב-adsb.py: adsb.py לא מכיר את REC_DIR,
    ורק app.py יודע לחפש גם ב-saved/ (`_iter_recordings`)."""
    buf = adsb.read_track_buffer()
    clips_available = False
    if buf["t_oldest"] is not None:
        for p in recordings._iter_recordings():
            try:
                if p.stat().st_mtime >= buf["t_oldest"]:
                    clips_available = True
                    break
            except OSError:
                continue
    return jsonify(ok=True, t_oldest=buf["t_oldest"], samples=buf["samples"],
                   gaps=buf["gaps"], clips_available=clips_available)


def _vcgencmd(*args):
    """מריץ vcgencmd ומחזיר stdout (או None אם לא Pi / לא מותקן / נכשל)."""
    try:
        r = subprocess.run(["vcgencmd", *args], capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


# cache קצר ל-/api/power: כל בקשה מריצה *שלושה* תהליכי vcgencmd, וה-UI מושך כל
# 5 שניות **בכל טאב פתוח** => עם N טאבים, 3N תהליכים כל 5 שניות. במצב סוללה זה
# בזבוז ממשי. TTL קצר מספיק כדי שהחיווי יישאר חי (הוא ממילא נדגם כל 5ש').
_POWER_TTL = 2.0
_power_cache = {"at": 0.0, "payload": None}
_POWER_LOCK = threading.Lock()


def _reset_power_cache():
    """מאפס את ה-cache. נחוץ לבדיקות (מצב גלובלי דולף בין בדיקות שמחליפות את
    _vcgencmd), ומשמש גם כנקודת-איפוס מפורשת אם יידרש בעתיד."""
    with _POWER_LOCK:
        _power_cache["at"], _power_cache["payload"] = 0.0, None


def _read_power():
    """קורא את מצב האספקה מ-vcgencmd. מחזיר dict (ה-payload של /api/power) או
    None כשאין vcgencmd (לא Pi). מופרד מה-route כדי שיהיה ניתן ל-cache ולבדיקה."""
    out = _vcgencmd("get_throttled")
    if out is None:
        return None                # אין vcgencmd (לא Pi / חסר) => הממשק מסתיר את החיווי

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

    return {"ok": True, "throttled": hex(flags),
            "undervolt_now": bool(flags & 0x1),
            "throttle_now": bool(flags & 0x4),
            "undervolt_ever": bool(flags & 0x10000),
            "throttle_ever": bool(flags & 0x40000),
            "volts_in": volts_in, "temp": temp}


def _power_cached():
    """_read_power דרך ה-cache (ר' _POWER_TTL) — משותף ל-/api/power וליומן הבדיקה."""
    now = time.time()
    with _POWER_LOCK:
        if _power_cache["payload"] is not None and now - _power_cache["at"] < _POWER_TTL:
            return _power_cache["payload"]
        cached = _read_power()
        _power_cache["at"], _power_cache["payload"] = now, cached
        return cached


@app.route("/api/power")
def api_power():
    """מצב אספקת המתח ל-Pi (שימושי במיוחד עם סוללה ניידת):
      get_throttled  -> דגלי undervoltage/throttling (כל דגמי Pi)
      pmic_read_adc  -> מתח כניסה 5V בפועל (Pi 5 בלבד)
      measure_temp   -> טמפ' ליבה
    ביטים של get_throttled: 0=under-volt עכשיו · 2=throttled עכשיו ·
    16=under-volt קרה מאז אתחול · 18=throttling קרה.
    מוגש מ-cache בן _POWER_TTL שניות (ר' שם) — מכווץ טאבים מקבילים לדגימה אחת."""
    cached = _power_cached()
    if cached is None:
        return jsonify(ok=False)
    return jsonify(**cached)


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
        if_gain = max(config.IFGR_MIN, min(config.IFGR_MAX, int(data.get("if_gain", config.IF_GAIN_DEFAULT))))
    except (TypeError, ValueError):
        if_gain = config.IF_GAIN_DEFAULT
    try:
        rf_gain = max(config.RFGR_MIN, min(config.RFGR_MAX, int(data.get("rf_gain", config.RF_GAIN_DEFAULT))))
    except (TypeError, ValueError):
        rf_gain = config.RF_GAIN_DEFAULT

    squelch_mode = str(data.get("squelch_mode", "auto")).lower()
    if squelch_mode not in SQUELCH_MODES:
        squelch_mode = "auto"
    try:
        squelch_snr = float(data.get("squelch_snr", config.SNR_DEFAULT))
    except (TypeError, ValueError):
        squelch_snr = config.SNR_DEFAULT
    squelch_snr = max(config.SNR_MIN, min(config.SNR_MAX, squelch_snr))

    # הגדרות "חומרה" קבועות (לא חלק מכיוונון רגעי): מפתח שלא נשלח ⇒ הערך השמור, לא
    # ברירת המחדל — אחרת כל קורא שבונה פרמטרים בעצמו ("האזן למגדל", curl, ‏/api/mode
    # עם freq) היה מאפס בשקט את מסנן הערוץ ואת ה-notch שהמשתמש בחר.
    saved = None
    if "chan_filter" not in data or "fm_notch" not in data:
        saved = appstate.load_state()
    # רשימה סגורה => שום ערך מהלקוח לא נכתב לקונפיג כמות שהוא
    chan_filter = str(data["chan_filter"] if "chan_filter" in data
                      else saved.get("chan_filter", config.CHAN_FILTER_DEFAULT))
    if chan_filter not in config.CHAN_FILTERS:
        chan_filter = config.CHAN_FILTER_DEFAULT
    notch_raw = data["fm_notch"] if "fm_notch" in data else saved.get("fm_notch", False)
    fm_notch = notch_raw if isinstance(notch_raw, bool) else str(notch_raw).lower() in ("true", "1", "on", "yes")

    return {"freq": freq, "mod": mod, "agc": agc, "if_gain": if_gain, "rf_gain": rf_gain,
            "squelch_mode": squelch_mode, "squelch_snr": squelch_snr,
            "chan_filter": chan_filter, "fm_notch": fm_notch}, None


def _voice_tune(params, keep_secondary=None):
    """מכוונן קול (rtl_airband). מבטיח יציאה ממצב ACARS/VDL2 תחילה (משחרר את ה-SDR).
    keep_secondary: None = אוטומטי (‏/api/tune — במצב "קול + ACARS" ה-ACARS על המקלט
    השני ממשיך); False = מעבר מפורש לקול בלבד (/api/mode voice).
    מחזיר (payload, http_status). serialized תחת TUNE_LOCK."""
    if not TUNE_LOCK.acquire(blocking=False):
        # state בתשובה => ה-UI מיישר את התצוגה האופטימית חזרה למציאות
        return {"ok": False, "error": "כיוונון אחר מתבצע כרגע — המתן שנייה ונסה שוב",
                "state": appstate.load_state()}, 409
    try:
        # תפסנו את הנעילה — הבקשה תקינה, עוצרים סבב קודם (אם יש), בדיוק כמו
        # שאר המצבים ב-/api/mode (ר' ההערה שם): *לא* לפני הנעילה, אחרת ניסיון
        # כיוונון שנכשל לתפוס נעילה (409, למשל בדיקת אנטנה מקבילה) היה עוצר
        # סבב תקין "בחינם" ומשאיר "scan זומבי" — צרכן רץ בלי thread שממשיך אותו.
        _scan_stop_thread()
        prev = appstate.load_state()   # ההגדרות האחרונות שעבדו, לרולבק במקרה כישלון
        pm = prev.get("app_mode")
        if keep_secondary is None:
            # ⚠ לפי הכוונה השמורה, לא לפי is-active של המשני: משני ב-"activating" (sleep המדורג,
            # RestartSec) בדיוק ברגע הכיוונון הוציא בשקט מהמצב הכפול — וה-reconcile כבר לא החזיר.
            keep_secondary = pm in COMPOSITE_SECONDARY
        vmode = pm if (keep_secondary and pm in COMPOSITE_SECONDARY) else "voice"
        if vmode == "voice2" and abs(params["freq"] - _voice2_params(prev)[0]) < 0.0005:
            # כמו _parse_voice2 מהצד השני: שני מקלטים על ערוץ אחד, ושמות ההקלטות
            # (<תאריך>_<Hz>) של שני התהליכים מתנגשים באותה שנייה
            return {"ok": False, "error": "התדר הזה כבר על הערוץ השני — בחר תדר אחר",
                    "state": prev}, 400
        # ⚠ השמירה ממזגת (_merge_state), לא דורסת: params מכיל רק שדות קול
        # (freq/mod/gain/squelch). דריסה מלאה הייתה מוחקת satcom_bias_tee/
        # satcom_gain/signal_baseline/scan_plan וכו' — עם satcom_bias_tee במיוחד,
        # load_state הבא היה ממזג מ-DEFAULT_STATE=True ומדליק bias-T מחדש בכניסה
        # הבאה ל-SATCOM גם כשהמשתמש כבר מזין LNA חיצוני (§12).
        log.info("tune %.3f MHz mod=%s agc=%s if_gain=%d rf_gain=%d squelch=%s snr=%.1f filter=%s fm_notch=%s (from %s)",
                 params["freq"], params["mod"], params["agc"], params["if_gain"],
                 params["rf_gain"], params["squelch_mode"], params["squelch_snr"],
                 params["chan_filter"], params["fm_notch"], request.remote_addr)

        err, detail, sdr_down = _enter_voice(params, keep_secondary=keep_secondary)
        if err:
            log.warning("tune %.3f MHz failed: %s (sdr_down=%s)", params["freq"], err, sdr_down)
            if sdr_down:
                # ה-SDR מנותק: רולבק ייתקע באותה המתנה בדיוק, אז מדלגים עליו.
                # הקונפיג החדש נשאר על הדיסק וייקלט כשהמכשיר יחובר (udev מרים
                # את השירותים) => שומרים state תואם לדיסק, לא את הקודם.
                new_state = appstate._merge_state({**params, "app_mode": vmode})
                return {"ok": False, "detail": detail, "state": new_state,
                        "error": err + " — התדר יוחל אוטומטית כשה-SDR יחובר"}, 500
            # config רע => מנסים את קונפיג הקול האחרון שעבד (retry בתוך קול, לא
            # עליונות-מצב). רק אם גם הוא לא עולה — נופלים ל-off לפי הדוקטרינה.
            if _rollback(prev):
                return {"ok": False, "error": err + " (חזרתי לתדר הקודם)",
                        "detail": detail, "state": {**prev, "app_mode": vmode}}, 500
            return _fail_to_off(prev, err + " — וגם החזרה לתדר הקודם נכשלה",
                                detail, "voice tune")

        # נשמר רק אחרי שאומת שהשירות חי => state תמיד משקף הגדרות שעובדות
        new_state = appstate._merge_state({**params, "app_mode": vmode})
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


def _parse_voice2(data, st):
    """{voice2_freq, voice2_mod} מבקשה (freq2/mod2) או מה-state. מחזיר (changes, error).
    אותו תדר כמו הראשי ⇒ שגיאה: שני מקלטים על ערוץ אחד הם בזבוז חשמל, ושמות ההקלטות
    (<תאריך>_<Hz>) היו מתנגשים באותה שנייה."""
    if "freq2" in data:
        try:
            f2 = float(data.get("freq2"))
        except (TypeError, ValueError):
            return None, "תדר שני לא תקין"
        if not (0.1 <= f2 <= 1999.5):
            return None, "תדר שני מחוץ לטווח (0.1–1999.5 MHz)"
    else:
        f2, _m = _voice2_params(st)
    mod_src = data["mod2"] if "mod2" in data else st.get("voice2_mod", "am")
    m2 = "nfm" if str(mod_src).lower() == "nfm" else "am"
    try:
        f1 = float(st.get("freq"))
    except (TypeError, ValueError):
        f1 = None
    if f1 is not None and abs(f1 - f2) < 0.0005:
        return None, "הערוץ השני זהה לראשי — בחר תדר אחר"
    return {"voice2_freq": round(f2, 4), "voice2_mod": m2}, None


@app.route("/api/tune2", methods=["POST"])
def api_tune2():
    """כיוונון הערוץ השני (קול כפול) — מאתחל רק את rtl_airband2, הראשי לא נוגע.
    409 מחוץ למצב voice2 (אין מה לכוונן). כשל ⇒ חוזרים לתדר השני הקודם; גם זה נכשל ⇒
    הראשי ממשיך, והמצב מדווח כתקלה (mode_ok=False) — ה-reconcile ינסה שוב."""
    data = request.get_json(silent=True) or {}
    if not TUNE_LOCK.acquire(blocking=False):
        return jsonify(ok=False, error="כיוונון אחר מתבצע כרגע — המתן שנייה ונסה שוב",
                       state=appstate.load_state()), 409
    try:
        st = appstate.load_state()
        if st.get("app_mode") != "voice2":
            return jsonify(ok=False, error="הערוץ השני פעיל רק במצב קול כפול", state=st), 409
        v2, verr = _parse_voice2(data, st)
        if verr:
            return jsonify(ok=False, error=verr, state=st), 400
        log.info("tune2 %.3f MHz mod=%s (from %s)", v2["voice2_freq"], v2["voice2_mod"],
                 request.remote_addr)
        err, detail = _start_voice2({**st, **v2})
        if err:
            log.warning("tune2 %.3f failed: %s — חוזר לתדר הקודם", v2["voice2_freq"], err)
            back, _d = _start_voice2(st)
            msg = err + (" (חזרתי לתדר הקודם)" if back is None else " — וגם החזרה לתדר הקודם נכשלה")
            return jsonify(ok=False, error=msg, detail=detail, state=appstate.load_state()), 500
        new = appstate._merge_state(v2)
        return jsonify(ok=True, voice2_freq=new["voice2_freq"], voice2_mod=new["voice2_mod"])
    finally:
        TUNE_LOCK.release()


def _acars_adsb():
    """העשרת ADS-B לזנבות שבזיכרון ה-ACARS (היתוך לפי רישום מנורמל). קריאת
    snapshot בזיכרון בלבד — אין רשת בנתיב הבקשה, אין אינטרנט => dict ריק."""
    with _acars_lock:
        regs = {adsb.norm_reg(m.get("tail")) for m in _acars_msgs if m.get("tail")}
    regs.discard(None)
    return adsb.aircraft_snapshot(regs) if regs else {}


@app.route("/api/acars")
def api_acars():
    """ר' _feed_response."""
    return _feed_response("acars")


ACARS_EXPORT_COLS = ["time_iso", "timestamp", "freq", "level", "snr", "mode", "label",
                     "category", "group", "dir", "tail", "flight", "actype", "msgno", "error",
                     "lat", "lon", "pos_src", "text"]
# ייצוא VDL2 = אותן עמודות + icao (זהות AVLC לפריימים בלי רישום) אחרי flight
VDL2_EXPORT_COLS = ["time_iso", "timestamp", "freq", "level", "snr", "mode", "label",
                    "category", "group", "dir", "tail", "flight", "icao", "actype", "msgno",
                    "error", "lat", "lon", "pos_src", "text"]


def _read_acars_log():
    return storage.read_jsonl_log(ACARS_LOG_PATH)


def _read_vdl2_log():
    return storage.read_jsonl_log(VDL2_LOG_PATH)


def _export_response(recs, cols, basename):
    """בונה תגובת ייצוא (CSV עם BOM ל-Excel / JSON) מרשומות מנורמלות. משותף
    ל-/api/acars/export ול-/api/vdl2/export — אותה סכמת כרטיס, עמודות לפי cols."""
    fmt = (request.args.get("format") or "csv").lower()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    if fmt == "json":
        resp = app.response_class(json.dumps(recs, ensure_ascii=False, indent=1),
                                  mimetype="application/json")
        fname = f"{basename}-{stamp}.json"
    else:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(cols)
        for r in recs:
            t = r.get("t")
            row = []
            for c in cols:
                if c == "time_iso":
                    row.append(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else "")
                elif c == "timestamp":
                    row.append(t)
                elif c == "text":
                    row.append(storage.csv_safe((r.get("text") or "").replace("\r", " ").replace("\n", " ")))
                else:
                    row.append(storage.csv_safe(r.get(c)))
            w.writerow(row)
        # BOM => Excel מזהה UTF-8 ומציג עברית (category) נכון
        resp = app.response_class("﻿" + buf.getvalue(),
                                  mimetype="text/csv; charset=utf-8")
        fname = f"{basename}-{stamp}.csv"
    resp.headers["Content-Disposition"] = f'attachment; filename="{fname}"'
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/acars/export")
def api_acars_export():
    """ייצוא כל הודעות ה-ACARS השמורות לקובץ מסודר (לניתוח offline).
    ?format=csv (ברירת מחדל) | json. GET => בלי PIN (כמו שאר ה-GET)."""
    return _export_response(_read_acars_log(), ACARS_EXPORT_COLS, "airam-acars")


def _vdl2_adsb():
    """העשרת ADS-B לזנבות שבזיכרון ה-VDL2 (היתוך לפי רישום מנורמל, כמו _acars_adsb).
    פריימים עם icao בלבד (בלי reg) אינם מועשרים — adsb.py ממופתח לפי רישום."""
    with _vdl2_lock:
        regs = {adsb.norm_reg(m.get("tail")) for m in _vdl2_msgs if m.get("tail")}
    regs.discard(None)
    return adsb.aircraft_snapshot(regs) if regs else {}


@app.route("/api/vdl2")
def api_vdl2():
    """ר' _feed_response."""
    return _feed_response("vdl2")


@app.route("/api/vdl2/export")
def api_vdl2_export():
    """ייצוא כל הודעות ה-VDL2 השמורות (vdl2.jsonl). ?format=csv | json."""
    return _export_response(_read_vdl2_log(), VDL2_EXPORT_COLS, "airam-vdl2")


def _read_satcom_log():
    return storage.read_jsonl_log(SATCOM_LOG_PATH)


SATCOM_EXPORT_COLS = ACARS_EXPORT_COLS   # אותה סכמת כרטיס בדיוק (בלי icao — ר' acars_parse._normalize_satcom)


@app.route("/api/satcom")
def api_satcom():
    """ר' _feed_response."""
    return _feed_response("satcom")


@app.route("/api/satcom/export")
def api_satcom_export():
    """ייצוא כל הודעות ה-SATCOM השמורות (satcom.jsonl). ?format=csv | json."""
    return _export_response(_read_satcom_log(), SATCOM_EXPORT_COLS, "airam-satcom")


def _fetch_satcom_web_state():
    """קורא GET /api/state מה-dashboard האבחוני המובנה של inmarsat-sniffer
    (‎--web=SATCOM_WEB_PORT, אומת מהמקור: options.c/web.c). מחזיר dict או None
    בכל כשל — satcom לא active, ה---web dashboard לא זמין/עוד לא עלה, timeout,
    או JSON לא תקין. לעולם לא מפיל את הקורא. לא מנסה HTTP כלל כש-satcom לא
    active (המקרה הנפוץ) — נמנע מ-connection-refused מיותר בכל poll."""
    return _fetch_satcom_web("/api/state", SATCOM_HEALTH_TIMEOUT)


def _fetch_satcom_web(path, timeout):
    """קורא נתיב שרירותי מלוח האבחון של inmarsat-sniffer (‎--web) ומחזיר dict או
    None בכל כשל. מנוע משותף ל-/api/state (health) ול-/api/spectrum — אותה
    התניה בדיוק: לא מנסים HTTP כלל כשהשירות לא active."""
    if not _is_active(SATCOM_SERVICE):
        return None
    url = f"http://127.0.0.1:{SATCOM_WEB_PORT}{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:   # noqa: S310 (לוקאלהוסט בלבד)
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


@app.route("/api/satcom/health")
def api_satcom_health():
    """אבחון SATCOM: proxy מקומי ל-dashboard האבחוני המובנה של inmarsat-sniffer
    (‎--web). חושף נעילת דמודולטור לכל ערוץ (lock) גם באפס הודעות מפוענחות —
    ההבדל בין "אין אנטנה"/"לא מכוון" ל"תקין, שקט כרגע" שחסר ב-/api/satcom
    הרגיל (זה בודק רק שהתהליך *רץ*, לא שהוא קולט). ‏available=False (לא
    שגיאה — ok תמיד True) כש-satcom כבוי, ה---web dashboard לא זמין, או
    התשובה לא תקינה — לעולם לא ממציאים ערך במקום זה (ר' §12 ב-CLAUDE.md)."""
    state = _fetch_satcom_web_state()
    if state is None:
        return jsonify(ok=True, available=False)
    channels = []
    for ch in (state.get("channels") or []):
        if not isinstance(ch, dict):
            continue
        channels.append({"ch": ch.get("ch"), "baud": ch.get("baud"),
                         "msgs": ch.get("msgs"), "age": ch.get("age"),
                         "mse": ch.get("mse"), "ebno": ch.get("ebno"),
                         "lock": bool(ch.get("lock"))})
    # spectrum_enabled מגיע מהכלי עצמו (web.c) ולא מה-state שלנו — כך ה-UI יודע
    # אם /api/satcom/spectrum באמת יעבוד *עכשיו*, ולא רק מה ביקשנו בכניסה
    # האחרונה (למשל אחרי שדרוג שהחליף את ה-unit בלי מעבר-מצב חדש).
    return jsonify(ok=True, available=True,
                   total_acars=state.get("total_acars"), feed_drops=state.get("feed_drops"),
                   spectrum=bool(state.get("spectrum_enabled")),
                   channels=channels, channels_locked=sum(1 for c in channels if c["lock"]),
                   channels_total=len(channels))


@app.route("/api/satcom/log")
def api_satcom_log():
    """זנב היומן של inmarsat-sniffer (‎journalctl -u airam-satcom).

    **למה זה route ולא שדה ב-/api/satcom/health:** שורות הפתיחה של המפענח הן
    האבחון החד-משמעי ביותר שיש — ‎"sdrplay: bias tee enabled" מול
    ‎"bias tee not supported on this model" (sdrplay.c, אומת מהמקור) עונה
    בוודאות אם ה-LNA בכלל מקבל מתח, ו-"Auto center freq"/"Active channels"
    מאשרות שהתוכנית שנטענה היא זו שציפינו לה. בשטח, מהטלפון, אין SSH — בלי
    זה אי אפשר לראות את זה בכלל. אבל זה **לא** נתון פולינג: כל קריאה היא
    fork ל-journalctl, וה-health כבר רץ בקצב 1s (ר' ההערה ב-pollSatcomHealth
    ב-index.html) — לכן על דרישה בלבד, בלחיצת כפתור."""
    try:
        r = subprocess.run(["journalctl", "-u", SATCOM_SERVICE, "-n",
                            str(SATCOM_LOG_TAIL_LINES), "--no-pager"],
                           capture_output=True, text=True, timeout=5)
        return jsonify(ok=True, log=r.stdout or "")
    except Exception as e:
        return jsonify(ok=False, error=str(e), log=""), 500


@app.route("/api/satcom/spectrum")
def api_satcom_spectrum():
    """ספקטרום baseband של ערוץ בודד מהדמודולטור של inmarsat-sniffer (proxy ל-
    GET /api/spectrum?ch=N&bins=N בלוח האבחון שלו, דורש ‎--spectrum).

    **למה זה קיים:** ‏ebno/lock ב-/api/satcom/health עונים "אין נעילה" באותה
    צורה בדיוק כשהאנטנה מנותקת, כשה-LNA לא מוזן, וכשהכיוון שגוי ב-5° — שלוש
    תקלות שונות לגמרי עם אותו חיווי. הספקטרום הוא הראיה הישירה היחידה שיש RF
    בכלל: רצפת רעש שמזנקת ~20-30dB ברגע שה-LNA מקבל מתח, וגבנון נראה לעין
    כשהאנטנה מכוונת. אנחנו מגישים את ‎mags_db **כמות שהוא** מהכלי ולא ממציאים
    ממנו סף/ציון (§12) — ההשוואה שהמשתמש עושה (LNA מחובר מול מנותק) היא
    המדידה, לא איזה מספר קסם שלנו.

    ‏available=False (לא שגיאה) כש-satcom כבוי, ‎--spectrum לא פעיל, או הערוץ
    לא קיים — בדיוק כמו api_satcom_health."""
    try:
        ch = int(request.args.get("ch", 0))
    except (TypeError, ValueError):
        ch = 0
    try:
        bins = int(request.args.get("bins", SATCOM_SPECTRUM_BINS))
    except (TypeError, ValueError):
        bins = SATCOM_SPECTRUM_BINS
    ch = max(0, ch)
    bins = min(1024, max(32, bins))          # אותם גבולות כמו web.c
    data = _fetch_satcom_web(f"/api/spectrum?ch={ch}&bins={bins}", SATCOM_SPECTRUM_TIMEOUT)
    if not data or not data.get("ok"):
        # reason מגיע מהכלי ("channel unavailable") — גם כש---spectrum כבוי.
        return jsonify(ok=True, available=False, ch=ch,
                       reason=(data or {}).get("reason"))
    mags = [m for m in (data.get("mags_db") or []) if isinstance(m, (int, float))]
    return jsonify(ok=True, available=True, ch=data.get("ch", ch),
                   baud=data.get("baud"), afc=bool(data.get("afc")),
                   mixer_hz=data.get("mixer_hz"), freq_center_hz=data.get("freq_center_hz"),
                   fs=data.get("fs"), lockingbw=data.get("lockingbw"),
                   mags_db=mags, bins=len(mags))


ROSTER_MAX = 200   # תקרת גודל תגובה — הישנים ביותר נגזמים


def _aircraft_identity(m):
    """מפתח זהות מטוס מהודעה מנורמלת (ACARS/VDL2/SATCOM): רישום מנורמל קודם
    (חוצה ACARS↔VDL2↔SATCOM↔ADS-B), אחרת icao (פריימי VDL2 בלי tail), אחרת
    מספר טיסה. ⚠ tail_is_station (ר' acars_parse._normalize_acars) מדיר את ה-tail מהזהות —
    בלעדיו הודעת squitter של תחנת-קרקע (SQ וכו') הייתה יוצרת שורת "מטוס" מזויפת
    ברוסטר המאוחד מכתובת התחנה עצמה."""
    reg = None if m.get("tail_is_station") else adsb.norm_reg(m.get("tail"))
    if reg:
        return ("reg", reg)
    if m.get("icao"):
        return ("icao", str(m["icao"]).upper())
    if m.get("flight"):
        return ("flight", str(m["flight"]).upper())
    return None


def _fold_roster_buckets(craft, from_type, to_types, field):
    """ממזג in-place bucket-ים מסוג from_type (למשל "icao") לתוך bucket שכבר
    יש לו זהות מסוג to_types (למשל "reg") שגם ה-field שלו (למשל "icao") תואם —
    אותו מטוס בפועל שדיווח לפעמים בלי הזהות החזקה (tail) ולפעמים איתה. ⚠
    רגרסיה אמיתית: VDL2 מערבב פריימי ACARS-over-AVLC (עם reg) ופריימי XID/x25
    (בלי reg, רק icao) מאותה כתובת AVLC בדיוק — בלי המיזוג הזה, _aircraft_identity
    לבד יוצר שני מפתחות שונים (("reg",...)  ו-("icao",...)) לאותו מטוס, והרוסטר
    המאוחד מציג אותו כשתי שורות נפרדות. פוסט-פאס (לא במהלך הצבירה עצמה) כי
    ה-reg/icao של bucket "חזק" יכולים עדיין להתמלא מאוחר יותר באותו מעבר."""
    index = {c[field]: k for k, c in craft.items() if k[0] in to_types and c.get(field)}
    for k in [k for k in craft if k[0] == from_type and craft[k].get(field) in index]:
        target_key = index[craft[k][field]]
        if target_key == k:
            continue
        target, src = craft[target_key], craft.pop(k)
        target["count"] += src["count"]
        target["sources"] |= src["sources"]
        if src["last_t"] is not None and (target["last_t"] is None or src["last_t"] >= target["last_t"]):
            target["last_t"] = src["last_t"]
            target["last_category"] = src["last_category"]
            target["last_group"] = src["last_group"]
            target["last_dir"] = src["last_dir"]
        target["tail"] = target["tail"] or src["tail"]
        target["flight"] = target["flight"] or src["flight"]
        target["icao"] = target["icao"] or src["icao"]
        target["actype"] = target["actype"] or src["actype"]
        if src["_pos_t"] is not None and (target["_pos_t"] is None or src["_pos_t"] >= target["_pos_t"]):
            target["lat"], target["lon"] = src["lat"], src["lon"]
            target["pos_src"], target["_pos_t"] = src["pos_src"], src["_pos_t"]


def _build_roster():
    """רוסטר מטוסים מאוחד: היתוך הודעות ACARS+VDL2+SATCOM (בזיכרון) + ADS-B חי,
    לפי זהות משותפת (רישום/icao/טיסה) — עצמאי לגמרי ממצב ה-SDR הפעיל, כי כל
    ה-listeners וה-thread של adsb.py רצים תמיד ברקע (ר' §12 ב-CLAUDE.md)."""
    craft = {}
    with _acars_lock:
        acars_snapshot = list(_acars_msgs)
    with _vdl2_lock:
        vdl2_snapshot = list(_vdl2_msgs)
    with _satcom_lock:
        satcom_snapshot = list(_satcom_msgs)
    for source, msgs in (("acars", acars_snapshot), ("vdl2", vdl2_snapshot),
                         ("satcom", satcom_snapshot)):
        for m in msgs:
            key = _aircraft_identity(m)
            if key is None:
                continue
            c = craft.setdefault(key, {
                "tail": None, "flight": None, "icao": None, "actype": None,
                "sources": set(), "count": 0, "last_t": None,
                "last_category": None, "last_group": None, "last_dir": None,
                "lat": None, "lon": None, "pos_src": None, "_pos_t": None,
            })
            c["sources"].add(source)
            c["count"] += 1
            t = m.get("t") or 0
            if c["last_t"] is None or t >= c["last_t"]:
                c["last_t"] = t
                c["last_category"] = m.get("category")
                c["last_group"] = m.get("group")
                c["last_dir"] = m.get("dir")
            # לא מזינים את כתובת התחנה עצמה כ-"tail" של הרשומה — גם אם ההודעה
            # הצטרפה תחת זהות icao/flight אחרת (השדה משמש להצגה ולחיפוש
            # ADS-B, לא רק לזהות ה-key).
            if not m.get("tail_is_station"):
                c["tail"] = c["tail"] or m.get("tail")
            c["flight"] = c["flight"] or m.get("flight")
            c["icao"] = c["icao"] or m.get("icao")
            c["actype"] = c["actype"] or m.get("actype")
            if m.get("lat") is not None and (c["_pos_t"] is None or t >= c["_pos_t"]):
                c["lat"], c["lon"], c["pos_src"], c["_pos_t"] = m["lat"], m["lon"], m.get("pos_src"), t
    _fold_roster_buckets(craft, "icao", ("reg",), "icao")
    _fold_roster_buckets(craft, "flight", ("reg", "icao"), "flight")
    regs = {adsb.norm_reg(c["tail"]) for c in craft.values() if c["tail"]}
    regs.discard(None)
    snap = adsb.aircraft_snapshot(regs) if regs else {}
    out = []
    for c in craft.values():
        c = {k: v for k, v in c.items() if not k.startswith("_")}
        c["sources"] = sorted(c["sources"])
        reg = adsb.norm_reg(c["tail"]) if c["tail"] else None
        if reg and reg in snap:
            c["adsb"] = snap[reg]
        out.append(c)
    out.sort(key=lambda c: c["last_t"] or 0, reverse=True)
    return out[:ROSTER_MAX]


@app.route("/api/aircraft")
def api_aircraft():
    """רוסטר מטוסים מאוחד (ACARS+VDL2+ADS-B) — חי בכל מצב, כולל standby/סריקה
    (הנתונים כבר בזיכרון/ADS-B, לא תלוי SDR הפעיל כרגע)."""
    return jsonify(ok=True, aircraft=_build_roster())


SESSION_FALLBACK_SEC = 3600.0   # אין סמן שמור (התקנה טרייה/שדרוג) => שעה אחורה, לא כל ההיסטוריה
SESSION_HIGHLIGHTS_MAX = 8      # תקרת הודעות ב"בולטות" — תמצית לסריקה מהירה, לא עוד פיד


@app.route("/api/session")
def api_session():
    """דוח סשן: 'מה קרה בזמן שלא הסתכלת' (ר' docs/field-station-roadmap.md).
    התשתית (_boot_restore/scan/שרידות reboot) בנויה לתחנה שרצה לבד לאורך זמן;
    בלי הדוח הזה המוצר היחיד שלה הוא פיד שדורש נוכחות רציפה. קורא מהדיסק
    (jsonl דרך _read_*_log), לא מהזיכרון — עקבי עם /api/<mode>?day= וזמין גם
    מיד אחרי restart. idempotent (לא מקדם סמן) — /api/session/ack עושה זאת
    במפורש. ‏?since=<epoch> אופציונלי לדריסת הסמן השמור (למשל מה-UI, לצפייה
    חוזרת)."""
    st = appstate.load_state()
    now = time.time()
    since = None
    raw_since = request.args.get("since")
    if raw_since:
        try:
            since = float(raw_since)
        except (TypeError, ValueError):
            since = None
    if since is None:
        since = st.get("last_session_view_at")
    if since is None:
        since = now - SESSION_FALLBACK_SEC
    since = min(since, now)   # שעון מערכת שהוזז אחורה לא ייתן חלון שלילי

    readers = {"acars": _read_acars_log, "vdl2": _read_vdl2_log, "satcom": _read_satcom_log}
    counts = {}
    ident_window, ident_before = set(), set()
    highlights = []
    # הודעה שלא אושרה משודרת שוב עם אותו MSN ואותו טקסט — ב-24.10.2026 ‏9H-SHO שידר
    # את אותו OUT (‏QP) 20 פעם. "בולטות" היא תמצית, לא פיד: מופע אחד (האחרון) לכל הודעה.
    hl_seen = set()
    for mode, reader in readers.items():
        try:
            recs = reader()
        except Exception:
            recs = []
        n_window = 0
        for r in recs:
            t = r.get("t") or 0
            ident = _aircraft_identity(r)
            if t < since:
                if ident:
                    ident_before.add(ident)
                continue
            if t >= now:   # שעון שהוזז קדימה — לא סופרים "עתיד"
                continue
            n_window += 1
            if ident:
                ident_window.add(ident)
            if r.get("notable"):
                highlights.append({"t": t, "mode": mode, "tail": r.get("tail"),
                                   "flight": r.get("flight"), "category": r.get("category"),
                                   "decoded": r.get("decoded"),
                                   "_key": (mode, ident, r.get("label"), r.get("msgno"),
                                            r.get("text"), r.get("decoded"))})
        counts[mode] = n_window
    highlights.sort(key=lambda h: h["t"], reverse=True)
    uniq = []
    for h in highlights:
        k = h.pop("_key")
        if k not in hl_seen:
            hl_seen.add(k)
            uniq.append(h)
    highlights = uniq[:SESSION_HIGHLIGHTS_MAX]

    try:
        airspace_series = adsb.session_series(since=since)
    except Exception:
        airspace_series = []

    return jsonify(ok=True, since=since, now=now, duration_sec=round(now - since, 1),
                   counts=counts, total=sum(counts.values()),
                   aircraft_count=len(ident_window), new_aircraft_count=len(ident_window - ident_before),
                   highlights=highlights, airspace_series=airspace_series)


@app.route("/api/session/ack", methods=["POST"])
def api_session_ack():
    """מסמן שהמשתמש ראה את דוח הסשן — מקדם את הסמן ל'עכשיו', כך שהדוח הבא
    יתחיל מכאן. פעולה מפורשת (לא חלק מ-GET) כדי ש-/api/session יישאר
    idempotent — פתיחה/רענון חוזרים של הכרטיס לא 'צורכים' אותו בטעות.
    ‏_merge_state (מפתח יחיד, תחת _STATE_LOCK) מספיק נגד lost update: גם מעברי
    המצב שומרים עכשיו דרכו. קודם זה היה תחת TUNE_LOCK, ולכן "✓ הבנתי" קיבל 409
    לאורך כל ניסוי כיול (~20 דק') וכל מעבר רגל בסריקה."""
    appstate._merge_state({"last_session_view_at": time.time()})
    return jsonify(ok=True)


# --- הצלבת ADS-B + אופק רדיו / BLOS (docs/adsb-crosscheck-design.md) ---------
# שלוש שכבות: adsb.py (נתונים: snapshot/buffer/lookup/trace), xcheck.py (ניתוח
# טהור), וכאן — תזמור, התמדת ראיות ו-API.
# ⚠ בידוד הרדיו (§12): ה-listeners רק *מכניסים לתור* (_xc_submit — heap בזיכרון,
# נעילה לרגע, בלי רשת ובלי I/O); כל ה-HTTP רץ ב-thread ה-worker או בבקשת "אמת"
# של המשתמש. ADS-B לא זמין => ההצלבה מדווחת "אין נתונים", הקליטה ממשיכה כרגיל.
XC_EVENTS_PATH = Path("/var/lib/airam/xcheck_events.jsonl")
XC_EVENTS_KEEP = 2000            # ראיות — שומרים הרבה (~5–10KB לאירוע)
XC_QUEUE_MAX = 400               # מעבר לזה הודעות לא מוצלבות (נספר ב-dropped) — הקליטה לא נפגעת
XC_RESULTS_MAX = 1500            # תקצירים לפיד (בזיכרון בלבד)
# שלב 2: ~40ש' אחרי הקליטה ה-poll (כל 15ש') כבר הניב נקודה *אחרי* הקליטה —
# ובזכותה אינטרפולציה במקום "הנקודה האחרונה לפני". לא שאלת איכות — תזמון ה-poll.
XC_RECHECK_SEC = 40.0
XC_LIVE_MAX_AGE_SEC = 120.0      # הודעה "חיה": snapshot/lookup חי עוד רלוונטיים לזמן הקליטה
XC_TRACK_WINDOW_SEC = 900.0      # מסלול למפה: ±15 דק' סביב הקליטה
XC_TRACK_MAX_POINTS = 400
XC_EVIDENCE_WINDOW_SEC = 300.0   # נקודות ADS-B "מקוריות" שנשמרות עם הראיה: ±5 דק'
XC_EVIDENCE_MAX_POINTS = 60
XC_VERIFY_BUDGET_SEC = 30.0      # "אמת": אחרי זה לא פותחים בקשת רשת נוספת (ה-UI מחכה ≤60ש')
XC_AUTO_LOOKUP = True            # ה-worker רשאי ל-lookup ממוקד כשהמטוס לא ב-snapshot
XC_EVENT_ID_RE = re.compile(r"^[0-9a-f]{16}$")
XC_SOURCES = ("acars", "vdl2", "satcom")
STATION_ALT_MIN_M, STATION_ALT_MAX_M = -450.0, 9000.0   # ים המלח .. מעבר לכל תורן סביר
STATION_NAME_MAX = 40


def _sanitize_station(d):
    """(station, error). lat/lon/alt_m חובה — אין ברירת מחדל (ר' DEFAULT_STATE)."""
    if not isinstance(d, dict):
        return None, "גוף לא תקין"
    try:
        lat, lon, alt = float(d.get("lat")), float(d.get("lon")), float(d.get("alt_m"))
    except (TypeError, ValueError):
        return None, "נדרשים lat, lon ו-alt_m מספריים"
    if not all(math.isfinite(v) for v in (lat, lon, alt)):
        return None, "ערך לא סופי"
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return None, "קואורדינטות מחוץ לתחום"
    if not STATION_ALT_MIN_M <= alt <= STATION_ALT_MAX_M:
        return None, f"גובה האנטנה חייב להיות בין {STATION_ALT_MIN_M:g} ל-{STATION_ALT_MAX_M:g} מ'"
    name = str(d.get("name") or "").strip()[:STATION_NAME_MAX] or None
    source = d.get("source") if d.get("source") in ("manual", "device") else "manual"
    return {"lat": round(lat, 6), "lon": round(lon, 6), "alt_m": round(alt, 1),
            "name": name, "source": source}, None


def _xc_buffers(src):
    """(lock, deque, jsonl) של מקור — נקרא בזמן ריצה (הבדיקות מחליפות נתיבים)."""
    return {"acars": (_acars_lock, _acars_msgs, ACARS_LOG_PATH),
            "vdl2": (_vdl2_lock, _vdl2_msgs, VDL2_LOG_PATH),
            "satcom": (_satcom_lock, _satcom_msgs, SATCOM_LOG_PATH)}.get(src)


def _xc_has_identity(rec):
    return bool((rec.get("tail") and not rec.get("tail_is_station")) or rec.get("flight")
                or rec.get("icao") or rec.get("aes"))


def _xc_event_id(src, rec):
    ident = rec.get("tail") or rec.get("icao") or rec.get("aes") or rec.get("flight") or ""
    key = f"{src}|{float(rec.get('t') or 0):.3f}|{ident}|{rec.get('label') or ''}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def _xc_direction(src, rec):
    """(direction, basis). VDL2/SATCOM — הכיוון המבני שנשמר (AVLC/ISU). ACARS —
    מחושב *מחדש*: block ID שנשמר (מ-v2.31.0), אחרת MSN (acarsdec מוסיף msgno *רק*
    לבלוק downlink — ר' acars_parse._acars_block_dir), ורק אז הכללים לפי label/header. הודעה
    שנשמרה לפני תיקון (B6 שסווג uplink עד v2.26.0; ‏C1/B3/B4 עד v2.30.0) מקבלת את
    הכיוון הנכון, והשינוי מתועד ב-basis. ⚠ קריטי להצלבה: uplink שסווג downlink
    (‏loadsheet של C1) היה נבחן כשידור *מהמטוס* מול אופק הרדיו."""
    if src == "vdl2" and rec.get("icao") and rec.get("dir"):
        return rec["dir"], "מבני — כתובת AVLC"
    if src == "satcom" and rec.get("aes") and rec.get("dir"):
        return rec["dir"], "מבני — שכבת ISU"
    label, text = rec.get("label"), rec.get("text")
    d = acars_parse._acars_block_dir(rec.get("block_id")) if src == "acars" else None
    if d:
        basis = "מבני — block ID של המסגרת"
    elif src == "acars" and rec.get("msgno"):
        d, basis = "downlink", "מבני — יש מספר הודעה (MSN), שנשלח רק מהמטוס"
    elif label in acars_parse._ACARS_DIR_BY_LABEL:
        d, basis = acars_parse._ACARS_DIR_BY_LABEL[label], f"לפי label {label}"
    else:
        d = acars_parse._acars_direction(label, text)
        basis = "לפי כותרת ניתוב (heuristic)" if d else None
    if d and rec.get("dir") and rec["dir"] != d:
        basis += f"; נשמר בעבר כ-{rec['dir']} — חושב מחדש"
    return d, basis


def _xc_adsc(rec, direction):
    """פרטי דוח ה-ADS-C (ר' acars_parse._adsc_details) + זמן מוחלט של הדוח. הודעה שנשמרה לפני
    שהשדה נוסף — re-decode מקומי ב-decode_acars_apps (כלי libacars הרשמי, מקור-האמת
    לתוכן; אותו מסלול כמו acars_parse._decode_libacars_app) בכיוון שחושב. אין => None."""
    d, how = rec.get("adsc"), "message"
    if (not d and rec.get("pos_src") == "adsc" and direction in ("uplink", "downlink")
            and rec.get("label") and isinstance(rec.get("text"), str)):
        out = acars_parse._run_libacars("d" if direction == "downlink" else "u", rec["label"], rec["text"],
                            as_json=True)
        body = acars_parse._strip_echoed_msg(out, rec["text"]) if out else ""
        i = body.find("{")
        if i >= 0:
            try:
                d, how = acars_parse._adsc_details(json.loads(body[i:])), "redecode"
            except ValueError:
                d = None
    if not d:
        return None
    return {**d, "t": xcheck.adsc_report_time(d.get("ts_sec"), rec.get("t")), "source": how}


def _xc_point(rec, provider, origin, t_fallback):
    """נקודת ADS-B מרשומת snapshot/lookup. רשומה משובשת (מיקום מדוכא) נשארת
    כנקודה בלי מיקום — כדי ש-position_at ידווח "מזויף" ולא "אין נתונים"."""
    t = rec.get("t_pos") if rec.get("t_pos") is not None else t_fallback
    return adsb.make_point(t, rec.get("lat"), rec.get("lon"), alt_baro=rec.get("alt_baro"),
                           alt_geom=rec.get("alt_geom"), gs=rec.get("gs"), track=rec.get("track"),
                           src_type=rec.get("src_type"), nic=rec.get("nic"), nac_p=rec.get("nac_p"),
                           provider=provider, origin=origin)


_XC_BY_RANK = {"hex": 3, "reg": 2, "flight": 1, None: 0}


def _xc_collect(rec, ident, *, network, full, prior=None, deadline=None, imported=None):
    """אוסף נקודות ADS-B סביב זמן הקליטה: snapshot חי → buffer מקומי → lookup
    ממוקד → (full בלבד) trace היסטורי → OpenSky. כל מקור מתועד ב-sources —
    כולל מה *לא* נמצא ולמה ("לא ניסינו" ≠ "ניסינו ולא נמצא", §12)."""
    t = float(rec.get("t") or time.time())
    now = time.time()
    pts, sources = list(prior or []), []
    best = {"by": None, "conflict": None, "meta": None, "conflict_src": None}

    def budget_ok():
        return deadline is None or time.monotonic() < deadline

    def consider(name, cand, weak=False):
        """מתאים רשומת ADS-B לזהות. מחזיר by או None; מעדכן את ההתאמה הטובה ביותר.
        ⚠ סתירה נרשמת רק כשהמועמד נמצא לפי מזהה *חזק* (weak=False): מטוס אחר עם
        אותו מספר טיסה הוא "לא זה", לא "סתירת זהות"."""
        by, conflict = xcheck.match_record(ident, cand)
        if weak and conflict:
            return None
        if by and _XC_BY_RANK[by] > _XC_BY_RANK[best["by"]]:
            best.update(by=by, meta={k: cand.get(k) for k in ("hex", "reg", "flight", "type")
                                     if cand.get(k)})
        if conflict in ("hex_mismatch", "reg_mismatch") and not best["conflict"]:
            best.update(conflict=conflict, conflict_src=name)
        if by and not ident["hex"] and adsb.norm_hex(cand.get("hex")):
            ident.update(hex=adsb.norm_hex(cand["hex"]),
                         hex_src="adsb_db" if by in ("hex", "reg") else "flight_lookup")
        return by

    recent = abs(now - t) <= XC_LIVE_MAX_AGE_SEC
    if recent:
        cand, weak = adsb.local_aircraft(ident["hex"], ident["reg"]), False
        if cand is None and ident["flight_key"]:
            cand, weak = next((a for a in adsb.all_aircraft()
                               if xcheck.flight_key(a.get("flight")) == ident["flight_key"]), None), True
        by = consider("snapshot", cand, weak) if cand else None
        snapshot_matched = bool(by)
        if by:
            pts.append(_xc_point(cand, "poll", "live", now - (cand.get("age") or 0)))
        sources.append({"name": "snapshot", "status": "ok" if by else "miss"})
    else:
        snapshot_matched = False
        sources.append({"name": "snapshot", "status": "skipped", "detail": "ההודעה ישנה מ-2 דק'"})

    if not full:
        # ⚠ ה-worker לא קורא את track.jsonl (עד ~5MB ב-300 מטוסים × 90 דק') על כל
        # הודעה — זה CPU (ו-GIL ליד ה-listeners) בלי צורך: נקודות ה-snapshot/lookup
        # של שלב 1 עוברות לשלב 2 (prior) ונותנות את שני צידי הקליטה.
        sources.append({"name": "buffer", "status": "skipped",
                        "detail": "מצב חי — נקודות snapshot/lookup משני שלבי הבדיקה"})
    elif now - t <= adsb.TRACK_BUFFER_MIN * 60.0 + XC_TRACK_WINDOW_SEC:
        bp, gaps = adsb.local_track_points(t - XC_TRACK_WINDOW_SEC, t + XC_TRACK_WINDOW_SEC,
                                           reg=ident["reg"], hex_=ident["hex"])
        near_gaps = [g for g in gaps if isinstance(g.get("t"), (int, float)) and abs(g["t"] - t) <= 120]
        if bp:
            pts += bp
            by = "reg" if ident["reg"] else "hex"
            if _XC_BY_RANK[by] > _XC_BY_RANK[best["by"]]:
                best.update(by=by, meta=best["meta"] or {"reg": ident["reg"]})
        sources.append({"name": "buffer", "status": "ok" if bp else "miss", "points": len(bp),
                        "detail": f"{len(near_gaps)} פערי קליטת ADS-B סביב זמן הקליטה" if near_gaps else None})
    else:
        sources.append({"name": "buffer", "status": "skipped",
                        "detail": f"מחוץ לחלון ה-buffer ({adsb.TRACK_BUFFER_MIN:g} דק')"})

    if not ident["hex"] and ident["reg"]:
        hx = adsb.reg_to_hex(ident["reg"])
        if hx:
            ident.update(hex=hx, hex_src="adsb_db")

    # lookup ממוקד רק כשצריך: הודעה חיה שהמטוס שלה לא ב-snapshot (מחוץ לרדיוס
    # ה-poll), או "אמת" מלא בלי hex (לזיהוי ה-hex עבור ה-trace). בשלב 2 של ה-worker
    # זה lookup *נוסף* — נקודה אחרי הקליטה (ה-cache של שלב 1 כבר פג, LOOKUP_TTL_HIT).
    want_lookup = network and ((recent and not snapshot_matched) or (full and not ident["hex"]))
    if want_lookup and budget_ok():
        if ident["hex"]:
            kind, val = "hex", ident["hex"]
        elif ident["reg"]:
            kind, val = "reg", rec.get("tail")
        else:
            kind, val = "callsign", ident["flight_key"]
        if val:
            res = adsb.lookup(kind, val)
            got = 0
            for a in res.get("aircraft") or []:
                if consider("lookup", a, weak=kind == "callsign") and recent:
                    pts.append(_xc_point(a, a.get("provider"), "lookup", a.get("fetched_at") or now))
                    got += 1
            sources.append({"name": "lookup", "status": res.get("status"), "kind": kind,
                            "provider": res.get("provider"), "points": got,
                            "detail": "; ".join(res.get("errors") or []) or None})

    if imported is not None:
        # trace שהמשתמש העלה (adsb.import_trace). ⚠ הקובץ חייב לזהות את *המטוס הזה*
        # לפי ה-icao/r שבו — קובץ של מטוס אחר שנבחר בטעות לא הופך לראיה. hex שנלמד
        # מכאן מסומן "import": זיהוי לפי רישום תואם, לא hex מבני מההודעה.
        meta = imported.get("meta") or {}
        f_hex, f_reg = adsb.norm_hex(meta.get("icao")), adsb.norm_reg(meta.get("r"))
        status, by = imported.get("status"), None
        if status == "ok":
            if ident["hex"] and f_hex and f_hex != ident["hex"]:
                status = "conflict"
                if not best["conflict"] and ident.get("hex_src") in ("message", "user"):
                    best.update(conflict="hex_mismatch", conflict_src="import")
            elif ident["reg"] and f_reg and f_reg != ident["reg"]:
                status = "conflict"
                if not best["conflict"]:
                    best.update(conflict="reg_mismatch", conflict_src="import")
            elif ident["hex"] and f_hex == ident["hex"]:
                by = "hex" if ident.get("hex_src") in ("message", "user") else "reg"
            elif ident["reg"] and f_reg == ident["reg"]:
                by = "reg"
            else:
                status = "unmatched"            # אין בקובץ מזהה שאפשר להשוות להודעה
        if by:
            pts += imported["points"]
            if not ident["hex"] and f_hex:
                ident.update(hex=f_hex, hex_src="import")
            if _XC_BY_RANK[by] > _XC_BY_RANK[best["by"]]:
                best.update(by=by, meta={"hex": f_hex, "reg": meta.get("r"), "type": meta.get("t")})
        covers = (imported.get("t_first") is not None
                  and imported["t_first"] <= t <= imported["t_last"])
        sources.append({"name": "import", "status": "ok" if by else status,
                        "file": imported.get("name"), "points": len(imported["points"]) if by else 0,
                        "covers": covers if by else None,
                        "detail": imported.get("error") or (
                            "הקובץ לא מזהה את המטוס שבהודעה" if status == "unmatched" else None)})
    if full and network and imported is None:
        if ident["hex"] and budget_ok():
            tr = adsb.fetch_trace(ident["hex"], t)
            use = tr.get("status") == "ok"
            meta_reg = adsb.norm_reg((tr.get("meta") or {}).get("r"))
            if use and ident["reg"] and meta_reg and meta_reg != ident["reg"]:
                use = False                     # ה-trace של hex אחר מהרישום שבהודעה
                if not best["conflict"] and ident.get("hex_src") in ("message", "user"):
                    best.update(conflict="reg_mismatch", conflict_src="trace")
            if use:
                pts += tr["points"]
                by = "hex" if ident.get("hex_src") in ("message", "user") else (
                    "reg" if ident.get("hex_src") == "adsb_db" else "flight")
                if _XC_BY_RANK[by] > _XC_BY_RANK[best["by"]]:
                    best.update(by=by, meta={"hex": ident["hex"], "reg": (tr.get("meta") or {}).get("r"),
                                             "type": (tr.get("meta") or {}).get("t")})
            sources.append({"name": "trace", "status": tr.get("status") if use or tr.get("status") != "ok"
                            else "conflict", "provider": tr.get("provider"), "url": tr.get("url"),
                            "points": len(tr.get("points") or []) if use else 0,
                            "covers": tr.get("covers"),
                            "detail": "; ".join(tr.get("errors") or []) or None})
            if not (use and tr.get("covers")) and budget_ok():
                os_ = adsb.fetch_opensky_track(ident["hex"], t)
                if os_.get("status") == "ok":
                    pts += os_["points"]
                sources.append({"name": "opensky", "status": os_.get("status"),
                                "points": len(os_.get("points") or []), "covers": os_.get("covers"),
                                "detail": "; ".join(os_.get("errors") or []) or None})
        elif not ident["hex"]:
            sources.append({"name": "trace", "status": "no_hex",
                            "detail": "כתובת ICAO לא ידועה (אפשר להזין ידנית)"})
        else:
            sources.append({"name": "trace", "status": "skipped", "detail": "חרגנו מתקציב הזמן"})

    # dedup: אותה נקודה משני מקורות (buffer+snapshot) לא נספרת פעמיים
    seen, uniq = set(), []
    for p in sorted((p for p in pts if p.get("t") is not None), key=lambda p: p["t"]):
        k = (round(p["t"], 1), p.get("lat"), p.get("lon"))
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    by = best["by"]
    if by == "hex" and ident.get("hex_src") == "adsb_db":
        by = "reg"                               # ה-hex נגזר מהרישום — ההתאמה היא לפי רישום
    return {"points": uniq, "sources": sources,
            "match": {"by": by, "conflict": best["conflict"], "conflict_src": best["conflict_src"],
                      "hex": ident["hex"], "hex_src": ident.get("hex_src"), "adsb": best["meta"]}}


def _xc_track(points, t):
    """מסלול למפה: נקודות עם מיקום בטווח ±XC_TRACK_WINDOW_SEC, מדולל ל-XC_TRACK_MAX_POINTS."""
    pts = [p for p in points if p.get("lat") is not None and p.get("lon") is not None
           and abs(p["t"] - t) <= XC_TRACK_WINDOW_SEC]
    step = max(1, math.ceil(len(pts) / XC_TRACK_MAX_POINTS))
    return [{"t": p["t"], "lat": p["lat"], "lon": p["lon"],
             "alt": p.get("alt_geom") if p.get("alt_geom") is not None else p.get("alt_baro"),
             "src_type": p.get("src_type"), "origin": p.get("origin")} for p in pts[::step]]


def _xc_evidence_points(points, t):
    near = [p for p in points if abs(p["t"] - t) <= XC_EVIDENCE_WINDOW_SEC]
    near.sort(key=lambda p: abs(p["t"] - t))
    return sorted(near[:XC_EVIDENCE_MAX_POINTS], key=lambda p: p["t"])


def _xc_evaluate(src, rec, *, network, full, hex_override=None, station=None, prior=None,
                 deadline=None, imported=None):
    """הצלבה מלאה להודעה אחת => (תוצאה, נקודות). התוצאה = xcheck.assess + כל מה
    שנדרש כדי לחקור אותה אחר כך (ההודעה המקורית, הזהות, מקורות שנוסו, נקודות
    ADS-B מקוריות, מסלול, התחנה שבשימוש, גרסת התוכנה)."""
    t = float(rec.get("t") or time.time())
    ident = xcheck.message_identity(rec, hex_override)
    direction, basis = _xc_direction(src, rec)
    adsc = _xc_adsc(rec, direction)
    col = _xc_collect(rec, ident, network=network, full=full, prior=prior, deadline=deadline,
                      imported=imported)
    pts = col["points"]
    rx_at = xcheck.position_at(pts, t)
    pos_at = (xcheck.position_at(pts, adsc["t"])
              if adsc and adsc.get("t") is not None and xcheck.valid_latlon(rec.get("lat"), rec.get("lon"))
              else None)
    st = station if station is not None else appstate.load_state().get("station")
    res = xcheck.assess(src, rec, direction, st, col["match"], rx_at, pos_at, adsc,
                        direction_basis=basis)
    res.update({
        "event_id": _xc_event_id(src, rec), "src": src, "t_rx": t,
        "msg": {k: v for k, v in rec.items() if k != "id"},
        "identity": ident, "match": col["match"], "sources": col["sources"],
        "rx_at": rx_at, "pos_at": pos_at,
        "adsb_points": _xc_evidence_points(pts, t), "track": _xc_track(pts, t),
        "mode": "full" if full else "live", "computed_at": round(time.time(), 1),
        "version": VERSION,
    })
    return res, pts


# --- worker: תור עם השהיה (שלב 1 מיד, שלב 2 אחרי XC_RECHECK_SEC) -----------------
_xc_cv = threading.Condition()
_xc_heap = []                          # (due_mono, tie, job)
_xc_tie = itertools.count()
_xc_stats = {"submitted": 0, "dropped": 0, "done": 0, "errors": 0, "events_saved": 0}
_xc_res_lock = threading.Lock()
_xc_results = collections.OrderedDict()    # (src, id) -> (seq, summary)
_xc_seq = 0
_XC_EVENTS_LOCK = threading.Lock()


def _xc_push(due, job):
    with _xc_cv:
        if len(_xc_heap) >= XC_QUEUE_MAX:
            _xc_stats["dropped"] += 1
            return False
        heapq.heappush(_xc_heap, (due, next(_xc_tie), job))
        _xc_cv.notify()
        return True


def _xc_submit(src, rec):
    """נקרא מ-thread ה-listener אחרי ההתמדה: הכנסה לתור בלבד — בלי רשת, בלי דיסק,
    נעילה לרגע. ⚠ לעולם לא זורק: שום דבר בהצלבה לא רשאי לעצור את קליטת הרדיו."""
    try:
        if not _xc_has_identity(rec):
            return
        if _xc_push(time.monotonic(), {"src": src, "rec": dict(rec), "stage": 1, "prior": []}):
            with _xc_cv:
                _xc_stats["submitted"] += 1
    except Exception:
        log.exception("xcheck: הכנסה לתור נכשלה — ההודעה לא תוצלב")


def _xc_store(src, msg_id, res):
    global _xc_seq
    if msg_id is None:
        return
    summ = xcheck.summary(res)
    with _xc_res_lock:
        _xc_seq += 1
        key = (src, msg_id)
        _xc_results[key] = (_xc_seq, summ)
        _xc_results.move_to_end(key)
        while len(_xc_results) > XC_RESULTS_MAX:
            _xc_results.popitem(last=False)


def _xc_feed_payload(src):
    """תקצירי הצלבה חדשים (מאז ?xseq=) לתצוגת המקור — נוסף לתשובת /api/<src>
    הקיימת, בלי בקשה נוספת מהדפדפן."""
    try:
        since = int(request.args.get("xseq", 0))
    except (TypeError, ValueError):
        since = 0
    with _xc_res_lock:
        items = {str(mid): summ for (s, mid), (seq, summ) in _xc_results.items()
                 if s == src and seq > since}
        cursor = _xc_seq
    return {"xcheck": items, "xcheck_cursor": cursor}


def _xc_run_job(job):
    src, rec = job["src"], job["rec"]
    res, pts = _xc_evaluate(src, rec, network=XC_AUTO_LOOKUP, full=False, prior=job["prior"])
    _xc_store(src, rec.get("id"), res)
    rx = res.get("rx_at") or {}
    improvable = (rx.get("status") not in ("measured", "interpolated")
                  or rx.get("status") == "measured" and abs(rx.get("dt_s") or 0) > 5)
    if job["stage"] == 1 and res["match"]["by"] and improvable:
        # שלב 2: נקודה *אחרי* הקליטה תאפשר אינטרפולציה. נקודות snapshot/lookup של
        # שלב 1 עוברות הלאה — הן הצד ה"לפני" (ה-snapshot מתחלף ב-poll הבא).
        delay = max(0.0, float(rec.get("t") or time.time()) + XC_RECHECK_SEC - time.time())
        if delay > 0 and _xc_push(time.monotonic() + delay,
                                  {"src": src, "rec": rec, "stage": 2,
                                   "prior": [p for p in pts if p.get("origin") in ("live", "lookup")]}):
            return
    if xcheck.is_anomalous(res):
        _xc_save_event(res, "auto")


def _xc_worker():
    """thread יחיד: מוציא משימות שזמנן הגיע. חריגה במשימה לא מפילה את ה-thread."""
    while True:
        with _xc_cv:
            while True:
                now = time.monotonic()
                if _xc_heap and _xc_heap[0][0] <= now:
                    _due, _tie, job = heapq.heappop(_xc_heap)
                    break
                _xc_cv.wait(None if not _xc_heap else max(0.05, _xc_heap[0][0] - now))
        try:
            _xc_run_job(job)
            with _xc_cv:
                _xc_stats["done"] += 1
        except Exception:
            with _xc_cv:
                _xc_stats["errors"] += 1
            log.exception("xcheck: הצלבה נכשלה (%s)", job.get("src"))


# --- ראיות: אירועים חריגים (xcheck_events.jsonl) --------------------------------
def _xc_save_event(res, reason):
    """שורה לאירוע (append + fsync — ראיה מניסוי שטח, כמו rf_log). אותו אירוע
    שנשמר שוב (אימות חוזר) — "האחרון מנצח" בקריאה. מחזיר את ה-id או None."""
    rec = {**res, "id": res["event_id"], "saved_at": round(time.time(), 1), "reason": reason}
    data = (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8")
    with _XC_EVENTS_LOCK:
        try:
            XC_EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
            with XC_EVENTS_PATH.open("a+b") as f:
                storage.append_line_on_boundary(f, data)
                f.flush()
                os.fsync(f.fileno())
        except OSError:
            log.exception("xcheck: שמירת ראיה נכשלה")
            return None
        with _xc_cv:
            _xc_stats["events_saved"] += 1
            n = _xc_stats["events_saved"]
        if n % 50 == 0:
            storage.trim_jsonl_log(XC_EVENTS_PATH, XC_EVENTS_KEEP)
    return rec["id"]


def _xc_read_events():
    """כל האירועים, הגרסה האחרונה לכל id, מהחדש לישן (לפי זמן הקליטה)."""
    try:
        lines = storage.read_log_lines(XC_EVENTS_PATH)
    except OSError:
        return []
    latest = {}
    for r in storage.jsonl_records(lines):
        if isinstance(r.get("id"), str):
            latest[r["id"]] = r
    return sorted(latest.values(), key=lambda r: r.get("t_rx") or 0, reverse=True)


def _xc_event_row(e):
    """שורת סיכום שטוחה לאירוע — לרשימה ב-UI ולייצוא CSV."""
    m, hz = e.get("msg") or {}, e.get("horizon") or {}
    rx, pc = e.get("rx_at") or {}, e.get("position_check") or {}
    st, mt = e.get("station") or {}, e.get("match") or {}
    return {
        "id": e.get("id"), "t": e.get("t_rx"), "src": e.get("src"), "tail": m.get("tail"),
        "flight": m.get("flight"), "hex": mt.get("hex"), "match_by": mt.get("by"),
        "freq": m.get("freq"), "signal_dbfs": m.get("level"), "error": m.get("error"),
        "label": m.get("label"), "dir": e.get("direction"),
        "msg_lat": m.get("lat"), "msg_lon": m.get("lon"), "pos_src": m.get("pos_src"),
        "adsb_status": rx.get("status"), "adsb_lat": rx.get("lat"), "adsb_lon": rx.get("lon"),
        "adsb_dt_s": json.dumps(rx.get("dt_s")) if isinstance(rx.get("dt_s"), list) else rx.get("dt_s"),
        "adsb_provider": rx.get("provider"), "adsb_src_type": rx.get("src_type"),
        "adsb_uncert_km": rx.get("uncert_km"),
        "pos_diff_km": pc.get("distance_km"), "pos_agree": pc.get("agree"),
        "station_lat": st.get("lat"), "station_lon": st.get("lon"), "station_alt_m": st.get("alt_m"),
        "distance_km": hz.get("distance_km"), "radio_horizon_km": hz.get("radio_km"),
        "geometric_horizon_km": hz.get("geometric_km"), "excess_km": hz.get("excess_km"),
        "aircraft_alt_ft": hz.get("aircraft_alt_ft"),
        "verdict": e.get("verdict"), "verdict_text": e.get("verdict_text"),
        "evidence_level": e.get("level"), "reason": e.get("reason"), "saved_at": e.get("saved_at"),
        "text": m.get("text"),
    }


XC_EXPORT_COLS = ["time_iso", "timestamp", "id", "src", "tail", "flight", "hex", "match_by",
                  "freq", "signal_dbfs", "error", "label", "dir", "msg_lat", "msg_lon", "pos_src",
                  "adsb_status", "adsb_lat", "adsb_lon", "adsb_dt_s", "adsb_provider",
                  "adsb_src_type", "adsb_uncert_km", "pos_diff_km", "pos_agree", "station_lat",
                  "station_lon", "station_alt_m", "distance_km", "radio_horizon_km",
                  "geometric_horizon_km", "excess_km", "aircraft_alt_ft", "verdict",
                  "verdict_text", "evidence_level", "reason", "saved_at", "text"]


def _xc_filter(events, args):
    """סינון לפי ?min_km=&max_km=&min_dbfs=&max_dbfs=&day=YYYY-MM-DD&evidence=&verdict=."""
    def fnum(k):
        try:
            return float(args.get(k)) if args.get(k) not in (None, "") else None
        except (TypeError, ValueError):
            return None
    min_km, max_km, min_db, max_db = fnum("min_km"), fnum("max_km"), fnum("min_dbfs"), fnum("max_dbfs")
    bounds = _day_bounds(args["day"]) if args.get("day") else None
    levels = set(filter(None, (args.get("evidence") or "").split(",")))
    verdicts = set(filter(None, (args.get("verdict") or "").split(",")))
    out = []
    for e in events:
        d = (e.get("horizon") or {}).get("distance_km")
        lv = (e.get("msg") or {}).get("level")
        if min_km is not None and (d is None or d < min_km):
            continue
        if max_km is not None and (d is None or d > max_km):
            continue
        if min_db is not None and (lv is None or lv < min_db):
            continue
        if max_db is not None and (lv is None or lv > max_db):
            continue
        if bounds and not bounds[0] <= (e.get("t_rx") or 0) < bounds[1]:
            continue
        if levels and e.get("level") not in levels:
            continue
        if verdicts and e.get("verdict") not in verdicts:
            continue
        out.append(e)
    return out


def _xc_export(events, basename):
    """JSON = הראיות המלאות (כל השדות); CSV = שורת סיכום לכל אירוע (עם BOM)."""
    if (request.args.get("format") or "json").lower() == "csv":
        return _export_response([_xc_event_row(e) for e in events], XC_EXPORT_COLS, basename)
    resp = app.response_class(json.dumps(events, ensure_ascii=False, indent=1),
                              mimetype="application/json")
    resp.headers["Content-Disposition"] = (
        f'attachment; filename="{basename}-{time.strftime("%Y%m%d-%H%M%S")}.json"')
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _xc_find_message(src, t, tail=None, label=None):
    """ההודעה המקורית — מהזיכרון, ואם אינה שם (ארכיון/אחרי restart) — מה-jsonl.
    ⚠ הראיה נבנית תמיד מהעותק *שבשרת*, לא ממה שהדפדפן שלח."""
    lock, msgs, path = _xc_buffers(src)

    def ok(r):
        return (abs(float(r.get("t") or 0) - t) < 1e-3
                and (tail is None or r.get("tail") == tail)
                and (label is None or r.get("label") == label))
    with lock:
        for m in reversed(msgs):
            if ok(m):
                return dict(m)
    for r in reversed(storage.read_jsonl_log(path)):
        if ok(r):
            return r
    return None


@app.route("/api/station", methods=["GET", "POST"])
def api_station():
    """תחנת הקליטה (מיקום + גובה אנטנה מעל פני הים) להצלבת ADS-B/אופק. POST
    ‏{lat, lon, alt_m, name?, source?} או {clear:true}. דרך _guard (POST)."""
    if request.method == "GET":
        return jsonify(ok=True, station=appstate.load_state().get("station"))
    data = request.get_json(silent=True) or {}
    if data.get("clear"):
        appstate._merge_state({"station": None})
        return jsonify(ok=True, station=None)
    st, err = _sanitize_station(data)
    if err:
        return jsonify(ok=False, error=err), 400
    st["set_at"] = round(time.time(), 1)
    appstate._merge_state({"station": st})
    return jsonify(ok=True, station=st)


@app.route("/api/xcheck/status")
def api_xcheck_status():
    with _xc_cv:
        queued, stats = len(_xc_heap), dict(_xc_stats)
    return jsonify(ok=True, station=appstate.load_state().get("station"), auto_lookup=XC_AUTO_LOOKUP,
                   queue=queued, stats=stats, net=adsb.net_status())


@app.route("/api/xcheck/verify", methods=["POST"])
def api_xcheck_verify():
    """"אמת מול ADS-B" להודעה אחת (חיה או ישנה): ‏{src, t, tail?, label?, hex?,
    station?, save?, trace_b64?, trace_name?}. מושך גם trace היסטורי — או, כשהמשתמש
    העלה קובץ trace (trace_b64), משתמש בו במקום ה-trace/OpenSky מהרשת. אירוע חריג נשמר אוטומטית; ‏save:true
    שומר כל תוצאה. בקשה חוסמת (רשת, ≤~XC_VERIFY_BUDGET_SEC) — בבקשת המשתמש בלבד,
    מחוץ לנתיב הרדיו. דרך _guard."""
    data = request.get_json(silent=True) or {}
    src = data.get("src")
    if src not in XC_SOURCES:
        return jsonify(ok=False, error="src לא תקין"), 400
    try:
        t = float(data.get("t"))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="נדרש t (זמן ההודעה)"), 400
    hex_override = data.get("hex") or None
    if hex_override and not adsb.norm_hex(hex_override):
        return jsonify(ok=False, error="כתובת ICAO חייבת להיות 6 ספרות hex"), 400
    station = None
    if data.get("station"):
        station, err = _sanitize_station(data["station"])
        if err:
            return jsonify(ok=False, error="תחנה: " + err), 400
    imported = None
    if data.get("trace_b64"):
        # ‏trace ידני (ר' adsb.import_trace): base64 בגוף ה-JSON — פשוט מ-multipart, וה-UI
        # ממילא שולח JSON. החסם נבדק *לפני* הפענוח, לא אחריו.
        b64 = data["trace_b64"]
        if not isinstance(b64, str) or len(b64) > adsb.TRACE_MAX_BYTES * 4 // 3 + 8:
            return jsonify(ok=False, error="קובץ ה-trace גדול מדי"), 413
        try:
            raw = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            return jsonify(ok=False, error="קובץ ה-trace לא קודד כראוי"), 400
        imported = adsb.import_trace(raw, data.get("trace_name"))
        if imported["status"] != "ok":
            return jsonify(ok=False, error="קובץ ה-trace לא תקין: " + (imported.get("error") or "")), 400
    rec = _xc_find_message(src, t, data.get("tail"), data.get("label"))
    if rec is None:
        return jsonify(ok=False, error="ההודעה לא נמצאה (לא בזיכרון ולא ביומן)"), 404
    res, _pts = _xc_evaluate(src, rec, network=True, full=True, hex_override=hex_override,
                             station=station, deadline=time.monotonic() + XC_VERIFY_BUDGET_SEC,
                             imported=imported)
    _xc_store(src, rec.get("id"), res)
    saved = None
    if data.get("save") or xcheck.is_anomalous(res):
        saved = _xc_save_event(res, "user" if data.get("save") else "auto")
    return jsonify(ok=True, result=res, saved=saved)


@app.route("/api/xcheck/events")
def api_xcheck_events():
    """אירועים חריגים שמורים (תקציר), מסוננים — ר' _xc_filter."""
    events = _xc_filter(_xc_read_events(), request.args)
    return jsonify(ok=True, events=[_xc_event_row(e) for e in events], total=len(events))


@app.route("/api/xcheck/events/export")
def api_xcheck_events_export():
    return _xc_export(_xc_filter(_xc_read_events(), request.args), "airam-xcheck")


@app.route("/api/xcheck/events/<event_id>", methods=["GET", "DELETE"])
def api_xcheck_event(event_id):
    if not XC_EVENT_ID_RE.match(event_id or ""):
        abort(404)
    events = _xc_read_events()
    ev = next((e for e in events if e.get("id") == event_id), None)
    if ev is None:
        return jsonify(ok=False, error="אירוע לא נמצא"), 404
    if request.method == "GET":
        return jsonify(ok=True, event=ev)
    with _XC_EVENTS_LOCK:                        # DELETE (דרך _guard): כתיבה מחדש אטומית
        try:
            lines = storage.read_log_lines(XC_EVENTS_PATH)
        except OSError:
            lines = []
        keep = [ln for ln, r in ((ln, storage.jsonl_records([ln])) for ln in lines)
                if not (r and r[0].get("id") == event_id)]
        try:
            storage.atomic_write(XC_EVENTS_PATH, "\n".join(keep) + ("\n" if keep else ""))
        except OSError:
            return jsonify(ok=False, error="המחיקה נכשלה (דיסק?)"), 500
    return jsonify(ok=True)


@app.route("/api/xcheck/events/<event_id>/export")
def api_xcheck_event_export(event_id):
    if not XC_EVENT_ID_RE.match(event_id or ""):
        abort(404)
    ev = next((e for e in _xc_read_events() if e.get("id") == event_id), None)
    if ev is None:
        abort(404)
    return _xc_export([ev], f"airam-xcheck-{event_id}")


@app.route("/api/mode", methods=["POST"])
def api_mode():
    """מעבר בין המצבים: קול (rtl_airband) / ACARS (acarsdec) / VDL2 (dumpvdl2) /
    SATCOM (inmarsat-sniffer) / off (standby) / scan (סבב אוטומטי בין המצבים).
    SDR אחד בהחלפה — צרכן אחד בכל רגע. המצבים שווי-מעמד: כישלון כניסה לכל אחד
    מהם נופל ל-off (בלי fallback לקול). POST => עובר דרך _guard (Origin + PIN
    אופציונלי), כמו /api/tune."""
    data = request.get_json(silent=True) or {}
    mode = str(data.get("mode", "")).lower()
    if mode not in ("voice", "acars", "vdl2", "satcom", "off", "scan", *COMPOSITE_SECONDARY):
        return jsonify(ok=False, error="mode לא תקין (voice/acars/vdl2/satcom/off/scan/"
                       + "/".join(COMPOSITE_SECONDARY) + ")"), 400

    # קודם ולידציה סטטית (לא תלוית-נעילה: פענוח פרמטרים/לוח/תדרים) — בקשה עם
    # פרמטרים לא-תקינים (400) לא נוגעת בסבב סריקה פעיל בכלל (אחרת סבב תקין
    # נעצר "בחינם" והמצב נשאר תקוע על הרגל האחרונה בלי סבב שממשיך אותו — "scan
    # זומבי"). _scan_stop_thread() עצמו נקרא רק *אחרי* שתפסנו את TUNE_LOCK
    # (בתוך ה-try למטה) — כך שאם יש סבב סריקה פעיל שמחזיק את הנעילה לרגע קצר
    # (מעבר רגל), אנחנו כבר בטוחים שנחזיק אותה בעצמנו לפני שננסה לעצור אותו,
    # ולא ניתקל ב-409 שקרי בגלל תחרות-עצמית עם הסבב. timeout קטן (לא 0) סופג
    # בדיוק את החלון הקצר הזה; רק חסימה ממושכת אמיתית (פעולה אחרת) עדיין
    # מחזירה 409 — ובלי לגעת בסבב כלל.
    st = appstate.load_state()

    if mode == "voice":
        # קול = כיוונון להגדרות השמורות האחרונות (או מפורשות). _voice_tune מחזיק
        # את ה-TUNE_LOCK בעצמו => לא לוקחים אותו כאן (deadlock).
        params, perr = _parse_tune(data if "freq" in data else st)
        if perr:   # state פגום => נופלים לברירת מחדל
            params, _ = _parse_tune(appstate.DEFAULT_STATE)
        # _scan_stop_thread נקרא בתוך _voice_tune עצמו, אחרי שהוא תופס את
        # TUNE_LOCK — לא כאן (ר' ההערה בתוך _voice_tune; אותה סיבה בדיוק
        # שהמצבים האחרים למטה קוראים לו רק אחרי הנעילה).
        payload, status = _voice_tune(params, keep_secondary=False)
        return jsonify(payload), status

    if mode in COMPOSITE_DATA:
        # שני מקלטים: קול (ההגדרות השמורות) על הראשי + מפענח דאטה (ACARS/VDL2 — הבנק השמור או
        # freqs) על המשני
        key = COMPOSITE_DATA[mode][0]
        is_vdl2 = mode == "voice_vdl2"
        label = "VDL2" if is_vdl2 else "ACARS"
        rerr = _roles_error(mode)
        if rerr:
            return jsonify(ok=False, error=rerr, state=st), 400
        freqs = _sanitize_freqs(data.get("freqs") or st.get(key),
                                config.VDL2_FREQS_DEFAULT if is_vdl2 else config.ACARS_FREQS_DEFAULT)
        werr = _vdl2_window_error(freqs) if is_vdl2 else _acars_window_error(freqs)
        if werr:
            return jsonify(ok=False, error=werr, state=st), 400
        start = _start_vdl2b if is_vdl2 else _start_acars2
        params, perr = _parse_tune(st)
        if perr:
            params, _ = _parse_tune(appstate.DEFAULT_STATE)
        if not TUNE_LOCK.acquire(timeout=0.5):
            return jsonify(ok=False, error="פעולה אחרת מתבצעת — נסה שוב", state=appstate.load_state()), 409
        try:
            _scan_stop_thread()
            st = appstate.load_state()
            if _composite_secondary_only(mode, st):
                # כבר במצב (או הקול חי והמשני נפל): החלפת תדרי הדאטה ⇒ רק המשני מאותחל, הקול לא נוגע
                if freqs == st.get(key) and _secondary_starting_or_up(mode):
                    # אותה בקשה בדיוק (לחיצה על הכרטיס החי) — אין מה לאתחל
                    return jsonify(ok=True, app_mode=mode, **{key: freqs}, secondary_only=True)
                log.info("mode -> %s: רק %s על המשני %s (from %s)", mode, label, freqs, request.remote_addr)
                err, detail = start(freqs)
                if err:
                    # ⚠ ה-env החדש נשאר על הדיסק ו-Restart=always היה מרים *אותו* בעוד ה-state
                    # מראה את הישן ⇒ מחזירים את הישן (כמו /api/tune2)
                    back, _d = start(st.get(key))
                    return jsonify(ok=False, error=err + (" — הקול על הראשי ממשיך" if back is None else
                                                          " — וגם החזרה לבנק הקודם נכשלה; הקול ממשיך"),
                                   detail=detail, state=appstate.load_state()), 500
                appstate._merge_state({key: freqs})
                return jsonify(ok=True, app_mode=mode, **{key: freqs}, secondary_only=True)
            log.info("mode -> %s freq=%.3f %s=%s (from %s)",
                     mode, params["freq"], label, freqs, request.remote_addr)
            err, detail, _sdr_down = (_enter_voice_vdl2 if is_vdl2 else _enter_voice_acars)(params, freqs)
            if err:
                payload, status = _fail_to_off(st, err, detail, "enter " + mode)
                return jsonify(payload), status
            appstate._merge_state({"app_mode": mode, key: freqs})
            return jsonify(ok=True, app_mode=mode, **{key: freqs})
        finally:
            TUNE_LOCK.release()

    if mode == "voice2":
        # שני מקלטים: קול (ההגדרות השמורות) על הראשי + ערוץ קול שני (freq2/mod2 או השמור) על המשני
        rerr = _roles_error("voice2")
        if rerr:
            return jsonify(ok=False, error=rerr, state=st), 400
        v2, verr = _parse_voice2(data, st)
        if verr:
            return jsonify(ok=False, error=verr, state=st), 400
        params, perr = _parse_tune(st)
        if perr:
            params, _ = _parse_tune(appstate.DEFAULT_STATE)
        if not TUNE_LOCK.acquire(timeout=0.5):
            return jsonify(ok=False, error="פעולה אחרת מתבצעת — נסה שוב", state=appstate.load_state()), 409
        try:
            _scan_stop_thread()
            st = {**appstate.load_state(), **v2}
            if _composite_secondary_only("voice2", st):
                # כבר בקול כפול: רק הערוץ השני מאותחל (כמו /api/tune2), הראשי לא נוגע
                old = appstate.load_state()
                if (_voice2_params(old) == (v2["voice2_freq"], v2["voice2_mod"])
                        and _secondary_starting_or_up("voice2")):
                    return jsonify(ok=True, app_mode="voice2", voice2_freq=v2["voice2_freq"],
                                   voice2_mod=v2["voice2_mod"], secondary_only=True)
                err, detail = _start_voice2(st)
                if err:
                    back, _d = _start_voice2(old)     # ר' voice_acars למעלה
                    return jsonify(ok=False, error=err + (" — הערוץ הראשי ממשיך" if back is None else
                                                          " — וגם החזרה לתדר הקודם נכשלה; הראשי ממשיך"),
                                   detail=detail, state=appstate.load_state()), 500
                new = appstate._merge_state(v2)
                return jsonify(ok=True, app_mode="voice2", voice2_freq=new["voice2_freq"],
                               voice2_mod=new["voice2_mod"], secondary_only=True)
            log.info("mode -> VOICE2 freq=%.3f freq2=%.3f (from %s)",
                     params["freq"], v2["voice2_freq"], request.remote_addr)
            err, detail, _sdr_down = _enter_voice2(params, st)
            if err:
                payload, status = _fail_to_off(st, err, detail, "enter voice2")
                return jsonify(payload), status
            new = appstate._merge_state({"app_mode": "voice2", **v2})
            return jsonify(ok=True, app_mode="voice2", voice2_freq=new["voice2_freq"],
                           voice2_mod=new["voice2_mod"])
        finally:
            TUNE_LOCK.release()

    plan = freqs = key = enter = bias_tee = skip_c = spectrum = gain = None
    if mode == "scan":
        plan = _validate_scan_plan(data.get("plan") or st.get("scan_plan"))
        if plan is None:
            return jsonify(ok=False, error="לוח סריקה לא תקין (1-8 רגלים, "
                           "כל רגל מצב+זמן שהייה תקין)", state=st), 400
    elif mode in ("acars", "vdl2", "satcom"):
        # satcom משתלב באותו זנב גנרי: "freqs" הוא כאן דגל לוויין בן-איבר-יחיד
        # (geostationary, לא בנק ערוצים) — ר' _sanitize_satellite/_satcom_window_error.
        key, default, sanitize, wcheck, enter = {
            "acars": ("acars_freqs", config.ACARS_FREQS_DEFAULT, _sanitize_freqs,
                      _acars_window_error, _enter_acars),
            "vdl2": ("vdl2_freqs", config.VDL2_FREQS_DEFAULT, _sanitize_freqs,
                     _vdl2_window_error, _enter_vdl2),
            "satcom": ("satcom_freqs", config.SATCOM_FREQS_DEFAULT, _sanitize_satellite,
                       _satcom_window_error, _enter_satcom),
        }[mode]
        freqs = sanitize(data.get("freqs") or st.get(key), default)
        werr = wcheck(freqs)                     # חייב להיכנס בחלון דגימה אחד (satcom: לוויין תקין)
        if werr:
            return jsonify(ok=False, error=werr, state=st), 400
        if mode == "satcom":
            # bias_tee=False למי שמזין את ה-LNA ממקור חיצוני (ר' _enter_satcom).
            # בקשה מפורשת (bool) גוברת; אחרת נשמר הבחירה הקודמת מה-state (כמו
            # freqs); state חדש/ישן-בלי-השדה => True (ההתנהגות ההיסטורית).
            bias_tee = (data["bias_tee"] if isinstance(data.get("bias_tee"), bool)
                       else bool(st.get("satcom_bias_tee", True)))
            # skip_c: אותו דפוס "מפורש גובר, אחרת הזכור, אחרת ברירת מחדל" —
            # אבל כאן ברירת המחדל היא True (חיסכון), ר' write_satcom_env.
            skip_c = (data["skip_c"] if isinstance(data.get("skip_c"), bool)
                      else bool(st.get("satcom_skip_c", True)))
            # spectrum: אותו דפוס בדיוק. ברירת מחדל True — כלי האבחון היחיד
            # שמבחין "אין RF" מ"יש RF בלי נעילה" (ר' SATCOM_SPECTRUM_BINS).
            spectrum = (data["spectrum"] if isinstance(data.get("spectrum"), bool)
                        else bool(st.get("satcom_spectrum", True)))
            # gain: כאן *לא* אפשר להשתמש ב-data.get() כדי לזהות "לא נשלח" —
            # ‏null הוא ערך משמעותי (AGC מפורש) ולא היעדר. לכן בדיקת מפתח.
            gain = (_sanitize_satcom_gain(data["gain"], st.get("satcom_gain"))
                    if "gain" in data
                    else _sanitize_satcom_gain(st.get("satcom_gain")))

    if not TUNE_LOCK.acquire(timeout=0.5):
        return jsonify(ok=False, error="פעולה אחרת מתבצעת — נסה שוב",
                       state=appstate.load_state()), 409
    try:
        _scan_stop_thread()   # תפסנו את הנעילה — הבקשה תקינה, עוצרים סבב קודם (אם יש)
        st = appstate.load_state()     # רענון אחרי stop_thread — לא לדרוס שינוי מקביל
        if mode == "off":
            # כיבוי (standby): עוצר את כל צרכני ה-SDR ומשחרר את ה-RSP1B ליישום
            # אחר. airam-web/הדף נשארים פעילים => אפשר להדליק שוב מה-UI בכל רגע.
            log.info("mode -> OFF (standby) (from %s)", request.remote_addr)
            err, detail = _enter_standby()
            if err:
                log.warning("enter standby failed: %s", err)
                return jsonify(ok=False, error=err, detail=detail, state=st), 500
            # prev_mode => כפתור ההדלקה ב-UI מחזיר את המצב האחרון, בלי לקודד קול.
            # ⚠ אם כבר off (טאב כפול/לחיצה כפולה ששלחו שתי בקשות off) — לא
            # דורסים prev_mode ב-"off" עצמו; שומרים את prev_mode הקודם כדי
            # שכפתור ההדלקה עדיין יזכור את המצב האמיתי האחרון שהיה פעיל.
            cur_mode = st.get("app_mode", "off")
            appstate._merge_state({"app_mode": "off",
                          "prev_mode": (cur_mode if cur_mode != "off"
                                        else st.get("prev_mode", "off"))})
            return jsonify(ok=True, app_mode="off")

        if mode == "scan":
            log.info("mode -> SCAN plan=%s (from %s)", plan, request.remote_addr)
            err, detail = _scan_activate(plan)
            if err:
                payload, status = _fail_to_off(st, err, detail, "enter scan (leg 0)")
                return jsonify(payload), status
            appstate._merge_state({"app_mode": "scan", "scan_plan": plan})
            return jsonify(ok=True, app_mode="scan", scan_plan=plan)

        # acars / vdl2 / satcom — מסלול דאטה סימטרי (satcom מקבל גם bias_tee/skip_c/spectrum)
        log.info("mode -> %s freqs=%s (from %s)", mode, freqs, request.remote_addr)
        err, detail = (enter(freqs, bias_tee, skip_c, spectrum, gain) if mode == "satcom"
                       else enter(freqs))
        if err:
            payload, status = _fail_to_off(st, err, detail, "enter " + mode)
            return jsonify(payload), status
        extra = ({"satcom_bias_tee": bias_tee, "satcom_skip_c": skip_c,
                  "satcom_spectrum": spectrum, "satcom_gain": gain}
                 if mode == "satcom" else {})
        appstate._merge_state({"app_mode": mode, key: freqs, **extra})
        return jsonify(ok=True, app_mode=mode, **{key: freqs}, **extra)
    finally:
        TUNE_LOCK.release()


@app.route("/api/scan")
def api_scan():
    """סטטוס סבב הסריקה החי: רגל נוכחית, אינדקס, ומועד המעבר הבא — ל-UI
    (ספירה לאחור, הדגשת הרגל הפעילה). ריק/idx=-1 כשאין סבב פעיל.
    ‏now: שעון השרת (epoch) — ה-UI מחשב ממנו סטייה חד-פעמית מול שעון הלקוח,
    כי next_switch_at הוא זמן-שרת ו-Date.now() בדפדפן הוא זמן-לקוח; בלי סטייה
    ל-Pi headless בלי RTC/NTP (תרחיש שדה אמיתי) הייתה יכולה להיות ספירה-
    לאחור שגויה (שלילית/ענקית) למרות שהשרת עצמו מדויק לגמרי."""
    with _scan_lock:
        status = dict(_scan_status)
        active = _scan_thread is not None and _scan_thread.is_alive()
    return jsonify(ok=True, active=active, now=time.time(), **status)


# --- שחזור מצב באתחול: airam-web הוא המתזמר ---------------------------------
BOOT_SDR_WAIT_SEC = 90    # המתנה ל-SDR באתחול לפני ניסיון כניסה (USB enumeration איטי)


def _config_stale():
    """קונפיג הקול חסר או ישן (שדרוג: בלי stats_filepath למדדי RF / localtime
    להקלטות / udp_stream לסינון הרעשים) => צריך שכתוב לפני שמרימים את rtl_airband."""
    try:
        cur = CONFIG_PATH.read_text()
    except OSError:
        return True
    return ("stats_filepath" not in cur or "localtime" not in cur
            or "udp_stream" not in cur)


def _boot_restore():
    """אורקסטרציית אתחול: אף צרכן SDR אינו enabled ב-systemd — airam-web (שעולה
    תמיד) קורא את state.json ומחזיר את המצב השמור, כולל off. כך המצב הנבחר שורד
    reboot בלי מצב ראשי ובלי הרחבת sudoers (רק restart/stop הקיימים).
    רץ ב-thread daemon => לא חוסם את app.run; כל כישלון => off + לוג, לעולם לא
    מפיל את שרת הווב."""
    try:
        st = appstate.load_state()
        mode = st.get("app_mode", "off")
        live = _live_mode()
        # scan: live הוא תמיד voice/acars/vdl2/None, לעולם לא "scan" עצמו (זו
        # אפליקציה מעל שלושת השירותים, לא שירות בפני עצמו) => הקיצור הזה תמיד
        # מדלג עליו וסבב הסריקה תמיד מתחיל מחדש מרגל 0 אחרי restart של airam-web
        # (גם אם רגל מסוימת כבר רצה תקין) — פשטות מכוונת, לא באג.
        if live == mode and not (mode in ("voice", *COMPOSITE_SECONDARY) and _config_stale()):
            if mode in ("voice", *COMPOSITE_SECONDARY):
                _denoise_after_voice()   # הקול כבר רץ — אולי בלי הסינון (שדרוג/restart)
            return   # restart של airam-web באמצע סשן: הצרכן השמור כבר רץ
        if (mode in COMPOSITE_SECONDARY and live == "voice" and not _config_stale()
                and _secondary_starting_or_up(mode)):
            # הקול חי והמשני בדרך (ה-sleep המדורג אחרי restart של sdrplay — install.sh מאתחל את
            # sdrplay ואז את airam-web): כניסה מלאה הייתה קוטעת את הקול ומרימה את המשני פעמיים
            _denoise_after_voice()
            return
        if mode == "off":
            if live:   # אחרי reboot ממילא כלום לא רץ => no-op
                _enter_standby()
            return
        # המתנה ל-SDR לפני הכניסה: ב-boot קר ה-USB עוד לא תמיד enumerated,
        # ו-airam-wait-sdrplay (ExecStartPre) מכסה רק ~30 שניות נוספות.
        for _ in range(BOOT_SDR_WAIT_SEC // 2):
            if _sdr_present():
                break
            time.sleep(2)
        if not TUNE_LOCK.acquire(blocking=False):
            return   # המשתמש כבר בחר מצב מה-UI — כוונתו גוברת על השחזור
        try:
            # ⚠ בין load_state() בראש הפונקציה לכאן עברו עד BOOT_SDR_WAIT_SEC
            # שניות (המתנה ל-SDR) — אם המשתמש הספיק לבחור מצב אחר מה-UI *ואותה
            # בחירה כבר הסתיימה* (הנעילה שוב פנויה), st הישן היה דורס אותה.
            # קוראים מחדש ומוותרים על השחזור אם המצב השמור השתנה בינתיים.
            st2 = appstate.load_state()
            if st2.get("app_mode", "off") != mode:
                log.info("boot restore: המצב השמור השתנה בזמן ההמתנה ל-SDR (%s) — מוותרים על השחזור",
                         st2.get("app_mode"))
                return
            st = st2
            if mode == "voice":
                params, perr = _parse_tune(st)
                if perr:   # state פגום => ברירת מחדל
                    params, _ = _parse_tune(appstate.DEFAULT_STATE)
                err, detail, sdr_down = _enter_voice(params)
                if err and sdr_down:
                    # הכוונה נשמרת: Restart=always של היחידה ימשיך לנסות,
                    # udev ירים את sdrplay כשה-SDR יחובר; health מראה תקלה.
                    log.warning("boot restore: SDR לא נוכח — הקול יעלה כשיחובר")
                    return
            elif mode in COMPOSITE_SECONDARY:
                params, perr = _parse_tune(st)
                if perr:
                    params, _ = _parse_tune(appstate.DEFAULT_STATE)
                err, _detail, sdr_down = _enter_composite_saved(mode, params, st)
                if err and sdr_down:
                    log.warning("boot restore: SDR לא נוכח — %s יעלה כשיחובר", mode)
                    return
            elif mode == "acars":
                err, _detail = _enter_acars(st.get("acars_freqs"))
            elif mode == "vdl2":
                err, _detail = _enter_vdl2(st.get("vdl2_freqs"))
            elif mode == "satcom":
                # ⚠ בטיחות: *לא* נכנסים אוטומטית ל-satcom באתחול. write_satcom_env
                # מדליק bias-T (‎+4.7V על מחבר האנטנה) כברירת מחדל, וכאן אין בן-אדם
                # בסביבה שיוודא איזו אנטנה מחוברת כרגע (VHF airband או L-band+LNA
                # שהוחלפה חזרה לפני ה-reboot). נופלים ל-off *בכוונה* (לא תקלת SDR) —
                # ה-err המלאכותי מפעיל את אותו מסלול "off + prev_mode" שלמטה, כך
                # שכפתור ⏻/כרטיס הבית יציעו כניסה ידנית (עם אישור אנטנה מפורש)
                # במקום לחכות ש-Restart=always יחזיר את bias-T בלי פיקוח.
                log.warning("boot restore: satcom לא משוחזר אוטומטית (בטיחות bias-T) — נשאר off, ממתין לכניסה ידנית")
                err, _detail = "satcom דורש כניסה ידנית אחרי reboot (בטיחות bias-T)", None
            else:   # scan
                plan = _validate_scan_plan(st.get("scan_plan"))
                if plan is None:
                    err, _detail = "לוח סריקה שמור לא תקין", None
                else:
                    err, _detail = _scan_activate(plan, wait_for_sdr=True)
            if err and mode in COMPOSITE_SECONDARY and _is_active("rtl_airband"):
                # התחלה חלקית: הקול על הראשי עלה, רק המשני נכשל (לרוב — לא מחובר). באתחול אין
                # מי שיחליט ⇒ לא הורסים קול עובד: הכוונה נשמרת, health מדווח תקלה, וה-reconcile
                # מרים את המשני כשיופיע (מסלול "הקול חי, המשני נפל").
                log.warning("boot restore: %s — הקול עלה, המשני לא (%s); ה-reconcile ינסה שוב", mode, err)
                return
            if err and mode in ("acars", "vdl2", *COMPOSITE_SECONDARY) and not _role_present("primary"):
                # ⚠ כמו voice למעלה: SDR מנותק באתחול (או USB שמתעורר אחרי
                # BOOT_SDR_WAIT_SEC) אינו כשל של המצב — הכוונה נשמרת, ו-
                # _mode_reconcile_loop נכנס מחדש כשה-SDR מופיע (הוא כבר מטפל
                # ב-acars/vdl2 ומדלג כל עוד _sdr_present()=False). קודם רק voice
                # קיבל את היחס הזה, ו-ACARS/VDL2 נפלו ל-off — ונשארו שם גם אחרי
                # שה-SDR חובר, כי ה-reconcile לא נוגע ב-off. health מדווח תקלה.
                log.warning("boot restore: SDR לא נוכח — %s יעלה כשיחובר", mode)
                return
            if err:
                log.warning("boot restore -> %s failed: %s — falling to off", mode, err)
                _enter_standby()
                appstate._merge_state({"app_mode": "off", "prev_mode": mode})
            else:
                log.info("boot restore -> %s", mode)
        finally:
            TUNE_LOCK.release()
    except Exception:
        log.exception("boot restore crashed (ignored)")


MODE_RECONCILE_INTERVAL_SEC = 60


def _mode_reconcile_once():
    """בדיקה בודדת (קרואה מ-_mode_reconcile_loop, וישירות מבדיקות): מתאוששת
    מקריסת sdrplay.service שלא הופצה לצרכן הפעיל.
    ⚠ Requires=sdrplay.service מפיץ עצירה *נקייה* בלבד (ש-Restart=always של
    הצרכן מתעלם ממנה במכוון), ו-PartOf=sdrplay.service מפיץ job מפורש של
    restart — אבל הפעלה-מחדש *פנימית* של sdrplay.service (Restart=always על
    קריסה עצמית, לא restart job חיצוני שמישהו יזם) לא בהכרח נחשבת ל-job כזה
    ומופצת ל-PartOf. בלעדיה, קריסת sdrplay.service יכולה להשאיר את התחנה
    שקטה גם אחרי ש-sdrplay עצמו כבר חזר לחיים לבד — בניגוד למטרה #3 בפרויקט
    ("מתאוששים מקריסה לבד"). בודקת אם המצב השמור *אמור* להריץ צרכן
    (voice/acars/vdl2 בלבד — לא off, לא scan שיש לו thread ייעודי משלו, ולא
    satcom שלא משוחזר אוטומטית בכוונה, ר' §12) אבל אף צרכן לא רץ בפועל, ומנסה
    כניסה מחדש. best-effort: כשל לא נופל ל-off (כדי לא להילחם עם המשתמש שאולי
    כבר מתקן ידנית) — פשוט מנסה שוב במחזור הבא."""
    st = appstate.load_state()
    mode = st.get("app_mode", "off")
    if mode not in ("voice", "acars", "vdl2", *COMPOSITE_SECONDARY):
        return                          # off/scan/satcom — לא בטיפול הפוליסה הזו
    composite = mode in COMPOSITE_SECONDARY
    live0 = _live_mode()
    if live0 is not None and not (composite and live0 == "voice"):
        return                          # צרכן כלשהו רץ בפועל — אין תקלה לתקן
    # ⚠ כל הבדיקות שלפני הנעילה זולות וקובעות *אם* בכלל לנסות: restart ליחידה שהמקלט שלה
    # לא מחובר חוסם עד 45ש' (השער + sleep המדורג) תחת TUNE_LOCK, ובזמן הזה כל כיוונון/מעבר
    # מצב — כולל היציאה של המשתמש מהמצב — מקבל 409 (ביקורת 17.11.2026, נמצא בבדיקה).
    if composite and live0 == "voice":
        if _secondary_starting_or_up(mode):
            return                      # systemd כבר מרים אותו (activating) — לא מתערבים
        if _roles_error(mode) or not _role_present("secondary"):
            return                      # המשני לא מוגדר / לא מחובר — הקול ממשיך, health מדווח
    elif not _role_present("primary"):
        # ⚠ גם כשהמשני חי (מצב כפול, הראשי נותק): כניסה מלאה הייתה עוצרת את המשני העובד בכל
        # דקה ואז נכשלת על הראשי — הורגת בדיוק את היתירות ששני מקלטים נותנים.
        return                          # המקלט הראשי לא מחובר — יעלה כשיחזור
    if not TUNE_LOCK.acquire(blocking=False):
        return                          # פעולה אחרת כרגע — לא מתחרים, ננסה שוב במחזור הבא
    try:
        st2 = appstate.load_state()
        live = _live_mode()
        if st2.get("app_mode") != mode or (live is not None and not (composite and live == "voice")):
            return                      # השתנה בזמן שחיכינו לנעילה
        if composite and live == "voice":
            # הקול חי, רק הצרכן על המשני נפל ⇒ מרימים רק אותו (לא נוגעים בקול)
            log.warning("mode reconcile: הצרכן על המקלט המשני (%s) לא פעיל — מנסה להרים אותו", mode)
            err, _d = _start_secondary(mode, st2)
            if err:
                log.warning("mode reconcile: המשני (%s) נכשל: %s — ננסה שוב במחזור הבא", mode, err)
            return
        log.warning("mode reconcile: %s אמור לרוץ אבל שום צרכן לא פעיל — מנסה כניסה מחדש", mode)
        if mode in ("voice", *COMPOSITE_SECONDARY):
            params, perr = _parse_tune(st2)
            if perr:
                params, _ = _parse_tune(appstate.DEFAULT_STATE)
            if mode == "voice":
                err, detail, _sdr_down = _enter_voice(params)
            elif _secondary_starting_or_up(mode):
                # הראשי נפל והמשני חי ⇒ מרימים רק את הראשי
                err, detail, _sdr_down = _enter_voice(params, keep_secondary=True)
            else:
                err, detail, _sdr_down = _enter_composite_saved(mode, params, st2)
        elif mode == "acars":
            err, detail = _enter_acars(st2.get("acars_freqs"))
        else:
            err, detail = _enter_vdl2(st2.get("vdl2_freqs"))
        if err:
            log.warning("mode reconcile: כניסה מחדש ל-%s נכשלה: %s — ננסה שוב במחזור הבא", mode, err)
    finally:
        TUNE_LOCK.release()


def _mode_reconcile_loop():
    """thread רקע: קורא ל-_mode_reconcile_once כל MODE_RECONCILE_INTERVAL_SEC.
    כשל בבדיקה בודדת לא מפיל את ה-thread — מנסה שוב במחזור הבא.
    גם מיישר את סינון הרעשים (_denoise_sync — שני forks לדקה): מכסה את מה
    שהווים הישירים לא רואים, למשל airam-web שאותחל בזמן שהקול כבר רץ, או
    התקנה של הרכיבים אחרי שהמתג כבר הודלק."""
    while True:
        time.sleep(MODE_RECONCILE_INTERVAL_SEC)
        try:
            _mode_reconcile_once()
        except Exception:
            log.exception("mode reconcile crashed (ignored)")
        _denoise_sync()   # לעולם לא זורק


# תלויות הניסוי/הרשם (experiment.py) — lambda שפותר את השם *בזמן הקריאה*, כך ש-monkeypatch על app ממשיך לעבוד.
def _scanning_now():
    with _scan_lock:
        return _scan_thread is not None and _scan_thread.is_alive()


experiment.bind(
    enter_voice=lambda *a, **k: _enter_voice(*a, **k),
    probe_params=lambda *a, **k: _probe_params(*a, **k),
    restore_after_probe=lambda *a, **k: _restore_after_probe(*a, **k),
    sample_probe_stats=lambda *a, **k: _sample_probe_stats(*a, **k),
    live_mode=lambda *a, **k: _live_mode(*a, **k),
    rtl_airband_start_wall=lambda *a, **k: _rtl_airband_start_wall(*a, **k),
    parse_stats=lambda *a, **k: parse_stats(*a, **k),
    tune_lock=lambda: TUNE_LOCK,
    stats_path=lambda: STATS_PATH,
    config_path=lambda: CONFIG_PATH,
    sample_sec=lambda: ANTENNA_CHECK_SAMPLE_SEC,
    version=VERSION,
    composite_secondary=lambda: COMPOSITE_SECONDARY,
    composite_label=lambda: COMPOSITE_LABEL,
    scanning=_scanning_now,
)


if __name__ == "__main__":
    # ניקוי קובצי tmp יתומים מכתיבה שנקטעה (כיבוי פתאומי) — *לפני* כל השאר,
    # ורק כאן: בעלייה אין מופע אחר באמצע כתיבה. ר' _cleanup_orphan_tmp.
    _orphans = _cleanup_orphan_tmp()
    if _orphans:
        log.warning("נוקו %d קובצי tmp יתומים (כתיבה שנקטעה — כיבוי פתאומי?)", _orphans)
    # sdr.env מסונכרן ל-state (שער airam-wait-sdrplay מחכה ל-serial הזה). עטוף: עלייה היא חובה.
    try:
        _st0 = appstate.load_state()
        if _st0.get("sdr_primary") or _st0.get("sdr_secondary") or SDR_ENV_PATH.exists():
            write_sdr_env(_st0.get("sdr_primary"), _st0.get("sdr_secondary"))
    except Exception as _e:
        log.warning("sdr.env sync failed: %s", _e)
    # אין צרכן SDR enabled ב-systemd => שחזור המצב השמור (voice/acars/vdl2/
    # satcom/scan/off) נעשה כאן, ברקע (satcom חריג — לא משוחזר אוטומטית,
    # ר' _boot_restore/§12 ב-CLAUDE.md). מכסה גם שדרוג קונפיג
    # (stats_filepath/localtime) — כניסה לקול תמיד משכתבת את הקונפיג מה-state.
    try:
        _diag_boot()      # בדיקה שנקטעה ב-restart/reboot ממשיכה (ר' שם)
    except Exception:
        log.exception("יומן בדיקה: שחזור בעלייה נכשל — ממשיכים בלי")
    threading.Thread(target=_boot_restore, daemon=True).start()
    # התאוששות מקריסת sdrplay.service שלא הופצה לצרכן הפעיל (ר' _mode_reconcile_loop) —
    # thread נפרד מ-_boot_restore: זה רץ *במשך* הסשן, לא רק פעם אחת באתחול.
    threading.Thread(target=_mode_reconcile_loop, daemon=True).start()
    recordings.REC_DIR.mkdir(parents=True, exist_ok=True)
    recordings._saved_dir().mkdir(parents=True, exist_ok=True)
    threading.Thread(target=_activity_watcher, daemon=True).start()
    # ⚠ טעינת ההיסטוריה עטופה: היא רצה לפני app.run(), וכל חריגה כאן מונעת
    # מהמתזמר לעלות => לולאת קריסה (Restart=always) והתחנה כולה מתה. זה כבר
    # קרה פעמיים מקובץ שנפגם בכיבוי פתאומי (שורה שאינה אובייקט, ותו UTF-8 קטוע
    # — ר' storage.jsonl_records/storage.read_log_lines). היסטוריה היא נוחות; עלייה היא חובה.
    for _load in (_load_acars_history, _load_vdl2_history, _load_satcom_history):
        try:
            _load()                                                 # לפני ה-listener => אין מרוץ
        except Exception:
            log.exception("%s נכשל — ממשיכים בלי היסטוריה", _load.__name__)
    threading.Thread(target=_acars_listener, daemon=True).start()   # פיד UDP מ-acarsdec (שקט במצב קול)
    threading.Thread(target=_vdl2_listener, daemon=True).start()    # פיד UDP מ-dumpvdl2 (שקט בשאר המצבים)
    threading.Thread(target=_satcom_listener, daemon=True).start()  # פיד UDP מ-inmarsat-sniffer (שקט בשאר המצבים)
    # הצלבת ADS-B (docs/adsb-crosscheck-design.md) — thread נפרד; ה-listeners רק מכניסים לתור
    threading.Thread(target=_xc_worker, name="xcheck", daemon=True).start()
    # תמלול ATC — דמון נפרד (לא חוסם את היומן/retention). עולה **תמיד**, גם
    # כש-transcribe_auto כבוי: תמלול לפי דרישה (📝) ותמלול של הקלטות שמורות
    # (★) עובדים בכל מקרה. ה-thread ישן כשwhisper לא מותקן ומזהה התקנה
    # מאוחרת לבד — קודם הוא עשה return ומת, וזו הייתה אחת הסיבות שהפיצ'ר
    # "לא עבד" בלי שאיש ידע (ר' _transcribe_worker).
    threading.Thread(target=_transcribe_worker, daemon=True).start()
    adsb.start()   # רק כשרצים כשרת (לא בזמן import) - דמון, לא מעכב עלייה
    # threaded: סטרים /stream הוא חיבור ארוך-טווח => חייב לא לחסום בקשות אחרות
    app.run(host="0.0.0.0", port=8080, threaded=True)
