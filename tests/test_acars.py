# ============================================================================
#  AIR-AM - בדיקות יחידה למצב ACARS (מצב משולב: SDR אחד בהחלפה)
# ----------------------------------------------------------------------------
#  רץ בלי חומרה: ACARS_ENV_PATH מנותב ל-tmp, ו-systemctl/SDR ממוקפים.
# ============================================================================
import json
import socket
import threading
import time
import types

import pytest

import app


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


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(app.time, "sleep", lambda *a, **k: None)


def _ok(**kw):
    return types.SimpleNamespace(returncode=0, stdout="", stderr="", **kw)


# --- _sanitize_freqs (הגנת הזרקה) -------------------------------------------

def test_sanitize_freqs_filters_and_defaults():
    assert app._sanitize_freqs(["131.525", "bad; reboot", "136.9"]) == ["131.525", "136.9"]
    assert app._sanitize_freqs([]) == list(app.ACARS_FREQS_DEFAULT)
    assert app._sanitize_freqs(["$(reboot)", "x"]) == list(app.ACARS_FREQS_DEFAULT)
    assert app._sanitize_freqs(["131.55"]) == ["131.55"]   # 2 ספרות עשרוניות גם תקין


# --- write_acars_env (פורמט EnvironmentFile של systemd) ---------------------

def test_write_acars_env_format(paths):
    app.write_acars_env(["131.525", "131.725"], gain=-10, ratemult=160)
    txt = app.ACARS_ENV_PATH.read_text()
    # ערך לא מצוטט: systemd לוקח את שארית השורה; ‎$ACARS_FREQS ב-ExecStart מפצל בחזרה
    assert "ACARS_FREQS=131.525 131.725" in txt
    assert "ACARS_GAIN=-10" in txt
    assert "ACARS_RATEMULT=160" in txt
    assert f"ACARS_UDP={app.ACARS_UDP_HOST}:{app.ACARS_UDP_PORT}" in txt


def test_write_acars_env_sanitizes(paths):
    app.write_acars_env(["131.525", "evil; rm -rf /"], gain=-10)
    line = [l for l in app.ACARS_ENV_PATH.read_text().splitlines() if l.startswith("ACARS_FREQS")][0]
    assert line == "ACARS_FREQS=131.525"          # הערך הזדוני סונן


# --- _normalize_acars -------------------------------------------------------

def test_normalize_acars_full():
    m = {"timestamp": 1750000000.5, "channel": 1, "freq": 131.725, "level": -24.3,
         "error": 0, "mode": "2", "label": "H1", "block_id": "7", "ack": False,
         "tail": "4X-EKF", "flight": "LY315", "msgno": "M01A", "text": "HELLO\r\nWORLD\r"}
    n = app._normalize_acars(m)
    assert (n["tail"], n["flight"], n["freq"], n["label"], n["mode"]) == \
        ("4X-EKF", "LY315", 131.725, "H1", "2")
    assert n["text"] == "HELLO\nWORLD"             # CRLF מנורמל + strip
    assert n["msgno"] == "M01A"


def test_normalize_acars_empty_ack_tolerated():
    n = app._normalize_acars({"timestamp": 1.0, "freq": 131.55, "error": 0})
    assert n["tail"] is None and n["flight"] is None and n["text"] is None
    assert n["freq"] == 131.55


# --- listener + /api/acars roundtrip ----------------------------------------

def test_acars_listener_and_api(client, monkeypatch):
    monkeypatch.setattr(app, "_is_active", lambda svc: True)
    with app._acars_lock:                          # מאפסים את ה-buffer הגלובלי
        app._acars_msgs.clear()
        app._acars_seq = 0
    threading.Thread(target=app._acars_listener, daemon=True).start()
    time.sleep(0.2)

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.sendto(json.dumps({"timestamp": 1.0, "freq": 131.725, "tail": "4X-EKF",
                         "flight": "LY1", "label": "H1", "text": "hi"}).encode(),
             (app.ACARS_UDP_HOST, app.ACARS_UDP_PORT))
    s.sendto(json.dumps({"timestamp": 2.0, "freq": 131.55, "error": 0}).encode(),
             (app.ACARS_UDP_HOST, app.ACARS_UDP_PORT))
    s.sendto(b"not-json-garbage", (app.ACARS_UDP_HOST, app.ACARS_UDP_PORT))  # יתעלם

    deadline = time.time() + 3
    while time.time() < deadline:
        data = client.get("/api/acars?since=0").get_json()
        if len(data["messages"]) >= 2:
            break
        time.sleep(0.05)

    assert data["ok"] and data["active"] is True
    assert len(data["messages"]) == 2              # ה-garbage לא נכנס
    assert data["messages"][0]["tail"] == "4X-EKF"
    cursor = data["cursor"]
    assert cursor == 2
    # since=cursor => אין חדשות
    assert client.get("/api/acars?since=%d" % cursor).get_json()["messages"] == []


# --- /api/mode --------------------------------------------------------------

def test_api_mode_invalid(client, paths):
    assert client.post("/api/mode", json={"mode": "bogus"}).status_code == 400


def test_api_mode_enter_acars(client, paths, no_sleep, monkeypatch):
    calls = []
    monkeypatch.setattr(app, "_sysctl",
                        lambda action, svc, timeout=45: calls.append((action, svc)) or _ok())
    monkeypatch.setattr(app, "_is_active", lambda svc: True)   # acarsdec "active" אחרי start
    r = client.post("/api/mode", json={"mode": "acars"})
    j = r.get_json()
    assert r.status_code == 200 and j["ok"] and j["app_mode"] == "acars"
    assert j["acars_freqs"] == list(app.ACARS_FREQS_DEFAULT)
    assert app.load_state()["app_mode"] == "acars"
    # שחרר את rtl_airband והרים את acarsdec
    assert ("stop", "rtl_airband") in calls and ("restart", app.ACARS_SERVICE) in calls
    # נכתב env תקין
    assert "ACARS_FREQS=131.525 131.550 131.725 131.825" in app.ACARS_ENV_PATH.read_text()


def test_api_mode_enter_acars_failure_recovers_voice(client, paths, no_sleep, monkeypatch):
    app.save_state({**app.DEFAULT_STATE, "freq": 121.5})
    monkeypatch.setattr(app, "_sysctl", lambda action, svc, timeout=45: _ok())
    monkeypatch.setattr(app, "_is_active", lambda svc: False)  # acarsdec לא עלה => כישלון
    r = client.post("/api/mode", json={"mode": "acars"})
    assert r.status_code == 500
    assert r.get_json()["state"]["app_mode"] == "voice"        # חזרה לקול


def test_api_mode_voice_stops_acars_and_tunes(client, paths, monkeypatch):
    app.save_state({**app.DEFAULT_STATE, "freq": 121.5, "app_mode": "acars"})
    monkeypatch.setattr(app, "_restart_and_verify", lambda: (None, None, False))
    monkeypatch.setattr(app, "_is_active", lambda svc: True)   # acars פעיל כרגע
    calls = []
    monkeypatch.setattr(app, "_sysctl",
                        lambda action, svc, timeout=45: calls.append((action, svc)) or _ok())
    r = client.post("/api/mode", json={"mode": "voice"})
    j = r.get_json()
    assert r.status_code == 200 and j["ok"] and j["app_mode"] == "voice"
    assert j["freq"] == 121.5                                  # התדר השמור האחרון
    assert ("stop", app.ACARS_SERVICE) in calls               # acarsdec נעצר


def test_api_tune_exits_acars_mode(client, paths, monkeypatch):
    monkeypatch.setattr(app, "_restart_and_verify", lambda: (None, None, False))
    monkeypatch.setattr(app, "_is_active", lambda svc: True)
    calls = []
    monkeypatch.setattr(app, "_sysctl",
                        lambda action, svc, timeout=45: calls.append((action, svc)) or _ok())
    r = client.post("/api/tune", json={"freq": 134.6})
    assert r.get_json()["ok"]
    st = app.load_state()
    assert st["app_mode"] == "voice" and st["freq"] == 134.6
    assert ("stop", app.ACARS_SERVICE) in calls               # כיוונון קולי עוצר ACARS


# --- /api/state reconciliation ----------------------------------------------

def test_api_state_reports_live_mode(client, paths, monkeypatch):
    monkeypatch.setattr(app, "_is_active", lambda svc: svc == app.ACARS_SERVICE)
    assert client.get("/api/state").get_json()["app_mode"] == "acars"
    monkeypatch.setattr(app, "_is_active", lambda svc: False)
    body = client.get("/api/state").get_json()
    assert body["app_mode"] == "voice"
    assert body["acars_freqs"] == list(app.ACARS_FREQS_DEFAULT)


# --- התמדה: acars.jsonl + טעינה בעלייה --------------------------------------

def _reset_buffer():
    with app._acars_lock:
        app._acars_msgs.clear()
        app._acars_seq = 0


def test_acars_log_append_and_load_history(paths):
    _reset_buffer()
    for t in (3.0, 1.0, 2.0):                       # סדר כתיבה לא-ממוין בזמן
        app._append_acars_log({"t": t, "freq": 131.55, "tail": "4X-A%d" % int(t)})
    app._load_acars_history()
    with app._acars_lock:
        msgs = list(app._acars_msgs)
    assert [m["t"] for m in msgs] == [1.0, 2.0, 3.0]   # ממוין עולה לפי t
    assert [m["id"] for m in msgs] == [1, 2, 3]        # id רץ הוקצה
    assert app._acars_seq == 3


def test_acars_log_trim(paths, monkeypatch):
    monkeypatch.setattr(app, "ACARS_LOG_KEEP", 5)
    for i in range(12):
        app._append_acars_log({"t": float(i), "freq": 131.55})
    app._trim_acars_log()
    lines = app.ACARS_LOG_PATH.read_text().splitlines()
    assert len(lines) == 5                             # נחתך ל-KEEP
    assert json.loads(lines[0])["t"] == 7.0            # נשמר הזנב (7..11)
    assert json.loads(lines[-1])["t"] == 11.0


def test_load_history_tolerates_garbage(paths):
    _reset_buffer()
    app.ACARS_LOG_PATH.write_text('{"t": 1.0, "freq": 131.5}\nnot-json\n{"t": 2.0}\n')
    app._load_acars_history()
    with app._acars_lock:
        msgs = list(app._acars_msgs)
    assert [m["t"] for m in msgs] == [1.0, 2.0]        # השורה הפגומה דולגה


# --- נרמול עשיר: קטגוריה + מיקום --------------------------------------------

def test_normalize_category_from_labels():
    q0 = app._normalize_acars({"timestamp": 1.0, "label": "Q0"})
    assert q0["category"] == "בדיקת קישור (link test)" and q0["group"] == "comm"
    assert app._normalize_acars({"timestamp": 1.0, "label": "QA"})["group"] == "oooi"
    unk = app._normalize_acars({"timestamp": 1.0, "label": "ZZ"})   # לא מוכר => fallback
    assert unk["category"] == "Label ZZ" and unk["group"] == "text"
    assert app._normalize_acars({"timestamp": 1.0})["category"] == "הודעה"   # בלי label


def test_normalize_position_from_libacars():
    m = {"timestamp": 1.0, "label": "B9",
         "libacars": {"arinc622": {"adsc": {"basic_report": {"lat": 32.1, "lon": 34.9}}}}}
    n = app._normalize_acars(m)
    assert (n["lat"], n["lon"], n["pos_src"]) == (32.1, 34.9, "adsc")
    assert n["group"] == "position"                    # מיקום => תמיד ירוק


def test_normalize_cpdlc_decoded_and_group():
    n = app._normalize_acars({"timestamp": 1.0, "libacars": {"cpdlc": {"msg": "CLIMB TO FL350"}}})
    assert n["category"] == "CPDLC" and n["group"] == "clearance"
    assert n["decoded"] == "CLIMB TO FL350"


def test_normalize_rejects_bad_latlon():
    assert app._normalize_acars({"timestamp": 1.0, "libacars": {"x": {"lat": 0, "lon": 0}}})["lat"] is None
    assert app._normalize_acars({"timestamp": 1.0, "libacars": {"x": {"lat": 999, "lon": 34}}})["lat"] is None


def test_normalize_position_from_text():
    n = app._normalize_acars({"timestamp": 1.0, "text": "POS N3206.0E03450.0 FL350"})
    assert n["pos_src"] == "text"
    assert 31 < n["lat"] < 33 and 34 < n["lon"] < 35


def test_acars_direction_heuristic():
    # label מוכר: OOOI/position/login => downlink; BA (אישור מהקרקע) => uplink
    assert app._acars_direction("QA", None) == "downlink"
    assert app._acars_direction("H1", "HELLO") == "downlink"
    assert app._acars_direction("BA", None) == "uplink"
    # header ניתוב בתחילת הטקסט => uplink (גם בלי label מוכר)
    assert app._acars_direction("ZZ", ".ATSXCXA CLEARED TO...") == "uplink"
    assert app._acars_direction(None, "/TLVATYA WX REPORT") == "uplink"
    # עמום => None (לא מנחשים)
    assert app._acars_direction("Q0", "PING") is None
    assert app._acars_direction(None, "JUST SOME TEXT") is None


def test_normalize_includes_direction():
    n = app._normalize_acars({"timestamp": 1.0, "label": "QA", "tail": "4X-A"})
    assert n["dir"] == "downlink"
    n2 = app._normalize_acars({"timestamp": 1.0, "label": "BA", "tail": "4X-A"})
    assert n2["dir"] == "uplink"
    assert app._normalize_acars({"timestamp": 1.0, "label": "Q0"})["dir"] is None


def test_text_latlon_rejects_noise_and_parses_arinc():
    assert app._text_latlon("FUEL 5678 KG PART N1278E56789") is None   # דקות 78>59 => נדחה
    assert app._text_latlon("CODE N32E034") is None                    # בלי DDMM מלא => נדחה
    lat, lon = app._text_latlon("POS S3206.5W03450.0 FL350")           # דרום/מערב
    assert -33 < lat < -31 and -35 < lon < -34


def test_text_latlon_comma_separated():
    """פורמט H1 של B737: N3203.7,E03455.7 (פסיק בין lat ל-lon, בלי רווח)."""
    lat, lon = app._text_latlon("10.17.24,DC,3201,01167,155.6,.240,024.0,027.5,N3203.7,E03455.7,138480")
    assert abs(lat - 32.0617) < 0.001 and abs(lon - 34.9283) < 0.001


def test_text_latlon_compact_no_decimal():
    """פורמט קומפקטי ללא נקודה עשרונית: N32042E034560 = N32°04.2' E034°56.0'."""
    # embedded mid-string with trailing digits (D56A style)
    lat, lon = app._text_latlon("N32042E03456010170135P238245007GXXXX22000HAB,")
    assert abs(lat - 32.07) < 0.001 and abs(lon - 34.9333) < 0.001
    # POS prefix (F50A style)
    lat, lon = app._text_latlon("POSN32010E034540,RW21,103458,2,1000,,GEMDA")
    assert abs(lat - 32.0167) < 0.001 and abs(lon - 34.9) < 0.001
    # FPON prefix, 3-digit degree lon (F00A style — Turkish airspace)
    lat, lon = app._text_latlon("LTAA.AFN/FMHIGT1166,.4L-GIT,,062600/FPON39428E038506,1/FCOADS")
    assert abs(lat - 39.7133) < 0.001 and abs(lon - 38.8433) < 0.001
    # SQ logon must NOT extract (ground-station address, not aircraft position)
    assert app._text_latlon("02XSTLVLLBG03200N03452EV136975/") is None


# --- ייצוא ------------------------------------------------------------------

def test_acars_export_csv(client, paths):
    app.ACARS_LOG_PATH.write_text(
        json.dumps({"t": 2.0, "freq": 131.55, "tail": "4X-B", "category": "ADS-C",
                    "group": "position", "dir": "downlink", "lat": 32.1, "lon": 34.9,
                    "text": "line1\nline2"}) + "\n"
        + json.dumps({"t": 1.0, "freq": 131.72, "tail": "4X-A", "category": "Label H1"}) + "\n")
    r = client.get("/api/acars/export?format=csv")
    assert r.status_code == 200
    assert r.headers["Content-Disposition"].startswith("attachment;")
    body = r.data.decode("utf-8-sig")                  # מתעלם מ-BOM
    lines = body.splitlines()
    assert lines[0].startswith("time_iso,timestamp,freq")
    assert "dir" in lines[0].split(",")                # עמודת כיוון בייצוא
    assert "4X-A" in lines[1] and "4X-B" in lines[2]   # ממוין לפי t עולה
    assert len(lines) == 3                             # newline בטקסט לא שובר שורה
    assert "line1 line2" in body


def test_acars_export_json(client, paths):
    app.ACARS_LOG_PATH.write_text(
        json.dumps({"t": 2.0, "tail": "B"}) + "\n" + json.dumps({"t": 1.0, "tail": "A"}) + "\n")
    data = json.loads(client.get("/api/acars/export?format=json").data)
    assert [d["tail"] for d in data] == ["A", "B"]     # ממוין לפי t
