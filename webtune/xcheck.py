#!/usr/bin/env python3
# ============================================================================
#  AIR-AM  -  הצלבת הודעות דאטה (ACARS/VDL2/SATCOM) מול ADS-B + אופק רדיו
# ----------------------------------------------------------------------------
#  פונקציות טהורות בלבד: stdlib, בלי רשת, בלי קבצים, בלי מצב גלובלי. משיכת
#  הנתונים (snapshot/lookup ממוקד/trace היסטורי) — adsb.py; תזמור, התמדה
#  וה-API — app.py. ר' docs/adsb-crosscheck-design.md.
#
#  שתי שאלות נפרדות, ושתי תשובות נפרדות (§2 בתכנון):
#   1. גאומטריה — האם מיקום המטוס בזמן הקליטה מעבר לאופק הרדיו הסטנדרטי
#      (k=4/3) של התחנה? זה חישוב על מודל, לא טענה על העולם.
#   2. ראיות — עד כמה הנתונים תומכים בכך שהאות שנקלט *שודר מהמטוס* באותו
#      מיקום ובאותו זמן? רשימת קריטריונים בינאריים (כמו _interest_score ב-
#      app.py), לא ציון מומצא. מנגנון ההתפשטות (ducting/troposcatter) לעולם לא
#      נקבע כאן: אין במערכת שום מדידה שמבחינה ביניהם.
#
#  ארבע קטגוריות מיקום, ולעולם לא מערבבים ביניהן (§12 ב-CLAUDE.md):
#    measured     — נקודת ADS-B אמיתית קרובה בזמן (± MEASURED_MAX_DT)
#    interpolated — מחושב בין שתי נקודות אמיתיות, רק בתנאים מחמירים
#    unverified   — הנקודה הקרובה רחוקה מדי בזמן: הקשר בלבד, לא "מיקום בזמן הקליטה"
#    none         — אין נתונים. לא ממציאים מיקום.
# ============================================================================
import math
import re

from adsb import SPOOF_NIC, make_point, norm_hex, norm_reg   # noqa: F401 (re-export)

# --- גאודזיה ----------------------------------------------------------------
WGS84_A = 6378137.0
WGS84_F = 1.0 / 298.257223563
WGS84_B = WGS84_A * (1.0 - WGS84_F)
R_EARTH_KM = 6371.0088          # רדיוס ממוצע — למודל האופק ולנפילת haversine
# אטמוספירה סטנדרטית: גרדיאנט רפרקטיביות ‎−40 N/km ⇒ k = 1/(1 − 6371·40e-6) ≈ 4/3.
# ⚠ זה *מודל*: בפועל k משתנה (במזרח הים התיכון super-refraction נפוצה בקיץ),
# ולכן "מעבר לאופק הסטנדרטי" אינו טענה שהקליטה הייתה "בלתי-אפשרית" — רק חריגה
# מהמודל. ר' caveats ב-assess.
K_STANDARD = 4.0 / 3.0
FT_M = 0.3048
NM_KM = 1.852
KT_KMS = NM_KM / 3600.0         # קשר => ק"מ/שנייה

# --- מיקום-בזמן -------------------------------------------------------------
# ⚠ פרמטרים, לא ספי-איכות מומצאים: כל אחד מתורגם לגבול-שגיאה פיזיקלי (מהירות ×
# זמן) שמוצג למשתמש לצד התוצאה ("± X ק"מ") — הבחירה שקופה ובת-בדיקה.
MEASURED_MAX_DT = 20.0          # s. ב-480kt זה ≤4.9 ק"מ, ונכנס לאי-הוודאות המוצגת
INTERP_MAX_GAP = 120.0          # s. אינטרפולציה רק בין שתי נקודות שהפער ביניהן ≤ 2 דק'
INTERP_MAX_TURN = 30.0          # °. מעבר לזה המסלול בין הנקודות כבר לא "קו ישר"
INTERP_SPEED_TOL_KT = 40.0      # |מהירות משתמעת − gs מדווח| מרבי, או 25% (הגדול)
INTERP_SPEED_TOL_FRAC = 0.25
INTERP_MAX_VRATE_FPM = 6000.0   # שינוי גובה סביר מרבי בין שתי הנקודות
CONTEXT_MAX_DT = 900.0          # s. רחוק מזה בזמן — לא מוצג בכלל; עד כאן — "הקשר, לא מאומת"
GS_FALLBACK_KT = 600.0          # אין gs לנקודה => חסם עליון שמרני (אי-ודאות גדולה, לא קטנה)
# NIC<7 = שלמות GNSS ירודה — אותו סף שכבר משמש את מחוון השיבוש ב-adsb.process()
# (שיטת gpsjam.org). "ירודה" ≠ "משובש": לא מסיקים שיבוש מנקודה בודדת.
INTEGRITY_NIC_MIN = 7
# מקור ADS-B שאינו בלתי-תלוי בהודעה עצמה: readsb מסווג "adsc" כשהמיקום הגיע
# בעצמו מ-ADS-C (מגשרי ACARS) — "אימות" ADS-C מול עצמו הוא מעגלי.
NOT_INDEPENDENT_TYPES = frozenset({"adsc"})

# DO-260B: ‏NACp => EPU (חסם שגיאת מיקום 95%), במטרים. 0 = לא ידוע (לא מוסיף כלום,
# ומסומן כ"לא ידוע" ולא כ"מדויק").
NACP_EPU_M = {11: 3.0, 10: 10.0, 9: 30.0, 8: 92.6, 7: 185.2, 6: 555.6,
              5: 926.0, 4: 1852.0, 3: 3704.0, 2: 7408.0, 1: 18520.0}

# --- השוואת מיקומים / תזמון ההודעה ------------------------------------------
# רצפת התאמה: רזולוציה + latency של שני הפורמטים + דיוק ניווט טיפוסי. מתווסף
# אליה תמיד גבול-השגיאה הפיזיקלי של שני הצדדים — היא לא "הסף" כולו.
POS_AGREE_FLOOR_KM = 2.0
# מיקום בהודעה *בלי* חותמת-זמן משלה (label 15/טקסט וכו'): ACARS יכול לשבת בתור
# ה-CMU. חסם מוצהר, לא ידוע מהפרוטוקול — ולכן מוסיף הרבה אי-ודאות, לא מעט.
MSG_POS_LATENCY_SEC = 120.0
# דוח ADS-C (ts_sec) בתוך ±30ש' מהקליטה = נוצר "בזמן אמת"; מעבר לזה המיקום
# שבהודעה אינו מיקום המשדר ברגע השידור.
REPORT_FRESH_SEC = 30.0
CLOCK_SKEW_NOTE_SEC = 5.0       # דוח "אחרי" הקליטה ביותר מזה => שעון התחנה כנראה מפגר

# --- גובה ---------------------------------------------------------------------
# אי-ודאות גובה לאופק: גאומטרי (GNSS) מדויק; גובה לחץ שונה מהגובה האמיתי
# בתנאים לא-ISA (טמפרטורה/לחץ) — חסם מוצהר. האופק ∝ √h ולכן ההשפעה קטנה.
ALT_UNCERT_FT = {"geom": 200.0, "baro": 1000.0, "adsc": 1000.0}
# גובה לא ידוע: מחשבים עם החסם העליון — "גם בגובה הזה זה מעבר לאופק" היא
# טענה שמרנית שעדיין נכונה; "בתוך האופק" בלי גובה — לא נטען.
ALT_MAX_ASSUMED_FT = 45000.0


# --- עזרים -------------------------------------------------------------------
def _f(v):
    """float סופי או None. ‏bool אינו מספר (True==1 ב-Python)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)
    return v if math.isfinite(v) else None


def valid_latlon(lat, lon):
    lat, lon = _f(lat), _f(lon)
    if lat is None or lon is None:
        return False
    return -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0 and not (lat == 0 and lon == 0)


def _norm180(deg):
    return (deg + 180.0) % 360.0 - 180.0


def haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2.0 * R_EARTH_KM * math.asin(min(1.0, math.sqrt(a)))


def geodesic_km(lat1, lon1, lat2, lon2):
    """מרחק גאודטי על אליפסואיד WGS84 (Vincenty inverse). בנקודות כמעט-אנטיפודליות
    Vincenty לא מתכנס — נופלים ל-haversine (שגיאה ≤0.5%, לא רלוונטי לטווחי VHF)."""
    if lat1 == lat2 and lon1 == lon2:
        return 0.0
    a, b, f = WGS84_A, WGS84_B, WGS84_F
    L = math.radians(lon2 - lon1)
    u1 = math.atan((1 - f) * math.tan(math.radians(lat1)))
    u2 = math.atan((1 - f) * math.tan(math.radians(lat2)))
    su1, cu1, su2, cu2 = math.sin(u1), math.cos(u1), math.sin(u2), math.cos(u2)
    lam = L
    for _ in range(200):
        sl, cl = math.sin(lam), math.cos(lam)
        ss = math.sqrt((cu2 * sl) ** 2 + (cu1 * su2 - su1 * cu2 * cl) ** 2)
        if ss == 0:
            return 0.0
        cs = su1 * su2 + cu1 * cu2 * cl
        sig = math.atan2(ss, cs)
        sa = cu1 * cu2 * sl / ss
        c2a = 1 - sa * sa
        c2sm = cs - 2 * su1 * su2 / c2a if c2a else 0.0     # קו המשווה: c2a=0
        C = f / 16 * c2a * (4 + f * (4 - 3 * c2a))
        prev = lam
        lam = L + (1 - C) * f * sa * (sig + C * ss * (c2sm + C * cs * (-1 + 2 * c2sm * c2sm)))
        if abs(lam - prev) < 1e-12:
            break
    else:
        return haversine_km(lat1, lon1, lat2, lon2)
    usq = c2a * (a * a - b * b) / (b * b)
    A = 1 + usq / 16384 * (4096 + usq * (-768 + usq * (320 - 175 * usq)))
    B = usq / 1024 * (256 + usq * (-128 + usq * (74 - 47 * usq)))
    dsig = B * ss * (c2sm + B / 4 * (cs * (-1 + 2 * c2sm * c2sm)
                                     - B / 6 * c2sm * (-3 + 4 * ss * ss) * (-3 + 4 * c2sm * c2sm)))
    return b * A * (sig - dsig) / 1000.0


def interpolate_gc(lat0, lon0, lat1, lon1, frac):
    """נקודה על המעגל הגדול בין שתי נקודות (slerp על כדור). על ≤2 דק' טיסה
    (≤30 ק"מ) ההבדל מהאליפסואיד זניח מול אי-הוודאות שמוצגת ממילא."""
    p1, l1, p2, l2 = map(math.radians, (lat0, lon0, lat1, lon1))
    d = 2 * math.asin(min(1.0, math.sqrt(math.sin((p2 - p1) / 2) ** 2
                                         + math.cos(p1) * math.cos(p2) * math.sin((l2 - l1) / 2) ** 2)))
    if d < 1e-12:
        return lat0, lon0
    a = math.sin((1 - frac) * d) / math.sin(d)
    b = math.sin(frac * d) / math.sin(d)
    x = a * math.cos(p1) * math.cos(l1) + b * math.cos(p2) * math.cos(l2)
    y = a * math.cos(p1) * math.sin(l1) + b * math.cos(p2) * math.sin(l2)
    z = a * math.sin(p1) + b * math.sin(p2)
    return math.degrees(math.atan2(z, math.hypot(x, y))), math.degrees(math.atan2(y, x))


# --- אופק --------------------------------------------------------------------
def horizon_km(h_m, k=1.0):
    """מרחק האופק (ק"מ) מגובה h_m מטרים מעל פני הים, על כדור ברדיוס k·R:
    d = √(2·k·R·h). ‏k=1 גאומטרי, ‏k=4/3 "אופק רדיו" סטנדרטי (≈4.12·√h_m ק"מ)."""
    h = _f(h_m)
    if h is None or h <= 0:
        return 0.0
    return math.sqrt(2.0 * k * R_EARTH_KM * h / 1000.0)


def horizons(station_alt_m, aircraft_alt_m, k=K_STANDARD):
    """אופק משולב תחנה+מטוס (כל צד תורם את האופק שלו): גאומטרי ורדיו."""
    return {
        "geometric_km": round(horizon_km(station_alt_m) + horizon_km(aircraft_alt_m), 1),
        "radio_km": round(horizon_km(station_alt_m, k) + horizon_km(aircraft_alt_m, k), 1),
        "k": round(k, 4),
    }


def classify_horizon(dist_km, dist_uncert_km, station_alt_m, alt_ft, alt_kind):
    """מסווג מרחק מול האופק, *עם* אי-הוודאות: "מעבר" רק אם גם הקצה הקרוב של
    טווח המרחק חורג מאופק הרדיו המחושב בקצה הגבוה של טווח הגובה. אחרת "גבולי".
    alt_kind: "geom"|"baro"|"adsc"|None (None => ALT_MAX_ASSUMED_FT, ר' שם)."""
    assumed = alt_ft is None
    if assumed:
        alt_ft, alt_kind = ALT_MAX_ASSUMED_FT, None
    alt_unc = 0.0 if assumed else ALT_UNCERT_FT.get(alt_kind, ALT_UNCERT_FT["baro"])
    hz = horizons(station_alt_m, alt_ft * FT_M)
    radio_hi = horizon_km(station_alt_m, K_STANDARD) + horizon_km((alt_ft + alt_unc) * FT_M, K_STANDARD)
    lo, hi = dist_km - dist_uncert_km, dist_km + dist_uncert_km
    if hi <= hz["geometric_km"]:
        status = "line_of_sight"
    elif hi <= hz["radio_km"]:
        status = "within_radio_horizon"
    elif lo > radio_hi:
        status = "beyond_radio_horizon"
    else:
        status = "marginal"
    if assumed and status != "beyond_radio_horizon":
        # בלי גובה אי-אפשר לטעון "בתוך האופק" (החישוב נעשה בגובה המרבי האפשרי)
        status = "unknown_altitude"
    return {**hz, "status": status, "distance_km": round(dist_km, 1),
            "distance_uncert_km": round(dist_uncert_km, 1),
            "excess_km": round(dist_km - hz["radio_km"], 1),
            "ratio": round(dist_km / hz["radio_km"], 3) if hz["radio_km"] else None,
            "aircraft_alt_ft": round(alt_ft), "aircraft_alt_kind": alt_kind,
            "aircraft_alt_assumed": assumed,
            "radio_km_upper": round(radio_hi, 1)}


# --- זהות --------------------------------------------------------------------
_FLT_ICAO_RE = re.compile(r"^([A-Z]{3})(\d{1,4})([A-Z]{0,2})$")
_FLT_IATA_RE = re.compile(r"^([A-Z0-9]{2})(\d{1,4})([A-Z]?)$")
# IATA => ICAO לחברות נפוצות במרחב. ACARS נושא מספר טיסה בפורמט IATA ("BA0060"),
# ADS-B אות-קריאה ICAO ("BAW60"). טבלה קטנה בכוונה: חברה שלא כאן פשוט לא מותאמת
# לפי טיסה ("אין התאמה" — לא ניחוש). ⚠ גם בהתאמה, חברות רבות משדרות אות-קריאה
# אלפא-נומרי שאינו מספר הטיסה (למשל BAW8MN) — ולכן טיסה היא אמצעי עזר בלבד.
IATA_TO_ICAO = {
    "LY": "ELY", "IZ": "AIZ", "6H": "ISR", "BA": "BAW", "LH": "DLH", "AF": "AFR",
    "KL": "KLM", "TK": "THY", "EK": "UAE", "QR": "QTR", "EY": "ETD", "UA": "UAL",
    "DL": "DAL", "AA": "AAL", "LX": "SWR", "OS": "AUA", "SN": "BEL", "A3": "AEE",
    "U2": "EZY", "FR": "RYR", "W6": "WZZ", "MS": "MSR", "RJ": "RJA", "ET": "ETH",
    "PC": "PGT", "CY": "CYP", "FZ": "FDB", "SQ": "SIA", "CX": "CPA", "AI": "AIC",
    "KE": "KAL", "IB": "IBE", "VS": "VIR", "AC": "ACA", "KQ": "KQA", "GF": "GFA",
    "SV": "SVA", "LO": "LOT", "OK": "CSA", "TP": "TAP", "AZ": "ITY", "SK": "SAS",
    "AY": "FIN", "EI": "EIN",
}


def norm_callsign(v):
    if not v:
        return None
    return "".join(ch for ch in str(v).upper() if ch.isalnum()) or None


def flight_key(v):
    """מפתח השוואה לטיסה: ‏"BA0060"(ACARS/IATA) ו-"BAW60"(ADS-B/ICAO) => "BAW60".
    None כשהפורמט לא מוכר או שהחברה לא בטבלה — לא מנחשים."""
    s = norm_callsign(v)
    if not s:
        return None
    m = _FLT_ICAO_RE.match(s)
    if m:
        prefix = m.group(1)
    else:
        m = _FLT_IATA_RE.match(s)
        if not m or m.group(1) not in IATA_TO_ICAO:
            return None
        prefix = IATA_TO_ICAO[m.group(1)]
    return f"{prefix}{int(m.group(2))}{m.group(3)}"


def message_identity(msg, hex_override=None):
    """מזהי המטוס מתוך כרטיס מנורמל. ‏hex רק ממקור *מבני* (כתובת AVLC של VDL2,
    ‏AES של SATCOM) או מהמשתמש — לעולם לא מנוחש. tail של תחנת-קרקע מודר
    (כמו _aircraft_identity ב-app.py)."""
    hx, src = norm_hex(hex_override), "user"
    if not hx:
        hx, src = norm_hex(msg.get("icao")), "message"
    if not hx:
        hx, src = norm_hex(msg.get("aes")), "message"
    reg = None if msg.get("tail_is_station") else norm_reg(msg.get("tail"))
    flight = norm_callsign(msg.get("flight"))
    return {"hex": hx, "hex_src": src if hx else None, "reg": reg,
            "flight": flight, "flight_key": flight_key(flight)}


def match_record(ident, rec):
    """(match_by, conflict) בין זהות ההודעה לרשומת ADS-B. סדר עדיפויות: hex ⇐
    רישום ⇐ טיסה (עזר בלבד). מזהה חזק *סותר* (hex/רישום שונים) = סתירה — גם אם
    מזהה חלש יותר תואם: סתירה לעולם לא "מתבטלת" בהתאמה חלשה."""
    r_hex, r_reg = norm_hex(rec.get("hex")), norm_reg(rec.get("reg"))
    if ident.get("hex") and r_hex and ident["hex"] != r_hex:
        return None, "hex_mismatch"
    if ident.get("reg") and r_reg and ident["reg"] != r_reg:
        return None, "reg_mismatch"
    if ident.get("hex") and r_hex:
        return "hex", None
    if ident.get("reg") and r_reg:
        return "reg", None
    fk = flight_key(rec.get("flight"))
    if ident.get("flight_key") and fk and ident["flight_key"] == fk:
        return "flight", None
    return None, "no_common_id"


# --- נקודות ADS-B ומיקום-בזמן -------------------------------------------------
def _usable(p):
    """(bool, סיבת-פסילה). ‏nic<SPOOF_NIC = מיקום מזויף (אותו כלל כמו adsb.py)."""
    if p.get("nic") is not None and p["nic"] < SPOOF_NIC:
        return False, "spoofed"             # גם כשהמיקום כבר דוכא (lat=None) — זו הסיבה
    if not valid_latlon(p.get("lat"), p.get("lon")):
        return False, "no_pos"
    if p.get("src_type") in NOT_INDEPENDENT_TYPES:
        return False, "adsc"
    return True, None


def _speed_kms(p):
    return (p.get("gs") if p.get("gs") is not None else GS_FALLBACK_KT) * KT_KMS


def _epu_km(p):
    nac = p.get("nac_p")
    return NACP_EPU_M.get(int(nac), 0.0) / 1000.0 if nac is not None else 0.0


def _alt(p):
    if p.get("alt_geom") is not None:
        return p["alt_geom"], "geom"
    if p.get("alt_baro") is not None:
        return p["alt_baro"], "baro"
    return None, None


def _consistent(p0, p1):
    """האם מותר לחבר שתי נקודות בקו ישר: מהירות משתמעת ≈ gs מדווח, בלי פנייה
    חדה ובלי קפיצת גובה. מחזיר (ok, reason)."""
    gap = p1["t"] - p0["t"]
    if gap <= 0:
        return False, "same_time"
    implied_kt = geodesic_km(p0["lat"], p0["lon"], p1["lat"], p1["lon"]) / gap / KT_KMS
    gss = [g for g in (p0.get("gs"), p1.get("gs")) if g is not None]
    if gss:
        ref = sum(gss) / len(gss)
        if abs(implied_kt - ref) > max(INTERP_SPEED_TOL_KT, INTERP_SPEED_TOL_FRAC * ref):
            return False, "speed_inconsistent"
    elif implied_kt > GS_FALLBACK_KT:
        return False, "speed_inconsistent"
    if p0.get("track") is not None and p1.get("track") is not None:
        if abs(_norm180(p1["track"] - p0["track"])) > INTERP_MAX_TURN:
            return False, "turning"
    a0, _k0 = _alt(p0)
    a1, _k1 = _alt(p1)
    if a0 is not None and a1 is not None and abs(a1 - a0) / (gap / 60.0) > INTERP_MAX_VRATE_FPM:
        return False, "altitude_inconsistent"
    return True, None


def _pos_result(status, t, lat, lon, alt, alt_kind, gs, track, dt, unc, used, reason=None):
    return {"status": status, "t": t, "lat": round(lat, 5), "lon": round(lon, 5),
            "alt_ft": None if alt is None else round(alt), "alt_kind": alt_kind,
            "gs": gs, "track": track, "dt_s": dt, "uncert_km": round(unc, 2),
            "points": used, "reason": reason,
            "src_type": used[0].get("src_type") if used else None,
            "provider": used[0].get("provider") if used else None,
            "nic": min((p["nic"] for p in used if p.get("nic") is not None), default=None),
            "nac_p": min((p["nac_p"] for p in used if p.get("nac_p") is not None), default=None)}


def position_at(points, t):
    """מיקום המטוס בזמן t מתוך נקודות ADS-B, באחת מארבע הקטגוריות (ר' ראש
    הקובץ). לעולם לא מייצר מיקום כשאין מספיק מידע: פער גדול / חוסר עקביות =>
    לכל היותר "unverified" (הנקודה האמיתית הקרובה, *לא* מוזזת), ומעבר ל-
    CONTEXT_MAX_DT — "none"."""
    t = _f(t)
    pts = sorted((p for p in points or [] if p.get("t") is not None), key=lambda p: p["t"])
    usable, excluded = [], {"no_pos": 0, "spoofed": 0, "adsc": 0}
    for p in pts:
        ok, why = _usable(p)
        if ok:
            usable.append(p)
        else:
            excluded[why] += 1
    if t is None or not usable:
        reason = ("spoofed" if excluded["spoofed"] else "not_independent" if excluded["adsc"]
                  else "no_position" if pts else "no_data")
        return {"status": "none", "reason": reason, "excluded": excluded}
    nearest = min(usable, key=lambda p: abs(p["t"] - t))
    dt = round(nearest["t"] - t, 1)
    alt, alt_kind = _alt(nearest)
    if abs(dt) <= MEASURED_MAX_DT:
        unc = _speed_kms(nearest) * (abs(dt) + nearest["t_uncert"]) + _epu_km(nearest)
        r = _pos_result("measured", t, nearest["lat"], nearest["lon"], alt, alt_kind,
                        nearest.get("gs"), nearest.get("track"), dt, unc, [nearest])
        r["excluded"] = excluded
        return r
    before = next((p for p in reversed(usable) if p["t"] <= t), None)
    after = next((p for p in usable if p["t"] >= t), None)
    reason = "one_sided"
    if before is not None and after is not None:
        gap = after["t"] - before["t"]
        ok, why = _consistent(before, after)
        if gap <= INTERP_MAX_GAP and ok:
            frac = (t - before["t"]) / gap
            lat, lon = interpolate_gc(before["lat"], before["lon"], after["lat"], after["lon"], frac)
            a0, k0 = _alt(before)
            a1, k1 = _alt(after)
            ialt = a0 + (a1 - a0) * frac if (a0 is not None and a1 is not None and k0 == k1) else (a0 or a1)
            ikind = k0 if k0 == k1 else (k0 or k1)
            chord = geodesic_km(before["lat"], before["lon"], after["lat"], after["lon"])
            turn = (abs(_norm180(after["track"] - before["track"]))
                    if before.get("track") is not None and after.get("track") is not None
                    else INTERP_MAX_TURN)       # כיוון לא ידוע => החסם המרבי שהותר
            # sagitta של קשת עם זווית פנייה θ ומיתר L: ‏(L/2)·tan(θ/4); תאוצה
            # קבועה בין הנקודות: סטייה מרבית מאינטרפולציה לינארית בזמן = Δv·T/8.
            sag = chord / 2 * math.tan(math.radians(turn) / 4)
            dv = (abs(after["gs"] - before["gs"]) * KT_KMS
                  if before.get("gs") is not None and after.get("gs") is not None else 0.0)
            unc = (sag + dv * gap / 8 + max(_epu_km(before), _epu_km(after))
                   + max(_speed_kms(before) * before["t_uncert"], _speed_kms(after) * after["t_uncert"]))
            gs_i = (before["gs"] + (after["gs"] - before["gs"]) * frac
                    if before.get("gs") is not None and after.get("gs") is not None
                    else before.get("gs") or after.get("gs"))
            r = _pos_result("interpolated", t, lat, lon, ialt, ikind,
                            None if gs_i is None else round(gs_i, 1),
                            before.get("track") if after.get("track") is None else after["track"],
                            [round(before["t"] - t, 1), round(after["t"] - t, 1)], unc,
                            [before, after])
            r["gap_s"] = round(gap, 1)
            r["excluded"] = excluded
            return r
        reason = "gap_too_large" if gap > INTERP_MAX_GAP else why
    if abs(dt) <= CONTEXT_MAX_DT:
        unc = _speed_kms(nearest) * (abs(dt) + nearest["t_uncert"]) + _epu_km(nearest)
        r = _pos_result("unverified", t, nearest["lat"], nearest["lon"], alt, alt_kind,
                        nearest.get("gs"), nearest.get("track"), dt, unc, [nearest], reason)
        r["excluded"] = excluded
        return r
    return {"status": "none", "reason": "too_far_in_time", "nearest_dt_s": dt,
            "excluded": excluded}


def compare_positions(msg_lat, msg_lon, at, msg_acc_km=None, msg_t_uncert_s=0.0):
    """מרחק בין מיקום ההודעה למיקום ADS-B *באותו זמן* (at = position_at בזמן
    המיקום שבהודעה). "agree" מוכרע רק מול נקודה measured/interpolated, וה-
    tolerance הוא סכום גבולות-השגיאה הפיזיקליים + POS_AGREE_FLOOR_KM — מוצג."""
    if not at or at.get("status") in (None, "none") or not valid_latlon(msg_lat, msg_lon):
        return None
    d = geodesic_km(msg_lat, msg_lon, at["lat"], at["lon"])
    speed = (at.get("gs") if at.get("gs") is not None else GS_FALLBACK_KT) * KT_KMS
    tol = POS_AGREE_FLOOR_KM + at["uncert_km"] + (msg_acc_km or 0.0) + speed * msg_t_uncert_s
    decisive = at["status"] in ("measured", "interpolated")
    return {"distance_km": round(d, 2), "tolerance_km": round(tol, 2),
            "agree": (d <= tol) if decisive else None, "adsb_status": at["status"],
            "dt_s": at.get("dt_s")}


def adsc_report_time(ts_sec, t_rx):
    """זמן מוחלט (epoch) של דוח ADS-C מ-ts_sec ("שניות מתחילת השעה", ‏libacars)
    — השעה שבה ts הכי קרוב לזמן הקליטה. None כשאין נתון תקין."""
    ts, t = _f(ts_sec), _f(t_rx)
    if ts is None or t is None or not 0 <= ts < 3600:
        return None
    base = math.floor(t / 3600.0) * 3600.0
    return min((base - 3600 + ts, base + ts, base + 3600 + ts), key=lambda c: abs(c - t))


# --- הערכה -------------------------------------------------------------------
VERDICT_TEXT = {
    "blos_supported": "קליטה מעבר לאופק הרדיו — נתמכת בראיות חזקות",
    "blos_possible": "קליטה מעבר לאופק הרדיו — ראיות חלקיות",
    "exceeds_unverified": "חריגה מאופק הרדיו לפי מיקום לא מאומת",
    "exceeds_conflict": "חריגה מאופק הרדיו לכאורה — אבל הנתונים סותרים",
    "marginal": "גבולי — בתוך אי-הוודאות של אופק הרדיו",
    "within_radio_horizon": "בתוך אופק הרדיו (מעבר לאופק הגאומטרי — שבירה רגילה)",
    "line_of_sight": "בקו ראייה גאומטרי",
    "unknown_altitude": "גובה המטוס לא ידוע — לא ניתן לקבוע מול האופק",
    "insufficient": "אין מיקום מטוס לזמן הקליטה — לא ניתן לחשב",
    "no_station": "מיקום תחנת הקליטה לא הוגדר",
    "na_satellite": "לא רלוונטי — האות עבר דרך לוויין",
    "na_uplink": "לא רלוונטי — ההודעה שודרה מתחנת קרקע אל המטוס",
}
LEVEL_TEXT = {"strong": "חזקה", "partial": "חלקית", "weak": "חלשה",
              "conflict": "סתירה", "none": "אין", "n/a": "לא רלוונטי"}
_STATUS_TEXT = {"measured": "נמדד", "interpolated": "אינטרפולציה",
                "unverified": "לא מאומת", "none": "אין נתונים"}
_NONE_REASON = {"no_data": "אין נתוני ADS-B למטוס הזה", "no_position": "אין מיקום בנתוני ה-ADS-B",
                "spoofed": "כל נקודות ה-ADS-B סומנו כמזויפות (NIC נמוך)",
                "not_independent": "ה-ADS-B היחיד מקורו ב-ADS-C — לא אימות עצמאי",
                "too_far_in_time": "הנקודה הקרובה רחוקה יותר מ-15 דק' מזמן הקליטה"}
_UNVERIFIED_REASON = {"gap_too_large": "הפער בין שתי הנקודות שסביב הקליטה גדול מ-2 דק'",
                      "one_sided": "יש נקודות רק מצד אחד של זמן הקליטה",
                      "speed_inconsistent": "המהירות בין הנקודות לא עקבית",
                      "turning": "המטוס פנה בין הנקודות", "same_time": "נקודות כפולות",
                      "altitude_inconsistent": "שינוי גובה לא סביר בין הנקודות"}


def _fmt_s(v):
    v = abs(v)
    return f"{v:.1f}" if v < 10 else f"{v:.0f}"


def _fmt_km(v):
    return "<0.1" if v < 0.1 else f"{v:.1f}"


def assess(source, msg, direction, station, match, rx_at, pos_at=None, adsc=None,
           direction_basis=None):
    """הערכה מלאה להודעה אחת. קלט:
      source    — "acars"|"vdl2"|"satcom"
      msg       — כרטיס מנורמל (t, lat, lon, pos_src, error, ...)
      direction — "downlink"|"uplink"|None (כפי שהקורא חישב; direction_basis מתאר איך)
      station   — {"lat","lon","alt_m"} או None
      match     — {"by": "hex"|"reg"|"flight"|None, "conflict": str|None, ...}
      rx_at     — position_at בזמן הקליטה (או None)
      pos_at    — position_at בזמן *המיקום שבהודעה* (ADS-C ts) או None
      adsc      — {"t": epoch הדוח|None, "alt_ft", "accuracy_nm"} או None
    מחזיר dict עם horizon, position_check, evidence (קריטריונים), level, verdict."""
    t_rx = _f(msg.get("t"))
    rf_path = "satellite" if source == "satcom" else "vhf"
    has_msg_pos = valid_latlon(msg.get("lat"), msg.get("lon"))
    adsc = adsc or {}
    report_t = _f(adsc.get("t"))
    report_age = None if (report_t is None or t_rx is None) else round(t_rx - report_t, 1)
    acc_nm = _f(adsc.get("accuracy_nm"))
    msg_acc_km = acc_nm * NM_KM if (acc_nm is not None and acc_nm > 0) else None

    # --- הצלבת מיקום: ההודעה מול ADS-B בזמן *המיקום שבהודעה* --------------
    position_check = None
    if has_msg_pos:
        at = pos_at if pos_at is not None else rx_at
        position_check = compare_positions(
            msg["lat"], msg["lon"], at, msg_acc_km,
            0.0 if report_t is not None else MSG_POS_LATENCY_SEC)
        if position_check is not None:
            position_check["msg_pos_t"] = report_t if report_t is not None else t_rx
            position_check["msg_pos_t_known"] = report_t is not None

    # --- מיקום המטוס ברגע הקליטה (= ברגע השידור: האות נע במהירות האור) -----
    rx_ok = bool(rx_at) and rx_at.get("status") in ("measured", "interpolated")
    ac = None
    if rx_ok:
        ac = {"lat": rx_at["lat"], "lon": rx_at["lon"], "alt_ft": rx_at.get("alt_ft"),
              "alt_kind": rx_at.get("alt_kind"), "uncert_km": rx_at["uncert_km"],
              "source": "adsb_" + rx_at["status"]}
    elif has_msg_pos:
        gs = rx_at.get("gs") if rx_at else None
        speed = (gs if gs is not None else GS_FALLBACK_KT) * KT_KMS
        if report_age is not None and abs(report_age) <= REPORT_FRESH_SEC:
            unc, src = speed * abs(report_age) + (msg_acc_km or 0.0), "message_timed"
        else:
            unc, src = speed * MSG_POS_LATENCY_SEC + (msg_acc_km or 0.0), "message_untimed"
        ac = {"lat": msg["lat"], "lon": msg["lon"], "alt_ft": None, "alt_kind": None,
              "uncert_km": round(unc, 2), "source": src}
    elif rx_at and rx_at.get("status") == "unverified":
        ac = {"lat": rx_at["lat"], "lon": rx_at["lon"], "alt_ft": rx_at.get("alt_ft"),
              "alt_kind": rx_at.get("alt_kind"), "uncert_km": rx_at["uncert_km"],
              "source": "adsb_unverified"}
    if ac is not None and ac["alt_ft"] is None:
        # גובה ממקור משלים: ADS-B (גם לא-מאומת בזמן — גובה משתנה לאט בשיוט) או ADS-C
        if rx_at and rx_at.get("alt_ft") is not None:
            ac["alt_ft"], ac["alt_kind"] = rx_at["alt_ft"], rx_at.get("alt_kind")
        elif _f(adsc.get("alt_ft")) is not None:
            ac["alt_ft"], ac["alt_kind"] = round(adsc["alt_ft"]), "adsc"

    # --- גאומטריה ----------------------------------------------------------
    horizon = None
    if station and ac is not None:
        dist = geodesic_km(station["lat"], station["lon"], ac["lat"], ac["lon"])
        horizon = classify_horizon(dist, ac["uncert_km"], station.get("alt_m") or 0.0,
                                   ac["alt_ft"], ac["alt_kind"])

    # --- קריטריוני ראיות ---------------------------------------------------
    crit = []

    def add(key, ok, text):
        crit.append({"key": key, "ok": ok, "text": text})

    basis = f" ({direction_basis})" if direction_basis else ""
    if direction == "downlink":
        add("downlink", True, "ההודעה שודרה מהמטוס (downlink)" + basis)
    elif direction == "uplink":
        add("downlink", False, "הודעת uplink — המשדר הוא תחנת קרקע, לא המטוס" + basis)
    else:
        add("downlink", None, "כיוון ההודעה לא ידוע")

    by, conflict = match.get("by"), match.get("conflict")
    if conflict in ("hex_mismatch", "reg_mismatch"):
        add("identity", False, "סתירת זהות: " + ("כתובת ICAO" if conflict == "hex_mismatch" else "רישום")
            + " שונה בין ההודעה לנתוני ה-ADS-B")
    elif by in ("hex", "reg"):
        how = "כתובת ICAO" if by == "hex" else "רישום"
        if by == "hex" and match.get("hex_src") == "user":
            how += " (שהוזנה ידנית)"
        add("identity", True, f"המטוס זוהה ב-ADS-B לפי {how}")
    elif by == "flight":
        add("identity", None, "התאמה לפי מספר טיסה בלבד — אמצעי עזר, לא זיהוי חזק")
    else:
        add("identity", False, "לא נמצא מטוס תואם בנתוני ה-ADS-B")

    st = rx_at.get("status") if rx_at else "none"
    if st == "measured":
        add("adsb_at_rx", True, f"מיקום ADS-B נמדד {_fmt_s(rx_at['dt_s'])}ש' "
            f"{'אחרי' if rx_at['dt_s'] > 0 else 'לפני'} הקליטה (±{_fmt_km(rx_at['uncert_km'])} ק\"מ)")
    elif st == "interpolated":
        add("adsb_at_rx", True, "מיקום ADS-B באינטרפולציה בין נקודות "
            f"{_fmt_s(rx_at['dt_s'][0])}ש' לפני ו-{_fmt_s(rx_at['dt_s'][1])}ש' אחרי הקליטה "
            f"(±{_fmt_km(rx_at['uncert_km'])} ק\"מ)")
    elif st == "unverified":
        add("adsb_at_rx", None, f"נקודת ה-ADS-B הקרובה רחוקה {_fmt_s(rx_at['dt_s'])}ש' מהקליטה — "
            + _UNVERIFIED_REASON.get(rx_at.get("reason"), "לא מאמת מיקום בזמן הקליטה"))
    else:
        add("adsb_at_rx", False, _NONE_REASON.get((rx_at or {}).get("reason"), "אין נתוני ADS-B"))

    if position_check is not None:
        pc = position_check
        if pc["agree"] is True:
            add("position_agrees", True, f"מיקום ההודעה תואם ל-ADS-B: {_fmt_km(pc['distance_km'])} ק\"מ "
                f"(בתוך גבול השגיאה {pc['tolerance_km']:.1f} ק\"מ)")
        elif pc["agree"] is False:
            add("position_agrees", False, f"מיקום ההודעה רחוק {pc['distance_km']:.1f} ק\"מ "
                f"מ-ADS-B — מעבר לגבול השגיאה ({pc['tolerance_km']:.1f} ק\"מ)")
        else:
            add("position_agrees", None, f"מרחק {pc['distance_km']:.1f} ק\"מ מנקודת ADS-B לא-מאומתת בזמן")
    elif has_msg_pos:
        add("position_agrees", None, "אין נתוני ADS-B להצלבת המיקום שבהודעה")
    else:
        add("position_agrees", None, "אין בהודעה מיקום להצלבה")

    if report_age is not None:
        if abs(report_age) <= REPORT_FRESH_SEC:
            add("report_timing", True, f"דוח ה-ADS-C נוצר {_fmt_s(report_age)}ש' "
                f"{'לפני' if report_age >= 0 else 'אחרי'} הקליטה — בזמן אמת")
        else:
            add("report_timing", False, f"דוח ה-ADS-C נוצר {_fmt_s(report_age)}ש' "
                f"{'לפני' if report_age >= 0 else 'אחרי'} הקליטה — לא בזמן אמת")
    elif has_msg_pos:
        add("report_timing", None, "אין בהודעה חותמת זמן למיקום")

    if acc_nm is not None:
        # libacars: קוד דיוק 0 = "none (NAV capability lost)" => ‏pos_accuracy_nm=-1.0
        if acc_nm < 0:
            add("nav_accuracy", False, "המטוס דיווח בדוח ה-ADS-C על אובדן יכולת ניווט — "
                "המיקום שבהודעה לא אמין")
        else:
            add("nav_accuracy", True, f"דיוק ניווט מדווח בדוח: ‎<{acc_nm:g}nm")

    err = msg.get("error")
    if isinstance(err, (int, float)) and not isinstance(err, bool):
        add("decode_clean", err == 0, "פענוח נקי (0 שגיאות)" if err == 0
            else f"המפענח דיווח על {err:g} שגיאות/תיקונים")
    else:
        add("decode_clean", None, "אין נתון שגיאות פענוח")

    nic = rx_at.get("nic") if rx_ok else None
    if nic is not None:
        add("gnss_integrity", nic >= INTEGRITY_NIC_MIN,
            f"שלמות GNSS ב-ADS-B: NIC={nic:g}" + ("" if nic >= INTEGRITY_NIC_MIN else " — ירודה"))
    elif rx_ok:
        add("gnss_integrity", None, "אין נתון NIC לנקודת ה-ADS-B")

    ok = {c["key"]: c["ok"] for c in crit}

    # --- רמת ראיות -----------------------------------------------------------
    applicable = bool(station) and rf_path == "vhf" and direction != "uplink"
    is_conflict = (conflict in ("hex_mismatch", "reg_mismatch")
                   or ok.get("position_agrees") is False)
    if not applicable:
        level = "n/a"
    elif is_conflict:
        level = "conflict"
    elif (ok["downlink"] is True and ok["identity"] is True and ok["adsb_at_rx"] is True
          and ok["position_agrees"] is True and ok["decode_clean"] is not False
          and ok.get("report_timing") is not False and ok.get("gnss_integrity") is not False
          and ok.get("nav_accuracy") is not False):
        level = "strong"
    elif ok["downlink"] is not False and ok["identity"] is not False and ok["adsb_at_rx"] is True:
        level = "partial"
    elif ac is not None:
        level = "weak"
    else:
        level = "none"

    # --- פסק דין משולב ------------------------------------------------------
    if rf_path == "satellite":
        verdict = "na_satellite"
    elif direction == "uplink":
        verdict = "na_uplink"
    elif not station:
        verdict = "no_station"
    elif ac is None or horizon is None:
        verdict = "insufficient"
    elif horizon["status"] == "beyond_radio_horizon":
        verdict = {"strong": "blos_supported", "partial": "blos_possible",
                   "conflict": "exceeds_conflict"}.get(level, "exceeds_unverified")
    else:
        verdict = horizon["status"]

    # --- הסתייגויות: מה *לא* נקבע ---------------------------------------------
    caveats = []
    if horizon is not None:
        caveats.append("מודל כדור חלק עם k=4/3: טופוגרפיה ומכשולים סביב התחנה לא מודלו.")
        if horizon["status"] in ("beyond_radio_horizon", "marginal"):
            caveats.append("מנגנון ההתפשטות (ducting / troposcatter / שבירה חריגה) לא נקבע — "
                           "אין במערכת מדידה מטאורולוגית שמבחינה ביניהם.")
        if horizon["aircraft_alt_assumed"]:
            caveats.append(f"גובה המטוס לא ידוע — האופק חושב בגובה {ALT_MAX_ASSUMED_FT:,.0f}ft "
                           "(החסם העליון), ולכן רק 'מעבר לאופק' הוא מסקנה תקפה.")
        elif horizon["aircraft_alt_kind"] in ("baro", "adsc"):
            caveats.append("גובה המטוס הוא גובה לחץ — הוא שונה מהגובה האמיתי בתנאים לא-סטנדרטיים "
                           f"(נלקחה אי-ודאות ±{ALT_UNCERT_FT['baro']:.0f}ft).")
    if rx_ok:
        stype = rx_at.get("src_type") or ""
        if stype == "mlat":
            caveats.append("מיקום ה-ADS-B ממולטילטרציה (MLAT) — בלתי-תלוי במערכת הניווט של המטוס.")
        elif stype.startswith(("adsb", "adsr")):
            caveats.append("ADS-B ו-ADS-C נגזרים שניהם ממערכת הניווט של המטוס: התאמה ביניהם "
                           "מאמתת פענוח, זהות ותזמון — לא את נכונות ה-GNSS עצמו (שיבוש עקבי "
                           "היה מזיז את שניהם יחד). רק MLAT בלתי-תלוי בה.")
    if report_age is not None and report_age < -CLOCK_SKEW_NOTE_SEC:
        caveats.append(f"דוח ה-ADS-C 'אחרי' הקליטה ב-{_fmt_s(report_age)}ש' — ייתכן ששעון התחנה מפגר.")
    if station:
        caveats.append(f"גובה אנטנת התחנה ({station.get('alt_m') or 0:g} מ' מעל פני הים) לפי ההגדרה — "
                       "לא נמדד ע\"י המערכת; האופק רגיש אליו (∝√h).")

    return {
        "rf_path": rf_path,
        "direction": direction,
        "direction_basis": direction_basis,
        "station": station,
        "aircraft_at_rx": ac,
        "horizon": horizon,
        "position_check": position_check,
        "adsc": {"report_t": report_t, "report_age_s": report_age,
                 "alt_ft": _f(adsc.get("alt_ft")), "accuracy_nm": acc_nm} if adsc else None,
        "evidence": crit,
        "level": level,
        "level_text": LEVEL_TEXT[level],
        "verdict": verdict,
        "verdict_text": VERDICT_TEXT[verdict],
        "caveats": caveats,
    }


def is_anomalous(result):
    """אירוע ששווה לשמור כראיה: חריגה מאופק הרדיו (או גבולי) בנתיב VHF."""
    hz = (result or {}).get("horizon") or {}
    return (result or {}).get("verdict") in ("blos_supported", "blos_possible",
                                             "exceeds_unverified", "exceeds_conflict",
                                             "marginal") and hz.get("distance_km") is not None


def summary(result):
    """תקציר קומפקטי לתג בפיד (נשלח בכל poll — קטן בכוונה)."""
    if not result:
        return None
    hz = result.get("horizon") or {}
    rx = result.get("rx_at") or {}
    pc = result.get("position_check") or {}
    return {"v": result.get("verdict"), "lvl": result.get("level"),
            "km": hz.get("distance_km"), "hz": hz.get("radio_km"),
            "adsb": rx.get("status"), "d": pc.get("distance_km"),
            "agree": pc.get("agree"), "by": (result.get("match") or {}).get("by")}
