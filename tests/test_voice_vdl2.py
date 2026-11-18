# ============================================================================
#  AIR-AM - מצב "קול + VDL2" (שני מקלטים) — docs/dual-sdr-feasibility.md §11.2 ג׳
# ----------------------------------------------------------------------------
#  rtl_airband על הראשי + dumpvdl2 (airam-vdl2-2) על המשני — אותו מבנה כמו "קול + ACARS":
#  env משלו (Hz, serial של המשני), אותו פורט UDP (פיד VDL2 רגיל), דירוג, שינוי בנק = רק המשני.
# ============================================================================
from pathlib import Path

import pytest

import app
from dual_env import A, B, ORIGIN, _params, _roles, env  # noqa: F401  (fixtures נטענים לפי שם)

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def env3(env, tmp_path, monkeypatch):
    monkeypatch.setattr(app, "VDL2B_ENV_PATH", tmp_path / "vdl2-2.env")
    return env


def test_vdl2b_env_pins_secondary_in_hz(env3):
    _roles()
    app._start_vdl2b(["136.975", "136.875"])
    t = app.VDL2B_ENV_PATH.read_text()
    assert "VDL2_FREQS=136975000 136875000\n" in t
    assert f"VDL2_SERIAL=,serial={A}\n" in t                   # המשני
    app.write_vdl2_env(["136.975"])                             # מצב VDL2 רגיל — עדיין הראשי
    assert f"VDL2_SERIAL=,serial={B}\n" in app.VDL2_ENV_PATH.read_text()


def test_enter_voice_vdl2_staggered(env3):
    _roles()
    env3.active |= {"airam-acars2"}
    err, _d, _down = app._enter_voice_vdl2(_params(), ["136.975"])
    assert err is None and env3.active == {"rtl_airband", "airam-vdl2-2"}
    order = [(e, u) for e, u, _t in env3.log if e in ("restart", "stats")]
    assert order.index(("stats", "rtl_airband")) < order.index(("restart", "airam-vdl2-2"))
    assert app._live_mode() == "voice_vdl2"


def test_api_mode_voice_vdl2_and_state_health(env3):
    _roles()
    c = app.app.test_client()
    r = c.post("/api/mode", json={"mode": "voice_vdl2", "freqs": ["136.975"]}, headers=ORIGIN)
    assert r.status_code == 200 and r.get_json()["vdl2_freqs"] == ["136.975"]
    assert app.load_state()["app_mode"] == "voice_vdl2"
    s = c.get("/api/state").get_json()
    assert s["app_mode"] == "voice_vdl2" and s["mode_ok"] is True
    h = c.get("/api/health").get_json()
    assert h["app_mode"] == "voice_vdl2" and h["ok"] is True
    assert c.get("/api/vdl2").get_json()["active"] is True


def test_voice_vdl2_window_and_roles_checked(env3):
    c = app.app.test_client()
    r = c.post("/api/mode", json={"mode": "voice_vdl2"}, headers=ORIGIN)
    assert r.status_code == 400 and "קול + VDL2" in r.get_json()["error"]
    _roles()
    r = c.post("/api/mode", json={"mode": "voice_vdl2", "freqs": ["131.550", "136.975"]}, headers=ORIGIN)
    assert r.status_code == 400 and not env3.active


def test_bank_change_restarts_only_secondary(env3):
    _roles()
    app._merge_state({"app_mode": "voice_vdl2", "vdl2_freqs": ["136.975"]})
    env3.active |= {"rtl_airband", "airam-vdl2-2"}
    env3.log.clear()
    r = app.app.test_client().post("/api/mode", json={"mode": "voice_vdl2",
                                                      "freqs": ["136.725", "136.975"]}, headers=ORIGIN)
    assert r.status_code == 200 and r.get_json()["secondary_only"] is True
    assert {u for _e, u, _t in env3.log} == {"airam-vdl2-2"}


def test_reconcile_and_boot(env3, monkeypatch):
    _roles()
    app._merge_state({"app_mode": "voice_vdl2", "vdl2_freqs": ["136.975"]})
    env3.active = {"rtl_airband"}
    app._mode_reconcile_once()
    assert env3.active == {"rtl_airband", "airam-vdl2-2"}
    env3.active = set()
    monkeypatch.setattr(app, "_config_stale", lambda: False)
    app._boot_restore()
    assert env3.active == {"rtl_airband", "airam-vdl2-2"}


@pytest.mark.parametrize("enter", [lambda: app._enter_acars(["131.550"]),
                                   lambda: app._enter_vdl2(["136.975"]),
                                   lambda: app._enter_standby()])
def test_other_modes_stop_vdl2b(env3, enter):
    env3.active |= {"rtl_airband", "airam-vdl2-2"}
    enter()
    assert "airam-vdl2-2" not in env3.active


def test_unit_and_install():
    u = (ROOT / "systemd/airam-vdl2-2.service").read_text()
    conflicts = [ln for ln in u.splitlines() if ln.startswith("Conflicts=")][0].split("=", 1)[1].split()
    assert "rtl_airband.service" not in conflicts
    assert set(conflicts) == {"airam-acars.service", "airam-vdl2.service", "airam-satcom.service",
                              "airam-acars2.service", "rtl_airband2.service"}
    assert "EnvironmentFile=-/etc/airam/vdl2-2.env" in u
    assert "airam-wait-sdrplay %n secondary" in u and "ExecStartPre=/bin/sleep 10" in u
    assert "port=5557" in u and "${VDL2_SERIAL}" in u and "[Install]" not in u
    inst = (ROOT / "install.sh").read_text()
    assert "systemctl restart airam-vdl2-2" in inst and "systemctl stop airam-vdl2-2" in inst
    assert "systemd/airam-vdl2-2.service" in inst
    assert "airam-vdl2-2.service" in (ROOT / "scripts/airam-wait-sdrplay").read_text()
