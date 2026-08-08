# ============================================================================
#  AIR-AM - מטריצת ADS-C: מתי מיקום *מותר* להיווצר, בכל שלושת המסלולים
# ----------------------------------------------------------------------------
#  למה הקובץ הזה קיים בנפרד:
#
#  חילוץ המיקום מ-ADS-C נשבר **שלוש פעמים** (v2.15.1, v2.15.2, ושוב בסבב
#  ה-4-סוכנים), בכל פעם במסלול אחר, ובכל פעם התיקון נעשה בנקודה אחרת בקוד:
#    • ‏_normalize_acars   — משמש גם ל-SATCOM וגם ל-VDL2 מסלול A
#    • ‏_normalize_vdl2    — מסלול B (x25/CLNP), גייטינג משלו
#  שתי הפעמים הראשונות התגלו רק אחרי שמשתמש הצליב מול ADS-B חיצוני ומצא מיקום
#  שגוי באלפי ק"מ. הבדיקות הקיימות מכסות מקרים *בודדים* (uplink אחד, שגיאה
#  אחת) — אבל לא את המטריצה, כך שרגרסיה בתא שלא נבדק לא נתפסת.
#
#  החוק היחיד, בכל שלושת המסלולים (ר' §12 ב-CLAUDE.md):
#      מיקום מ-ADS-C מותר  ⟺  (הפענוח לא נכשל)  וגם  (הכיוון אינו uplink)
#
#  ‏uplink חסום גם **בלי שום שגיאה גלויה**: ‏tag מספרי ב-ADS-C מתפרש הפוך לפי
#  כיוון ההודעה (‏libacars/adsc.c — טבלאות תגיות נפרדות ל-uplink/downlink), כך
#  ש-uplink שנותח עם טבלת downlink "מצליח" מבנית ושולף פרמטרי-בקשה כאילו הם
#  נ"צ. כיוון *לא ידוע* נשאר permissive במכוון — לא משנים התנהגות במקום שלא נבדק.
#
#  ⚠ מגבלת אמינות מוצהרת: אין עדיין לכידת שדה אמיתית של ADS-C **downlink**
#  (כל מה שנקלט עד היום היה uplink — "P channel בלבד"). התאים החיוביים כאן
#  משוחזרים לפי הפרוטוקול, ומאמתים שה-guard אינו חוסם ביתר — לא שהמספרים
#  עצמם נצפו בשטח.
# ============================================================================
import itertools

import pytest

import app

LAT, LON = 18.34167, 2.11006


# --- בוני הודעות: אותו תוכן ADS-C בדיוק, בשלושת המסלולים ---------------------

def _adsc_app(decode_failed):
    """יישום ADS-C מפוענח. decode_failed => err:true בתוך היישום המקונן."""
    return {"msg_type": "adsc_msg", "crc_ok": True,
            "adsc": {"err": bool(decode_failed),
                     "basic_report": {"lat": LAT, "lon": LON}}}


def _satcom_msg(direction, decode_failed):
    inner = {"err": False, "crc_ok": True, "reg": ".A7-BBB", "mode": "2",
             "label": "A6", "msg_text": "/RECOEYA.ADS.A7-BBB070D0B00",
             "arinc622": _adsc_app(decode_failed)}
    acars = {"mode": "2", "label": "A6", "reg": "A7-BBB",
             "msg_text": "/RECOEYA.ADS.A7-BBB070D0B00",
             "arinc622": {"acars": inner}}          # מעטפת כפולה (ר' _normalize_satcom)
    aes, ges = "Aircraft Earth Station", "Ground Earth Station"
    src, dst = ((aes, ges) if direction == "downlink"
                else (ges, aes) if direction == "uplink" else ("Unknown", "Unknown"))
    return {"app": {"name": "JAERO"},
            "isu": {"acars": acars, "refno": "01", "qno": "02",
                    "src": {"addr": "738065", "type": src},
                    "dst": {"addr": "10", "type": dst}},
            "t": {"sec": int(app._today_start() + 100), "usec": 0}}


def _avlc(direction, extra):
    ac = {"addr": "738065", "type": "Aircraft", "status": "Airborne"}
    gs = {"addr": "10917A", "type": "Ground station"}
    if direction == "downlink":
        src, dst = ac, gs
    elif direction == "uplink":
        src, dst = gs, ac
    else:                                   # כיוון לא ידוע — לא Aircraft בשני הצדדים
        src = {"addr": "10917A", "type": "Unknown"}
        dst = {"addr": "738065", "type": "Unknown"}
    return {"src": src, "dst": dst, "cr": "Command", "frame_type": "I",
            "rseq": 0, "sseq": 2, "poll": False, **extra}


def _wrap_vdl2(avlc):
    return {"vdl2": {"app": {"name": "dumpvdl2", "ver": "2.6.0"},
                     "t": {"sec": int(app._today_start() + 100), "usec": 0},
                     "freq": 136975000, "sig_level": -22.1, "noise_level": -44.4,
                     "avlc": avlc}}


def _vdl2_path_a(direction, decode_failed):
    """ACARS-over-AVLC: היישום המקונן עובר דרך _normalize_acars."""
    return _wrap_vdl2(_avlc(direction, {"acars": {
        "err": False, "crc_ok": True, "more": False, "reg": ".4X-EDA",
        "mode": "2", "label": "H1", "blk_id": "2", "ack": "!",
        "flight": "LY0027", "msg_num": "D64", "msg_num_seq": "A",
        "msg_text": "#DFB...", "arinc622": _adsc_app(decode_failed)}}))


def _vdl2_path_b(direction, decode_failed):
    """‏X.25/CLNP — גייטינג נפרד לגמרי בתוך _normalize_vdl2."""
    return _wrap_vdl2(_avlc(direction, {"x25": {
        "err": False, "pkt_type_name": "Data",
        "clnp": {"cotp": {"adsc": {"err": bool(decode_failed),
                                   "basic_report": {"lat": LAT, "lon": LON}}}}}}))


PATHS = {
    "satcom": (_satcom_msg, lambda m: app._normalize_satcom(m)),
    "vdl2_path_a": (_vdl2_path_a, lambda m: app._normalize_vdl2(m)),
    "vdl2_path_b": (_vdl2_path_b, lambda m: app._normalize_vdl2(m)),
}


@pytest.mark.parametrize(
    "path,direction,decode_failed",
    list(itertools.product(PATHS, ["downlink", "uplink", "unknown"], [False, True])))
def test_adsc_position_matrix(path, direction, decode_failed):
    """מיקום מ-ADS-C מותר ⟺ הפענוח הצליח *וגם* הכיוון אינו uplink."""
    build, normalize = PATHS[path]
    rec = normalize(build(direction, decode_failed))
    assert rec is not None, f"{path}/{direction}: ההודעה לא נורמלה כלל"

    allowed = (not decode_failed) and direction != "uplink"
    got_pos = rec.get("lat") is not None or rec.get("lon") is not None
    assert got_pos is allowed, (
        f"{path} · dir={direction} · decode_failed={decode_failed}: "
        f"ציפינו למיקום={allowed}, קיבלנו lat={rec.get('lat')} lon={rec.get('lon')}")

    if allowed:
        assert rec["lat"] == pytest.approx(LAT)
        assert rec["lon"] == pytest.approx(LON)
        assert rec.get("pos_src") == "adsc"
        assert rec.get("group") == "position"
    else:
        # ⚠ לא רק ה-lat/lon: גם pos_src וגם group. ‏group משמש **לסינון** ב-UI,
        # ולכן כרטיס בלי מיקום שמסווג "📍 מיקום" הוא בעצמו הטעיה (הבאג המקורי).
        assert rec.get("pos_src") is None
        assert rec.get("group") != "position"


@pytest.mark.parametrize("path", list(PATHS))
def test_adsc_decode_failure_is_visible_not_silent(path):
    """"לא ניסינו" ≠ "ניסינו ונכשלנו": כשהמפענח עצמו החזיר שגיאה, הכרטיס אומר
    זאת במפורש במקום להציג None זהה למקרה שבו לא היה יישום מקונן כלל."""
    build, normalize = PATHS[path]
    rec = normalize(build("downlink", True))
    assert rec.get("decoded"), f"{path}: כישלון פענוח לא דווח למשתמש"
    assert "לא פוענח" in rec["decoded"]
