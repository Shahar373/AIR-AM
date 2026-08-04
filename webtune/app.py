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

# --- מד שדה מאוחד + בדיקת אנטנה (ר' docs/field-station-roadmap.md) ---------
# ב-ACARS/VDL2 אין מד אות רציף כמו ב-קול (rtl_airband): acarsdec/dumpvdl2 לא
# חושפים רצפת רעש כששקט, רק בתוך הודעה מפוענחת. "בדיקת אנטנה" עוקפת את זה
# ע"י מעבר זמני לקול (AGC, סקוולץ' פתוח) על התדר המבוקש ומדידה אמיתית.
ANTENNA_CHECK_SAMPLE_SEC = 3.0   # פולינג עד שדגימה טרייה לתדר הזה מופיעה ב-stats
SIGNAL_LAST_MSG_MAX_AGE = 300.0  # מעל זה "הודעה אחרונה" מסומנת לא-טרייה (5 דק') — לא נעלמת, רק מסומנת
# ירידה (dB) ברצפת הרעש מתחת לבסיס שהמשתמש כייל, שנחשבת חריגה. *לא* סף
# "איכות אות" מומצא (§12 ב-CLAUDE.md אוסר את זה) — תצפית פיזיקלית: ניתוק
# אנטנה מנתק את המקלט מרעש-הסביבה שקושר אותו לעולם ומשאיר את רעש-הפנים של
# המקלט עצמו, שבד״כ נמוך בהרבה מ-10dB. פסק הדין תמיד מול הבסיס של המשתמש
# עצמו, לעולם לא מול ערך מוחלט שניחשנו.
DISCONNECT_DROP_DB = 10.0

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
                 "last_session_view_at": None}


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
    default_dirs = (STATE_PATH.parent, CONFIG_PATH.parent, ACARS_ENV_PATH.parent, REC_DIR)
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


def write_config(freq, mod, agc, if_gain, rf_gain, squelch_mode="auto", squelch_snr=SNR_DEFAULT):
    _atomic_write(CONFIG_PATH, render_config(freq, mod, agc, if_gain, rf_gain, squelch_mode, squelch_snr))


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
    return {**DEFAULT_STATE, **st}


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
    """כיוונון נכשל => משחזרים את ההגדרות האחרונות שעבדו ומרימים מחדש.
    מחזיר True אם השחזור הצליח (rtl_airband חי) — רולבק שנכשל מטופל אצל הקורא
    בנפילה ל-off (לא משאירים שירות בלולאת קריסה ולא מעמידים פנים שהקול חזר)."""
    log.warning("rollback to %.3f MHz", prev["freq"])
    try:
        write_config(prev["freq"], prev["mod"], prev["agc"], prev["if_gain"],
                     prev["rf_gain"], prev["squelch_mode"], prev["squelch_snr"])
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
_LOADSHEET_ZFW_RE = re.compile(r"\bZFW\s+(\d+)")
_LOADSHEET_TOW_RE = re.compile(r"\bTOW\s+(\d+)")
_LOADSHEET_TOF_RE = re.compile(r"\bTOF\s+(\d+)")
_LOADSHEET_PAX_RE = re.compile(r"\bCREW\s+(\d+)/(\d+)\s+PAX\s+(\d+)")
_LOADSHEET_TTL_RE = re.compile(r"\bTTL\s+(\d+)")


def _parse_loadsheet(text):
    """Loadsheet אלקטרוני (label C1, 'LOADSHEET FINAL'): משקל המראה (ZFW/TOW/TOF)
    ונוסעים/צוות. best-effort — כל בלוק מציג רק את מה שהוא נושא."""
    if not text or "LOADSHEET" not in text:
        return None
    parts = []
    m = _LOADSHEET_ZFW_RE.search(text)
    if m:
        parts.append(f"ZFW {m.group(1)}kg")
    m = _LOADSHEET_TOW_RE.search(text)
    if m:
        parts.append(f"TOW {m.group(1)}kg")
    m = _LOADSHEET_TOF_RE.search(text)
    if m:
        parts.append(f"TOF {m.group(1)}kg")
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
    decoded = (f"UTC {utc[:2]}:{utc[2:]}z · דלק {fob}t · {alt}ft · "
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
    tail_val = g("tail", "registration")
    tail_is_station = bool(tail_val) and bool(_UPLINK_HEADER_RE.match(str(tail_val)))

    # פענוח ARINC-622 (libacars): kind => badge וקבוצה, וטקסט קריא אם יש.
    lat = lon = pos_src = decoded = None
    libacars = m.get("libacars")
    if libacars:
        kind, dtext, decode_failed = _libacars_decode(libacars)
        category = kind
        # ⚠ decode_failed=True ≠ "אין נתון" — יש הבדל אמיתי בין "לא ניסינו" ל"ניסינו
        # ונכשלנו" (המפענח עצמו החזיר err:true על היישום המקונן, אימות מקליטת שדה).
        # לא ממציאים תוכן, אבל *כן* אומרים למשתמש שהיה ניסיון — עדיף מ-"—" סתמי.
        decoded = dtext if dtext else (
            "לא פוענח — המפענח החזיר שגיאה (כנראה איתות שולי)" if decode_failed else None)
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
    }
    rec["notable"] = _interest_score(rec)     # לדוח הסשן + סינון/התראות ב-UI (ר' §1 "האנליסט")
    return rec


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
            "msgno": ((acars.get("msg_num") or "") + (acars.get("msg_num_seq") or "")) or None,
            "text": acars.get("msg_text"),
            # err=פריים פגום / crc_ok=False => כמו acarsdec error>0 (מגדר heuristics של טקסט)
            "error": 0 if (not acars.get("err") and acars.get("crc_ok", True)) else 1,
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
                "לא פוענח — המפענח החזיר שגיאה (כנראה איתות שולי)" if decode_failed else None)
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
            # ⚠ אותה הגנה כמו SATCOM (ר' ההערה המקבילה ב-_normalize_acars): CRC
            # תקין ב-AVLC ≠ פענוח-יישום מוצלח — decode_failed=True חוסם גם כאן,
            # וכיוון שגוי (adsc_dir_ok) חוסם גם כשאין שום err (ר' ההערה למעלה).
            if is_adsc and not decode_failed and adsc_dir_ok:
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
    recs = []
    for ln in lines[-VDL2_BUF_MAX:]:
        try:
            recs.append(json.loads(ln))
        except ValueError:
            continue                          # שורה פגומה (כתיבה חלקית) => דילוג
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
    recs = []
    for ln in lines[-SATCOM_BUF_MAX:]:
        try:
            recs.append(json.loads(ln))
        except ValueError:
            continue
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
                 params["rf_gain"], params["squelch_mode"], params["squelch_snr"])
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
            time.sleep(step)
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
                # כל עוד אף רגל אחרת לא נכנסת בפועל.
                if consumer_active:
                    log.info("scan: אף רגל לא בחלון השעות — מכבה את הצרכן הפעיל")
                    _enter_standby()
                    consumer_active = False
                    last_entered = None
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
                _enter_standby()
                if stop_evt.is_set():
                    return   # מעבר מצב אחר כבר תפס פיקוד בינתיים — לא דורסים את ה-state שלו
                cur = load_state()
                save_state({**cur, "app_mode": "off", "prev_mode": "scan"})
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
    מחזיר (error, detail) — error רק על כשל אמיתי בכניסה לרגל."""
    global _scan_thread, _scan_thread_stop
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
    for svc in ("rtl_airband", "icecast2", "sdrplay", "airam-acars", "airam-vdl2", "airam-satcom"):
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
    return jsonify(ok=ok, app_mode=mode,
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


def _read_voice_metrics():
    """מדדי RF רציפים לתדר הנוכחי מ-rtl_airband stats. מקור אמת יחיד לפענוח
    הקובץ — משותף ל-/api/metrics (תצוגת קול) ול-/api/signal (מד השדה המאוחד,
    מצב voice). rtl_airband מרענן את הקובץ כל ~1 שנייה."""
    try:
        age = time.time() - STATS_PATH.stat().st_mtime
        text = STATS_PATH.read_text()
    except OSError:
        return {"fresh": False, "age": None, "signal": None, "noise": None,
                "snr": None, "overload": False, "squelch_opens": None}

    want = f"{load_state()['freq']:.3f}"       # מדדים מתויגים freq=MHz ב-3 ספרות
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
    """מדדי RF חיים לתדר הנוכחי. rtl_airband מרענן את הקובץ כל ~1 שנייה."""
    return jsonify(ok=True, overload_dbfs=OVERLOAD_DBFS, **_read_voice_metrics())


def _signal_verdict(noise, baseline):
    """פסק דין *רק* מול בסיס שהמשתמש כייל בעצמו — לעולם לא סף איכות מומצא
    (§12 ב-CLAUDE.md). ‏noise=None (אין מדידה נוכחית) => 'unknown'. בלי בסיס
    כלל => 'no_baseline' — לא ניחוש. אחרת משווים מול DISCONNECT_DROP_DB."""
    if noise is None:
        return "unknown"
    if not baseline or baseline.get("noise") is None:
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
    payload = {"ok": True, "mode": mode or "off"}

    if mode == "voice":
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
            age = time.time() - STATS_PATH.stat().st_mtime
            text = STATS_PATH.read_text()
        except OSError:
            age, text = None, ""
        if text:
            vals = parse_stats(text, want)
            noise = vals.get("channel_dbfs_noise_level")
            if age is not None and age <= STATS_MAX_AGE and noise is not None:
                sig = vals.get("channel_dbfs_signal_level")
                return {"signal": sig, "noise": noise,
                        "snr": round(sig - noise, 1) if sig is not None else None}
        time.sleep(0.3)
    return None


def _restore_after_probe(prev_state, prev_live):
    """משחזר את מה שבאמת רץ לפני בדיקת האנטנה הזמנית — בצד השרת, כדי שהבדיקה
    תישאר עסקה סינכרונית אחת ולא תלויה בפולינג הבא של הקליינט. לא נועל
    TUNE_LOCK (הקורא כבר מחזיק אותו, כמו כל _enter_* אחר) ולא כותב ל-state.json
    (זו לא בקשת מעבר-מצב — הכוונה השמורה לא השתנתה בכלל). best-effort: כישלון
    שחזור לא הופך את הבדיקה עצמה לכישלון, רק נרשם ללוג; המשתמש עדיין יכול
    להיכנס למצב מחדש ידנית, בדיוק כמו כל _enter_* אחר שנכשל."""
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
                         "squelch_mode": prev_state["squelch_mode"],
                         "squelch_snr": prev_state["squelch_snr"]})
        else:
            _enter_standby()
    except Exception:
        log.warning("בדיקת אנטנה: שחזור המצב הקודם (%s) נכשל", prev_live, exc_info=True)


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
        already_voice = (prev_live == "voice" and abs(prev.get("freq", -999.0) - freq) < 5e-4)
        if not already_voice:
            params = {"freq": freq, "mod": "am", "agc": True, "if_gain": IF_GAIN_DEFAULT,
                      "rf_gain": RF_GAIN_DEFAULT, "squelch_mode": "open", "squelch_snr": SNR_DEFAULT}
            err, detail, _sdr_down = _enter_voice(params)
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
            return jsonify(ok=False, error="לא התקבלו מדדים מה-SDR בזמן — נסה שוב"), 504

        if calibrate:
            baseline = {"noise": result["noise"], "freq": freq, "ts": time.time()}
            save_state({**load_state(), "signal_baseline": baseline})
        else:
            baseline = prev.get("signal_baseline")
        return jsonify(ok=True, freq=freq, calibrated=calibrate, baseline=baseline,
                       verdict=_signal_verdict(result["noise"], baseline), **result)
    finally:
        TUNE_LOCK.release()


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
    """מכוונן קול (rtl_airband). מבטיח יציאה ממצב ACARS/VDL2 תחילה (משחרר את ה-SDR).
    מחזיר (payload, http_status). serialized תחת TUNE_LOCK."""
    if not TUNE_LOCK.acquire(blocking=False):
        # state בתשובה => ה-UI מיישר את התצוגה האופטימית חזרה למציאות
        return {"ok": False, "error": "כיוונון אחר מתבצע כרגע — המתן שנייה ונסה שוב",
                "state": load_state()}, 409
    try:
        prev = load_state()   # ההגדרות האחרונות שעבדו, לרולבק במקרה כישלון
        new_state = {**params, "app_mode": "voice",
                     "acars_freqs": prev.get("acars_freqs", ACARS_FREQS_DEFAULT),
                     "vdl2_freqs": prev.get("vdl2_freqs", VDL2_FREQS_DEFAULT)}
        log.info("tune %.3f MHz mod=%s agc=%s if_gain=%d rf_gain=%d squelch=%s snr=%.1f (from %s)",
                 params["freq"], params["mod"], params["agc"], params["if_gain"],
                 params["rf_gain"], params["squelch_mode"], params["squelch_snr"], request.remote_addr)

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
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    out.sort(key=lambda r: r.get("t") or 0)
    return out


def _read_acars_log():
    return _read_jsonl_log(ACARS_LOG_PATH)


def _read_vdl2_log():
    return _read_jsonl_log(VDL2_LOG_PATH)


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
                    row.append((r.get("text") or "").replace("\r", " ").replace("\n", " "))
                else:
                    row.append(r.get(c))
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
    מספר טיסה."""
    reg = adsb.norm_reg(m.get("tail"))
    if reg:
        return ("reg", reg)
    if m.get("icao"):
        return ("icao", str(m["icao"]).upper())
    if m.get("flight"):
        return ("flight", str(m["flight"]).upper())
    return None


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
            c["tail"] = c["tail"] or m.get("tail")
            c["flight"] = c["flight"] or m.get("flight")
            c["icao"] = c["icao"] or m.get("icao")
            c["actype"] = c["actype"] or m.get("actype")
            if m.get("lat") is not None and (c["_pos_t"] is None or t >= c["_pos_t"]):
                c["lat"], c["lon"], c["pos_src"], c["_pos_t"] = m["lat"], m["lon"], m.get("pos_src"), t
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
    idempotent — פתיחה/רענון חוזרים של הכרטיס לא 'צורכים' אותו בטעות."""
    save_state({**load_state(), "last_session_view_at": time.time()})
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
        _scan_stop_thread()
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
            # prev_mode => כפתור ההדלקה ב-UI מחזיר את המצב האחרון, בלי לקודד קול
            new_state = {**st, "app_mode": "off",
                         "prev_mode": st.get("app_mode", "off")}
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
    (ספירה לאחור, הדגשת הרגל הפעילה). ריק/idx=-1 כשאין סבב פעיל."""
    with _scan_lock:
        status = dict(_scan_status)
        active = _scan_thread is not None and _scan_thread.is_alive()
    return jsonify(ok=True, active=active, **status)


# --- שחזור מצב באתחול: airam-web הוא המתזמר ---------------------------------
BOOT_SDR_WAIT_SEC = 90    # המתנה ל-SDR באתחול לפני ניסיון כניסה (USB enumeration איטי)


def _config_stale():
    """קונפיג הקול חסר או ישן (שדרוג: בלי stats_filepath למדדי RF / localtime
    להקלטות) => צריך שכתוב לפני שמרימים את rtl_airband."""
    try:
        cur = CONFIG_PATH.read_text()
    except OSError:
        return True
    return "stats_filepath" not in cur or "localtime" not in cur


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
    threading.Thread(target=_boot_restore, daemon=True).start()
    REC_DIR.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=_activity_watcher, daemon=True).start()
    _load_acars_history()                                           # היסטוריית ACARS שורדת restart (לפני ה-listener)
    threading.Thread(target=_acars_listener, daemon=True).start()   # פיד UDP מ-acarsdec (שקט במצב קול)
    _load_vdl2_history()                                            # היסטוריית VDL2 (לפני ה-listener, אין מרוץ)
    threading.Thread(target=_vdl2_listener, daemon=True).start()    # פיד UDP מ-dumpvdl2 (שקט בשאר המצבים)
    _load_satcom_history()                                          # היסטוריית SATCOM (לפני ה-listener, אין מרוץ)
    threading.Thread(target=_satcom_listener, daemon=True).start()  # פיד UDP מ-inmarsat-sniffer (שקט בשאר המצבים)
    if TRANSCRIBE:   # תמלול ATC אופציונלי - דמון נפרד (לא חוסם את היומן/retention)
        threading.Thread(target=_transcribe_worker, daemon=True).start()
    adsb.start()   # רק כשרצים כשרת (לא בזמן import) - דמון, לא מעכב עלייה
    # threaded: סטרים /stream הוא חיבור ארוך-טווח => חייב לא לחסום בקשות אחרות
    app.run(host="0.0.0.0", port=8080, threaded=True)
