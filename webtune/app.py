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
import gzip
import hmac
import io
import json
import logging
import os
import re
import shutil
import socket
import statistics
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, request, jsonify, send_from_directory, send_file, abort

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
# MHz — מזיזים את centerfreq מהתדר כדי להתרחק מ-spike ה-DC. ⚠ **לא 0.3 עגול** (v2.28.0):
# rtl_airband v5.2.0 בוחר את ה-bin ב-ceil(x−1) (config.cpp:670), כש-x = (freq+rate−center)/
# (rate/fft_size). ‏0.3MHz = בדיוק 60 bins של 5kHz ⇒ x שלם ⇒ נבחר bin אחד *מתחת* — כל ערוץ
# נדגם 5kHz מתחת לתדר (סימולציה מלאה: עיוות גבוה יותר, ודחיית הערוץ השכן מלמטה ‎-34dB
# במקום ‎-57dB). ב-0.2999 ‏x≈452.02 ⇒ ה-bin הנכון (452), 100Hz ממרכזו — מרווח שבולע את
# קיטום (int)(MHz·1e6) של שני הערכים (parse_anynum2int, config.cpp:298-310). אל "תעגל" ל-0.3.
DC_OFFSET = 0.2999
CHANNEL_BW_NARROW = 7000    # Hz — `bandwidth` לערוץ (מסנן Bessel מסדר 2 ב-bw/2, config.cpp:595-618)
AUDIO_LOWPASS_DEFAULT = 2500   # Hz — ברירת המחדל של rtl_airband (config.cpp:328)
AUDIO_LOWPASS_OPTIONS = (2500, 3000)
# רווח SDRplay (מודל legacy של SoapySDRPlay3): שני אלמנטים נפרדים, וקטן יותר = רווח גדול יותר.
#   IFGR - הפחתת רווח בתדר הביניים, 20–59 dB.
#   RFGR - מצב ה-LNA (הפחתת רווח RF), 0–9 (לא-לינארי, ~7dB לצעד).
# כש-AGC כבוי כותבים gain = "IFGR=..,RFGR=.."; כש-AGC דלוק משמיטים את gain => AGC חומרתי.
IFGR_MIN, IFGR_MAX = 20, 59
RFGR_MIN, RFGR_MAX = 0, 9
IF_GAIN_DEFAULT = 40            # IFGR - אמצע הטווח, בטוח מפני עומס יתר
# RFGR - מצב LNA בינוני. ⚠ עד v2.26.0 הקבוע הזה **לא נאכף במצב AGC** (בלי שורת
# gain הדרייבר השאיר LNAstate=0 — רווח RF מקסימלי, ר' render_config). מאז
# הוא נכתב גם תחת AGC (‎rfgain_sel ב-device_string) — docs/voice-rf-quality-plan.md §2.2/1.
RF_GAIN_DEFAULT = 4
# ⚠ הוסר: OVERLOAD_DBFS (‎-3dBFS על רמת *הערוץ* מה-stats). הערוץ נמדד אחרי
# ה-AGC ובתוך bin אחד — הוא לא רואה את ה-ADC/LNA, ולכן שתק בעומס אמיתי
# (docs/voice-rf-quality-plan.md §2.2/3). חיווי העומס מגיע עכשיו מאירועי
# החומרה עצמם (‎AIRAM_RF, ר' "טלמטריית RF מהחומרה" למטה), או "לא ידוע".
SQUELCH_MODES = {"auto", "open", "manual"}
SNR_MIN, SNR_MAX = 0.0, 60.0   # dB - תחום clamp ל-SNR ידני
SNR_DEFAULT = 9.0              # ≈ סף ה-auto הפנימי של rtl_airband (~9.54 dB)
STATS_PATH = Path("/run/rtl_airband_stats.txt")   # tmpfs - בלי שחיקת SD
STATS_MAX_AGE = 5.0            # rtl_airband כותב כל ~1 שנייה; ~5 כתיבות => סובל ג'יטר אך עדיין מזהה restart

# --- טלמטריית RF מהחומרה (docs/voice-rf-quality-plan.md, PR 1 · 1.4/1.5) -------
# SoapySDRPlay3 נבנה ע"י install.sh עם patch מקומי (patches/soapysdrplay3-airam-rf.patch)
# שרושם את אירועי ה-API שה-upstream בולע (Streaming.cpp ev_callback — הערות
# בלבד) ל-log של SoapySDR => stderr של rtl_airband => journald:
#   "AIRAM_RF overload=1" / "AIRAM_RF overload=0"   (Overload_Detected/Corrected)
#   "AIRAM_RF gain grdb=<uint> lna_grdb=<uint>"    (GainChange, מוגבל-קצב)
# ⚠ lna_grdb הוא הפחתת-הרווח של ה-LNA ב-dB, *לא* מצב ה-LNA (0–9) — ההערה
# "Beware, lnaGRdB is really the LNA GR, NOT the LNA state" ב-Streaming.cpp:163.
# סימן-הבנייה נכתב ע"י install.sh *רק* כשה-patch הוחל ונבנה בהצלחה => קיום
# הקובץ = "יש טלמטריה". בלעדיו כל שדה עומס מוצג כ"לא ידוע" (None), לעולם לא
# כ"תקין" (§12) — בלי patch שום שורת AIRAM_RF לא תגיע, ושקט אינו "אין עומס".
SOAPY_RF_MARK = Path("/usr/local/share/airam/soapysdrplay3.build-sig")
# תהליך journalctl *אחד* ארוך-חיים (לא fork לכל בקשה כמו _journal_tail):
# ‏-n 0 = רק שורות חדשות; -o cat = הודעה בלבד (בלי חותמת/מזהה) — הזמן נלקח
# בצד שלנו ברגע הקריאה (השהיית journald זניחה מול חלון של שידור שלם).
RF_JOURNAL_CMD = ["journalctl", "-f", "-u", "rtl_airband", "-o", "cat", "-n", "0", "--no-pager"]
RF_JOURNAL_BACKOFF_MIN = 2.0       # journalctl יצא => המתנה לפני הפעלה מחדש (מוכפלת עד MAX)
RF_JOURNAL_BACKOFF_MAX = 60.0
RF_JOURNAL_HEALTHY_SEC = 60.0      # ריצה ארוכה מזה => ה-backoff מתאפס
RF_BOOT_ATTACH_WAIT_SEC = 5.0     # _boot_restore ממתין עד כאן לעוקב לפני שמרים את rtl_airband (ר' _rf_follow_attached)
RF_MARK_RECHECK_SEC = 60.0         # בלי סימן-בנייה: אין מה לקרוא; בודקים שוב מדי פעם (התקנה מאוחרת)
# אירועי טלמטריה עם חותמת זמן — לחישוב חלון של הקלטה בודדת (sidecar ‏.rf.json).
# ה-GainChange מוגבל ל-≤1/ש' ב-patch => 4096 אירועים ≥ ~68 דק' בקצב המרבי שלו —
# הרבה מעבר לעיכוב הזיהוי של _activity_watcher (WATCH_INTERVAL). overload אינו
# מוגבל (כל קצה-מצב נרשם); בסערת קצוות שדוחקת את תחילת חלון, _rf_window_summary
# מחזיר None ("לא ידוע") ולא ספירה חלקית.
RF_EVENTS_MAX = 4096

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
DISCONNECT_DROP_DB = 10.0

# --- רשם ניסוי RF (docs/antenna-calibration-experiment.md) -----------------
# קובץ ה-stats של rtl_airband נדרס כל שנייה בלי היסטוריה, ובשטח אין SSH —
# כלומר בדיוק מה שהניסוי צריך (השניות שאחרי restart, הדקה שאחרי ניתוק אנטנה)
# לא ניתן לקריאה בעין מהטלפון. הרשם כותב שורה לכל כתיבה *חדשה* של ה-stats,
# עם הקונפיג שבאמת רץ (airband.conf, לא state.json — בדיקת האנטנה לא כותבת
# state), ו-mtime של שני הקבצים. ⚠ השוואת שני ה-mtime **לא** מזהה flush-יציאה
# של התהליך הקודם: ה-flush נכתב בזמן העצירה — *אחרי* כתיבת הקונפיג — ולכן
# stats_mtime שלו גדול מ-conf_mtime. ההבחנה האמינה היא מול זמן ההפעלה של
# התהליך (`_rtl_airband_start_wall`), שהניסוי האוטומטי רושם אחרי כל הפעלה.
# כבוי כברירת מחדל; thread רק כשמפעילים; כיבוי אוטומטי.
RFLOG_PATH = Path("/var/lib/airam/rf_log.jsonl")
RFLOG_MAX_SEC = 2 * 3600          # לא כותבים ל-SD לנצח אם שכחו לכבות
RFLOG_POLL_SEC = 0.4              # rtl_airband כותב ~1Hz — דוגמים מהר יותר כדי לא לפספס כתיבה
RFLOG_ROTATE_BYTES = 5_000_000    # בהפעלה: קובץ גדול מזה עובר ל-.prev (שעתיים ≈ 1.5MB)
RFLOG_LABEL_MAX = 40
# ה-fsync של הרשם דרך כינוי ברמת המודול — כך בדיקה שדוחסת זמן (test_experiment)
# יכולה לנטרל *רק אותו*, בלי למקף את os.fsync הגלובלי (ר' no_sleep ב-CHANGELOG:
# מיקוף גלובלי כבר הפך בדיקה בפרויקט לתלוית-מזל).
_rflog_fsync = os.fsync

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
    "A3": ("אישור טרום-המראה (PDC)", "clearance"),
    "16": ("דיווח מיקום (label 16)", "text"),
    "1L": ("דוח ניווט/דלק (1L)", "text"),
    # ארבעת אלה נצפו בקליטת SATCOM אמיתית (Alphasat, 16 דק', 206 הודעות) —
    # לא היו ממופים קודם ונפלו ל-fallback הגנרי "Label X". A0 מזוהה בוודאות
    # (מכיל "AFN" בטקסט עצמו — Aircraft/Airline Network logon, ARINC 620 A0-A3).
    # השלושה האחרים (1B/4P/2F) אינם labels אוניברסליים מתועדים — המיפוי מבוסס
    # על תוכן ההודעה שנצפתה בפועל (כמו 16/1L למעלה), לא על מפרט רשמי.
    "A0": ("AFN · רישום רשת (A0)", "comm"),
    "1B": ("יזום קישור רשת (1B)", "comm"),
    "4P": ("הודעת חברה חופשית (4P)", "text"),
    "2F": ("בקשת מיקום (2F)", "comm"),
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
    "A3": "uplink",     # PDC — אישור טרום-המראה מהקרקע אל המטוס
    "16": "downlink",   # דיווח מיקום מהמטוס
    "1L": "downlink",   # דוח ניווט/דלק מהמטוס
    ":;": "uplink",     # autotune — הוראת קרקע למקלט לעבור תדר
}
# header ניתוב של תחנת קרקע בתחילת הטקסט (למשל ‎.ATSXCXA או ‎/TLVATYA) => uplink.
# שמרני: דורש ‎. או ‎/ בתחילת השורה ואחריו מזהה תחנה אותיות-גדולות/ספרות.
_UPLINK_HEADER_RE = re.compile(r"^[./][A-Z][A-Z0-9]{3,7}\b")

# --- VDL2 (מצב שלישי: SDR אחד בהחלפה) --------------------------------------
# VDL Mode 2 (D8PSK, 31.5kbps) הוא הדור הבא של דאטה-לינק: רוב התעבורה בו היא
# ACARS-over-AVLC (אותן הודעות ACARS => כל הפרסרים הקיימים חלים), והשאר ATN/X.25
# (CPDLC/ADS-C) ו-XID (ניהול קישור). dumpvdl2 שולח כל פריים מפוענח כ-JSON ב-UDP
# ל-listener כאן (כמו acarsdec), וה-UI מושך מ-/api/vdl2. CHANGELOG ‏1.10.0 קבע
# ש-CPDLC לא קיים על ACARS VHF באזורנו — הוא רץ על VDL2; המצב הזה סוגר את הפער.
VDL2_SERVICE = "airam-vdl2"
VDL2_ENV_PATH = Path("/etc/airam/vdl2.env")
VDL2_UDP_PORT = 5557                  # חייב להתאים ל-port ב-airam-vdl2.service (host: ACARS_UDP_HOST)
# בנקי תדרי VDL2: כל התדרים בצביר 136.7–137.0 (span ‏250kHz) => תמיד חלון דגימה אחד.
# 136.975 הוא ה-CSC (Common Signalling Channel) העולמי — כמעט כל התעבורה באזורנו שם;
# 4 הערוצים המשניים (אירופה) מפוענחים בו-זמנית בחינם. בנק CSC-בלבד = fallback ל-CPU.
VDL2_BANKS = [
    {"id": "eu_csc", "name": "עולמי + אירופה (CSC+4)",
     "freqs": ["136.725", "136.775", "136.825", "136.875", "136.975"]},
    {"id": "csc", "name": "CSC בלבד (136.975)", "freqs": ["136.975"]},
]
VDL2_FREQS_DEFAULT = VDL2_BANKS[0]["freqs"]
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
# _normalize_acars, בדיוק כמו מסלול A של VDL2 (ר' _normalize_satcom).
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
SATCOM_SPECTRUM_BINS = 256            # ברירת מחדל לבקשת ספקטרום (web.c: 32..1024)
SATCOM_SPECTRUM_TIMEOUT = 3.0         # מעט יותר מ-health: מערך גדול יותר
SATCOM_LOG_TAIL_LINES = 40            # GET /api/satcom/log — מספיק לשורות הפתיחה
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
# רווח ברירת מחדל = AGC (ריק, כמו ACARS/VDL2). לרווח ידני מעבירים gRdB ל-
# write_satcom_env => --sdrplay-gain (הפחתה, קטן=רווח גדול). לא --soapy-gain (ר' שם).
SATCOM_BUF_MAX = 500                  # הודעות אחרונות בזיכרון (כמו ACARS/VDL2)
SATCOM_LOG_PATH = Path("/var/lib/airam/satcom.jsonl")
SATCOM_LOG_KEEP = 5000                # retention על הדיסק (זנב נשמר; ייצוא לניתוח)

# הקלטות: rtl_airband כותב קובץ MP3 לכל שידור (split_on_transmission) בשם
# <REC_BASENAME>_YYYYMMDD_HHMMSS_<Hz>.mp3 (.tmp בזמן כתיבה, rename בסגירה
# ~0.5ש' אחרי שהסקוולץ' נסגר). קובץ שהסתיים = אירוע ביומן השידורים.
REC_DIR = Path("/var/lib/airam/recordings")
REC_BASENAME = "airam"         # filename_template ב-config וגם עוגן הפרסור של השמות
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
ACTIVITY_PATH = Path("/var/lib/airam/activity.jsonl")
ACTIVITY_KEEP = 500            # היומן שורד את מחיקת הקבצים (retention) - רק בלי נגינה
ACTIVITY_RETURN = 50
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

DEFAULT_STATE = {"freq": 132.500, "mod": "am", "agc": True,
                 # הגדרות שמע (v2.28.0): מסנן ערוץ צר (bandwidth=7000) ורוחב שמע (lowpass)
                 "voice_narrow": False, "voice_lowpass": AUDIO_LOWPASS_DEFAULT,
                 # rf_gain = מצב ה-LNA של ה-RSP1B (0–9, קטן=רווח גדול) — חל **בשני**
                 # המצבים: ב-AGC ה-AGC של ה-API שולט רק ב-gRdB (IF), כך שה-LNA הוא
                 # בחירה שלנו גם שם (ר' render_config).
                 "if_gain": IF_GAIN_DEFAULT, "rf_gain": RF_GAIN_DEFAULT,
                 # מסנן ה-FM (‎rfNotchEnable של ה-RSP1B). כבוי כברירת מחדל = ברירת
                 # המחדל של ה-API (sdrplay_api_rsp1a.h:13) — אין עדיין מדידה שמצדיקה
                 # אחרת (docs/voice-rf-quality-plan.md §3).
                 "fm_notch": False,
                 "squelch_mode": "open", "squelch_snr": SNR_DEFAULT,  # ברירת מחדל ATIS => תמיד פתוח
                 # "voice" (rtl_airband) | "acars" (acarsdec) | "vdl2" (dumpvdl2) |
                 # "satcom" (inmarsat-sniffer) | "off" (standby).
                 # ברירת המחדל ניטרלית (off): אין "מצב ראשי" — התקנה טרייה נוחתת במסך
                 # הבית והמשתמש בוחר מצב. המצב הנבחר שורד reboot (משוחזר ע"י _boot_restore).
                 "app_mode": "off",
                 "acars_freqs": ACARS_FREQS_DEFAULT,
                 "vdl2_freqs": VDL2_FREQS_DEFAULT,
                 "satcom_freqs": SATCOM_FREQS_DEFAULT,
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
                 "satcom_gain": SATCOM_GAIN_DEFAULT,
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
                 "transcribe_lang": TX_LANG_DEFAULT}


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
def _device_string(agc, rf_gain, fm_notch):
    """ה-device_string של SoapySDR — מקור-אמת יחיד (גם _parse_airband_conf/_config_stale
    נשענים על הצורה שלו).
    ⚠ למה מפתחות נוספים ב-device_string *מגיעים* לדרייבר (מאומת מהמקור, לא הנחה):
      1. rtl_airband v5.2.0 מעביר את המחרוזת כמות שהיא ל-SoapySDRDevice_makeStrArgs —
         ‏input-soapysdr.cpp:196 (בדיקת יכולות, ואז unmake) ו-:220 (הפתיחה האמיתית).
      2. SoapySDR (Factory.cpp:154-157) ממזג את כל ה-kwargs של הקלט מעל תוצאת
         ה-enumerate ‏(hybridArgs) ומעביר אותם ל-make של הדרייבר (:177). ה-find של
         sdrplay מסנן רק לפי serial/mode (Registration.cpp:55,100) — מפתחות אחרים
         לא מפילים את ההתאמה.
      3. הבנאי של SoapySDRPlay3 (Settings.cpp:105-114) קורא writeSetting(key, value)
         לכל kwarg שאינו driver/label/mode/serial/soapy — בכל אחת משתי הפתיחות של
         rtl_airband, כך שגם המכשיר שבאמת מזרים מקבל אותם.
      4. writeSetting: ‏"rfgain_sel" => tunerParams.gain.LNAstate (Settings.cpp:1628-1630,
         תחת RF_GAIN_IN_MENU — ON כברירת מחדל ב-CMakeLists.txt:36); "rfnotch_ctrl" ל-RSP1B
         => rsp1aParams.rfNotchEnable (Settings.cpp:1745-1748,1777-1783; "false"=>0, כל
         ערך אחר=>1).
    ⚠ ולמה ה-LNA לא נדרס אחר כך תחת AGC: LNAstate נכתב **רק** ב-writeSetting
    (rfgain_sel) וב-setGain("RFGR") (Settings.cpp:597-601). rtl_airband קורא
    setGainMode(agc) תמיד, אבל setGain/setGainElement **רק כש-AGC כבוי**
    (input-soapysdr.cpp:247-270); setGainMode עצמו נוגע רק ב-agc.enable
    (Settings.cpp:553-566). ה-AGC של ה-API פועל על gRdB בלבד — אין שדה LNA ב-
    sdrplay_api_AgcT (sdrplay_api_control.h:36-45). ‏selectDevice() החוזר (מ-getSettingInfo)
    בוחר מחדש רק כשמכשיר *אחר* נבחר באותו תהליך (Settings.cpp:2031-2038) — לא אצלנו
    (RSP1B יחיד). לכן עד v2.26.0 (בלי rfgain_sel)
    ה-AGC רץ עם LNAstate=0 של ברירת-המחדל (sdrplay_api_tuner.h:63) — רווח RF מרבי
    ורוויה *לפני* ה-AGC (docs/voice-rf-quality-plan.md §2.2/1).
    ⚠ ברווח ידני לא מוסיפים rfgain_sel: ה-LNA נקבע שם ע"י RFGR בשורת gain
    (setGainElement אחרי הבנאי) — שני מקורות לאותו ערך היו מזמינים סתירה."""
    parts = ["driver=sdrplay", f"rfnotch_ctrl={'true' if fm_notch else 'false'}"]
    if agc:
        parts.append(f"rfgain_sel={int(rf_gain)}")
    return ",".join(parts)


def render_config(freq, mod, agc, if_gain, rf_gain, squelch_mode="auto", squelch_snr=SNR_DEFAULT,
                  fm_notch=False, narrow=False, lowpass=AUDIO_LOWPASS_DEFAULT):
    # מעגלים *פעם אחת* ובונים את שני הערכים מאותו מספר: עיגול נפרד של freq ושל freq+DC_OFFSET
    # בתדר חופשי עם 5 ספרות (למשל 132.28125) נתן הפרש 0.3000 — שוב ה-bin הלא-נכון
    f = round(float(freq), 4)
    lines = [
        "# נוצר אוטומטית ע\"י AIR-AM web tuner. שינויים ידניים נדרסים בכל כיוונון.",
        "localtime = true;   # חותמות הזמן בשמות קובצי ההקלטה בזמן מקומי",
        f'stats_filepath = "{STATS_PATH}";   # מדדי RF (signal/noise) ל-/api/metrics',
        "devices:",
        "(",
        "  {",
        '    type = "soapysdr";',
        f'    device_string = "{_device_string(agc, rf_gain, fm_notch)}";',
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
    if narrow and mod == "am":
        # רק ב-AM: ב-NFM מסנן ב-3.5kHz לפני הדיסקרימינטור היה חותך את הסטייה ומעוות
        # מסנן ערוץ לפני גלאי המעטפה — דוחה ערוצים צמודים (25/8.33kHz) שה-bin הרחב
        # (‎-3dB ב-±6.2kHz) מעביר; לא משפר SNR בתוך הערוץ. כבוי כברירת מחדל עד A/B בשטח.
        lines.append(f"        bandwidth = {CHANNEL_BW_NARROW};")
    if int(lowpass) != AUDIO_LOWPASS_DEFAULT:
        lines.append(f"        lowpass = {int(lowpass)};")   # רוחב השמע (LAME) — ברירת המחדל לא נכתבת
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
    (Restart=always / udev) ואסור שיקרא קובץ חצי-כתוב. tmp ייחודי לפר-thread
    (pid+ident) => שתי בקשות מקבילות (PUT /api/presets וכו') לא דורסות זו את
    קובץ ה-tmp של זו; ה-rename האחרון פשוט מנצח (last-write-wins), בלי קובץ פגום.
    ⚠ ‏fsync (על הקובץ *ועל התיקייה*) הוא מה שהופך את זה גם לעמיד-בניתוק-חשמל,
    לא רק אטומי-מול-קוראים: בלעדיו הנתונים יכולים לשבת ב-page cache ולהיעלם
    בכיבוי פתאומי (תרחיש אמיתי בהפעלה מסוללה — ר' README, אזהרת ספק כוח).
    ה-fsync על התיקייה נדרש כי בלעדיו ה-rename עצמו לא בהכרח שרד."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.tmp{os.getpid()}-{threading.get_ident()}")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    # התיקייה עצמה: best-effort — כשל כאן לא שווה הפלת הכתיבה שכבר הצליחה
    try:
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


# תבנית קובצי ה-tmp של _atomic_write (‎<שם>.<סיומת>.tmp<pid>-<tid>) — לניקוי
# יתומים בעלייה. ניתוק חשמל *בין* הכתיבה ל-rename משאיר קובץ כזה מאחור.
_TMP_GLOB = "*.tmp*"
_TMP_ORPHAN_AGE_SEC = 3600     # שעה: זהיר בהרבה ממשך כתיבה אמיתי (מילישניות)


def _cleanup_orphan_tmp(dirs=None):
    """מוחק קובצי tmp יתומים שנשארו מכתיבה שנקטעה (ניתוק חשמל באמצע
    _atomic_write). ⚠ נקרא *רק בעלייה* ורק על קבצים ישנים מ-_TMP_ORPHAN_AGE_SEC:
    ה-pid בשם חוזר על עצמו במערכת, ולכן אי אפשר להסיק ממנו בבטחה שהתהליך מת —
    מחיקת tmp של כתיבה *חיה* תפיל אותה. מחזיר את מספר הקבצים שנמחקו (ללוג)."""
    removed = 0
    now = time.time()
    default_dirs = (STATE_PATH.parent, CONFIG_PATH.parent, ACARS_ENV_PATH.parent,
                    REC_DIR, _saved_dir())
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


def write_config(freq, mod, agc, if_gain, rf_gain, squelch_mode="auto", squelch_snr=SNR_DEFAULT,
                 fm_notch=False, narrow=None, lowpass=None):
    """narrow/lowpass=None => מההעדפה השמורה (voice_narrow/voice_lowpass) — כך כל מסלול
    שכותב קונפיג קול (בדיקת אנטנה, 🩺, ניסוי, סריקה, שחזור) שומר על הגדרות השמע של המשתמש."""
    if narrow is None or lowpass is None:
        st = load_state()
        narrow = bool(st.get("voice_narrow", False)) if narrow is None else narrow
        lowpass = _sanitize_lowpass(st.get("voice_lowpass")) if lowpass is None else lowpass
    _atomic_write(CONFIG_PATH, render_config(freq, mod, agc, if_gain, rf_gain, squelch_mode,
                                             squelch_snr, fm_notch=fm_notch, narrow=bool(narrow),
                                             lowpass=_sanitize_lowpass(lowpass)))


def _sanitize_lowpass(v):
    try:
        v = int(v)
    except (TypeError, ValueError):
        return AUDIO_LOWPASS_DEFAULT
    return v if v in AUDIO_LOWPASS_OPTIONS else AUDIO_LOWPASS_DEFAULT


_state_corrupt_warned = False   # חד-פעמי לאירוע פגימה, לא לכל קריאה — ר' load_state


def load_state():
    """קורא את המצב השמור, ממוזג על ברירות המחדל (שדות חדשים בשדרוג נכנסים לבד).
    ⚠ מבחין בין *קובץ חסר* (תקין לגמרי — התקנה טרייה) לבין *קובץ פגום*: פגימות
    מתרחשת בעיקר בכיבוי פתאומי (ר' _atomic_write), ובלי לוג המשתמש היה מאבד
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
    merged = {**DEFAULT_STATE, **st}
    # הגירה חד-פעמית (v2.26.0): עד v2.26.0 rf_gain היה *חסר-השפעה* תחת AGC (שום שורת
    # LNA לא נכתבה, והסליידר היה מושבת) — אבל ה-UI שלח אותו בכל כיוונון, כך שב-state
    # נשאר ערך ה-RFGR הידני האחרון. מאז rfgain_sel חל תחת AGC, ובלי הגירה _boot_restore
    # (‏_config_stale) היה מחיל בשקט, למשל, RFGR=9 — הפחתת ה-LNA הגדולה ביותר — על מי
    # שפעם הזיז את הסליידר ידנית וחזר ל-AGC. state מלפני v2.26.0 מזוהה בהיעדר
    # המפתח fm_notch (כל save_state מאז כותב אותו — ר' DEFAULT_STATE), ולכן ההגירה
    # נעלמת מעצמה בשמירה הראשונה. ברווח ידני rf_gain תמיד היה בתוקף => לא נוגעים.
    if "fm_notch" not in st and _parse_bool(merged.get("agc", True), True):
        merged["rf_gain"] = RF_GAIN_DEFAULT
    return merged


def _reset_state_corrupt_warned():
    """מאפס את ה-flag. נחוץ לבדיקות (מצב גלובלי דולף בין בדיקות שמשאירות
    state.json פגום — בלי איפוס, בדיקה הבאה שמצפה ללוג הייתה מדוכאת בשקט)."""
    global _state_corrupt_warned
    _state_corrupt_warned = False


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
    ה-restart עצמו יכול לחסום עד ~30 שניות (airam-wait-sdrplay) כשה-SDR מנותק.
    ⚠ נקודת-החנק של כל הפעלה של rtl_airband ש-AIR-AM יוזם (כיוונון, כניסה לקול,
    סריקה, בדיקת אנטנה, ניסוי) — ולכן כאן מאפסים את מוני הטלמטריה של הסשן
    (‏_rf_session_reset => "לא ידוע" עד שה-stream=start של התהליך החדש מגיע).
    הפעלה שלא אנחנו יזמנו (Restart=always אחרי קריסה, PartOf של sdrplay) נתפסת
    משורת ה-stream=start של התהליך החדש ביומן (ר' _RF_START_RE)."""
    _rf_session_reset("restart")
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
    """כיוונון נכשל => משחזרים את ההגדרות האחרונות שעבדו ומרימים מחדש.
    מחזיר True אם השחזור הצליח (rtl_airband חי) — רולבק שנכשל מטופל אצל הקורא
    בנפילה ל-off (לא משאירים שירות בלולאת קריסה ולא מעמידים פנים שהקול חזר)."""
    log.warning("rollback to %.3f MHz", prev["freq"])
    try:
        write_config(prev["freq"], prev["mod"], prev["agc"], prev["if_gain"],
                     prev["rf_gain"], prev["squelch_mode"], prev["squelch_snr"],
                     fm_notch=bool(prev.get("fm_notch", False)),
                     narrow=prev.get("voice_narrow"), lowpass=prev.get("voice_lowpass"))
        _rf_session_reset("rollback")   # הפעלה שנייה של rtl_airband — ר' _restart_and_verify
        subprocess.run([*SUDO, "systemctl", "restart", "rtl_airband"],
                       capture_output=True, text=True, timeout=45)
    except Exception:
        return False
    for _ in range(3):
        time.sleep(0.5)
        if not _is_active("rtl_airband"):
            return False
    return True


# --- ACARS: listener, ring-buffer, ומעבר מצב -----------------------------
_acars_lock = threading.Lock()
_acars_msgs = collections.deque(maxlen=ACARS_BUF_MAX)
_acars_seq = 0                 # מזהה רץ גלובלי (cursor ל-UI: "תן לי הודעות חדשות מ-id")

# --- VDL2: ring-buffer נפרד (אותה תבנית) -----------------------------------
_vdl2_lock = threading.Lock()
_vdl2_msgs = collections.deque(maxlen=VDL2_BUF_MAX)
_vdl2_seq = 0                  # cursor נפרד ל-/api/vdl2
_vdl2_drop_count = 0           # פריימים לא-מזוהים (סכמה לא תואמת) — נחשף בלוג תקופתי

# --- SATCOM: ring-buffer נפרד (אותה תבנית) ---------------------------------
_satcom_lock = threading.Lock()
_satcom_msgs = collections.deque(maxlen=SATCOM_BUF_MAX)
_satcom_seq = 0                # cursor נפרד ל-/api/satcom
_satcom_drop_count = 0         # הודעות לא-מזוהות (סכמה לא תואמת) — נחשף בלוג תקופתי


def _scan_latlon(obj):
    """סורק רקורסיבית מבנה libacars אחר *זוג* lat/lon תקין (ADS-C/CPDLC).
    מחזיר (lat, lon) או None. הגנתי לשינויי סכמה בין גרסאות — מזהה לפי שם
    המפתח, לא מבנה.
    ⚠ רגרסיה אמיתית שתוקנה: הגרסה הקודמת חיפשה lat ראשון ו-lon ראשון *בנפרד*
    בכל העץ ושילבה ביניהם — שני מפתחות שנמצאים בתת-עצים לא-קשורים בכלל
    (למשל שני tags שונים ברשימת ADS-C) הורכבו לקואורדינטה מזויפת. עכשיו lat
    ו-lon חייבים להופיע *יחד* באותו dict. בנוסף: ADS-C יכול לשאת גם
    "predicted_route"/"next_wpt"/"intermediate_projected_intent" עם lat/lon
    תקינים לכל דבר — אבל אלה waypoints עתידיים/מתוכננים, לא המיקום הנוכחי;
    "basic_report" הוא הדיווח בפועל (ר' libacars/adsc.c) ומועדף כשקיים."""
    def pair_in(o):
        lat = lon = None
        for k, v in o.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                kl = str(k).lower()
                if kl in ("lat", "latitude"):
                    lat = float(v)
                elif kl in ("lon", "lng", "long", "longitude"):
                    lon = float(v)
        return (lat, lon) if lat is not None and lon is not None else None

    def first_pair(o):
        if isinstance(o, dict):
            p = pair_in(o)
            if p:
                return p
            for v in o.values():
                p = first_pair(v)
                if p:
                    return p
        elif isinstance(o, list):
            for v in o:
                p = first_pair(v)
                if p:
                    return p
        return None

    def find_by_key(o, key):
        if isinstance(o, dict):
            for k, v in o.items():
                if str(k).lower() == key:
                    return v
                r = find_by_key(v, key)
                if r is not None:
                    return r
        elif isinstance(o, list):
            for v in o:
                r = find_by_key(v, key)
                if r is not None:
                    return r
        return None

    basic = find_by_key(obj, "basic_report")
    pair = first_pair(basic) if isinstance(basic, dict) else None
    if pair is None:
        pair = first_pair(obj)
    if pair is None:
        return None
    lat, lon = pair
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
    r"([NS])(\d{2})([0-5]\d\d)"     # lat: NS, 2-digit deg, MMf עם דקות 00–59
    r"([EW])(\d{3})([0-5]\d\d)"     # lon: EW, 3-digit deg, MMf עם דקות 00–59
                                    # (כמו _L15_RE: הפורמט נחלץ גם עם error ⇒ ספרת
                                    # דקות שהתהפכה חייבת להידחות, לא להזיז את המטוס)
    r",,\d{6},\d+,"                 # gap fields (time2, unknown)
    r"([A-Z][A-Z0-9]{1,7})"        # next waypoint (2–8 chars)
    r",(\d{6})"                     # ETA to waypoint (HHMMSS)
    r"(?:,,[^,]*,,[^,]*,([A-Z0-9]{2,6}))?"  # optional: fuel,,spd,{FL/alt code}
)


def _text_latlon(text):
    """heuristic שמרני לחילוץ מיקום מטקסט חופשי (פורמט ARINC קומפקטי). מחזיר (lat, lon)
    או None. מכוון לדיוק על פני כיסוי => מחזיר רק כשהתבנית מלאה וברורה.
    ⚠ דורש בדיוק התאמה *אחת* בטקסט: נצפה בקליטת שטח אמיתית (SATCOM) שהודעת H1
    עם תוכנית טיסה (‎#M3FPN/.../F:IVAKI,N32558E015065..LUMED,N34200E014420..)
    מכילה *שרשרת* waypoints בפורמט קומפקטי זהה לפורמט מיקום — ואם היינו לוקחים
    את ההתאמה הראשונה (כמו לפני התיקון), היינו מדביקים את נ"צ ה-waypoint
    הראשון במסלול כאילו הוא מיקום המטוס בפועל (לקח נוסף על 1.7.1/_parse_sq:
    לא רק "כתובת תחנה נראית כמו נ"צ", גם "מסלול מתוכנן נראה כמו דיווח מיקום
    בודד"). דיווח מיקום אמיתי מכיל זוג קואורדינטות *אחד*; שרשרת = לא מיקום."""
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

    matches = list(_TEXT_POS_RE.finditer(text))
    compact = False
    if not matches:
        matches = list(_TEXT_POS_COMPACT_RE.finditer(text))
        compact = True
    if len(matches) != 1:      # 0 = אין התאמה; 2+ = שרשרת (מסלול) — לא ניחוש איזו נכונה
        return None
    return _parse(matches[0].groups(), compact=compact)


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


# מזהה-סוג פנימי של libacars (למשל "adsc_msg", "basic_report") — snake_case נקי,
# לא טקסט אנושי. נצפה בקליטה אמיתית: "decoded" הציג "adsc_msg" כאילו זה תוכן
# ההודעה, כי המפתח (msg_type) תואם ל-"msg" והערך הוא תג-סוג ולא תוכן.
_LIBACARS_TAG_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _libacars_decode(obj):
    """(kind, text, decode_failed) ממבנה libacars: kind ל-badge ('CPDLC'/'ADS-C'/
    'ARINC-622'), טקסט קצר קריא (CPDLC clearance וכו') אם נמצא, ו-decode_failed
    (bool) — האם *המפענח עצמו* (inmarsat-sniffer/libacars) ניסה לפענח את היישום
    המקונן (CPDLC/ADS-C) והחזיר `err:true`, למרות שמעטפת ה-ACARS החיצונית עברה
    CRC בהצלחה. אומת מקליטת שדה אמיתית: הודעת CPDLC עם `crc_ok:true` ברמת
    המעטפת אבל `"cpdlc":{"err":true}` בפנים — כלומר יש הבדל אמיתי בין "לא ניסינו
    לפענח" (libacars ריק/חסר) ל"ניסינו, ונכשל" (איתות שולי מדי לתוכן, לרוב
    ב-CPDLC/ADS-C בקליטה ראשונה עם נעילה גבולית). §12: לא ממציאים טקסט-פענוח,
    אבל *כן* חושפים את העובדה שהניסיון נכשל — זה מידע אמיתי שקיים במבנה,
    לא ניחוש. הגנתי לשינויי סכמה."""
    blob = json.dumps(obj, ensure_ascii=False).lower()
    kind = ("CPDLC" if "cpdlc" in blob
            else "ADS-C" if ("adsc" in blob or "ads-c" in blob)
            else "ARINC-622")
    texts = []
    failed = [False]

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if str(k).lower() == "err" and v is True:
                    failed[0] = True
                if (isinstance(v, str) and len(v.strip()) > 3
                        and any(t in str(k).lower() for t in ("text", "msg", "message"))
                        and not _LIBACARS_TAG_RE.match(v.strip())):
                    texts.append(v.strip())
                else:
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(obj)
    text = " · ".join(dict.fromkeys(texts))[:300] or None   # dedup בשמירת סדר
    return kind, text, failed[0]


# --- re-decode של ARINC-622 בכיוון הנכון (libacars CLI) ---------------------
# ⚠ הרקע (קליטת SATCOM 14.08.2026, 12 CPDLC + 11 ADS-C, *כולן* uplink): מעטפות
# ה-ARINC-622 היו תקינות (crc_ok=true במעטפת הפנימית) אבל רוב ההודעות הוצגו
# כ-"לא פוענח". הסיבה לא הייתה איכות קליטה אלא ש-inmarsat-sniffer פענח את
# היישום המקונן ב*כיוון הלא-נכון*: ASN.1 PER תלוי-כיוון (FANSATCUplinkMessage
# מול FANSATCDownlinkMessage ב-CPDLC), ומשמעות ה-tags ב-ADS-C מתהפכת לגמרי
# (la_adsc_uplink_tag_descriptor_table מול la_adsc_downlink_tag_descriptor_table —
# tag 7 = "Periodic contract request" ב-uplink מול "Basic report" ב-downlink).
# ‏AIR-AM *כן* יודע את הכיוון האמיתי משכבת ה-ISU (structural_dir), ולכן אפשר
# לפענח מחדש מקומית בכיוון הנכון. הכיוון מגיע תמיד מ-structural_dir — **אין**
# כלל קשיח ש-AA/A6 הם GND2AIR (בקליטה הזו כן, בעתיד ייתכן downlink).
LIBACARS_DECODER = "decode_acars_apps"     # כלי ה-CLI הרשמי של libacars (מותקן ב-4b)
LIBACARS_DECODE_TIMEOUT = 2.0
LIBACARS_TEXT_MAX = 600                    # תקרת טקסט, כמו ה-300 של _libacars_decode
LIBACARS_CACHE_MAX = 128                   # ריבוי-בלוקים/שידור חוזר => אותה הודעה שוב

_ARINC622_DIRECT_LABELS = {"AA", "A6"}
_ARINC622_IMI_RE = re.compile(r"\.(AT1|CR1|CC1|DR1|ADS|DIS)\.")

_libacars_cache = collections.OrderedDict()   # (label, dir, text) -> (json, text)
_libacars_missing_logged = [False]            # אזהרת "בינארי חסר" פעם אחת, לא לכל הודעה


def _arinc622_kind(text):
    """מזהה את יישום ARINC-622 מתוך ה-IMI הגולמי: 'CPDLC' / 'ADS-C' / None.

    ⚠ לא מניחים AA==CPDLC ו-A6==ADS-C (בדרך כלל נכון, אבל ה-IMI בטקסט הוא מקור
    טוב יותר). משמש כשער-כניסה בלבד (בלי IMI מוכר לא מריצים subprocess) — libacars
    עצמו נשאר מקור-האמת היחיד לתוכן המפוענח."""
    if not isinstance(text, str):
        return None
    m = _ARINC622_IMI_RE.search(text)
    if not m:
        return None
    imi = m.group(1)
    if imi in ("AT1", "CR1", "CC1", "DR1"):
        return "CPDLC"
    if imi in ("ADS", "DIS"):
        return "ADS-C"
    return None


def _run_libacars(dir_arg, label, text, as_json):
    """מריץ decode_acars_apps פעם אחת ומחזיר stdout (str) או None. fail-safe מוחלט.

    ⚠ אבטחה: msg_text הוא תוכן רשת לא-מהימן => לעולם לא shell=True, תמיד argv נפרד."""
    env = os.environ.copy()
    if as_json:
        env["LA_JSON"] = "1"
    else:
        env.pop("LA_JSON", None)      # ירושה מהסביבה הייתה הופכת את מסלול הטקסט ל-JSON
    try:
        proc = subprocess.run([LIBACARS_DECODER, dir_arg, label, text],
                              capture_output=True, text=True,
                              timeout=LIBACARS_DECODE_TIMEOUT, env=env, check=False)
    except FileNotFoundError:
        if not _libacars_missing_logged[0]:      # פעם אחת, לא להציף את היומן
            _libacars_missing_logged[0] = True
            log.warning("libacars: %s לא נמצא; משאיר את פענוח inmarsat-sniffer",
                        LIBACARS_DECODER)
        return None
    except subprocess.TimeoutExpired:
        log.warning("libacars: timeout בפענוח %s %s", dir_arg, label)
        return None
    except OSError:
        log.exception("libacars: לא ניתן להריץ decoder")
        return None
    if proc.returncode != 0:
        log.warning("libacars: decoder נכשל rc=%d label=%s dir=%s: %s",
                    proc.returncode, label, dir_arg, (proc.stderr or "").strip()[:300])
        return None
    return proc.stdout or ""


def _strip_echoed_msg(stdout, text):
    """decode_acars_apps מדפיס קודם את ההודעה הגולמית (printf("%s\\n", txt) ב-
    examples/decode_acars_apps.c) ורק אחריה את הפענוח — מסירים את ההד."""
    lines = (stdout or "").splitlines()
    if lines and lines[0].strip() == (text or "").strip():
        lines = lines[1:]
    return "\n".join(lines)


def _decode_libacars_app(label, text, direction):
    """מפענח מחדש יישום ARINC-622 עם libacars בכיוון הנכון.

    direction: "uplink" -> LA_MSG_DIR_GND2AIR ('u') · "downlink" -> AIR2GND ('d').
    מחזיר (json_dict|None, text|None) — לעולם לא זורק exception.

    ⚠ שתי הרצות בכוונה, כי הן משלימות (אומת מהמקור של libacars):
      • TEXT (בלי LA_JSON) — המקור *היחיד* לטקסט האנושי. ב-ADS-C ה-JSON מכיל רק
        מפתחות snake_case וערכים מספריים (la_json_append_int64(…,"contract_num",…)
        ב-adsc.c) בלי שום משפט; ב-CPDLC הטקסט קיים ב-JSON אבל תחת המפתח
        "choice_label" (la_format_CHOICE_as_json ב-asn1-format-common.c),
        ש-_libacars_decode לא קוצר (הוא מחפש text/msg/message בלבד).
      • JSON — המבנה שמוזרם ל-_normalize_acars, כך שכל המנגנונים הקיימים
        (kind, decode_failed, adsc_dir_ok, _scan_latlon) ממשיכים לעבוד ללא שינוי.
    """
    if label not in _ARINC622_DIRECT_LABELS:
        return None, None
    if not isinstance(text, str) or not text.strip():
        return None, None
    if direction == "uplink":
        dir_arg = "u"
    elif direction == "downlink":
        dir_arg = "d"
    else:
        return None, None            # כיוון לא ידוע => לא מנחשים כיוון ASN.1
    if _arinc622_kind(text) is None:
        return None, None

    key = (label, direction, text)
    hit = _libacars_cache.get(key)
    if hit is not None:
        _libacars_cache.move_to_end(key)
        return hit

    decoded_json = None
    out = _run_libacars(dir_arg, label, text, as_json=True)
    if out is not None:
        body = _strip_echoed_msg(out, text)
        start = body.find("{")
        if start < 0:
            log.warning("libacars: לא התקבל JSON עבור label=%s dir=%s", label, direction)
        else:
            try:
                parsed = json.loads(body[start:])
            except (ValueError, TypeError):
                log.warning("libacars: JSON לא תקין עבור label=%s dir=%s", label, direction)
            else:
                if isinstance(parsed, dict):
                    decoded_json = parsed

    decoded_text = None
    out = _run_libacars(dir_arg, label, text, as_json=False)
    if out is not None:
        body = "\n".join(ln.rstrip() for ln in _strip_echoed_msg(out, text).splitlines()
                         if ln.strip())
        decoded_text = body.strip()[:LIBACARS_TEXT_MAX] or None

    result = (decoded_json, decoded_text)
    _libacars_cache[key] = result
    while len(_libacars_cache) > LIBACARS_CACHE_MAX:
        _libacars_cache.popitem(last=False)
    return result


def _acars_direction(label, text):
    """heuristic שמרני לכיוון ההודעה: 'uplink' (קרקע→מטוס) / 'downlink' (מטוס→קרקע) / None.
    label מוכר קודם (אמין), אחרת header ניתוב בטקסט => uplink. None כשלא חד-משמעי (לא מנחשים)."""
    d = _ACARS_DIR_BY_LABEL.get(label)
    if d:
        return d
    if isinstance(text, str) and _UPLINK_HEADER_RE.match(text.lstrip()):
        return "uplink"
    return None


# ⚠ רגרסיה אמיתית: הגרסה הקודמת דרשה "/" בין כיוון למהירות בכל מקרה, אז קבוצת
# רוח בתקן METAR הרגיל (dddssKT/dddssGggKT מחוברים, בלי "/" — הפורמט הנפוץ
# ביותר בפועל) לא נתפסה בכלל. גם הענף השני (WIND ddd/ss) לא לכד יחידה/משב
# (gust). שלושה ענפים: (1) תקן METAR מחובר, כולל gust אופציונלי; (2) ddd/ssKT
# בלי prefix "WIND" (פורמט שכבר נצפה); (3) "WIND ddd/ss" מילולי, עכשיו כולל
# gust ויחידה אופציונליים במקום לזרוק אותם.
_ATIS_WIND_RE = re.compile(
    r"\b(?P<d1>\d{3})(?P<s1>\d{2,3})(?P<g1>G\d{2,3})?KT\b"
    r"|(?P<d2>\d{3})/(?P<sk2>\d{2,3}KT)"
    r"|WIND\s+(?P<d3>\d+)/(?P<s3>\d+)(?P<g3>G\d+)?(?P<u3>KT)?"
)
_ATIS_RWY_RE = re.compile(r"R(?:WY|/W)\s?(\d{1,2}[LRC]?)", re.IGNORECASE)
_ATIS_QNH_RE = re.compile(r"Q(?:NH\s?)?(\d{4})")
_ACTYPE_RE = re.compile(r"\b(B7[3-9]\d|A[23][0-9]\d|E[17][0-9]\d|CRJ\d|AT[57]\d)\b")
# זוגות OUT/OFF/ON/IN + זמן (HHMM עם/בלי :) — \b לפני הכותרת, בלי \b אחריה כי הזמן
# עלול להיות צמוד (OUT1420). IN לא בתחילת מילה לפני ספרה אבל הפורמטים בשטח לא מופרדים.
_OOOI_PAIR_RE = re.compile(r"\b(OUT|OFF|ON|IN)\s?(\d{2}[:.]\d{2}|\d{4})", re.IGNORECASE)

# WX (בקשות מזג אוויר): מחלץ קודי ICAO מארבע אותיות. מסנן מילות-מפתח שאינן שדות תעופה.
# ⚠ אין לנו רשימת-אמת גלובלית של prefixes אזוריים תקינים לקוד ICAO — בלי אחת,
# הרג'קס לבד תופס *כל* מילה אנגלית בת 4 אותיות. _WX_NON_AIRPORT הוא הגנה
# יחידה, מטבעה חלקית; הורחב אחרי שהודגם בפועל ש-TEMP/DEWP/INFO/GATE/TIME/FUEL
# (מונחי METAR/דוח-מצב נפוצים, לא שדות) נספרו כ"alternate" (וניפחו גם את
# הרשימה ל-2+ קודים כשמדובר במונח בודד אמיתי) — כמו ACARS_LABELS, מתעדכן רק
# ממקרים שהודגמו, לא ניחוש מקיף.
_WX_ICAO_RE = re.compile(r"\b([A-Z]{4})\b")
_WX_NON_AIRPORT = frozenset({
    "METAR", "SPECI", "SIGMET", "PIREP", "ATIS", "CAVOK", "NOSIG", "TEMPO",
    "BECMG", "PROB", "FROM", "TILL", "WIND", "GUST", "SHRA", "TSRA", "ACFT",
    "ACARS", "UPDT", "REQU", "RESP",
    "TEMP", "DEWP", "INFO", "GATE", "TIME", "FUEL", "DATA", "LAST", "NEXT",
    "RMRK", "REMK",
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
        if m.group("d1"):        # תקן METAR מחובר: dddssGggKT
            wind = m.group("d1") + "/" + m.group("s1") + (m.group("g1") or "") + "KT"
        elif m.group("d2"):      # ddd/ssKT בלי prefix
            wind = m.group("d2") + "/" + m.group("sk2")
        else:                     # WIND ddd/ss[Ggg][KT] מילולי
            # ⚠ רגרסיה שנמצאה בסבב-בדיקה עצמאי: "or 'KT'" המציא יחידה שלא
            # הייתה בטקסט כש-KT לא נכתב במפורש — בדיוק ההפרה ש-§12 אוסר,
            # ושהתיקון הזה עצמו אוכף מפורשות ב-loadsheet/nav_fuel. אין KT
            # בקלט = אין KT בפלט, בדיוק כמו כל שאר השדות כאן.
            wind = (m.group("d3") + "/" + m.group("s3")
                   + (m.group("g3") or "") + (m.group("u3") or ""))
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
_SA_RE = re.compile(r"^0([EL])([VSHGCX2IT])([01]\d|2[0-3])([0-5]\d)([0-5]\d)([VSHGCX2IT]*)")

# H1 sub-label: '#' + מזהה מקור בן 2 תווים (#DF = מקליט, #M1 = FMC...). לא תמיד
# ממש בתחילת הטקסט: נצפה בקליטת SATCOM אמיתית (inmarsat-sniffer) שה-'#' מגיע
# אחרי prefix כמו "- " (‎"- #MDREQPOS037B") או אחרי שורת-הדר עם \n ("...\n- #DFREQ02")
# — ‏^#... בלבד (בלי \s) לא תפס אף הודעת H1 אמיתית אחת בקליטה של 465 הודעות/
# 12 H1. ‏(?:^|\s) מכסה גם את הפורמט המקורי (VHF, '#' ממש בהתחלה) וגם את זה —
# תוספתי בלבד, לא מצמצם את מה שכבר תפס.
_H1_SUB_RE = re.compile(r"(?:^|\s)#([A-Z][A-Z0-9])")
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
    """‎/FPN/ (תוכנית טיסה ב-H1): יציאה→יעד + רשימת waypoints. מחזיר string או None.
    ⚠ VHF: "/FPN/" (עם קו נטוי משני הצדדים). SATCOM אמיתי (inmarsat-sniffer):
    ה-'/' הפותח נבלע ע"י ה-sub-label עצמו (‎"#M3FPN/RP:DA:..." — FPN מודבק
    ישירות ל-M3, בלי '/' לפניו) — "FPN/" בלי הקו הנטוי הפותח הוא נפילה
    תוספתית, לא מחליפה את "/FPN/" (שנבדק ראשון, מדויק יותר)."""
    idx = text.find("/FPN/")
    if idx < 0:
        idx = text.find("FPN/")
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
    מחזיר string קצר או None (H1 בלי הדר '#' => אין מה להסיק, לא מנחשים).
    ‏search (לא match): ה-'#' לא תמיד ממש בתחילת הטקסט (ר' _H1_SUB_RE)."""
    if not text:
        return None
    text = text.lstrip()
    parts = []
    m = _H1_SUB_RE.search(text)
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


# --- פרסרים נוספים שנבנו מקליטה אמיתית (labels C1/16/1L/A3, לא מתועדים ב-ARINC) --

# Loadsheet אלקטרוני (label C1): מגיע בבלוקים נפרדים (multi-block, msgno D57A/B/C...) —
# כל בלוק מחלץ מה שיש בו; \b לפני הקיצור מונע התאמה בתוך "MACZFW"/"LIZFW"/"MACTOW".
# ⚠ רגרסיה אמיתית שתוקנה: (1) \d+ בלבד קטע משקלים עשרוניים (חלק מה-loadsheets
# מדווחים ב-XX.X); (2) "kg" הודבק בכוח בקוד הישן למרות שהטקסט האמיתי (ר'
# test_parse_loadsheet_real_capture) לא נושא שום יחידה בכלל — carrier אמריקאי
# שמדווח ב-LB היה מקבל תווית "kg" שגויה. עכשיו קולטים יחידה אופציונלית מהטקסט
# עצמו (KG/LB/LBS) ומציגים אותה *רק* כשהיא באמת שם — בלי יחידה בקלט = בלי
# יחידה בפלט (§12). ⚠ רגרסיה שנמצאה בסבב-בדיקה עצמאי: \b *מחוץ* לקבוצת
# היחידה (כלומר על השרשרת כולה) נכשל כשמספר מוצמד-בלי-רווח לסיומת לא-מוכרת
# ("62000KGS"/"62000K") — הקבוצה נסוגה לריקה, אבל ה-\b הסוגר עדיין נבדק מול
# התו הבא (עדיין אות), אז כל ההתאמה נכשלת ו-ZFW/TOW/TOF שלמים אבדו (בעוד
# הקוד הישן, בלי יחידה בכלל, כן היה תופס את המספר). ה-\b עבר *לתוך* קבוצת
# היחידה (מותנה בה בלבד) כדי שסיומת לא-מוכרת רק תדיר את היחידה, לא את המספר.
_LOADSHEET_UNIT = r"(\d+(?:\.\d+)?)(?:\s*(KG|LBS?)\b)?"
_LOADSHEET_ZFW_RE = re.compile(r"\bZFW\s+" + _LOADSHEET_UNIT)
_LOADSHEET_TOW_RE = re.compile(r"\bTOW\s+" + _LOADSHEET_UNIT)
_LOADSHEET_TOF_RE = re.compile(r"\bTOF\s+" + _LOADSHEET_UNIT)
_LOADSHEET_PAX_RE = re.compile(r"\bCREW\s+(\d+)/(\d+)\s+PAX\s+(\d+)")
_LOADSHEET_TTL_RE = re.compile(r"\bTTL\s+(\d+)")


def _parse_loadsheet(text):
    """Loadsheet אלקטרוני (label C1, 'LOADSHEET FINAL'): משקל המראה (ZFW/TOW/TOF)
    ונוסעים/צוות. best-effort — כל בלוק מציג רק את מה שהוא נושא."""
    if not text or "LOADSHEET" not in text:
        return None
    parts = []
    for name, rx in (("ZFW", _LOADSHEET_ZFW_RE), ("TOW", _LOADSHEET_TOW_RE),
                     ("TOF", _LOADSHEET_TOF_RE)):
        m = rx.search(text)
        if m:
            parts.append(f"{name} {m.group(1)}{m.group(2) or ''}")
    m = _LOADSHEET_PAX_RE.search(text)
    if m:
        parts.append(f"נוסעים {m.group(3)} · צוות {m.group(1)}/{m.group(2)}")
    m = _LOADSHEET_TTL_RE.search(text)
    if m:
        parts.append(f'סה"כ {m.group(1)}')
    return " · ".join(parts) if parts else None


# דיווח מיקום עשרוני (label 16, לא מתועד רשמית ב-ARINC 620): נצפה בקליטה אמיתית —
# 'WPT ,N dd.ddd,E ddd.ddd,ALT,...\TS hhmmss,ddmmyy'. שדות באמצע (בין alt ל-\TS)
# לא ברורים דיים כדי לתייג (לא מנחשים) — מחלצים רק waypoint+נ"צ+גובה.
_L16_RE = re.compile(
    r"^([A-Z0-9\-]{2,8})\s*,([NS])\s*([\d.]+),([EW])\s*([\d.]+),(\d{4,5})")


def _parse_label16(text):
    """label 16: נ"צ עשרוני + גובה. פחות נוקשה-פורמט מ-/.POS//label15 (שדות
    באורך משתנה) => לא נחלץ עם error (בניגוד לפורמטים המבניים ה-DDMM)."""
    if not text:
        return None
    m = _L16_RE.match(text.strip())
    if not m:
        return None
    wpt, ns, la, ew, lo, alt = m.groups()
    try:
        lat, lon = float(la), float(lo)
    except ValueError:
        return None
    if ns == "S":
        lat = -lat
    if ew == "W":
        lon = -lon
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return None
    return round(lat, 5), round(lon, 5), f"WPT {wpt.strip()} · {int(alt)}ft"


# דוח ניווט/דלק (label 1L, לא מתועד רשמית): נ"צ עשרוני + UTC/דלק/גובה/מהירות/ETA.
# עוגן ארוך וספציפי (7 שדות ברצף קבוע) => מבני מספיק לחילוץ גם עם error, כמו /.POS/.
_NAV_FUEL_RE = re.compile(
    r"\bN\s*([\d.]+)/E\s*([\d.]+)/UTC\s*(\d{4})/FOB\s+([\d.]+)/"
    r"ALT\s+(\d+)/CAS\s+([\d.]+)/ETA\s+(\d{4})")


def _parse_nav_fuel(text):
    """label 1L: נ"צ עשרוני + UTC/דלק(טון)/גובה/מהירות/ETA. מדגם מצומצם בקליטה
    שלנו — לא כל הודעות 1L תואמות (יש גם וריאנט קצר בלי נ"צ, שנופל ל-None כאן)."""
    if not text:
        return None
    m = _NAV_FUEL_RE.search(text)
    if not m:
        return None
    la, lo, utc, fob, alt, cas, eta = m.groups()
    try:
        lat, lon = float(la), float(lo)
    except ValueError:
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return None
    # ⚠ רגרסיה אמיתית: "t" (טונות) הודבק בעבר בכוח על FOB למרות שהטקסט לא נושא
    # שום יחידה עבורו (בניגוד ל-ALT/CAS, ששם ft/kt הן מוסכמות תעופה אוניברסליות
    # כמעט-בלי-חלופה — לא כך דלק, שיכול להיות מדווח ב-kg/lb/t לפי חברה/מטוס).
    # §12: לא ממציאים יחידה שלא הייתה בקלט.
    decoded = (f"UTC {utc[:2]}:{utc[2:]}z · דלק {fob} · {alt}ft · "
               f"CAS {cas}kt · ETA {eta[:2]}:{eta[2:]}z")
    return round(lat, 5), round(lon, 5), decoded


# PDC — Pre-Departure Clearance (label A3): אישור טרום-המראה מלא בטקסט חופשי.
# מילות-המפתח (CLRD TO/OFF/VIA/SQUAWK/NEXT FREQ/CLIMB INIT ALT) הן סטנדרט תעשייתי
# (FAA/EUROCONTROL DCL) ולא ספציפיות לחברה — אך מדגם יחיד בקליטה שלנו, best-effort.
_PDC_DEST_RE = re.compile(r"\bCLRD TO ([A-Z]{4})\b")
_PDC_RWY_RE = re.compile(r"\bOFF (\d{1,2}[LRC]?)\b")
_PDC_SID_RE = re.compile(r"\bVIA ([A-Z0-9]{2,8})\b")
_PDC_SQUAWK_RE = re.compile(r"\bSQUAWK (\d{4})\b")
_PDC_FREQ_RE = re.compile(r"\bNEXT FREQ ([\d.]+)")
_PDC_CLIMB_RE = re.compile(r"\bCLIMB INIT ALT (\d+)")


def _parse_pdc(text):
    """PDC (label A3): יעד/מסלול-המראה/SID/סקוואק/תדר הבא/גובה טיפוס ראשוני —
    כל שדה אופציונלי, מוצגים רק אלה שנמצאו."""
    if not text:
        return None
    parts = []
    m = _PDC_DEST_RE.search(text)
    if m:
        parts.append(f"ל-{m.group(1)}")
    m = _PDC_RWY_RE.search(text)
    if m:
        parts.append(f"המראה {m.group(1)}")
    m = _PDC_SID_RE.search(text)
    if m:
        parts.append(f"SID {m.group(1)}")
    m = _PDC_SQUAWK_RE.search(text)
    if m:
        parts.append(f"Squawk {m.group(1)}")
    m = _PDC_FREQ_RE.search(text)
    if m:
        parts.append(f"תדר הבא {m.group(1)}")
    m = _PDC_CLIMB_RE.search(text)
    if m:
        parts.append(f"טפס ל-{m.group(1)}ft")
    return "אישור טרום-המראה: " + " · ".join(parts) if parts else None


_INTEREST_LABELS = {"A3", "C1", "15", "16", "1L"}   # labels שידועים כבעלי תוכן עשיר (PDC/loadsheet/מיקום/ניווט)


def _interest_score(rec):
    """'מעניינת' = שווה תשומת לב מעבר לרעש התפעולי (ACK ריקים/link test/squitter
    חוזרים) — לא ציון מספרי מומצא, קריטריונים בינאריים מתוך שדות שכבר קיימים
    בכרטיס המנורמל (ר' docs/field-station-roadmap.md, §1 "האנליסט"). כל אחד
    מהם לבדו מספיק: קטגוריה לא-גנרית (לא comm/text), יש טקסט מפוענח, מיקום
    מ-ADS-C (איכות המיקום הגבוהה ביותר), או label שידוע כבעל תוכן עשיר."""
    if rec.get("group") not in (None, "comm", "text"):
        return True
    if rec.get("decoded"):
        return True
    if rec.get("pos_src") == "adsc":
        return True
    if rec.get("label") in _INTEREST_LABELS:
        return True
    return False


def _normalize_acars(m):
    """מצמצם הודעת acarsdec JSON לשדות שה-UI מציג, בפורמט *אחיד* לכל סוגי ההודעות:
    קטגוריה קריאה (label => תיאור), קבוצה (לצבע), ומיקום (lat/lon) כשזמין. עמיד
    לשדות חסרים (הרבה הודעות ACARS הן ACK ריק בלי tail/flight/text).
    מדדי איכות קליטה: "level" (dBFS) מגיע ישירות מהמפענח — נשמר כמות שהוא, בלי
    עיבוד. "snr" מחושב רק כש-"noise" (רצפת רעש) קיים בקלט — acarsdec עצמו *לא*
    מספק רצפת רעש לכל הודעה (בניגוד ל-dumpvdl2), אז בהודעות ACARS אמיתיות snr
    יהיה None תמיד; רק VDL2 (מסלול A, שמזרים raw דרך הפונקציה הזו) מזין "noise"
    ומקבל SNR אמיתי. לעולם לא מעריכים ערך משוער — אם אין נתון אמין, השדה חסר."""
    def g(*keys):
        for k in keys:
            v = m.get(k)
            if v not in (None, ""):
                return v
        return None

    level = g("level")
    noise = g("noise")
    snr = round(level - noise, 1) if (level is not None and noise is not None) else None

    text = g("text")
    if isinstance(text, str):
        text = text.replace("\r\n", "\n").replace("\r", "\n").strip()

    label = g("label")
    desc, group = ACARS_LABELS.get(label, (None, "text")) if label else (None, "comm")
    category = desc or (f"Label {label}" if label else "הודעה")

    # תג-tail שנראה כמו כתובת תחנת-קרקע (‎.TCARC/‎.CNTMM וכו', אותו דפוס בדיוק
    # כמו _UPLINK_HEADER_RE שכבר משמש לזיהוי הדר-ניתוב בטקסט) לא יכול לקבל
    # מיקום מ-heuristic טקסטואלי: תחנת קרקע לא "טסה", וכל נ"צ שיימצא בהודעה
    # שלה (למשל בתוך תוכן שהיא משדרת/מעבירה) הוא לא מיקומה. מגביל *רק* את
    # הנתיבים ה-heuristic (text_latlon/label16) — הנתיבים המבניים (/.POS/,
    # label15, ADS-C) לא רלוונטיים לתחנת-קרקע מלכתחילה (אלה פורמטים שרק
    # מטוס-בפועל משדר), אז אין צורך לגדר אותם.
    # ⚠ רגרסיה אמיתית: acarsdec מרפד גם רישומי מטוס קצרים בנקודה מובילה עד
    # לאורך קבוע (ר' adsb.norm_reg: ‎'.4X-EHD') — אותו דפוס *בדיוק* כמו כתובת
    # תחנת-קרקע. בלי הגבלה נוספת, _UPLINK_HEADER_RE לבד תפס גם רישומים אמיתיים
    # שמתחילים באות ומרופדים בנקודה (למשל ‎.N806NN האמריקאי, ‎.JA801A היפני,
    # ‎.HL7783 הקוריאני) ודיכא את מיקומם האמיתי — 4X-/C-/G- לא נפגעו כי המקף
    # שבהם שובר את הרצף התווי הנדרש ע"י הרג'קס (ר' ‎.4X-EHD/‎.C-GHKX למטה),
    # ולכן זה לא נתפס בקליטה המקומית. ההבחנה שכן שורדת: כתובות תחנת-קרקע
    # שנצפו בפועל (‎.TCARC/‎.CNTMM) הן אותיות בלבד בלי ספרה, בעוד שרישומי מטוס
    # אמיתיים כמעט תמיד מכילים ספרה — לכן ספרה כלשהי בתג פוסלת את הסיווג
    # כתחנת-קרקע, גם אם הצורה הכללית תואמת את הרג'קס.
    tail_val = g("tail", "registration")
    tail_is_station = (bool(tail_val) and bool(_UPLINK_HEADER_RE.match(str(tail_val)))
                       and not any(ch.isdigit() for ch in str(tail_val)))

    # פענוח ARINC-622 (libacars): kind => badge וקבוצה, וטקסט קריא אם יש.
    lat = lon = pos_src = decoded = None
    libacars = m.get("libacars")
    if libacars:
        kind, dtext, decode_failed = _libacars_decode(libacars)
        category = kind
        # ⚠ עדיפות ה-decoded: (1) טקסט אנושי מ-re-decode מקומי בכיוון הנכון
        # (_libacars_text, ר' _decode_libacars_app) — הוא היחיד שמפיק משפטים
        # קריאים כמו "CONFIRM ASSIGNED ROUTE"; ה-JSON שממנו חושב kind/decode_failed
        # למעלה לא מכיל אותם (ADS-C: רק מפתחות מספריים; CPDLC: תחת "choice_label"
        # ש-_libacars_decode לא קוצר). (2) dtext — מה ש-_libacars_decode עצמו כבר
        # חילץ מה-JSON (בעיקר למקרים שאין בהם re-decode, כמו arinc622 גנרי).
        # (3) הודעת-כישלון מפורשת, רק אם decode_failed. "לא ניסינו" ≠ "ניסינו
        # ונכשלנו" — לא ממציאים תוכן, אבל *כן* אומרים למשתמש שהיה ניסיון.
        lib_text = m.get("_libacars_text")
        decoded = lib_text or dtext or (
            "לא פוענח — libacars החזיר שגיאת פענוח; הטקסט הגולמי נשמר"
            if decode_failed else None)
        # ⚠ "position" רק כשיהיה בפועל lat/lon (ר' ההערה למטה) — אחרת כרטיס
        # ADS-C-שנכשל-פענוח היה מסונן תחת "📍 מיקום" ב-UI בלי שום מיקום אמיתי.
        # CPDLC נשאר "clearance" גם בלי decoded — סוג ההודעה ידוע מהמעטפת עצמה,
        # רק התוכן נכשל (בניגוד למיקום, שם "position" *הוא* טענת-תוכן).
        # ⚠ ADS-C: אותו tag מספרי פירושו *הפוך* לגמרי לפי כיוון ההודעה (מאומת
        # ישירות מ-libacars/adsc.c במקור: la_adsc_uplink_tag_descriptor_table
        # מול la_adsc_downlink_tag_descriptor_table — tag 7 = "Periodic contract
        # request" ב-uplink (קרקע→מטוס, בקשה — *אין* בה מיקום מטוס) מול "Basic
        # report" ב-downlink (מטוס→קרקע, דיווח מיקום אמיתי). אם המפענח (או
        # קריאה שגויה אליו) יישם את טבלת-הכיוון הלא-נכונה, הפענוח "מצליח" מבחינה
        # מבנית (בלי err) אבל שולף נ"צ מבייטים שהם בכלל פרמטרי-בקשה, לא מיקום —
        # ‏decode_failed=False לא עוזר כאן, כי אין שום שגיאה שהתגלתה. נצפה בפועל:
        # A7-BBB (dir=uplink!) "קיבל" 18.34/2.11 באותה קליטה בדיוק שבה C-GHKX
        # (גם uplink) קיבל 5.69/2.11 — שני "מיקומים" ממסרים-בקשה, לא ממטוסים.
        # ‏structural_dir מגיע מ-`_structural_dir` שהקורא (SATCOM/VDL2) ממלא
        # *לפני* הקריאה הזו מתוך src/dst.type המבני (לא heuristic) — key חסר
        # (ACARS רגיל, שלא צפוי להפיק ADS-C בכלל — ר' §12) לא חוסם, כדי לא
        # לשנות התנהגות קיימת שם; אבל "uplink" מפורש כן חוסם תמיד.
        structural_dir = m.get("_structural_dir")
        adsc_dir_ok = structural_dir != "uplink"
        group = ("clearance" if kind == "CPDLC"
                 else "position" if (kind == "ADS-C" and not decode_failed and adsc_dir_ok)
                 else group)
        # מיקום *רק* מ-ADS-C דו-הגנתי: (1) decode_failed=True לא מנע בעבר
        # מ-_scan_latlon לרוץ בכל זאת — סריקה רקורסיבית על מבנה שהמפענח סימן
        # כ"נכשל" עלולה לתפוס שריד-מפענוח-חלקי בשם lat/lon (נצפה: C-GHKX קיבל
        # 5.69,2.11 באלפי ק"מ מהאמיתי, על הודעה עם decoded="לא פוענח"). (2)
        # כיוון שגוי (למעלה) — CPDLC (או ARINC-622 גנרי אחר) עלול גם הוא לשאת
        # נ"צ מוטבע (waypoint ב-clearance) שאינו מיקום המטוס — לכן גם kind
        # מסונן ל-ADS-C בלבד, לא כל libacars (אותה הגנה כמו VDL2 מסלול B).
        if kind == "ADS-C" and not decode_failed and adsc_dir_ok:
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

    # label 1L (דוח ניווט/דלק): עוגן ארוך וספציפי (7 שדות ברצף) => מבני כמו /.POS/.
    if lat is None and label == "1L" and text:
        pos = _parse_nav_fuel(text)
        if pos:
            lat, lon, pos_src = pos[0], pos[1], "nav-fuel"
            if decoded is None:
                decoded = pos[2]

    # נפילה: מיקום מקודד בטקסט חופשי — אבל *רק* מ-frame נקי. acarsdec error>0 = ביטים
    # שתוקנו/לא-תוקנו; ספרה אחת שהתהפכה בקואורדינטה => מטוס במקום שגוי על המפה. ADS-C
    # (libacars) לעיל מוגן-CRC ולכן נשמר גם עם error; ה-heuristic הטקסטואלי לא — לכן מגודר.
    # ‏tail_is_station: תחנת-קרקע (ר' למעלה) לא מקבלת מיקום מ-heuristic טקסטואלי בכלל.
    if lat is None and not m.get("error") and not tail_is_station:
        pos = _text_latlon(text)
        if pos:
            lat, lon, pos_src = pos[0], pos[1], "text"

    # label 16 (דיווח מיקום עשרוני): פורמט פחות נוקשה מ-DDMM המבני => מגודר כמו heuristic.
    if lat is None and label == "16" and text and not m.get("error") and not tail_is_station:
        pos = _parse_label16(text)
        if pos:
            lat, lon, pos_src = pos[0], pos[1], "label16"
            if decoded is None:
                decoded = pos[2]

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
        elif label == "C1":
            decoded = _parse_loadsheet(text)
        elif label == "A3":
            decoded = _parse_pdc(text)

    rec = {
        "t": g("timestamp") or time.time(),   # epoch seconds (float) מ-acarsdec (חסר => עכשיו)
        "freq": g("freq"),                    # MHz
        "level": level,                       # dBFS — מקורי מהמפענח, לא מעובד
        "snr": snr,                           # dB — רק כש-noise זמין (ר' docstring); אחרת None
        "label": label,
        "category": category,                 # תיאור קריא אחיד (label/ARINC-622)
        "group": group,                       # קבוצה לצבע ב-UI / עמודה בייצוא
        "tail": tail_val,
        "flight": g("flight", "fid"),
        "mode": g("mode"),
        "msgno": g("msgno"),
        "dir": _acars_direction(label, text),  # "uplink" | "downlink" | None (best-effort)
        "lat": lat,
        "lon": lon,
        "pos_src": pos_src,                   # "adsc" | "pos-report" | "label15" | "nav-fuel"
                                               # | "label16" | "text" | None
        "decoded": decoded,                   # טקסט מפוענח קצר (CPDLC/ATIS/OOOI וכו') או None
        "text": text,
        "error": m.get("error"),
        "actype": _extract_actype(label, text),  # סוג מטוס best-effort (H1/C1) או None
        # מוזרם החוצה (לא רק שדה-עזר פנימי) כדי ש-_aircraft_identity/_build_roster
        # לא יתייחסו לכתובת תחנת-קרקע כאילו היא רישום מטוס — בלעדיו הרוסטר
        # המאוחד הציג בעבר תחנות קרקע (SQ squitters וכו') כשורות "מטוס" משלהן.
        "tail_is_station": tail_is_station,
    }
    rec["notable"] = _interest_score(rec)     # לדוח הסשן + סינון/התראות ב-UI (ר' §1 "האנליסט")
    return rec


def _jsonl_records(lines):
    """מפרסר שורות JSONL לרשומות — **רק אובייקטים**. משותף לכל הקוראים
    (ייצוא, ארכיון, וטעינת ההיסטוריה באתחול).

    ⚠ למה בדיקת ה-dict קריטית ולא קוסמטית: הקוראים ניגשים מיד ל-`r.get("t")`.
    שורה שהיא JSON *תקין* אך אינה אובייקט (‏`null`, ‏`0`, מחרוזת, מערך — שריד
    אפשרי של בלוק פגום אחרי כיבוי פתאומי, בדיוק התרחיש ש-_atomic_write נבנה
    בשבילו) הפילה את הקוראים ב-AttributeError. ‏json.loads הצליח עליה, ולכן
    ה-`except ValueError` הקיים *לא* תפס אותה.
    החומרה: `_load_*_history` נקראות ב-__main__ **בלי try** לפני `app.run()`,
    כך ששורה אחת כזאת מנעה מ-airam-web לעלות בכלל — וזה המתזמר שמשחזר את מצב
    ה-SDR (‏`_boot_restore`). עם `Restart=always` זו לולאת קריסה: התחנה כולה
    מתה, ובשטח אין SSH כדי לאבחן (ר' §9/§12 ב-CLAUDE.md)."""
    out = []
    for ln in lines:
        try:
            rec = json.loads(ln)
        except ValueError:
            continue                          # שורה פגומה (כתיבה חלקית) => דילוג
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _append_jsonl_log(path, rec):
    """מוסיף הודעה מנורמלת לקובץ JSONL (append; thread ה-listener הוא הכותב היחיד).
    נכשל בשקט (דיסק מלא וכו') => הפיד החי ממשיך לפעול. משותף ל-ACARS ול-VDL2."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        log.exception("jsonl log append (%s)", path)


def _trim_jsonl_log(path, keep):
    """קיצוץ ל-keep שורות (rewrite אטומי). נקרא מדי פעם מ-thread ה-listener
    (הכותב היחיד => אין מרוץ). קוראים (ייצוא) סובלים שורה אחרונה חלקית."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    if len(lines) > keep:
        _atomic_write(path, "\n".join(lines[-keep:]) + "\n")


def _append_acars_log(rec):
    _append_jsonl_log(ACARS_LOG_PATH, rec)


def _trim_acars_log():
    _trim_jsonl_log(ACARS_LOG_PATH, ACARS_LOG_KEEP)


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


def _load_acars_history():
    """טוען את זנב acars.jsonl ל-ring buffer בעלייה => הודעות *היום* שורדות restart,
    ממוינות לפי זמן (t עולה) עם id רץ. נקרא *לפני* הפעלת thread ה-listener (אין מרוץ).
    רק הודעות מהיום נטענות לזיכרון (ההיסטוריה המלאה נשמרת בדיסק)."""
    global _acars_seq
    try:
        lines = ACARS_LOG_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    recs = _jsonl_records(lines[-ACARS_BUF_MAX:])
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
        try:
            rec = _normalize_acars(msg)
        except Exception:
            # שדה עם טיפוס בלתי-צפוי (label כרשימה, level כמחרוזת וכו') לא יפיל
            # את ה-thread לצמיתות — הפיד ימשיך לזרום להודעות הבאות.
            log.exception("ACARS: נרמול נכשל על דאטהגרם — מדולג")
            continue

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


# --- VDL2: נרמול, התמדה ו-listener ------------------------------------------
# סכמת dumpvdl2 v2.6.0 (אומתה מהמקור): ‏{"vdl2": {"t": {"sec","usec"}, "freq" (Hz),
# "sig_level", "avlc": {"src"/"dst": {"addr","type","status"}, "frame_type",
# "acars": {err,crc_ok,reg,mode,label,blk_id,ack,flight,msg_num,msg_num_seq,msg_text,
#           + יישומים מפוענחים *מקוננים בפנים* (arinc622/adsc/cpdlc/miam...)},
# או "xid": {type,type_descr,...} או "x25": {pkt_type_name, + clnp/cotp מקוננים}}}
_VDL2_ACARS_FIELDS = frozenset({
    "err", "crc_ok", "more", "reg", "mode", "label", "blk_id", "ack",
    "flight", "msg_num", "msg_num_seq", "sublabel", "mfi", "msg_text",
})


def _normalize_vdl2(m):
    """ממיר פריים dumpvdl2 JSON לאותה סכמת כרטיס אחידה של _normalize_acars, בתוספת
    שדה icao (כתובת ICAO 24-bit של צד-המטוס — זהות לפריימים בלי tail). מחזיר None
    לפריים שאינו בר-הצגה (בלי שכבת AVLC). שני מסלולים:
      A. ‏avlc.acars קיים => מסנתזים dict בסגנון acarsdec ומזרימים דרך _normalize_acars
         — כל הפרסרים (ATIS/OOOI/PDC/15/16/1L/H1...) והקטגוריות חלים כמות שהם.
      B. אחרת => כרטיס גנרי בסיסי: CPDLC/ADS-C (תקציר libacars) / XID / X.25."""
    v = m.get("vdl2")
    if not isinstance(v, dict):
        return None
    avlc = v.get("avlc")
    if not isinstance(avlc, dict):
        return None                           # פריים בלי AVLC (שגיאת פענוח) => מדלגים

    t_obj = v.get("t") or {}
    try:
        t = float(t_obj.get("sec") or 0) + float(t_obj.get("usec") or 0) / 1e6
    except (TypeError, ValueError):
        t = 0
    t = t or time.time()
    try:
        freq_mhz = round(float(v.get("freq")) / 1e6, 3) if v.get("freq") else None
    except (TypeError, ValueError):
        freq_mhz = None
    level = v.get("sig_level")             # dBFS — מקורי מהמפענח
    noise = v.get("noise_level")           # dBFS — רצפת רעש; dumpvdl2 מודד בעצמו (בניגוד ל-acarsdec)
    snr = round(level - noise, 1) if (level is not None and noise is not None) else None

    # זהות + כיוון מבניים משכבת ה-AVLC: src=Aircraft => downlink (עובדה פיזית,
    # אמינה יותר מכל heuristic של label/טקסט => דורסת את _acars_direction בסוף).
    src, dst = avlc.get("src") or {}, avlc.get("dst") or {}
    icao = direction = None
    if str(src.get("type") or "").lower() == "aircraft":
        icao, direction = src.get("addr"), "downlink"
    elif str(dst.get("type") or "").lower() == "aircraft":
        icao = dst.get("addr")
        if str(src.get("type") or "").lower().startswith("ground"):
            direction = "uplink"
    icao = str(icao).upper() if icao else None

    acars = avlc.get("acars")
    if isinstance(acars, dict):
        # מסלול A: יישומים מפוענחים (arinc622 וכו') מקוננים בתוך אובייקט ה-acars
        # (libacars סוגר את ההורה אחרי הצאצא) => כל מפתח מבני לא-מוכר הוא יישום.
        apps = {k: val for k, val in acars.items()
                if k not in _VDL2_ACARS_FIELDS and isinstance(val, (dict, list))}
        raw = {
            "timestamp": t,
            "freq": freq_mhz,
            "level": level,
            "noise": noise,                   # מוזן ל-_normalize_acars => snr אמיתי (לא הודעה משוערת)
            "mode": acars.get("mode"),
            "label": acars.get("label"),
            "tail": acars.get("reg"),         # יתכן '.' מוביל — כמו acarsdec (norm_reg מטפל)
            "flight": acars.get("flight"),
            # msg_num/msg_num_seq עלולים להגיע כ-int (dumpvdl2 לא מבטיח str) —
            # str() לפני החיבור, אחרת TypeError מפיל את כל הפריים ב-_vdl2_listener.
            "msgno": (str(acars.get("msg_num") or "") + str(acars.get("msg_num_seq") or "")) or None,
            "text": acars.get("msg_text"),
            # err=פריים פגום / crc_ok=False => כמו acarsdec error>0 (מגדר heuristics של טקסט)
            "error": 0 if (not acars.get("err") and acars.get("crc_ok", True)) else 1,
            # ⚠ בלי זה, חסם ה-ADS-C-uplink (adsc_dir_ok ב-_normalize_acars) לא
            # חל בכלל במסלול הזה — הוא היחיד מבין שלושת המסלולים (כאן/VDL2
            # מסלול B/SATCOM) שלא העביר את הכיוון המבני, בדיוק המסלול הכי סביר
            # לשאת ADS-C אמיתי (ACARS-over-AVLC). אותו קלאס-באג שתועד ותוקן
            # פעמיים ב-§12 — כאן פשוט לא יושם על המסלול השלישי.
            "_structural_dir": direction,
        }
        if apps:
            raw["libacars"] = apps
        card = _normalize_acars(raw)
    else:
        # מסלול B: כרטיס גנרי (החלטת עיצוב: בלי פרסרים ייעודיים ל-ATN בשלב זה)
        category, group, decoded = "VDL2", "comm", None
        lat = lon = pos_src = None
        x25, xid = avlc.get("x25"), avlc.get("xid")
        if isinstance(x25, dict):
            blob = json.dumps(x25, ensure_ascii=False).lower()
            is_adsc = "adsc" in blob or "ads-c" in blob
            # תקציר טקסט קריא אם קיים במבנה; decode_failed => אותה הבחנה כמו ב-SATCOM
            # (ר' _libacars_decode) — "ניסינו ונכשלנו" מול "אין נתון" בכלל. מחושב
            # *לפני* קביעת group כדי ש-"position" יינתן רק כשיהיה בפועל lat/lon
            # (ר' ההערה למטה + המקבילה ב-_normalize_acars) — אחרת כרטיס ADS-C
            # שנכשל פענוח היה מסונן תחת "📍 מיקום" בלי שום מיקום אמיתי.
            _, dtext, decode_failed = _libacars_decode(x25)
            decoded = dtext if dtext else (
                "לא פוענח — libacars החזיר שגיאת פענוח; הטקסט הגולמי נשמר"
                if decode_failed else None)
            # ⚠ tag ADS-C מספרי פירושו הפוך לגמרי בין uplink/downlink ב-libacars
            # (מאומת מ-adsc.c: la_adsc_uplink_tag_descriptor_table מול
            # ...downlink...; tag 7 = "בקשה" ב-uplink מול "דיווח מיקום אמיתי"
            # ב-downlink — ר' ההערה המלאה ב-_normalize_acars ליד adsc_dir_ok).
            # ‏direction כאן כבר חושב למעלה (AVLC src/dst.type, עובדה מבנית) —
            # לא heuristic, ולא תלוי ב-decode_failed (שם אין שום err שמתגלה).
            adsc_dir_ok = direction != "uplink"
            if "cpdlc" in blob:
                category, group = "CPDLC (VDL2)", "clearance"
            elif is_adsc:
                category = "ADS-C (VDL2)"
                group = "position" if (not decode_failed and adsc_dir_ok) else group
            else:
                category = "VDL2 · X.25"
            # מיקום *רק* מ-ADS-C: CPDLC עלול לשאת נ"צ מוטבע (waypoint ב-clearance)
            # שאינו מיקום המטוס עצמו — לא מייחסים אותו כמיקום כדי לא להטעות במפה.
            # ⚠ רגרסיה אמיתית: is_adsc הוא בדיקת-substring גולמית על *כל* ה-blob
            # (לא exclusive מול "cpdlc" בו) — בלוק ה-if/elif למעלה כן exclusive
            # (cpdlc זוכה קודם), אבל הבדיקה כאן חזרה על is_adsc הגולמי במקום על
            # התוצאה שכבר הוכרעה, אז הודעת CPDLC שגם מכילה את המחרוזת "adsc"
            # במקום כלשהו (למשל free_text שמזכיר ADSC, או waypoint מקונן) עדיין
            # עברה את התנאי הזה וייחסה נ"צ-clearance כמיקום מטוס — גם כש-category
            # כבר "CPDLC (VDL2)". גייטינג לפי category (לא is_adsc הגולמי) מחזיר
            # את ה-exclusivity: מיקום רק כשההודעה *בפועל* סווגה כ-ADS-C למעלה.
            # ⚠ אותה הגנה כמו SATCOM (ר' ההערה המקבילה ב-_normalize_acars): CRC
            # תקין ב-AVLC ≠ פענוח-יישום מוצלח — decode_failed=True חוסם גם כאן,
            # וכיוון שגוי (adsc_dir_ok) חוסם גם כשאין שום err (ר' ההערה למעלה).
            if category == "ADS-C (VDL2)" and not decode_failed and adsc_dir_ok:
                pos = _scan_latlon(x25)          # מוגן-CRC בשכבת AVLC + decode_failed + כיוון
                if pos:
                    lat, lon, pos_src, group = pos[0], pos[1], "adsc", "position"
        elif isinstance(xid, dict):
            category = "VDL2 · XID (ניהול קישור)"
            decoded = xid.get("type_descr") or xid.get("type")
        else:
            ft = avlc.get("frame_type")
            category = f"VDL2 · {ft}" if ft else "VDL2"
        card = {
            "t": t, "freq": freq_mhz, "level": level, "snr": snr,
            "label": None, "category": category, "group": group,
            "tail": None, "flight": None, "mode": None, "msgno": None,
            "dir": None, "lat": lat, "lon": lon, "pos_src": pos_src,
            "decoded": decoded, "text": None, "error": 0, "actype": None,
        }

    card["icao"] = icao
    if direction:
        card["dir"] = direction
    return card


def _append_vdl2_log(rec):
    _append_jsonl_log(VDL2_LOG_PATH, rec)


def _trim_vdl2_log():
    _trim_jsonl_log(VDL2_LOG_PATH, VDL2_LOG_KEEP)


def _load_vdl2_history():
    """טוען את זנב vdl2.jsonl ל-ring buffer בעלייה (היום בלבד, כמו ACARS).
    נקרא *לפני* הפעלת thread ה-listener (אין מרוץ)."""
    global _vdl2_seq
    try:
        lines = VDL2_LOG_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    recs = _jsonl_records(lines[-VDL2_BUF_MAX:])
    floor = _today_start()
    recs = [r for r in recs if (r.get("t") or 0) >= floor]
    recs.sort(key=lambda r: r.get("t") or 0)
    with _vdl2_lock:
        for r in recs:
            _vdl2_seq += 1
            r["id"] = _vdl2_seq
            _vdl2_msgs.append(r)
    if recs:
        log.info("VDL2: נטענו %d הודעות מההיסטוריה", len(recs))


def _vdl2_listener():
    """thread רקע: מאזין ל-UDP מ-dumpvdl2, שומר ל-vdl2.jsonl ומכניס ל-ring buffer.
    רץ תמיד (גם כשהמצב אחר) — פשוט לא מגיעות דאטהגרמות כש-dumpvdl2 כבוי.
    dedup כמו ב-ACARS: זהות = tail או icao (לפריימים בלי רישום)."""
    global _vdl2_seq, _vdl2_drop_count
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((ACARS_UDP_HOST, VDL2_UDP_PORT))
    except OSError:
        log.warning("VDL2 listener: port %d busy - /api/vdl2 יחזיר ריק", VDL2_UDP_PORT)
        return
    seen = 0
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
        try:
            rec = _normalize_vdl2(msg)
        except Exception:
            # שדה עם טיפוס בלתי-צפוי לא יפיל את ה-thread לצמיתות (כמו ב-ACARS).
            log.exception("VDL2: נרמול נכשל על דאטהגרם — מדולג")
            continue
        if rec is None:
            # פריים לא בר-הצגה (בלי AVLC, סכמה לא תואמת וכו') — לוג תקופתי (לא רועש)
            # כדי להבדיל "אין תעבורה" מ"dumpvdl2 שינה סכמה" בלי לקרוא קוד.
            _vdl2_drop_count += 1
            if _vdl2_drop_count % 200 == 1:
                log.warning("VDL2: פריים לא זוהה (סכמה לא תואמת?) — %d עד כה", _vdl2_drop_count)
            continue

        ident = rec.get("tail") or rec.get("icao")
        text = rec.get("text") or ""
        ts = rec.get("t") or time.time()
        if ident and text:
            dedup_key = (ident, rec.get("label"), text[:80])
            prev_ts, prev_rec = _dedup.get(dedup_key, (0, None))
            if prev_rec is not None and ts - prev_ts < 90:
                with _vdl2_lock:              # prev_rec חי גם ב-_vdl2_msgs => מוטציה תחת נעילה
                    prev_rec["retry_count"] = prev_rec.get("retry_count", 1) + 1
                continue
            _dedup[dedup_key] = (ts, rec)
            if len(_dedup) > 500:
                cutoff = ts - 90
                for k in [k for k, (t0, _) in _dedup.items() if t0 < cutoff]:
                    del _dedup[k]

        _append_vdl2_log(rec)                 # התמדה לפני הקצאת id (הקובץ נקי מ-id)
        with _vdl2_lock:
            _vdl2_seq += 1
            rec["id"] = _vdl2_seq
            _vdl2_msgs.append(rec)
        seen += 1
        if seen % 200 == 0:
            _trim_vdl2_log()


# --- SATCOM: normalize + listener (מסלול יחיד, בניגוד ל-VDL2) ----------------
def _normalize_satcom(m):
    """ממיר הודעת inmarsat-sniffer JSON (סכמת JAERO JSONdump, כפי שנפלטת מ-
    feed_aero_message ב-inmarsat-sniffer/feed.c — אומתה מהמקור, *לא* מ-README)
    לאותה סכמת כרטיס אחידה של _normalize_acars. מסלול יחיד (לא A/B כמו VDL2):
    הכלי (במצב --mode=aero, היחיד הנתמך כרגע) מפיק *רק* הודעות ACARS מפוענחות.
    מחזיר None להודעה לא בת-הצגה (בלי isu.acars — למשל STD-C/EGC, שלא מופעל).
    ⚠ בשונה מ-acarsdec/dumpvdl2: אין level/noise/freq ברמת ההודעה (המפענח לא
    חושף אותם ב---feed/--udp) — level/snr תמיד None, בלי המצאת ערך (ר' §12
    ב-CLAUDE.md: "לעולם לא ממציאים ערך"). מיקום מגיע רק מטקסט ההודעה (כמו
    ACARS רגיל) או מ-arinc622 (ADS-C) המקונן תחת isu.acars.arinc622 — כמו VDL2
    מסלול A, כך שכל הפרסרים הקיימים (כולל ADS-C) חלים בחינם. ⚠ בשונה מ-VDL2:
    inmarsat-sniffer עוטף שם מחדש את *כל* עץ ה-ACARS (main.c:889-897 מפעיל
    la_proto_tree_format_json על ה-tree המושרש בצומת ה-ACARS עצמו, לא רק
    ביישום המקונן) — כלומר isu.acars.arinc622 הוא בפועל
    {"acars": {mode/label/reg/msg_text/... שוב, "arinc622": {תוכן האמיתי}}},
    לא היישום ישירות. מפרקים שכבה אחת עם _VDL2_ACARS_FIELDS (כמו מסלול A של
    VDL2) לפני שמעבירים ל-_normalize_acars — אחרת _libacars_decode/_scan_latlon
    "מוצאים" את msg_text המשוכפל בתוך המעטפת כאילו הוא תוכן מפוענח.
    src/dst.type ("Aircraft Earth Station"/"Ground Earth Station") הם עובדה
    מבנית של הכלי (לא heuristic) => דורסים את _acars_direction, כמו ה-icao/dir
    המבניים של VDL2. ⚠ בניגוד להנחה קודמת: AES **כן** מטופל ע"י inmarsat-sniffer
    עצמו כ-hex זהה ל-ICAO (aircraft_db_lookup_by_aes מפרמט %06X ומחפש באותה
    טבלת icao_hex/tar1090-db — אומת מהמקור, aircraft_db.c/.h). אנחנו עדיין
    *לא* ממפים אותו ל-card["icao"] — לא כי הם "לא זהים", אלא כדי לא לבלבל את
    זהות הרוסטר עם מרחב-הכתובות של VDL2 (icao שם מגיע מ-AVLC אמיתי, לא-לוויני;
    ערבוב השניים תחת אותו מפתח roster היה יוצר זהויות-שווא)."""
    isu = m.get("isu")
    if not isinstance(isu, dict):
        return None
    acars = isu.get("acars")
    if not isinstance(acars, dict):
        return None
    t_obj = m.get("t") or {}
    try:
        t = float(t_obj.get("sec") or 0) + float(t_obj.get("usec") or 0) / 1e6
    except (TypeError, ValueError):
        t = 0
    t = t or time.time()
    # ⚠ מחושב *לפני* הקריאה ל-_normalize_acars (לא רק אחריה, כמו בעבר) — הכיוון
    # המבני חייב להיות ידוע ל-_normalize_acars *לפני* חילוץ מיקום מ-ADS-C, כי
    # tag 7 פירושו הפוך לגמרי בין uplink/downlink ב-libacars (ר' ההערה המלאה
    # ליד adsc_dir_ok ב-_normalize_acars). src/dst.type הם עובדה מבנית של הכלי.
    src_type = str((isu.get("src") or {}).get("type") or "").lower()
    dst_type = str((isu.get("dst") or {}).get("type") or "").lower()
    structural_dir = ("downlink" if "aircraft" in src_type
                      else "uplink" if "aircraft" in dst_type else None)
    raw = {
        "timestamp": t,
        "mode": acars.get("mode"),
        "label": acars.get("label"),
        "tail": acars.get("reg"),
        "flight": acars.get("flight"),
        "_structural_dir": structural_dir,   # ר' adsc_dir_ok ב-_normalize_acars
        # msgno (MSN של ACARS) לא נחשף ע"י inmarsat-sniffer: isu.refno/qno הם
        # מספרי-רצף של שכבת הלוויין (uint8), *לא* ה-MSN הקלאסי — לא ממפים אותם
        # ל-msgno כדי לא להטעות (ר' §12 ב-CLAUDE.md: לא מזייפים/ממפים-שגוי ערך).
        "text": acars.get("msg_text"),
        # ⚠ isu.acars החיצוני (feed_aero_message ב-feed.c) *לעולם* לא כולל
        # err/crc_ok — אומת מהמקור: הפונקציה בונה את ה-JSON ידנית בלי השדות
        # האלה בכלל. הגייט היחיד לפני feed הוא reasm_status+err (main.c:830) —
        # לא crc_ok (שדה נפרד ב-libacars, acars.c:31-32/299) — כלומר הודעה עם
        # CRC כושל עדיין יכולה להגיע. err/crc_ok *אמיתיים* קיימים רק במעטפת
        # הפנימית הכפולה (isu.acars.arinc622.acars, ר' למטה) וגם זה רק כשיש
        # יישום ARINC-622/ADS-C/CPDLC מקונן. לרוב הודעות הטקסט הרגילות (בלי
        # arinc622) אין לנו שום איתות CRC — error נשאר 0 (לא מומצא: פשוט לא ידוע).
        "error": 0,
    }
    # ⚠ re-decode מקומי בכיוון המבני הנכון (ר' התיעוד המלא ליד _decode_libacars_app) —
    # לפני שנוגעים בפענוח arinc622 שכבר הגיע (אולי שגוי-כיוון) מ-inmarsat-sniffer.
    # error (CRC המעטפת הפנימית, למטה) הוא ערוץ נפרד לגמרי מהצלחת פענוח היישום —
    # re-decode מוצלח מחליף רק את תוצאת ה-application decode, לעולם לא "מתקן" CRC כושל.
    corrected_json, corrected_text = _decode_libacars_app(
        acars.get("label"), acars.get("msg_text"), structural_dir)

    apps = acars.get("arinc622")
    if isinstance(apps, dict):
        # מפרקים את מעטפת ה-ACARS הכפולה (ר' התיעוד למעלה) — inner הוא היישום
        # המקונן האמיתי (ADS-C/CPDLC), לא ACARS שוב. אם הצורה לא כצפוי (שינוי
        # גרסה אצל inmarsat-sniffer) נופלים חזרה ל-apps כמות שהוא ולא קורסים.
        inner = apps.get("acars")
        if isinstance(inner, dict):
            # רק כאן יש err/crc_ok אמיתיים (הסריאליזציה הגנרית של libacars,
            # acars.c:299/560) — בדיוק אותה נוסחה כמו VDL2 מסלול A (app.py
            # למעלה, ליד _VDL2_ACARS_FIELDS).
            raw["error"] = 0 if (not inner.get("err") and inner.get("crc_ok", True)) else 1
        apps = ({k: v for k, v in inner.items()
                if k not in _VDL2_ACARS_FIELDS and isinstance(v, (dict, list))}
                if isinstance(inner, dict) else apps)
        if apps:
            raw["libacars"] = apps
    # עדיפות: re-decode מתוקן > הפענוח המקורי מ-inmarsat-sniffer > טקסט גולמי בלבד.
    # כשל ב-helper (בינארי חסר/timeout/JSON פגום) לא מאבד את הפענוח הישן שכבר יש.
    if corrected_json:
        raw["libacars"] = corrected_json
    if corrected_text:
        raw["_libacars_text"] = corrected_text
    card = _normalize_acars(raw)
    if structural_dir:      # כבר חושב למעלה, לפני הקריאה (ר' ההערה שם) — לא כפול
        card["dir"] = structural_dir
    return card


def _append_satcom_log(rec):
    _append_jsonl_log(SATCOM_LOG_PATH, rec)


def _trim_satcom_log():
    _trim_jsonl_log(SATCOM_LOG_PATH, SATCOM_LOG_KEEP)


def _load_satcom_history():
    """טוען את זנב satcom.jsonl ל-ring buffer בעלייה (היום בלבד, כמו ACARS/VDL2).
    נקרא *לפני* הפעלת thread ה-listener (אין מרוץ)."""
    global _satcom_seq
    try:
        lines = SATCOM_LOG_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    recs = _jsonl_records(lines[-SATCOM_BUF_MAX:])
    floor = _today_start()
    recs = [r for r in recs if (r.get("t") or 0) >= floor]
    recs.sort(key=lambda r: r.get("t") or 0)
    with _satcom_lock:
        for r in recs:
            _satcom_seq += 1
            r["id"] = _satcom_seq
            _satcom_msgs.append(r)
    if recs:
        log.info("SATCOM: נטענו %d הודעות מההיסטוריה", len(recs))


def _satcom_listener():
    """thread רקע: מאזין ל-UDP מ-inmarsat-sniffer (‎--udp=127.0.0.1:5558), שומר
    ל-satcom.jsonl ומכניס ל-ring buffer. רץ תמיד (גם כשהמצב אחר) — פשוט לא
    מגיעות דאטהגרמות כש-inmarsat-sniffer כבוי. dedup כמו ב-ACARS/VDL2: זהות =
    tail (רוב הודעות ה-ACARS הלוויני נושאות רישום, בניגוד ל-icao ב-VDL2)."""
    global _satcom_seq, _satcom_drop_count
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((ACARS_UDP_HOST, SATCOM_UDP_PORT))
    except OSError:
        log.warning("SATCOM listener: port %d busy - /api/satcom יחזיר ריק", SATCOM_UDP_PORT)
        return
    seen = 0
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
        try:
            rec = _normalize_satcom(msg)
        except Exception:
            # שדה עם טיפוס בלתי-צפוי לא יפיל את ה-thread לצמיתות (כמו ב-ACARS/VDL2)
            log.exception("SATCOM: נרמול נכשל על דאטהגרם — מדולג")
            continue
        if rec is None:
            _satcom_drop_count += 1
            if _satcom_drop_count % 200 == 1:
                log.warning("SATCOM: הודעה לא זוהתה (סכמה לא תואמת?) — %d עד כה", _satcom_drop_count)
            continue

        ident = rec.get("tail")
        text = rec.get("text") or ""
        ts = rec.get("t") or time.time()
        if ident and text:
            dedup_key = (ident, rec.get("label"), text[:80])
            prev_ts, prev_rec = _dedup.get(dedup_key, (0, None))
            if prev_rec is not None and ts - prev_ts < 90:
                with _satcom_lock:             # prev_rec חי גם ב-_satcom_msgs => מוטציה תחת נעילה
                    prev_rec["retry_count"] = prev_rec.get("retry_count", 1) + 1
                continue
            _dedup[dedup_key] = (ts, rec)
            if len(_dedup) > 500:
                cutoff = ts - 90
                for k in [k for k, (t0, _) in _dedup.items() if t0 < cutoff]:
                    del _dedup[k]

        _append_satcom_log(rec)               # התמדה לפני הקצאת id (הקובץ נקי מ-id)
        with _satcom_lock:
            _satcom_seq += 1
            rec["id"] = _satcom_seq
            _satcom_msgs.append(rec)
        seen += 1
        if seen % 200 == 0:
            _trim_satcom_log()


HEALTH_SERVICES = ("rtl_airband", "icecast2", "sdrplay",
                   "airam-acars", "airam-vdl2", "airam-satcom")


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
    airam-satcom, ו-reset-failed של airam-satcom בלבד — ר' _enter_satcom)."""
    return subprocess.run([*SUDO, "systemctl", action, service],
                          capture_output=True, text=True, timeout=timeout)


def _sanitize_freqs(freqs, default=None):
    """מסנן רשימת תדרים לערכים תקינים (MHz). נכתבים ל-env => חובה לוודא
    שאין הזרקה: רק ספרות ונקודה (אף ש-systemd מנתח בבטחה, שמירה על קלט נקי).
    ‏default => רשימת הנפילה כשלא נשאר כלום (ברירת מחדל: תדרי ה-ACARS)."""
    out = [str(f).strip() for f in (freqs or []) if _FREQ_RE.match(str(f).strip())]
    return out or list(default if default is not None else ACARS_FREQS_DEFAULT)


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
    return out[:1] or list(default if default is not None else SATCOM_FREQS_DEFAULT)


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
        return max(IFGR_MIN, min(IFGR_MAX, int(float(value))))
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
    if vals[0] not in SATCOM_SATELLITES:
        return "לוויין לא מוכר: %s (אפשרויות: %s)" % (vals[0], ", ".join(sorted(SATCOM_SATELLITES)))
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


def write_vdl2_env(freqs, ifgr=None, rfgr=None):
    """כותב /etc/airam/vdl2.env בפורמט EnvironmentFile של systemd. שים לב:
    ‏dumpvdl2 מקבל תדרים ב-*Hz* — ההמרה מ-MHz (הפורמט של state/UI) נעשית רק כאן.
    ‏VDL2_GAIN מכיל את הדגל *כולו* (או ריק): ‏$VDL2_GAIN לא-מצוטט ב-ExecStart נעלם
    לגמרי כשהערך ריק => ברירת המחדל היא AGC של הדרייבר (כמו rtl_airband בלי שורת gain).
    האפליקציה כותבת רק מחרוזת ריקה או ints מפורמטים => אין משטח הזרקה."""
    mhz = _sanitize_freqs(freqs, VDL2_FREQS_DEFAULT)
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
        "",
    ])
    _atomic_write(VDL2_ENV_PATH, text)


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
        "",
    ])
    _atomic_write(SATCOM_ENV_PATH, text)


def _enter_vdl2(freqs):
    """עוצר את שני צרכני ה-SDR האחרים ומריץ dumpvdl2. מחזיר (error, detail).
    Conflicts ב-unit עוצר אותם ממילא, אבל עוצרים מפורשות תחילה כדי לשחרר את
    ה-SDR לפני ש-dumpvdl2 פותח אותו (מונע מרוץ על המכשיר)."""
    for svc in ("rtl_airband", ACARS_SERVICE, SATCOM_SERVICE):
        try:
            _sysctl("stop", svc, timeout=30)
        except Exception:
            pass
    write_vdl2_env(freqs)
    try:
        r = _sysctl("restart", VDL2_SERVICE, timeout=45)
    except subprocess.TimeoutExpired:
        return "הפעלת VDL2 נתקעה — בדוק שה-SDR מחובר", None
    if r.returncode != 0:
        return (r.stderr or "dumpvdl2 failed").strip(), _journal_tail(VDL2_SERVICE)
    # כמו ב-acarsdec: השירות יכול לעלות ואז לקרוס => פולינג ולא בדיקה בודדת
    for _ in range(7):
        time.sleep(0.5)
        if not _is_active(VDL2_SERVICE):
            return "dumpvdl2 נכשל לעלות — בדוק journalctl -u airam-vdl2", _journal_tail(VDL2_SERVICE)
    return None, None


def _enter_acars(freqs):
    """עוצר את שאר צרכני ה-SDR ומריץ acarsdec. מחזיר (error, detail).
    Conflicts ב-unit עוצר אותם ממילא, אבל עוצרים מפורשות תחילה כדי לשחרר את
    ה-SDR לפני ש-acarsdec פותח אותו (מונע מרוץ על המכשיר)."""
    for svc in ("rtl_airband", VDL2_SERVICE, SATCOM_SERVICE):
        try:
            _sysctl("stop", svc, timeout=30)
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
    for svc in ("rtl_airband", ACARS_SERVICE, VDL2_SERVICE):
        try:
            _sysctl("stop", svc, timeout=30)
        except Exception:
            pass
    write_satcom_env(freqs, gain=gain, bias_tee=bias_tee, skip_c=skip_c, spectrum=spectrum)
    try:
        # airam-satcom.service (בשונה משאר צרכני ה-SDR) מוגדר עם StartLimitBurst
        # סופי — הגנה מפני קריסה חוזרת שמדליקה מחדש bias-T ללא פיקוח (ר' ההערה
        # ביחידה). אם התקרה הופעלה מקריסה קודמת, restart רגיל ייכשל עד
        # reset-failed; best-effort, לא תלוי הצלחה (no-op תקין כשלא היה כשל).
        _sysctl("reset-failed", SATCOM_SERVICE, timeout=10)
    except Exception:
        pass
    try:
        r = _sysctl("restart", SATCOM_SERVICE, timeout=45)
    except subprocess.TimeoutExpired:
        return "הפעלת SATCOM נתקעה — בדוק שה-SDR מחובר", None
    if r.returncode != 0:
        return (r.stderr or "inmarsat-sniffer failed").strip(), _journal_tail(SATCOM_SERVICE)
    # כמו ב-acarsdec/dumpvdl2: השירות יכול לעלות ואז לקרוס => פולינג ולא בדיקה בודדת
    for _ in range(7):
        time.sleep(0.5)
        if not _is_active(SATCOM_SERVICE):
            return ("inmarsat-sniffer נכשל לעלות — בדוק journalctl -u airam-satcom",
                    _journal_tail(SATCOM_SERVICE))
    return None, None


def _enter_standby():
    """מצב כיבוי (standby): עוצר את *ארבעת* צרכני ה-SDR (rtl_airband + acarsdec +
    dumpvdl2 + inmarsat-sniffer) => משחרר את ה-RSP1B ליישום SDR אחר, בעוד
    airam-web/הדף נשארים פעילים. את sdrplay.service משאירים חי בכוונה: ה-API
    daemon הוא המתווך שמאפשר לאפליקציית SDRplay אחרת להתחבר מיד — וגם ה-sudoers
    ממילא אינו מתיר לעצור אותו. מחזיר (error, detail). serialized תחת TUNE_LOCK
    ע"י הקורא."""
    consumers = (ACARS_SERVICE, VDL2_SERVICE, SATCOM_SERVICE, "rtl_airband")
    for svc in consumers:
        try:
            _sysctl("stop", svc, timeout=30)
        except Exception:
            pass
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
            return m
    return None


def _enter_voice(params):
    """כניסה סימטרית לקול (peer של _enter_acars/_enter_vdl2/_enter_satcom): עוצר
    את צרכני הדאטה, כותב את קונפיג rtl_airband ומרים עם אימות.
    מחזיר (error, detail, sdr_down) — כמו _restart_and_verify."""
    # אם acarsdec/dumpvdl2/inmarsat-sniffer רץ הוא מחזיק את ה-SDR => עוצרים
    # מפורשות לפני שמרימים את rtl_airband (Conflicts גיבוי, אבל זה משחרר את
    # המכשיר מיד).
    for svc in (ACARS_SERVICE, VDL2_SERVICE, SATCOM_SERVICE):
        if _is_active(svc):
            try:
                _sysctl("stop", svc, timeout=30)
            except Exception:
                pass
    write_config(params["freq"], params["mod"], params["agc"], params["if_gain"],
                 params["rf_gain"], params["squelch_mode"], params["squelch_snr"],
                 fm_notch=bool(params.get("fm_notch", False)),
                 narrow=params.get("voice_narrow"), lowpass=params.get("voice_lowpass"))
    return _restart_and_verify()


def _fail_to_off(st, err, detail, log_prefix):
    """כישלון כניסה למצב => נפילה ל-off (standby) — לעולם לא fallback לקול.
    עוצר את כל הצרכנים (best-effort), שומר state עם off + prev_mode, ומחזיר
    (payload, 500) בחוזה שה-UI מכיר: app_mode/state תמיד off => נחיתה במסך הבית."""
    log.warning("%s failed: %s — falling to standby", log_prefix, err)
    try:
        _enter_standby()   # שגיאה משנית לא מעניינת — ממילא מדווחים על המקורית
    except Exception:
        pass
    new_state = {**st, "app_mode": "off", "prev_mode": st.get("app_mode", "off")}
    save_state(new_state)
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
_scan_status = {"idx": -1, "leg": None, "next_switch_at": None, "plan": []}


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
            default = ACARS_FREQS_DEFAULT if mode == "acars" else VDL2_FREQS_DEFAULT
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
        params, perr = _parse_tune(load_state())
        if perr:
            params, _ = _parse_tune(DEFAULT_STATE)
        err, detail, _sdr_down = _enter_voice(params)
        return err, detail
    key = "acars_freqs" if mode == "acars" else "vdl2_freqs"
    default = ACARS_FREQS_DEFAULT if mode == "acars" else VDL2_FREQS_DEFAULT
    enter = _enter_acars if mode == "acars" else _enter_vdl2
    freqs = leg.get("freqs") or load_state().get(key) or default
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
            _scan_status.update(idx=-1, leg=None, next_switch_at=None)


def _scan_loop(stop_evt, plan, start_idx, first_dwell, consumer_active=False):
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
                _scan_status.update(idx=-1, leg=None, next_switch_at=None)
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
        same = (last_entered is not None and last_entered["mode"] == leg["mode"]
                and last_entered.get("freqs") == leg.get("freqs"))
        if same:
            # אותה רגל בדיוק כבר רצה (מצב+תדרים) — אין טעם ב-restart של השירות,
            # רק מרעננים את הטיימר. חוסך נתק שמע/הקלטות כל dwell בלוח עם רגל
            # יחידה (או רגלים חוזרות) בעלת חלון שעות.
            with _scan_lock:
                _scan_status.update(idx=idx % len(plan), leg=leg,
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
            if consecutive_fail >= len(plan):
                log.warning("scan: כל הרגלים נכשלו בסבב — נופל ל-off")
                # תחת TUNE_LOCK עד סוף כתיבת ה-state (כולל) — לא רק סביב
                # _enter_standby: בלעדיו קריאה/כתיבה מקבילה ל-state.json (כמו
                # /api/session/ack, שגם הוא עכשיו תחת TUNE_LOCK) הייתה עלולה
                # לקרוא לפני השינוי הזה ולדרוס אותו אחרי — lost update.
                if not TUNE_LOCK.acquire(timeout=5):
                    log.warning("scan: לא ניתן היה לתפוס TUNE_LOCK לכיבוי — פעולה אחרת כנראה כבר משתלטת")
                    return
                try:
                    _enter_standby()
                    if stop_evt.is_set():
                        return   # מעבר מצב אחר כבר תפס פיקוד בינתיים — לא דורסים את ה-state שלו
                    cur = load_state()
                    save_state({**cur, "app_mode": "off", "prev_mode": "scan"})
                finally:
                    TUNE_LOCK.release()
                with _scan_lock:
                    _scan_status.update(idx=-1, leg=None, next_switch_at=None)
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
            _scan_status.update(idx=idx % len(plan), leg=leg,
                                next_switch_at=time.time() + leg["dwell_sec"])
        remaining = leg["dwell_sec"]
        idx += 1


def _scan_activate(plan):
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
        stop_evt = threading.Event()
        thread = threading.Thread(target=_scan_loop, args=(stop_evt, plan, 0, 0, False), daemon=True)
        with _scan_lock:
            _scan_status.update(idx=-1, leg=None, next_switch_at=None, plan=plan)
            _scan_thread, _scan_thread_stop = thread, stop_evt
        thread.start()
        return None, None
    err, detail = _scan_enter_leg(plan[active_idx])
    if err:
        return err, detail
    stop_evt = threading.Event()
    thread = threading.Thread(target=_scan_loop,
                              args=(stop_evt, plan, active_idx + 1, plan[active_idx]["dwell_sec"], True),
                              daemon=True)
    with _scan_lock:
        _scan_status.update(idx=active_idx, leg=plan[active_idx],
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
                # read1: מחזיר מה שכבר הגיע (עד 4KB) במקום לחכות ל-8KB מלאים — ב-48kbps
                # (~6KB/ש') read(8192) צבר ~1.4ש' לפני כל שליחה, בלי שום מרווח לרשת חלשה
                chunk = up.read1(4096) if hasattr(up, "read1") else up.read(4096)
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
        st["mode_ok"] = (live is not None) or not any_due
    else:
        st["app_mode"] = live or saved
        # mode_ok=False: המצב השמור אמור להריץ צרכן ואף אחד לא רץ (קריסה / עליית
        # מערכת / _boot_restore עוד בדרך). True גם ב-off — standby מכוון אינו תקלה.
        st["mode_ok"] = (live is not None) or (saved == "off")
    st.update(presets=load_presets(), mount=MOUNT, port=ICECAST_PORT, version=VERSION,
              acars_banks=ACARS_BANKS, vdl2_banks=VDL2_BANKS, satcom_banks=SATCOM_BANKS)
    return jsonify(st)


# --- פרופילי רווח לפי מקום (PR 4, docs/voice-rf-quality-plan.md) ------------
# "פארק אריאל שרון: LNA 6, מסנן FM" — הגדרות הקצה הקדמי/השמע שנשמרו *ע"י המשתמש*
# (אחרי 🩺 או ניסוי ידני) ומוחלות בנגיעה אחת. §12: אין פרופילים מובנים עם מספרים
# מומצאים — רק מה שנמדד/נבחר במקום עצמו. נשמרים ב-state["gain_profiles"].
PROFILE_KEYS = ("agc", "if_gain", "rf_gain", "fm_notch", "voice_narrow", "voice_lowpass")
PROFILES_MAX = 20
PROFILE_NAME_MAX = 40


def _profile_from_state(st, name):
    p, _ = _parse_tune({**st, "freq": st.get("freq", DEFAULT_STATE["freq"])})
    p = p or {}
    return {"id": time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}",
            "name": name, "created": time.time(),
            "agc": bool(p.get("agc", True)), "if_gain": int(p.get("if_gain", IF_GAIN_DEFAULT)),
            "rf_gain": int(p.get("rf_gain", RF_GAIN_DEFAULT)),
            "fm_notch": bool(st.get("fm_notch", False)),
            "voice_narrow": bool(st.get("voice_narrow", False)),
            "voice_lowpass": _sanitize_lowpass(st.get("voice_lowpass"))}


def _profile_matches(st, prof):
    """האם ההגדרות הנוכחיות זהות לפרופיל (ה-UI מסמן "שונה" כשלא)."""
    cur = _profile_from_state(st, "")
    return all(cur[k] == prof.get(k) for k in PROFILE_KEYS)


@app.route("/api/profiles", methods=["GET", "POST"])
def api_profiles():
    """GET: הרשימה + איזה פרופיל תואם כרגע. POST {action}: save {name} (מההגדרות הנוכחיות)
    | delete {id} | apply {id} (בקול חי — דרך _voice_tune; אחרת רק state). דרך _guard."""
    if request.method == "GET":
        st = load_state()
        profs = st.get("gain_profiles") or []
        match = next((p["id"] for p in profs if _profile_matches(st, p)), None)
        return jsonify(ok=True, profiles=profs, active=match, max=PROFILES_MAX)
    data = request.get_json(silent=True) or {}
    action = data.get("action")
    if action == "apply":
        st = load_state()
        prof = next((p for p in st.get("gain_profiles") or [] if p.get("id") == data.get("id")), None)
        if not prof:
            return jsonify(ok=False, error="הפרופיל לא נמצא"), 404
        fields = {k: prof[k] for k in PROFILE_KEYS if k in prof}
        if st.get("app_mode") == "voice" and _live_mode() == "voice":
            params, perr = _parse_tune({**st, **fields})
            if perr:
                return jsonify(ok=False, error=perr), 400
            payload, status = _voice_tune(params)
            return jsonify(payload), status
        # לא בקול — נשמר ויחול בכניסה הבאה (נופל לכתיבת state למטה)
        def mutate(st):
            st.update(fields)
            return None
    elif action == "save":
        name = str(data.get("name") or "").strip()[:PROFILE_NAME_MAX]
        if not name:
            return jsonify(ok=False, error="חסר שם לפרופיל"), 400

        def mutate(st):
            profs = list(st.get("gain_profiles") or [])
            if len(profs) >= PROFILES_MAX:
                return ("אפשר לשמור עד %d פרופילים — מחק אחד קודם" % PROFILES_MAX, 409)
            profs.append(_profile_from_state(st, name))
            st["gain_profiles"] = profs
            return None
    elif action == "delete":
        def mutate(st):
            profs = [p for p in st.get("gain_profiles") or [] if p.get("id") != data.get("id")]
            if len(profs) == len(st.get("gain_profiles") or []):
                return ("הפרופיל לא נמצא", 404)
            st["gain_profiles"] = profs
            return None
    else:
        return jsonify(ok=False, error="פעולה לא מוכרת"), 400
    # read-modify-write של state תחת TUNE_LOCK (כמו כל כתיבה אחרת של state)
    if not TUNE_LOCK.acquire(blocking=False):
        return jsonify(ok=False, error="פעולה אחרת מתבצעת כרגע — נסה שוב בעוד רגע"), 409
    try:
        st = load_state()
        err = mutate(st)
        if err:
            return jsonify(ok=False, error=err[0]), err[1]
        save_state(st)
    finally:
        TUNE_LOCK.release()
    note = "נשמר — יחול בכניסה הבאה לקול" if action == "apply" else None
    return jsonify(ok=True, profiles=st.get("gain_profiles") or [], note=note)


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
SDR_CONSUMERS = ("rtl_airband", ACARS_SERVICE, VDL2_SERVICE, SATCOM_SERVICE)
_SDR_CONSUMER_MODE = {svc: m for m, svc in MODE_SERVICE.items()}
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
    try:
        r = subprocess.run(["lsusb", "-d", "1df7:"], capture_output=True, text=True, timeout=5)
    except Exception:
        return None, None
    if r.returncode != 0:
        return False, None
    line = (r.stdout.splitlines() or [""])[0]
    m = re.search(r"ID\s+([0-9a-fA-F]{4}:[0-9a-fA-F]{4})\s*(.*)", line)
    return True, ((m.group(2).strip() or m.group(1)) if m else line.strip() or None)


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
    usb, desc = _sdr_usb()
    res = {"ok": True, "usb": usb, "usb_desc": desc, "state": None, "mode": None,
           "service_state": None, "api": None, "label": None, "detail": None,
           "suspects": [], "checked_age": None}
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


@app.route("/api/health")
def api_health():
    """סטטוס המערכת — מאפשר ל-UI להבדיל בין "אין שידור" ל"משהו נפל"."""
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
    # standby מכוון: כל הצרכנים כבויים ו-state מסומן off => תקין, *לא* תקלה (אחרת
    # מצב הכיבוי שביקש המשתמש היה נראה כקריסה). sdrplay נשאר active במפה.
    saved_state = load_state()
    saved = saved_state.get("app_mode", "off")
    off_ok = (saved == "off"
              and services["rtl_airband"] != "active"
              and services["airam-acars"] != "active"
              and services["airam-vdl2"] != "active"
              and services["airam-satcom"] != "active")
    # המצב נגזר מהשירות הפעיל, ובאין פעיל — מהכוונה השמורה (אין ברירת-מחדל לקול).
    # ok = בריאות המצב הנגזר בלבד: מצב שמור שלא רץ => ok=False (תקלה מדווחת),
    # למשל אחרי קריסת שירות או בזמן ש-_boot_restore עוד מחזיר את המצב.
    active = ("vdl2" if services["airam-vdl2"] == "active"
              else "acars" if services["airam-acars"] == "active"
              else "satcom" if services["airam-satcom"] == "active"
              else "voice" if services["rtl_airband"] == "active" else None)
    if saved == "scan":
        # סריקה: תקין כל עוד הרגל הנוכחית (איזשהו צרכן) פועלת, *או* שאף רגל לא
        # אמורה לרוץ כרגע (חלון שעות) — "ממתין" אינו תקלה.
        plan = saved_state.get("scan_plan") or []
        any_due = any(_leg_active_now(leg) for leg in plan) if plan else True
        mode, ok = "scan", (active is not None) or not any_due
    else:
        mode = active or saved
        ok = (voice_ok if mode == "voice" else acars_ok if mode == "acars"
              else vdl2_ok if mode == "vdl2" else satcom_ok if mode == "satcom" else off_ok)
    # 🩺 בדיקת RF רצה: rtl_airband עולה ויורד בין מצבי ה-LNA — זו פעולה מתוכננת, לא
    # תקלה; ה-UI מקבל rf_check=True ומציג "בדיקת RF" במקום "תקלה"/"קול".
    with _rfc_lock:
        rf_check = _rfc["running"]
    if rf_check and services.get("sdrplay") == "active":
        ok = True   # רק ההחלפה של rtl_airband מוסתרת — sdrplay שנפל עדיין תקלה
    return jsonify(ok=ok, app_mode=mode, rf_check=rf_check,
                   services=services, sdr_present=_sdr_present(), stats_age=stats_age)


# שורת מדד בקובץ ה-stats של rtl_airband (פורמט Prometheus):
#   channel_dbfs_signal_level{freq="132.500"}	-42.3
# ה-label freq מאותר בתוך הסוגריים בנפרד => עמיד לשינוי סדר/הוספת labels ב-upstream.
_METRIC_RE = re.compile(r'^(\w+)\{([^}]*)\}\s+(-?[0-9.]+)')
_FREQ_LABEL_RE = re.compile(r'(?:^|[,{\s])freq="([0-9.]+)"')


# מוני "איבוד" של rtl_airband (output.cpp — buffer_overflow_count{device},
# output_overrun_count{device|mixer}, input_overrun_count{mixer,input}): מתאפסים עם
# התהליך, כלומר "מאז הכיוונון/ההפעלה האחרונים". עולים כשה-CPU לא עומד בקצב (מתח
# נמוך/throttling) — מבדילים "השמע נקטע ב-Pi" מ"השמע נקטע ברשת לטלפון".
_COUNTER_NAMES = ("buffer_overflow_count", "output_overrun_count", "input_overrun_count")


def parse_counters(text):
    """{שם: סכום על כל ה-labels} למוני האיבוד; מונה שלא מופיע בקובץ — לא במילון
    (לא 0 מומצא: input_overrun_count נכתב רק כשיש mixer)."""
    out = {}
    for line in text.splitlines():
        m = _METRIC_RE.match(line)
        if m and m.group(1) in _COUNTER_NAMES:
            try:
                out[m.group(1)] = out.get(m.group(1), 0) + int(float(m.group(3)))
            except ValueError:
                continue
    return out


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
            "file": p.name, "dur": round(st.st_size / REC_BYTES_PER_SEC, 1)}


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
    if err:
        rec["err"] = err
    if lang:
        rec["lang"] = lang
    _atomic_write(_tx_path(mp3), json.dumps(rec, ensure_ascii=False))
    return rec


# --- sidecar טלמטריית RF להקלטה (‎<file>.mp3.rf.json, PR 1 · 1.6) ------------
# בשטח לא נשארו ראיות מהסשן (docs/voice-rf-quality-plan.md §1): ה-stats נדרסים
# כל שנייה ואין היסטוריה. ה-sidecar מצמיד לכל שידור את מה שידוע עליו — הקונפיג
# שרץ, טלמטריית החומרה בחלון השידור, ותמונת stats — ונוסע עם ההקלטה לכל מקום
# שה-sidecar של התמלול נוסע (★, retention, סשן, ZIP — ר' _rec_sidecars).
# ⚠ חלון השידור, מאומת ממקור rtl_airband v5.2.0 (output.cpp):
#   start — חותמת שם הקובץ: strftime("_%Y%m%d_%H%M%S") של gettimeofday ברגע
#           הפתיחה (:440, open_time :468), בזמן מקומי (localtime=true בקונפיג).
#           דיוק שנייה, מעוגל *למטה* => start ≤ ההתחלה האמיתית.
#   end   — mtime: הקובץ נסגר אחרי ≤0.5ש' שקט (:372), עם כתיבת ה-lametag בסגירה
#           (:343) ו-rename מ-.tmp (:354, rename לא משנה mtime) => סוף השידור + ≤0.5ש'.
_REC_TS_RE = re.compile(rf"^{re.escape(REC_BASENAME)}_(\d{{8}})_(\d{{6}})_\d+\.mp3$")
# אורך מרבי של קובץ שידור: rtl_airband סוגר קובץ אחרי MAX_TRANSMISSION_TIME_SEC=3600
# (output.cpp:371,385) + שנייה של עיגול-למטה של start (ר' למעלה). משמש *רק*
# להכרעה בין שני ה-start האפשריים בשעה החוזרת של סוף שעון הקיץ (ר' _rec_start_ts)
# — המועמד השגוי רחוק בדיוק שעה, ולכן ‎end−start שלו ‏<0 או ‏>3601 (קובץ נסגר רק
# אחרי ‎>1ש' שידור, MIN_TRANSMISSION_TIME_SEC ‏:370).
REC_MAX_SPAN_SEC = 3600.0 + 1.0
# דיוק ה-start של חלון הטלמטריה (שנייה — חותמת שם הקובץ). אירועים ב-[start, start+1)
# יכולים להיות זנב של השידור הקודם (שידורים צמודים) — נרשם ב-sidecar כדי שמי
# שקורא אותו (ייצוא, סקריפט) יידע שהשיוך בשנייה הראשונה אינו חד.
REC_START_PRECISION_SEC = 1


def _rf_path(mp3):
    """airam_....mp3 => airam_....mp3.rf.json."""
    return mp3.parent / (mp3.name + ".rf.json")


def _rec_sidecars(mp3):
    """*כל* קובצי-הצד של הקלטה (תמלול חדש+ישן, טלמטריית RF) — מקור-אמת יחיד
    להעברה (★/סשן), להעתקה (סשן של שמורה) ולמחיקה (retention)."""
    return (*_tx_sidecars(mp3), _rf_path(mp3))


def _read_rf(mp3):
    """ה-sidecar כמילון, או None כשאין (הקלטה מלפני v2.26.0 / כתיבה שנכשלה) —
    "אין רשומה", לא "תקין". עמיד לקובץ פגום (כמו _read_tx)."""
    try:
        d = json.loads(_rf_path(mp3).read_text(errors="replace"))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def _rec_start_ts(name, end=None):
    """epoch של תחילת השידור מתוך שם הקובץ (זמן מקומי), או None.
    ⚠ בסוף שעון הקיץ שעה אחת חוזרת פעמיים (בישראל: 01:00–02:00 בלילה האחרון של
    אוקטובר) — mktime עם tm_isdst=-1 בוחר אחת מהן *שרירותית*, ושעה מוקדמת מדי
    הייתה מצמידה לשידור קצר ונקי שעה שלמה של טלמטריה זרה ("⚠ עומס ×N" של שידור
    אחר — §12: ערך אמיתי לישות הלא-נכונה). לכן: כל המועמדים (base, ‎base±3600)
    שמתפרשים חזרה לאותה מחרוזת זמן-מקומי הם תקפים; כשיש שניים, מכריעים לפי ה-mtime
    (‏end) — רק מועמד עם ‎0 ≤ end−start ≤ REC_MAX_SPAN_SEC. אין הכרעה (אין end /
    שניהם / אף אחד) => None ("לא ידוע"), לא ניחוש. ‎±3600: היסט ה-DST בישראל
    (ובאזורים הנפוצים) — באזור עם היסט אחר הכפילות לא תזוהה ונופלים להתנהגות
    mktime הרגילה."""
    m = _REC_TS_RE.match(name)
    if not m:
        return None
    stamp = m.group(1) + m.group(2)
    try:
        base = time.mktime(time.strptime(stamp, "%Y%m%d%H%M%S"))
        valid = sorted(c for c in {base - 3600, base, base + 3600}
                       if time.strftime("%Y%m%d%H%M%S", time.localtime(c)) == stamp)
    except (ValueError, OverflowError, OSError):
        return None
    if len(valid) == 1:
        return valid[0]
    if len(valid) != 2 or end is None:
        return None                      # אין מועמד תקף (TZ השתנה?) / אין במה להכריע
    fits = [c for c in valid if 0 <= end - c <= REC_MAX_SPAN_SEC]
    return fits[0] if len(fits) == 1 else None


def _build_rf_sidecar(mp3, now=None):
    """מה ידוע על השידור — כל שדה שאי אפשר לדעת נשאר None (§12):
      * קונפיג (mod/agc/if_gain/lna_state/fm_notch) — מ-airband.conf *רק* אם הוא
        נכתב לפני תחילת השידור ובאותו תדר; אחרת כוונן מחדש מאז => לא יודעים.
        ‏if_gain=None תחת AGC (ה-IFGR המוגדר לא חל — ר' ifgr_min/max מהחומרה).
      * post_stats {signal, noise, snr, t} — ⚠ **תמונת-מצב אחת ברגע הזיהוי**
        (≤WATCH_INTERVAL אחרי סוף השידור), **לא של השידור**: ה-noise הוא רצפת הערוץ
        אחרי השידור, וה-signal כנראה כבר לא של השידור עצמו. מקונן תחת שם מפורש (ולא
        signal/snr ברמה העליונה ליד start/end) כי ה-sidecar יוצא כמות שהוא ב-ZIP —
        קורא של ה-JSON היה לוקח את ‏snr כ-SNR של השידור (§12: ערך אמיתי לישות הלא-
        נכונה). None כשלא נלקחה: השלמה אחרי restart, או stats לא טריים/תדר אחר.
      * טלמטריית חומרה בחלון — _rf_window_summary, רק עם סימן-הבנייה. ‏start_precision_s:
        דיוק ה-start (ר' REC_START_PRECISION_SEC)."""
    now = time.time() if now is None else now
    end = mp3.stat().st_mtime
    start = _rec_start_ts(mp3.name, end)
    freq = _rec_freq_mhz(mp3.name)
    telemetry = _rf_telemetry_available()
    rec = {"v": 1, "written_at": round(now, 1), "freq": freq,
           "start": start, "end": round(end, 2),
           "config_known": False, "mod": None, "agc": None, "if_gain": None,
           "lna_state": None, "fm_notch": None,
           "post_stats": None, "start_precision_s": REC_START_PRECISION_SEC,
           "telemetry": telemetry, "telemetry_covered": False,
           "overload": None, "overload_at_start": None, "overload_events": None,
           "gain_events": None, "ifgr_at_start": None, "ifgr_min": None, "ifgr_max": None}
    try:
        cst = CONFIG_PATH.stat()
        conf = _parse_airband_conf(CONFIG_PATH.read_text())
    except OSError:
        cst, conf = None, {}
    if (cst is not None and start is not None and freq is not None
            and cst.st_mtime <= start and conf.get("freq") is not None
            and abs(conf["freq"] - freq) < 5e-4):
        rec.update(config_known=True, mod=conf.get("mod"), agc=conf.get("agc"),
                   if_gain=None if conf.get("agc") else conf.get("ifgr"),
                   lna_state=conf.get("lna"), fm_notch=conf.get("fm_notch"))
    if freq is not None and now - end <= WATCH_INTERVAL + STATS_MAX_AGE:
        try:
            smt = STATS_PATH.stat().st_mtime
            text = STATS_PATH.read_text()
        except OSError:
            smt, text = None, ""
        if smt is not None and now - smt <= STATS_MAX_AGE:
            vals = parse_stats(text, f"{freq:.3f}")
            sig = vals.get("channel_dbfs_signal_level")
            noise = vals.get("channel_dbfs_noise_level")
            if sig is not None or noise is not None:
                rec["post_stats"] = {
                    "signal": sig, "noise": noise, "t": round(smt, 2),
                    "snr": round(sig - noise, 1) if (sig is not None and noise is not None) else None}
    if telemetry and start is not None and start <= end:
        summ = _rf_window_summary(start, end)
        if summ is not None:
            rec["telemetry_covered"] = True
            rec.update(summ)
    return rec


def _write_rf_sidecar(mp3, now=None):
    """כותב את ה-sidecar אם עוד אין (idempotent: סריקה חוזרת אחרי append שנכשל
    לא דורסת רשומה שנכתבה ברגע הזיהוי המקורי). מחזיר את הרשומה או None."""
    p = _rf_path(mp3)
    if p.exists():
        return None
    rec = _build_rf_sidecar(mp3, now)
    _atomic_write(p, json.dumps(rec, ensure_ascii=False))
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


def _tx_status():
    """מצב מנגנון התמלול כולו — הבסיס לשורת הסטטוס ב-UI. ⚠ הערך שהופך את
    הפיצ'ר לגלוי: בלעדיו 'whisper לא מותקן' נראה בדיוק כמו 'אין מה לתמלל'."""
    st = load_state()
    lang = st.get("transcribe_lang") or TX_LANG_DEFAULT
    bin_ok = Path(WHISPER_BIN).exists()
    model = _whisper_model(lang)
    langs = {ln: bool(_whisper_model(ln)) for ln in TX_LANGS}
    return {"available": bool(bin_ok and model), "bin_ok": bin_ok,
            "model_name": Path(model).name if model else None,
            "lang": lang, "langs": langs,
            "auto": bool(st.get("transcribe_auto")),
            "queue": _tx_queue_len(), "busy": _tx_busy_file(),
            "install_hint": INSTALL_WHISPER_HINT}


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


def _transcribe_worker():
    """לולאת רקע יחידה (whisper לוקח את ה-CPU => לא מקבילים אותו).
    ⚠ ה-thread **לא מת** כשwhisper חסר: הוא ממשיך לישון ולבדוק זמינות, כך
    שהתקנת whisper בזמן ריצה נתפסת בלי restart לשירות."""
    warned = False
    while True:
        try:
            st = load_state()
            lang = st.get("transcribe_lang") or TX_LANG_DEFAULT
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
            mp3 = _tx_next_target(bool(st.get("transcribe_auto")))
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


def _sweep_recordings():
    """retention: עד REC_MAX_FILES / REC_MAX_BYTES (חדש=>ישן), ו-.tmp נטושים
    (שידור שנקטע בקריסה משאיר .tmp שלעולם לא ייסגר ל-mp3). קובצי-הצד (תמלול,
    טלמטריית RF — _rec_sidecars) נמחקים יחד עם ההקלטה שלהם.
    ⚠ הקלטות שמורות (★) לא מטופלות כאן **בכלל** — הן יושבות ב-`saved/`,
    ו-`glob("*.mp3")` אינו רקורסיבי. זה כל מנגנון הפטור: אין רשימה לקרוא,
    אין מה לסנכרן, ואין מצב שבו קובץ פגום גורם למחיקת מה שהמשתמש שמר.
    ⚠ כולו תחת `_STAR_LOCK`: ‏_move_recording (★/ביטול/שמירת סשן) מעביר קודם את
    קובצי-הצד ואז את ה-mp3. מעבר יתומים שרץ *בין* שני ה-os.replace ראה sidecar
    ב-saved/ בלי mp3 לידו ומחק אותו לצמיתות — בדיוק הקלטה שהמשתמש בחר לשמור, ו-
    ‎.rf.json אינו בר-שחזור (אירועי החלון כבר נדחקו). אותה נעילה כמו _write_rf_sidecars."""
    with _STAR_LOCK:
        _sweep_recordings_locked()


def _sweep_recordings_locked():
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
                for s in _rec_sidecars(p):
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
        for pat, strip in (("*.txt", ".txt"), ("*.tx.json", ".tx.json"), ("*.rf.json", ".rf.json")):
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


def _write_rf_sidecars(rows):
    """sidecar טלמטריה לכל הקלטה שזה עתה זוהתה — *לפני* שורת היומן, כך שהשורה
    מופיעה כבר עם ה-rf שלה. כשל בקובץ בודד לא עוצר את היומן. תחת _STAR_LOCK:
    ★ שמעביר את ההקלטה ל-saved/ באמצע היה משאיר את ה-sidecar יתום ב-REC_DIR."""
    for r in rows:
        try:
            with _STAR_LOCK:
                mp3 = _rec_path(str(r.get("file") or ""))
                if mp3 is not None:
                    _write_rf_sidecar(mp3)
        except Exception:
            log.warning("sidecar RF ל-%s נכשל", r.get("file"), exc_info=True)


def _activity_watcher():
    """לולאת רקע: הקלטה חדשה שהסתיימה => שורה ביומן; ואז retention.
    בעלייה ממשיכים מה-ts האחרון שנרשם => הקלטות מהזמן שהשרת היה כבוי נקלטות."""
    last_seen = _last_logged_ts()
    while True:
        try:
            rows, newest = _scan_new_recordings(last_seen)
            if rows:
                _write_rf_sidecars(rows)
                _append_activity(rows)
                last_seen = newest   # מקדמים רק אחרי כתיבה מוצלחת => כישלון append לא מאבד אירועים
            _sweep_recordings()
        except Exception:
            log.exception("activity watcher")
        time.sleep(WATCH_INTERVAL)


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
    # טלמטריית RF של השידור — נקראת חי מה-sidecar (כמו tx); None = אין רשומה
    # (הקלטה ישנה/נמחקה), לא "תקין".
    ev["rf"] = _read_rf(mp3) if mp3 else None
    return ev


@app.route("/api/activity")
def api_activity():
    """אירועי השידור האחרונים, חדש=>ישן. exists=False כשההקלטה כבר נמחקה ב-retention.
    ‏?starred=1 => רק ההקלטות השמורות, **נסרקות מ-`saved/`** ולא מהיומן — כך הן
    נשארות נגישות גם אחרי שהשורה שלהן קוצצה מ-activity.jsonl (ACTIVITY_KEEP),
    בלי מאגר-מצב מקביל שאפשר לאבד/לפגום (ר' הערת SAVED_DIRNAME)."""
    count, used = _saved_usage()
    meta = {"starred_count": count, "starred_max": REC_STAR_MAX_FILES,
            "starred_bytes": used, "starred_max_bytes": REC_STAR_MAX_BYTES}
    if request.args.get("starred") in ("1", "true", "yes"):
        events = []
        for p in _iter_recordings():
            if not _is_saved(p.name):
                continue
            try:
                events.append(_decorate_event(_rec_event(p)))
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
        # ⚠ אותה משפחת באג כמו ב-_jsonl_records: שורת JSON תקין שאינה אובייקט
        # (‏null/מספר/מערך — שריד בלוק פגום אחרי כיבוי פתאומי) עברה את
        # ה-except ValueError ואז הפילה את הראוט ב-TypeError. ‏/api/activity
        # נקרא בפולינג כל 15 שניות במצב קול, כך שזה 500 חוזר ולא תקלה חד-פעמית.
        if not isinstance(ev, dict):
            continue
        events.append(_decorate_event(ev))
    return jsonify(ok=True, events=events, **meta)


def _rec_name_arg():
    """(name, error_response) — שם הקלטה מגוף הבקשה, מאומת מול תבנית השם.
    ⚠ האימות מול _REC_NAME_RE הוא גם ההגנה מפני path traversal: השם חייב להיות
    בדיוק airam_<תאריך>_<שעה>_<Hz>.mp3, כך שאין בו לוכסנים/נקודות-נקודה בכלל."""
    data = request.get_json(silent=True) or {}
    name = str(data.get("file") or "")
    if not _REC_NAME_RE.match(name):
        return None, (jsonify(ok=False, error="שם הקלטה לא תקין"), 400)
    return name, None


def _move_recording(src, dst_dir):
    """מעביר הקלטה + קובצי-הצד שלה. ⚠ `os.replace` אטומי בתוך אותו filesystem
    => אין רגע שבו הקובץ לא קיים באף צד, ואין מה לסנכרן עם רשומת-מצב."""
    dst_dir.mkdir(parents=True, exist_ok=True)
    for s in _rec_sidecars(src):
        if s.exists():
            try:
                os.replace(s, dst_dir / s.name)
            except OSError:
                pass          # ה-sidecar אינו קריטי; ה-mp3 הוא מה שחשוב
    os.replace(src, dst_dir / src.name)


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
    with _STAR_LOCK:
        live, saved = REC_DIR / name, _saved_dir() / name
        if not want:
            if saved.is_file():
                try:
                    _move_recording(saved, REC_DIR)
                except OSError as e:
                    return jsonify(ok=False, error=f"ביטול השמירה נכשל: {e}"), 500
            count, used = _saved_usage()
            return jsonify(ok=True, file=name, starred=False,
                           starred_count=count, starred_max=REC_STAR_MAX_FILES)
        if saved.is_file():
            count, used = _saved_usage()          # כבר שמורה — idempotent
            return jsonify(ok=True, file=name, starred=True,
                           starred_count=count, starred_max=REC_STAR_MAX_FILES)
        if not live.is_file():
            return jsonify(ok=False, error="ההקלטה כבר לא קיימת"), 404
        count, used = _saved_usage()
        try:
            size = live.stat().st_size
        except OSError:
            return jsonify(ok=False, error="ההקלטה כבר לא קיימת"), 404
        # ⚠ מסרבים, לא מוחקים ותיקה: המשתמש הגן על שתיהן במפורש, והבחירה מי
        # מהן להסיר היא שלו.
        if count >= REC_STAR_MAX_FILES or used + size > REC_STAR_MAX_BYTES:
            return jsonify(ok=False, starred_count=count, starred_max=REC_STAR_MAX_FILES,
                           error=(f"אין מקום לשמירה נוספת ({count}/{REC_STAR_MAX_FILES}) — "
                                  "בטל שמירה של הקלטה אחרת")), 409
        try:
            _move_recording(live, _saved_dir())
        except OSError as e:
            return jsonify(ok=False, error=f"השמירה נכשלה: {e}"), 500
        count, used = _saved_usage()
    return jsonify(ok=True, file=name, starred=True,
                   starred_count=count, starred_max=REC_STAR_MAX_FILES)


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
    mp3 = _rec_path(name)
    if mp3 is None:
        return jsonify(ok=False, error="ההקלטה כבר לא קיימת"), 404
    data = request.get_json(silent=True) or {}
    lang = data.get("lang") or load_state().get("transcribe_lang") or TX_LANG_DEFAULT
    if lang not in TX_LANGS:
        return jsonify(ok=False, error="שפה לא נתמכת"), 400
    bin_ok, model = _whisper_ready(lang)
    if not (bin_ok and model):
        return jsonify(ok=False, tx=_tx_status(),
                       error=(("אין מודל תמלול לעברית — נדרש מודל רב-לשוני. הרץ על ה-Pi: "
                               if lang == "he" else "תמלול לא מותקן — הרץ על ה-Pi: ")
                              + INSTALL_WHISPER_HINT)), 501
    cur = _read_tx(mp3)
    if cur.get("state") == "pending":
        return jsonify(ok=True, file=name, tx=cur, queue=_tx_queue_len())
    # לחיצה מפורשת של המשתמש היא **תמיד** ניסיון חוזר — גם על 'empty'.
    # ⚠ קודם `force` נשלח רק ב-'failed', ולכן לחיצה על שורת "לא זוהה דיבור"
    # החזירה את אותו sidecar ולא עשתה כלום: הכפתור נראה שבור.
    _write_tx(mp3, "pending", lang=lang)
    _TX_FAILS.pop(name, None)
    return jsonify(ok=True, file=name, queue=_tx_queue_len(),
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
        st = load_state()
        if "auto" in data:
            st["transcribe_auto"] = bool(data.get("auto"))
        if "lang" in data:
            if data.get("lang") not in TX_LANGS:
                return jsonify(ok=False, error="שפה לא נתמכת"), 400
            st["transcribe_lang"] = data["lang"]
        save_state(st)
    return jsonify(ok=True, tx=_tx_status())


@app.route("/api/recordings/starred.zip")
def api_starred_zip():
    """ייצוא כל ההקלטות השמורות (+התמלולים) כ-ZIP אחד.
    ⚠ הסיבה שזה קיים: השמורות יושבות על כרטיס SD, וכרטיסי SD ב-Pi מתים.
    ‏ZIP_STORED ולא DEFLATED — MP3 כבר דחוס, ודחיסה חוזרת היא רק CPU על ה-Pi.
    נכתב לקובץ זמני ולא ל-BytesIO: עד REC_STAR_MAX_BYTES בזיכרון על מכונה עם
    4GB, בזמן שהרדיו רץ, זה בדיוק מה שאסור."""
    import zipfile
    files = sorted(_saved_dir().glob("*.mp3")) if _saved_dir().is_dir() else []
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
                tx = _read_tx(p)
                if tx.get("text"):
                    z.writestr(p.name + ".txt", tx["text"] + "\n")
                rf = _rf_path(p)
                if rf.is_file():
                    try:
                        z.write(rf, rf.name)      # טלמטריית RF של השידור (sidecar כמות שהוא)
                    except OSError:
                        pass
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


@app.route("/api/sessions", methods=["GET", "POST"])
def api_sessions():
    """GET — רשימת סשנים שמורים (חדש→ישן, ממוין לפי created_at ב-meta.json —
    לא לפי שם-תיקייה, כדי לא להיתקל בסדר-מחרוזות מוזר בהתנגשות ‏-2/-3).
    POST ‏{minutes, note?} — שומר את N הדקות האחרונות מה-buffer המתגלגל
    (adsb.read_track_slice) + את ההקלטות שבחלון הזמן הזה. דרך _guard."""
    if request.method == "GET":
        sessions = []
        if SESSIONS_DIR.is_dir():
            for d in SESSIONS_DIR.iterdir():
                if not d.is_dir() or not SESSION_ID_RE.match(d.name):
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

    session_id = _new_session_id()
    sdir = SESSIONS_DIR / session_id
    clips_dir = sdir / SESSION_CLIPS_DIRNAME
    clips_dir.mkdir(parents=True, exist_ok=True)

    # קליפים בחלון הזמן: שמורה (★) מ*עתיקים* (שומרת על ההגנה ב-saved/ גם),
    # לא-שמורה *מועברת* (משתמשת ב-_move_recording הקיים — משחררת מ-retention
    # החי, בדיוק כמו §12: "מיקום הקובץ הוא המצב", אפס לוגיקת-פטור חדשה).
    clips = []
    for p in _iter_recordings():
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if not (t_start <= mtime <= t_end):
            continue
        ev = _rec_event(p)
        # תחת _STAR_LOCK: ★ מקביל או מעבר-יתומים של _sweep_recordings באמצע
        # ההעברה/העתקה היו משאירים/מוחקים קובצי-צד (ר' _sweep_recordings)
        with _STAR_LOCK:
            if _is_saved(p.name):
                try:
                    shutil.copy2(p, clips_dir / p.name)
                    for s in _rec_sidecars(p):
                        if s.exists():
                            shutil.copy2(s, clips_dir / s.name)
                except OSError:
                    continue
            else:
                try:
                    _move_recording(p, clips_dir)
                except OSError:
                    continue
        clips.append(ev)

    aircraft = sorted({ac[0] for row in rows for ac in row.get("ac", [])})
    gaps = [{"t": row["t"], "reason": row.get("gap"), "detail": row.get("detail")}
            for row in rows if "gap" in row]

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

    # ⚠ app_mode/freq הם תמונת-מצב *נוכחית* בזמן השמירה, לא היסטוריה מלאה:
    # אין ב-AIR-AM יומן מעברי-מצב (ר' docs/session-replay-design.md §4.3 —
    # `modes` שם היה תכנון-אידיאלי; לממש אותו דורש מנגנון חדש לגמרי, לא רק
    # קריאת state קיים). מתועד כאן כסטייה מכוונת, לא כשגיאה.
    st = load_state()
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
        _atomic_write(sdir / "meta.json", json.dumps(meta, ensure_ascii=False, indent=2))
    except OSError as e:
        shutil.rmtree(sdir, ignore_errors=True)
        return jsonify(ok=False, error=f"שמירת מטא-דאטה נכשלה: {e}"), 500
    return jsonify(ok=True, id=session_id, session=meta)


@app.route("/api/sessions/<session_id>", methods=["GET", "DELETE"])
def api_session_detail(session_id):
    """GET — meta.json. DELETE — מחיקת הסשן כולו (קליפים+מסלול+מטא-דאטה).
    ⚠ DELETE על סשן ששמר קליפ *לא-שמור* לא מחזיר אותו לחיים — הוא נמחק
    יחד עם התיקייה, בדיוק כמו שמחיקת הקלטה רגילה בלתי-הפיכה."""
    sdir = _session_dir(session_id)
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
    clips_dir = sdir / SESSION_CLIPS_DIRNAME
    for c in meta.get("clips") or []:
        if isinstance(c, dict) and c.get("file"):
            c["tx"] = _read_tx(clips_dir / c["file"])
            c["rf"] = _read_rf(clips_dir / c["file"])
    return jsonify(ok=True, session=meta)


@app.route("/api/sessions/<session_id>/track")
def api_session_track(session_id):
    """מסלול ה-ADS-B של הסשן, מפוענח מ-track.jsonl.gz ל-JSON (שורות ac+gap
    כמו שהן ב-adsb.read_track_slice — ה-UI מקבל אותו סכימה משני המקורות)."""
    sdir = _session_dir(session_id)
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
    sdir = _session_dir(session_id)
    if sdir is None or not _REC_NAME_RE.match(name):
        return jsonify(ok=False, error="לא נמצא"), 404
    return send_from_directory(str(sdir / SESSION_CLIPS_DIRNAME), name)


@app.route("/api/sessions/<session_id>/export.zip")
def api_session_export(session_id):
    """ייצוא סשן שלם (מטא-דאטה + מסלול + כל הקליפים) כ-ZIP — אותו דפוס בדיוק
    כמו api_starred_zip (ZIP_STORED, קובץ זמני ולא BytesIO, ניקוי ב-call_on_close)."""
    import zipfile
    sdir = _session_dir(session_id)
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
            clips_dir = sdir / SESSION_CLIPS_DIRNAME
            if clips_dir.is_dir():
                for p in sorted(clips_dir.glob("*.mp3")):
                    try:
                        z.write(p, f"{SESSION_CLIPS_DIRNAME}/{p.name}")
                    except OSError:
                        continue
                    rf = _rf_path(p)
                    if rf.is_file():
                        try:
                            z.write(rf, f"{SESSION_CLIPS_DIRNAME}/{rf.name}")
                        except OSError:
                            pass
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
def recordings(name):
    # send_from_directory חוסם path traversal; ‏<name> (לא <path:>) חוסם תתי-תיקיות.
    # שמורה יושבת ב-saved/ => מנסים שם כשאיננה בתיקייה החיה.
    d = REC_DIR if (REC_DIR / name).is_file() else _saved_dir()
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


# --- טלמטריית RF מהחומרה: עוקב journalctl יחיד (ר' SOAPY_RF_MARK) ------------
# ⚠ regex *לא מעוגן* (search): ה-defaultLogHandler של SoapySDR מקדים "[INFO] "
# (LoggerC.cpp:63 ב-SoapySDR 0.8.1) ורמה אחרת הייתה עוטפת בקודי ANSI (:61).
_RF_OVERLOAD_RE = re.compile(r"AIRAM_RF overload=([01])(?!\d)")
_RF_GAIN_RE = re.compile(r"AIRAM_RF gain grdb=(\d+) lna_grdb=(\d+)")
# גבול-סשן *והוכחת-חיים*: ה-patch רושם אותה ב-activateStream, לפני sdrplay_api_Init
# (ולכן לפני כל אירוע overload של הסשן — ev_callback נרשם רק ב-Init). זו הראיה
# היחידה בזמן ריצה שהמודול *המתוקן* נטען ושה-log שלו מגיע לעוקב — סימן-הבנייה
# מוכיח רק בנייה. בלעדיה overload נשאר None ("לא ידוע"), לעולם לא False: מודול
# לא-מתוקן שזכה ברישום, SOAPY_SDR_LOG_LEVEL מעל INFO, airam בלי הרשאת יומן — כולם
# "עוקב מחובר ושקט", ושקט אינו "אין עומס" (§12).
# ⚠ למה לא שורת האתחול של rtl_airband ("SoapySDR: device '..' initialized",
# input-soapysdr.cpp:272) כגבול, כפי שהיה: היא נרשמת דרך syslog (do_syslog=1
# כברירת מחדל, rtl_airband.cpp:747,876-878 — ה-unit לא מעביר ‎-e) => /dev/log,
# בעוד שורות AIRAM_RF הן fprintf(stderr) של SoapySDR (LoggerC.cpp:63) => ה-stream
# socket של ה-unit. journald לא מבטיח סדר בין שני מקורות, ותחת עומס (אתחול) שורת
# אתחול מאוחרת הייתה מאפסת "עומס" אמיתי שכבר נקלט. stream=start עוברת באותו זרם
# בדיוק כמו שורות ה-overload, כך שהסדר ביניהן נשמר.
_RF_START_RE = re.compile(r"AIRAM_RF stream=start(?!\w)")

_rf_lock = threading.Lock()
# ‏follower_since: מתי journalctl הנוכחי התחיל לקרוא (None = לא קוראים כרגע).
# ‏overload: True/False רק מראיה חיובית מהדרייבר (stream=start של הסשן, או קצה-
# מצב overload=1/0) — None כשהעוקב לא רץ, הצטרף באמצע סשן (‎-n 0 => אירועים
# שקדמו לו לא נראו), או שסשן חדש עוד לא אישר שהוא מדווח.
# ‏session_start: הגבול האחרון (reset שלנו או stream=start) — להבחנה בין "הצטרפנו
# באמצע" ל"סשן חדש שעוד לא אישר" ב-unknown_reason.
_rf = {"follower_since": None, "session_start": None, "overload": None,
       "overload_events": 0, "last_overload_t": None, "ifgr": None, "lna_grdb": None}
# אירועים עם חותמת זמן (לחלון של הקלטה בודדת — _rf_window_summary). כולל גם
# אירועי מחזור-חיים (reset/follow) כדי שאפשר יהיה לשחזר את המצב בתחילת חלון.
_rf_events = collections.deque(maxlen=RF_EVENTS_MAX)
# נקבע כשה-journalctl של העוקב כבר רץ. ‏-n 0 => מה שנכתב ליומן *לפני* שהוא עלה לא
# ייראה לעולם, ובראשם "AIRAM_RF stream=start" — הראיה היחידה ל"אין עומס". בלי
# ההמתנה הזאת _boot_restore (שעלה ראשון) היה מרים את rtl_airband לפני שהעוקב
# מחובר, והסשן הראשון אחרי אתחול היה נשאר "לא ידוע" עד הכיוונון הבא.
_rf_follow_attached = threading.Event()


def _rf_telemetry_available():
    """יש טלמטריית חומרה <=> install.sh בנה את SoapySDRPlay3 עם ה-patch (ר' SOAPY_RF_MARK)."""
    try:
        return SOAPY_RF_MARK.is_file()
    except OSError:
        return False


def _rf_session_reset(reason, now=None):
    """AIR-AM עומד להפעיל את rtl_airband מחדש: מאפסים מונים ו-overload=None — **לא**
    False. עוקב מחובר אינו ראיה שהוא רואה משהו (מודול לא-מתוקן, רמת log, הרשאות —
    ר' _RF_START_RE); False מגיע רק משורת stream=start של התהליך החדש (§12)."""
    now = time.time() if now is None else now
    with _rf_lock:
        following = _rf["follower_since"] is not None
        _rf.update(session_start=now, overload=None,
                   overload_events=0, last_overload_t=None, ifgr=None, lna_grdb=None)
        _rf_events.append({"t": now, "ev": "reset", "following": following, "reason": reason})


def _rf_stream_start(now):
    """שורת "AIRAM_RF stream=start" — סשן חדש *והדרייבר הוכיח שהוא מדווח*: מכאן
    "לא ראינו Overload_Detected" באמת אומר "אין עומס" (ה-Detected הבא יגיע באותו
    זרם, אחרי השורה הזו)."""
    with _rf_lock:
        _rf.update(session_start=now, overload=False,
                   overload_events=0, last_overload_t=None, ifgr=None, lna_grdb=None)
        _rf_events.append({"t": now, "ev": "start"})


def _rf_handle_line(line, now=None):
    """שורה אחת מהיומן => עדכון מצב הטלמטריה. מחזיר את סוג השורה
    ("start"/"overload"/"gain"/None) — לבדיקות. לעולם לא זורק על תוכן שורה.
    קצה overload=1/0 הוא בעצמו ראיה חיובית למצב הנוכחי (גם אחרי הצטרפות באמצע)."""
    now = time.time() if now is None else now
    if "AIRAM_RF" in line:
        if _RF_START_RE.search(line):
            _rf_stream_start(now)
            return "start"
        m = _RF_OVERLOAD_RE.search(line)
        if m:
            on = m.group(1) == "1"
            with _rf_lock:
                _rf["overload"] = on
                if on:
                    _rf["overload_events"] += 1
                    _rf["last_overload_t"] = now
                _rf_events.append({"t": now, "ev": "ovl", "on": on})
            return "overload"
        m = _RF_GAIN_RE.search(line)
        if m:
            # ערכים גולמיים מה-API — בלי סינון טווח: ה-spec (3.15) לא מגדיר טווח
            # ל-gRdB של GainChange, ולכן מסנן כאן היה סף מומצא (§12).
            grdb, lna_grdb = int(m.group(1)), int(m.group(2))
            with _rf_lock:
                _rf["ifgr"], _rf["lna_grdb"] = grdb, lna_grdb
                _rf_events.append({"t": now, "ev": "gain", "grdb": grdb, "lna_grdb": lna_grdb})
            return "gain"
    return None


def _rf_follow_once(popen=None):
    """הרצה אחת של journalctl -f עד שהוא יוצא. מחזיר כמה שניות רץ.
    ⚠ ‏stdin/stderr ל-DEVNULL: stderr לא נקרא, ובלי זה buffer מלא היה תוקע את
    journalctl. כל חריגה בפענוח שורה נבלעת — שורה רעה אחת לא מפילה את העוקב."""
    popen = popen or subprocess.Popen
    proc = popen(RF_JOURNAL_CMD, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                 stderr=subprocess.DEVNULL, text=True, bufsize=1, errors="replace")
    started = time.time()
    with _rf_lock:
        # הצטרפות באמצע סשן: ‏-n 0 => מה שקרה לפני עכשיו לא נראה => לא יודעים
        _rf.update(follower_since=started, overload=None, ifgr=None, lna_grdb=None)
        _rf_events.append({"t": started, "ev": "follow", "up": True})
    _rf_follow_attached.set()
    try:
        for line in proc.stdout:
            try:
                _rf_handle_line(line)
            except Exception:
                log.debug("טלמטריית RF: שורה לא פוענחה", exc_info=True)
    finally:
        now = time.time()
        with _rf_lock:
            _rf.update(follower_since=None, overload=None)
            _rf_events.append({"t": now, "ev": "follow", "up": False})
        _rf_follow_attached.clear()
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
    return now - started


def _rf_follower_loop(stop_evt=None):
    """thread רקע (מ-__main__): עוקב יחיד אחרי היומן של rtl_airband. journalctl
    שיצא (journald הופעל מחדש, נהרג) מופעל שוב עם backoff מעריכי; ריצה תקינה
    ארוכה מאפסת אותו. **לעולם לא מת** — כמו _transcribe_worker: גם סימן-בנייה
    שמופיע מאוחר יותר (התקנה בזמן ריצה) נתפס בלי restart."""
    stop_evt = stop_evt or threading.Event()
    backoff = RF_JOURNAL_BACKOFF_MIN
    warned = False
    while not stop_evt.is_set():
        if not _rf_telemetry_available():
            stop_evt.wait(RF_MARK_RECHECK_SEC)
            continue
        ran = 0.0
        try:
            ran = _rf_follow_once()
            warned = False
        except Exception as e:                    # אין journalctl / אין הרשאה וכו'
            if not warned:
                log.warning("טלמטריית RF: הפעלת journalctl נכשלה (%s) — ננסה שוב", e)
                warned = True
        if ran >= RF_JOURNAL_HEALTHY_SEC:
            backoff = RF_JOURNAL_BACKOFF_MIN
        stop_evt.wait(backoff)
        backoff = min(backoff * 2, RF_JOURNAL_BACKOFF_MAX)


def _rf_unknown_reason(telemetry, voice_live, snap):
    """*למה* overload לא ידוע (None כשהוא ידוע) — כדי שה-UI יאמר את הסיבה האמיתית
    ולא "ממתין לדרייבר" לכל מקרה (§12: פיצ'ר שלא יודע לומר למה הוא כבוי):
      no_telemetry       — הדרייבר נבנה בלי ה-patch (אין סימן-בנייה).
      voice_not_live     — rtl_airband לא רץ (standby/מצב אחר/נכשל).
      follower_down      — journalctl לא קורא כרגע (ר' _rf_follower_loop).
      joined_mid_session — העוקב התחיל אחרי תחילת הסשן הנוכחי (למשל airam-web
                           הופעל מחדש בזמן שהקול רץ) ועוד לא הגיע קצה-מצב;
                           כוונון מחדש יפתח סשן עם stream=start.
      no_driver_evidence — סשן שהתחיל בזמן שהעוקב קרא, ושורת stream=start שלו לא
                           הגיעה: רגעי מיד אחרי כוונון; מתמשך => הדרייבר לא מדווח."""
    if not telemetry:
        return "no_telemetry"
    if not voice_live:
        return "voice_not_live"
    if snap["follower_since"] is None:
        return "follower_down"
    if snap["overload"] is not None:
        return None
    ss = snap["session_start"]
    if ss is None or ss < snap["follower_since"]:
        return "joined_mid_session"
    return "no_driver_evidence"


def _rf_metrics(voice_live, st):
    """אובייקט "rf" ל-/api/metrics (חוזה PR 1, פריט 5). ‏overload/ifgr/lna_grdb
    הם None ("לא ידוע") כשאין טלמטריה, כשהקול לא רץ, או כשהעוקב לא קורא — לעולם
    לא False מומצא; ‏unknown_reason אומר למה (ר' _rf_unknown_reason). ‏overload_events
    תמיד int (חוזה) — משמעותי רק כש-overload אינו None.
    ‏lna_state/fm_notch/agc — הקונפיגורציה המבוקשת (state), לא מדידה."""
    telemetry = _rf_telemetry_available()
    now = time.time()
    with _rf_lock:
        snap = dict(_rf)
    known = telemetry and voice_live and snap["follower_since"] is not None
    last = snap["last_overload_t"]
    try:
        lna_state = int(st.get("rf_gain", RF_GAIN_DEFAULT))
    except (TypeError, ValueError):
        lna_state = RF_GAIN_DEFAULT
    return {"telemetry": telemetry,
            "overload": snap["overload"] if known else None,
            "overload_events": snap["overload_events"] if known else 0,
            "last_overload_age": round(now - last, 1) if (known and last is not None) else None,
            "ifgr": snap["ifgr"] if known else None,
            "lna_grdb": snap["lna_grdb"] if known else None,
            "lna_state": lna_state,
            "fm_notch": bool(st.get("fm_notch", False)),
            "agc": bool(st.get("agc", True)),
            "unknown_reason": _rf_unknown_reason(telemetry, voice_live, snap)}


def _rf_window_summary(start, end):
    """טלמטריה בחלון [start, end] (שידור בודד). None כשאין כיסוי מלא: העוקב לא
    רץ ברציפות מ*לפני* start, או שאירועי החלון כבר נדחקו מה-deque — "לא ראינו"
    אינו "לא היה" (§12). אחרת:
      overload_at_start — מצב העומס בתחילת החלון: False רק אחרי stream=start של
                          הסשן (עוגן "reset"/"follow" => None — ר' _RF_START_RE),
      overload_events   — Overload_Detected בתוך החלון,
      overload          — היה עומס בנקודה כלשהי בחלון (None כשאי אפשר לדעת).
                          קצה Overload_Corrected בתוך החלון מוכיח עומס *לפניו*
                          => True גם כשמצב ההתחלה לא ידוע,
      ifgr_min/max      — מהערך שבתוקף בתחילת החלון + אירועי GainChange בתוכו
                          (ה-patch מגביל אותם ל-≤1/ש' — ערכי-ביניים בפרץ לא נראים)."""
    with _rf_lock:
        since = _rf["follower_since"]
        events = list(_rf_events)
        full = len(_rf_events) == _rf_events.maxlen
    if since is None or since > start:
        return None
    if full and (not events or events[0]["t"] > start):
        return None                      # תחילת החלון נדחקה — ספירה חלקית היא לא ספירה
    ovl, ifgr, anchored = None, None, False
    corrected = broken = False
    n_ovl = n_gain = 0
    grdbs = []
    for e in events:
        if e["t"] > end:
            break
        ev = e["ev"]
        if e["t"] <= start:
            if ev == "follow":
                anchored = e["up"]
                ovl, ifgr = None, None
            elif ev == "reset":
                anchored = True
                ovl, ifgr = None, None   # AIR-AM הפעיל מחדש — False רק מ-stream=start
            elif ev == "start":
                anchored = True
                ovl, ifgr = False, None
            elif ev == "ovl":
                ovl = e["on"]
            elif ev == "gain":
                ifgr = e["grdb"]
            continue
        if ev == "ovl":
            if e["on"]:
                n_ovl += 1
            else:
                corrected = True         # Overload_Corrected => היה עומס לפניו, בתוך החלון
        elif ev == "gain":
            n_gain += 1
            grdbs.append(e["grdb"])
        elif ev in ("reset", "start", "follow"):
            broken = True                # גבול-סשן בתוך שידור — "אין קצה" כבר לא אומר "אין עומס"
    if not anchored:
        ovl, ifgr = None, None           # העוגן נדחק — מצב תחילת החלון לא ידוע
    vals = grdbs + ([ifgr] if ifgr is not None else [])
    if n_ovl or corrected or ovl:
        during = True
    elif ovl is False and not broken:
        during = False
    else:
        during = None
    return {"overload_at_start": ovl, "overload_events": n_ovl, "overload": during,
            "gain_events": n_gain, "ifgr_at_start": ifgr,
            "ifgr_min": min(vals) if vals else None, "ifgr_max": max(vals) if vals else None}


def _read_voice_metrics():
    """מדדי RF רציפים לתדר הנוכחי מ-rtl_airband stats. מקור אמת יחיד לפענוח
    הקובץ — משותף ל-/api/metrics (תצוגת קול) ול-/api/signal (מד השדה המאוחד,
    מצב voice). rtl_airband מרענן את הקובץ כל ~1 שנייה."""
    try:
        age = time.time() - STATS_PATH.stat().st_mtime
        text = STATS_PATH.read_text()
    except OSError:
        return {"fresh": False, "age": None, "signal": None, "noise": None,
                "snr": None, "squelch_opens": None, "counters": None, "flappy": None}

    want = f"{load_state()['freq']:.3f}"       # מדדים מתויגים freq=MHz ב-3 ספרות
    vals = parse_stats(text, want)

    sig = vals.get("channel_dbfs_signal_level")
    noise = vals.get("channel_dbfs_noise_level")
    snr = round(sig - noise, 1) if (sig is not None and noise is not None) else None
    # ⚠ אין כאן "overload": רמת הערוץ (אחרי ה-AGC, bin יחיד) לא רואה רוויה של
    # ה-ADC/LNA — ר' הערת ההסרה של OVERLOAD_DBFS ו-_rf_metrics.
    return {"fresh": (age <= STATS_MAX_AGE and snr is not None), "age": round(age, 1),
            "signal": sig, "noise": noise, "snr": snr,
            "squelch_opens": vals.get("channel_squelch_counter"),
            "counters": parse_counters(text), "flappy": vals.get("channel_flappy_counter")}


@app.route("/api/metrics")
def api_metrics():
    """מדדי RF חיים לתדר הנוכחי. rtl_airband מרענן את הקובץ כל ~1 שנייה.
    ‏rf — טלמטריית החומרה (_rf_metrics). ‏overload ברמה העליונה = rf.overload
    (תאימות לקליינט ישן): True/False מאירועי החומרה, None = לא ידוע."""
    # ‏systemctl is-active רק כשיש טלמטריה בכלל — בלעדיה התשובה ממילא "לא ידוע",
    # ואין טעם ב-fork נוסף בכל פולינג (~1ש') של תצוגת הקול.
    rf = _rf_metrics(_rf_telemetry_available() and _is_active("rtl_airband"), load_state())
    return jsonify(ok=True, overload=rf["overload"], rf=rf, **_read_voice_metrics())


def _baseline_tag(lna, fm_notch):
    """הקצה הקדמי שבו בסיס נמדד / שבו המדידה הנוכחית רצה. רצפת הרעש ב-dBFS
    נמדדת *אחרי* ה-LNA ומסנן ה-FM (שניהם לפני ה-AGC), ולכן בסיס בר-השוואה רק
    למדידה באותו LNA ובאותו מצב מסנן. ‏agc נשמר לתיעוד — הבדיקה תמיד תחת AGC."""
    return {"lna": int(lna), "fm_notch": bool(fm_notch), "agc": True}


def _verdict_reason(noise, baseline, frontend=None):
    """*למה* אין פסק-דין (או None כשיש): ‏'no_reading' / 'no_baseline' /
    ‏'baseline_untagged' (בסיס מלפני v2.26.0 — נמדד כשה-LNA תחת AGC היה 0 בפועל,
    ר' _device_string, ואין לדעת אם הוא בר-השוואה) / 'baseline_config_mismatch'
    (נמדד ב-LNA/מסנן אחרים). ‏frontend=None => בלי בדיקת תיוג (קוראים פנימיים
    בלבד — כל מסלול API מעביר frontend)."""
    if noise is None:
        return "no_reading"
    if not baseline or baseline.get("noise") is None:
        return "no_baseline"
    if frontend is not None:
        if "lna" not in baseline or "fm_notch" not in baseline:
            return "baseline_untagged"
        if (baseline.get("lna") != frontend.get("lna")
                or bool(baseline.get("fm_notch")) != bool(frontend.get("fm_notch"))):
            return "baseline_config_mismatch"
    return None


def _signal_verdict(noise, baseline, frontend=None):
    """פסק דין *רק* מול בסיס שהמשתמש כייל בעצמו — לעולם לא סף איכות מומצא
    (§12 ב-CLAUDE.md). ‏noise=None (אין מדידה נוכחית) => 'unknown'. בלי בסיס
    כלל => 'no_baseline' — לא ניחוש. בסיס שנמדד בקצה-קדמי אחר (או בלי תיוג)
    => גם 'no_baseline' (הסיבה ב-_verdict_reason): השוואה בין LNA שונים היא
    בדיוק פסק-הדין המומצא ש-§12 אוסר — רצפת הרעש ב-dBFS נמדדת אחרי ה-LNA, ושינוי
    LNA לבדו (הפחתה לא-לינארית של כמה dB לצעד, ר' הערת RFGR_MIN; כמה ממנו ה-AGC
    מפצה ב-IF — לא נמדד) עלול להיראות כמו "ירידה מהבסיס" או להסתיר אחת.
    אחרת משווים מול DISCONNECT_DROP_DB."""
    reason = _verdict_reason(noise, baseline, frontend)
    if reason == "no_reading":
        return "unknown"
    if reason is not None:
        return "no_baseline"
    return "below_baseline" if (baseline["noise"] - noise) >= DISCONNECT_DROP_DB else "ok"


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
    st = load_state()
    mode = _live_mode()
    baseline = st.get("signal_baseline")
    payload = {"ok": True, "mode": mode or "off", "verdict_reason": None}

    if mode == "voice":
        m = _read_voice_metrics()
        agc_ok = bool(st.get("agc", True))   # gain ידני => לא בר-השוואה לבסיס (§12: לא משווים תפוחים לתפוזים)
        # הקצה הקדמי של הקול שרץ — אותה גזירה בדיוק כמו בבדיקת האנטנה (_probe_frontend),
        # כך שבסיס שכויל בקונפיג הנוכחי בר-השוואה, וכל שינוי LNA/מסנן מבטל אותו בגלוי.
        frontend = _baseline_tag(*_probe_frontend(st))
        noise = m["noise"] if agc_ok else None
        payload.update(kind="continuous", fresh=m["fresh"], age=m["age"],
                       signal=m["signal"], noise=m["noise"], snr=m["snr"],
                       baseline=baseline, frontend=frontend,
                       verdict=_signal_verdict(noise, baseline, frontend),
                       verdict_reason=(_verdict_reason(noise, baseline, frontend)
                                       if agc_ok else "manual_gain"))
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
            _enter_acars(prev_state.get("acars_freqs", ACARS_FREQS_DEFAULT))
        elif prev_live == "vdl2":
            _enter_vdl2(prev_state.get("vdl2_freqs", VDL2_FREQS_DEFAULT))
        elif prev_live == "satcom":
            _enter_satcom(prev_state.get("satcom_freqs", SATCOM_FREQS_DEFAULT),
                          bias_tee=prev_state.get("satcom_bias_tee", True),
                          skip_c=prev_state.get("satcom_skip_c", True),
                          spectrum=prev_state.get("satcom_spectrum", True),
                          gain=_sanitize_satcom_gain(prev_state.get("satcom_gain")))
        elif prev_live == "voice":
            _enter_voice({"freq": prev_state["freq"], "mod": prev_state["mod"],
                         "agc": prev_state["agc"], "if_gain": prev_state["if_gain"],
                         "rf_gain": prev_state["rf_gain"],
                         "fm_notch": bool(prev_state.get("fm_notch", False)),
                         "squelch_mode": prev_state["squelch_mode"],
                         "squelch_snr": prev_state["squelch_snr"]})
        else:
            _enter_standby()
    except Exception:
        log.warning("בדיקת אנטנה: שחזור המצב הקודם (%s) נכשל", prev_live, exc_info=True)


def _probe_frontend(st):
    """(lna, fm_notch) שבהם בדיקת האנטנה (והניסוי) מודדים — **הקצה הקדמי השמור של
    הקול** (state["rf_gain"]/["fm_notch"]), לא ערך קבוע.
    ⚠ למה לא RF_GAIN_DEFAULT קבוע: מאז v2.26.0 ה-LNA חל גם תחת AGC, ורצפת הרעש
    ב-dBFS תלויה בו ישירות. בסיס שנמדד ב-LNA קבוע היה בר-השוואה *רק* לקול שרץ
    באותו LNA — כלומר משתמש שבחר LNA אחר לאתר שלו (בדיוק מה שהתוכנית מבקשת,
    docs/voice-rf-quality-plan.md §3) לא היה מקבל פסק-דין בקול **לעולם**, גם אחרי
    כיול. מדידה בקצה הקדמי של המשתמש + תיוג הבסיס (_baseline_tag) נותנים פסק-דין
    בכל קונפיגורציה, ושינוי LNA/מסנן מבטל בגלוי את ההשוואה ("no_baseline" +
    reason) במקום להשוות בשקט בין קצוות-קדמיים שונים (§12)."""
    try:
        lna = max(RFGR_MIN, min(RFGR_MAX, int(st.get("rf_gain", RF_GAIN_DEFAULT))))
    except (TypeError, ValueError):
        lna = RF_GAIN_DEFAULT
    return lna, bool(st.get("fm_notch", False))


def _probe_params(freq, lna=RF_GAIN_DEFAULT, fm_notch=False):
    """תנאי המדידה של בדיקת האנטנה (AGC, סקוולץ' פתוח, AM, LNA+מסנן כפי שנמסרו —
    ר' _probe_frontend) — מקור-אמת יחיד ל-/api/antenna/check ולניסוי האוטומטי,
    כדי שהניסוי יבדוק *את* המסלול של המוצר."""
    return {"freq": freq, "mod": "am", "agc": True, "if_gain": IF_GAIN_DEFAULT,
            "rf_gain": int(lna), "fm_notch": bool(fm_notch),
            "squelch_mode": "open", "squelch_snr": SNR_DEFAULT}


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
    prev = load_state()
    try:
        freq = float(data.get("freq"))
        if not (0.1 <= freq <= 1999.5):
            raise ValueError
    except (TypeError, ValueError):
        freq = prev.get("freq", DEFAULT_STATE["freq"])
    calibrate = bool(data.get("calibrate"))

    if not TUNE_LOCK.acquire(blocking=False):
        return jsonify(ok=False, error="פעולה אחרת מתבצעת כרגע — המתן שנייה ונסה שוב"), 409
    try:
        prev_live = _live_mode()
        # ⚠ "כבר בקול" לא מספיק כדי לדלג על ההכנה: הדגימה חייבת להיעשות *באותם
        # תנאים* שבהם נמדד הבסיס, אחרת ההשוואה של _signal_verdict חסרת משמעות.
        # רצפת הרעש ב-dBFS נמדדת אחרי שרשרת הרווח => משתמש שיושב בקול עם רווח
        # ידני (IFGR שונה מברירת המחדל) היה מקבל רצפה שונה בעשרות dB מהבסיס
        # שנמדד תחת AGC, ו-verdict של "below_baseline" (= "האנטנה מנותקת!") בלי
        # שדבר באמת השתנה. זו בדיוק ההמצאה שעקרון §12 אוסר, רק בכיוון של
        # פסק-דין במקום ערך. לכן מדלגים רק כשהמצב החי *זהה* לתנאי הבדיקה.
        lna, fm_notch = _probe_frontend(prev)
        probe = _probe_params(freq, lna, fm_notch)
        # ‏rf_gain/fm_notch משתתפים בהשוואה כי הם משנים את רצפת הרעש (LNA לפני
        # ה-AGC); היום הם נגזרים מ-prev ולכן תמיד שווים, אבל ההשוואה המפורשת
        # שומרת על הנכונות אם _probe_frontend ישתנה.
        already_voice = (prev_live == "voice"
                         and abs(prev.get("freq", -999.0) - freq) < 5e-4
                         and prev.get("mod") == "am"
                         and bool(prev.get("agc")) is True
                         and prev.get("squelch_mode") == "open"
                         and prev.get("rf_gain") == probe["rf_gain"]
                         and bool(prev.get("fm_notch", False)) == probe["fm_notch"])
        if not already_voice:
            err, detail, _sdr_down = _enter_voice(probe)
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
            _rflog_event({"ev": "probe", "freq": freq, "calibrate": calibrate, "lna": lna,
                          "fm_notch": fm_notch, "already_voice": already_voice,
                          "error": "no_fresh_stats"})
            return jsonify(ok=False, error="לא התקבלו מדדים מה-SDR בזמן — נסה שוב"), 504

        frontend = _baseline_tag(lna, fm_notch)
        if calibrate:
            baseline = {"noise": result["noise"], "freq": freq, "ts": time.time(), **frontend}
            save_state({**load_state(), "signal_baseline": baseline})
        else:
            baseline = prev.get("signal_baseline")
        verdict = _signal_verdict(result["noise"], baseline, frontend)
        reason = _verdict_reason(result["noise"], baseline, frontend)
        _rflog_event({"ev": "probe", "freq": freq, "calibrate": calibrate, "lna": lna,
                      "fm_notch": fm_notch, "already_voice": already_voice,
                      "noise": result["noise"], "signal": result["signal"], "verdict": verdict,
                      "verdict_reason": reason, "baseline_noise": (baseline or {}).get("noise")})
        return jsonify(ok=True, freq=freq, calibrated=calibrate, baseline=baseline,
                       verdict=verdict, verdict_reason=reason, lna=lna, fm_notch=fm_notch,
                       **result)
    finally:
        TUNE_LOCK.release()


# --- 🩺 בדיקת RF: מעבר על מצבי LNA מעל ATIS -----------------------------------
# docs/voice-rf-quality-plan.md (PR 2, הגרסה הפשוטה). השאלה: "יש עומס? באיזה LNA
# לשים?". נמדד על ATIS נתב"ג — נשא *רציף*: אין שקט בין שידורים ואין "דוברים שונים"
# בין המצבים, כך שהשוואת SNR בין מצבי LNA משווה את אותו אות. העומס (אם יש) נגרם
# מסביבת ה-RF — כל החלון והחזית, לא הערוץ עצמו — ולכן ⚠ ההנחה (מוצהרת גם ב-UI) היא
# שסביבת ה-RF ב-132.5 דומה לזו של תדר המגדל, ‏2MHz משם. אותו מסלול כמו בדיקת
# האנטנה (_probe_params/_enter_voice/_restore_after_probe), בלי שירות/תלות חדשים.
RFCHECK_FREQ = 132.5               # ATIS נתב"ג (= ברירת המחדל של config/airband.conf)
RFCHECK_STATES = (0, 2, 4, 6, 8)   # מצבי LNA (0 = רווח מרבי ... 8 = ‎57dB הנחתה ב-RSP1B)
RFCHECK_SETTLE_SEC = 2.0           # אחרי הדגימה הראשונה — ה-AGC של ה-IF מתייצב
RFCHECK_MEASURE_SEC = 5.0          # חלון המדידה לכל מצב (~5 כתיבות stats, ‏1Hz)
RFCHECK_START_TIMEOUT_SEC = 15.0   # עד שדגימה של התהליך *החדש* מופיעה ב-stats

_rfc_lock = threading.Lock()
_rfc = {"running": False, "started_at": None, "finished_at": None, "step": None,
        "rows": [], "error": None, "stop": None}


def _read_stats_snapshot(want):
    """(mtime, vals) של קובץ ה-stats לתדר want, או (None, {}) כשאין קובץ."""
    try:
        mtime = STATS_PATH.stat().st_mtime
        text = STATS_PATH.read_text()
    except OSError:
        return None, {}
    return mtime, parse_stats(text, want)


def _spread(vals):
    """פיזור המדידות (IQR; עם פחות מ-4 דגימות — max−min). זה ה"שוויון" של ההמלצה:
    הפרש SNR קטן מהרעש של המדידה עצמה אינו הבדל — נגזר מהנתונים, לא סף מומצא (§12)."""
    if len(vals) < 2:
        return None
    s = sorted(vals)
    if len(s) < 4:
        return round(s[-1] - s[0], 1)
    q = statistics.quantiles(s, n=4, method="inclusive")   # exclusive ב-5 דגימות נותן לחריג לנפח את ה-IQR
    return round(q[2] - q[0], 1)


def _rfcheck_measure(lna, fm_notch, stop_evt):
    """מצב LNA אחד: כניסה לקול על ATIS (AGC, סקוולץ' פתוח), המתנה לדגימה של התהליך
    החדש, התייצבות, ואז חלון מדידה. שורה עם SNR (חציון) ועומס מהטלמטריה של PR 1
    (None = לא ידוע — לעולם לא "תקין" בהיעדר ראיה)."""
    row = {"lna": lna, "snr": None, "snr_spread": None, "signal": None, "noise": None,
           "samples": 0, "overload": None, "overload_events": None,
           "ifgr_min": None, "ifgr_max": None, "error": None}
    err, _detail, sdr_down = _enter_voice(_probe_params(RFCHECK_FREQ, lna, fm_notch))
    if err:
        row["error"] = err
        row["sdr_down"] = bool(sdr_down)   # ה-SDR לא נוכח — אין טעם להמשיך למצב הבא
        return row
    want = f"{RFCHECK_FREQ:.3f}"
    # ⚠ לא כל דגימה טרייה: ה-flush של התהליך *הקודם* נכתב אחרי הקונפיג החדש (ר'
    # _rtl_airband_start_wall). מקבלים רק כתיבות מאחרי עליית התהליך הנוכחי.
    born = _rtl_airband_start_wall() or time.time()
    deadline = time.time() + RFCHECK_START_TIMEOUT_SEC
    while time.time() < deadline and not stop_evt.is_set():
        mtime, vals = _read_stats_snapshot(want)
        if mtime is not None and mtime > born and vals.get("channel_dbfs_noise_level") is not None:
            break
        stop_evt.wait(0.3)
    else:
        if not stop_evt.is_set():
            row["error"] = "לא התקבלו מדדים מה-SDR בזמן"
        return row
    if stop_evt.wait(RFCHECK_SETTLE_SEC):
        return row
    t0 = time.time()
    seen, snrs, sigs, noises = None, [], [], []
    while time.time() - t0 < RFCHECK_MEASURE_SEC and not stop_evt.is_set():
        mtime, vals = _read_stats_snapshot(want)
        if mtime is not None and mtime != seen and mtime > born:
            seen = mtime
            sig = vals.get("channel_dbfs_signal_level")
            noise = vals.get("channel_dbfs_noise_level")
            if sig is not None and noise is not None:
                sigs.append(sig)
                noises.append(noise)
                snrs.append(sig - noise)
        stop_evt.wait(0.3)
    t1 = time.time()
    if snrs:
        row.update(snr=round(statistics.median(snrs), 1), snr_spread=_spread(snrs),
                   signal=round(statistics.median(sigs), 1),
                   noise=round(statistics.median(noises), 1), samples=len(snrs))
    tele = _rf_window_summary(t0, t1)   # None = העוקב לא כיסה את החלון => לא ידוע
    if tele:
        row.update(overload=tele["overload"], overload_events=tele["overload_events"],
                   ifgr_min=tele["ifgr_min"], ifgr_max=tele["ifgr_max"])
    return row


def _rfcheck_recommend(rows):
    """המלצה מהשורות — פונקציה טהורה. הכלל (אושר ע"י המשתמש): עומס *מוכח* פוסל
    מצב; מבין השאר — SNR הגבוה ביותר; כשההפרש מהמיטבי קטן מפיזור המדידה (שוויון)
    — בוחרים את המצב עם *יותר* הנחתה (מרווח מעומס, ליד שדה תעופה). עומס לא-ידוע
    (None) לא פוסל, אבל מוריד את ההמלצה ל-"partial" — לא טוענים "אין עומס" בלי ראיה."""
    valid = [r for r in rows if r.get("snr") is not None and not r.get("error")]
    if not valid:
        return {"rf_gain": None, "reason": "no_data", "confidence": None}
    clean = [r for r in valid if r.get("overload") is not True]
    if not clean:
        return {"rf_gain": None, "reason": "all_overloaded", "confidence": "full"}
    best = max(clean, key=lambda r: r["snr"])

    def tie(r):
        tol = max(best.get("snr_spread") or 0.0, r.get("snr_spread") or 0.0)
        return best["snr"] - r["snr"] <= tol

    # הולכים מהמיטבי לכיוון יותר הנחתה ועוצרים במצב הראשון שאינו בשוויון — לא "קופצים"
    # מעל מצב ביניים גרוע בבירור אל מצב רחוק שבמקרה יצא בשוויון.
    rec = best
    for r in sorted((r for r in clean if r["lna"] > best["lna"]), key=lambda r: r["lna"]):
        if not tie(r):
            break
        rec = r
    known = all(r.get("overload") is not None for r in valid)
    overloaded = sorted(r["lna"] for r in valid if r.get("overload") is True)
    return {"rf_gain": rec["lna"], "reason": "tie_more_attenuation" if rec is not best else "best_snr",
            "confidence": "full" if known else "partial", "best_snr_lna": best["lna"],
            "overloaded": overloaded}


def _rfcheck_run(prev, prev_live, stop_evt):
    """thread: מחזיק את TUNE_LOCK לכל אורך הבדיקה (כמו הניסוי האוטומטי); תמיד משחזר
    את מה שרץ קודם ומשחרר ב-finally — גם בביטול/שגיאה."""
    rows, err = [], None
    lna0, fm_notch = _probe_frontend(prev)
    try:
        for i, lna in enumerate(RFCHECK_STATES):
            if stop_evt.is_set():
                break
            with _rfc_lock:
                _rfc["step"] = i
            rows.append(_rfcheck_measure(lna, fm_notch, stop_evt))
            with _rfc_lock:
                _rfc["rows"] = list(rows)
            if rows[-1].get("sdr_down"):
                # בלי זה כל מצב נוסף היה מחכה ~45ש' ל-_enter_voice — דקות של TUNE_LOCK תפוס
                err = "ה-SDR לא נמצא — בדוק את חיבור ה-USB"
                break
    except Exception:
        log.warning("🩺 בדיקת RF נכשלה", exc_info=True)
        err = "הבדיקה נכשלה — ר' journalctl -u airam-web"
    finally:
        aborted = stop_evt.is_set()
        try:
            _restore_after_probe(prev, prev_live)
            if not aborted and not err:
                result = {"ts": time.time(), "freq": RFCHECK_FREQ, "fm_notch": fm_notch,
                          "lna_before": lna0, "agc": bool(prev.get("agc", True)),
                          "rows": rows, "recommendation": _rfcheck_recommend(rows)}
                save_state({**load_state(), "rf_check_last": result})
                _rflog_event({"ev": "rfcheck", **result})
        except Exception:
            log.warning("🩺 בדיקת RF: שחזור/שמירה נכשלו", exc_info=True)
            err = err or "שחזור המצב הקודם נכשל — היכנס למצב ידנית"
        finally:
            TUNE_LOCK.release()
            with _rfc_lock:
                _rfc.update(running=False, finished_at=time.time(), step=None,
                            error=("בוטל" if aborted and not err else err))


def _rfcheck_status():
    with _rfc_lock:
        s = {k: _rfc[k] for k in ("running", "started_at", "finished_at", "step", "rows", "error")}
    s.update(freq=RFCHECK_FREQ, states=list(RFCHECK_STATES),
             est_sec=int(len(RFCHECK_STATES) * (7 + RFCHECK_SETTLE_SEC + RFCHECK_MEASURE_SEC)),   # restart+אימות ~7ש'
             result=load_state().get("rf_check_last"))
    return s


@app.route("/api/rfcheck", methods=["GET", "POST"])
def api_rfcheck():
    """GET: מצב/תוצאה. POST {action}: start | abort | apply. דרך _guard (POST)."""
    if request.method == "GET":
        return jsonify(ok=True, **_rfcheck_status())
    action = (request.get_json(silent=True) or {}).get("action")
    if action == "abort":
        with _rfc_lock:
            if _rfc["running"] and _rfc["stop"]:
                _rfc["stop"].set()
        return jsonify(ok=True, **_rfcheck_status())
    if action == "apply":
        return _rfcheck_apply()
    if action != "start":
        return jsonify(ok=False, error="פעולה לא מוכרת"), 400
    with _rfc_lock:
        if _rfc["running"]:
            return jsonify(ok=False, error="הבדיקה כבר רצה"), 409
    with _exp_lock:
        if _exp["running"]:
            return jsonify(ok=False, error="ניסוי הכיול רץ — המתן לסיומו"), 409
    if not TUNE_LOCK.acquire(blocking=False):
        return jsonify(ok=False, error="פעולה אחרת מתבצעת כרגע — נסה שוב בעוד רגע"), 409
    try:
        # תחת הנעילה: מעבר מצב/סריקה שהסתיימו רגע לפני כן היו נותנים prev_live ישן
        # (שחזור למצב הלא-נכון בסוף הבדיקה)
        with _scan_lock:
            scanning = _scan_thread is not None and _scan_thread.is_alive()
        prev_live = _live_mode()
        if scanning or prev_live == "satcom":
            TUNE_LOCK.release()
            return jsonify(ok=False, error=("עצור את הסריקה לפני הבדיקה" if scanning else
                                            "SATCOM פעיל — עצור אותו וחבר את אנטנת ה-VHF")), 409
        stop_evt = threading.Event()
        with _rfc_lock:
            _rfc.update(running=True, started_at=time.time(), finished_at=None, step=0,
                        rows=[], error=None, stop=stop_evt)
        threading.Thread(target=_rfcheck_run, args=(load_state(), prev_live, stop_evt),
                         daemon=True).start()
    except Exception:
        TUNE_LOCK.release()
        with _rfc_lock:
            _rfc["running"] = False
        raise
    return jsonify(ok=True, **_rfcheck_status())


def _rfcheck_apply():
    """מחיל את ה-LNA המומלץ. בקול חי — דרך _voice_tune (אותו חוזה ורולבק כמו /api/tune);
    אחרת רק נשמר ב-state ויחול בכניסה הבאה לקול. מסרב כשהתוצאה כבר לא מתאימה להגדרות
    (מסנן FM השתנה / רווח ידני — נמדד תחת AGC), במקום להחיל מדידה של קצה-קדמי אחר."""
    with _rfc_lock:
        if _rfc["running"]:
            return jsonify(ok=False, error="הבדיקה עדיין רצה"), 409
    st = load_state()
    res = st.get("rf_check_last") or {}
    rec = (res.get("recommendation") or {}).get("rf_gain")
    if rec is None:
        return jsonify(ok=False, error="אין המלצה להחיל"), 409
    if "fm_notch" in res and bool(res["fm_notch"]) != bool(st.get("fm_notch", False)):
        return jsonify(ok=False, error="מסנן ה-FM השתנה מאז הבדיקה — הרץ אותה שוב"), 409
    if not st.get("agc", True):
        return jsonify(ok=False, error="הבדיקה נמדדה תחת AGC — הדלק AGC או קבע את ה-LNA ידנית"), 409
    if int(st.get("rf_gain", RF_GAIN_DEFAULT)) == int(rec):
        return jsonify(ok=False, error="ההמלצה כבר בתוקף"), 409
    if st.get("app_mode") == "voice" and _live_mode() == "voice":
        # _voice_tune תופס את TUNE_LOCK וממזג על state טרי; params מ-_parse_tune כדי
        # שכיוונון שהושלם רגע לפני לא יידרס בערכים ישנים (פרט ל-rf_gain עצמו)
        params, perr = _parse_tune({**st, "rf_gain": int(rec)})
        if perr:
            return jsonify(ok=False, error=perr), 400
        payload, status = _voice_tune(params)
        return jsonify(payload), status
    if not TUNE_LOCK.acquire(blocking=False):
        return jsonify(ok=False, error="פעולה אחרת מתבצעת כרגע — נסה שוב בעוד רגע"), 409
    try:
        save_state({**load_state(), "rf_gain": int(rec)})   # טרי, תחת הנעילה
    finally:
        TUNE_LOCK.release()
    return jsonify(ok=True, applied=int(rec), note="נשמר — יחול בכניסה הבאה לקול")


# --- רשם ניסוי RF ------------------------------------------------------------
_rflog_lock = threading.Lock()
_rflog = {"active": False, "started_at": None, "until": None, "rows": 0,
          "marks": 0, "write_errors": 0, "thread": None, "stop": None}

_CONF_FREQ_RE = re.compile(r"^\s*freq\s*=\s*([0-9.]+)\s*;", re.M)          # לא centerfreq
_CONF_CENTER_RE = re.compile(r"^\s*centerfreq\s*=\s*([0-9.]+)\s*;", re.M)
_CONF_GAIN_RE = re.compile(r'^\s*gain\s*=\s*"IFGR=(\d+),RFGR=(\d+)"', re.M)
_CONF_SQ_RE = re.compile(r"^\s*squelch_snr_threshold\s*=\s*(-?[0-9.]+)\s*;", re.M)
_CONF_MOD_RE = re.compile(r'^\s*modulation\s*=\s*"(\w+)"', re.M)
_CONF_DEVSTR_RE = re.compile(r'^\s*device_string\s*=\s*"([^"]*)"', re.M)


def _conf_device_kwargs(text):
    """ה-kwargs של device_string (‏"driver=sdrplay,rfnotch_ctrl=false,...") כמילון,
    בפענוח זהה ל-SoapySDR (פסיקים בין זוגות, '=' ראשון מפריד) — {} כשאין שורה."""
    m = _CONF_DEVSTR_RE.search(text)
    out = {}
    if not m:
        return out
    for part in m.group(1).split(","):
        k, sep, v = part.partition("=")
        if sep and k.strip():
            out[k.strip()] = v.strip()
    return out


def _parse_airband_conf(text):
    """הקונפיג *שבאמת רץ* מתוך airband.conf שכתב render_config. בלי שורת gain
    = AGC (כך render_config מבקש אותו — ר' שם); squelch=None = אוטומטי.
    ‏lna = מצב ה-LNA שבאמת הוחל: RFGR ברווח ידני, rfgain_sel תחת AGC. קונפיג ישן
    (מלפני v2.26.0) בלי rfgain_sel תחת AGC => 0 — לא ניחוש: זה ה-LNAstate של
    ברירת-המחדל של ה-API (sdrplay_api_tuner.h:63) שאף אחד לא דרס (ר' _device_string).
    באותו אופן rfnotch_ctrl חסר => False (‏rfNotchEnable default 0, sdrplay_api_rsp1a.h:13)."""
    out = {}
    m = _CONF_FREQ_RE.search(text)
    if m:
        try:
            out["freq"] = float(m.group(1))
        except ValueError:
            pass
    g = _CONF_GAIN_RE.search(text)
    out["agc"] = g is None
    out["ifgr"] = int(g.group(1)) if g else None
    out["rfgr"] = int(g.group(2)) if g else None
    q = _CONF_SQ_RE.search(text)
    out["squelch_snr"] = float(q.group(1)) if q else None
    mm = _CONF_MOD_RE.search(text)
    out["mod"] = mm.group(1) if mm else None
    kw = _conf_device_kwargs(text)
    if g:
        out["lna"] = out["rfgr"]
    else:
        try:
            out["lna"] = int(kw["rfgain_sel"]) if "rfgain_sel" in kw else 0
        except ValueError:
            out["lna"] = None             # ערך לא-מספרי: לא יודעים מה הדרייבר עשה איתו
    # ‏writeSetting: "false" => 0, *כל* ערך אחר => 1 (Settings.cpp:1748) — אותה סמנטיקה
    out["fm_notch"] = kw["rfnotch_ctrl"] != "false" if "rfnotch_ctrl" in kw else False
    return out


def _rflog_sample(prev_sig):
    """דגימה אחת: מחזיר (row, sig). row=None כשאין כתיבה חדשה של ה-stats או של
    הקונפיג (אותה חתימת mtime) — כך כל שורה בקובץ היא מדידה חדשה, ובמצבים
    שאינם קול (stats קפוא) לא נכתב כלום."""
    try:
        st = STATS_PATH.stat()
        text = STATS_PATH.read_text()
    except OSError:
        return None, prev_sig
    try:
        cst = CONFIG_PATH.stat()
        conf = _parse_airband_conf(CONFIG_PATH.read_text())
    except OSError:
        cst, conf = None, {}
    sig = (st.st_mtime, cst.st_mtime if cst else None)
    if sig == prev_sig:
        return None, prev_sig
    # ⚠ המדדים מתויגים לפי תדר: stats של תהליך קודם על תדר אחר => None, לא
    # ערך "קרוב" (§12). על *אותו* תדר הערך נכתב — וההבחנה אם הוא של התהליך
    # הקודם נעשית בניתוח, מול proc_start (ר' _experiment_summary), לא כאן.
    vals = parse_stats(text, f"{conf['freq']:.3f}") if conf.get("freq") is not None else {}
    row = {"t": round(time.time(), 2), "stats_mtime": round(st.st_mtime, 2),
           "conf_mtime": round(cst.st_mtime, 2) if cst else None, **conf,
           "signal": vals.get("channel_dbfs_signal_level"),
           "noise": vals.get("channel_dbfs_noise_level")}
    return row, sig


def _rflog_write(obj):
    """append + fsync. בניגוד ל-track.jsonl (buffer אפמרי, בלי fsync) — כאן זו
    תוצאת ניסוי שדה, והתרחיש הסביר לאבדן הוא בדיוק power bank שקוטע (§12).
    ⚠ לעולם לא זורק: כשל כתיבה (כרטיס SD מלא, הרשאה) נספר ב-write_errors
    ומוחזר False. בגרסה הראשונה הוא זרק — ובתוך ה-finally של _experiment_run
    זה דילג על סימון הסיום, כך שהניסוי נשאר "רץ" לנצח וסירב להתחיל מחדש."""
    line = json.dumps(obj, ensure_ascii=False) + "\n"
    with _rflog_lock:
        try:
            RFLOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(RFLOG_PATH, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                _rflog_fsync(f.fileno())
        except OSError as e:
            _rflog["write_errors"] += 1
            if _rflog["write_errors"] in (1, 100):      # לא מציפים את היומן בכל שנייה
                log.warning("רשם RF: כתיבה ל-%s נכשלה: %s", RFLOG_PATH, e)
            return False
        _rflog["rows"] += 1
    return True


def _rflog_stop(reason):
    """עוצר את הרשם (idempotent). לא עושה join כשנקרא מתוך ה-worker עצמו."""
    with _rflog_lock:
        if not _rflog["active"]:
            return False
        _rflog["active"] = False
        evt, th = _rflog["stop"], _rflog["thread"]
    if evt:
        evt.set()
    _rflog_write({"ev": "stop", "t": round(time.time(), 2), "reason": reason})
    if th and th is not threading.current_thread():
        th.join(timeout=2)
    return True


def _rflog_worker(stop_evt):
    prev = None
    while not stop_evt.is_set():
        with _rflog_lock:
            until = _rflog["until"]
        if until is not None and time.time() >= until:
            _rflog_stop("timeout")
            return
        try:
            row, prev = _rflog_sample(prev)
            if row:
                _rflog_write(row)
        except Exception:
            log.warning("רשם RF: דגימה נכשלה", exc_info=True)   # לא מפילים את ה-thread על שורה אחת
        stop_evt.wait(RFLOG_POLL_SEC)


def _rflog_start():
    with _rflog_lock:
        if _rflog["active"]:
            return False
        try:
            if RFLOG_PATH.exists() and RFLOG_PATH.stat().st_size > RFLOG_ROTATE_BYTES:
                os.replace(RFLOG_PATH, RFLOG_PATH.with_suffix(".jsonl.prev"))
        except OSError:
            pass
        now = time.time()
        evt = threading.Event()
        _rflog.update(active=True, started_at=now, until=now + RFLOG_MAX_SEC,
                      rows=0, marks=0, write_errors=0, stop=evt, thread=None)
    _rflog_write({"ev": "start", "t": round(now, 2), "version": VERSION,
                  "max_sec": RFLOG_MAX_SEC})
    th = threading.Thread(target=_rflog_worker, args=(evt,), daemon=True)
    with _rflog_lock:
        _rflog["thread"] = th
    th.start()
    return True


def _rflog_event(obj):
    """אירוע מתויג (סימון משתמש / תוצאת בדיקת אנטנה) — רק כשהרשם פעיל."""
    with _rflog_lock:
        active = _rflog["active"]
    if active:
        _rflog_write({"t": round(time.time(), 2), **obj})
    return active


def _rflog_status():
    with _rflog_lock:
        st = {k: _rflog[k] for k in ("active", "started_at", "until", "rows", "marks", "write_errors")}
    # "נותרו" מחושב כאן ולא בטלפון: שעון הטלפון ושעון ה-Pi לא בהכרח מסונכרנים בשטח
    st["remaining"] = max(0, round(st["until"] - time.time())) if st["active"] and st["until"] else None
    try:
        st["size"] = RFLOG_PATH.stat().st_size
    except OSError:
        st["size"] = 0
    return st


@app.route("/api/rflog", methods=["GET", "POST"])
def api_rflog():
    """GET: מצב הרשם. POST {active: bool}: הפעלה/כיבוי (idempotent). דרך _guard."""
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        if bool(data.get("active")):
            _rflog_start()
        else:
            with _exp_lock:
                exp_running = _exp["running"]
            if exp_running:
                return jsonify(ok=False, error="הניסוי האוטומטי רץ ומשתמש ברשם — עצור את הניסוי קודם",
                               **_rflog_status()), 409
            _rflog_stop("user")
    return jsonify(ok=True, **_rflog_status())


@app.route("/api/rflog/mark", methods=["POST"])
def api_rflog_mark():
    """{label} — סימון אירוע פיזי ("מחובר"/"מנותק"/"מסיים 50Ω"). 409 כשהרשם כבוי:
    סימון שלא נרשם לא יכול להיראות כאילו נרשם."""
    data = request.get_json(silent=True) or {}
    label = str(data.get("label") or "").strip()[:RFLOG_LABEL_MAX]
    if not label:
        return jsonify(ok=False, error="חסרה תווית"), 400
    if not _rflog_event({"ev": "mark", "label": label}):
        return jsonify(ok=False, error="הרשם כבוי — הפעל אותו קודם"), 409
    with _rflog_lock:
        _rflog["marks"] += 1
    return jsonify(ok=True, label=label, **_rflog_status())


@app.route("/api/rflog/export")
def api_rflog_export():
    if not RFLOG_PATH.exists():
        return jsonify(ok=False, error="אין הקלטה עדיין"), 404
    return send_file(str(RFLOG_PATH), mimetype="application/x-ndjson", as_attachment=True,
                     download_name=f"airam-rflog-{time.strftime('%Y%m%d-%H%M')}.jsonl")


# --- ניסוי כיול אוטומטי (docs/antenna-calibration-experiment.md) -----------
# ה-Pi מריץ לבד את כל התנאים (תדר × רווח קבוע/AGC × בדיקת-האנטנה של המוצר)
# פעם עם אנטנה מחוברת ופעם מנותקת. המשתמש מבצע **שתי פעולות פיזיות בלבד**
# (לנתק, לחבר) — במקום ניתוק/חיבור לכל תנאי. הנתונים נרשמים ברשם ה-RF,
# והסיכום מחושב על ה-Pi (_experiment_summary). הניסוי מחזיק את TUNE_LOCK לכל
# אורכו: כוונון/מעבר-מצב מהטלפון מקבלים 409, ו-_mode_reconcile_once מדלג.
EXP_ATIS_FREQ = 132.5        # ה-ATIS עצמו: אימות שהניתוק/החיבור באמת קרו (הנשא נעלם/חוזר)
EXP_CLEAN_FREQ = 122.6       # חלון 122.13–123.67: אף תדר של נתב"ג בחלון ה-AGC
EXP_ATISWIN_FREQ = 132.0     # חלון 131.53–133.07: ה-ATIS 0.2MHz ממרכזו
# IFGR ברווח הקבוע: 20 = מקסימום IF ("AGC על המסילה"), ועוד אחד לזיהוי רוויה.
# ⚠ ה-LNA (RFGR) **אינו** חלק מהקבוע: כל צעדי הריצה — רווח קבוע, AGC ובדיקות
# האנטנה — רצים באותו מצב LNA+מסנן, זה של בדיקת האנטנה של המוצר (_probe_frontend,
# נקבע פעם אחת ב-_experiment_start ונרשם ב-exp_start/בכל step). עד v2.26.0 זה
# היה (IFGR, 0) — ו-AGC רץ בפועל ב-LNA 0 (ר' _device_string), כך שההשוואה "AGC על
# המסילה ≈ רווח קבוע IFGR 20" (משטר A ב-docs/antenna-calibration-experiment.md)
# החזיקה. מאז שה-LNA נאכף גם תחת AGC, רק LNA משותף לכל הצעדים שומר עליה.
EXP_FIXED_IFGRS = (20, 35)
EXP_REF_SEC = 30
EXP_FIXED_SEC = 45           # רווח קבוע: גשש-הרעש מתכנס תוך ~0.2ש' (אין הליכת AGC)
EXP_AGC_SEC = 90             # AGC: כל restart מתחיל מ-gRdB=50 — צריך זמן לראות אם/איך מתכנס
EXP_AFTER_PROMPT_SEC = 90    # אחרי ניתוק/חיבור: הזחילה האיטית של הגשש (משטר C)
EXP_FINAL_AGC_SEC = 60
EXP_PROMPT_TIMEOUT_SEC = 600
EXP_RESTART_EST_SEC = 6      # הערכה ל-ETA בלבד, לא ללוגיקה
# חלונות הניתוח (ר' _experiment_summary)
EXP_FIXED_SETTLE_SEC = 10
EXP_AGC_TAIL_SEC = 30
EXP_EARLY_SEC = 3            # "מה שבדיקת האנטנה רואה": 3 השניות שאחרי שה-restart אומת
EXP_CREEP_HEAD_SEC = 5
EXP_CREEP_TAIL_SEC = 15
EXP_REF_SETTLE_SEC = 5       # ATIS: מדלגים על השניות הראשונות אחרי ה-restart
EXP_STALE_WINDOW_SEC = 5     # כמה זמן אחרי restart מחפשים כתיבה של התהליך הקודם
# mtime של הקרנל נלקח משעון גס (מפגר מילישניות אחרי time.time()) ו-stats_mtime
# מעוגל ל-10ms. בין flush-היציאה של התהליך הקודם להפעלת החדש עוברות לפחות מאות
# מילישניות (עצירה + airam-wait-sdrplay), כך ש-50ms הם מרווח בטוח לשני הכיוונים.
EXP_MTIME_TOL_SEC = 0.05
# ⚠ יוריסטיקת-שלמות *של הניסוי*, לא פסק-דין של המוצר: נשא ATIS חזק שלא ירד
# לפחות בזה בניתוק => כנראה שהאנטנה לא נותקה בפועל (או שדולף הרבה). מוצג
# עם המספר הגולמי, כדי שאפשר יהיה לשפוט אחרת.
EXP_ATIS_GONE_DB = 20.0

_exp_lock = threading.Lock()
_exp = {"running": False, "id": None, "started_at": None, "finished_at": None,
        "plan": [], "i": -1, "step_started_at": None, "waiting": None, "error": None,
        "result": None, "stop": None, "confirm": None, "thread": None}


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


def _experiment_plan(probe_freq, lna=RF_GAIN_DEFAULT, fm_notch=False):
    """תוכנית הניסוי: מחזור מלא עם אנטנה מחוברת, בקשת ניתוק, אותו מחזור מנותק,
    בקשת חיבור, ומחזור קצר לאימות. כל מחזור מסתיים על 132.000 AGC מתכנס — כך
    הניתוק/החיבור קורים בדיוק במצב שבו ה-AGC אמור להסתיר (משטר B), והדקה שאחריהם
    מתעדת את הזחילה (משטר C). ‏lna/fm_notch — משותפים לכל הצעדים (ר' EXP_FIXED_IFGRS)."""
    lna, fm_notch = int(lna), bool(fm_notch)

    def dwell(phase, key, label, freq, agc, ifgr=None, sec=None):
        return {"kind": "dwell", "phase": phase, "key": key, "label": label, "freq": freq,
                "agc": agc, "ifgr": ifgr, "rfgr": None if agc else lna,
                "lna": lna, "fm_notch": fm_notch,
                "sec": sec if sec is not None else (EXP_AGC_SEC if agc else EXP_FIXED_SEC)}

    def probe(phase, key, label, freq):
        return {"kind": "probe", "phase": phase, "key": key, "label": label, "freq": freq,
                "lna": lna, "fm_notch": fm_notch}

    def cycle(phase):
        steps = [dwell(phase, "atis", "ATIS 132.500 — אימות", EXP_ATIS_FREQ, True, sec=EXP_REF_SEC)]
        for freq, fk, fl in ((EXP_CLEAN_FREQ, "clean", "122.600 · אין נתב\"ג בחלון"),
                             (EXP_ATISWIN_FREQ, "atiswin", "132.000 · ATIS בחלון")):
            for ifgr in EXP_FIXED_IFGRS:
                steps.append(dwell(phase, f"{fk}_f{ifgr}", f"{fl} · רווח קבוע IFGR {ifgr}",
                                   freq, False, ifgr))
            if fk == "atiswin":
                steps.append(probe(phase, "probe_product", f"בדיקת האנטנה של המוצר · {probe_freq:.3f}",
                                   probe_freq))
                steps.append(probe(phase, "probe_atiswin", "בדיקת האנטנה · 132.000", EXP_ATISWIN_FREQ))
            steps.append(dwell(phase, f"{fk}_agc", f"{fl} · AGC", freq, True))
        return steps

    plan = cycle(1)
    # prompt: הקונפיג לא משתנה (נשארים על 132.000 AGC של הצעד הקודם) — lna לתיעוד בלבד
    plan.append({"kind": "prompt", "phase": 2, "key": "after_disconnect", "action": "disconnect",
                 "label": "נתק את האנטנה בכניסת ה-SDR", "sec": EXP_AFTER_PROMPT_SEC,
                 "lna": lna, "fm_notch": fm_notch})
    plan += cycle(2)
    plan.append({"kind": "prompt", "phase": 3, "key": "after_reconnect", "action": "reconnect",
                 "label": "חבר חזרה את האנטנה", "sec": EXP_AFTER_PROMPT_SEC,
                 "lna": lna, "fm_notch": fm_notch})
    plan.append(dwell(3, "atis", "ATIS 132.500 — אימות", EXP_ATIS_FREQ, True, sec=EXP_REF_SEC))
    plan.append(dwell(3, "clean_agc", "122.600 · אין נתב\"ג בחלון · AGC", EXP_CLEAN_FREQ, True,
                      sec=EXP_FINAL_AGC_SEC))
    for i, st in enumerate(plan):
        st["i"] = i
    return plan


def _exp_step_est(st):
    if st["kind"] == "dwell":
        return st["sec"] + EXP_RESTART_EST_SEC
    if st["kind"] == "probe":
        return EXP_RESTART_EST_SEC + ANTENNA_CHECK_SAMPLE_SEC
    return st["sec"]          # prompt: רק ההקלטה שאחרי האישור (זמן התגובה שלך לא ידוע)


def _experiment_status():
    with _exp_lock:
        e = dict(_exp)
    plan, i = e["plan"], e["i"]
    st = {k: e[k] for k in ("running", "id", "started_at", "finished_at", "error", "result")}
    st["steps_total"] = len(plan)
    st["step_index"] = i
    st["waiting"] = e["waiting"]
    cur = plan[i] if 0 <= i < len(plan) else None
    st["step"] = ({k: cur.get(k) for k in ("kind", "phase", "key", "label", "freq", "agc", "ifgr",
                                           "lna", "fm_notch", "action")}
                  if cur else None)
    eta = eta_prompt = None
    if e["running"] and plan:
        elapsed = time.time() - e["step_started_at"] if e["step_started_at"] else 0
        cur_left = max(0.0, _exp_step_est(cur) - elapsed) if cur and not e["waiting"] else 0.0
        rest = plan[i + 1:] if i >= 0 else plan
        eta = round(cur_left + sum(_exp_step_est(s) for s in rest))
        nxt = next((s for s in rest if s["kind"] == "prompt"), None)
        if cur and cur["kind"] == "prompt" and e["waiting"]:
            eta_prompt = 0
        elif nxt is not None:
            eta_prompt = round(cur_left + sum(_exp_step_est(s) for s in rest[:rest.index(nxt)]))
    st["eta_sec"] = eta
    st["eta_prompt_sec"] = eta_prompt
    return st


def _exp_voice_params(st):
    # ‏rf_gain = ה-LNA של הריצה בשני המצבים: RFGR ברווח קבוע, rfgain_sel תחת AGC
    return {"freq": st["freq"], "mod": "am", "agc": st["agc"],
            "if_gain": st["ifgr"] if st["ifgr"] is not None else IF_GAIN_DEFAULT,
            "rf_gain": st["lna"], "fm_notch": st["fm_notch"],
            "squelch_mode": "open", "squelch_snr": SNR_DEFAULT}


def _exp_meta(st):
    return {k: st.get(k) for k in ("i", "kind", "phase", "key", "label", "freq", "agc",
                                    "ifgr", "rfgr", "lna", "fm_notch", "sec", "action")}


def _experiment_run(run_id, prev, prev_live, plan, stop_evt, confirm_evt, own_rflog):
    """ה-thread של הניסוי. מחזיק את TUNE_LOCK (נתפס ב-_experiment_start) ומשחרר
    אותו תמיד ב-finally, אחרי שחזור המצב הקודם — בדיוק כמו /api/antenna/check."""
    err = None
    proc_start = None
    try:
        for st in plan:
            if stop_evt.is_set():
                err = "הניסוי בוטל"
                break
            with _exp_lock:
                _exp["i"] = st["i"]
                _exp["step_started_at"] = time.time()
            if st["kind"] in ("dwell", "probe"):
                t_enter = time.time()
                params = (_exp_voice_params(st) if st["kind"] == "dwell"
                          else _probe_params(st["freq"], st["lna"], st["fm_notch"]))
                e, _detail, _down = _enter_voice(params)
                t_ready = time.time()
                proc_start = _rtl_airband_start_wall()
                if e:
                    _rflog_event({"ev": "step", "exp": run_id, **_exp_meta(st), "t_enter": t_enter,
                                  "t_ready": t_ready, "proc_start": proc_start, "error": e})
                    err = f"המעבר לקול נכשל ({st['label']}): {e}"
                    break
                if st["kind"] == "dwell":
                    _rflog_event({"ev": "step", "exp": run_id, **_exp_meta(st), "t_enter": t_enter,
                                  "t_ready": t_ready, "proc_start": proc_start})
                    if stop_evt.wait(st["sec"]):
                        err = "הניסוי בוטל"
                        break
                    _rflog_event({"ev": "step_end", "exp": run_id, "i": st["i"]})
                else:
                    res = _sample_probe_stats(st["freq"], ANTENNA_CHECK_SAMPLE_SEC)
                    _rflog_event({"ev": "probe", "exp": run_id, **_exp_meta(st), "t_enter": t_enter,
                                  "t_ready": t_ready, "proc_start": proc_start,
                                  "noise": (res or {}).get("noise"), "signal": (res or {}).get("signal"),
                                  "stats_mtime": (res or {}).get("stats_mtime"),
                                  "error": None if res else "no_fresh_stats"})
            else:   # prompt: פעולה פיזית — ממתינים לאישור מהטלפון, בלי לשנות קונפיג
                confirm_evt.clear()
                with _exp_lock:
                    _exp["waiting"] = {"action": st["action"], "label": st["label"], "since": time.time()}
                _rflog_event({"ev": "prompt", "exp": run_id, **_exp_meta(st)})
                deadline = time.time() + EXP_PROMPT_TIMEOUT_SEC
                confirmed = False
                while not stop_evt.is_set() and time.time() < deadline:
                    if confirm_evt.wait(0.5):
                        confirmed = True
                        break
                with _exp_lock:
                    _exp["waiting"] = None
                    _exp["step_started_at"] = time.time()
                if not confirmed:
                    err = ("הניסוי בוטל" if stop_evt.is_set()
                           else f"לא התקבל אישור לפעולה הפיזית תוך {EXP_PROMPT_TIMEOUT_SEC // 60} דקות — הניסוי נעצר")
                    break
                t_c = time.time()
                _rflog_event({"ev": "mark", "exp": run_id, "label":
                              "מנותק" if st["action"] == "disconnect" else "מחובר", "prompted": True})
                _rflog_event({"ev": "step", "exp": run_id, **_exp_meta(st), "kind": "after_prompt",
                              "freq": EXP_ATISWIN_FREQ, "agc": True, "ifgr": None,
                              "t_enter": t_c, "t_ready": t_c, "proc_start": proc_start})
                if stop_evt.wait(st["sec"]):
                    err = "הניסוי בוטל"
                    break
                _rflog_event({"ev": "step_end", "exp": run_id, "i": st["i"]})
    except Exception as ex:                       # לא משאירים את ה-SDR תקוע בגלל באג בניסוי
        log.warning("ניסוי כיול: שגיאה פנימית", exc_info=True)
        err = f"שגיאה פנימית: {ex}"
    finally:
        try:
            _restore_after_probe(prev, prev_live)
        except Exception:
            log.warning("ניסוי כיול: שחזור המצב הקודם נכשל", exc_info=True)
        TUNE_LOCK.release()
        _rflog_event({"ev": "exp_end", "exp": run_id, "error": err})
        result = None
        try:
            result = _experiment_summary(_read_rflog_run(run_id))
            _rflog_event({"ev": "summary", "exp": run_id, "result": result})
        except Exception:
            log.warning("ניסוי כיול: חישוב הסיכום נכשל", exc_info=True)
        if own_rflog:
            _rflog_stop("experiment")
        with _exp_lock:
            _exp.update(running=False, finished_at=time.time(), error=err, result=result,
                        waiting=None, stop=None, confirm=None)


def _experiment_start():
    """מחזיר (payload, status)."""
    with _exp_lock:
        if _exp["running"]:
            return {"ok": False, "error": "הניסוי כבר רץ"}, 409
    with _scan_lock:
        scanning = _scan_thread is not None and _scan_thread.is_alive()
    if scanning:
        return {"ok": False, "error": "עצור את הסריקה לפני הניסוי"}, 409
    prev_live = _live_mode()
    if prev_live == "satcom":
        return {"ok": False, "error": "SATCOM פעיל — עצור אותו וחבר את אנטנת ה-VHF לפני הניסוי"}, 409
    if not TUNE_LOCK.acquire(blocking=False):
        return {"ok": False, "error": "פעולה אחרת מתבצעת כרגע — נסה שוב בעוד רגע"}, 409
    try:
        prev = load_state()
        acars = prev.get("acars_freqs") or ACARS_FREQS_DEFAULT
        try:
            probe_freq = float(acars[0])
        except (TypeError, ValueError, IndexError):
            probe_freq = float(ACARS_FREQS_DEFAULT[0])
        lna, fm_notch = _probe_frontend(prev)   # אותו קצה-קדמי כמו בדיקת האנטנה של המוצר
        plan = _experiment_plan(probe_freq, lna, fm_notch)
        own_rflog = _rflog_start()          # False => המשתמש כבר הקליט; לא נכבה אותו בסוף
        run_id = time.strftime("%Y%m%d-%H%M%S")
        stop_evt, confirm_evt = threading.Event(), threading.Event()
        with _exp_lock:
            _exp.update(running=True, id=run_id, started_at=time.time(), finished_at=None,
                        plan=plan, i=-1, step_started_at=None, waiting=None, error=None,
                        result=None, stop=stop_evt, confirm=confirm_evt)
        _rflog_event({"ev": "exp_start", "exp": run_id, "version": VERSION, "prev_live": prev_live,
                      "probe_freq": probe_freq, "lna": lna, "fm_notch": fm_notch, "steps": len(plan)})
        th = threading.Thread(target=_experiment_run, daemon=True,
                              args=(run_id, prev, prev_live, plan, stop_evt, confirm_evt, own_rflog))
        with _exp_lock:
            _exp["thread"] = th
        th.start()
    except Exception:
        TUNE_LOCK.release()
        with _exp_lock:
            _exp["running"] = False
        raise
    return {"ok": True, **_experiment_status()}, 200


def _read_rflog_run(run_id):
    """כל שורות הרשם בין exp_start ל-exp_end של ריצה מסוימת (שורה פגומה מדולגת)."""
    rows, inside = [], False
    try:
        lines = RFLOG_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if not isinstance(r, dict):
            continue
        if r.get("ev") == "exp_start" and r.get("exp") == run_id:
            inside = True
        if inside:
            rows.append(r)
        if r.get("ev") == "exp_end" and r.get("exp") == run_id:
            break
    return rows


def _med(vals):
    vals = [v for v in vals if isinstance(v, (int, float))]
    return round(statistics.median(vals), 1) if vals else None


def _experiment_summary(rows):
    """מספרים גולמיים בלבד (§12): חציוני רצפת-רעש לכל תנאי, מחובר מול מנותק.
    'detects' = האם *הסף של המוצר עצמו* (DISCONNECT_DROP_DB) היה מזהה את הניתוק
    בתנאי הזה — לא סף חדש. None בכל מקום שאין מספיק נתונים, לא ניחוש."""
    data = [r for r in rows if "ev" not in r and "noise" in r]
    steps, ends = {}, {}
    for r in rows:
        if r.get("ev") == "step":
            steps[r["i"]] = r
        elif r.get("ev") == "step_end":
            ends[r["i"]] = r["t"]

    def match(st, r):
        if r.get("freq") is None or abs(r["freq"] - st["freq"]) > 5e-4:
            return False
        if bool(r.get("agc")) != bool(st.get("agc")):
            return False
        if not st.get("agc") and r.get("ifgr") != st.get("ifgr"):
            return False
        ps = st.get("proc_start")
        # רק כתיבות של התהליך הנוכחי — flush-יציאה של הקודם נספר בנפרד (stale)
        return ps is None or (r.get("stats_mtime") or 0) >= ps - EXP_MTIME_TOL_SEC

    def vals(st, a, b, key="noise"):
        return [r[key] for r in data if a <= r["t"] <= b and match(st, r) and r.get(key) is not None]

    cond, atis, prompts = {}, {}, {}
    stale_rows = 0
    for i, st in steps.items():
        t_end = ends.get(i)
        if t_end is None:
            continue                      # צעד שלא הושלם (ביטול) — לא מנתחים חלקי
        ps = st.get("proc_start")
        if ps is not None:
            stale_rows += sum(1 for r in data
                              if st["t_enter"] <= r["t"] <= st["t_ready"] + EXP_STALE_WINDOW_SEC
                              and r.get("freq") is not None and abs(r["freq"] - st["freq"]) <= 5e-4
                              and r.get("noise") is not None
                              and (r.get("stats_mtime") or 0) < ps - EXP_MTIME_TOL_SEC)
        if st["kind"] == "after_prompt":
            head = vals(st, st["t_ready"], st["t_ready"] + EXP_CREEP_HEAD_SEC)
            tail = vals(st, t_end - EXP_CREEP_TAIL_SEC, t_end)
            inst = round(min(head), 1) if head else None
            after = _med(tail)
            prompts[st["key"]] = {"instant": inst, "after": after,
                                  "rise": round(after - inst, 1) if after is not None and inst is not None else None}
            continue
        if st["kind"] != "dwell":
            continue
        if st["key"] == "atis":
            atis[st["phase"]] = _med(vals(st, st["t_ready"] + EXP_REF_SETTLE_SEC, t_end, "signal"))
            continue
        if st["agc"]:
            steady = vals(st, t_end - EXP_AGC_TAIL_SEC, t_end)
        else:
            steady = vals(st, st["t_ready"] + EXP_FIXED_SETTLE_SEC, t_end)
        early = vals(st, st["t_ready"], st["t_ready"] + EXP_EARLY_SEC)
        c = cond.setdefault(st["key"], {"key": st["key"], "label": st["label"], "freq": st["freq"],
                                        "agc": st["agc"], "ifgr": st.get("ifgr")})
        c[f"p{st['phase']}"] = {"steady": _med(steady), "early": _med(early), "n": len(steady)}

    def drop(a, b):
        return round(a - b, 1) if a is not None and b is not None else None

    conditions = []
    for c in cond.values():
        p1, p2 = c.get("p1", {}), c.get("p2", {})
        d = drop(p1.get("steady"), p2.get("steady"))
        c["drop"] = d
        c["drop_early"] = drop(p1.get("early"), p2.get("early"))
        c["detects"] = None if d is None else d >= DISCONNECT_DROP_DB
        conditions.append(c)

    probes = {}
    stale_probes = 0
    for r in rows:
        if r.get("ev") != "probe":
            continue
        p = probes.setdefault(r["key"], {"key": r["key"], "label": r["label"], "freq": r["freq"]})
        stale = (r.get("stats_mtime") is not None and r.get("proc_start") is not None
                 and r["stats_mtime"] < r["proc_start"] - EXP_MTIME_TOL_SEC)
        stale_probes += int(stale)
        p[f"p{r['phase']}"] = {"noise": r.get("noise"), "stale": stale, "error": r.get("error")}
    for p in probes.values():
        d = drop((p.get("p1") or {}).get("noise"), (p.get("p2") or {}).get("noise"))
        p["drop"] = d
        p["detects"] = None if d is None else d >= DISCONNECT_DROP_DB

    atis_drop = drop(atis.get(1), atis.get(2))
    clean_agc = cond.get("clean_agc", {})
    p3 = clean_agc.get("p3") or {}
    start = next((r for r in rows if r.get("ev") == "exp_start"), {})
    return {
        "threshold_db": DISCONNECT_DROP_DB,
        # הקצה הקדמי שבו *כל* הריצה נמדדה (None בריצה מלפני v2.26.0 — לא ניחוש)
        "lna": start.get("lna"),
        "fm_notch": start.get("fm_notch"),
        "conditions": conditions,
        "probes": list(probes.values()),
        "atis": {"p1": atis.get(1), "p2": atis.get(2), "p3": atis.get(3), "drop": atis_drop,
                 "gone": None if atis_drop is None else atis_drop >= EXP_ATIS_GONE_DB,
                 "back": drop(atis.get(3), atis.get(1))},
        "after_disconnect": prompts.get("after_disconnect"),
        "after_reconnect": prompts.get("after_reconnect"),
        "drift_clean_agc": drop(p3.get("steady"), (clean_agc.get("p1") or {}).get("steady")),
        "stale_rows": stale_rows,
        "stale_probes": stale_probes,
    }


@app.route("/api/experiment", methods=["GET", "POST"])
def api_experiment():
    """GET: מצב הניסוי (+ result כשהסתיים). POST {action}: start / confirm (הפעולה
    הפיזית בוצעה) / abort. דרך _guard (POST)."""
    if request.method == "GET":
        return jsonify(ok=True, **_experiment_status())
    action = (request.get_json(silent=True) or {}).get("action")
    if action == "start":
        payload, code = _experiment_start()
        return jsonify(**payload), code
    with _exp_lock:
        running, stop_evt, confirm_evt, waiting = (_exp["running"], _exp["stop"],
                                                   _exp["confirm"], _exp["waiting"])
    if action == "confirm":
        if not running or not waiting:
            # ⚠ הסטטוס כבר מכיל error (של הריצה האחרונה) — מיזוג, לא kwargs כפולים
            return jsonify({**_experiment_status(), "ok": False,
                            "error": "אין פעולה שממתינה לאישור כרגע"}), 409
        confirm_evt.set()
        return jsonify(ok=True, **_experiment_status())
    if action == "abort":
        if running and stop_evt:
            stop_evt.set()
        return jsonify(ok=True, **_experiment_status())
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
        for p in _iter_recordings():
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


@app.route("/api/power")
def api_power():
    """מצב אספקת המתח ל-Pi (שימושי במיוחד עם סוללה ניידת):
      get_throttled  -> דגלי undervoltage/throttling (כל דגמי Pi)
      pmic_read_adc  -> מתח כניסה 5V בפועל (Pi 5 בלבד)
      measure_temp   -> טמפ' ליבה
    ביטים של get_throttled: 0=under-volt עכשיו · 2=throttled עכשיו ·
    16=under-volt קרה מאז אתחול · 18=throttling קרה.
    מוגש מ-cache בן _POWER_TTL שניות (ר' שם) — מכווץ טאבים מקבילים לדגימה אחת."""
    now = time.time()
    with _POWER_LOCK:
        if _power_cache["payload"] is not None and now - _power_cache["at"] < _POWER_TTL:
            cached = _power_cache["payload"]
        else:
            cached = _read_power()
            _power_cache["at"], _power_cache["payload"] = now, cached
    if cached is None:
        return jsonify(ok=False)
    return jsonify(**cached)


_FALSY_STR = ("false", "0", "off", "no")
_TRUTHY_STR = ("true", "1", "on", "yes")


def _parse_bool(raw, default):
    """bool עמיד ל-JSON טקסטואלי (curl: ‏"false"/"0"/"off") — אותה סמנטיקה כמו
    ה-agc הוותיק, כך ש-"false" לעולם לא נקרא True רק כי מחרוזת לא-ריקה.
    ‏None/ערך לא מזוהה => default (לא ניחוש לאף כיוון)."""
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return default
    v = str(raw).strip().lower()
    if v in _FALSY_STR:
        return False
    if v in _TRUTHY_STR:
        return True
    return default


def _parse_tune(data):
    """מנקה/מאמת פרמטרי כיוונון קולי. מחזיר (params, error). תדר נכתב כ-float
    מפורמט => ללא סיכון הזרקה.
    ‏rf_gain (מצב ה-LNA) נקלט ונשמר **גם כש-agc=True**: מאז v2.26.0 הוא נכתב
    לקונפיג בשני המצבים (rfgain_sel ב-AGC, RFGR ברווח ידני — ר' _device_string)."""
    try:
        freq = float(data.get("freq"))
    except (TypeError, ValueError):
        return None, "תדר לא תקין"
    if not (0.1 <= freq <= 1999.5):   # מרווח עבור DC_OFFSET (centerfreq <= 2000)
        return None, "תדר מחוץ לטווח (0.1–1999.5 MHz)"

    mod = "nfm" if str(data.get("mod", "am")).lower() == "nfm" else "am"
    agc = _parse_bool(data.get("agc", True), True)   # עמיד גם ל-"false" טקסטואלי (curl), לא רק bool
    # ‏fm_notch לפי *נוכחות* המפתח (כמו gain של satcom ב-/api/mode): חסר => None,
    # ו-_voice_tune משלים מה-state השמור. ברירת-מחדל False הייתה מכבה בשקט מסנן
    # שהמשתמש הדליק — כל לקוח שלא מכיר את השדה (טאב/PWA שנפתח לפני השדרוג ועדיין
    # מריץ JS ישן, curl) היה שולח כיוונון "מלא" בלעדיו.
    fm_notch = _parse_bool(data["fm_notch"], False) if "fm_notch" in data else None
    # אותו כלל נוכחות להגדרות השמע (v2.28.0) — לקוח ישן לא מכבה אותן בשקט
    voice_narrow = _parse_bool(data["voice_narrow"], False) if "voice_narrow" in data else None
    voice_lowpass = _sanitize_lowpass(data["voice_lowpass"]) if "voice_lowpass" in data else None
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
            "fm_notch": fm_notch, "voice_narrow": voice_narrow, "voice_lowpass": voice_lowpass,
            "squelch_mode": squelch_mode, "squelch_snr": squelch_snr}, None


def _voice_tune(params):
    """מכוונן קול (rtl_airband). מבטיח יציאה ממצב ACARS/VDL2 תחילה (משחרר את ה-SDR).
    מחזיר (payload, http_status). serialized תחת TUNE_LOCK."""
    if not TUNE_LOCK.acquire(blocking=False):
        # state בתשובה => ה-UI מיישר את התצוגה האופטימית חזרה למציאות
        return {"ok": False, "error": "כיוונון אחר מתבצע כרגע — המתן שנייה ונסה שוב",
                "state": load_state()}, 409
    try:
        # תפסנו את הנעילה — הבקשה תקינה, עוצרים סבב קודם (אם יש), בדיוק כמו
        # שאר המצבים ב-/api/mode (ר' ההערה שם): *לא* לפני הנעילה, אחרת ניסיון
        # כיוונון שנכשל לתפוס נעילה (409, למשל בדיקת אנטנה מקבילה) היה עוצר
        # סבב תקין "בחינם" ומשאיר "scan זומבי" — צרכן רץ בלי thread שממשיך אותו.
        _scan_stop_thread()
        prev = load_state()   # ההגדרות האחרונות שעבדו, לרולבק במקרה כישלון
        if params.get("fm_notch") is None:   # לא נשלח => נשאר כפי שנשמר (ר' _parse_tune)
            params = {**params, "fm_notch": bool(prev.get("fm_notch", False))}
        if params.get("voice_narrow") is None:
            params = {**params, "voice_narrow": bool(prev.get("voice_narrow", False))}
        if params.get("voice_lowpass") is None:
            params = {**params, "voice_lowpass": _sanitize_lowpass(prev.get("voice_lowpass"))}
        # ⚠ מיזוג על גבי prev, לא דריסה: params מכיל רק שדות קול (freq/mod/
        # gain/squelch). דריסה מלאה הייתה מוחקת satcom_bias_tee/satcom_gain/
        # signal_baseline/scan_plan/last_session_view_at וכו' — עם satcom_bias_tee
        # במיוחד, load_state הבא היה ממזג מ-DEFAULT_STATE=True ומדליק bias-T
        # מחדש בכניסה הבאה ל-SATCOM גם כשהמשתמש כבר מזין LNA חיצוני (§12).
        new_state = {**prev, **params, "app_mode": "voice"}
        log.info("tune %.3f MHz mod=%s agc=%s if_gain=%d rf_gain=%d fm_notch=%s squelch=%s snr=%.1f (from %s)",
                 params["freq"], params["mod"], params["agc"], params["if_gain"],
                 params["rf_gain"], params["fm_notch"], params["squelch_mode"], params["squelch_snr"],
                 request.remote_addr)

        err, detail, sdr_down = _enter_voice(params)
        if err:
            log.warning("tune %.3f MHz failed: %s (sdr_down=%s)", params["freq"], err, sdr_down)
            if sdr_down:
                # ה-SDR מנותק: רולבק ייתקע באותה המתנה בדיוק, אז מדלגים עליו.
                # הקונפיג החדש נשאר על הדיסק וייקלט כשהמכשיר יחובר (udev מרים
                # את השירותים) => שומרים state תואם לדיסק, לא את הקודם.
                save_state(new_state)
                return {"ok": False, "detail": detail, "state": new_state,
                        "error": err + " — התדר יוחל אוטומטית כשה-SDR יחובר"}, 500
            # config רע => מנסים את קונפיג הקול האחרון שעבד (retry בתוך קול, לא
            # עליונות-מצב). רק אם גם הוא לא עולה — נופלים ל-off לפי הדוקטרינה.
            if _rollback(prev):
                return {"ok": False, "error": err + " (חזרתי לתדר הקודם)",
                        "detail": detail, "state": {**prev, "app_mode": "voice"}}, 500
            return _fail_to_off(prev, err + " — וגם החזרה לתדר הקודם נכשלה",
                                detail, "voice tune")

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
    ימים קודמים. ?all=1 => כל מה שבזיכרון; ההיסטוריה המלאה תמיד זמינה בייצוא.
    ?day=YYYY-MM-DD => ארכיון: קורא מהדיסק (acars.jsonl, לא מהזיכרון) ומחזיר
    את כל הודעות אותו יום מקומי — מצב סטטי (בלי cursor/adsb), עצמאי מהפיד החי."""
    day = request.args.get("day")
    if day:
        bounds = _day_bounds(day)
        if bounds is None:
            return jsonify(ok=False, error="תאריך לא תקין (פורמט: YYYY-MM-DD)"), 400
        start, end = bounds
        msgs = [r for r in _read_acars_log() if start <= (r.get("t") or 0) < end]
        return jsonify(ok=True, day=day, messages=msgs)
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


ACARS_EXPORT_COLS = ["time_iso", "timestamp", "freq", "level", "snr", "mode", "label",
                     "category", "group", "dir", "tail", "flight", "actype", "msgno", "error",
                     "lat", "lon", "pos_src", "text"]
# ייצוא VDL2 = אותן עמודות + icao (זהות AVLC לפריימים בלי רישום) אחרי flight
VDL2_EXPORT_COLS = ["time_iso", "timestamp", "freq", "level", "snr", "mode", "label",
                    "category", "group", "dir", "tail", "flight", "icao", "actype", "msgno",
                    "error", "lat", "lon", "pos_src", "text"]


def _read_jsonl_log(path):
    """כל ההודעות מקובץ JSONL, ממוינות לפי זמן (t עולה). סובל שורות פגומות
    (כתיבה חלקית של ההודעה האחרונה בזמן הקריאה). משותף ל-ACARS ול-VDL2."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = _jsonl_records(lines)
    out.sort(key=lambda r: r.get("t") or 0)
    return out


def _read_acars_log():
    return _read_jsonl_log(ACARS_LOG_PATH)


def _read_vdl2_log():
    return _read_jsonl_log(VDL2_LOG_PATH)


_CSV_FORMULA_CHARS = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(v):
    """מונע הזרקת נוסחה ב-Excel/LibreOffice/Sheets: תא שמתחיל ב-=/+/-/@/Tab/CR
    מתפרש כנוסחה (או DDE/HYPERLINK חי) עם פתיחת הקובץ. תוכן ה-text/decoded/
    tail מגיע משידור רדיו — לא נתון סטטי-מהימן — אז לא מניחים שהוא 'בטוח' רק
    כי הוא טקסט. מוסיף ' מוביל (המוסכמה הסטנדרטית לניטרול) רק כשצריך."""
    if isinstance(v, str) and v[:1] in _CSV_FORMULA_CHARS:
        return "'" + v
    return v


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
                    row.append(_csv_safe((r.get("text") or "").replace("\r", " ").replace("\n", " ")))
                else:
                    row.append(_csv_safe(r.get(c)))
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
    """הודעות VDL2 אחרונות. ?since=<id> => רק חדשות מאותו cursor (פולינג יעיל).
    כברירת מחדל רק הודעות *היום*; ?all=1 => כל מה שבזיכרון (כמו /api/acars).
    ?day=YYYY-MM-DD => ארכיון מהדיסק (vdl2.jsonl), כמו ב-/api/acars."""
    day = request.args.get("day")
    if day:
        bounds = _day_bounds(day)
        if bounds is None:
            return jsonify(ok=False, error="תאריך לא תקין (פורמט: YYYY-MM-DD)"), 400
        start, end = bounds
        msgs = [r for r in _read_vdl2_log() if start <= (r.get("t") or 0) < end]
        return jsonify(ok=True, day=day, messages=msgs)
    try:
        since = int(request.args.get("since", 0))
    except (TypeError, ValueError):
        since = 0
    show_all = request.args.get("all") in ("1", "true", "yes")
    floor = 0 if show_all else _today_start()
    with _vdl2_lock:
        # עותקים (לא references): retry_count עלול להתעדכן ע"י ה-listener תוך כדי סדרול
        msgs = [dict(m) for m in _vdl2_msgs
                if m["id"] > since and (m.get("t") or 0) >= floor]
        cursor = _vdl2_seq
    return jsonify(ok=True, active=_is_active(VDL2_SERVICE),
                   freqs=load_state().get("vdl2_freqs", VDL2_FREQS_DEFAULT),
                   cursor=cursor, messages=msgs, adsb=_vdl2_adsb())


@app.route("/api/vdl2/export")
def api_vdl2_export():
    """ייצוא כל הודעות ה-VDL2 השמורות (vdl2.jsonl). ?format=csv | json."""
    return _export_response(_read_vdl2_log(), VDL2_EXPORT_COLS, "airam-vdl2")


def _read_satcom_log():
    return _read_jsonl_log(SATCOM_LOG_PATH)


SATCOM_EXPORT_COLS = ACARS_EXPORT_COLS   # אותה סכמת כרטיס בדיוק (בלי icao — ר' _normalize_satcom)


@app.route("/api/satcom")
def api_satcom():
    """הודעות SATCOM (Inmarsat, inmarsat-sniffer) אחרונות. ?since=<id> => רק
    חדשות מאותו cursor. כברירת מחדל רק הודעות *היום*; ?all=1 => כל מה שבזיכרון
    (כמו /api/acars). ?day=YYYY-MM-DD => ארכיון מהדיסק (satcom.jsonl)."""
    day = request.args.get("day")
    if day:
        bounds = _day_bounds(day)
        if bounds is None:
            return jsonify(ok=False, error="תאריך לא תקין (פורמט: YYYY-MM-DD)"), 400
        start, end = bounds
        msgs = [r for r in _read_satcom_log() if start <= (r.get("t") or 0) < end]
        return jsonify(ok=True, day=day, messages=msgs)
    try:
        since = int(request.args.get("since", 0))
    except (TypeError, ValueError):
        since = 0
    show_all = request.args.get("all") in ("1", "true", "yes")
    floor = 0 if show_all else _today_start()
    with _satcom_lock:
        msgs = [dict(m) for m in _satcom_msgs
                if m["id"] > since and (m.get("t") or 0) >= floor]
        cursor = _satcom_seq
    return jsonify(ok=True, active=_is_active(SATCOM_SERVICE),
                   freqs=load_state().get("satcom_freqs", SATCOM_FREQS_DEFAULT),
                   cursor=cursor, messages=msgs)


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
    מספר טיסה. ⚠ tail_is_station (ר' _normalize_acars) מדיר את ה-tail מהזהות —
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
    st = load_state()
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
                                   "decoded": r.get("decoded")})
        counts[mode] = n_window
    highlights.sort(key=lambda h: h["t"], reverse=True)
    highlights = highlights[:SESSION_HIGHLIGHTS_MAX]

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
    תחת TUNE_LOCK כמו כל read-modify-write אחר של state.json — בלעדיו בקשה
    שמגיעה בדיוק תוך כדי מעבר מצב יכולה לקרוא state ישן ולדרוס את app_mode
    החדש בחזרה לישן (lost update); זו לא פעולת חומרה, אז timeout קצר מספיק
    ו-409 (לא 500) כשעסוקה — המשתמש פשוט מנסה שוב."""
    if not TUNE_LOCK.acquire(timeout=2):
        return jsonify(ok=False, error="פעולה אחרת מתבצעת — נסה שוב"), 409
    try:
        save_state({**load_state(), "last_session_view_at": time.time()})
    finally:
        TUNE_LOCK.release()
    return jsonify(ok=True)


@app.route("/api/mode", methods=["POST"])
def api_mode():
    """מעבר בין המצבים: קול (rtl_airband) / ACARS (acarsdec) / VDL2 (dumpvdl2) /
    SATCOM (inmarsat-sniffer) / off (standby) / scan (סבב אוטומטי בין המצבים).
    SDR אחד בהחלפה — צרכן אחד בכל רגע. המצבים שווי-מעמד: כישלון כניסה לכל אחד
    מהם נופל ל-off (בלי fallback לקול). POST => עובר דרך _guard (Origin + PIN
    אופציונלי), כמו /api/tune."""
    data = request.get_json(silent=True) or {}
    mode = str(data.get("mode", "")).lower()
    if mode not in ("voice", "acars", "vdl2", "satcom", "off", "scan"):
        return jsonify(ok=False, error="mode לא תקין (voice/acars/vdl2/satcom/off/scan)"), 400

    # קודם ולידציה סטטית (לא תלוית-נעילה: פענוח פרמטרים/לוח/תדרים) — בקשה עם
    # פרמטרים לא-תקינים (400) לא נוגעת בסבב סריקה פעיל בכלל (אחרת סבב תקין
    # נעצר "בחינם" והמצב נשאר תקוע על הרגל האחרונה בלי סבב שממשיך אותו — "scan
    # זומבי"). _scan_stop_thread() עצמו נקרא רק *אחרי* שתפסנו את TUNE_LOCK
    # (בתוך ה-try למטה) — כך שאם יש סבב סריקה פעיל שמחזיק את הנעילה לרגע קצר
    # (מעבר רגל), אנחנו כבר בטוחים שנחזיק אותה בעצמנו לפני שננסה לעצור אותו,
    # ולא ניתקל ב-409 שקרי בגלל תחרות-עצמית עם הסבב. timeout קטן (לא 0) סופג
    # בדיוק את החלון הקצר הזה; רק חסימה ממושכת אמיתית (פעולה אחרת) עדיין
    # מחזירה 409 — ובלי לגעת בסבב כלל.
    st = load_state()

    if mode == "voice":
        # קול = כיוונון להגדרות השמורות האחרונות (או מפורשות). _voice_tune מחזיק
        # את ה-TUNE_LOCK בעצמו => לא לוקחים אותו כאן (deadlock).
        params, perr = _parse_tune(data if "freq" in data else st)
        if perr:   # state פגום => נופלים לברירת מחדל
            params, _ = _parse_tune(DEFAULT_STATE)
        # _scan_stop_thread נקרא בתוך _voice_tune עצמו, אחרי שהוא תופס את
        # TUNE_LOCK — לא כאן (ר' ההערה בתוך _voice_tune; אותה סיבה בדיוק
        # שהמצבים האחרים למטה קוראים לו רק אחרי הנעילה).
        payload, status = _voice_tune(params)
        return jsonify(payload), status

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
            "acars": ("acars_freqs", ACARS_FREQS_DEFAULT, _sanitize_freqs,
                      _acars_window_error, _enter_acars),
            "vdl2": ("vdl2_freqs", VDL2_FREQS_DEFAULT, _sanitize_freqs,
                     _vdl2_window_error, _enter_vdl2),
            "satcom": ("satcom_freqs", SATCOM_FREQS_DEFAULT, _sanitize_satellite,
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
                       state=load_state()), 409
    try:
        _scan_stop_thread()   # תפסנו את הנעילה — הבקשה תקינה, עוצרים סבב קודם (אם יש)
        st = load_state()     # רענון אחרי stop_thread — לא לדרוס שינוי מקביל
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
            new_state = {**st, "app_mode": "off",
                         "prev_mode": (cur_mode if cur_mode != "off"
                                      else st.get("prev_mode", "off"))}
            save_state(new_state)
            return jsonify(ok=True, app_mode="off")

        if mode == "scan":
            log.info("mode -> SCAN plan=%s (from %s)", plan, request.remote_addr)
            err, detail = _scan_activate(plan)
            if err:
                payload, status = _fail_to_off(st, err, detail, "enter scan (leg 0)")
                return jsonify(payload), status
            new_state = {**st, "app_mode": "scan", "scan_plan": plan}
            save_state(new_state)
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
        new_state = {**st, "app_mode": mode, key: freqs, **extra}
        save_state(new_state)
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
    """קונפיג הקול חסר או ישן => צריך שכתוב לפני שמרימים את rtl_airband. ישן =
    בלי stats_filepath (מדדי RF) / localtime (הקלטות), או — מאז v2.26.0 — בלי
    rfnotch_ctrl (נכתב בשני המצבים) או AGC בלי rfgain_sel. ⚠ בלי הבדיקה האחרונה
    התקנה משודרגת שיושבת בקול הייתה ממשיכה לרוץ עם LNA=0 תחת AGC (בדיוק הבאג
    ש-v2.26.0 מתקן, ר' _device_string) עד הכיוונון הידני הבא — _boot_restore מדלג
    על הכניסה כשהצרכן השמור כבר רץ וה-config לא stale."""
    try:
        cur = CONFIG_PATH.read_text()
    except OSError:
        return True
    if "stats_filepath" not in cur or "localtime" not in cur:
        return True
    kw = _conf_device_kwargs(cur)
    if "rfnotch_ctrl" not in kw:
        return True
    # v2.28.0: היסט 0.3 עגול = ה-bin הלא-נכון ב-rtl_airband (ר' DC_OFFSET) => שכתוב באתחול
    mf, mc = _CONF_FREQ_RE.search(cur), _CONF_CENTER_RE.search(cur)
    if mf and mc and abs(float(mc.group(1)) - float(mf.group(1)) - DC_OFFSET) > 5e-5:
        return True
    agc = _CONF_GAIN_RE.search(cur) is None
    return agc and "rfgain_sel" not in kw


def _boot_restore():
    """אורקסטרציית אתחול: אף צרכן SDR אינו enabled ב-systemd — airam-web (שעולה
    תמיד) קורא את state.json ומחזיר את המצב השמור, כולל off. כך המצב הנבחר שורד
    reboot בלי מצב ראשי ובלי הרחבת sudoers (רק restart/stop הקיימים).
    רץ ב-thread daemon => לא חוסם את app.run; כל כישלון => off + לוג, לעולם לא
    מפיל את שרת הווב."""
    try:
        st = load_state()
        mode = st.get("app_mode", "off")
        live = _live_mode()
        # scan: live הוא תמיד voice/acars/vdl2/None, לעולם לא "scan" עצמו (זו
        # אפליקציה מעל שלושת השירותים, לא שירות בפני עצמו) => הקיצור הזה תמיד
        # מדלג עליו וסבב הסריקה תמיד מתחיל מחדש מרגל 0 אחרי restart של airam-web
        # (גם אם רגל מסוימת כבר רצה תקין) — פשטות מכוונת, לא באג.
        if live == mode and not (mode == "voice" and _config_stale()):
            return   # restart של airam-web באמצע סשן: הצרכן השמור כבר רץ
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
        # העוקב (journalctl -n 0) חייב להיות מחובר *לפני* שמרימים צרכן — אחרת
        # "AIRAM_RF stream=start" של הסשן הראשון הולך לאיבוד (ר' _rf_follow_attached).
        # best-effort בלבד: בלי סימן-בנייה אין למה לחכות, ו-timeout לא חוסם שחזור.
        if _rf_telemetry_available():
            _rf_follow_attached.wait(RF_BOOT_ATTACH_WAIT_SEC)
        if not TUNE_LOCK.acquire(blocking=False):
            return   # המשתמש כבר בחר מצב מה-UI — כוונתו גוברת על השחזור
        try:
            # ⚠ בין load_state() בראש הפונקציה לכאן עברו עד BOOT_SDR_WAIT_SEC
            # שניות (המתנה ל-SDR) — אם המשתמש הספיק לבחור מצב אחר מה-UI *ואותה
            # בחירה כבר הסתיימה* (הנעילה שוב פנויה), st הישן היה דורס אותה.
            # קוראים מחדש ומוותרים על השחזור אם המצב השמור השתנה בינתיים.
            st2 = load_state()
            if st2.get("app_mode", "off") != mode:
                log.info("boot restore: המצב השמור השתנה בזמן ההמתנה ל-SDR (%s) — מוותרים על השחזור",
                         st2.get("app_mode"))
                return
            st = st2
            if mode == "voice":
                params, perr = _parse_tune(st)
                if perr:   # state פגום => ברירת מחדל
                    params, _ = _parse_tune(DEFAULT_STATE)
                err, detail, sdr_down = _enter_voice(params)
                if err and sdr_down:
                    # הכוונה נשמרת: Restart=always של היחידה ימשיך לנסות,
                    # udev ירים את sdrplay כשה-SDR יחובר; health מראה תקלה.
                    log.warning("boot restore: SDR לא נוכח — הקול יעלה כשיחובר")
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
                    err, _detail = _scan_activate(plan)
            if err:
                log.warning("boot restore -> %s failed: %s — falling to off", mode, err)
                _enter_standby()
                save_state({**st, "app_mode": "off", "prev_mode": mode})
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
    st = load_state()
    mode = st.get("app_mode", "off")
    if mode not in ("voice", "acars", "vdl2"):
        return                          # off/scan/satcom — לא בטיפול הפוליסה הזו
    if _live_mode() is not None:
        return                          # צרכן כלשהו רץ בפועל — אין תקלה לתקן
    if not _sdr_present():
        return                          # ה-SDR עצמו לא מחובר — udev יטפל כשיחזור
    if not TUNE_LOCK.acquire(blocking=False):
        return                          # פעולה אחרת כרגע — לא מתחרים, ננסה שוב במחזור הבא
    try:
        st2 = load_state()
        if st2.get("app_mode") != mode or _live_mode() is not None:
            return                      # השתנה בזמן שחיכינו לנעילה
        log.warning("mode reconcile: %s אמור לרוץ אבל שום צרכן לא פעיל — מנסה כניסה מחדש", mode)
        if mode == "voice":
            params, perr = _parse_tune(st2)
            if perr:
                params, _ = _parse_tune(DEFAULT_STATE)
            err, detail, _sdr_down = _enter_voice(params)
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
    כשל בבדיקה בודדת לא מפיל את ה-thread — מנסה שוב במחזור הבא."""
    while True:
        time.sleep(MODE_RECONCILE_INTERVAL_SEC)
        try:
            _mode_reconcile_once()
        except Exception:
            log.exception("mode reconcile crashed (ignored)")


if __name__ == "__main__":
    # ניקוי קובצי tmp יתומים מכתיבה שנקטעה (כיבוי פתאומי) — *לפני* כל השאר,
    # ורק כאן: בעלייה אין מופע אחר באמצע כתיבה. ר' _cleanup_orphan_tmp.
    _orphans = _cleanup_orphan_tmp()
    if _orphans:
        log.warning("נוקו %d קובצי tmp יתומים (כתיבה שנקטעה — כיבוי פתאומי?)", _orphans)
    # אין צרכן SDR enabled ב-systemd => שחזור המצב השמור (voice/acars/vdl2/
    # satcom/scan/off) נעשה כאן, ברקע (satcom חריג — לא משוחזר אוטומטית,
    # ר' _boot_restore/§12 ב-CLAUDE.md). מכסה גם שדרוג קונפיג
    # (stats_filepath/localtime) — כניסה לקול תמיד משכתבת את הקונפיג מה-state.
    # טלמטריית RF מהחומרה: journalctl -f *אחד* ארוך-חיים על יומן rtl_airband
    # (ר' _rf_follower_loop) — *לפני* _boot_restore (ר' _rf_follow_attached) ולפני ה-watcher, כדי שה-sidecar של ההקלטה
    # הראשונה כבר ייהנה מכיסוי. רדום כשאין סימן-בנייה; לעולם לא מפיל את השרת.
    threading.Thread(target=_rf_follower_loop, daemon=True).start()
    threading.Thread(target=_boot_restore, daemon=True).start()
    # התאוששות מקריסת sdrplay.service שלא הופצה לצרכן הפעיל (ר' _mode_reconcile_loop) —
    # thread נפרד מ-_boot_restore: זה רץ *במשך* הסשן, לא רק פעם אחת באתחול.
    threading.Thread(target=_mode_reconcile_loop, daemon=True).start()
    REC_DIR.mkdir(parents=True, exist_ok=True)
    _saved_dir().mkdir(parents=True, exist_ok=True)
    threading.Thread(target=_activity_watcher, daemon=True).start()
    _load_acars_history()                                           # היסטוריית ACARS שורדת restart (לפני ה-listener)
    threading.Thread(target=_acars_listener, daemon=True).start()   # פיד UDP מ-acarsdec (שקט במצב קול)
    _load_vdl2_history()                                            # היסטוריית VDL2 (לפני ה-listener, אין מרוץ)
    threading.Thread(target=_vdl2_listener, daemon=True).start()    # פיד UDP מ-dumpvdl2 (שקט בשאר המצבים)
    _load_satcom_history()                                          # היסטוריית SATCOM (לפני ה-listener, אין מרוץ)
    threading.Thread(target=_satcom_listener, daemon=True).start()  # פיד UDP מ-inmarsat-sniffer (שקט בשאר המצבים)
    # תמלול ATC — דמון נפרד (לא חוסם את היומן/retention). עולה **תמיד**, גם
    # כש-transcribe_auto כבוי: תמלול לפי דרישה (📝) ותמלול של הקלטות שמורות
    # (★) עובדים בכל מקרה. ה-thread ישן כשwhisper לא מותקן ומזהה התקנה
    # מאוחרת לבד — קודם הוא עשה return ומת, וזו הייתה אחת הסיבות שהפיצ'ר
    # "לא עבד" בלי שאיש ידע (ר' _transcribe_worker).
    threading.Thread(target=_transcribe_worker, daemon=True).start()
    adsb.start()   # רק כשרצים כשרת (לא בזמן import) - דמון, לא מעכב עלייה
    # threaded: סטרים /stream הוא חיבור ארוך-טווח => חייב לא לחסום בקשות אחרות
    app.run(host="0.0.0.0", port=8080, threaded=True)
