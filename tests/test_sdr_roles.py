# ============================================================================
#  AIR-AM - תפקידי המקלטים: קיבוע לפי serial (docs/dual-sdr-feasibility.md §3.1)
# ----------------------------------------------------------------------------
#  עם שני מקלטים מחוברים, "driver=sdrplay" פותח את הראשון שה-API מחזיר — איזה
#  מקלט (ואיזו אנטנה) נקבע במקרה. כאן: ה-serial של הראשי בכל צרכן, ה-API לבחירה,
#  והשער airam-wait-sdrplay שמחכה למקלט הספציפי ולא בועט ב-sdrplay כשצרכן אחר רץ.
#  ⚠ בלי בחירה ההתנהגות זהה לגמרי למה שהיה — מקלט יחיד לא מושפע.
# ============================================================================
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

import app

ROOT = Path(__file__).resolve().parent.parent
B, A = "240404AF60", "230405A498"            # ה-serials מה-Pi (17.11.2026)
FIND_TWO = (f"Found device 0\n  driver = sdrplay\n  label = SDRplay Dev0 RSP1A {A}\n  serial = {A}\n\n"
            f"Found device 1\n  driver = sdrplay\n  label = SDRplay Dev1 RSP1B {B}\n  serial = {B}\n\n")


@pytest.fixture
def paths(tmp_path, monkeypatch):
    for name, fn in (("STATE_PATH", "state.json"), ("ACARS_ENV_PATH", "acars.env"),
                     ("VDL2_ENV_PATH", "vdl2.env"), ("SATCOM_ENV_PATH", "satcom.env"),
                     ("SDR_ENV_PATH", "sdr.env"), ("CONFIG_PATH", "airband.conf")):
        monkeypatch.setattr(app, name, tmp_path / fn)
    app._sdr_probe_cache.update(t=0.0, result=None)
    return tmp_path


@pytest.fixture
def client(paths):
    return app.app.test_client()


def _set_primary(serial):
    app._merge_state({"sdr_primary": serial})


# --- בלי בחירה: בדיוק כמו קודם ----------------------------------------------

def test_no_primary_means_any_device_everywhere(paths):
    conf = app.render_config(132.5, "am", True, 40, 0)
    assert 'device_string = "driver=sdrplay";' in conf
    app.write_acars_env(["131.550"])
    app.write_vdl2_env(["136.975"])
    app.write_satcom_env(["AF1"])
    assert "ACARS_SERIAL=\n" in app.ACARS_ENV_PATH.read_text()
    assert "VDL2_SERIAL=\n" in app.VDL2_ENV_PATH.read_text()
    assert "SATCOM_SERIAL=\n" in app.SATCOM_ENV_PATH.read_text()


# --- עם ראשי: כל צרכן מקובע ---------------------------------------------------

def test_primary_pins_every_consumer(paths):
    _set_primary(B)
    app.write_config(132.5, "am", True, 40, 0, fm_notch=True)
    assert f'device_string = "driver=sdrplay,serial={B},rfnotch_ctrl=true";' in app.CONFIG_PATH.read_text()
    app.write_acars_env(["131.550"])
    app.write_vdl2_env(["136.975"])
    app.write_satcom_env(["AF1"])
    assert f"ACARS_SERIAL=,serial={B}\n" in app.ACARS_ENV_PATH.read_text()
    assert f"VDL2_SERIAL=,serial={B}\n" in app.VDL2_ENV_PATH.read_text()
    # inmarsat-sniffer: -i sdrplay-<serial> (options.c:374, נבדק במקור)
    assert f"SATCOM_SERIAL=-{B}\n" in app.SATCOM_ENV_PATH.read_text()


@pytest.mark.parametrize("bad", ["", "abc", "24 04", "2404,serial=X", "$(reboot)",
                                 "A" * 33, "240404AF60\nACARS_GAIN=0", None, 123])
def test_sanitize_serial_rejects(bad):
    assert app._sanitize_serial(bad) is None


def test_bad_serial_in_state_never_reaches_device_string(paths):
    # state.json ידני/פגום — לא נכנס לקונפיג של תהליך root
    app._merge_state({"sdr_primary": "X,driver=evil"})
    assert app._primary_serial() is None
    assert 'device_string = "driver=sdrplay";' in app.render_config(132.5, "am", True, 40, 0,
                                                                    serial=app._primary_serial())


# --- יחידות systemd + ברירות מחדל -------------------------------------------

@pytest.mark.parametrize("unit,frag,env,key", [
    ("airam-acars.service", "-d driver=sdrplay${ACARS_SERIAL} ", "acars.env", "ACARS_SERIAL="),
    ("airam-vdl2.service", "--soapysdr driver=sdrplay${VDL2_SERIAL} ", "vdl2.env", "VDL2_SERIAL="),
    ("airam-satcom.service", "-i sdrplay${SATCOM_SERIAL} ", "satcom.env", "SATCOM_SERIAL="),
])
def test_units_take_serial_inside_the_word(unit, frag, env, key):
    # ${VAR} בתוך מילה: חסר = ריק ⇒ env ישן משדרוג (בלי המפתח) עדיין עובד
    assert frag in (ROOT / "systemd" / unit).read_text()
    assert f"\n{key}\n" in (ROOT / "config" / env).read_text()


# --- API ---------------------------------------------------------------------

def _fake_run(find=FIND_TWO, lsusb="Bus 001 Device 003: ID 1df7:3050 SDRplay\n"
                                    "Bus 003 Device 020: ID 1df7:3000 SDRplay RSP1a\n",
              services=None):
    services = services or {}

    def run(cmd, **kw):
        if cmd[0] == "lsusb":
            return types.SimpleNamespace(returncode=0, stdout=lsusb, stderr="")
        if cmd[0] == "SoapySDRUtil":
            return types.SimpleNamespace(returncode=0, stdout=find, stderr="")
        if cmd[:2] == ["systemctl", "is-active"]:
            out = "\n".join(services.get(n, "inactive") for n in cmd[2:]) + "\n"
            return types.SimpleNamespace(returncode=3, stdout=out, stderr="")
        raise AssertionError(f"unexpected {cmd}")
    return run


def test_parse_sdr_find():
    devs = app._parse_sdr_find(FIND_TWO)
    assert [(d["serial"], d["model"]) for d in devs] == [(A, "RSP1A"), (B, "RSP1B")]
    assert app._parse_sdr_find("Found device 0\n  driver = sdrplay\n  serial = bad serial\n") == []


def test_devices_endpoint_lists_and_roles(client, monkeypatch):
    monkeypatch.setattr(app.subprocess, "run", _fake_run())
    _set_primary(B)
    d = client.get("/api/sdr/devices").get_json()
    assert d["ok"] and d["usb_count"] == 2 and d["primary"] == B and d["secondary"] is None
    assert {x["serial"] for x in d["devices"]} == {A, B}


def test_sdr_status_flags_two_receivers_without_primary(client, monkeypatch):
    monkeypatch.setattr(app.subprocess, "run", _fake_run())
    d = client.get("/api/sdr").get_json()
    assert d["usb_count"] == 2 and d["roles_needed"] is True
    _set_primary(B)
    app._sdr_probe_cache.update(t=0.0, result=None)
    d = client.get("/api/sdr").get_json()
    assert d["roles_needed"] is False and d["primary"] == B


def test_single_receiver_never_asks_for_roles(client, monkeypatch):
    monkeypatch.setattr(app.subprocess, "run",
                        _fake_run(lsusb="Bus 001 Device 003: ID 1df7:3050 SDRplay\n"))
    d = client.get("/api/sdr").get_json()
    assert d["usb_count"] == 1 and d["roles_needed"] is False


def _post(client, body):
    return client.post("/api/sdr/roles", json=body, headers={"Origin": "http://localhost"})


def test_roles_set_writes_state_and_sdr_env(client):
    r = _post(client, {"primary": B, "secondary": A})
    assert r.status_code == 200, r.get_json()
    st = app.load_state()
    assert st["sdr_primary"] == B and st["sdr_secondary"] == A
    assert app.SDR_ENV_PATH.read_text().splitlines()[1:3] == [f"SDR_PRIMARY={B}", f"SDR_SECONDARY={A}"]


def test_roles_clear_and_partial_update(client):
    _post(client, {"primary": B, "secondary": A})
    assert _post(client, {"secondary": None}).status_code == 200
    st = app.load_state()
    assert st["sdr_primary"] == B and st["sdr_secondary"] is None
    assert "SDR_SECONDARY=\n" in app.SDR_ENV_PATH.read_text()


@pytest.mark.parametrize("body", [{}, {"other": 1}, {"primary": "bad serial"},
                                  {"primary": B, "secondary": B}, {"primary": "X,y=z"}])
def test_roles_rejects(client, body):
    assert _post(client, body).status_code == 400
    assert app.load_state().get("sdr_primary") is None


def test_roles_requires_guard(client):
    # כמו כל route משנה-מצב: Origin זר ⇒ נחסם ב-_guard
    r = client.post("/api/sdr/roles", json={"primary": B}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    assert app.load_state().get("sdr_primary") is None


# --- השער airam-wait-sdrplay ---------------------------------------------------

GATE = ROOT / "scripts" / "airam-wait-sdrplay"


@pytest.fixture
def gate(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.log"
    fakes = {
        # find עם serial מחזיר רק אותו (Registration.cpp:55) — כאן: רק אם FAKE_PRESENT כולל אותו
        "SoapySDRUtil": f"""
import os, sys
arg = sys.argv[1]
present = os.environ.get("FAKE_PRESENT", "").split(",")
for s in present:
    if not s:
        continue
    if "serial=" in arg and arg.split("serial=")[1] != s:
        continue
    print("Found device 0\\n  driver = sdrplay\\n  serial = " + s)
""",
        "systemctl": f"""
import os, sys
open({str(log)!r}, "a").write(" ".join(sys.argv[1:]) + "\\n")
if sys.argv[1] == "is-active":
    sys.exit(0 if sys.argv[-1] in os.environ.get("FAKE_ACTIVE", "").split(",") else 3)
""",
        "lsusb": "print('Bus 001 Device 003: ID 1df7:3050 SDRplay')\n",
        "sleep": "pass\n",
    }
    for name, body in fakes.items():
        p = bindir / name
        p.write_text(f"#!{sys.executable}\n" + body)
        p.chmod(0o755)
    sdr_env = tmp_path / "sdr.env"

    def run_gate(caller, role=None, present="", active="", primary=None):
        sdr_env.write_text(f"# x\nSDR_PRIMARY={primary or ''}\nSDR_SECONDARY=\n")
        log.write_text("")
        env = dict(os.environ, PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}",
                   AIRAM_SDR_ENV=str(sdr_env), FAKE_PRESENT=present, FAKE_ACTIVE=active)
        args = ["bash", str(GATE), caller] + ([role] if role else [])
        r = subprocess.run(args, env=env, capture_output=True, text=True, timeout=60)
        return r.returncode, log.read_text()
    return run_gate


def test_gate_waits_for_the_pinned_serial_not_any(gate):
    # ה-RSP1A פנוי, אבל הראשי (RSP1B) תפוס ⇒ לא "מוכן"
    rc, _ = gate("rtl_airband.service", present=A, primary=B)
    assert rc == 1
    rc, _ = gate("rtl_airband.service", present=f"{A},{B}", primary=B)
    assert rc == 0


def test_gate_without_primary_accepts_any(gate):
    rc, _ = gate("rtl_airband.service", present=A)
    assert rc == 0


def test_gate_does_not_kick_sdrplay_while_other_consumer_runs(gate):
    # קול + ACARS: הראשי חסר בזמן שהמשני רץ ⇒ restart ל-sdrplay היה מפיל את המשני
    rc, calls = gate("airam-acars.service", present=A, primary=B, active="rtl_airband.service")
    assert rc == 1 and "restart" not in calls
    rc, calls = gate("airam-acars.service", present=A, primary=B)
    assert rc == 1 and "restart --no-block sdrplay.service" in calls


def test_gate_rejects_injected_serial_in_sdr_env(gate, tmp_path):
    # sdr.env שייך ל-airam ⇒ ערך לא-תקין לא נכנס לפקודה (נופל ל"כל מקלט")
    rc, _ = gate("rtl_airband.service", present=A, primary="x$(touch /tmp/pwn)")
    assert rc == 0
