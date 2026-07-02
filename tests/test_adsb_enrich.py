# ============================================================================
#  AIR-AM - בדיקות היתוך ADS-B↔ACARS (העשרת /api/acars מ-snapshot של adsb.py)
# ----------------------------------------------------------------------------
#  רץ בלי רשת ובלי חומרה: process() מוזן fixtures ישירות (סכמת ADSBExchange v2),
#  ו-app ממוקף כמו ב-test_acars.py.
# ============================================================================
import time

import pytest

import adsb
import app


@pytest.fixture
def clean_adsb():
    with adsb._LOCK:
        adsb._S["aircraft"].clear()
    yield
    with adsb._LOCK:
        adsb._S["aircraft"].clear()


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "CONFIG_PATH", tmp_path / "airband.conf")
    monkeypatch.setattr(app, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(app, "ACARS_ENV_PATH", tmp_path / "acars.env")
    monkeypatch.setattr(app, "ACARS_LOG_PATH", tmp_path / "acars.jsonl")
    return tmp_path


@pytest.fixture
def client(paths):
    return app.app.test_client()


def _feed(ac_list, now=None):
    with adsb._LOCK:
        adsb.process(ac_list, now if now is not None else time.monotonic())


# מטוס תקין: מיקום אמין + סוג + callsign (עם רווח ריפוד, כמו בשידור אמיתי)
AC_OK = {"hex": "738065", "r": "4X-EHD", "t": "B789", "flight": "ELY315 ",
         "lat": 32.2, "lon": 34.7, "alt_baro": 12000, "gs": 320.0,
         "track": 290.0, "nic": 8, "seen_pos": 3}
# משובש GPS (nic=0): נ"צ "קופץ" ללבנון — מזויף
AC_SPOOF = {"hex": "5b1234", "r": "4X-EKS", "t": "B738", "lat": 33.8, "lon": 35.5,
            "alt_baro": 3000, "gs": 150.0, "track": 209.0, "nic": 0, "seen_pos": 2}


# --- norm_reg: גשר הנרמול ACARS↔ADS-B ---------------------------------------

def test_norm_reg():
    assert adsb.norm_reg(".4X-EHD") == "4XEHD"     # ריפוד נקודות של acarsdec
    assert adsb.norm_reg("4X-EHD") == "4XEHD"      # פורמט ADS-B
    assert adsb.norm_reg(" n123ab ") == "N123AB"
    assert adsb.norm_reg("") is None
    assert adsb.norm_reg(None) is None
    assert adsb.norm_reg("...") is None            # רק תווים לא-אלפאנומריים


# --- process() בונה snapshot פר-מטוס -----------------------------------------

def test_process_builds_snapshot(clean_adsb):
    _feed([AC_OK])
    a = adsb.aircraft_snapshot()["4XEHD"]
    assert a["type"] == "B789"
    assert a["flight"] == "ELY315"                 # רווח הריפוד קוצץ
    assert a["pos_ok"] and not a["spoofed"]
    assert a["lat"] == 32.2 and a["alt"] == 12000
    assert a["age"] >= 0                           # שניות מאז שנראה (לא t_mono פנימי)
    assert "t_mono" not in a


def test_spoofed_position_suppressed(clean_adsb):
    """nic<SPOOF_NIC => הנ"צ המזויף מדוכא, אבל גובה/track/מהירות (ששורדים
    את השיבוש) נשמרים — אותו עיקרון כמו classify()."""
    _feed([AC_SPOOF])
    s = adsb.aircraft_snapshot()["4XEKS"]
    assert s["spoofed"] and not s["pos_ok"]
    assert s["lat"] is None and s["lon"] is None
    assert s["alt"] == 3000 and s["track"] == 209.0 and s["gs"] == 150.0


def test_no_registration_not_in_snapshot(clean_adsb):
    _feed([{"hex": "abcdef", "lat": 32.1, "lon": 34.9, "alt_baro": 5000, "nic": 8}])
    assert adsb.aircraft_snapshot() == {}


def test_regs_filter(clean_adsb):
    """סינון regs => התשובה נושאת רק זנבות שמופיעים ב-ACARS."""
    _feed([AC_OK, AC_SPOOF])
    assert set(adsb.aircraft_snapshot({"4XEHD"})) == {"4XEHD"}
    assert set(adsb.aircraft_snapshot()) == {"4XEHD", "4XEKS"}
    assert adsb.aircraft_snapshot(set()) == {}


def test_prune_after_keep_sec(clean_adsb):
    """חסם זיכרון: מטוס שלא נראה AC_KEEP_SEC נגזם מה-snapshot."""
    now = time.monotonic()
    _feed([AC_OK], now)
    _feed([], now + adsb.AC_KEEP_SEC + 1)
    assert adsb.aircraft_snapshot() == {}


def test_ground_aircraft_alt_none(clean_adsb):
    _feed([{**AC_OK, "alt_baro": "ground"}])
    a = adsb.aircraft_snapshot()["4XEHD"]
    assert a["ground"] is True and a["alt"] is None


# --- אינטגרציה: /api/acars מחזיר את ההעשרה לזנבות שבזיכרון --------------------

def test_api_acars_adsb_enrichment(client, clean_adsb, monkeypatch):
    """הזרעת ACARS עם tail מרופד-נקודה ('.4X-EHD') + snapshot ADS-B ('4X-EHD')
    => /api/acars מחזיר adsb["4XEHD"] — גשר הנרמול בין הפורמטים עובד end-to-end.
    מטוס ADS-B שאינו ב-ACARS מסונן החוצה."""
    monkeypatch.setattr(app, "_is_active", lambda svc: True)
    with app._acars_lock:
        app._acars_msgs.clear()
        app._acars_seq = 1
        app._acars_msgs.append({"id": 1, "t": time.time(), "tail": ".4X-EHD",
                                "label": "H1", "text": "hi"})
    _feed([AC_OK, {**AC_SPOOF, "r": "G-XXXX"}])    # G-XXXX לא ב-ACARS => מסונן
    data = client.get("/api/acars?since=0").get_json()
    assert "4XEHD" in data["adsb"]
    assert data["adsb"]["4XEHD"]["type"] == "B789"
    assert "GXXXX" not in data["adsb"]


def test_api_acars_adsb_empty_without_data(client, clean_adsb, monkeypatch):
    """אין נתוני ADS-B (אין אינטרנט / thread טרם משך) => adsb == {} — נפילה
    חיננית, הפיד ממשיך כרגיל."""
    monkeypatch.setattr(app, "_is_active", lambda svc: True)
    with app._acars_lock:
        app._acars_msgs.clear()
        app._acars_seq = 1
        app._acars_msgs.append({"id": 1, "t": time.time(), "tail": "4X-EKF",
                                "label": "H1", "text": "hi"})
    data = client.get("/api/acars?since=0").get_json()
    assert data["adsb"] == {}
