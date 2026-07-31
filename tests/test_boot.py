# בדיקות _boot_restore: אורקסטרציית האתחול של airam-web (המתזמר).
# אף צרכן SDR אינו enabled ב-systemd — airam-web משחזר את המצב השמור באתחול,
# כולל off. אלה הבדיקות שמעגנות את "אין מצב ראשי": השחזור סימטרי לכל המצבים,
# וכישלון נופל ל-off (לעולם לא לקול). כמו בשאר הבדיקות — systemd/SDR ממוקפים.
import types

import pytest

import app


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "CONFIG_PATH", tmp_path / "airband.conf")
    monkeypatch.setattr(app, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(app, "ACARS_ENV_PATH", tmp_path / "acars.env")
    monkeypatch.setattr(app, "VDL2_ENV_PATH", tmp_path / "vdl2.env")
    monkeypatch.setattr(app, "SATCOM_ENV_PATH", tmp_path / "satcom.env")
    return tmp_path


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(app.time, "sleep", lambda *a, **k: None)


def _ok(**kw):
    return types.SimpleNamespace(returncode=0, stdout="", stderr="", **kw)


@pytest.fixture
def sysctl_calls(monkeypatch):
    """מקליט קריאות systemctl; ברירת מחדל: SDR נוכח, אף שירות לא פעיל."""
    calls = []
    monkeypatch.setattr(app, "_sysctl",
                        lambda action, svc, timeout=45: calls.append((action, svc)) or _ok())
    monkeypatch.setattr(app, "_sdr_present", lambda: True)
    monkeypatch.setattr(app, "_is_active", lambda svc: False)
    return calls


def test_boot_restore_voice_restarts_rtl_airband(paths, no_sleep, sysctl_calls, monkeypatch):
    app.save_state({**app.DEFAULT_STATE, "freq": 121.5, "app_mode": "voice"})
    monkeypatch.setattr(app, "_restart_and_verify", lambda: (None, None, False))
    app._boot_restore()
    # הקונפיג נכתב מה-state (מכסה גם שדרוג stats_filepath/localtime)
    assert "freq = 121.5000;" in app.CONFIG_PATH.read_text()
    assert app.load_state()["app_mode"] == "voice"     # השחזור לא משנה את הכוונה


def test_boot_restore_acars_enters_acars(paths, no_sleep, sysctl_calls, monkeypatch):
    app.save_state({**app.DEFAULT_STATE, "app_mode": "acars", "acars_freqs": ["131.550"]})
    # אחרי reboot כלום לא רץ; השירות "נהיה פעיל" רק אחרי ה-restart של השחזור
    # (מדמה את המציאות וגם מספק את אימות הפולינג של _enter_acars)
    monkeypatch.setattr(app, "_is_active", lambda svc: ("restart", svc) in sysctl_calls)
    app._boot_restore()
    assert ("restart", app.ACARS_SERVICE) in sysctl_calls
    assert "ACARS_FREQS=131.550" in app.ACARS_ENV_PATH.read_text()
    assert app.load_state()["app_mode"] == "acars"


def test_boot_restore_vdl2_enters_vdl2(paths, no_sleep, sysctl_calls, monkeypatch):
    app.save_state({**app.DEFAULT_STATE, "app_mode": "vdl2", "vdl2_freqs": ["136.975"]})
    monkeypatch.setattr(app, "_is_active", lambda svc: ("restart", svc) in sysctl_calls)
    app._boot_restore()
    assert ("restart", app.VDL2_SERVICE) in sysctl_calls
    assert "VDL2_FREQS=136975000" in app.VDL2_ENV_PATH.read_text()   # MHz→Hz
    assert app.load_state()["app_mode"] == "vdl2"


def test_boot_restore_satcom_does_not_auto_enter(paths, no_sleep, sysctl_calls, monkeypatch):
    """בטיחות: satcom *לא* משוחזר אוטומטית באתחול — write_satcom_env מדליק
    bias-T (‎+4.7V על מחבר האנטנה) כברירת מחדל, ואחרי reboot אין בן-אדם
    בסביבה שיוודא איזו אנטנה מחוברת. נופל ל-off עם prev_mode=satcom (כפתור
    ⏻/כרטיס הבית יציעו כניסה ידנית עם אישור אנטנה) — בלי לקרוא ל-_enter_satcom
    בכלל. גם מוודא שלא נפלנו בטעות לענף ה-scan הגנרי (שהיה "מצליח" להגיע
    ל-off באותה תוצאה שטחית, אבל מהסיבה הלא-נכונה): _validate_scan_plan לא
    נקרא כלל עבור מצב satcom."""
    app.save_state({**app.DEFAULT_STATE, "app_mode": "satcom", "satcom_freqs": ["AF1"]})
    monkeypatch.setattr(app, "_is_active", lambda svc: ("restart", svc) in sysctl_calls)
    entered = []
    monkeypatch.setattr(app, "_enter_satcom", lambda freqs: entered.append(freqs) or (None, None))
    scan_validated = []
    monkeypatch.setattr(app, "_validate_scan_plan", lambda raw: scan_validated.append(raw) or None)
    app._boot_restore()
    assert entered == []
    assert scan_validated == []
    assert ("restart", app.SATCOM_SERVICE) not in sysctl_calls
    st = app.load_state()
    assert st["app_mode"] == "off"
    assert st["prev_mode"] == "satcom"


def test_boot_restore_off_is_noop(paths, no_sleep, sysctl_calls):
    # off שורד reboot: אחרי אתחול כלום לא רץ => אין שום קריאת systemctl
    app.save_state({**app.DEFAULT_STATE, "app_mode": "off"})
    app._boot_restore()
    assert sysctl_calls == []
    assert app.load_state()["app_mode"] == "off"


def test_boot_restore_off_stops_leftover_consumer(paths, no_sleep, sysctl_calls, monkeypatch):
    # state אומר off אבל צרכן רץ (restart של airam-web באמצע אי-התאמה) => עוצרים
    app.save_state({**app.DEFAULT_STATE, "app_mode": "off"})
    monkeypatch.setattr(app, "_is_active", lambda svc: svc == "rtl_airband")
    app._boot_restore()
    assert ("stop", "rtl_airband") in sysctl_calls


def test_boot_restore_skips_when_consumer_already_running(paths, no_sleep, sysctl_calls, monkeypatch):
    # restart של airam-web באמצע סשן: הצרכן השמור כבר רץ => no-op (לא מפריעים לקליטה)
    app.save_state({**app.DEFAULT_STATE, "app_mode": "acars"})
    monkeypatch.setattr(app, "_is_active", lambda svc: svc == app.ACARS_SERVICE)
    app._boot_restore()
    assert sysctl_calls == []


def test_boot_restore_voice_running_but_stale_config_rewrites(paths, no_sleep, sysctl_calls, monkeypatch):
    # קול רץ אבל הקונפיג ישן (שדרוג: חסר stats_filepath) => משכתבים ומרימים פעם אחת
    app.save_state({**app.DEFAULT_STATE, "freq": 121.5, "app_mode": "voice"})
    app.CONFIG_PATH.write_text("# old config without the new attributes")
    monkeypatch.setattr(app, "_is_active", lambda svc: svc == "rtl_airband")
    monkeypatch.setattr(app, "_restart_and_verify", lambda: (None, None, False))
    app._boot_restore()
    assert "stats_filepath" in app.CONFIG_PATH.read_text()


def test_boot_restore_failure_falls_to_off(paths, no_sleep, sysctl_calls, monkeypatch):
    # הכניסה למצב השמור נכשלה (SDR נוכח) => off + prev_mode, לא נפילה לקול
    app.save_state({**app.DEFAULT_STATE, "app_mode": "vdl2"})
    app._boot_restore()          # _is_active=False => האימות של _enter_vdl2 נכשל
    st = app.load_state()
    assert st["app_mode"] == "off" and st["prev_mode"] == "vdl2"
    assert ("restart", "rtl_airband") not in sysctl_calls


def test_boot_restore_sdr_absent_keeps_voice_intent(paths, no_sleep, sysctl_calls, monkeypatch):
    # SDR מנותק באתחול: הכוונה (voice) נשמרת — Restart=always של היחידה ימשיך
    # לנסות, ו-health מדווח תקלה. לא נופלים ל-off.
    app.save_state({**app.DEFAULT_STATE, "app_mode": "voice"})
    monkeypatch.setattr(app, "_sdr_present", lambda: False)
    monkeypatch.setattr(app, "_restart_and_verify", lambda: ("נתקע", None, True))
    app._boot_restore()
    assert app.load_state()["app_mode"] == "voice"


def test_boot_restore_defers_to_user_lock(paths, no_sleep, sysctl_calls):
    # המשתמש כבר התחיל מעבר מצב מה-UI (TUNE_LOCK תפוס) => השחזור מוותר בשקט
    app.save_state({**app.DEFAULT_STATE, "app_mode": "acars"})
    assert app.TUNE_LOCK.acquire(blocking=False)
    try:
        app._boot_restore()
        assert sysctl_calls == []
        assert app.load_state()["app_mode"] == "acars"
    finally:
        app.TUNE_LOCK.release()


def test_boot_restore_never_raises(paths, monkeypatch):
    # חוזה הבטיחות: כל חריגה נבלעת — _boot_restore לעולם לא מפיל את שרת הווב
    monkeypatch.setattr(app, "load_state", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    app._boot_restore()          # לא זורק


def test_boot_restore_state_changed_while_waiting_for_sdr(paths, no_sleep, sysctl_calls, monkeypatch):
    # בין load_state() בראש הפונקציה לתפיסת TUNE_LOCK עוברת המתנה ל-SDR (עד
    # BOOT_SDR_WAIT_SEC שניות). אם המשתמש הספיק לבחור מצב אחר מה-UI *ואותה
    # בחירה כבר הסתיימה* (הנעילה שוב פנויה) — השחזור חייב לוותר, לא לדרוס אותה
    # עם ה-state הישן שנקרא לפני ההמתנה.
    app.save_state({**app.DEFAULT_STATE, "app_mode": "voice", "freq": 121.5})

    def sdr_present_and_switch():
        # מדמה שהמשתמש עבר ל-acars מה-UI *בזמן* שה-boot restore חיכה ל-SDR
        app.save_state({**app.DEFAULT_STATE, "app_mode": "acars", "acars_freqs": ["131.550"]})
        return True
    monkeypatch.setattr(app, "_sdr_present", sdr_present_and_switch)
    app._boot_restore()
    assert sysctl_calls == []                        # לא נגע בכוונה הישנה (voice) בכלל
    assert app.load_state()["app_mode"] == "acars"    # הבחירה הטרייה של המשתמש נשמרת
