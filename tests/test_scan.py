# בדיקות מצב הסריקה/סבב (scan): מחזור אוטומטי בין המצבים לפי לוח זמנים.
# המצבים שווי-מעמד: כשל בכניסה לרגל הראשונה נופל ל-off (בלי fallback לקול),
# וכשל של *כל* הרגלים ברצף (סבב שלם) נופל ל-off גם הוא. systemd/SDR ממוקפים.
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
    monkeypatch.setattr(app, "VDL2_ENV_PATH", tmp_path / "vdl2.env")
    return tmp_path


@pytest.fixture
def client(paths):
    return app.app.test_client()


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(app.time, "sleep", lambda *a, **k: None)


def _ok(**kw):
    return types.SimpleNamespace(returncode=0, stdout="", stderr="", **kw)


def _mk_is_active(calls):
    """is_active לפי הפעולה האחרונה שנרשמה על השירות — כדי ש-stop/restart על
    אותו שירות ישקפו מצב אמיתי (במקום True/False קבוע שמטעה את _enter_standby)."""
    def is_active(svc):
        for action, s in reversed(calls):
            if s == svc:
                return action == "restart"
        return False
    return is_active


@pytest.fixture(autouse=True)
def _stop_leftover_scan():
    """מבטיח שאף thread סריקה לא דולף בין בדיקות (גם אם בדיקה נכשלת באמצע)."""
    yield
    app._scan_stop_thread()


ACARS_LEG = {"mode": "acars", "dwell_sec": 60}
VDL2_LEG = {"mode": "vdl2", "dwell_sec": 60}


# --- _validate_scan_plan -----------------------------------------------------

def test_validate_scan_plan_valid():
    plan = app._validate_scan_plan([ACARS_LEG, VDL2_LEG])
    assert plan == [ACARS_LEG, VDL2_LEG]


def test_validate_scan_plan_rejects_non_list():
    assert app._validate_scan_plan(None) is None
    assert app._validate_scan_plan({"mode": "acars"}) is None


def test_validate_scan_plan_rejects_empty():
    assert app._validate_scan_plan([]) is None


def test_validate_scan_plan_rejects_too_many_legs():
    assert app._validate_scan_plan([ACARS_LEG] * (app.SCAN_LEGS_MAX + 1)) is None


def test_validate_scan_plan_rejects_bad_mode():
    assert app._validate_scan_plan([{"mode": "off", "dwell_sec": 60}]) is None
    assert app._validate_scan_plan([{"mode": "scan", "dwell_sec": 60}]) is None


def test_validate_scan_plan_rejects_dwell_out_of_range():
    assert app._validate_scan_plan([{"mode": "acars", "dwell_sec": app.SCAN_DWELL_MIN - 1}]) is None
    assert app._validate_scan_plan([{"mode": "acars", "dwell_sec": app.SCAN_DWELL_MAX + 1}]) is None


def test_validate_scan_plan_rejects_bad_dwell_type():
    assert app._validate_scan_plan([{"mode": "acars", "dwell_sec": "soon"}]) is None


def test_validate_scan_plan_rejects_wide_window():
    wide = {"mode": "acars", "dwell_sec": 60,
            "freqs": ["131.550", "136.900"]}    # span > ACARS_WINDOW_MHZ
    assert app._validate_scan_plan([wide]) is None


def test_validate_scan_plan_normalizes_freqs():
    leg = {"mode": "vdl2", "dwell_sec": 30, "freqs": ["136.975"]}
    plan = app._validate_scan_plan([leg])
    assert plan[0]["freqs"] == ["136.975"]


# --- /api/mode: כניסה לסריקה --------------------------------------------------

def test_api_mode_enter_scan_starts_leg0_and_thread(client, paths, no_sleep, monkeypatch):
    calls = []
    monkeypatch.setattr(app, "_sysctl",
                        lambda action, svc, timeout=45: calls.append((action, svc)) or _ok())
    monkeypatch.setattr(app, "_is_active", lambda svc: True)
    r = client.post("/api/mode", json={"mode": "scan", "plan": [ACARS_LEG, VDL2_LEG]})
    j = r.get_json()
    assert r.status_code == 200 and j["ok"] and j["app_mode"] == "scan"
    assert j["scan_plan"] == [ACARS_LEG, VDL2_LEG]
    assert app.load_state()["app_mode"] == "scan"
    assert app.load_state()["scan_plan"] == [ACARS_LEG, VDL2_LEG]
    # רגל 0 (acars) הוכנסה סינכרונית — כמו שאר ה-_enter_* (משוב מיידי)
    assert ("restart", app.ACARS_SERVICE) in calls
    # thread הסבב רץ ברקע לשאר הלוח
    assert app._scan_thread is not None and app._scan_thread.is_alive()
    status = client.get("/api/scan").get_json()
    assert status["ok"] and status["active"] is True
    assert status["idx"] == 0 and status["leg"] == ACARS_LEG


def test_api_mode_enter_scan_leg0_failure_falls_to_off(client, paths, no_sleep, monkeypatch):
    app.save_state({**app.DEFAULT_STATE, "app_mode": "acars"})
    monkeypatch.setattr(app, "_sysctl", lambda action, svc, timeout=45: _ok())
    monkeypatch.setattr(app, "_is_active", lambda svc: False)   # אף שירות לא עולה => כשל
    r = client.post("/api/mode", json={"mode": "scan", "plan": [ACARS_LEG]})
    assert r.status_code == 500
    body = r.get_json()
    assert body["app_mode"] == "off" and body["state"]["app_mode"] == "off"
    assert body["state"]["prev_mode"] == "acars"
    assert app.load_state()["app_mode"] == "off"
    assert app._scan_thread is None            # לא נשאר thread תלוי אחרי כשל


def test_api_mode_scan_rejects_invalid_plan(client, paths):
    r = client.post("/api/mode", json={"mode": "scan", "plan": []})
    assert r.status_code == 400
    assert app.load_state().get("app_mode") != "scan"


def test_api_mode_scan_uses_saved_plan_when_none_supplied(client, paths, no_sleep, monkeypatch):
    app.save_state({**app.DEFAULT_STATE, "app_mode": "off", "scan_plan": [VDL2_LEG]})
    monkeypatch.setattr(app, "_sysctl", lambda action, svc, timeout=45: _ok())
    monkeypatch.setattr(app, "_is_active", lambda svc: True)
    r = client.post("/api/mode", json={"mode": "scan"})
    assert r.get_json()["ok"] and r.get_json()["scan_plan"] == [VDL2_LEG]


def test_api_mode_switch_away_from_scan_stops_thread(client, paths, no_sleep, monkeypatch):
    calls = []
    monkeypatch.setattr(app, "_sysctl",
                        lambda action, svc, timeout=45: calls.append((action, svc)) or _ok())
    monkeypatch.setattr(app, "_is_active", _mk_is_active(calls))
    client.post("/api/mode", json={"mode": "scan", "plan": [ACARS_LEG, VDL2_LEG]})
    assert app._scan_thread is not None
    r = client.post("/api/mode", json={"mode": "off"})
    assert r.get_json()["ok"]
    assert app._scan_thread is None
    status = client.get("/api/scan").get_json()
    assert status["active"] is False


# --- api_state/api_health עם scan --------------------------------------------

def test_api_state_reports_scan_not_current_leg(client, paths, no_sleep, monkeypatch):
    # app_mode נשאר "scan" (לא "acars") גם כשהרגל הנוכחית שרצה בפועל היא acars —
    # הרגל עצמה מגיעה מ-/api/scan, לא מ-/api/state.
    monkeypatch.setattr(app, "_sysctl", lambda action, svc, timeout=45: _ok())
    monkeypatch.setattr(app, "_is_active", lambda svc: svc == app.ACARS_SERVICE)
    client.post("/api/mode", json={"mode": "scan", "plan": [ACARS_LEG]})
    st = client.get("/api/state").get_json()
    assert st["app_mode"] == "scan" and st["mode_ok"] is True


def test_api_health_scan_ok_reflects_live_consumer(client, paths, no_sleep, monkeypatch):
    monkeypatch.setattr(app, "_sysctl", lambda action, svc, timeout=45: _ok())
    monkeypatch.setattr(app, "_is_active", lambda svc: svc == app.ACARS_SERVICE)
    client.post("/api/mode", json={"mode": "scan", "plan": [ACARS_LEG]})

    def fake_run(cmd, **kw):
        svc = cmd[-1]
        active = svc == "airam-acars"
        return types.SimpleNamespace(stdout=("active" if active else "inactive"), stderr="")
    monkeypatch.setattr(app.subprocess, "run", fake_run)
    h = client.get("/api/health").get_json()
    assert h["app_mode"] == "scan" and h["ok"] is True


# --- _scan_loop: התנהגות פנימית של הסבב --------------------------------------

def test_scan_loop_all_legs_fail_falls_to_off(paths, no_sleep, monkeypatch):
    app.save_state({**app.DEFAULT_STATE, "app_mode": "scan"})
    monkeypatch.setattr(app, "_scan_enter_leg", lambda leg: ("נכשל", None))
    monkeypatch.setattr(app, "_sysctl", lambda action, svc, timeout=45: _ok())
    monkeypatch.setattr(app, "_is_active", lambda svc: False)
    stop_evt = threading.Event()
    plan = [ACARS_LEG, VDL2_LEG]
    app._scan_loop(stop_evt, plan, 0, 0)      # קורא ישירות (לא thread) — חסום עד שמסיים
    st = app.load_state()
    assert st["app_mode"] == "off" and st["prev_mode"] == "scan"


def test_scan_loop_skips_failed_leg_and_recovers(paths, no_sleep):
    calls = []

    def fake_enter(leg):
        calls.append(leg["mode"])
        if leg["mode"] == "vdl2" and calls.count("vdl2") == 1:
            return "נכשל פעם אחת", None      # רק הניסיון הראשון ל-vdl2 נכשל
        return None, None

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(app, "_scan_enter_leg", fake_enter)
        stop_evt = threading.Event()
        plan = [ACARS_LEG, VDL2_LEG]
        th = threading.Thread(target=app._scan_loop, args=(stop_evt, plan, 0, 0), daemon=True)
        th.start()
        time.sleep(0.2)                       # מספיק זמן אמיתי לכמה סבבים (dwell מדומה, בלי sleep)
        stop_evt.set()
        th.join(timeout=5)
    assert "vdl2" in calls                    # לא ננטש אחרי כשל אחד — ניסה שוב
    assert calls.count("vdl2") >= 2           # ניסיון ראשון (כשל) + לפחות ניסיון חוזר שהצליח


def test_scan_activate_starts_thread_on_success(paths, no_sleep, monkeypatch):
    monkeypatch.setattr(app, "_sysctl", lambda action, svc, timeout=45: _ok())
    monkeypatch.setattr(app, "_is_active", lambda svc: True)
    err, detail = app._scan_activate([ACARS_LEG, VDL2_LEG])
    assert err is None
    assert app._scan_thread is not None and app._scan_thread.is_alive()
    assert app._scan_status["idx"] == 0 and app._scan_status["leg"] == ACARS_LEG


def test_scan_activate_leg0_failure_no_thread(paths, no_sleep, monkeypatch):
    monkeypatch.setattr(app, "_sysctl", lambda action, svc, timeout=45: _ok())
    monkeypatch.setattr(app, "_is_active", lambda svc: False)
    err, detail = app._scan_activate([ACARS_LEG])
    assert err is not None
    assert app._scan_thread is None


# --- _boot_restore עם scan ----------------------------------------------------

def test_boot_restore_scan_enters_leg0(paths, no_sleep, monkeypatch):
    app.save_state({**app.DEFAULT_STATE, "app_mode": "scan", "scan_plan": [ACARS_LEG, VDL2_LEG]})
    calls = []
    monkeypatch.setattr(app, "_sysctl",
                        lambda action, svc, timeout=45: calls.append((action, svc)) or _ok())
    monkeypatch.setattr(app, "_sdr_present", lambda: True)
    monkeypatch.setattr(app, "_is_active", lambda svc: ("restart", svc) in calls)
    app._boot_restore()
    assert ("restart", app.ACARS_SERVICE) in calls
    assert app.load_state()["app_mode"] == "scan"
    assert app._scan_thread is not None and app._scan_thread.is_alive()


def test_boot_restore_scan_invalid_plan_falls_to_off(paths, no_sleep, monkeypatch):
    app.save_state({**app.DEFAULT_STATE, "app_mode": "scan", "scan_plan": []})
    monkeypatch.setattr(app, "_sdr_present", lambda: True)
    monkeypatch.setattr(app, "_is_active", lambda svc: False)
    app._boot_restore()
    st = app.load_state()
    assert st["app_mode"] == "off" and st["prev_mode"] == "scan"


def test_boot_restore_scan_leg0_failure_falls_to_off(paths, no_sleep, monkeypatch):
    app.save_state({**app.DEFAULT_STATE, "app_mode": "scan", "scan_plan": [ACARS_LEG]})
    monkeypatch.setattr(app, "_sysctl", lambda action, svc, timeout=45: _ok())
    monkeypatch.setattr(app, "_sdr_present", lambda: True)
    monkeypatch.setattr(app, "_is_active", lambda svc: False)
    app._boot_restore()
    st = app.load_state()
    assert st["app_mode"] == "off" and st["prev_mode"] == "scan"
