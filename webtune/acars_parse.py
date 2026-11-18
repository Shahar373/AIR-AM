# ============================================================================
#  AIR-AM — נרמול ופענוח הודעות דאטה: ACARS · VDL2 · SATCOM
# ----------------------------------------------------------------------------
#  הוצא מ-app.py (שלב ג2). כל פונקציה כאן טהורה: JSON גולמי מהמפענח ⇒ כרטיס אחיד. אין תלות
#  ב-Flask, ב-state או ב-systemd (נבדק מכנית: הסגור הטרנזיטיבי של שלוש פונקציות הנרמול לא נוגע
#  בהם). החריגים היחידים: `_run_libacars` מריץ את `decode_acars_apps` (subprocess, shell=False,
#  msg_text כ-argv — CLAUDE.md §9) ו-`adsb` לנרמול מזהים.
#  המדיניות (CLAUDE.md §5/§12 + docs/lessons-learned.md): לעולם לא ממציאים ערך; כיוון מבני קודם
#  לטבלת labels; מיקום ADS-C רק כשהפענוח לא נכשל *וגם* הכיוון אינו uplink.
#  ⚠ הקוראים ניגשים כ-`acars_parse.<שם>` (לא `from acars_parse import`) — כך החלפה בבדיקה משפיעה.
# ============================================================================
import collections
import json
import logging
import os
import re
import subprocess
import time

import adsb

log = logging.getLogger("airam")

# הערה: פורמט ה-login של LLBG (`02XSTLVLLBG03200N03452E...`) *אינו* מחולץ —
# ה-DDMM שם הוא נ"צ ה*שדה* (reference של נתב"ג שמשותף לכל מטוס שמתחבר), לא מיקום
# המטוס. חילוצו הדביק 📍 מטעה על כל הודעת login. ראה CHANGELOG 1.7.1.


# --- חבילת פענוח עמוק: SA / H1 / FPN / label 15 / SQ / autotune -------------
# כל ה-parsers מופעלים *רק* לפי label (dispatch ב-_normalize_acars) => אין סיכון
# false-positive בין labels; בתוך ה-label — regex מעוגן-תחילה ושמרני.


# --- פרסרים נוספים שנבנו מקליטה אמיתית (labels C1/16/1L/A3, לא מתועדים ב-ARINC) --


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
    # QP–QS: אותם ארבעה אירועים, בפורמט שנושא גם יעד (מוצא+יעד+שעה — ר' _parse_oooi_q).
    # נצפו בקליטת 24.10.2026 (QP/QR/QS) ונפלו עד כה ל-"Label QP" הגנרי.
    "QP": ("OOOI · יציאה (Out)", "oooi"),
    "QQ": ("OOOI · המראה (Off)", "oooi"),
    "QR": ("OOOI · נחיתה (On)", "oooi"),
    "QS": ("OOOI · חניה (In)", "oooi"),
    "80": ("OOOI · דוח OFFRP/INRP (80)", "oooi"),
    "A9": ("ATIS · מידע שדה (A9)", "comm"),
    "B9": ("בקשת אישור ATC", "clearance"),
    "BA": ("אישור ATC (clearance)", "clearance"),
    "A3": ("אישור טרום-המראה (PDC)", "clearance"),
    # DCL (ARINC 623) בכיוון המטוס — נצפו בקליטת 24.10.2026 (‏4X-EMF, ‏IZ815): ‏B3 = בקשת
    # אישור יציאה (‎"/LLBG.DC1/RCD …"), ‏B4 = הקריאה-החוזרת של האישור (‎"…/CDA … CLRD TO …").
    "B3": ("בקשת אישור יציאה (DCL)", "clearance"),
    "B4": ("קריאה חוזרת של אישור יציאה (DCL)", "clearance"),
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
# ⚠ זו *נפילה* בלבד: מסגרת acarsdec נושאת block ID, והוא מכריע (_acars_block_dir).
# קליטת 24.10.2026 הראתה למה: הטבלה הזו (וה-header) טעו ב-19 מתוך 614 הודעות — 14
# בלוקים של C1 (‏loadsheet ו-NOTOC אל מדפסת הקוקפיט: uplink, לא downlink), שתי H1
# (‏H1 דו-כיווני — הוסר מכאן), B3/B4 שה-header ‎"/LLBG.DC1/…" סיווג כ-uplink, ופריים
# Q0 פגום אחד — ועוד 262 (בעיקר ‎_d/5V) נשארו בלי כיוון בכלל.
# ‏3L הוסר: 12/12 הודעות 3L בייצוא היו בלי MSN (block ID אות ⇒ uplink), בניגוד לערך הקודם.
_ACARS_DIR_BY_LABEL = {
    "5Z": "downlink",
    "C1": "uplink",     # הודעה למדפסת הקוקפיט (loadsheet וכו') — מהקרקע
    "QA": "downlink", "QB": "downlink", "QC": "downlink", "QD": "downlink",
    "QP": "downlink", "QQ": "downlink", "QR": "downlink", "QS": "downlink",
    "80": "downlink",   # דוח OOOI (OFFRP/INRP) מהמטוס
    "Q0": "downlink",   # link test ממטוס
    "B9": "downlink",   # בקשת אישור מהמטוס
    "B3": "downlink", "B4": "downlink",   # DCL: בקשה + קריאה-חוזרת מהמטוס
    "WX": "downlink",   # בקשת METAR לשדות גיבוי מהמטוס
    "SA": "downlink",   # media advisory — המטוס מדווח על מצב הקישורים שלו
    "15": "downlink",   # דיווח מיקום מהמטוס
    # ARINC 620/libacars: ‏A6/AA = ADS-C/CPDLC uplink, ‏B6/BA = ADS-C/CPDLC downlink.
    # ⚠ גם ה-downlink מתחיל בכתובת תחנת קרקע (‎/QUKAXBA.ADS.G-ZBKM…, קליטת VHF
    # 23.10.2026), ולכן _UPLINK_HEADER_RE סיווג אותו בטעות כ-uplink. ‏BA תוקן
    # (בעבר "uplink", אישור מהקרקע). אומת ב-decode_acars_apps: ‏d B6 ⇒ Basic report
    # תקין, ‏u B6 ⇒ "Malformed ADS-C message".
    "A6": "uplink", "AA": "uplink",
    "B6": "downlink", "BA": "downlink",
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
    except ValueError:
        # ‏NUL בטקסט מהרשת => "embedded null byte" מ-subprocess. בלי זה כל ההודעה
        # נזרקה ב-listener, בניגוד ל"כשל לא מאבד הודעה" (§9).
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


def _acars_block_dir(block_id):
    """כיוון *מבני* מה-block ID של מסגרת ACARS (ARINC 618): ספרה 0–9 = downlink
    (מטוס→קרקע), אות = uplink (קרקע→מטוס). זה הכלל של acarsdec עצמו (output.c:
    ‏IS_DOWNLINK_BLK — ולכן msgno/flight נפלטים *רק* בבלוק downlink), לא heuristic.
    None כשאין block ID, או כשהתו אינו ספרה/אות לטינית (פריים פגום — לא מנחשים)."""
    if not isinstance(block_id, str) or len(block_id) != 1:
        return None
    if "0" <= block_id <= "9":
        return "downlink"
    if "A" <= block_id <= "Z" or "a" <= block_id <= "z":
        return "uplink"
    return None


def _acars_direction(label, text):
    """heuristic שמרני לכיוון ההודעה כשאין block ID (ר' _acars_block_dir): 'uplink'
    (קרקע→מטוס) / 'downlink' (מטוס→קרקע) / None. label מוכר קודם, אחרת header ניתוב
    בטקסט => uplink. None כשלא חד-משמעי (לא מנחשים)."""
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


# OOOI בפורמט QP–QS: מוצא(4) + יעד(4) + HHMM, במיקומים קבועים — הפריסה של acarsdec
# עצמו (label.c: ‏label_qp/qq/qr/qs). דוגמאות מקליטת 24.10.2026: ‏QR ‎"LFPGLLBG1136…"
# (‏F-GSQI נחת 11:36), ‏QS ‎"LFPGLLBG1145…". השאר אחרי השעה לא מתויג (לא מנחשים).
_OOOI_Q_EVENT = {"QP": "OUT", "QQ": "OFF", "QR": "ON", "QS": "IN"}


_OOOI_Q_RE = re.compile(r"^([A-Z]{4})([A-Z]{4})([01]\d|2[0-3])([0-5]\d)")


def _parse_oooi_q(label, text):
    """OOOI של QP/QQ/QR/QS: ‏"LFPG→LLBG · ON 11:36", או None כשהטקסט לא בפורמט."""
    ev = _OOOI_Q_EVENT.get(label)
    m = _OOOI_Q_RE.match(text.strip()) if (ev and isinstance(text, str)) else None
    if not m:
        return None
    sa, da, hh, mm = m.groups()
    return f"{sa}→{da} · {ev} {hh}:{mm}"


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
# ⚠ שדה המעלות ברוחב קבוע, והאפס המוביל יכול להגיע כ*רווח* — ובפועל זה הפורמט
# היחיד שנקלט: שלוש הודעות ה-‎"(2…" האמיתיות בייצוא (‏T7AVI ×2 אחרי ההמראה מנתב"ג
# ב-24.10.2026, ‏MSSML ב-11.07) הן ‎"E 34499…", ו-‎\d{3} דחה את כולן (lat=null).
# ‏int(" 34") = 34, ולכן _ddmmf לא משתנה. בחלופה המרופדת הדקות חייבות להסתיים לפני
# ספרה נוספת — אחרת ‎"E 034499" (רווח *וגם* אפס) היה נקרא כ-‎" 03"+‎"449".
_L15_RE = re.compile(r"^\(2([NS])( \d|\d{2})([0-5]\d\d)([EW])"
                     r"(\d{3}|(?:  \d| \d{2})(?=[0-5]\d\d(?!\d)))([0-5]\d\d)")


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


# וריאנט מופרד-'/' בלי waypoint (קליטת 24.10.2026, ‏TC-LCO/TK647):
# ‎'/N 30.796/E 31.504/38000/2410/1124/06312/1313/' — אותם נ"צ עשרוני + גובה בראש.
# השדות שאחרי הגובה לא מתויגים (מדגם יחיד — לא מנחשים).
_L16_SLASH_RE = re.compile(r"^/([NS])\s*(\d{1,2}\.\d+)/([EW])\s*(\d{1,3}\.\d+)/(\d{3,5})/")


def _parse_label16(text):
    """label 16: נ"צ עשרוני + גובה (שני וריאנטים — ר' למעלה). פחות נוקשה-פורמט
    מ-/.POS//label15 (שדות באורך משתנה) => לא נחלץ עם error (בניגוד לפורמטים
    המבניים ה-DDMM)."""
    if not text:
        return None
    m = _L16_RE.match(text.strip())
    if m:
        wpt, ns, la, ew, lo, alt = m.groups()
        prefix = f"WPT {wpt.strip()} · "
    else:
        m = _L16_SLASH_RE.match(text.strip())
        if not m:
            return None
        ns, la, ew, lo, alt = m.groups()
        prefix = ""
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
    return round(lat, 5), round(lon, 5), f"{prefix}{int(alt)}ft"


# דיווח מיקום בפורמט POS/CAS/LAT/LON (label 11, קליטת 24.10.2026, ‏B-5930/MU224):
# ‎'POS\nCAS 280,LAT N 32.424,LON E 31.721,ALT 34008,FOB 147000,UTC 111500,PD ----.--,…'
# פורמט חברה ולא ARINC — acarsdec עצמו מפרש label 11 בפורמט אחר לגמרי (‏"/DS …/ETA",
# label.c), ולכן העוגן הוא התבנית המלאה, לא ה-label לבדו. שישה שדות בשמם ובסדר קבוע
# => מבני כמו 1L (נחלץ גם עם error). אומת על שתי ההודעות של אותה טיסה: זו עם error=1
# (‏11:15:00z) וזו שבלי (‏11:31:01z) רחוקות 221 ק"מ ב-16 דק' — ‏~447kt, עקבי עם CAS 280
# ב-FL340. ‏WD/WS/TAT/PD לא מתויגים (לא מנחשים), ו-FOB בלי יחידה — הטקסט לא נושא אחת.
_L11_POS_RE = re.compile(
    r"^POS\s+CAS\s*(\d{2,3}),LAT\s*([NS])\s*(\d{1,2}\.\d+),LON\s*([EW])\s*(\d{1,3}\.\d+),"
    r"ALT\s*(\d{3,5}),FOB\s*(\d+),UTC\s*([01]\d|2[0-3])([0-5]\d)([0-5]\d)")


def _parse_label11_pos(text):
    """label 11 בפורמט POS/CAS/LAT/LON: ‏(lat, lon, decoded) או None."""
    if not text:
        return None
    m = _L11_POS_RE.match(text.strip())
    if not m:
        return None
    cas, ns, la, ew, lo, alt, fob, hh, mm, ss = m.groups()
    lat, lon = float(la), float(lo)
    if ns == "S":
        lat = -lat
    if ew == "W":
        lon = -lon
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return None
    decoded = f"UTC {hh}:{mm}:{ss}z · דלק {fob} · {int(alt)}ft · CAS {cas}kt"
    return round(lat, 5), round(lon, 5), decoded


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


def _parse_pdc(text, title="אישור טרום-המראה"):
    """PDC (label A3): יעד/מסלול-המראה/SID/סקוואק/תדר הבא/גובה טיפוס ראשוני —
    כל שדה אופציונלי, מוצגים רק אלה שנמצאו. משמש גם את B4 (הקריאה-החוזרת של
    המטוס מצטטת את אותו אישור) — עם title אחר, כדי לא להציג downlink כאישור מהקרקע."""
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
    return f"{title}: " + " · ".join(parts) if parts else None


_INTEREST_LABELS = {"A3", "C1", "15", "16", "1L"}   # labels שידועים כבעלי תוכן עשיר (PDC/loadsheet/מיקום/ניווט)


# ניהול קישור: גם כשיש להן decoded (‏SQ: תחנה+תדר; ‏SA: מצב מדיה) זו תעבורה תפעולית
# חוזרת, לא תוכן. בקליטת 24.10.2026 ‏SQ+SA היו 138 מתוך 160 ההודעות שסומנו "מעניינת"
# (‏86%) — והציפו את דוח הסשן ואת ההתראות.
_LINK_MGMT_LABELS = {"SQ", "SA", "Q0", "_d", "5V"}


def _interest_score(rec):
    """'מעניינת' = שווה תשומת לב מעבר לרעש התפעולי (ACK ריקים/link test/squitter
    חוזרים) — לא ציון מספרי מומצא, קריטריונים בינאריים מתוך שדות שכבר קיימים
    בכרטיס המנורמל (ר' docs/field-station-roadmap.md, §1 "האנליסט"). כל אחד
    מהם לבדו מספיק: קטגוריה לא-גנרית (לא comm/text), יש טקסט מפוענח, מיקום
    מ-ADS-C (איכות המיקום הגבוהה ביותר), או label שידוע כבעל תוכן עשיר."""
    if rec.get("group") not in (None, "comm", "text"):
        return True
    if rec.get("decoded") and rec.get("label") not in _LINK_MGMT_LABELS:
        return True
    if rec.get("pos_src") == "adsc":
        return True
    if rec.get("label") in _INTEREST_LABELS:
        return True
    return False


def _find_key(obj, key):
    """הערך הראשון (חיפוש לעומק) של מפתח בשם key בעץ JSON, או None."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() == key:
                return v
            r = _find_key(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_key(v, key)
            if r is not None:
                return r
    return None


def _adsc_details(obj):
    """פרטי ה-Basic report של ADS-C מתוך JSON של libacars, או None. המפתחות אומתו
    מ-libacars/adsc.c: ‏basic_report = lat/lon/alt/ts_sec/pos_accuracy_nm/
    nav_redundancy/tcas_avail; ‏earth_ref_data = true_trk_deg/gnd_spd_kts/vspd_ftmin.
    ‏ts_sec = "שניות מתחילת השעה" (רזולוציה 0.125ש') — הזמן שבו המיקום *נמדד*
    במטוס, שונה מזמן הקליטה. ‏pos_accuracy_nm: ‏-1.0 = "אין יכולת ניווט" (קוד 0)."""
    br = _find_key(obj, "basic_report")
    if not isinstance(br, dict):
        return None

    def num(d, k):
        v = d.get(k) if isinstance(d, dict) else None
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    out = {"alt_ft": num(br, "alt"), "ts_sec": num(br, "ts_sec"),
           "accuracy_nm": num(br, "pos_accuracy_nm"),
           "nav_redundancy": br.get("nav_redundancy") if isinstance(br.get("nav_redundancy"), bool) else None,
           "tcas_avail": br.get("tcas_avail") if isinstance(br.get("tcas_avail"), bool) else None}
    er = _find_key(obj, "earth_ref_data")
    if isinstance(er, dict):
        out["true_trk_deg"] = num(er, "true_trk_deg") if er.get("true_trk_valid", True) else None
        out["gnd_spd_kts"] = num(er, "gnd_spd_kts")
        out["vspd_ftmin"] = num(er, "vspd_ftmin")
    return out


def _clean_ident(v):
    """tail/flight עם תו בקרה (‎< 0x20 או DEL) => None. ‏ACARS הוא 7-bit ותו כזה לא
    יכול להיות חלק מרישום/טיסה — זה פריים שתוקן חלקית: בקליטת 24.10.2026 ‎'4L-B\\x01T'
    (‏error=3, המקסימום ש-acarsdec מתקן) יצר מטוס מזויף ברוסטר לצד ‏4L-GIT האמיתי.
    לא "מנקים" את התו (‎'4L-BT' היה רישום מומצא) — הזהות פשוט לא ידועה; הטקסט נשמר."""
    if isinstance(v, str) and any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in v):
        return None
    return v


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
    tail_val = _clean_ident(g("tail", "registration"))
    tail_is_station = (bool(tail_val) and bool(_UPLINK_HEADER_RE.match(str(tail_val)))
                       and not any(ch.isdigit() for ch in str(tail_val)))

    # פענוח ARINC-622 (libacars): kind => badge וקבוצה, וטקסט קריא אם יש.
    lat = lon = pos_src = decoded = adsc_info = None
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
                adsc_info = _adsc_details(libacars)

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

    # label 11 בפורמט POS/CAS/LAT/LON: שישה שדות בשמם ברצף קבוע => מבני כמו 1L.
    if lat is None and label == "11" and text:
        pos = _parse_label11_pos(text)
        if pos:
            lat, lon, pos_src = pos[0], pos[1], "label11"
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
        elif label == "B4":
            decoded = _parse_pdc(text, title="קריאה חוזרת של האישור")
        elif label in _OOOI_Q_EVENT:
            decoded = _parse_oooi_q(label, text)

    block_id = g("block_id")
    rec = {
        "t": g("timestamp") or time.time(),   # epoch seconds (float) מ-acarsdec (חסר => עכשיו)
        "freq": g("freq"),                    # MHz
        "level": level,                       # dBFS — מקורי מהמפענח, לא מעובד
        "snr": snr,                           # dB — רק כש-noise זמין (ר' docstring); אחרת None
        "label": label,
        "category": category,                 # תיאור קריא אחיד (label/ARINC-622)
        "group": group,                       # קבוצה לצבע ב-UI / עמודה בייצוא
        "tail": tail_val,
        "flight": _clean_ident(g("flight", "fid")),
        "mode": g("mode"),
        "msgno": g("msgno"),
        "block_id": block_id,                 # תו ה-block ID הגולמי (ARINC 618) — מקור הכיוון
        # כיוון: block ID מבני קודם (כל מסגרת acarsdec); בלעדיו — label/header best-effort
        # (‏VDL2/SATCOM המסונתזים אינם נושאים block ID ודורסים בכיוון מבני משלהם).
        "dir": _acars_block_dir(block_id) or _acars_direction(label, text),
        "lat": lat,
        "lon": lon,
        "pos_src": pos_src,                   # "adsc" | "pos-report" | "label15" | "nav-fuel"
                                               # | "label11" | "label16" | "text" | None
        "decoded": decoded,                   # טקסט מפוענח קצר (CPDLC/ATIS/OOOI וכו') או None
        "text": text,
        "error": m.get("error"),
        "actype": _extract_actype(label, text),  # סוג מטוס best-effort (H1/C1) או None
        # מוזרם החוצה (לא רק שדה-עזר פנימי) כדי ש-_aircraft_identity/_build_roster
        # לא יתייחסו לכתובת תחנת-קרקע כאילו היא רישום מטוס — בלעדיו הרוסטר
        # המאוחד הציג בעבר תחנות קרקע (SQ squitters וכו') כשורות "מטוס" משלהן.
        "tail_is_station": tail_is_station,
        # פרטי דוח ה-ADS-C (גובה, חותמת-זמן "שניות מתחילת השעה", דיוק) — רק כשהמיקום
        # בא מ-ADS-C. משמש את ההצלבה מול ADS-B (xcheck); None בכל השאר.
        "adsc": adsc_info,
    }
    rec["notable"] = _interest_score(rec)     # לדוח הסשן + סינון/התראות ב-UI (ר' §1 "האנליסט")
    return rec


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
    # כתובת ה-AES של צד-המטוס (= ICAO 24-bit, ר' ה-docstring) — בשדה *נפרד* מ-icao
    # (הרוסטר לא מתערבב עם VDL2), לשימוש ההצלבה מול ADS-B בלבד (xcheck).
    aes = (isu.get("src") or {}).get("addr") if "aircraft" in src_type else (
        (isu.get("dst") or {}).get("addr") if "aircraft" in dst_type else None)
    card["aes"] = adsb.norm_hex(aes)
    return card
