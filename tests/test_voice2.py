# ============================================================================
#  AIR-AM - מצב "קול כפול" (voice2, שני מקלטים) — docs/dual-sdr-feasibility.md §8
# ----------------------------------------------------------------------------
#  rtl_airband על הראשי + rtl_airband2 על המשני (תהליך לכל מקלט — §7.6). נבדק:
#  * קונפיג הערוץ השני: serial של המשני, stats/mount משלו, *בלי* פלט UDP לסינון.
#  * הסדר: הערוץ השני עולה רק אחרי שהראשי סיים לעלות (§7.8 — שני rtl_airband יחד
#    הפילו power bank 5V/3A).
#  * /api/tune2 מאתחל רק את המשני; /api/tune שומר את הערוץ השני.
#  * state/health/reconcile/boot: חי רק כששניהם רצים.
#  משתמש באותו systemd מדומה כמו test_voice_acars.
# ============================================================================
import pytest

import app
import appstate  # noqa: E402
from dual_env import A, B, ORIGIN, _params, _roles, env, env2  # noqa: F401  (fixtures נטענים לפי שם)


def test_voice2_config_pins_secondary_own_stats_mount_no_udp(env2):
    _roles()
    appstate._merge_state({"voice2_freq": 120.5, "squelch_mode": "auto"})
    app.write_voice2_config()
    c = app.VOICE2_CONFIG_PATH.read_text()
    assert f'device_string = "driver=sdrplay,serial={A}' in c
    assert f'stats_filepath = "{app.STATS2_PATH}"' in c
    assert f'mountpoint = "{app.MOUNT2}"' in c
    assert "udp_stream" not in c                  # הסינון מאזין לערוץ הראשי בלבד
    assert "freq = 120.5000" in c
    assert "gain =" not in c                      # AGC תמיד
    assert 'type = "file"' in c                   # הקלטות לאותו יומן


def test_primary_config_unchanged_has_udp(env2):
    _roles()
    app.write_config(132.5, "am", True, 40, 4)
    c = app.CONFIG_PATH.read_text()
    assert "udp_stream" in c and f'mountpoint = "{app.MOUNT}"' in c
    assert f"serial={B}" in c


@pytest.mark.parametrize("p,s", [(None, None), (B, None), (B, B)])
def test_roles_required(env2, p, s):
    _roles(p, s)
    err, _d, _down = app._enter_voice2(_params())
    assert err and not env2.active
    if not p:
        assert "קול כפול" in err


def test_enter_voice2_staggers_after_voice_settled(env2):
    _roles()
    env2.active |= {"airam-acars2"}             # היה קול + ACARS — המשני מתחלף
    err, _d, _down = app._enter_voice2(_params())
    assert err is None
    assert env2.active == {"rtl_airband", "rtl_airband2"}
    order = [(e, u) for e, u, _t in env2.log if e in ("restart", "stats")]
    assert order.index(("stats", "rtl_airband")) < order.index(("restart", "rtl_airband2"))
    assert app._live_mode() == "voice2"


def test_voice2_failure_stops_secondary(env2):
    _roles()
    env2.fail = {"rtl_airband2"}
    err, _d, _down = app._enter_voice2(_params())
    assert err and "rtl_airband2" not in env2.active


def test_api_mode_voice2_saves_and_rejects_same_freq(env2):
    _roles()
    c = app.app.test_client()
    r = c.post("/api/mode", json={"mode": "voice2", "freq2": 132.5}, headers=ORIGIN)   # = הראשי
    assert r.status_code == 400 and not env2.active
    r = c.post("/api/mode", json={"mode": "voice2", "freq2": 121.4, "mod2": "nfm"}, headers=ORIGIN)
    d = r.get_json()
    assert r.status_code == 200 and d["app_mode"] == "voice2" and d["voice2_freq"] == 121.4
    st = appstate.load_state()
    assert st["app_mode"] == "voice2" and st["voice2_mod"] == "nfm"
    s = c.get("/api/state").get_json()
    assert s["app_mode"] == "voice2" and s["mode_ok"] is True and s["mount2"] == app.MOUNT2
    h = c.get("/api/health").get_json()
    assert h["app_mode"] == "voice2" and h["ok"] is True


def test_api_mode_voice2_without_roles_400(env2):
    r = app.app.test_client().post("/api/mode", json={"mode": "voice2"}, headers=ORIGIN)
    assert r.status_code == 400 and not env2.active


def test_partial_is_fault(env2):
    _roles()
    appstate._merge_state({"app_mode": "voice2"})
    env2.active |= {"rtl_airband"}
    c = app.app.test_client()
    assert c.get("/api/state").get_json()["mode_ok"] is False
    h = c.get("/api/health").get_json()
    assert h["app_mode"] == "voice2" and h["ok"] is False


def test_tune2_restarts_only_secondary(env2):
    _roles()
    appstate._merge_state({"app_mode": "voice2", "voice2_freq": 121.4})
    env2.active |= {"rtl_airband", "rtl_airband2"}
    env2.log.clear()
    r = app.app.test_client().post("/api/tune2", json={"freq2": 118.05}, headers=ORIGIN)
    assert r.status_code == 200 and r.get_json()["voice2_freq"] == 118.05
    units = {u for e, u, _t in env2.log if e in ("restart", "stop")}
    assert units == {"rtl_airband2"}
    assert appstate.load_state()["voice2_freq"] == 118.05
    assert "freq = 118.0500" in app.VOICE2_CONFIG_PATH.read_text()


def test_tune2_outside_voice2_409(env2):
    _roles()
    appstate._merge_state({"app_mode": "voice"})
    r = app.app.test_client().post("/api/tune2", json={"freq2": 118.05}, headers=ORIGIN)
    assert r.status_code == 409


def test_tune2_failure_rolls_back_and_keeps_state(env2):
    _roles()
    appstate._merge_state({"app_mode": "voice2", "voice2_freq": 121.4})
    env2.active |= {"rtl_airband", "rtl_airband2"}
    env2.fail = {"rtl_airband2"}
    r = app.app.test_client().post("/api/tune2", json={"freq2": 118.05}, headers=ORIGIN)
    assert r.status_code == 500
    assert appstate.load_state()["voice2_freq"] == 121.4


def test_tune2_requires_guard(env2):
    _roles()
    appstate._merge_state({"app_mode": "voice2"})
    r = app.app.test_client().post("/api/tune2", json={"freq2": 118.05},
                                   headers={"Origin": "http://evil.example"})
    assert r.status_code == 403


def test_primary_tune_keeps_second_channel(env2):
    _roles()
    appstate._merge_state({"app_mode": "voice2", "voice2_freq": 121.4})
    env2.active |= {"rtl_airband", "rtl_airband2"}
    r = app.app.test_client().post("/api/tune", json={"freq": 134.6}, headers=ORIGIN)
    assert r.status_code == 200 and r.get_json()["app_mode"] == "voice2"
    assert "rtl_airband2" in env2.active


def test_mode_voice_leaves_voice2(env2):
    _roles()
    appstate._merge_state({"app_mode": "voice2"})
    env2.active |= {"rtl_airband", "rtl_airband2"}
    r = app.app.test_client().post("/api/mode", json={"mode": "voice"}, headers=ORIGIN)
    assert r.status_code == 200 and "rtl_airband2" not in env2.active


@pytest.mark.parametrize("enter", [lambda: app._enter_acars(["131.550"]),
                                   lambda: app._enter_vdl2(["136.975"]),
                                   lambda: app._enter_standby()])
def test_other_modes_stop_second_channel(env2, enter):
    env2.active |= {"rtl_airband", "rtl_airband2"}
    enter()
    assert "rtl_airband2" not in env2.active


def test_reconcile_restarts_only_secondary(env2):
    _roles()
    appstate._merge_state({"app_mode": "voice2"})
    env2.active |= {"rtl_airband"}
    env2.log.clear()
    app._mode_reconcile_once()
    assert env2.active == {"rtl_airband", "rtl_airband2"}
    assert {u for e, u, _t in env2.log if e == "restart"} == {"rtl_airband2"}


def test_boot_restore_voice2(env2, monkeypatch):
    _roles()
    appstate._merge_state({"app_mode": "voice2"})
    monkeypatch.setattr(app, "_config_stale", lambda: False)
    app._boot_restore()
    assert env2.active == {"rtl_airband", "rtl_airband2"}


def test_metrics_ch2_reads_second_stats(env2):
    appstate._merge_state({"voice2_freq": 121.4})
    app.STATS2_PATH.write_text(
        'channel_dbfs_signal_level{freq="121.400"} -20.0\n'
        'channel_dbfs_noise_level{freq="121.400"} -40.0\n')
    m = app.app.test_client().get("/api/metrics?ch=2").get_json()
    assert m["ch"] == 2 and m["snr"] == 20.0


def test_stream_proxy_closed_list():
    import inspect
    src = inspect.getsource(app.stream_proxy)
    assert '"live2": MOUNT2' in src


def test_antenna_check_409_in_voice2(env2):
    env2.active |= {"rtl_airband", "rtl_airband2"}
    r = app.app.test_client().post("/api/antenna/check", json={}, headers=ORIGIN)
    assert r.status_code == 409


def test_unit_conflicts_and_stagger():
    from pathlib import Path
    u = (Path(app.__file__).resolve().parent.parent / "systemd/rtl_airband2.service").read_text()
    conflicts = [ln for ln in u.splitlines() if ln.startswith("Conflicts=")][0]
    assert "rtl_airband.service" not in conflicts.split("=", 1)[1].split()
    for svc in ("airam-acars.service", "airam-vdl2.service", "airam-satcom.service",
                "airam-acars2.service"):
        assert svc in conflicts
    assert "ExecStartPre=/bin/sleep 10" in u
    assert "airam-wait-sdrplay %n secondary" in u
    assert "[Install]" not in u
    inst = (Path(app.__file__).resolve().parent.parent / "install.sh").read_text()
    assert "systemctl restart rtl_airband2" in inst and "systemctl stop rtl_airband2" in inst
    assert "systemd/rtl_airband2.service" in inst


# --- בקשת המשתמש: שינוי תדרים בצד הדאטה/המשני לא נוגע במקלט הראשי -------------

def test_acars_bank_change_in_voice_acars_restarts_only_secondary(env2):
    _roles()
    appstate._merge_state({"app_mode": "voice_acars", "acars_freqs": ["131.550"]})
    env2.active |= {"rtl_airband", "airam-acars2"}
    env2.log.clear()
    r = app.app.test_client().post("/api/mode", json={"mode": "voice_acars",
                                                      "freqs": ["131.725", "131.850"]}, headers=ORIGIN)
    d = r.get_json()
    assert r.status_code == 200 and d["secondary_only"] is True
    assert {u for e, u, _t in env2.log} == {"airam-acars2"}       # הקול לא נעצר ולא אותחל
    assert env2.active == {"rtl_airband", "airam-acars2"}
    assert appstate.load_state()["acars_freqs"] == ["131.725", "131.850"]


def test_acars_bank_change_failure_keeps_voice(env2):
    _roles()
    appstate._merge_state({"app_mode": "voice_acars"})
    env2.active |= {"rtl_airband", "airam-acars2"}
    env2.fail = {"airam-acars2"}
    r = app.app.test_client().post("/api/mode", json={"mode": "voice_acars", "freqs": ["131.725"]},
                                   headers=ORIGIN)
    assert r.status_code == 500 and "rtl_airband" in env2.active
    assert appstate.load_state()["app_mode"] == "voice_acars"            # לא נפל ל-off — הקול רץ


def test_voice2_mode_repeat_restarts_only_secondary(env2):
    _roles()
    appstate._merge_state({"app_mode": "voice2", "voice2_freq": 121.4})
    env2.active |= {"rtl_airband", "rtl_airband2"}
    env2.log.clear()
    r = app.app.test_client().post("/api/mode", json={"mode": "voice2", "freq2": 118.05}, headers=ORIGIN)
    assert r.status_code == 200 and r.get_json()["secondary_only"] is True
    assert {u for e, u, _t in env2.log} == {"rtl_airband2"}


def test_voice_acars_from_plain_voice_is_full_entry(env2):
    # קול רגיל ⇒ voice_acars: כניסה מלאה (המצב השמור הוא voice, לא voice_acars)
    _roles()
    appstate._merge_state({"app_mode": "voice"})
    env2.active |= {"rtl_airband"}
    r = app.app.test_client().post("/api/mode", json={"mode": "voice_acars"}, headers=ORIGIN)
    assert r.status_code == 200 and "secondary_only" not in r.get_json()


def test_voice2_config_uses_airam2_recording_prefix(env2):
    _roles()
    appstate._merge_state({"squelch_mode": "auto"})              # "open" (ATIS) לא מקליט בכלל
    app.write_voice2_config()
    assert 'filename_template = "airam2";' in app.VOICE2_CONFIG_PATH.read_text()
    app.write_config(132.5, "am", True, 40, 4, "auto")
    assert 'filename_template = "airam";' in app.CONFIG_PATH.read_text()
