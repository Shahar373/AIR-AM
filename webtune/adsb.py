#!/usr/bin/env python3
# ============================================================================
#  AIR-AM  -  ניתוח ADS-B: מסלול פעיל לנחיתות/המראות + אינדיקציית שיבוש GPS
# ----------------------------------------------------------------------------
#  thread דמון מושך כל ~60 שניות את המטוסים ברדיוס 50nm סביב נתב"ג ממקור
#  ADS-B קהילתי חופשי (adsb.lol, גיבוי adsb.fi) ומסיק:
#   1. מסלול נחיתות פעיל - מטוסים בגישה סופית (יורדים, מיושרים לקו המסלול
#      המוארך) נצברים כאירועים; המסלול עם הציון הגבוה (דעיכה אקספוננציאלית)
#      הוא הפעיל. אין API שמדווח "מסלול בשימוש" - זו הסקה סטטיסטית.
#   2. מסלול המראות פעיל - אותו עיקרון על מטוסים מטפסים.
#   3. שיבוש GPS - אחוז המטוסים באזור שמשדרים NIC נמוך (<7), אותה שיטה
#      וספים כמו gpsjam.org (ירוק <2%, צהוב 2-10%, אדום >10%).
#
#  עצמאי לחלוטין: stdlib בלבד, וכשל רשת לעולם לא נוגע בנתיב הרדיו -
#  הלולאה בולעת כל חריגה ו-snapshot() רק קורא מצב בזיכרון.
# ============================================================================
import json
import math
import sys
import threading
import time
import urllib.request
from collections import deque

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
POLL_SEC = 60            # adsb.lol מבקשים עד ~1 בקשה/שנייה; פעם בדקה נדיב
RADIUS_NM = 50           # קריאה אחת משרתת גם את זיהוי המסלול וגם את ה-GPS
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


# --- סיווג מטוס בודד --------------------------------------------------------
def classify(ac):
    """("landing"|"takeoff", rwy) אם המטוס בגישה סופית / המראה, אחרת None."""
    lat, lon = _num(ac.get("lat")), _num(ac.get("lon"))
    track = _num(ac.get("track"))
    gs = _num(ac.get("gs"))
    alt = _num(ac.get("alt_baro"))
    if alt is None:
        alt = _num(ac.get("alt_geom"))
    rate = _num(ac.get("baro_rate"))
    if rate is None:
        rate = _num(ac.get("geom_rate"))
    seen_pos = _num(ac.get("seen_pos"))
    if None in (lat, lon, track, gs, alt, rate) or (seen_pos or 0) > 30:
        return None

    # גישה סופית: ירידה, מהירות גישה, גובה לחץ נמוך (טווח רחב - אין QNH),
    # בתוך משפך של 5° סביב הקו המוארך ועם track בכיוון המסלול.
    if rate <= -300 and 90 <= gs <= 220 and 200 <= alt <= 6500:
        best = None
        for rwy in RUNWAYS:
            along, cross = _final_geometry(lat, lon, rwy)
            if not (0.3 <= along <= 12.0):
                continue
            if cross >= max(0.6, along * _TAN_GLIDE):
                continue
            if abs(_norm180(track - RUNWAYS[rwy][2])) >= 20:
                continue
            if best is None or cross < best[1]:   # פיינלים מתכנסים => הקרוב לקו
                best = (rwy, cross)
        if best:
            return ("landing", best[0])

    # המראה: טיפוס מעבר ל-threshold בכיוון ההמראה, קרוב לשדה.
    if rate >= 500 and gs >= 100 and 200 <= alt <= 6000:
        ax, ay = _enu_nm(lat, lon, ARP_LAT, ARP_LON)
        if math.hypot(ax, ay) <= 8.0:
            best = None
            for rwy in RUNWAYS:
                along, cross = _final_geometry(lat, lon, rwy)
                # ‎-along = המרחק מעבר ל-threshold בכיוון ההמראה
                if not (-0.5 <= -along <= 8.0):
                    continue
                if cross >= 1.5 or abs(_norm180(track - RUNWAYS[rwy][2])) >= 20:
                    continue
                if best is None or cross < best[1]:
                    best = (rwy, cross)
            if best:
                return ("takeoff", best[0])
    return None


# --- מצב משותף (נכתב ע"י ה-thread, נקרא ע"י snapshot) ------------------------
_LOCK = threading.Lock()
_THREAD = None
_S = {
    "events": deque(),        # (t_mono, rwy, kind)
    "last_event": {},         # (hex, kind) -> t_mono  (dedup)
    "gps_hist": deque(),      # (t_mono, bad, total)
    "last_known": {},         # kind -> (rwy, t_mono)  - גם אחרי שהחלון התרוקן
    "last_ok": None,          # t_mono של המשיכה המוצלחת האחרונה
    "source": None,
    "error": None,
    "fails": 0,
    "src_idx": 0,
    "ac_count": 0,
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

    bad = total = 0
    for ac in ac_list:
        # אינדיקציית GPS: דגימת NIC מעל GPS_ALT_MIN (nac_p רק כשאין nic)
        alt = _num(ac.get("alt_baro"))
        if alt is None:
            alt = _num(ac.get("alt_geom"))
        if alt is not None and alt > GPS_ALT_MIN and (_num(ac.get("seen_pos")) or 0) < 60:
            integ = _num(ac.get("nic"))
            if integ is None:
                integ = _num(ac.get("nac_p"))
            if integ is not None:
                total += 1
                bad += integ < 7

        hit = classify(ac)
        if hit:
            kind, rwy = hit
            key = (ac.get("hex"), kind)
            prev = last_event.get(key)
            if prev is None or now - prev >= DEDUP_MIN * 60:
                last_event[key] = now
                events.append((now, rwy, kind))
                _S["last_known"][kind] = (rwy, now)

    _S["gps_hist"].append((now, bad, total))

    # גיזום חלונות + מפתחות dedup ישנים
    while events and now - events[0][0] > WINDOW_MIN * 60:
        events.popleft()
    while _S["gps_hist"] and now - _S["gps_hist"][0][0] > GPS_WINDOW_MIN * 60:
        _S["gps_hist"].popleft()
    for key in [k for k, t in last_event.items() if now - t > DEDUP_MIN * 60]:
        del last_event[key]
    return len(ac_list)


def _decide_runway(kind, now):
    """המסלול הפעיל לסוג אירוע: ציון עם דעיכה + override לרצף עדכני."""
    evs = [(t, rwy) for t, rwy, k in _S["events"] if k == kind]
    if not evs:
        return None, None, 0, None
    scores = {}
    for t, rwy in evs:
        scores[rwy] = scores.get(rwy, 0.0) + math.exp(-(now - t) / 60.0 / DECAY_TAU_MIN)
    primary = max(scores, key=scores.get)
    # החלפת מסלול באמצע החלון: 3 מתוך 4 האירועים האחרונים גוברים על הציון
    last4 = [rwy for _, rwy in evs[-4:]]
    if len(last4) == 4:
        for rwy in set(last4):
            if rwy != primary and last4.count(rwy) >= 3:
                primary = rwy
                break
    secondary = None
    if kind == "landing":   # בשעות שיא נוחתים בנתב"ג על שני מסלולים במקביל
        cand = {r: s for r, s in scores.items()
                if r != primary and s >= 0.3 * scores[primary]
                and sum(1 for t, rw in evs if rw == r and now - t <= SECONDARY_MIN * 60) >= 2}
        if cand:
            secondary = max(cand, key=cand.get)
    last_age_min = (now - max(t for t, _ in evs)) / 60.0
    return primary, secondary, len(evs), last_age_min


def snapshot():
    """תמונת המצב ל-/api/airspace. תמיד מצליח - אף פעם לא זורק."""
    with _LOCK:
        now = time.monotonic()
        ok_age = None if _S["last_ok"] is None else now - _S["last_ok"]
        fresh = ok_age is not None and ok_age <= FRESH_SEC

        landing, secondary, n_land, land_age = _decide_runway("landing", now)
        takeoff, _, _, to_age = _decide_runway("takeoff", now)
        confidence = "none"
        if landing:
            confidence = "high" if (n_land >= 3 and land_age < 15) else "low"
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
                "last_known": last_known,
            },
            "gps": {"status": gps_status,
                    "bad_ratio": None if ratio is None else round(ratio, 4),
                    "sample_n": g_total},
        }


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
        return
    with _LOCK:
        _S["ac_count"] = process(data.get("ac") or [])
        _S["last_ok"] = time.monotonic()
        _S["source"] = name
        _S["error"] = None
        _S["fails"] = 0


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
        assert got == ("landing", rwy), f"{rwy}: {got}"
    # track הפוך / סטייה הצידה / גבוה מדי / לא יורד => לא גישה
    assert classify(ac("30", 6.0, 0.1, 121.4, -700, 2200)) is None
    assert classify(ac("30", 6.0, 2.0, 301.4, -700, 2200)) is None
    assert classify(ac("30", 6.0, 0.1, 301.4, -700, 9000)) is None
    assert classify(ac("30", 6.0, 0.1, 301.4, 0, 2200)) is None
    # ‏alt_baro="ground" או שדות חסרים => דילוג שקט
    assert classify({"hex": "x", "lat": 32.0, "lon": 34.88, "alt_baro": "ground"}) is None
    # המראה: 3nm מעבר ל-threshold של 26, מטפס בכיוון 260
    got = classify(ac("26", -3.0, 0.2, 260.0, 1800, 2500, gs=170))
    assert got == ("takeoff", "26"), got
    # מטוס יורד בגובה שיוט רחוק => כלום
    assert classify(ac("30", 11.0, 0.1, 301.4, -700, 2200, gs=300)) is None

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
    for hex_, flight, kind, rwy in hits:
        print(f"  {hex_}  {flight or '-':<9} {kind:<8} RWY {rwy}")
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
