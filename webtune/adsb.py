#!/usr/bin/env python3
# ============================================================================
#  AIR-AM  -  ניתוח ADS-B: מסלול פעיל לנחיתות/המראות + אינדיקציית שיבוש GPS
# ----------------------------------------------------------------------------
#  thread דמון מושך כל ~15 שניות את המטוסים ברדיוס רחב סביב נתב"ג ממקור
#  ADS-B קהילתי חופשי (adsb.lol, גיבוי adsb.fi) ומסיק:
#   1. מסלול נחיתות פעיל - מטוסים בגישה סופית נצברים כאירועים; המסלול עם
#      הציון הגבוה (דעיכה אקספוננציאלית) הוא הפעיל. עמיד לשיבוש GPS האזורי:
#      מטוס שמיקומו מזויף (nic=0, "קופץ" ללבנון/ירדן) מזוהה לפי הכיוון (track)
#      והגובה הברומטרי - ששורדים את השיבוש. אין API ל"מסלול בשימוש" - הסקה.
#   2. מסלול המראות פעיל - אותו עיקרון על מטוסים מטפסים.
#   3. שיבוש GPS - אחוז המטוסים באזור שמשדרים NIC נמוך (<7), אותה שיטה
#      וספים כמו gpsjam.org (ירוק <2%, צהוב 2-10%, אדום >10%).
#   4. buffer מתגלגל למפה (docs/session-replay-design.md, שלב 1) - כל poll
#      מוסיף שורה ל-track.jsonl: תמונת-מצב של _S["aircraft"] בהצלחה, או שורת
#      "gap" בכשל. משמש את POST /api/sessions (שלב 2).
#   5. נתונים להצלבת ACARS↔ADS-B (docs/adsb-crosscheck-design.md): זמן *המדידה*
#      של כל מיקום (now − seen_pos, לא זמן קבלת ה-API), סוג המקור (adsb/mlat/...),
#      אינדקס לפי hex (גם למטוסים בלי רישום), מיפוי רישום↔hex נלמד, חיפוש ממוקד
#      לפי מטוס מחוץ לרדיוס (lookup) ו-trace היסטורי. הניתוח עצמו — xcheck.py.
#
#  עצמאי לחלוטין: stdlib בלבד, וכשל רשת לעולם לא נוגע בנתיב הרדיו -
#  הלולאה בולעת כל חריגה ו-snapshot() רק קורא מצב בזיכרון. lookup/trace נקראים
#  רק מ-thread ההצלבה או מבקשת "אמת" של המשתמש — לעולם לא מ-listener של רדיו.
# ============================================================================
import collections
import json
import math
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections import deque
from pathlib import Path

# --- גאומטריית נתב"ג (LLBG) -------------------------------------------------
# נקודת ייחוס (ARP) וקואורדינטות ה-threshold לכל כיוון נחיתה, מ-OurAirports.
# הקורסים אמיתיים (true), כמו track ב-ADS-B - לא מגנטיים.
ARP_LAT, ARP_LON = 32.0114, 34.8867
_COS_LAT = math.cos(math.radians(ARP_LAT))

# כיוון נחיתה -> (lat של ה-threshold, lon, קורס גישה אמיתי במעלות)
RUNWAYS = {
    "03": (31.99622, 34.88608, 29.0),
    "21": (32.01812, 34.90023, 209.0),
    "08": (32.01300, 34.86040, 80.0),
    "26": (32.01890, 34.89860, 260.0),
    "12": (32.01470, 34.86580, 121.4),
    "30": (31.99990, 34.89420, 301.4),
}

# --- כוונון האלגוריתם -------------------------------------------------------
# ⚠ 15 ולא 60: מטוס ב-250 קשר עובר 7.7 ק"מ/דקה - קפיצות בלתי-שמישות לאנימציה
# ב-docs/session-replay-design.md. עדיין נדיב פי 15 מ-adsb.fi ("rate limited to
# 1 request per second" — README הרשמי). ⚠ תיקון: כאן נכתב בעבר ש-adsb.lol
# "מרשה ~1/s" — ה-README שלו אומר בפועל "Rate limits are dynamic based on the
# environment load" (ר' §3 ב-docs/adsb-crosscheck-design.md). גם משפר את המסלול
# הפעיל/מחוון השיבוש הקיימים, שהיום מתעדכנים בהשהיה של דקה.
POLL_SEC = 15
RADIUS_NM = 250          # רחב בכוונה: מטוסים מזויפים "קופצים" ללבנון/ירדן (>50nm)
HTTP_TIMEOUT = 10.0
FRESH_SEC = 180.0        # משיכה ישנה מזה => fresh:false ב-API וכרטיס דהוי
FAILS_TO_SWITCH = 3      # כשלים רצופים עד מעבר למקור הגיבוי

WINDOW_MIN = 40.0        # חלון אירועי נחיתה/המראה
DECAY_TAU_MIN = 15.0     # דעיכת ציון: אירוע בן 15 דק' שוקל ~37% מאירוע טרי
DEDUP_MIN = 10.0         # אירוע אחד לכל מטוס (go-around לא נספר פעמיים)
SECONDARY_MIN = 20.0     # מסלול משני: נדרשות >=2 גישות ב-20 הדקות האחרונות

GPS_WINDOW_MIN = 15.0    # החלקה של יחס ה-NIC הפגום
GPS_MIN_SAMPLE = 10      # פחות מדגימות מזה => "אין נתונים" ולא ירוק כוזב
GPS_ALT_MIN = 5000.0     # מתחת לזה NIC נמוך נפוץ גם בלי שיבוש (multipath)

AC_KEEP_SEC = 600.0      # snapshot פר-מטוס (היתוך ACARS↔ADS-B): גיזום אחרי 10 דק'

# דוח סשן (ר' docs/field-station-roadmap.md, /api/session ב-app.py): בניגוד
# ל-gps_hist (15 דק', להחלקת היחס הרגעי בלבד) — סדרה ארוכה יותר, בזיכרון
# בלבד (לא נכתבת לדיסק — עקבי עם הבידוד הקיים: תקלת רשת/כתיבה לעולם לא נוגעת
# ברדיו), שנועדה במפורש להישכח בין הפעלות: "מה קרה בזמן שלא הסתכלת" הוא על
# הסשן הנוכחי, לא ארכיון קבוע.
# ⚠ 1440 ולא 360: כשהעלינו את POLL_SEC מ-60 ל-15 (פי 4), בלי לתקן כאן היה
# החלון האפקטיבי מתכווץ בשקט מ-6 שעות ל-1.5 שעות — בדיוק הפוך ממטרת "מה קרה
# בזמן שלא הסתכלת" (משתמש שחוזר אחרי 4 שעות היה מאבד את שעתיים-וחצי הראשונות).
SESSION_SERIES_MAX = 1440   # 1440 דגימות × POLL_SEC=15 = 6 שעות אחורה — סשן שטח טיפוסי

# גילוי עמיד-שיבוש: באזור נתב"ג השיבוש מתמשך - מטוסים בגישה משדרים מיקום
# מזויף או nic=0, אבל שדות ה-baro וה-track שורדים. nic=0 הוא בעצמו אות איתור:
# השיבוש מקומי => המטוס פיזית קרוב לשדה (מטוסים על הקרקע כלל לא מושפעים).
SPOOF_NIC = 2            # nic < זה => מיקום מזויף; מניחים שהמטוס קרוב לנתב"ג
NEAR_NM = 25.0           # מיקום אמין בתוך הרדיוס הזה = אזור המסוף של נתב"ג
TRACK_TOL = 25.0         # track בתוך כך מקורס המסלול => אותו מסלול (הקורסים רחוקים >40°)
LAND_RATE = -300.0       # קצב ירידה מרבי לגישה (ft/min)
LAND_GS_MIN, LAND_GS_MAX = 90.0, 200.0
ALT_MIN, ALT_MAX = 200.0, 6500.0   # גובה לחץ (טווח רחב - אין תיקון QNH)
TO_RATE = 500.0          # קצב טיפוס מזערי להמראה
TO_GS_MIN = 100.0
TO_ALT_MAX = 6000.0

# גישה סופית: שיפוע 5° סביב הקו המוארך, עד 12nm מה-threshold
_TAN_GLIDE = math.tan(math.radians(5.0))

# מקורות באותה סכמה (ADSBExchange v2). אחרי FAILS_TO_SWITCH כשלים מתחלפים.
# ⚠ ה-README של adsb.fi מתאר את ה-endpoint הזה כ-deprecated, שמחזיר "data in a
# different format than other v2 endpoints" (המומלץ: ‎/v3/lat/../lon/../dist/..).
# לא הוחלף בלי לבדוק תשובה חיה מה-Pi; בינתיים _ac_list מקבל גם "aircraft"
# (ר' docs/adsb-crosscheck-design.md §3).
SOURCES = [
    ("adsb.lol", "https://api.adsb.lol/v2/point/{lat}/{lon}/{r}"),
    ("adsb.fi", "https://opendata.adsb.fi/api/v2/lat/{lat}/lon/{lon}/dist/{r}"),
]

# --- חיפוש ממוקד לפי מטוס (מחוץ לרדיוס ה-poll) — docs/adsb-crosscheck-design.md §3 ---
# מטוס במרחק 700 ק"מ לא נמצא ב-poll (250nm מנתב"ג). במקום להגדיל את הרדיוס
# (בקשה כבדה כל 15ש'), שואלים על *מטוס אחד* — רק כשצריך. הנתיבים אומתו מקוד
# המקור של adsb.lol (api_v2.py) ומה-README של adsb.fi (אין שם ‎/v2/reg — רק
# ‎/v2/registration). שניהם בלי מפתח API; adsb.fi: אישי/לא-מסחרי + ציון המקור.
LOOKUP_SOURCES = [
    ("adsb.lol", {"hex": "https://api.adsb.lol/v2/hex/{v}",
                  "reg": "https://api.adsb.lol/v2/reg/{v}",
                  "callsign": "https://api.adsb.lol/v2/callsign/{v}"}),
    ("adsb.fi", {"hex": "https://opendata.adsb.fi/api/v2/hex/{v}",
                 "reg": "https://opendata.adsb.fi/api/v2/registration/{v}",
                 "callsign": "https://opendata.adsb.fi/api/v2/callsign/{v}"}),
]
LOOKUP_TIMEOUT = 6.0
LOOKUP_MAX_BYTES = 1_000_000
# הגבלת קצב *לכל ספק*: adsb.fi — ‎≤1/s וגם תשובות 4xx נספרות; adsb.lol — "דינמי".
# 2ש' בין בקשות ו-12 לדקה = לכל היותר poll נוסף כל 5ש', על מטוס בודד.
LOOKUP_MIN_INTERVAL = 2.0
LOOKUP_MAX_PER_MIN = 12
LOOKUP_TTL_HIT = 20.0            # מטוס שנמצא: המיקום מתיישן מהר
LOOKUP_TTL_MISS = 120.0          # לא נמצא: לא שואלים שוב על אותו מזהה דקותיים
LOOKUP_CACHE_MAX = 256

# --- trace היסטורי (tar1090/readsb) — "אמת מול ADS-B" על הודעה ישנה ------------
# ⚠ לא API מתועד: אלה הקבצים שממשק ה-globe של adsb.lol (tar1090) מושך בעצמו.
# תבנית הנתיב אומתה מ-tar1090 (script.js, getTrace): היום — ‎data/traces/<2 אחרונות>/
# trace_full_<hex>.json; ימים קודמים — ‎globe_history/YYYY/MM/DD/traces/... ⚠ *לא*
# אומת שהשרת מגיש אותם ועד כמה אחורה — ‎`python3 adsb.py --trace HEX` על ה-Pi בודק.
# כשל (404/403/timeout) = "אין נתונים", לעולם לא קריסה. רישיון הנתונים: ODbL.
TRACE_SOURCES = [
    ("adsb.lol", "https://globe.adsb.lol/data/traces/{last2}/trace_full_{hex}.json",
                 "https://globe.adsb.lol/globe_history/{date}/traces/{last2}/trace_full_{hex}.json"),
]
TRACE_TIMEOUT = 12.0
TRACE_MAX_BYTES = 6_000_000      # trace של יום שלם — מאות KB; חסם נגד תשובה חריגה/פצצת gzip
TRACE_MIN_INTERVAL = 5.0
TRACE_MAX_PER_MIN = 6
TRACE_TTL = 300.0
TRACE_LIVE_SPAN_SEC = 86400.0    # readsb שומר trace_full ליממה האחרונה

# --- OpenSky (אופציונלי) — מסלולים עד 30 יום אחורה ------------------------------
# אומת מתיעוד OpenSky (rest.rst): ‏/tracks/all "experimental", ≤30 יום, נקודת דרך
# לפחות כל 15 דק' + בשינוי track>2.5°/גובה>100m — כלומר דליל מכדי "אינטרפולציה"
# (xcheck.INTERP_MAX_GAP) ברוב המקרים: הקשר + מסלול למפה, לא אימות. מאז 18.3.2026
# רק OAuth2 client-credentials (‏AIRAM_OPENSKY_CLIENT_ID/SECRET ב-/etc/airam/airam.env);
# בלעדיהם מנסים אנונימי, ו-401/403 מדווח "נדרשים פרטי OpenSky".
OPENSKY_TRACK_URL = "https://opensky-network.org/api/tracks/all?icao24={hex}&time={t}"
OPENSKY_TOKEN_URL = ("https://auth.opensky-network.org/auth/realms/opensky-network/"
                     "protocol/openid-connect/token")
OPENSKY_MAX_AGE_SEC = 30 * 86400.0
OPENSKY_MIN_INTERVAL = 10.0
OPENSKY_MAX_PER_MIN = 4

# --- מיפוי רישום↔hex נלמד (כל poll רואה ~300 מטוסים עם שניהם) -------------------
# trace היסטורי ממופתח לפי hex, ו-ACARS ב-VHF נושא רק רישום. רישום↔hex קבוע לכל
# גוף-מטוס, ולכן מיפוי שנלמד אתמול תקף היום. נשמר נדיר (כל 10 דק', רק כשהשתנה).
REG_HEX_PATH = Path("/var/lib/airam/reg_hex.json")
REG_HEX_MAX = 20000
REG_HEX_SAVE_SEC = 600.0


# --- גאומטריה (פונקציות טהורות - נבדקות ב---selftest ללא רשת) ---------------
def _enu_nm(lat, lon, ref_lat, ref_lon):
    """מיקום יחסי שטוח ב-nm: ‏x מזרחה, y צפונה. מספיק מדויק ל-50nm."""
    return (lon - ref_lon) * 60.0 * _COS_LAT, (lat - ref_lat) * 60.0


def _norm180(deg):
    return (deg + 180.0) % 360.0 - 180.0


def _final_geometry(lat, lon, rwy):
    """along: ‏nm לפני ה-threshold לאורך קו הגישה (חיובי = בגישה סופית);
    cross: ‏nm הצידה מהקו המוארך (תמיד חיובי)."""
    thr_lat, thr_lon, crs = RUNWAYS[rwy]
    vx, vy = _enu_nm(lat, lon, thr_lat, thr_lon)
    rad = math.radians(crs)
    ux, uy = math.sin(rad), math.cos(rad)
    along = -(vx * ux + vy * uy)
    cross = abs(vx * uy - vy * ux)
    return along, cross


def _num(v):
    """float או None. ‏alt_baro יכול להיות "ground" - לא מספר."""
    return float(v) if isinstance(v, (int, float)) else None


def norm_reg(s):
    """נרמול רישום מטוס להשוואת ACARS↔ADS-B: אותיות גדולות, בלי תווים שאינם
    A-Z/0-9. ‏acarsdec מרפד בנקודות ('.4X-EHD'), ‏ADS-B עם מקף ('4X-EHD') —
    שניהם => '4XEHD'. מחזיר None על ריק."""
    if not s:
        return None
    return "".join(ch for ch in str(s).upper() if ch.isalnum()) or None


_HEX_RE = re.compile(r"^[0-9A-F]{6}$")


def norm_hex(v):
    """כתובת ICAO 24-bit מנורמלת (6 ספרות hex, אותיות גדולות) או None.
    ‏readsb מסמן כתובת לא-ICAO (TIS-B/אנונימית) ב-"~" — זו לא זהות מטוס."""
    if not v:
        return None
    s = str(v).strip().upper()
    if s.startswith("~"):
        return None
    return s if _HEX_RE.match(s) else None


def make_point(t, lat, lon, alt_baro=None, alt_geom=None, gs=None, track=None,
               src_type=None, nic=None, nac_p=None, provider=None, origin=None,
               t_uncert=0.0):
    """נקודת ADS-B בפורמט פנימי אחיד, מכל מקור (snapshot/lookup/trace/buffer) —
    הפורמט שהניתוח ב-xcheck.position_at צורך. t = זמן *המדידה* (epoch), לא זמן
    קבלת ה-API. ‏t_uncert = אי-ודאות ידועה בזמן המדידה (שורת buffer ישנה)."""
    return {"t": _num(t), "lat": _num(lat), "lon": _num(lon), "alt_baro": _num(alt_baro),
            "alt_geom": _num(alt_geom), "gs": _num(gs), "track": _num(track),
            "src_type": src_type if isinstance(src_type, str) else None,
            "nic": _num(nic), "nac_p": _num(nac_p), "provider": provider, "origin": origin,
            "t_uncert": _num(t_uncert) or 0.0}


# --- התאמת מסלול ------------------------------------------------------------
def _match_track(track):
    """המסלול שהקורס שלו הכי קרוב ל-track (בתוך TRACK_TOL), אחרת None.
    עמיד-שיבוש: track נגזר ממדידת מהירות, לא מ-lat/lon המזויף. הקורסים בנתב"ג
    רחוקים זה מזה (>40°) => אין דו-משמעות בתוך הטולרנס."""
    best = None
    for rwy, (_la, _lo, crs) in RUNWAYS.items():
        d = abs(_norm180(track - crs))
        if d < TRACK_TOL and (best is None or d < best[1]):
            best = (rwy, d)
    return best[0] if best else None


def _match_landing_pos(lat, lon, track):
    """מסלול נחיתה לפי גאומטריית הקו המוארך (משפך 5°) - כשהמיקום אמין."""
    best = None
    for rwy in RUNWAYS:
        along, cross = _final_geometry(lat, lon, rwy)
        if not (0.3 <= along <= 12.0):
            continue
        if cross >= max(0.6, along * _TAN_GLIDE):
            continue
        if abs(_norm180(track - RUNWAYS[rwy][2])) >= TRACK_TOL:
            continue
        if best is None or cross < best[1]:   # פיינלים מתכנסים => הקרוב לקו
            best = (rwy, cross)
    return best[0] if best else None


def _match_takeoff_pos(lat, lon, track):
    """מסלול המראה: טיפוס מעבר ל-threshold בכיוון המסלול, קרוב לשדה."""
    if math.hypot(*_enu_nm(lat, lon, ARP_LAT, ARP_LON)) > 8.0:
        return None
    best = None
    for rwy in RUNWAYS:
        along, cross = _final_geometry(lat, lon, rwy)
        if not (-0.5 <= -along <= 8.0):   # ‎-along = מרחק מעבר ל-threshold
            continue
        if cross >= 1.5 or abs(_norm180(track - RUNWAYS[rwy][2])) >= TRACK_TOL:
            continue
        if best is None or cross < best[1]:
            best = (rwy, cross)
    return best[0] if best else None


# --- סיווג מטוס בודד (עמיד לשיבוש GPS) --------------------------------------
def classify(ac):
    """מחזיר (kind, rwy, mode) או None.
    kind: "landing"|"takeoff" · mode: "pos" (מיקום אמין) | "track" (לפי כיוון).

    איתור המטוס לאזור נתב"ג:
      • מיקום אמין (nic תקין) וקרוב => משתמשים בגאומטריית הקו המוארך (mode=pos).
      • מיקום אמין אך רחוק (לרנקה/עמאן) => המטוס באמת במקום אחר => מתעלמים.
      • מזויף (nic<SPOOF_NIC) => אין מיקום שמיש, אבל השיבוש מקומי => המטוס קרוב;
        מסווגים לפי ה-track והגובה הברומטרי בלבד (mode=track)."""
    track = _num(ac.get("track"))
    gs = _num(ac.get("gs"))
    alt = _num(ac.get("alt_baro"))
    if alt is None:
        alt = _num(ac.get("alt_geom"))
    rate = _num(ac.get("baro_rate"))
    if rate is None:
        rate = _num(ac.get("geom_rate"))

    nic = _num(ac.get("nic"))
    spoofed = nic is not None and nic < SPOOF_NIC
    lat, lon = _num(ac.get("lat")), _num(ac.get("lon"))
    seen_pos = _num(ac.get("seen_pos")) or 0.0
    has_pos = lat is not None and lon is not None and seen_pos <= 60 and not spoofed

    if has_pos:
        if math.hypot(*_enu_nm(lat, lon, ARP_LAT, ARP_LON)) > NEAR_NM:
            return None                 # אמין ורחוק => לא בנתב"ג
    elif not spoofed:
        return None                     # אין מיקום אמין ואין דגל שיבוש => אי-אפשר לאתר

    if None in (track, gs, alt, rate):
        return None

    # נחיתה: יורד במהירות גישה בגובה לחץ נמוך
    if rate <= LAND_RATE and LAND_GS_MIN <= gs <= LAND_GS_MAX and ALT_MIN <= alt <= ALT_MAX:
        rwy = _match_landing_pos(lat, lon, track) if has_pos else _match_track(track)
        if rwy:
            return ("landing", rwy, "pos" if has_pos else "track")

    # המראה: מטפס מעל השדה בכיוון המסלול
    if rate >= TO_RATE and gs >= TO_GS_MIN and ALT_MIN <= alt <= TO_ALT_MAX:
        rwy = _match_takeoff_pos(lat, lon, track) if has_pos else _match_track(track)
        if rwy:
            return ("takeoff", rwy, "pos" if has_pos else "track")
    return None


# --- מצב משותף (נכתב ע"י ה-thread, נקרא ע"י snapshot) ------------------------
_LOCK = threading.Lock()
_THREAD = None
_S = {
    "events": deque(),        # (t_mono, rwy, kind, mode) — mode: "pos"/"track"
    "last_event": {},         # (hex, kind) -> t_mono  (dedup)
    "gps_hist": deque(),      # (t_mono, bad, total)
    "last_known": {},         # kind -> (rwy, t_mono)  - גם אחרי שהחלון התרוקן
    "last_ok": None,          # t_mono של המשיכה המוצלחת האחרונה
    "source": None,
    "error": None,
    "fails": 0,
    "src_idx": 0,
    "ac_count": 0,
    "spoofed_now": 0,         # מטוסים מזויפים (nic<SPOOF_NIC) בדגימה האחרונה
    "aircraft": {},           # norm_reg(r) -> רשומת מטוס אחרונה (היתוך ACARS↔ADS-B)
    "by_hex": {},             # norm_hex -> אותה רשומה, *כולל* מטוסים בלי רישום (הצלבה)
    "reg_hex": collections.OrderedDict(),   # norm_reg -> norm_hex נלמד (LRU, נשמר לדיסק)
    "reg_hex_loaded": False,
    "reg_hex_dirty": False,
    "reg_hex_saved_mono": None,
    "session_series": deque(maxlen=SESSION_SERIES_MAX),   # (t_wall, gps_ratio|None, runway|None) לדוח הסשן
    # None = עוד לא נדחס מאז העלייה => compaction כבר ב-append הראשון. בלי זה,
    # airam-web שמופעל מחדש כל פחות משעה לא דחס לעולם, והקובץ גדל בלי גבול.
    "track_appends": None,    # מונה מ-compaction אחרון (ר' _append_track/_compact_track)
}


def _fetch(src_idx):
    name, tmpl = SOURCES[src_idx]
    url = tmpl.format(lat=ARP_LAT, lon=ARP_LON, r=RADIUS_NM)
    req = urllib.request.Request(url, headers={"User-Agent": "AIR-AM/1.0"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return name, json.loads(resp.read().decode("utf-8"))


def _resp_now(data):
    """זמן התמונה של המקור (epoch שניות) — הבסיס לזמן המדידה (now − seen_pos).
    ‏readsb jv2 (adsb.lol): ‏"now" במילישניות (README-json.md); נורמלי — בשניות.
    חסר => שעון ה-Pi (זמן הקבלה; קירוב, ולכן seen_pos עדיין נלקח בחשבון)."""
    v = data.get("now") if isinstance(data, dict) else None
    if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
        return v / 1000.0 if v > 1e11 else float(v)
    return time.time()


def _ac_list(data):
    """רשימת המטוסים מתשובת מקור, או None כשהצורה לא צפויה. ‏"ac" הוא הסכמה
    המתועדת; "aircraft" מתקבל גם כן (ר' ההערה ליד SOURCES על adsb.fi)."""
    if not isinstance(data, dict):
        return None
    for key in ("ac", "aircraft"):
        v = data.get(key)
        if isinstance(v, list):
            return v
    return None


def _ac_record(ac, resp_now):
    """רשומת מטוס אחידה מאובייקט readsb — משותפת ל-poll ול-lookup הממוקד.
    מיקום מפורסם רק כשהוא אמין: nic<SPOOF_NIC = מזויף (שיבוש GPS) => lat/lon
    מדוכאים, אבל גובה/מהירות/track (ששורדים שיבוש) נשמרים תמיד. השדות שמתחת
    ל"הרחבה" חדשים (הצלבה, docs/adsb-crosscheck-design.md) — הקיימים לא השתנו."""
    nic = _num(ac.get("nic"))
    ac_spoofed = nic is not None and nic < SPOOF_NIC
    lat, lon = _num(ac.get("lat")), _num(ac.get("lon"))
    seen_pos = _num(ac.get("seen_pos"))
    pos_ok = (lat is not None and lon is not None
              and (seen_pos or 0) <= 60 and not ac_spoofed)
    on_ground = ac.get("alt_baro") == "ground"
    alt = _num(ac.get("alt_baro"))
    if alt is None:
        alt = _num(ac.get("alt_geom"))
    reg = ac.get("r")
    stype = ac.get("type")
    return {
        "reg": str(reg).strip() if reg else None,
        "hex": ac.get("hex"),
        "flight": (ac.get("flight") or "").strip() or None,
        "type": ac.get("t") or None,
        "lat": lat if pos_ok else None,
        "lon": lon if pos_ok else None,
        "alt": None if on_ground else alt,
        "ground": on_ground,
        "gs": _num(ac.get("gs")),
        "track": _num(ac.get("track")),
        "nic": nic,
        "spoofed": ac_spoofed,
        "pos_ok": pos_ok,
        # --- הרחבה ---
        "alt_baro": None if on_ground else _num(ac.get("alt_baro")),
        "alt_geom": None if on_ground else _num(ac.get("alt_geom")),
        "nac_p": _num(ac.get("nac_p")),
        "rc": _num(ac.get("rc")),               # Radius of Containment, מטרים
        "src_type": stype if isinstance(stype, str) else None,   # adsb_icao/mlat/tisb_*/adsc/...
        "seen_pos": seen_pos,
        # זמן *המדידה* של המיקום (לא זמן קבלת ה-API): ‏now של המקור − seen_pos
        "t_pos": round(resp_now - (seen_pos or 0.0), 1) if pos_ok else None,
    }


def _learn_reg_hex(reg, hx):
    """רישום↔hex נלמד (נקרא תחת _LOCK). LRU חסום ב-REG_HEX_MAX."""
    m = _S["reg_hex"]
    if m.get(reg) != hx:
        m[reg] = hx
        _S["reg_hex_dirty"] = True
    m.move_to_end(reg)
    while len(m) > REG_HEX_MAX:
        m.popitem(last=False)
        _S["reg_hex_dirty"] = True


def process(ac_list, now=None, resp_now=None):
    """מעדכן את המצב מרשימת מטוסים אחת (poll או fixture). מחזיר את מספרם.
    ‏resp_now = זמן התמונה של המקור (epoch, ר' _resp_now); None => שעון ה-Pi."""
    now = time.monotonic() if now is None else now
    resp_now = time.time() if resp_now is None else resp_now
    events, last_event = _S["events"], _S["last_event"]

    bad = total = spoofed = 0
    for ac in ac_list:
        # ⚠ מקור חיצוני (adsb.lol/adsb.fi) => לא מניחים סכמה. רשומה שאינה
        # אובייקט הפילה את *כל* ה-poll ב-AttributeError: ‏_loop אמנם תופס ולא
        # מפיל את ה-thread, אבל המנה כולה נזרקת — כלומר דקה שלמה בלי מסלול
        # פעיל ובלי אינדיקציית שיבוש, בגלל רשומה פגומה אחת. דילוג נקודתי
        # שומר על שאר המנה (§12: בידוד מקורות חיצוניים, נפילה חיננית).
        if not isinstance(ac, dict):
            continue
        nic = _num(ac.get("nic"))
        # ספירת מזויפים (להצגה): מטוסים באוויר עם nic נמוך
        if ac.get("alt_baro") != "ground" and nic is not None and nic < SPOOF_NIC:
            spoofed += 1
        # אינדיקציית GPS: דגימת NIC מעל GPS_ALT_MIN (nac_p רק כשאין nic)
        alt = _num(ac.get("alt_baro"))
        if alt is None:
            alt = _num(ac.get("alt_geom"))
        if alt is not None and alt > GPS_ALT_MIN and (_num(ac.get("seen_pos")) or 0) < 60:
            integ = nic if nic is not None else _num(ac.get("nac_p"))
            if integ is not None:
                total += 1
                bad += integ < 7

        # snapshot פר-מטוס (היתוך ACARS↔ADS-B; ר' _ac_record). ‏by_hex כולל גם
        # מטוסים בלי רישום — VDL2/SATCOM נושאים כתובת ICAO מבנית, ומטוס בלי רשומה
        # במאגר (צבאי וכו') הוא בדיוק המטוס שהכי מעניין להצליב.
        reg = norm_reg(ac.get("r"))
        hx = norm_hex(ac.get("hex"))
        if reg or hx:
            rec = _ac_record(ac, resp_now)
            rec["t_mono"] = now
            if reg:
                _S["aircraft"][reg] = rec
            if hx:
                _S["by_hex"][hx] = rec
            if reg and hx:
                _learn_reg_hex(reg, hx)

        hit = classify(ac)
        if hit:
            kind, rwy, mode = hit
            key = (ac.get("hex"), kind)
            prev = last_event.get(key)
            if prev is None or now - prev >= DEDUP_MIN * 60:
                last_event[key] = now
                events.append((now, rwy, kind, mode))
                _S["last_known"][kind] = (rwy, now)

    _S["gps_hist"].append((now, bad, total))
    _S["spoofed_now"] = spoofed
    _S["last_process_mono"] = now      # ר' _build_track_row: רק מטוסים מה-poll הזה

    # גיזום חלונות + מפתחות dedup ישנים
    while events and now - events[0][0] > WINDOW_MIN * 60:
        events.popleft()
    while _S["gps_hist"] and now - _S["gps_hist"][0][0] > GPS_WINDOW_MIN * 60:
        _S["gps_hist"].popleft()
    for key in [k for k, t in last_event.items() if now - t > DEDUP_MIN * 60]:
        del last_event[key]
    for index in (_S["aircraft"], _S["by_hex"]):
        for key in [k for k, rec in index.items() if now - rec["t_mono"] > AC_KEEP_SEC]:
            del index[key]                # חסם זיכרון: מטוס שיצא מטווח נגזם
    return len(ac_list)


def _decide_runway(kind, now):
    """המסלול הפעיל לסוג אירוע: ציון עם דעיכה + override לרצף עדכני.
    מחזיר (primary, secondary, count, last_age_min, pos_confirmed)."""
    evs = [(t, rwy, mode) for t, rwy, k, mode in _S["events"] if k == kind]
    if not evs:
        return None, None, 0, None, False
    scores = {}
    for t, rwy, m in evs:
        # זיהוי-מיקום אמין יותר מזיהוי-כיוון => משקל מעט גבוה (שובר תיקו לטובתו,
        # אך נפח של זיהויי-כיוון בשיבוש כבד עדיין גובר)
        w = 1.0 if m == "pos" else 0.7
        scores[rwy] = scores.get(rwy, 0.0) + w * math.exp(-(now - t) / 60.0 / DECAY_TAU_MIN)
    primary = max(scores, key=scores.get)
    # החלפת מסלול באמצע החלון: 3 מתוך 4 האירועים האחרונים גוברים על הציון
    last4 = [rwy for _t, rwy, _m in evs[-4:]]
    if len(last4) == 4:
        for rwy in set(last4):
            if rwy != primary and last4.count(rwy) >= 3:
                primary = rwy
                break
    secondary = None
    if kind == "landing":   # בשעות שיא נוחתים בנתב"ג על שני מסלולים במקביל
        cand = {r: s for r, s in scores.items()
                if r != primary and s >= 0.3 * scores[primary]
                and sum(1 for t, rw, _m in evs if rw == r and now - t <= SECONDARY_MIN * 60) >= 2}
        if cand:
            secondary = max(cand, key=cand.get)
    last_age_min = (now - max(t for t, _r, _m in evs)) / 60.0
    pos_confirmed = any(m == "pos" for _t, rw, m in evs if rw == primary)
    return primary, secondary, len(evs), last_age_min, pos_confirmed


def aircraft_snapshot(regs=None):
    """רשומות פר-מטוס להעשרת ACARS (‏/api/acars). מפתח: רישום מנורמל (norm_reg).
    ‏regs (set של רישומים מנורמלים) => מוחזרים רק המטוסים המבוקשים — כך התשובה
    נושאת רק זנבות שמופיעים ב-ACARS, לא את כל ~300 המטוסים ברדיוס.
    קריאה בלבד תחת הנעילה; מחזיר עותקים עם age (שניות מאז שנראה). לעולם לא זורק."""
    out = {}
    try:
        with _LOCK:
            now = time.monotonic()
            for reg, rec in _S["aircraft"].items():
                if regs is not None and reg not in regs:
                    continue
                r = dict(rec)
                r["age"] = round(now - r.pop("t_mono"), 1)
                out[reg] = r
    except Exception:                     # אותו חוזה כמו snapshot: לא נוגעים ברדיו
        pass
    return out


def _copy_rec(rec, now):
    r = dict(rec)
    r["age"] = round(now - r.pop("t_mono"), 1)
    return r


def local_aircraft(hex_=None, reg=None):
    """רשומת מטוס מה-snapshot החי לפי hex (גם בלי רישום) או רישום — עותק עם
    age, או None. קריאה בלבד, בלי רשת; לעולם לא זורק."""
    try:
        with _LOCK:
            now = time.monotonic()
            hx, rg = norm_hex(hex_), norm_reg(reg)
            if hx and hx in _S["by_hex"]:
                return _copy_rec(_S["by_hex"][hx], now)
            if rg and rg in _S["aircraft"]:
                return _copy_rec(_S["aircraft"][rg], now)
    except Exception:
        pass
    return None


def all_aircraft():
    """כל המטוסים ב-snapshot החי (עותקים עם age) — להתאמה לפי טיסה (xcheck)."""
    try:
        with _LOCK:
            now = time.monotonic()
            return [_copy_rec(rec, now) for rec in _S["by_hex"].values()]
    except Exception:
        return []


def reg_to_hex(reg):
    """hex שנלמד לרישום הזה (snapshot חי או מיפוי שמור), או None. רישום↔hex קבוע
    לגוף-מטוס, ולכן תקף גם להודעה מאתמול. המקור הוא שדה r של ADS-B (מאגר
    readsb), *לא* ההודעה — xcheck מסמן את זה כ-hex_src="adsb_db"."""
    rg = norm_reg(reg)
    if not rg:
        return None
    _load_reg_hex()
    with _LOCK:
        rec = _S["aircraft"].get(rg)
        if rec and norm_hex(rec.get("hex")):
            return norm_hex(rec["hex"])
        return _S["reg_hex"].get(rg)


def _load_reg_hex():
    """טוען את המיפוי השמור פעם אחת. קובץ חסר/פגום => מתחילים ריק (נוחות, לא state)."""
    with _LOCK:
        if _S["reg_hex_loaded"]:
            return
        _S["reg_hex_loaded"] = True
    try:
        raw = json.loads(REG_HEX_PATH.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return
    if not isinstance(raw, dict):
        return
    with _LOCK:
        m = _S["reg_hex"]
        for k, v in list(raw.items())[-REG_HEX_MAX:]:
            rg, hx = norm_reg(k), norm_hex(v)
            if rg and hx and rg not in m:
                m[rg] = hx
                m.move_to_end(rg, last=False)   # הנלמד בסשן הנוכחי נשאר "חדש" יותר


def _save_reg_hex(force=False):
    """כתיבה אטומית (tmp+fsync+rename), רק כשהשתנה ולכל היותר כל REG_HEX_SAVE_SEC.
    כשל כתיבה נבלע — המיפוי נבנה מחדש מה-polls בכל מקרה."""
    with _LOCK:
        now = time.monotonic()
        last = _S["reg_hex_saved_mono"]
        if not _S["reg_hex_dirty"] or (not force and last is not None
                                       and now - last < REG_HEX_SAVE_SEC):
            return
        data = json.dumps(dict(_S["reg_hex"]), separators=(",", ":"))
        _S["reg_hex_dirty"] = False
        _S["reg_hex_saved_mono"] = now
    tmp = REG_HEX_PATH.with_name(REG_HEX_PATH.name + f".tmp{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, REG_HEX_PATH)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def snapshot():
    """תמונת המצב ל-/api/airspace. תמיד מצליח - אף פעם לא זורק."""
    with _LOCK:
        now = time.monotonic()
        ok_age = None if _S["last_ok"] is None else now - _S["last_ok"]
        fresh = ok_age is not None and ok_age <= FRESH_SEC

        landing, secondary, n_land, land_age, land_pos = _decide_runway("landing", now)
        takeoff, _, _, to_age, _ = _decide_runway("takeoff", now)
        method = ("position" if land_pos else "track") if landing else None
        confidence = "none"
        if landing:
            # ביטחון גבוה דורש אישור מיקום; זיהוי לפי כיוון בלבד (שיבוש) => "low"
            confidence = "high" if (land_pos and n_land >= 3 and land_age < 15) else "low"
        lk = _S["last_known"].get("landing")
        last_known = ({"landing": lk[0], "age_min": round((now - lk[1]) / 60.0, 1)}
                      if lk else None)

        g_bad = sum(b for _, b, _t in _S["gps_hist"])
        g_total = sum(t for _, _b, t in _S["gps_hist"])
        if not fresh or g_total < GPS_MIN_SAMPLE:
            gps_status, ratio = "unknown", None
        else:
            ratio = g_bad / g_total
            gps_status = "ok" if ratio < 0.02 else ("moderate" if ratio <= 0.10 else "severe")

        return {
            "ok": True,
            "fresh": fresh,
            "age": None if ok_age is None else round(ok_age, 1),
            "source": _S["source"],
            "error": _S["error"],
            "ac_count": _S["ac_count"],
            "runway": {
                "landing": landing,
                "landing_secondary": secondary,
                "takeoff": takeoff,
                "landing_count": n_land,
                "last_landing_age_min": None if land_age is None else round(land_age, 1),
                "last_takeoff_age_min": None if to_age is None else round(to_age, 1),
                "confidence": confidence,
                "method": method,
                "last_known": last_known,
            },
            "gps": {"status": gps_status,
                    "bad_ratio": None if ratio is None else round(ratio, 4),
                    "sample_n": g_total,
                    "spoofed_count": _S["spoofed_now"]},
        }


def session_series(since=None):
    """סדרת (t, gps_bad_ratio, runway) לדוח הסשן (/api/session ב-app.py) —
    דגימה אחת לכל poll (~60 שנייה), בזיכרון בלבד (ר' SESSION_SERIES_MAX).
    ‏since (epoch שניות, t_wall) מסנן דגימות ישנות ממנו; None => הכול שנשמר.
    לעולם לא זורק — אותו חוזה כמו snapshot()/aircraft_snapshot()."""
    try:
        with _LOCK:
            series = list(_S["session_series"])
    except Exception:
        return []
    if since is not None:
        series = [s for s in series if s[0] >= since]
    return [{"t": t, "gps_bad_ratio": ratio, "runway": rwy} for t, ratio, rwy in series]


# --- buffer מתגלגל של מסלולי ADS-B (docs/session-replay-design.md, שלב 1) ---
# ⚠ בכוונה **לא** כתיבה-אטומית-על-כל-שורה (tmp+fsync+rename, כמו state.json):
# זה rolling buffer אפמרי, לא state שחייב לשרוד ניתוק-חשמל בלי שריטה — אובדן
# השורה האחרונה בקריסה הוא לא-נורא (התוצאה היחידה: שורת JSON חתוכה, שנפסלת
# בשקט בקריאה כמו כל שורת-jsonl פגומה אחרת בפרויקט). הכתיבה קורית כל
# ‏POLL_SEC (15 שניות) לנצח — fsync על כל שורה הוא עלות אמיתית וקבועה על כרטיס
# ה-SD לתועלת שולית. append גולמי זול; compaction (סינון-לפי-גיל + כתיבה
# אטומית אמיתית עם fsync) קורה נדיר — ר' _compact_track — כי שם כשל-חצי-כתיבה
# היה מאבד את *כל* הבאפר, לא רק שורה אחת.
TRACK_PATH = Path("/var/lib/airam/track.jsonl")
TRACK_BUFFER_MIN = 90.0        # חלון רטרואקטיבי ל"שמור סשן" (שלב 2, טרם מומש)
TRACK_COMPACT_EVERY = 240      # ~שעה ב-POLL_SEC=15 — לא compaction בכל append


def _build_track_row():
    """שורת buffer מ-_S['aircraft'] הנוכחי. נקרא *בתוך* _LOCK (עקביות הקריאה
    מול process() שרץ באותו thread ממש לפני כן) — הכתיבה לדיסק עצמה קורית
    מחוץ לנעילה (_append_track), כדי לא לחסום קוראים אחרים על I/O."""
    ac_rows = []
    seen_now = _S.get("last_process_mono")
    row_t = round(time.time(), 1)
    for reg, rec in _S["aircraft"].items():
        # ⚠ _S["aircraft"] שומר מטוס עוד AC_KEEP_SEC (10 דק') אחרי שיצא מהפיד —
        # בשביל היתוך ACARS, לא בשביל המסלול. בלי הסינון, השחזור הציג מטוס
        # "קפוא" במיקומו האחרון עד 10 דק', בניגוד ל"לא בשורה = לא נראה" (§4.1).
        if rec["t_mono"] != seen_now:
            continue
        lat = round(rec["lat"], 5) if rec["lat"] is not None else None
        lon = round(rec["lon"], 5) if rec["lon"] is not None else None
        alt = round(rec["alt"]) if rec["alt"] is not None else None
        gs = round(rec["gs"], 1) if rec["gs"] is not None else None
        trk = round(rec["track"], 1) if rec["track"] is not None else None
        # ⚠ lat/lon=None נשמר *במפורש* (לא מדלגים על המטוס) — מבדיל "לא נראה"
        # מ"נראה, מיקום משובש" (§7.1 במסמך התכנון). מטוס משובש עדיין נכנס
        # לשורה עם alt/gs/track ששרדו את השיבוש, כמו ב-aircraft_snapshot.
        # הרחבה (הצלבה, docs/adsb-crosscheck-design.md): [7]=hex, [8]=גיל המיקום
        # בשניות ביחס ל-t של השורה (זמן המדידה = t − [8]). קוראים לפי אינדקס
        # (נגן השחזור: [0..3]) לא מושפעים; שורה ישנה בלי [8] => זמן מדידה לא ידוע
        # עד 60ש' (pos_ok דורש seen_pos≤60) — ר' local_track_points.
        t_pos = rec.get("t_pos")
        age = round(max(0.0, row_t - t_pos), 1) if (t_pos is not None and lat is not None) else None
        ac_rows.append([reg, lat, lon, alt, trk, gs, rec["nic"], rec.get("hex"), age])
    return {"t": row_t, "ac": ac_rows}


def _append_track(row):
    """מוסיף שורה אחת ל-track.jsonl (append גולמי, לא rewrite של הקובץ כולו).
    כשל כתיבה (דיסק מלא/הרשאה) נבלע — buffer אפמרי, לא שווה להפיל עליו poll."""
    data = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
    try:
        with open(TRACK_PATH, "a+b") as f:
            # ⚠ כיבוי פתאומי משאיר שורה קטועה בלי \n (או זנב NUL של ext4);
            # append רגיל היה מדביק אליה את השורה הבאה, והקורא מדלג על השתיים
            # יחד — אובדת הדגימה הראשונה אחרי כל כיבוי. אותה לוגיקה כמו
            # _append_line_on_boundary ב-app.py (המודול הזה עצמאי בכוונה).
            f.seek(0, os.SEEK_END)
            if f.tell() > 0:
                f.seek(-1, os.SEEK_END)
                if f.read(1) != b"\n":
                    data = b"\n" + data
            f.write(data)
    except OSError:
        return
    n = _S["track_appends"]
    _S["track_appends"] = TRACK_COMPACT_EVERY if n is None else n + 1
    if _S["track_appends"] >= TRACK_COMPACT_EVERY:
        try:
            _compact_track()
        finally:
            _S["track_appends"] = 0     # חריגה לא תגרום ל-compaction בכל poll


def _compact_track():
    """קורא את כל הבאפר, זורק שורות מעל TRACK_BUFFER_MIN דקות, וכותב מחדש
    אטומית (tmp+fsync+rename) — הפעולה הנדירה היחידה שבאמת כותבת את כל
    הקובץ, ולכן כאן כן משתלם fsync (בניגוד ל-_append_track)."""
    cutoff = time.time() - TRACK_BUFFER_MIN * 60.0
    try:
        lines = TRACK_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    kept = []
    for ln in lines:
        try:
            row = json.loads(ln)
        except ValueError:
            continue                      # שורה פגומה (כתיבה שנקטעה) — מדלגים, לא כושלים
        if not isinstance(row, dict):     # JSON תקין שאינו אובייקט (שריד-כתיבה) — כמו בקוראים
            continue
        t = row.get("t")
        if isinstance(t, (int, float)) and t >= cutoff:
            kept.append(ln)
    text = "\n".join(kept) + ("\n" if kept else "")
    tmp = TRACK_PATH.with_name(TRACK_PATH.name + f".tmp{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, TRACK_PATH)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def read_track_buffer():
    """מצב הבאפר הנוכחי מהדיסק, ל-GET /api/replay/buffer ב-app.py. קורא ישירות
    מהקובץ (לא ממצב-בזיכרון) — הקובץ הוא מקור-האמת. לעולם לא זורק, כמו
    snapshot()/session_series()."""
    try:
        lines = TRACK_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {"t_oldest": None, "samples": 0, "gaps": []}
    t_oldest = None
    samples = 0
    gaps = []
    # שורות מעבר לחלון (לפני compaction, למשל אחרי השבתה) לא נספרות: אחרת ה-UI
    # הציג "ניתן לשמור 1200 דק' אחורה" כשבפועל POST /api/sessions חותך ל-90.
    cutoff = time.time() - TRACK_BUFFER_MIN * 60.0
    for ln in lines:
        try:
            row = json.loads(ln)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        t = row.get("t")
        if not isinstance(t, (int, float)) or t < cutoff:
            continue
        if t_oldest is None or t < t_oldest:
            t_oldest = t
        if "gap" in row:
            gaps.append({"t": t, "reason": row.get("gap"), "detail": row.get("detail")})
        else:
            samples += 1
    return {"t_oldest": t_oldest, "samples": samples, "gaps": gaps}


def read_track_slice(t_start, t_end):
    """כל שורות הבאפר (ac + gap) בטווח [t_start, t_end], ל-POST /api/sessions
    ב-app.py (שלב 2). מחזיר את הרשומות המפוענחות כמו שהן — app.py אחראי על
    gzip/כתיבה, לא adsb.py. שורה פגומה מדולגת בשקט, כמו read_track_buffer."""
    try:
        lines = TRACK_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            row = json.loads(ln)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        t = row.get("t")
        if isinstance(t, (int, float)) and t_start <= t <= t_end:
            out.append(row)
    return out


def local_track_points(t_start, t_end, reg=None, hex_=None):
    """נקודות מה-buffer המקומי (track.jsonl, ‏90 דק', ‏250nm) למטוס אחד, בפורמט
    make_point — המקור ההיסטורי הראשון, בלי רשת. מחזיר (points, gaps).
    ⚠ ערך הגובה בשורה הוא alt_baro (או alt_geom כשאין baro — כמו ב-snapshot),
    ולכן מסומן כ-baro (השמרני מבין השניים לאופק)."""
    rg, hx = norm_reg(reg), norm_hex(hex_)
    if not rg and not hx:
        return [], []
    pts, gaps = [], []
    for row in read_track_slice(t_start, t_end):
        t = row.get("t")
        if "gap" in row:
            gaps.append({"t": t, "reason": row.get("gap"), "detail": row.get("detail")})
            continue
        for a in row.get("ac") or []:
            if not isinstance(a, list) or len(a) < 7:
                continue
            if not ((rg and norm_reg(a[0]) == rg) or (hx and len(a) > 7 and norm_hex(a[7]) == hx)):
                continue
            age = _num(a[8]) if len(a) > 8 else None
            pts.append(make_point(
                t - age if age is not None else t, a[1], a[2], alt_baro=a[3], track=a[4],
                gs=a[5], nic=a[6], provider="local", origin="buffer",
                # שורה בלי גיל-מיקום (לפני ההרחבה): המדידה הייתה עד 60ש' לפני t
                t_uncert=0.0 if age is not None else 60.0))
    return pts, gaps


# --- רשת: הגבלת קצב לכל ספק + GET חסום ----------------------------------------
# נעילה נפרדת מ-_LOCK בכוונה: בקשת HTTP (עד TRACE_TIMEOUT) לעולם לא מחזיקה את
# הנעילה שה-poll וה-snapshot צריכים — הנעילה כאן מגינה רק על הספירה.
_NET_LOCK = threading.Lock()
_NET = {"last": {}, "window": {}, "stats": {}, "cache": collections.OrderedDict(),
        "opensky_token": None}


def _rate_take(provider, min_interval, max_per_min):
    """מקצה "אסימון" לבקשה לספק. False = נחסם (לא ממתינים — הקורא מדווח)."""
    with _NET_LOCK:
        now = time.monotonic()
        last = _NET["last"].get(provider)
        if last is not None and now - last < min_interval:
            return False
        win = _NET["window"].setdefault(provider, deque())
        while win and now - win[0] > 60.0:
            win.popleft()
        if len(win) >= max_per_min:
            return False
        win.append(now)
        _NET["last"][provider] = now
        return True


def _net_note(provider, ok, err=None):
    with _NET_LOCK:
        s = _NET["stats"].setdefault(provider, {"ok": 0, "fail": 0, "limited": 0,
                                                "last_ok": None, "last_error": None})
        if ok is None:
            s["limited"] += 1
        elif ok:
            s["ok"] += 1
            s["last_ok"] = round(time.time(), 1)
        else:
            s["fail"] += 1
            s["last_error"] = {"t": round(time.time(), 1), "error": str(err)[:200]}


def _cache_get(key):
    with _NET_LOCK:
        hit = _NET["cache"].get(key)
        if hit and hit[0] > time.monotonic():
            _NET["cache"].move_to_end(key)
            return hit[1]
    return None


def _cache_put(key, value, ttl):
    with _NET_LOCK:
        _NET["cache"][key] = (time.monotonic() + ttl, value)
        _NET["cache"].move_to_end(key)
        while len(_NET["cache"]) > LOOKUP_CACHE_MAX:
            _NET["cache"].popitem(last=False)


class _HTTPStatus(Exception):
    def __init__(self, code):
        super().__init__(f"HTTP {code}")
        self.code = code


def _http_get_json(url, timeout, max_bytes, headers=None):
    """GET + JSON, חסום בגודל (גם אחרי gzip — tar1090 מגיש traces דחוסים, ו-urllib
    לא פורס לבד). HTTPError => _HTTPStatus (404 = "אין", לא "תקלה")."""
    req = urllib.request.Request(url, headers={"User-Agent": "AIR-AM/1.0", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(max_bytes + 1)
    except urllib.error.HTTPError as e:
        raise _HTTPStatus(e.code) from None
    return _decode_json_bytes(body, max_bytes)


def _decode_json_bytes(body, max_bytes):
    """bytes => JSON, חסום בגודל גם אחרי gzip (פצצת דחיסה). משותף ל-GET ולקובץ
    trace שהמשתמש מעלה (import_trace) — globe_history דוחס את קובצי ה-trace."""
    if len(body) > max_bytes:
        raise ValueError("גדול מהחסם")
    if body[:2] == b"\x1f\x8b":
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        body = d.decompress(body, max_bytes)
        if d.unconsumed_tail:
            raise ValueError("תוכן דחוס גדול מהחסם")
    return json.loads(body.decode("utf-8"))


def lookup(kind, value):
    """חיפוש ממוקד של מטוס בודד ב-API החי (מחוץ לרדיוס ה-poll). kind: "hex" |
    "reg" | "callsign" (אות-קריאה ICAO, למשל BAW60). מחזיר
    {"status": "ok"|"miss"|"rate_limited"|"error"|"invalid", "provider",
     "fetched_at", "aircraft": [רשומות _ac_record + provider/fetched_at], "errors"}.
    cache (hit/miss), הגבלת קצב לכל ספק, timeout, ו-fallback לספק הבא. לעולם לא
    זורק. ⚠ רשת — לקרוא רק מ-thread ההצלבה/בקשת משתמש, לא מנתיב הרדיו."""
    if kind not in ("hex", "reg", "callsign"):
        return {"status": "invalid", "aircraft": []}
    if kind == "hex":
        v = norm_hex(value)
        v = v.lower() if v else None
    else:
        # רישום בצורת התצוגה (‎"G-ZBKM", המקף נשמר — readsb משווה ל-r כמות שהוא);
        # acarsdec מרפד בנקודות מובילות (‎".G-ZBKM") — מסירים אותן בלבד.
        v = str(value or "").strip().lstrip(".").upper() or None
        if v and not re.match(r"^[A-Z0-9-]{2,10}$", v):
            v = None
    if not v:
        return {"status": "invalid", "aircraft": []}
    key = ("lookup", kind, v)
    hit = _cache_get(key)
    if hit is not None:
        return {**hit, "cached": True}
    errors, limited = [], 0
    # ⚠ הספק שה-poll משתמש בו כרגע — *אחרון*: ה-poll כבר פונה אליו כל 15ש' מאותה
    # כתובת IP, ו-lookup נוסף אליו קיבל HTTP 429 בבדיקה על ה-Pi (23.10.2026, adsb.lol
    # — "Rate limits are dynamic"). הספק השני פנוי, וה-poll עדיין נשאר גיבוי.
    with _LOCK:
        poll_src = SOURCES[_S["src_idx"]][0]
    order = [n for n, _t in LOOKUP_SOURCES if n != poll_src] + [poll_src]
    for name in order:
        tmpl = dict(LOOKUP_SOURCES).get(name, {}).get(kind)
        if not tmpl:
            continue
        if not _rate_take(name, LOOKUP_MIN_INTERVAL, LOOKUP_MAX_PER_MIN):
            limited += 1
            _net_note(name, None)
            errors.append(f"{name}: הגבלת קצב מקומית")
            continue
        url = tmpl.format(v=urllib.parse.quote(v, safe=""))
        try:
            data = _http_get_json(url, LOOKUP_TIMEOUT, LOOKUP_MAX_BYTES)
            lst = _ac_list(data)
            if lst is None:
                raise ValueError("תשובה לא צפויה (אין רשימת ac)")
        except Exception as e:                       # timeout/HTTP/JSON — הספק הבא
            _net_note(name, False, e)
            errors.append(f"{name}: {e}")
            continue
        _net_note(name, True)
        rn, fetched = _resp_now(data), round(time.time(), 1)
        recs = []
        for a in lst:
            if isinstance(a, dict):
                rec = _ac_record(a, rn)
                rec.update(provider=name, fetched_at=fetched, resp_now=round(rn, 1))
                recs.append(rec)
        with _LOCK:
            for rec in recs:
                rg, hx = norm_reg(rec["reg"]), norm_hex(rec["hex"])
                if rg and hx:
                    _learn_reg_hex(rg, hx)
        res = {"status": "ok" if recs else "miss", "provider": name, "url": url,
               "fetched_at": fetched, "aircraft": recs, "errors": errors}
        _cache_put(key, res, LOOKUP_TTL_HIT if recs else LOOKUP_TTL_MISS)
        return res
    return {"status": "rate_limited" if errors and limited == len(errors) else "error",
            "aircraft": [], "errors": errors}


def parse_trace(data, provider="adsb.lol"):
    """trace של readsb/tar1090 => (points, meta). פורמט (README-json.md של readsb):
    ‏timestamp (epoch) + trace: [dt, lat, lon, alt|"ground"|null, gs, track, flags,
    vrate, details|null, type|null, alt_geom, ...]. ‏flags&8 = הגובה גאומטרי.
    ⚠ details (nic/nac_p) לא מופיע בכל נקודה — חסר = לא ידוע, לא "תקין"."""
    if not isinstance(data, dict):
        raise ValueError("trace אינו אובייקט")
    base = _num(data.get("timestamp"))
    trace = data.get("trace")
    if base is None or not isinstance(trace, list):
        raise ValueError("trace בלי timestamp/trace")
    pts = []
    for e in trace:
        if not isinstance(e, list) or len(e) < 6:
            continue
        dt = _num(e[0])
        if dt is None:
            continue
        flags = int(e[6]) if len(e) > 6 and isinstance(e[6], int) and not isinstance(e[6], bool) else 0
        details = e[8] if len(e) > 8 and isinstance(e[8], dict) else {}
        stype = e[9] if len(e) > 9 and isinstance(e[9], str) else details.get("type")
        alt = e[3]
        ground = alt == "ground"
        alt_geom = None if ground else (_num(e[10]) if len(e) > 10 else None)
        alt_baro = None if ground else _num(alt)
        if flags & 8:                     # הגובה בשדה 3 הוא גאומטרי, לא baro
            alt_geom, alt_baro = alt_geom if alt_geom is not None else alt_baro, None
        pts.append(make_point(base + dt, e[1], e[2], alt_baro=alt_baro, alt_geom=alt_geom,
                              gs=e[4], track=e[5], src_type=stype, nic=details.get("nic"),
                              nac_p=details.get("nac_p"), provider=provider, origin="trace"))
    meta = {k: data.get(k) for k in ("icao", "r", "t", "desc") if data.get(k)}
    return pts, meta


def import_trace(raw_bytes, name=None):
    """trace שהמשתמש העלה ידנית — קובץ trace_full_<hex>.json מתוך הארכיון היומי של
    adsb.lol ב-GitHub (globe_history_YYYY, ‏ODbL), למשל כש-globe.adsb.lol מחזיר 403
    (נבדק על ה-Pi, 23.10.2026). אותו פורמט readsb בדיוק (parse_trace), דחוס או לא.
    מחזיר {"status": "ok"|"invalid", "points", "meta", "t_first", "t_last", "error"}.
    לעולם לא זורק. ⚠ קלט מהמשתמש — חסום בגודל, ולא נכתב לדיסק (רק הנקודות בראיה)."""
    try:
        data = _decode_json_bytes(raw_bytes, TRACE_MAX_BYTES)
        pts, meta = parse_trace(data, provider="import")
    except Exception as e:                      # JSON פגום/לא-trace/גדול מדי/gzip שבור
        return {"status": "invalid", "points": [], "error": str(e)[:200]}
    for p in pts:
        p["origin"] = "import"
    ts = [p["t"] for p in pts if p["t"] is not None]
    return {"status": "ok" if pts else "invalid", "points": pts, "meta": meta,
            "name": (str(name)[:120] if name else None),
            "t_first": min(ts) if ts else None, "t_last": max(ts) if ts else None,
            "error": None if pts else "אין נקודות בקובץ"}


def fetch_trace(hex_, t):
    """trace היסטורי של מטוס אחד סביב זמן t (epoch) מ-TRACE_SOURCES. ניסיון ראשון:
    trace_full של היממה האחרונה; אם t ביום UTC קודם — גם globe_history של אותו
    יום. מחזיר {"status": "ok"|"miss"|"error"|"rate_limited"|"invalid", "points",
    "meta", "provider", "url", "t_first", "t_last", "covers", "errors"}.
    לעולם לא זורק. ⚠ רשת — רק מבקשת "אמת" (לא מנתיב הרדיו)."""
    hx = norm_hex(hex_)
    t = _num(t)
    if not hx or t is None:
        return {"status": "invalid", "points": []}
    hx = hx.lower()
    day = time.strftime("%Y/%m/%d", time.gmtime(t))
    cands = []
    for name, live_tmpl, hist_tmpl in TRACE_SOURCES:
        if time.time() - t <= TRACE_LIVE_SPAN_SEC:
            cands.append((name, "live", live_tmpl.format(last2=hx[-2:], hex=hx)))
        if day != time.strftime("%Y/%m/%d", time.gmtime(time.time())):
            cands.append((name, "history", hist_tmpl.format(date=day, last2=hx[-2:], hex=hx)))
    errors, limited, best = [], 0, None
    for name, kind, url in cands:
        key = ("trace", url)
        res = _cache_get(key)
        if res is None:
            if not _rate_take("trace:" + name, TRACE_MIN_INTERVAL, TRACE_MAX_PER_MIN):
                limited += 1
                _net_note("trace:" + name, None)
                errors.append(f"{name} ({kind}): הגבלת קצב מקומית")
                continue
            try:
                pts, meta = parse_trace(_http_get_json(url, TRACE_TIMEOUT, TRACE_MAX_BYTES), name)
            except _HTTPStatus as e:
                _net_note("trace:" + name, e.code == 404, e)
                errors.append(f"{name} ({kind}): {e}")
                if e.code == 404:
                    _cache_put(key, {"status": "miss", "points": [], "url": url}, TRACE_TTL)
                continue
            except Exception as e:
                _net_note("trace:" + name, False, e)
                errors.append(f"{name} ({kind}): {e}")
                continue
            _net_note("trace:" + name, True)
            ts = [p["t"] for p in pts if p["t"] is not None]
            res = {"status": "ok" if pts else "miss", "provider": name, "kind": kind, "url": url,
                   "points": pts, "meta": meta, "t_first": min(ts) if ts else None,
                   "t_last": max(ts) if ts else None}
            _cache_put(key, res, TRACE_TTL)
        if res.get("status") != "ok":
            continue
        res = {**res, "covers": res["t_first"] <= t <= res["t_last"]}
        if res["covers"]:
            return {**res, "errors": errors}
        best = best or res
    if best:
        return {**best, "errors": errors}
    status = ("rate_limited" if errors and limited == len(errors)
              else "error" if any("HTTP 404" not in e for e in errors) else "miss")
    return {"status": status, "points": [], "errors": errors}


def _opensky_token():
    """Bearer token מ-OAuth2 client-credentials (‏AIRAM_OPENSKY_CLIENT_ID/SECRET),
    שמור עד דקה לפני תפוגה. None כשאין פרטים או כשההנפקה נכשלה."""
    cid = os.environ.get("AIRAM_OPENSKY_CLIENT_ID")
    sec = os.environ.get("AIRAM_OPENSKY_CLIENT_SECRET")
    if not cid or not sec:
        return None
    with _NET_LOCK:
        tok = _NET["opensky_token"]
        if tok and tok[1] > time.monotonic():
            return tok[0]
    body = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": cid,
                                   "client_secret": sec}).encode()
    req = urllib.request.Request(OPENSKY_TOKEN_URL, data=body,
                                 headers={"User-Agent": "AIR-AM/1.0",
                                          "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=LOOKUP_TIMEOUT) as resp:
            data = json.loads(resp.read(100_000).decode("utf-8"))
        token, ttl = data["access_token"], float(data.get("expires_in") or 1800)
    except Exception as e:
        _net_note("opensky", False, f"token: {e}")
        return None
    with _NET_LOCK:
        _NET["opensky_token"] = (token, time.monotonic() + max(60.0, ttl - 60.0))
    return token


def fetch_opensky_track(hex_, t):
    """מסלול OpenSky (‏/tracks/all) סביב t — נקודות דרך דלילות (ר' OPENSKY_*).
    ‏path: [time, lat, lon, baro_altitude(מטרים), true_track, on_ground]; אין gs
    ואין סוג חיישן => src_type="opensky_track". לעולם לא זורק."""
    hx, t = norm_hex(hex_), _num(t)
    if not hx or t is None:
        return {"status": "invalid", "points": []}
    if time.time() - t > OPENSKY_MAX_AGE_SEC:
        return {"status": "out_of_range", "points": [],
                "errors": ["OpenSky: מסלולים זמינים רק 30 יום אחורה"]}
    url = OPENSKY_TRACK_URL.format(hex=hx.lower(), t=int(t))
    key = ("opensky", url)
    hit = _cache_get(key)
    if hit is not None:
        return hit
    if not _rate_take("opensky", OPENSKY_MIN_INTERVAL, OPENSKY_MAX_PER_MIN):
        _net_note("opensky", None)
        return {"status": "rate_limited", "points": [], "errors": ["OpenSky: הגבלת קצב מקומית"]}
    token = _opensky_token()
    headers = {"Authorization": f"Bearer {token}"} if token else None
    try:
        data = _http_get_json(url, TRACE_TIMEOUT, TRACE_MAX_BYTES, headers)
        path = data.get("path") if isinstance(data, dict) else None
        if not isinstance(path, list):
            raise ValueError("תשובה בלי path")
    except _HTTPStatus as e:
        _net_note("opensky", e.code == 404, e)
        if e.code in (401, 403):
            return {"status": "auth_required", "points": [],
                    "errors": ["OpenSky: נדרשים פרטי OAuth2 (AIRAM_OPENSKY_CLIENT_ID/SECRET)"]}
        res = {"status": "miss" if e.code == 404 else "error", "points": [],
               "errors": [f"OpenSky: {e}"]}
        if e.code == 404:
            _cache_put(key, res, TRACE_TTL)
        return res
    except Exception as e:
        _net_note("opensky", False, e)
        return {"status": "error", "points": [], "errors": [f"OpenSky: {e}"]}
    _net_note("opensky", True)
    pts = []
    for e in path:
        if not isinstance(e, list) or len(e) < 6:
            continue
        alt_m = _num(e[3])
        pts.append(make_point(e[0], e[1], e[2],
                              alt_baro=None if (e[5] is True or alt_m is None) else alt_m / 0.3048,
                              track=e[4], src_type="opensky_track", provider="opensky",
                              origin="trace"))
    ts = [p["t"] for p in pts if p["t"] is not None]
    res = {"status": "ok" if pts else "miss", "provider": "opensky", "url": url, "points": pts,
           "meta": {"callsign": (data.get("callsign") or "").strip() or None},
           "t_first": min(ts) if ts else None, "t_last": max(ts) if ts else None,
           "covers": bool(ts) and min(ts) <= t <= max(ts), "authenticated": bool(token)}
    _cache_put(key, res, TRACE_TTL)
    return res


def net_status():
    """מצב רכיב הרשת של ההצלבה — ל-/api/xcheck/status. לעולם לא זורק."""
    with _NET_LOCK:
        stats = {k: dict(v) for k, v in _NET["stats"].items()}
        cache = len(_NET["cache"])
    with _LOCK:
        n_hex, n_map = len(_S["by_hex"]), len(_S["reg_hex"])
    return {"providers": stats, "cache_entries": cache, "live_by_hex": n_hex,
            "reg_hex_known": n_map,
            "opensky_credentials": bool(os.environ.get("AIRAM_OPENSKY_CLIENT_ID")
                                        and os.environ.get("AIRAM_OPENSKY_CLIENT_SECRET"))}


def _poll_once():
    try:
        name, data = _fetch(_S["src_idx"])
        # גוף-שגיאה ב-HTTP 200 (למשל {"msg": ...}) נרשם בעבר כ-poll תקין עם 0
        # מטוסים — השחזור הציג שמיים ריקים במקום פער-קליטה (§4.2).
        ac_list = _ac_list(data)
        if ac_list is None:
            raise ValueError("תשובה לא צפויה (אין רשימת ac)")
    except Exception as e:
        with _LOCK:
            _S["fails"] += 1
            _S["error"] = f"{SOURCES[_S['src_idx']][0]}: {e}"
            if _S["fails"] >= FAILS_TO_SWITCH:   # מקור תקוע => עוברים לגיבוי
                _S["src_idx"] = (_S["src_idx"] + 1) % len(SOURCES)
                _S["fails"] = 0
            gap_detail = _S["error"]
        _append_track({"t": round(time.time(), 1), "gap": "no_adsb", "detail": gap_detail})
        return
    _load_reg_hex()                       # פעם אחת; אחר כך no-op
    with _LOCK:
        _S["ac_count"] = process(ac_list, resp_now=_resp_now(data))
        _S["last_ok"] = time.monotonic()
        _S["source"] = name
        _S["error"] = None
        _S["fails"] = 0
        # דגימה אחת לסשן: אותו חישוב בדיוק כמו snapshot() (יחס NIC מוחלק על
        # GPS_WINDOW_MIN + מסלול נחיתה נוכחי), אבל בזמן קיר (t_wall) כדי
        # שיהיה בר-השוואה ל-since של /api/session (שנגזר מ-jsonl, גם הוא t_wall).
        g_bad = sum(b for _, b, _t in _S["gps_hist"])
        g_total = sum(t for _, _b, t in _S["gps_hist"])
        ratio = round(g_bad / g_total, 4) if g_total >= GPS_MIN_SAMPLE else None
        rwy, _sec, _n, _age, _pos = _decide_runway("landing", time.monotonic())
        _S["session_series"].append((time.time(), ratio, rwy))
        track_row = _build_track_row()
    _append_track(track_row)
    _save_reg_hex()


def _loop():
    while True:
        try:
            _poll_once()
        except Exception as e:   # רשת הופלה באמצע parse וכד' - לא מפילים thread
            with _LOCK:
                _S["error"] = str(e)
        time.sleep(POLL_SEC)


def start():
    """מפעיל את ה-thread פעם אחת (אידמפוטנטי). נקרא מ-app.py לפני app.run."""
    global _THREAD
    if _THREAD is None or not _THREAD.is_alive():
        _THREAD = threading.Thread(target=_loop, name="adsb-poll", daemon=True)
        _THREAD.start()


# --- בדיקות והרצה ידנית ------------------------------------------------------
def _pos_at(rwy, along_nm, cross_nm):
    """קואורדינטות של נקודה ב-along לפני ה-threshold ו-cross הצידה (לבדיקות)."""
    thr_lat, thr_lon, crs = RUNWAYS[rwy]
    rad = math.radians(crs)
    ux, uy = math.sin(rad), math.cos(rad)
    x = -along_nm * ux + cross_nm * uy
    y = -along_nm * uy - cross_nm * ux
    return thr_lat + y / 60.0, thr_lon + x / (60.0 * _COS_LAT)


def _selftest():
    def ac(rwy, along, cross, track, rate, alt, gs=140, **kw):
        lat, lon = _pos_at(rwy, along, cross)
        return {"hex": kw.pop("hex", "ab1234"), "lat": lat, "lon": lon,
                "track": track, "baro_rate": rate, "alt_baro": alt, "gs": gs,
                "seen_pos": 1, **kw}

    # גישה סופית תקינה לכל מסלול
    for rwy, (_la, _lo, crs) in RUNWAYS.items():
        got = classify(ac(rwy, 6.0, 0.1, crs, -700, 2200))
        assert got == ("landing", rwy, "pos"), f"{rwy}: {got}"
    # track הפוך / סטייה הצידה / גבוה מדי / לא יורד => לא גישה
    assert classify(ac("30", 6.0, 0.1, 121.4, -700, 2200)) is None
    assert classify(ac("30", 6.0, 2.0, 301.4, -700, 2200)) is None
    assert classify(ac("30", 6.0, 0.1, 301.4, -700, 9000)) is None
    assert classify(ac("30", 6.0, 0.1, 301.4, 0, 2200)) is None
    # ‏alt_baro="ground" או שדות חסרים => דילוג שקט
    assert classify({"hex": "x", "lat": 32.0, "lon": 34.88, "alt_baro": "ground"}) is None
    # המראה: 3nm מעבר ל-threshold של 26, מטפס בכיוון 260
    got = classify(ac("26", -3.0, 0.2, 260.0, 1800, 2500, gs=170))
    assert got == ("takeoff", "26", "pos"), got
    # מטוס יורד בגובה שיוט רחוק => כלום
    assert classify(ac("30", 11.0, 0.1, 301.4, -700, 2200, gs=300)) is None

    # --- עמידות לשיבוש GPS: בלי מיקום אמין (nic=0), זיהוי לפי track בלבד -------
    # ה-lat/lon מזויף ללבנון (33.8,35.5) - מתעלמים ממנו, מסווגים לפי הכיוון.
    spoof = lambda trk, rate, alt, gs=160: {
        "hex": "5p00f", "track": trk, "baro_rate": rate, "alt_baro": alt,
        "gs": gs, "nic": 0, "lat": 33.8, "lon": 35.5, "seen_pos": 2}
    assert classify(spoof(209.0, -700, 2500)) == ("landing", "21", "track")
    assert classify(spoof(301.4, -700, 2500)) == ("landing", "30", "track")
    assert classify(spoof(260.0, 1500, 2000, gs=180)) == ("takeoff", "26", "track")
    # track דו-משמעי (בין 029 ל-080, מעל 25° משניהם) => לא מסווג
    assert classify(spoof(55.0, -700, 2500)) is None
    # מיקום אמין אך רחוק (לרנקה) => המטוס באמת במקום אחר => לא אצלנו
    assert classify({"hex": "cy", "lat": 34.9, "lon": 33.6, "track": 184.0,
                     "baro_rate": -800, "alt_baro": 4000, "gs": 250,
                     "nic": 7, "seen_pos": 1}) is None

    # ‏pipeline מלא: 5 נחיתות על 30, המראה על 26, ו-15% מטוסים עם nic פגום
    _S["events"].clear(); _S["gps_hist"].clear()
    _S["last_event"].clear(); _S["last_known"].clear()
    now = time.monotonic()
    fleet = [ac("30", 4 + i, 0.05, 301.4, -700, 1500 + 300 * i, hex=f"a{i:05x}")
             for i in range(5)]
    fleet.append(ac("26", -2.5, 0.1, 260.0, 2000, 2000, gs=180, hex="dep001"))
    fleet += [{"hex": f"c{i:05x}", "lat": 32.5, "lon": 34.5, "alt_baro": 35000,
               "nic": 5 if i < 15 else 8, "seen_pos": 1} for i in range(100)]
    with _LOCK:
        process(fleet, now)
        _S["last_ok"] = now
    snap = snapshot()
    r, g = snap["runway"], snap["gps"]
    assert r["landing"] == "30" and r["takeoff"] == "26", r
    assert r["landing_count"] == 5 and r["confidence"] == "high", r
    assert g["status"] == "severe" and abs(g["bad_ratio"] - 0.15) < 1e-9, g

    # ‏dedup: אותם מטוסים שוב אחרי דקה => לא נספרים פעמיים
    with _LOCK:
        process(fleet, now + 60)
    assert snapshot()["runway"]["landing_count"] == 5

    # החלפת מסלול: 3 גישות טריות על 21 גוברות על היסטוריה עשירה של 30
    with _LOCK:
        for i in range(3):
            process([ac("21", 5.0, 0.05, 209.0, -650, 1800, hex=f"b{i:05x}")],
                    now + 120 + i)
    snap = snapshot()
    assert snap["runway"]["landing"] == "21", snap["runway"]

    # --- snapshot פר-מטוס (היתוך ACARS↔ADS-B) --------------------------------
    assert norm_reg(".4X-EHD") == "4XEHD" == norm_reg("4x-ehd")
    assert norm_reg("") is None and norm_reg(None) is None
    with _LOCK:
        _S["aircraft"].clear()
        now3 = time.monotonic()
        process([
            # מטוס תקין: מיקום אמין + סוג + callsign
            {"hex": "738065", "r": "4X-EHD", "t": "B789", "flight": "ELY315 ",
             "lat": 32.2, "lon": 34.7, "alt_baro": 12000, "gs": 320.0,
             "track": 290.0, "nic": 8, "seen_pos": 3},
            # משובש GPS: נ"צ מדוכא, גובה/track/מהירות נשמרים
            {"hex": "5b1234", "r": "4X-EKS", "t": "B738", "lat": 33.8, "lon": 35.5,
             "alt_baro": 3000, "gs": 150.0, "track": 209.0, "nic": 0, "seen_pos": 2},
            # בלי רישום => לא נכנס ל-snapshot
            {"hex": "abcdef", "lat": 32.1, "lon": 34.9, "alt_baro": 5000, "nic": 8},
        ], now3)
    ac_snap = aircraft_snapshot()
    assert set(ac_snap) == {"4XEHD", "4XEKS"}, ac_snap
    a = ac_snap["4XEHD"]
    assert a["type"] == "B789" and a["flight"] == "ELY315" and a["pos_ok"]
    assert a["lat"] == 32.2 and a["age"] >= 0
    s = ac_snap["4XEKS"]
    assert s["spoofed"] and s["lat"] is None and s["lon"] is None
    assert s["alt"] == 3000 and s["track"] == 209.0     # שורדים שיבוש
    assert set(aircraft_snapshot({"4XEHD"})) == {"4XEHD"}    # סינון לזנבות ACARS
    with _LOCK:                                # גיזום: מטוס שלא נראה AC_KEEP_SEC נעלם
        process([], now3 + AC_KEEP_SEC + 1)
    assert aircraft_snapshot() == {}

    # רגרסיה: מונה המזויפים שורד מטוס עם רישום (בעבר ההשמה בבלוק ה-snapshot
    # דרסה את המונה => spoofed_count יצא bool/אפס אחרי כל מטוס רשום)
    with _LOCK:
        process([
            {"hex": "s1", "alt_baro": 3000, "nic": 0},
            {"hex": "s2", "alt_baro": 4000, "nic": 0},
            {"hex": "c1", "r": "4X-CLN", "alt_baro": 12000, "nic": 8,
             "lat": 32.2, "lon": 34.7, "seen_pos": 1},
        ], now3 + AC_KEEP_SEC + 60)
        _S["last_ok"] = now3 + AC_KEEP_SEC + 60
    assert snapshot()["gps"]["spoofed_count"] == 2, snapshot()["gps"]
    print("selftest: OK")


def _print_report(data, source):
    acs = data.get("ac") or []
    with _LOCK:
        _S["ac_count"] = process(acs)
        _S["last_ok"] = time.monotonic()
        _S["source"] = source
    hits = [(a.get("hex"), a.get("flight", "").strip(), *c)
            for a in acs if (c := classify(a))]
    print(f"{len(acs)} aircraft from {source}; classified:")
    for hex_, flight, kind, rwy, mode in hits:
        print(f"  {hex_}  {flight or '-':<9} {kind:<8} RWY {rwy:<3} [{mode}]")
    print(json.dumps(snapshot(), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    elif "--file" in sys.argv:   # ‏fixture שמור: python3 adsb.py --file dump.json
        path = sys.argv[sys.argv.index("--file") + 1]
        _print_report(json.loads(open(path).read()), path)
    elif "--lookup" in sys.argv:
        # בדיקת שלב 0 להצלבה (docs/adsb-crosscheck-design.md §9), מה-Pi:
        #   python3 adsb.py --lookup hex 406f79 | reg G-ZBKM | callsign BAW60
        i = sys.argv.index("--lookup")
        res = lookup(sys.argv[i + 1], sys.argv[i + 2])
        for a in res.get("aircraft", []):
            print(f"  {a.get('hex')} {a.get('reg') or '-':<8} {a.get('flight') or '-':<8} "
                  f"pos={a.get('lat')},{a.get('lon')} alt={a.get('alt')} type={a.get('src_type')} "
                  f"seen_pos={a.get('seen_pos')} t_pos={a.get('t_pos')}")
        print(json.dumps({k: v for k, v in res.items() if k != "aircraft"},
                         indent=1, ensure_ascii=False))
    elif "--trace" in sys.argv:
        # האם trace היסטורי זמין בפועל, ועד כמה אחורה (לא אומת מחוץ ל-Pi):
        #   python3 adsb.py --trace 406f79 [epoch]      (ברירת מחדל: עכשיו)
        i = sys.argv.index("--trace")
        when = float(sys.argv[i + 2]) if len(sys.argv) > i + 2 else time.time()
        for res in (fetch_trace(sys.argv[i + 1], when), fetch_opensky_track(sys.argv[i + 1], when)):
            iso = lambda v: time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(v)) if v else None
            types = sorted({str(p.get("src_type")) for p in res.get("points", [])})
            print(json.dumps({"status": res.get("status"), "provider": res.get("provider"),
                              "url": res.get("url"), "points": len(res.get("points", [])),
                              "t_first": iso(res.get("t_first")), "t_last": iso(res.get("t_last")),
                              "covers": res.get("covers"), "types": types,
                              "meta": res.get("meta"), "errors": res.get("errors")},
                             indent=1, ensure_ascii=False))
    else:                        # משיכה חיה אחת + דו"ח (דורש אינטרנט פתוח)
        name, data = _fetch(_S["src_idx"])
        _print_report(data, name)
