# ============================================================================
#  AIR-AM — סביבת הבדיקה של המצבים הכפולים (שני מקלטים)
# ----------------------------------------------------------------------------
#  משותף ל-test_voice_acars / test_voice2 / test_voice_vdl2 / test_dual_robustness.
#  קודם שלושת האחרונים ייבאו את ה-fixture מקובץ בדיקה אחר (test_voice_acars).
#  systemd מדומה: dict של יחידות פעילות; _restart_and_verify כותב stats.
# ============================================================================
import time

import pytest

import app
import appstate  # noqa: E402

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
    monkeypatch.setattr(appstate, "STATE_PATH", tmp_path / "state.json")
    for name, fn in (("ACARS_ENV_PATH", "acars.env"),
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
    # מקלטים ב-USB / מה ה-API מציע (‏_role_present) ו-systemctl show — דטרמיניסטי, בלי lsusb/Soapy אמיתיים
    monkeypatch.setattr(app, "_sdr_usb_full", lambda: (True, "SDRplay", 2))
    monkeypatch.setattr(app, "_serial_on_api", lambda serial: None)
    monkeypatch.setattr(app, "_rtl_airband_start_wall", lambda: None)
    monkeypatch.setattr(app, "_denoise_after_voice", lambda: None)
    monkeypatch.setattr(app, "VOICE_STARTUP_WAIT_SEC", 2)
    monkeypatch.setattr(app.time, "sleep", lambda s: None)
    monkeypatch.setattr(app, "_services_status",
                        lambda names: {n: ("active" if n in sd.active or n in ("icecast2", "sdrplay")
                                           else "inactive") for n in names})
    monkeypatch.setattr(app, "_denoise_status", lambda services: {})
    return sd


def _roles(primary=B, secondary=A):
    appstate._merge_state({"sdr_primary": primary, "sdr_secondary": secondary})


def _params():
    p, _ = app._parse_tune(appstate.DEFAULT_STATE)
    return p


@pytest.fixture
def env2(env, tmp_path, monkeypatch):
    monkeypatch.setattr(app, "VOICE2_CONFIG_PATH", tmp_path / "airband2.conf")
    monkeypatch.setattr(app, "STATS2_PATH", tmp_path / "stats2.txt")
    return env
