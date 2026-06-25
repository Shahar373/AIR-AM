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
