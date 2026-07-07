# ============================================================================
#  AIR-AM - בדיקות הרוסטר המאוחד (/api/aircraft): היתוך ACARS+VDL2+ADS-B
# ----------------------------------------------------------------------------
#  חי בכל מצב — לא תלוי SDR הפעיל. רץ בלי רשת/חומרה: _acars_msgs/_vdl2_msgs
#  מוזרעים ישירות, ו-ADS-B מוזן ל-adsb.process() כמו ב-test_adsb_enrich.py.
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
    monkeypatch.setattr(app, "VDL2_ENV_PATH", tmp_path / "vdl2.env")
    monkeypatch.setattr(app, "VDL2_LOG_PATH", tmp_path / "vdl2.jsonl")
    return tmp_path


@pytest.fixture
def client(paths):
    return app.app.test_client()


@pytest.fixture(autouse=True)
def _reset_buffers():
    with app._acars_lock:
        app._acars_msgs.clear()
        app._acars_seq = 0
    with app._vdl2_lock:
        app._vdl2_msgs.clear()
        app._vdl2_seq = 0
    yield


def _feed_adsb(ac_list, now=None):
    with adsb._LOCK:
        adsb.process(ac_list, now if now is not None else time.monotonic())


def _acars(tail=None, flight=None, icao=None, t=1.0, category="הודעה", group="text",
          dir_=None, lat=None, lon=None, pos_src=None, actype=None):
    with app._acars_lock:
        app._acars_seq += 1
        app._acars_msgs.append({"id": app._acars_seq, "t": t, "tail": tail, "flight": flight,
                                "icao": icao, "category": category, "group": group,
                                "dir": dir_, "lat": lat, "lon": lon, "pos_src": pos_src,
                                "actype": actype})


def _vdl2(tail=None, flight=None, icao=None, t=1.0, category="VDL2", group="comm",
         dir_=None, lat=None, lon=None, pos_src=None, actype=None):
    with app._vdl2_lock:
        app._vdl2_seq += 1
        app._vdl2_msgs.append({"id": app._vdl2_seq, "t": t, "tail": tail, "flight": flight,
                               "icao": icao, "category": category, "group": group,
                               "dir": dir_, "lat": lat, "lon": lon, "pos_src": pos_src,
                               "actype": actype})


AC_OK = {"hex": "738065", "r": "4X-EHD", "t": "B789", "flight": "ELY315",
         "lat": 32.2, "lon": 34.7, "alt_baro": 12000, "gs": 320.0,
         "track": 290.0, "nic": 8, "seen_pos": 3}


# --- זהות + היתוך -------------------------------------------------------------

def test_roster_fuses_acars_and_vdl2_by_registration():
    """אותו רישום (עם/בלי נקודה מובילה) ב-ACARS וב-VDL2 => רשומה אחת מאוחדת."""
    _acars(tail=".4X-EKF", t=1.0, category="ATIS")
    _vdl2(tail="4X-EKF", t=2.0, category="OOOI")
    roster = app._build_roster()
    assert len(roster) == 1
    c = roster[0]
    assert sorted(c["sources"]) == ["acars", "vdl2"]
    assert c["count"] == 2
    assert c["last_category"] == "OOOI"     # ההודעה המאוחרת (t=2.0) קובעת


def test_roster_identity_by_icao_when_no_tail():
    """פריים VDL2 בלי tail (רק icao) => זהות נפרדת, לא מתמזג עם מטוס עם רישום."""
    _acars(tail="4X-AAA", t=1.0)
    _vdl2(tail=None, icao="ABCDEF", t=2.0)
    roster = app._build_roster()
    assert len(roster) == 2
    icao_entry = next(c for c in roster if c["icao"] == "ABCDEF")
    assert icao_entry["tail"] is None and icao_entry["sources"] == ["vdl2"]


def test_roster_identity_by_flight_when_no_tail_or_icao():
    _acars(tail=None, flight="LY001", t=1.0)
    roster = app._build_roster()
    assert len(roster) == 1 and roster[0]["flight"] == "LY001"


def test_roster_message_without_identity_ignored():
    _acars(tail=None, flight=None, icao=None, t=1.0)
    assert app._build_roster() == []


def test_roster_position_kept_from_latest_message_that_had_one():
    """הודעה מאוחרת בלי מיקום לא דורסת מיקום מהודעה קודמת עם מיקום."""
    _acars(tail="4X-POS", t=1.0, lat=32.0, lon=34.0, pos_src="text")
    _acars(tail="4X-POS", t=2.0, lat=None, lon=None)
    c = app._build_roster()[0]
    assert c["lat"] == 32.0 and c["lon"] == 34.0 and c["pos_src"] == "text"


def test_roster_fields_fill_from_whichever_message_has_them():
    """actype/flight/icao מתמלאים מכל הודעה שנושאת אותם, לא רק מהאחרונה."""
    _acars(tail="4X-FIL", t=1.0, actype="B738", flight="LY100")
    _acars(tail="4X-FIL", t=2.0, actype=None, flight=None, category="OOOI")
    c = app._build_roster()[0]
    assert c["actype"] == "B738" and c["flight"] == "LY100"
    assert c["last_category"] == "OOOI"


# --- מיון וגזירה ---------------------------------------------------------------

def test_roster_sorted_by_last_seen_desc():
    _acars(tail="4X-OLD", t=1.0)
    _acars(tail="4X-NEW", t=5.0)
    _acars(tail="4X-MID", t=3.0)
    roster = app._build_roster()
    assert [c["tail"] for c in roster] == ["4X-NEW", "4X-MID", "4X-OLD"]


def test_roster_caps_at_max(monkeypatch):
    monkeypatch.setattr(app, "ROSTER_MAX", 3)
    for i in range(6):
        _acars(tail="4X-%03d" % i, t=float(i))
    roster = app._build_roster()
    assert len(roster) == 3
    assert roster[0]["tail"] == "4X-005"    # החדשים ביותר נשארים


# --- העשרת ADS-B ---------------------------------------------------------------

def test_roster_includes_adsb_enrichment(clean_adsb):
    _acars(tail="4X-EHD", t=1.0)
    _feed_adsb([AC_OK])
    c = app._build_roster()[0]
    assert "adsb" in c and c["adsb"]["type"] == "B789"


def test_roster_no_adsb_key_without_match(clean_adsb):
    _acars(tail="4X-NOPE", t=1.0)
    c = app._build_roster()[0]
    assert "adsb" not in c


# --- /api/aircraft -------------------------------------------------------------

def test_api_aircraft_endpoint(client):
    _acars(tail="4X-EKF", t=1.0, category="ATIS")
    _vdl2(tail="4X-VDL", t=2.0, category="CPDLC (VDL2)")
    r = client.get("/api/aircraft")
    j = r.get_json()
    assert r.status_code == 200 and j["ok"]
    tails = {c["tail"] for c in j["aircraft"]}
    assert tails == {"4X-EKF", "4X-VDL"}


def test_api_aircraft_empty_when_no_messages(client):
    j = client.get("/api/aircraft").get_json()
    assert j["ok"] and j["aircraft"] == []
