# ============================================================================
#  AIR-AM - חיווי SDR: מזוהה? פנוי? (GET /api/sdr)
# ----------------------------------------------------------------------------
#  "מזוהה" = lsusb רואה vendor 1df7. "פנוי" = `SoapySDRUtil --find` מחזיר מכשיר
#  (sdrplay_api_GetDevices לא מחזיר מכשיר שלקוח אחר כבר בחר — ר' app.py).
#  בלי חומרה: subprocess.run ממוקף לפי הפקודה.
# ============================================================================
import threading
import types

import pytest

import app

FIND_OK = """######################################################
##     Soapy SDR -- the SDR abstraction library     ##
######################################################

Found device 0
  driver = sdrplay
  label = SDRplay Dev0 RSP1B 2305012345
  serial = 2305012345

"""
FIND_API_ERR = "[ERROR] sdrplay_api_Open() Error: sdrplay_api_ServiceNotResponding\n"
IDLE = {"sdrplay": "active", "rtl_airband": "inactive", "airam-acars": "inactive",
        "airam-vdl2": "inactive", "airam-satcom": "inactive"}


@pytest.fixture(autouse=True)
def _fresh_probe_cache():
    app._sdr_probe_cache.update(t=0.0, result=None)
    yield
    app._sdr_probe_cache.update(t=0.0, result=None)


@pytest.fixture
def client():
    return app.app.test_client()


def _fake_run(lsusb=(0, "Bus 001 Device 004: ID 1df7:3300 SDRplay RSP1B\n"),
              find=(0, FIND_OK, ""), services=IDLE, calls=None):
    """subprocess.run מדומה: עונה לפי הפקודה (lsusb / SoapySDRUtil / systemctl)."""
    def run(cmd, **kw):
        if calls is not None:
            calls.append(cmd[0])
        if cmd[0] == "lsusb":
            if isinstance(lsusb, BaseException):
                raise lsusb
            return types.SimpleNamespace(returncode=lsusb[0], stdout=lsusb[1], stderr="")
        if cmd[0] == "SoapySDRUtil":
            if isinstance(find, BaseException):
                raise find
            return types.SimpleNamespace(returncode=find[0], stdout=find[1], stderr=find[2])
        if cmd[:2] == ["systemctl", "is-active"]:
            out = "\n".join(services.get(n, "inactive") for n in cmd[2:]) + "\n"
            return types.SimpleNamespace(returncode=3, stdout=out, stderr="")
        raise AssertionError(f"unexpected command {cmd}")
    return run


# --- "מזוהה" ------------------------------------------------------------------

def test_usb_present_with_model_text(monkeypatch):
    monkeypatch.setattr(app.subprocess, "run", _fake_run())
    assert app._sdr_usb() == (True, "SDRplay RSP1B")


def test_usb_present_without_name_falls_back_to_id(monkeypatch):
    # usbutils ישן בלי שם מוצר ל-1df7:3300 — מציגים את ה-ID, לא ממציאים דגם
    monkeypatch.setattr(app.subprocess, "run",
                        _fake_run(lsusb=(0, "Bus 001 Device 004: ID 1df7:3300\n")))
    assert app._sdr_usb() == (True, "1df7:3300")


def test_usb_absent(monkeypatch):
    monkeypatch.setattr(app.subprocess, "run", _fake_run(lsusb=(1, "")))
    assert app._sdr_usb() == (False, None)


def test_usb_unknown_when_lsusb_missing(monkeypatch):
    """§12: בלי lsusb לא אומרים "מזוהה" (בניגוד ל-_sdr_present שמניח True לרולבק)."""
    monkeypatch.setattr(app.subprocess, "run", _fake_run(lsusb=FileNotFoundError()))
    assert app._sdr_usb() == (None, None)


# --- ה-probe ------------------------------------------------------------------

def test_probe_found_parses_label(monkeypatch):
    monkeypatch.setattr(app.subprocess, "run", _fake_run())
    assert app._sdr_probe_api() == {"result": "found", "label": "SDRplay Dev0 RSP1B 2305012345"}


def test_probe_none_is_not_an_api_error(monkeypatch):
    monkeypatch.setattr(app.subprocess, "run",
                        _fake_run(find=(1, "", "No devices found! driver=sdrplay\n")))
    assert app._sdr_probe_api() == {"result": "none"}


def test_probe_api_error_is_distinguished_from_busy(monkeypatch):
    """daemon תקוע ≠ תוכנה אחרת מחזיקה — טיפול שונה לגמרי, חיווי שונה."""
    monkeypatch.setattr(app.subprocess, "run", _fake_run(find=(1, "", FIND_API_ERR)))
    r = app._sdr_probe_api()
    assert r["result"] == "api_error" and "ServiceNotResponding" in r["detail"]


def test_probe_timeout_and_missing_tool(monkeypatch):
    monkeypatch.setattr(app.subprocess, "run",
                        _fake_run(find=app.subprocess.TimeoutExpired("SoapySDRUtil", 12)))
    assert app._sdr_probe_api() == {"result": "timeout"}
    monkeypatch.setattr(app.subprocess, "run", _fake_run(find=FileNotFoundError()))
    assert app._sdr_probe_api() == {"result": "no_tool"}


# --- המצב המלא ------------------------------------------------------------------

def test_missing_does_not_probe(client, monkeypatch):
    calls = []
    monkeypatch.setattr(app.subprocess, "run", _fake_run(lsusb=(1, ""), calls=calls))
    d = client.get("/api/sdr").get_json()
    assert d["state"] == "missing" and d["usb"] is False
    assert calls == ["lsusb"]


def test_ours_when_consumer_active_and_no_probe(client, monkeypatch):
    calls = []
    monkeypatch.setattr(app.subprocess, "run",
                        _fake_run(services={**IDLE, "airam-vdl2": "active"}, calls=calls))
    d = client.get("/api/sdr").get_json()
    assert d["state"] == "ours" and d["mode"] == "vdl2" and d["service_state"] == "active"
    assert "SoapySDRUtil" not in calls        # התשובה ידועה — לא נוגעים ב-API


def test_ours_includes_restart_loop(client, monkeypatch):
    """Restart=always בלולאה = "activating": המכשיר עדיין לא פנוי לאחרים."""
    monkeypatch.setattr(app.subprocess, "run",
                        _fake_run(services={**IDLE, "rtl_airband": "activating"}))
    d = client.get("/api/sdr").get_json()
    assert d["state"] == "ours" and d["mode"] == "voice" and d["service_state"] == "activating"


def test_api_down(client, monkeypatch):
    monkeypatch.setattr(app.subprocess, "run", _fake_run(services={**IDLE, "sdrplay": "failed"}))
    d = client.get("/api/sdr").get_json()
    assert d["state"] == "api_down" and d["api"] == "failed"


def test_switching_while_tune_lock_held(client, monkeypatch):
    calls = []
    monkeypatch.setattr(app.subprocess, "run", _fake_run(calls=calls))
    assert app.TUNE_LOCK.acquire(blocking=False)
    try:
        d = client.get("/api/sdr").get_json()
    finally:
        app.TUNE_LOCK.release()
    assert d["state"] == "switching" and "SoapySDRUtil" not in calls


def test_free(client, monkeypatch):
    monkeypatch.setattr(app.subprocess, "run", _fake_run())
    d = client.get("/api/sdr").get_json()
    assert d["state"] == "free" and d["usb"] is True
    assert d["label"] == "SDRplay Dev0 RSP1B 2305012345" and d["checked_age"] is not None


def test_busy_lists_suspects(client, monkeypatch, tmp_path):
    for pid, name in ((101, "sdrpp"), (102, "bash"), (103, "rtl_airband")):
        (tmp_path / str(pid)).mkdir()
        (tmp_path / str(pid) / "comm").write_text(name + "\n")
    (tmp_path / "self").mkdir()
    real = app._sdr_suspects
    monkeypatch.setattr(app, "_sdr_suspects", lambda: real(str(tmp_path)))
    monkeypatch.setattr(app.subprocess, "run", _fake_run(find=(1, "", "No devices found!\n")))
    d = client.get("/api/sdr").get_json()
    assert d["state"] == "busy"
    assert d["suspects"] == [{"pid": 101, "name": "sdrpp"}, {"pid": 103, "name": "rtl_airband"}]


def test_not_found_without_lsusb_is_not_called_busy(client, monkeypatch):
    monkeypatch.setattr(app.subprocess, "run",
                        _fake_run(lsusb=FileNotFoundError(), find=(1, "", "")))
    d = client.get("/api/sdr").get_json()
    assert d["state"] == "unavailable" and d["usb"] is None


def test_api_error_and_unknown(client, monkeypatch):
    monkeypatch.setattr(app.subprocess, "run", _fake_run(find=(1, "", FIND_API_ERR)))
    d = client.get("/api/sdr").get_json()
    assert d["state"] == "api_error" and "ServiceNotResponding" in d["detail"]
    app._sdr_probe_cache.update(t=0.0, result=None)
    monkeypatch.setattr(app.subprocess, "run", _fake_run(find=FileNotFoundError()))
    assert client.get("/api/sdr").get_json()["state"] == "unknown"


def test_consumer_started_during_probe_is_ours_not_foreign(client, monkeypatch):
    """מעבר-מצב מטלפון שני בזמן ה-probe: find ריק כי *אנחנו* תפסנו אותו."""
    state = {"n": 0}

    def run(cmd, **kw):
        if cmd[:2] == ["systemctl", "is-active"]:
            state["n"] += 1
            svc = {**IDLE, "airam-acars": "active"} if state["n"] > 1 else IDLE
            out = "\n".join(svc.get(n, "inactive") for n in cmd[2:]) + "\n"
            return types.SimpleNamespace(returncode=3, stdout=out, stderr="")
        return _fake_run(find=(1, "", "No devices found!\n"))(cmd, **kw)
    monkeypatch.setattr(app.subprocess, "run", run)
    d = client.get("/api/sdr").get_json()
    assert d["state"] == "ours" and d["mode"] == "acars" and d["suspects"] == []


def test_probe_is_cached(client, monkeypatch):
    calls = []
    monkeypatch.setattr(app.subprocess, "run", _fake_run(calls=calls))
    client.get("/api/sdr")
    client.get("/api/sdr")
    assert calls.count("SoapySDRUtil") == 1
    app._sdr_probe_cache["t"] -= app.SDR_PROBE_TTL_SEC + 1
    client.get("/api/sdr")
    assert calls.count("SoapySDRUtil") == 2


def test_concurrent_request_does_not_wait_for_running_probe(client, monkeypatch):
    """probe תקוע (daemon לא עונה) לא תוקע בקשה שנייה עד ה-timeout."""
    started, release = threading.Event(), threading.Event()
    base = _fake_run()

    def slow(cmd, **kw):
        if cmd[0] == "SoapySDRUtil":
            started.set()
            release.wait(5)
        return base(cmd, **kw)
    monkeypatch.setattr(app.subprocess, "run", slow)
    t = threading.Thread(target=lambda: app.app.test_client().get("/api/sdr"))
    t.start()
    assert started.wait(5)
    try:
        d = client.get("/api/sdr").get_json()
        assert d["state"] == "checking"
    finally:
        release.set()
        t.join(5)
    assert client.get("/api/sdr").get_json()["state"] == "free"
