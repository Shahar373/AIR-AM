# ============================================================================
#  AIR-AM - עמידות המצבים של שני מקלטים (ביקורת 17.11.2026)
# ----------------------------------------------------------------------------
#  כל בדיקה כאן שחזרה באג אמיתי בביקורת הקוד של v2.35.0 (נכשלה לפני התיקון):
#  reconcile שמחזיק TUNE_LOCK על משני מנותק, כיוונון שמוציא מהמצב הכפול כשהמשני
#  ב-activating, משני "רוח רפאים", מקלט אחד מתוך שניים חסר ⇒ off, ראשי שנפל הורג
#  משני עובד, תדר זהה בשני הערוצים, החלפת תפקידים באמצע מצב כפול, ועוד.
# ============================================================================
import threading
import time

import pytest

import app
from dual_env import A, B, ORIGIN, _params, _roles, env, env2  # noqa: F401  (fixtures נטענים לפי שם)


def _activating(monkeypatch, env, units):
    """יחידות ב-"activating" (השער / sleep המדורג / RestartSec): לא active, אבל systemd מרים אותן."""
    monkeypatch.setattr(app, "_services_status",
                        lambda names: {n: ("active" if n in env.active or n in ("icecast2", "sdrplay")
                                           else "activating" if n in units else "inactive")
                                       for n in names})


def _only_one_receiver(monkeypatch, present_serial):
    """שני תפקידים קבועים, מקלט אחד ב-USB — ה-API מציע רק אותו."""
    monkeypatch.setattr(app, "_sdr_usb_full", lambda: (True, "SDRplay", 1))
    monkeypatch.setattr(app, "_serial_on_api", lambda serial: serial == present_serial)


# --- 1. reconcile לא חוסם את המשתמש בגלל משני מנותק ---------------------------

def test_reconcile_skips_unplugged_secondary_without_lock(env, monkeypatch):
    _roles()
    app._merge_state({"app_mode": "voice_acars", "acars_freqs": ["131.550"]})
    env.active = {"rtl_airband"}
    _only_one_receiver(monkeypatch, B)                    # המשני (A) לא מחובר
    calls = []
    monkeypatch.setattr(app, "_start_secondary", lambda *a: calls.append(a) or ("x", None))
    app._mode_reconcile_once()
    assert not calls
    assert app.TUNE_LOCK.acquire(blocking=False)          # לא נשארה נעולה
    app.TUNE_LOCK.release()


def test_reconcile_leaves_activating_secondary_alone(env, monkeypatch):
    _roles()
    app._merge_state({"app_mode": "voice2"})
    env.active = {"rtl_airband"}
    _activating(monkeypatch, env, {"rtl_airband2"})
    env.log.clear()
    app._mode_reconcile_once()
    assert not [e for e in env.log if e[0] == "restart"]


def test_reconcile_still_restarts_dead_present_secondary(env):
    _roles()
    app._merge_state({"app_mode": "voice_acars", "acars_freqs": ["131.550"]})
    env.active = {"rtl_airband"}
    app._mode_reconcile_once()
    assert env.active == {"rtl_airband", "airam-acars2"}


# --- 2+3. כיוונון לפי הכוונה, ועצירת משני שעוד לא סיים לעלות ------------------

def test_tune_keeps_composite_while_secondary_activating(env, monkeypatch):
    _roles()
    app._merge_state({"app_mode": "voice_acars", "acars_freqs": ["131.550"]})
    env.active = {"rtl_airband"}
    _activating(monkeypatch, env, {"airam-acars2"})
    r = app.app.test_client().post("/api/tune", json={"freq": 120.5}, headers=ORIGIN)
    assert r.status_code == 200 and r.get_json()["app_mode"] == "voice_acars"
    assert app.load_state()["app_mode"] == "voice_acars"
    assert ("stop", "airam-acars2") not in [(e, u) for e, u, _t in env.log]


def test_mode_voice_stops_activating_secondary(env):
    _roles()
    app._merge_state({"app_mode": "voice2"})
    env.active = {"rtl_airband"}                          # rtl_airband2 ב-activating (לא active)
    r = app.app.test_client().post("/api/mode", json={"mode": "voice"}, headers=ORIGIN)
    assert r.status_code == 200
    stops = {u for e, u, _t in env.log if e == "stop"}
    assert {"rtl_airband2", "airam-acars2"} <= stops     # בלי תנאי is-active


# --- 4. מקלט אחד מתוך שניים חסר ⇒ לא off --------------------------------------

def test_boot_secondary_missing_keeps_voice_and_intent(env, monkeypatch):
    _roles()
    app._merge_state({"app_mode": "voice_acars", "acars_freqs": ["131.550"]})
    env.fail = {"airam-acars2"}
    monkeypatch.setattr(app, "_config_stale", lambda: False)
    app._boot_restore()
    assert app.load_state()["app_mode"] == "voice_acars"
    assert "rtl_airband" in env.active                    # הקול לא נהרס


def test_tune_with_pinned_primary_missing_keeps_intent(env, monkeypatch):
    _roles()
    app._merge_state({"app_mode": "voice", "freq": 132.5})
    _only_one_receiver(monkeypatch, A)                    # רק המשני מחובר
    monkeypatch.setattr(app, "_restart_and_verify",
                        lambda: ("restart failed", "", not app._role_present("primary")))
    rolled = []
    monkeypatch.setattr(app, "_rollback", lambda prev: rolled.append(1) or False)
    r = app.app.test_client().post("/api/tune", json={"freq": 120.5}, headers=ORIGIN)
    assert r.status_code == 500
    assert app.load_state()["app_mode"] == "voice"        # לא off
    assert not rolled                                     # בלי רולבק שנדון לאותו כשל


def test_role_present_logic(env, monkeypatch):
    _roles()
    _only_one_receiver(monkeypatch, A)
    assert app._role_present("primary") is False and app._role_present("secondary") is True
    monkeypatch.setattr(app, "_sdr_usb_full", lambda: (True, "SDRplay", 2))
    assert app._role_present("primary") is True           # שניים ב-USB — לא שואלים את ה-API
    _roles(B, None)                                       # תפקיד אחד — כמו תמיד
    monkeypatch.setattr(app, "_sdr_usb_full", lambda: (True, "SDRplay", 1))
    assert app._role_present("primary") is True
    monkeypatch.setattr(app, "_sdr_present", lambda: False)
    assert app._role_present("primary") is False


# --- 5. ראשי שנותק לא הורג את המשני העובד -------------------------------------

def test_reconcile_primary_missing_keeps_secondary(env, monkeypatch):
    _roles()
    app._merge_state({"app_mode": "voice_acars", "acars_freqs": ["131.550"]})
    env.active = {"airam-acars2"}
    _only_one_receiver(monkeypatch, A)
    env.log.clear()
    app._mode_reconcile_once()
    assert env.active == {"airam-acars2"} and not env.log


def test_reconcile_primary_dead_present_restarts_only_primary(env):
    _roles()
    app._merge_state({"app_mode": "voice_acars", "acars_freqs": ["131.550"]})
    env.active = {"airam-acars2"}
    env.log.clear()
    app._mode_reconcile_once()
    assert env.active == {"rtl_airband", "airam-acars2"}
    assert ("stop", "airam-acars2") not in [(e, u) for e, u, _t in env.log]


# --- 6. אותו תדר בשני הערוצים --------------------------------------------------

def test_tune_primary_to_voice2_freq_rejected(env2):
    _roles()
    app._merge_state({"app_mode": "voice2", "voice2_freq": 121.4, "freq": 132.5})
    env2.active = {"rtl_airband", "rtl_airband2"}
    r = app.app.test_client().post("/api/tune", json={"freq": 121.4}, headers=ORIGIN)
    assert r.status_code == 400
    assert app.load_state()["freq"] == 132.5


# --- 7. החלפת תפקידים באמצע מצב כפול -------------------------------------------

def test_roles_change_refused_in_composite(env):
    _roles(B, A)
    app._merge_state({"app_mode": "voice_acars"})
    env.active = {"rtl_airband", "airam-acars2"}
    r = app.app.test_client().post("/api/sdr/roles", json={"primary": A, "secondary": B}, headers=ORIGIN)
    assert r.status_code == 409
    st = app.load_state()
    assert st["sdr_primary"] == B and st["sdr_secondary"] == A


def test_roles_change_allowed_in_single_mode(env):
    _roles(B, None)
    app._merge_state({"app_mode": "voice"})
    r = app.app.test_client().post("/api/sdr/roles", json={"secondary": A}, headers=ORIGIN)
    assert r.status_code == 200 and app.load_state()["sdr_secondary"] == A


def test_roles_change_409_while_tune_lock_held(env):
    assert app.TUNE_LOCK.acquire(blocking=False)
    try:
        r = app.app.test_client().post("/api/sdr/roles", json={"primary": B}, headers=ORIGIN)
        assert r.status_code == 409
    finally:
        app.TUNE_LOCK.release()


# --- 8. מסלולי המשני בלי תפקידים -----------------------------------------------

def test_start_secondary_refuses_without_roles(env2):
    _roles(B, None)
    err, _d = app._start_secondary("voice2", app.load_state())
    assert err and not env2.active
    assert not app.VOICE2_CONFIG_PATH.exists()            # שום קונפיג "כל מקלט"


# --- 9. כשל במשני בלבד מחזיר את הקונפיג הקודם ----------------------------------

def test_mode_voice2_secondary_only_failure_restores_config(env2, monkeypatch):
    _roles()
    app._merge_state({"app_mode": "voice2", "voice2_freq": 121.4, "freq": 132.5})
    env2.active = {"rtl_airband", "rtl_airband2"}
    real = env2.sysctl
    n = {"restart": 0}

    def sysctl(action, svc, timeout=45):
        if action == "restart" and svc == "rtl_airband2":
            n["restart"] += 1
            if n["restart"] == 1:                         # התדר החדש נכשל, החזרה עובדת
                env2.active.discard(svc)
                return type("R", (), {"returncode": 1, "stderr": "bad", "stdout": ""})()
        return real(action, svc, timeout)
    monkeypatch.setattr(app, "_sysctl", sysctl)
    r = app.app.test_client().post("/api/mode", json={"mode": "voice2", "freq2": 119.1}, headers=ORIGIN)
    assert r.status_code == 500 and "rtl_airband" in env2.active
    assert "freq = 121.4000" in app.VOICE2_CONFIG_PATH.read_text()
    assert app.load_state()["voice2_freq"] == 121.4


def test_mode_voice2_same_params_is_noop(env2):
    _roles()
    app._merge_state({"app_mode": "voice2", "voice2_freq": 121.4, "voice2_mod": "am"})
    env2.active = {"rtl_airband", "rtl_airband2"}
    env2.log.clear()
    r = app.app.test_client().post("/api/mode", json={"mode": "voice2"}, headers=ORIGIN)
    assert r.status_code == 200 and not env2.log           # לחיצה על הכרטיס החי — בלי restart


def test_mode_voice_acars_same_bank_is_noop(env):
    _roles()
    app._merge_state({"app_mode": "voice_acars", "acars_freqs": ["131.550"]})
    env.active = {"rtl_airband", "airam-acars2"}
    env.log.clear()
    r = app.app.test_client().post("/api/mode", json={"mode": "voice_acars", "freqs": ["131.550"]},
                                   headers=ORIGIN)
    assert r.status_code == 200 and not env.log


# --- 10. הדירוג לא נופל לקובץ ה-stats של התהליך הקודם ---------------------------

def test_stagger_uses_new_process_start_time(env, monkeypatch):
    _roles()
    waited = []
    monkeypatch.setattr(app, "_wait_voice_settled", lambda since: waited.append(since))
    t_start = time.time() + 1000                          # התהליך החדש "הופעל" אחרי since
    monkeypatch.setattr(app, "_rtl_airband_start_wall", lambda: t_start)
    app._enter_voice_acars(_params(), ["131.550"])
    assert waited == [t_start]


# --- boot: משני בדרך (activating) לא גורם לכניסה מלאה ---------------------------

def test_boot_restore_with_secondary_activating_is_noop(env, monkeypatch):
    _roles()
    app._merge_state({"app_mode": "voice2"})
    env.active = {"rtl_airband"}
    _activating(monkeypatch, env, {"rtl_airband2"})
    monkeypatch.setattr(app, "_config_stale", lambda: False)
    env.log.clear()
    app._boot_restore()
    assert not [e for e in env.log if e[0] in ("restart", "stop")]


def test_sdr_holder_reports_composite_mode():
    h = app._sdr_holder({"rtl_airband": "active", "rtl_airband2": "activating"})
    assert h == ("voice2", "active")
    assert app._sdr_holder({"rtl_airband": "active"}) == ("voice", "active")
    assert app._sdr_holder({"airam-acars2": "active"})[0] == "voice_acars"
