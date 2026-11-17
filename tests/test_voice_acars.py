# ============================================================================
#  AIR-AM - מצב "קול + ACARS" (שני מקלטים) — docs/dual-sdr-feasibility.md §6/§7.8
# ----------------------------------------------------------------------------
#  rtl_airband על הראשי + acarsdec (airam-acars2) על המשני. מה שנבדק כאן:
#  * הסדר: הקול עולה ו*מסיים* לעלות (stats ראשון) — ורק אז ACARS. על ה-Pi שני
#    צרכנים שעלו יחד הפילו power bank 5V/3A (§7.8); אחד אחרי השני — עבר נקי.
#  * בלי ראשי+משני מוגדרים — מסרבים (אחרת acars2 "כל מקלט" היה פותח את הראשי).
#  * כיוונון קול בתוך המצב לא מפיל את ה-ACARS; מעבר מפורש לקול — כן.
#  * state/health/reconcile/boot יודעים שהמצב חי רק כששניהם רצים.
#  systemd מדומה: dict של יחידות פעילות; _restart_and_verify כותב stats.
# ============================================================================
import time
from pathlib import Path

import pytest

import app

ROOT = Path(__file__).resolve().parent.parent
B, A = "240404AF60", "230405A498"
ORIGIN = {"Origin": "http://localhost"}


class FakeSystemd:
    def __init__(self):
        self.active = set()
        self.log = []            # (event, unit, t)

    def sysctl(self, action, svc, timeout=45):
        self.log.append((action, svc, time.monotonic()))
        if action == "stop":
            self.active.discard(svc)
        elif action in ("restart", "start"):
            if svc in self.fail:
                self.active.discard(svc)
            else:
                self.active.add(svc)
        return type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})()

    def is_active(self, svc):
        return svc in self.active

    fail = set()


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name, fn in (("STATE_PATH", "state.json"), ("ACARS_ENV_PATH", "acars.env"),
                     ("ACARS2_ENV_PATH", "acars2.env"), ("SDR_ENV_PATH", "sdr.env"),
                     ("CONFIG_PATH", "airband.conf"), ("STATS_PATH", "stats.txt"),
                     ("VDL2_ENV_PATH", "vdl2.env"), ("SATCOM_ENV_PATH", "satcom.env")):
        monkeypatch.setattr(app, name, tmp_path / fn)
    sd = FakeSystemd()
    sd.fail = set()

    def restart_voice():
        sd.log.append(("restart", "rtl_airband", time.monotonic()))
        sd.active.add("rtl_airband")
        app.STATS_PATH.write_text("x")          # rtl_airband סיים לעלות
        sd.log.append(("stats", "rtl_airband", time.monotonic()))
        return None, None, False

    monkeypatch.setattr(app, "_sysctl", sd.sysctl)
    monkeypatch.setattr(app, "_is_active", sd.is_active)
    monkeypatch.setattr(app, "_restart_and_verify", restart_voice)
    monkeypatch.setattr(app, "_journal_tail", lambda *a, **k: "")
    monkeypatch.setattr(app, "_sdr_present", lambda: True)
    monkeypatch.setattr(app, "_denoise_after_voice", lambda: None)
    monkeypatch.setattr(app, "ACARS2_STAGGER_SEC", 0)
    monkeypatch.setattr(app, "VOICE_STARTUP_WAIT_SEC", 2)
    monkeypatch.setattr(app.time, "sleep", lambda s: None)
    monkeypatch.setattr(app, "_services_status",
                        lambda names: {n: ("active" if n in sd.active or n in ("icecast2", "sdrplay")
                                           else "inactive") for n in names})
    monkeypatch.setattr(app, "_denoise_status", lambda services: {})
    return sd


def _roles(primary=B, secondary=A):
    app._merge_state({"sdr_primary": primary, "sdr_secondary": secondary})


def _params():
    p, _ = app._parse_tune(app.DEFAULT_STATE)
    return p


# --- env + תנאי תפקידים -------------------------------------------------------

def test_acars2_env_pins_secondary_and_acars_env_keeps_primary(env):
    _roles()
    app.write_acars2_env(["131.550"])
    app.write_acars_env(["131.550"])
    assert f"ACARS_SERIAL=,serial={A}\n" in app.ACARS2_ENV_PATH.read_text()
    assert f"ACARS_SERIAL=,serial={B}\n" in app.ACARS_ENV_PATH.read_text()
    assert f"ACARS_UDP={app.ACARS_UDP_HOST}:{app.ACARS_UDP_PORT}" in app.ACARS2_ENV_PATH.read_text()


@pytest.mark.parametrize("p,s", [(None, None), (B, None), (None, A), (B, B)])
def test_roles_required(env, p, s):
    _roles(p, s)
    assert app._roles_error()
    err, _d, _down = app._enter_voice_acars(_params(), ["131.550"])
    assert err and not env.active                        # לא הרים כלום


def test_api_mode_without_roles_is_400(env):
    r = app.app.test_client().post("/api/mode", json={"mode": "voice_acars"}, headers=ORIGIN)
    assert r.status_code == 400 and "ראשי" in r.get_json()["error"]
    assert not env.active


# --- הסדר: קול מסיים לעלות, ורק אז ACARS ---------------------------------------

def test_enter_voice_acars_staggers_after_voice_settled(env):
    _roles()
    env.active |= {"airam-vdl2"}                         # מצב קודם רץ
    err, _d, _down = app._enter_voice_acars(_params(), ["131.550", "131.725"])
    assert err is None
    assert env.active == {"rtl_airband", "airam-acars2"}
    seq = [(a, u) for a, u, _t in env.log]
    i_stats = seq.index(("stats", "rtl_airband"))
    i_acars2 = seq.index(("restart", "airam-acars2"))
    assert i_stats < i_acars2                            # לא במקביל
    assert ("stop", "airam-vdl2") in seq[:i_stats]
    assert "ACARS_FREQS=131.550 131.725" in app.ACARS2_ENV_PATH.read_text()


def test_stagger_waits_for_fresh_stats_not_old_file(env, monkeypatch):
    # קובץ stats ישן מסשן קודם לא נחשב "הקול עלה" ⇒ מחכים עד התקרה; stats טרי ⇒ מיד
    import os
    monkeypatch.setattr(app, "VOICE_STARTUP_WAIT_SEC", 0.2)
    app.STATS_PATH.write_text("old")
    old = time.time() - 100
    os.utime(app.STATS_PATH, (old, old))
    t0 = time.monotonic()
    app._wait_voice_settled(time.time())
    assert time.monotonic() - t0 >= 0.2
    since = time.time() - 1
    app.STATS_PATH.write_text("fresh")
    t0 = time.monotonic()
    app._wait_voice_settled(since)
    assert time.monotonic() - t0 < 0.1


def test_acars2_failure_stops_it_and_reports(env):
    _roles()
    env.fail = {"airam-acars2"}
    err, _d, _down = app._enter_voice_acars(_params(), ["131.550"])
    assert err and "המשני" in err
    assert "airam-acars2" not in env.active


def test_api_mode_success_and_failure_paths(env):
    _roles()
    c = app.app.test_client()
    r = c.post("/api/mode", json={"mode": "voice_acars", "freqs": ["131.550"]}, headers=ORIGIN)
    assert r.status_code == 200, r.get_json()
    st = app.load_state()
    assert st["app_mode"] == "voice_acars" and st["acars_freqs"] == ["131.550"]
    env.fail = {"airam-acars2"}
    # כבר במצב ⇒ רק המשני מאותחל; כשלו לא מפיל את הקול (test_voice2: ‏secondary_only)
    r = c.post("/api/mode", json={"mode": "voice_acars"}, headers=ORIGIN)
    assert r.status_code == 500 and "rtl_airband" in env.active
    # כניסה מלאה (ממצב אחר) שנכשלת ⇒ off
    c.post("/api/mode", json={"mode": "off"}, headers=ORIGIN)
    r = c.post("/api/mode", json={"mode": "voice_acars"}, headers=ORIGIN)
    assert r.status_code == 500 and r.get_json()["app_mode"] == "off"   # לעולם לא fallback לקול
    assert not env.active


def test_window_check_applies(env):
    _roles()
    r = app.app.test_client().post("/api/mode", json={"mode": "voice_acars",
                                                      "freqs": ["131.550", "136.975"]}, headers=ORIGIN)
    assert r.status_code == 400


# --- מצב חי / state / health -------------------------------------------------

def test_live_mode_needs_both(env):
    env.active = {"rtl_airband", "airam-acars2"}
    assert app._live_mode() == "voice_acars"
    env.active = {"rtl_airband"}
    assert app._live_mode() == "voice"


def test_state_and_health_report_partial_as_fault(env):
    _roles()
    app._merge_state({"app_mode": "voice_acars"})
    c = app.app.test_client()
    env.active = {"rtl_airband", "airam-acars2"}
    s = c.get("/api/state").get_json()
    h = c.get("/api/health").get_json()
    assert s["app_mode"] == "voice_acars" and s["mode_ok"] is True and s["roles_ready"] is True
    assert h["app_mode"] == "voice_acars" and h["ok"] is True
    env.active = {"rtl_airband"}                         # ה-ACARS על המשני נפל
    s = c.get("/api/state").get_json()
    h = c.get("/api/health").get_json()
    assert s["app_mode"] == "voice_acars" and s["mode_ok"] is False
    assert h["ok"] is False


def test_standby_stops_acars2(env):
    env.active = {"rtl_airband", "airam-acars2"}
    err, _ = app._enter_standby()
    assert err is None and not env.active


# --- כיוונון קול בתוך המצב ---------------------------------------------------

def test_tune_inside_dual_keeps_acars(env):
    _roles()
    app._merge_state({"app_mode": "voice_acars"})
    env.active = {"rtl_airband", "airam-acars2"}
    r = app.app.test_client().post("/api/tune", json={"freq": 120.5}, headers=ORIGIN)
    assert r.status_code == 200, r.get_json()
    assert "airam-acars2" in env.active
    assert app.load_state()["app_mode"] == "voice_acars"
    assert ("stop", "airam-acars2") not in [(a, u) for a, u, _t in env.log]


def test_explicit_voice_mode_leaves_dual(env):
    _roles()
    app._merge_state({"app_mode": "voice_acars"})
    env.active = {"rtl_airband", "airam-acars2"}
    r = app.app.test_client().post("/api/mode", json={"mode": "voice"}, headers=ORIGIN)
    assert r.status_code == 200
    assert env.active == {"rtl_airband"} and app.load_state()["app_mode"] == "voice"


@pytest.mark.parametrize("mode", ["acars", "vdl2"])
def test_other_modes_stop_acars2(env, mode):
    env.active = {"rtl_airband", "airam-acars2"}
    enter = app._enter_acars if mode == "acars" else app._enter_vdl2
    err, _d = enter(["131.550"] if mode == "acars" else ["136.975"])
    assert err is None and "airam-acars2" not in env.active


# --- התאוששות ---------------------------------------------------------------

def test_reconcile_restarts_only_acars2_when_voice_alive(env):
    _roles()
    app._merge_state({"app_mode": "voice_acars", "acars_freqs": ["131.550"]})
    env.active = {"rtl_airband"}
    app._mode_reconcile_once()
    seq = [(a, u) for a, u, _t in env.log]
    assert ("restart", "airam-acars2") in seq
    assert ("restart", "rtl_airband") not in seq        # לא נוגעים בקול שעובד
    assert env.active == {"rtl_airband", "airam-acars2"}


def test_reconcile_full_reenter_when_nothing_runs(env):
    _roles()
    app._merge_state({"app_mode": "voice_acars", "acars_freqs": ["131.550"]})
    app._mode_reconcile_once()
    assert env.active == {"rtl_airband", "airam-acars2"}


def test_boot_restore_enters_dual(env):
    _roles()
    app._merge_state({"app_mode": "voice_acars", "acars_freqs": ["131.550"]})
    app._boot_restore()
    assert env.active == {"rtl_airband", "airam-acars2"}
    assert app.load_state()["app_mode"] == "voice_acars"


def test_antenna_check_refused_in_dual(env):
    env.active = {"rtl_airband", "airam-acars2"}
    r = app.app.test_client().post("/api/antenna/check", json={}, headers=ORIGIN)
    assert r.status_code == 409


# --- יחידה + התקנה -------------------------------------------------------------

def test_unit_runs_beside_voice_on_secondary():
    unit = (ROOT / "systemd" / "airam-acars2.service").read_text()
    conflicts = [l for l in unit.splitlines() if l.startswith("Conflicts=")][0]
    assert "rtl_airband" not in conflicts               # לצד הקול — לא במקומו
    assert all(u in conflicts for u in ("airam-acars.service", "airam-vdl2.service", "airam-satcom.service"))
    assert "ExecStartPre=/usr/local/bin/airam-wait-sdrplay %n secondary" in unit
    assert "-d driver=sdrplay${ACARS_SERIAL} " in unit
    assert "EnvironmentFile=-/etc/airam/acars2.env" in unit
    assert "[Install]" not in unit                       # לא enabled — airam-web משחזר
    # הדירוג חייב לחול גם כש-systemd מאתחל את שנינו יחד (PartOf=sdrplay), לא רק דרך airam-web
    pre = [l for l in unit.splitlines() if l.startswith("ExecStartPre=")]
    assert pre[-1].startswith("ExecStartPre=/bin/sleep ") and int(pre[-1].split()[-1]) >= 9


def test_install_copies_unit_and_grants_only_restart_stop():
    sh = (ROOT / "install.sh").read_text()
    assert 'cp "$REPO_DIR/systemd/airam-acars2.service" /etc/systemd/system/' in sh
    grants = [l for l in sh.splitlines() if "airam-acars2" in l and "NOPASSWD" in l]
    assert sorted(l.split()[-2] for l in grants) == ["restart", "stop"]
