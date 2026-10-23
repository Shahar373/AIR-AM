# ============================================================================
#  AIR-AM - בדיקות xcheck.py: גאודזיה, אופק רדיו, זהות, מיקום-בזמן והערכת BLOS
# ----------------------------------------------------------------------------
#  פונקציות טהורות — בלי רשת, בלי קבצים. מקרה הבדיקה המרכזי: הודעת ה-ADS-C
#  האמיתית של G-ZBKM (VHF, ‏131.825, ‏23.10.2026 13:45:40 UTC). ‏⚠ נתוני ה-ADS-B
#  כאן *סינתטיים* (מסלול ישר שנבנה בבדיקה) — הם בודקים את הלוגיקה, לא טוענים
#  דבר על המיקום האמיתי של המטוס. האימות האמיתי רץ על ה-Pi מול adsb.lol.
# ============================================================================
import calendar
import math

import pytest

import xcheck as xc

# --- המקרה האמיתי --------------------------------------------------------------
# זמן הקליטה כפי שדווח, ו-ts_sec מתוך הדוח עצמו: פענוח ידני של
# ‎071798089D4D8946EACD9F (Basic report, tag 7) לפי פריסת הביטים של libacars/adsc.c
# (lat/lon 21 ביט, alt 16×4ft, ts 15×0.125s) נותן 33.17888N 27.65087E, ‏37996ft,
# ‏ts=2739.375 (xx:45:39.375). אותם ערכים ש-decode_acars_apps החזיר (CHANGELOG 2.26.0).
T_RX = calendar.timegm((2026, 10, 23, 13, 45, 40)) + 0.0
ZBKM_LAT, ZBKM_LON, ZBKM_ALT = 33.17888, 27.65087, 37996
ZBKM_TS_SEC = 2739.375
# ⚠ נ"צ משוערים של תל חדיד לבדיקה בלבד — ההגדרה של המשתמש היא מקור-האמת.
STATION = {"lat": 31.968, "lon": 34.935, "alt_m": 150.0}

ZBKM_MSG = {"t": T_RX, "freq": 131.825, "level": -46.9, "label": "B6",
            "tail": ".G-ZBKM", "flight": "BA0060", "lat": ZBKM_LAT, "lon": ZBKM_LON,
            "pos_src": "adsc", "error": 0, "dir": "downlink",
            "text": "/QUKAXBA.ADS.G-ZBKM071798089D4D8946EACD9F84D7"}


def _dest(lat, lon, brg_deg, km):
    """נקודת יעד על כדור (לבניית מסלולים סינתטיים)."""
    d = km / xc.R_EARTH_KM
    p1, l1, b = math.radians(lat), math.radians(lon), math.radians(brg_deg)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
    l2 = l1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(p1),
                         math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), math.degrees(l2)


def _track(lat0, lon0, t0, brg=120.0, gs=480.0, times=(-60, -30, 0, 30, 60), **kw):
    """מסלול ישר סינתטי: נקודות ב-t0+dt, עובר ב-(lat0,lon0) בזמן t0."""
    pts = []
    for dt in times:
        km = gs * xc.KT_KMS * dt
        la, lo = _dest(lat0, lon0, brg if km >= 0 else brg + 180, abs(km))
        pts.append(xc.make_point(t0 + dt, la, lo, alt_baro=38000, alt_geom=38600, gs=gs,
                                 track=brg, src_type=kw.get("src_type", "adsb_icao"),
                                 nic=kw.get("nic", 8), nac_p=kw.get("nac_p", 9),
                                 provider="test", origin="trace"))
    return pts


# --- גאודזיה -----------------------------------------------------------------

def test_geodesic_vincenty_reference():
    """דוגמת הייחוס של Vincenty (Flinders Peak → Buninyong): 54,972.271 מ'."""
    d = xc.geodesic_km(-(37 + 57 / 60 + 3.72030 / 3600), 144 + 25 / 60 + 29.52440 / 3600,
                       -(37 + 39 / 60 + 10.15610 / 3600), 143 + 55 / 60 + 35.38390 / 3600)
    assert abs(d * 1000 - 54972.271) < 0.01


def test_geodesic_degenerate_and_antipodal():
    assert xc.geodesic_km(32.0, 34.8, 32.0, 34.8) == 0.0
    # כמעט-אנטיפודלי: Vincenty לא מתכנס => haversine, לא חריגה ולא לולאה
    d = xc.geodesic_km(0.0, 0.0, 0.5, 179.7)
    assert 19900 < d < 20050
    # קו המשווה (cos²α=0) לא מחלק באפס
    assert abs(xc.geodesic_km(0.0, 10.0, 0.0, 11.0) - 111.32) < 0.05


def test_interpolate_gc_midpoint_on_meridian():
    lat, lon = xc.interpolate_gc(30.0, 35.0, 32.0, 35.0, 0.5)
    assert abs(lat - 31.0) < 1e-9 and abs(lon - 35.0) < 1e-9


# --- אופק ----------------------------------------------------------------------

def test_horizon_textbook_values():
    # "4.12·√h" ק"מ למודל 4/3 ו-"3.57·√h" לגאומטרי
    assert abs(xc.horizon_km(150, xc.K_STANDARD) - 4.12 * math.sqrt(150)) < 0.1
    assert abs(xc.horizon_km(150) - 3.57 * math.sqrt(150)) < 0.1
    assert xc.horizon_km(0) == 0.0 and xc.horizon_km(None) == 0.0 and xc.horizon_km(-5) == 0.0


def test_horizons_zbkm():
    """150 מ' + 37,996ft: אופק רדיו ≈494 ק"מ, גאומטרי ≈428 ק"מ."""
    hz = xc.horizons(150.0, ZBKM_ALT * xc.FT_M)
    assert 490 < hz["radio_km"] < 498
    assert 424 < hz["geometric_km"] < 432
    assert hz["k"] == round(4 / 3, 4)


@pytest.mark.parametrize("dist,unc,expect", [
    (300.0, 1.0, "line_of_sight"),
    (460.0, 1.0, "within_radio_horizon"),
    (699.0, 5.0, "beyond_radio_horizon"),
    (496.0, 10.0, "marginal"),            # הקצה הקרוב של הטווח בתוך האופק
    (497.0, 0.0, "marginal"),             # מעבר לנומינלי, בתוך חסם הגובה העליון (baro ±1000ft)
])
def test_classify_horizon(dist, unc, expect):
    r = xc.classify_horizon(dist, unc, 150.0, ZBKM_ALT, "baro")
    assert r["status"] == expect, r


def test_classify_horizon_unknown_altitude_is_conservative():
    """בלי גובה: מחשבים עם 45,000ft — "מעבר" רק אם חורג גם מזה; אחרת לא נטען כלום."""
    assert xc.classify_horizon(699.0, 5.0, 150.0, None, None)["status"] == "beyond_radio_horizon"
    r = xc.classify_horizon(400.0, 5.0, 150.0, None, None)
    assert r["status"] == "unknown_altitude" and r["aircraft_alt_assumed"]


# --- זהות ----------------------------------------------------------------------

def test_norm_hex():
    assert xc.norm_hex("406f79") == "406F79"
    assert xc.norm_hex(" 406F79 ") == "406F79"
    assert xc.norm_hex("~4a1b2c") is None          # readsb: כתובת לא-ICAO
    assert xc.norm_hex("406F7") is None and xc.norm_hex("ZZZZZZ") is None
    assert xc.norm_hex(None) is None


@pytest.mark.parametrize("raw,key", [
    ("BA0060", "BAW60"), ("BAW60", "BAW60"), ("BAW60 ", "BAW60"),
    ("LY0315", "ELY315"), ("ELY315", "ELY315"), ("W64321", "WZZ4321"),
    ("XX1234", None),          # חברה שלא בטבלה => לא מנחשים
    ("BAW8MN", "BAW8MN"), ("", None), (None, None),
])
def test_flight_key(raw, key):
    assert xc.flight_key(raw) == key


def test_message_identity_hex_only_from_structural_source():
    ident = xc.message_identity({"tail": ".G-ZBKM", "flight": "BA0060"})
    assert ident["hex"] is None and ident["reg"] == "GZBKM" and ident["flight_key"] == "BAW60"
    v = xc.message_identity({"icao": "406f79", "tail": "G-ZBKM"})
    assert v["hex"] == "406F79" and v["hex_src"] == "message"
    u = xc.message_identity({"tail": "G-ZBKM"}, hex_override="406F79")
    assert u["hex_src"] == "user"
    # tail של תחנת קרקע לא נחשב רישום מטוס
    assert xc.message_identity({"tail": ".TCARC", "tail_is_station": True})["reg"] is None


def test_match_record_priority_and_conflicts():
    ident = {"hex": None, "reg": "GZBKM", "flight_key": "BAW60"}
    assert xc.match_record(ident, {"hex": "406f79", "reg": "G-ZBKM", "flight": "BAW60"}) == ("reg", None)
    assert xc.match_record(ident, {"hex": "406f79", "reg": None, "flight": "BAW60"}) == ("flight", None)
    # רישום סותר => סתירה, גם כשמספר הטיסה תואם
    assert xc.match_record(ident, {"hex": "400000", "reg": "G-XXXX", "flight": "BAW60"}) == (None, "reg_mismatch")
    hex_ident = {"hex": "406F79", "reg": "GZBKM", "flight_key": None}
    assert xc.match_record(hex_ident, {"hex": "406f79", "reg": "G-ZBKM"}) == ("hex", None)
    assert xc.match_record(hex_ident, {"hex": "4CA000", "reg": "G-ZBKM"}) == (None, "hex_mismatch")
    assert xc.match_record({"hex": None, "reg": None, "flight_key": None}, {"hex": "406f79"}) == (None, "no_common_id")


# --- מיקום-בזמן ------------------------------------------------------------------

def test_position_measured():
    pts = _track(ZBKM_LAT, ZBKM_LON, T_RX + 8, times=(0,))
    r = xc.position_at(pts, T_RX)
    assert r["status"] == "measured" and r["dt_s"] == 8.0
    # אי-ודאות = מהירות × |dt| + EPU של NACp=9 (30 מ')
    assert abs(r["uncert_km"] - (480 * xc.KT_KMS * 8 + 0.03)) < 0.02
    assert r["alt_ft"] == 38600 and r["alt_kind"] == "geom"     # גאומטרי עדיף על baro


def test_position_interpolated_lands_on_track():
    # נקודות ב-±40ש' (מעבר ל-MEASURED_MAX_DT) => אינטרפולציה; המסלול ישר => נוחת
    # על הנקודה שהמסלול עובר בה בזמן הקליטה
    pts = _track(ZBKM_LAT, ZBKM_LON, T_RX, times=(-40, 45))
    r = xc.position_at(pts, T_RX)
    assert r["status"] == "interpolated", r
    assert xc.geodesic_km(r["lat"], r["lon"], ZBKM_LAT, ZBKM_LON) < 0.2
    assert r["dt_s"] == [-40.0, 45.0] and r["gap_s"] == 85.0
    assert r["uncert_km"] < 0.5                 # קו ישר, מהירות קבועה


def test_position_gap_too_large_is_unverified_not_interpolated():
    pts = _track(ZBKM_LAT, ZBKM_LON, T_RX, times=(-100, 100))
    r = xc.position_at(pts, T_RX)
    assert r["status"] == "unverified" and r["reason"] == "gap_too_large"
    assert abs(r["dt_s"]) == 100.0             # הנקודה האמיתית — לא מוזזת


def test_position_turn_between_points_blocks_interpolation():
    a = _track(ZBKM_LAT, ZBKM_LON, T_RX, brg=120.0, times=(-40,))
    b = _track(ZBKM_LAT, ZBKM_LON, T_RX, brg=180.0, times=(45,))
    r = xc.position_at(a + b, T_RX)
    assert r["status"] == "unverified" and r["reason"] == "turning"


def test_position_speed_inconsistent_blocks_interpolation():
    a = _track(ZBKM_LAT, ZBKM_LON, T_RX, times=(-40,))
    far = xc.make_point(T_RX + 45, ZBKM_LAT - 2.0, ZBKM_LON + 2.0, alt_baro=38000, gs=480,
                        track=120.0, nic=8)
    r = xc.position_at(a + [far], T_RX)
    assert r["status"] == "unverified" and r["reason"] == "speed_inconsistent"


def test_position_one_sided_and_too_far():
    r = xc.position_at(_track(ZBKM_LAT, ZBKM_LON, T_RX, times=(-300,)), T_RX)
    assert r["status"] == "unverified" and r["reason"] == "one_sided"
    r = xc.position_at(_track(ZBKM_LAT, ZBKM_LON, T_RX, times=(-3600,)), T_RX)
    assert r["status"] == "none" and r["reason"] == "too_far_in_time"


def test_position_spoofed_and_adsc_points_are_excluded():
    """nic<SPOOF_NIC = מזויף; type=adsc = מקור לא בלתי-תלוי. אף אחד לא נספר."""
    spoof = _track(ZBKM_LAT, ZBKM_LON, T_RX, times=(0,), nic=0)
    r = xc.position_at(spoof, T_RX)
    assert r["status"] == "none" and r["reason"] == "spoofed"
    adsc = _track(ZBKM_LAT, ZBKM_LON, T_RX, times=(0,), src_type="adsc")
    r = xc.position_at(adsc, T_RX)
    assert r["status"] == "none" and r["reason"] == "not_independent"
    assert xc.position_at([], T_RX) == {"status": "none", "reason": "no_data",
                                        "excluded": {"no_pos": 0, "spoofed": 0, "adsc": 0}}


def test_position_t_uncert_widens_error():
    p = _track(ZBKM_LAT, ZBKM_LON, T_RX, times=(0,))[0]
    p["t_uncert"] = 60.0
    r = xc.position_at([p], T_RX)
    assert r["uncert_km"] > 480 * xc.KT_KMS * 60 - 0.01


def test_adsc_report_time_hour_wrap():
    assert xc.adsc_report_time(ZBKM_TS_SEC, T_RX) == T_RX - 0.625
    # קליטה ב-14:00:02, דוח ב-xx:59:58 => השעה הקודמת
    t = calendar.timegm((2026, 10, 23, 14, 0, 2))
    assert xc.adsc_report_time(3598.0, t) == t - 4.0
    assert xc.adsc_report_time(3600.0, t) is None and xc.adsc_report_time(None, t) is None


# --- הערכה: G-ZBKM ---------------------------------------------------------------

def _assess_zbkm(points, msg=None, station=STATION, match=None, source="acars", direction="downlink"):
    msg = msg or ZBKM_MSG
    rx_at = xc.position_at(points, msg["t"])
    report_t = xc.adsc_report_time(ZBKM_TS_SEC, msg["t"]) if msg.get("pos_src") == "adsc" else None
    pos_at = xc.position_at(points, report_t) if report_t else None
    return xc.assess(source, msg, direction, station,
                     match or {"by": "reg", "conflict": None}, rx_at, pos_at,
                     {"t": report_t, "alt_ft": ZBKM_ALT, "accuracy_nm": 0.05} if report_t else None,
                     direction_basis="label B6")


def test_zbkm_without_adsb_is_exceedance_but_unverified():
    """רק מיקום ה-ADS-C עצמו (בלי ADS-B): חריגה מאופק — אבל ראיה חלשה בלבד."""
    r = _assess_zbkm([], match={"by": None, "conflict": "no_common_id"})
    hz = r["horizon"]
    assert hz["status"] == "beyond_radio_horizon"
    assert 690 < hz["distance_km"] < 705 and 490 < hz["radio_km"] < 498
    assert hz["aircraft_alt_kind"] == "adsc"
    assert r["aircraft_at_rx"]["source"] == "message_timed"     # הדוח נוצר 0.6ש' לפני הקליטה
    assert r["verdict"] == "exceeds_unverified" and r["level"] == "weak"
    ev = {c["key"]: c["ok"] for c in r["evidence"]}
    assert ev["report_timing"] is True and ev["adsb_at_rx"] is False


def test_zbkm_with_agreeing_adsb_is_blos_supported():
    """ADS-B (סינתטי) שעובר בדיוק במיקום ה-ADS-C בזמן הדוח => כל הקריטריונים
    מתקיימים: רמת ראיות "strong", ופסק-דין blos_supported — עדיין עם הסתייגות
    מפורשת שמנגנון ההתפשטות לא נקבע."""
    pts = _track(ZBKM_LAT, ZBKM_LON, T_RX - 0.625, times=(-40, 45))
    r = _assess_zbkm(pts)
    assert r["verdict"] == "blos_supported", r["evidence"]
    assert r["level"] == "strong"
    assert r["aircraft_at_rx"]["source"] == "adsb_interpolated"
    assert r["position_check"]["agree"] is True and r["position_check"]["distance_km"] < 1.0
    assert r["position_check"]["msg_pos_t_known"] is True
    assert any("מנגנון ההתפשטות" in c for c in r["caveats"])
    assert any("MLAT" in c for c in r["caveats"])               # ADS-B ו-ADS-C מאותו GNSS


def test_zbkm_mlat_source_noted_as_independent():
    pts = _track(ZBKM_LAT, ZBKM_LON, T_RX - 0.625, times=(5,), src_type="mlat", nac_p=None)
    r = _assess_zbkm(pts)
    assert r["level"] == "strong"
    assert any("בלתי-תלוי במערכת הניווט" in c for c in r["caveats"])


def test_zbkm_conflicting_adsb_rejects_blos_claim():
    """ADS-B מראה את המטוס 300 ק"מ ממיקום ה-ADS-C באותו זמן => "סתירה" — בדיוק
    משפחת הבאג של §12 (מיקום ADS-C מפענוח שגוי), לא "BLOS"."""
    la, lo = _dest(ZBKM_LAT, ZBKM_LON, 300.0, 300.0)
    pts = _track(la, lo, T_RX - 0.625, times=(-10, 10))
    r = _assess_zbkm(pts)
    assert r["level"] == "conflict"
    assert r["position_check"]["agree"] is False
    ev = {c["key"]: c["ok"] for c in r["evidence"]}
    assert ev["position_agrees"] is False


def test_zbkm_adsb_far_in_time_is_weak():
    pts = _track(ZBKM_LAT, ZBKM_LON, T_RX, times=(-600,))
    r = _assess_zbkm(pts)
    ev = {c["key"]: c["ok"] for c in r["evidence"]}
    assert ev["adsb_at_rx"] is None                 # הקשר בלבד
    assert r["level"] == "weak" and r["verdict"] == "exceeds_unverified"


def test_zbkm_flight_only_match_caps_at_partial():
    pts = _track(ZBKM_LAT, ZBKM_LON, T_RX - 0.625, times=(-40, 45))
    r = _assess_zbkm(pts, match={"by": "flight", "conflict": None})
    assert r["level"] == "partial" and r["verdict"] == "blos_possible"


def test_low_nic_caps_at_partial():
    pts = _track(ZBKM_LAT, ZBKM_LON, T_RX - 0.625, times=(5,), nic=4)
    r = _assess_zbkm(pts)
    assert r["level"] == "partial"
    ev = {c["key"]: c["ok"] for c in r["evidence"]}
    assert ev["gnss_integrity"] is False


def test_message_without_position_uses_adsb_and_is_partial():
    """הודעה בלי מיקום: מיקום המטוס בזמן הקליטה מגיע מ-ADS-B — מספיק לגאומטריה,
    אבל בלי הצלבה עצמית הרמה נעצרת ב"partial"."""
    msg = {**ZBKM_MSG, "lat": None, "lon": None, "pos_src": None, "label": "H1"}
    pts = _track(ZBKM_LAT, ZBKM_LON, T_RX, times=(3,))
    r = _assess_zbkm(pts, msg=msg)
    assert r["horizon"]["status"] == "beyond_radio_horizon"
    assert r["verdict"] == "blos_possible" and r["level"] == "partial"
    assert r["position_check"] is None


def test_not_applicable_paths():
    pts = _track(ZBKM_LAT, ZBKM_LON, T_RX, times=(3,))
    assert _assess_zbkm(pts, source="satcom")["verdict"] == "na_satellite"
    up = _assess_zbkm(pts, direction="uplink")
    assert up["verdict"] == "na_uplink" and up["level"] == "n/a"
    ns = _assess_zbkm(pts, station=None)
    assert ns["verdict"] == "no_station" and ns["horizon"] is None


def test_decode_errors_cap_strong():
    pts = _track(ZBKM_LAT, ZBKM_LON, T_RX - 0.625, times=(-40, 45))
    r = _assess_zbkm(pts, msg={**ZBKM_MSG, "error": 2})
    assert r["level"] == "partial"


def test_within_horizon_message():
    """מטוס 150 ק"מ מהתחנה ב-FL380 — בקו ראייה; לא אירוע חריג."""
    la, lo = _dest(STATION["lat"], STATION["lon"], 270.0, 150.0)
    msg = {**ZBKM_MSG, "lat": la, "lon": lo}
    pts = _track(la, lo, T_RX - 0.625, times=(-40, 45))
    r = _assess_zbkm(pts, msg=msg)
    assert r["verdict"] == "line_of_sight" and r["level"] == "strong"
    assert not xc.is_anomalous(r)


def test_is_anomalous_and_summary():
    pts = _track(ZBKM_LAT, ZBKM_LON, T_RX - 0.625, times=(-40, 45))
    r = _assess_zbkm(pts)
    assert xc.is_anomalous(r)
    full = {**r, "rx_at": xc.position_at(pts, T_RX), "match": {"by": "reg"}}
    s = xc.summary(full)
    assert s["v"] == "blos_supported" and s["adsb"] == "interpolated" and s["by"] == "reg"
    assert 690 < s["km"] < 705 and s["agree"] is True
    assert xc.summary(None) is None
