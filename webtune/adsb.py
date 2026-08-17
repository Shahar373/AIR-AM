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
#      "gap" בכשל. משמש בעתיד את POST /api/sessions (שלב 2, טרם מומש).
#
#  עצמאי לחלוטין: stdlib בלבד, וכשל רשת לעולם לא נוגע בנתיב הרדיו -
#  הלולאה בולעת כל חריגה ו-snapshot() רק קורא מצב בזיכרון.
# ============================================================================
import json
import math
import os
import sys
import threading
import time
import urllib.request
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
# ב-docs/session-replay-design.md. עדיין נדיב פי 15 מהמותר בשני המקורות
# (adsb.lol *וגם* adsb.fi מרשים ~1 בקשה/שנייה — אומת מה-README הרשמי של שניהם,
# לא רק adsb.lol; ר' §6 במסמך). גם משפר את המסלול הפעיל/מחוון השיבוש הקיימים,
# שהיום מתעדכנים בהשהיה של דקה.
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
SOURCES = [
    ("adsb.lol", "https://api.adsb.lol/v2/point/{lat}/{lon}/{r}"),
    ("adsb.fi", "https://opendata.adsb.fi/api/v2/lat/{lat}/lon/{lon}/dist/{r}"),
]


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
    "session_series": deque(maxlen=SESSION_SERIES_MAX),   # (t_wall, gps_ratio|None, runway|None) לדוח הסשן
    "track_appends": 0,       # מונה מ-compaction אחרון (ר' _append_track/_compact_track)
}


def _fetch(src_idx):
    name, tmpl = SOURCES[src_idx]
    url = tmpl.format(lat=ARP_LAT, lon=ARP_LON, r=RADIUS_NM)
    req = urllib.request.Request(url, headers={"User-Agent": "AIR-AM/1.0"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return name, json.loads(resp.read().decode("utf-8"))


def process(ac_list, now=None):
    """מעדכן את המצב מרשימת מטוסים אחת (poll או fixture). מחזיר את מספרם."""
    now = time.monotonic() if now is None else now
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

        # snapshot פר-מטוס (היתוך ACARS↔ADS-B): מיקום מפורסם רק כשהוא אמין —
        # nic<SPOOF_NIC = מזויף (שיבוש GPS) => lat/lon מדוכאים, אבל גובה/מהירות/
        # track (ששורדים שיבוש) נשמרים תמיד.
        reg = norm_reg(ac.get("r"))
        if reg:
            # שם נפרד מהמונה spoofed שלמעלה — שימוש חוזר באותו שם דרס את הספירה
            # (bool במקום מונה) וכל spoofed_count ב-/api/airspace יצא שגוי.
            ac_spoofed = nic is not None and nic < SPOOF_NIC
            lat, lon = _num(ac.get("lat")), _num(ac.get("lon"))
            pos_ok = (lat is not None and lon is not None
                      and (_num(ac.get("seen_pos")) or 0) <= 60 and not ac_spoofed)
            on_ground = ac.get("alt_baro") == "ground"
            _S["aircraft"][reg] = {
                "reg": str(ac.get("r")).strip(),
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
                "t_mono": now,
            }

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

    # גיזום חלונות + מפתחות dedup ישנים
    while events and now - events[0][0] > WINDOW_MIN * 60:
        events.popleft()
    while _S["gps_hist"] and now - _S["gps_hist"][0][0] > GPS_WINDOW_MIN * 60:
        _S["gps_hist"].popleft()
    for key in [k for k, t in last_event.items() if now - t > DEDUP_MIN * 60]:
        del last_event[key]
    aircraft = _S["aircraft"]
    for reg in [r for r, rec in aircraft.items() if now - rec["t_mono"] > AC_KEEP_SEC]:
        del aircraft[reg]                 # חסם זיכרון: מטוס שיצא מטווח נגזם
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
    for reg, rec in _S["aircraft"].items():
        lat = round(rec["lat"], 5) if rec["lat"] is not None else None
        lon = round(rec["lon"], 5) if rec["lon"] is not None else None
        alt = round(rec["alt"]) if rec["alt"] is not None else None
        gs = round(rec["gs"], 1) if rec["gs"] is not None else None
        trk = round(rec["track"], 1) if rec["track"] is not None else None
        # ⚠ lat/lon=None נשמר *במפורש* (לא מדלגים על המטוס) — מבדיל "לא נראה"
        # מ"נראה, מיקום משובש" (§7.1 במסמך התכנון). מטוס משובש עדיין נכנס
        # לשורה עם alt/gs/track ששרדו את השיבוש, כמו ב-aircraft_snapshot.
        ac_rows.append([reg, lat, lon, alt, trk, gs, rec["nic"]])
    return {"t": round(time.time(), 1), "ac": ac_rows}


def _append_track(row):
    """מוסיף שורה אחת ל-track.jsonl (append גולמי, לא rewrite של הקובץ כולו).
    כשל כתיבה (דיסק מלא/הרשאה) נבלע — buffer אפמרי, לא שווה להפיל עליו poll."""
    try:
        with open(TRACK_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        return
    _S["track_appends"] += 1
    if _S["track_appends"] >= TRACK_COMPACT_EVERY:
        _compact_track()
        _S["track_appends"] = 0


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
            t = json.loads(ln).get("t")
        except ValueError:
            continue                      # שורה פגומה (כתיבה שנקטעה) — מדלגים, לא כושלים
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
    for ln in lines:
        try:
            row = json.loads(ln)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        t = row.get("t")
        if not isinstance(t, (int, float)):
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


def _poll_once():
    try:
        name, data = _fetch(_S["src_idx"])
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
    with _LOCK:
        _S["ac_count"] = process(data.get("ac") or [])
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
    else:                        # משיכה חיה אחת + דו"ח (דורש אינטרנט פתוח)
        name, data = _fetch(_S["src_idx"])
        _print_report(data, name)
