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


# --- חלונות שעות פר-רגל (active_from/active_to) ------------------------------

def _at(monkeypatch, hh, mm=0):
    """קובע את time.localtime() לשעה נתונה (שאר השדות לא רלוונטיים לבדיקת חלון)."""
    fixed = time.struct_time((2026, 7, 6, hh, mm, 0, 0, 187, -1))
    monkeypatch.setattr(app.time, "localtime", lambda *a: fixed)


def test_leg_active_now_no_window_always_true(monkeypatch):
    _at(monkeypatch, 3)
    assert app._leg_active_now({"mode": "acars", "dwell_sec": 60}) is True


def test_leg_active_now_within_window(monkeypatch):
    leg = {"mode": "acars", "dwell_sec": 60, "active_from": "06:00", "active_to": "22:00"}
    _at(monkeypatch, 12, 30)
    assert app._leg_active_now(leg) is True


def test_leg_active_now_outside_window(monkeypatch):
    leg = {"mode": "acars", "dwell_sec": 60, "active_from": "06:00", "active_to": "22:00"}
    _at(monkeypatch, 23, 0)
    assert app._leg_active_now(leg) is False


def test_leg_active_now_overnight_wraparound(monkeypatch):
    leg = {"mode": "acars", "dwell_sec": 60, "active_from": "22:00", "active_to": "06:00"}
    _at(monkeypatch, 23, 30)
    assert app._leg_active_now(leg) is True
    _at(monkeypatch, 2, 0)
    assert app._leg_active_now(leg) is True
    _at(monkeypatch, 12, 0)
    assert app._leg_active_now(leg) is False


def test_validate_scan_plan_accepts_valid_window():
    leg = {"mode": "acars", "dwell_sec": 60, "active_from": "06:00", "active_to": "22:00"}
    plan = app._validate_scan_plan([leg])
    assert plan == [leg]


def test_validate_scan_plan_rejects_bad_window_format():
    assert app._validate_scan_plan([{"mode": "acars", "dwell_sec": 60,
                                     "active_from": "25:00", "active_to": "06:00"}]) is None
    assert app._validate_scan_plan([{"mode": "acars", "dwell_sec": 60,
                                     "active_from": "9:00", "active_to": "06:00"}]) is None


def test_validate_scan_plan_rejects_partial_window():
    assert app._validate_scan_plan([{"mode": "acars", "dwell_sec": 60,
                                     "active_from": "06:00"}]) is None
    assert app._validate_scan_plan([{"mode": "acars", "dwell_sec": 60,
                                     "active_to": "22:00"}]) is None


# --- scan activate/loop עם חלונות שעות ---------------------------------------

def test_scan_activate_skips_to_active_leg(paths, monkeypatch):
    # בלי no_sleep בכוונה: VDL2_LEG.dwell_sec=120 אמיתי => ה-thread לא מספיק
    # להתקדם לרגל הבאה (ACARS, לא בחלון) לפני שהבדיקה מסתיימת — לא תלוי-תזמון.
    monkeypatch.setattr(app, "_sysctl", lambda action, svc, timeout=45: _ok())
    monkeypatch.setattr(app, "_is_active", lambda svc: True)
    monkeypatch.setattr(app, "_leg_active_now", lambda leg: leg["mode"] == "vdl2")
    err, detail = app._scan_activate([ACARS_LEG, VDL2_LEG])
    assert err is None
    assert app._scan_status["idx"] == 1 and app._scan_status["leg"] == VDL2_LEG


def test_scan_activate_waits_when_no_leg_active(paths, no_sleep, monkeypatch):
    calls = []
    monkeypatch.setattr(app, "_sysctl",
                        lambda action, svc, timeout=45: calls.append((action, svc)) or _ok())
    monkeypatch.setattr(app, "_leg_active_now", lambda leg: False)
    err, detail = app._scan_activate([ACARS_LEG, VDL2_LEG])
    assert err is None
    assert calls == []                     # לא ניסינו להיכנס לשום רגל
    assert app._scan_status["idx"] == -1 and app._scan_status["leg"] is None
    assert app._scan_thread is not None and app._scan_thread.is_alive()


def test_scan_loop_skips_leg_outside_window(paths, no_sleep):
    calls = []

    def fake_enter(leg):
        calls.append(leg["mode"])
        return None, None

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(app, "_scan_enter_leg", fake_enter)
        mp.setattr(app, "_leg_active_now", lambda leg: leg["mode"] == "vdl2")
        stop_evt = threading.Event()
        plan = [ACARS_LEG, VDL2_LEG]
        th = threading.Thread(target=app._scan_loop, args=(stop_evt, plan, 0, 0), daemon=True)
        th.start()
        time.sleep(0.2)
        stop_evt.set()
        th.join(timeout=5)
    assert "acars" not in calls            # אף פעם לא בחלון => אף פעם לא נכנסת
    assert "vdl2" in calls


def test_api_state_scan_waiting_for_window_not_fault(client, paths, no_sleep, monkeypatch):
    leg = {"mode": "acars", "dwell_sec": 60, "active_from": "02:00", "active_to": "03:00"}
    app.save_state({**app.DEFAULT_STATE, "app_mode": "scan", "scan_plan": [leg]})
    monkeypatch.setattr(app, "_is_active", lambda svc: False)   # אין צרכן חי
    _at(monkeypatch, 12, 0)                                    # מחוץ לחלון
    st = client.get("/api/state").get_json()
    assert st["app_mode"] == "scan" and st["mode_ok"] is True   # "ממתין", לא תקלה


def test_api_health_scan_waiting_for_window_not_fault(client, paths, no_sleep, monkeypatch):
    leg = {"mode": "acars", "dwell_sec": 60, "active_from": "02:00", "active_to": "03:00"}
    app.save_state({**app.DEFAULT_STATE, "app_mode": "scan", "scan_plan": [leg]})
    _at(monkeypatch, 12, 0)

    def fake_run(cmd, **kw):
        return types.SimpleNamespace(stdout="inactive", stderr="")
    monkeypatch.setattr(app.subprocess, "run", fake_run)
    h = client.get("/api/health").get_json()
    assert h["app_mode"] == "scan" and h["ok"] is True


# --- באג: חלון שנסגר באמצע סבב חייב לכבות את הצרכן הרץ בפועל, לא רק את החיווי ----

def test_scan_loop_turns_off_consumer_when_window_closes_mid_scan(paths, no_sleep, monkeypatch):
    """רגל יחידה עם חלון שעות: אחרי שה-dwell מסתיים ואף רגל לא בחלון (כולל היא
    עצמה), הצרכן שכבר רץ (consumer_active=True, כאילו _scan_activate הכניס אותו)
    חייב להיכבות בפועל — לא רק להיעלם מ-_scan_status."""
    standby_calls = []
    monkeypatch.setattr(app, "_enter_standby", lambda: standby_calls.append(1) or (None, None))
    monkeypatch.setattr(app, "_leg_active_now", lambda leg: False)   # אף רגל לא בחלון, אף פעם
    stop_evt = threading.Event()
    leg = {"mode": "acars", "dwell_sec": 60, "active_from": "08:00", "active_to": "20:00"}
    plan = [leg]
    # start_idx=0, first_dwell=0, consumer_active=True: כאילו leg כבר הוכנס ע"י
    # _scan_activate (הרגל שרצה כשה-thread התחיל).
    th = threading.Thread(target=app._scan_loop, args=(stop_evt, plan, 0, 0, True), daemon=True)
    th.start()
    time.sleep(0.2)
    stop_evt.set()
    th.join(timeout=5)
    assert standby_calls, "הצרכן הרץ היה אמור להיכבות כשאף רגל לא בחלון"


def test_scan_loop_no_standby_when_nothing_was_running(paths, no_sleep, monkeypatch):
    """אם אף רגל מעולם לא נכנסה (consumer_active=False, כמו ב-_scan_activate
    כשאף רגל לא הייתה בחלון מלכתחילה) — אין צורך לכבות שום דבר."""
    standby_calls = []
    monkeypatch.setattr(app, "_enter_standby", lambda: standby_calls.append(1) or (None, None))
    monkeypatch.setattr(app, "_leg_active_now", lambda leg: False)
    stop_evt = threading.Event()
    plan = [{"mode": "acars", "dwell_sec": 60, "active_from": "08:00", "active_to": "20:00"}]
    th = threading.Thread(target=app._scan_loop, args=(stop_evt, plan, 0, 0, False), daemon=True)
    th.start()
    time.sleep(0.2)
    stop_evt.set()
    th.join(timeout=5)
    assert standby_calls == []


def test_scan_loop_does_not_restart_identical_leg_each_dwell(paths, no_sleep, monkeypatch):
    """לוח עם רגל יחידה (או רגלים חוזרות): כניסה חוזרת לאותה רגל בדיוק (מצב+
    תדרים) לא אמורה לגרום ל-restart מיותר של השירות בכל dwell."""
    calls = []

    def fake_enter(leg):
        calls.append(leg["mode"])
        return None, None

    monkeypatch.setattr(app, "_scan_enter_leg", fake_enter)
    stop_evt = threading.Event()
    leg = {"mode": "voice", "dwell_sec": 60}
    plan = [leg]
    th = threading.Thread(target=app._scan_loop, args=(stop_evt, plan, 1, 60, True), daemon=True)
    th.start()
    time.sleep(0.2)
    stop_evt.set()
    th.join(timeout=5)
    # last_entered אותחל מ-plan[(1-1)%1]=plan[0]=leg (consumer_active=True) => כל
    # מחזור מזהה "אותה רגל" ולא קורא ל-_scan_enter_leg בכלל.
    assert calls == []


# --- באג: /api/mode לא אמור לגעת בסבב פעיל אם הבקשה תיכשל בולידציה --------------

def test_api_mode_invalid_new_plan_does_not_stop_running_scan(client, paths, no_sleep, monkeypatch):
    monkeypatch.setattr(app, "_sysctl", lambda action, svc, timeout=45: _ok())
    monkeypatch.setattr(app, "_is_active", lambda svc: True)
    client.post("/api/mode", json={"mode": "scan", "plan": [ACARS_LEG, VDL2_LEG]})
    thread_before = app._scan_thread
    assert thread_before is not None and thread_before.is_alive()
    r = client.post("/api/mode", json={"mode": "scan", "plan": [{"mode": "acars", "dwell_sec": 1}]})
    assert r.status_code == 400
    # הסבב הקודם (התקין) לא נגע בו כלל — אותו thread עדיין רץ
    assert app._scan_thread is thread_before and app._scan_thread.is_alive()
    assert app.load_state()["app_mode"] == "scan"


def test_api_mode_tune_lock_busy_does_not_stop_running_scan(client, paths, monkeypatch):
    """כש-TUNE_LOCK תפוס ע"י פעולה אחרת (409), סבב סריקה פעיל לא אמור להיעצר —
    אחרת נשאר "scan זומבי": state עדיין scan אבל אין thread שממשיך אותו.
    בלי no_sleep בכוונה (כמו test_scan_activate_skips_to_active_leg): dwell
    אמיתי (60ש') => thread הסריקה יושב בהמתנה ולא מתחרה על TUNE_LOCK, כך
    שתפיסת הנעילה כאן דטרמיניסטית ולא תלוית-תזמון."""
    monkeypatch.setattr(app, "_sysctl", lambda action, svc, timeout=45: _ok())
    monkeypatch.setattr(app, "_is_active", lambda svc: True)
    client.post("/api/mode", json={"mode": "scan", "plan": [ACARS_LEG, VDL2_LEG]})
    thread_before = app._scan_thread
    assert thread_before is not None and thread_before.is_alive()
    assert app.TUNE_LOCK.acquire(blocking=False)   # מדמה פעולה מתמשכת אחרת
    try:
        r = client.post("/api/mode", json={"mode": "acars", "freqs": ["131.525"]})
        assert r.status_code == 409
    finally:
        app.TUNE_LOCK.release()
    assert app._scan_thread is thread_before and app._scan_thread.is_alive()
    assert app.load_state()["app_mode"] == "scan"


# --- באג: active_from==active_to אמור להיות "תמיד פעיל", לא "אף פעם" -----------

def test_leg_active_now_from_equals_to_means_always(monkeypatch):
    leg = {"mode": "acars", "dwell_sec": 60, "active_from": "09:00", "active_to": "09:00"}
    _at(monkeypatch, 3, 0)
    assert app._leg_active_now(leg) is True
    _at(monkeypatch, 23, 59)
    assert app._leg_active_now(leg) is True


# --- באג: active_from/active_to שאינם מחרוזת (JSON תקין) לא אמורים לזרוק 500 ----

def test_validate_scan_plan_rejects_non_string_window_fields():
    assert app._validate_scan_plan([{"mode": "acars", "dwell_sec": 60,
                                     "active_from": 900, "active_to": "22:00"}]) is None
    assert app._validate_scan_plan([{"mode": "acars", "dwell_sec": 60,
                                     "active_from": ["06", "00"], "active_to": "22:00"}]) is None
