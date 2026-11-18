# ============================================================================
#  AIR-AM — יומן בדיקה + ייצוא לוגים (/api/diag*, webtune/diag.py)
# ----------------------------------------------------------------------------
#  diag.py נבדק על טקסט קבוע של journald/systemctl. app.py נבדק עם _diag_cmd
#  מזויף (אין journalctl/systemctl ב-CI) וסנכרון דרך Event — בלי sleep.
# ============================================================================
import io
import json
import threading
import time
import zipfile

import pytest

import app
import appstate  # noqa: E402
import diag

ORIGIN = {"Origin": "http://localhost"}


def _j(t, msg, unit="", ident="", transport=None, raw_msg=None):
    o = {"__REALTIME_TIMESTAMP": str(int(t * 1e6)), "MESSAGE": raw_msg if raw_msg is not None else msg,
         "SYSLOG_IDENTIFIER": ident or unit or "kernel", "_PID": "1"}
    if unit:
        o["UNIT" if ident == "systemd" else "_SYSTEMD_UNIT"] = unit + ".service"
    if transport:
        o["_TRANSPORT"] = transport
    return json.dumps(o)


# --- diag.py: פענוח ----------------------------------------------------------

def test_parse_journal_json_tolerates_garbage_and_byte_messages():
    text = "\n".join([
        _j(100.5, "Started rtl_airband.service", unit="rtl_airband", ident="systemd"),
        "not json",
        "[1,2]",
        _j(101.0, "", unit="rtl_airband", raw_msg=list("שלום".encode("utf-8"))),
        json.dumps({"MESSAGE": "no timestamp"}),
    ])
    recs = diag.parse_journal_json(text)
    assert [r["t"] for r in recs] == [100.5, 101.0]
    assert recs[0]["unit"] == "rtl_airband" and recs[1]["msg"] == "שלום"


def test_unit_and_kernel_events():
    recs = diag.parse_journal_json("\n".join([
        _j(1, "Started airam-acars2.service", unit="airam-acars2", ident="systemd"),
        _j(2, "airam-acars2.service: Main process exited, code=exited, status=1/FAILURE",
           unit="airam-acars2", ident="systemd"),
        _j(3, "airam-acars2.service: Failed with result 'exit-code'.", unit="airam-acars2", ident="systemd"),
        _j(4, "rtl_airband.service: Main process exited, code=exited, status=0/SUCCESS",
           unit="rtl_airband", ident="systemd"),
        _j(5, "Traceback (most recent call last):\n  File x", unit="airam-web", ident="python3"),
    ]))
    counts, tl = diag.unit_events(recs)
    assert counts["airam-acars2"] == {"started": 1, "exited": 1, "failed": 1}
    assert "exited" not in counts.get("rtl_airband", {})       # יציאה נקייה אינה תקלה
    assert counts["airam-web"]["traceback"] == 1
    assert [k for _t, _u, k, _m in tl] == ["started", "exited", "failed", "traceback"]

    k = diag.kernel_events(diag.parse_journal_json("\n".join([
        _j(10, "hwmon hwmon1: Undervoltage detected!"),
        _j(14, "hwmon hwmon1: Voltage normalised"),
        _j(20, "usb 3-1: USB disconnect, device number 4"),
        _j(22, "usb 3-1: New USB device found, idVendor=1df7, idProduct=3010, bcdDevice= 2.00"),
        _j(23, "random noise"),
    ])))
    assert [t for t, _ in k["undervoltage"]] == [10]
    assert set(k) == {"undervoltage", "voltage_normal", "usb_disconnect", "usb_new"}


def test_parse_systemctl_show_blocks():
    text = ("Id=rtl_airband.service\nActiveState=active\nNRestarts=0\n\n"
            "Id=airam-acars2.service\nActiveState=failed\nResult=exit-code\nNRestarts=3\n")
    u = diag.parse_systemctl_show(text)
    assert u["rtl_airband"]["ActiveState"] == "active"
    assert u["airam-acars2"]["NRestarts"] == "3"


# --- diag.py: הבדיקות --------------------------------------------------------

def _levels(checks):
    return {c["title"]: c["level"] for c in checks}


def test_analyze_undervoltage_in_window_is_fail():
    checks = diag.analyze({"health": {"ok": True, "app_mode": "voice2"},
                           "kernel_events": {"undervoltage": [(time.time(), "Undervoltage detected!")]},
                           "power": {"undervolt_ever": True}})
    assert checks[0]["level"] == "fail" and "undervoltage" in checks[0]["title"]
    assert diag.worst(c["level"] for c in checks) == "fail"


def test_analyze_unread_kernel_log_is_not_reported_as_ok():
    checks = diag.analyze({"health": {"ok": True, "app_mode": "off"}, "kernel_events": None,
                           "kernel_error": "אין הרשאה"})
    lv = _levels(checks)
    assert any(lvl == "warn" and "הקרנל לא נקרא" in t for t, lvl in lv.items())
    assert not any("אין נפילת מתח" in t for t in lv)


def test_analyze_flag_since_boot_without_window_events_is_warn():
    checks = diag.analyze({"health": {"ok": True, "app_mode": "voice"}, "kernel_events": {},
                           "power": {"undervolt_ever": True}})
    assert any(c["level"] == "warn" and "מאז האתחול" in c["title"] for c in checks)


def test_analyze_failed_unit_start_limit_and_health():
    checks = diag.analyze({
        "health": {"ok": False, "app_mode": "voice_acars", "services": {"airam-acars2": "failed"}},
        "units": {"airam-acars2": {"ActiveState": "failed", "Result": "exit-code", "NRestarts": "2"}},
        "unit_events": {"airam-acars2": {"start_limit": 1, "failed": 2, "restart": 2}},
        "kernel_events": {}, "power": {"undervolt_ever": False}})
    fails = [c["title"] for c in checks if c["level"] == "fail"]
    assert any("voice_acars" in t for t in fails)
    assert any("failed עכשיו" in t for t in fails)
    assert any("תקרת ההפעלות" in t for t in fails)
    assert checks[0]["level"] == "fail"           # החמורות קודם


def test_analyze_voltage_is_reported_as_value_not_verdict():
    # §12: אין סף מתח מומצא — 4.6V מוצג כמידע, לא כאזהרה
    samples = [{"t": 1, "ok": True, "power": {"volts_in": 4.6, "temp": 70.0}},
               {"t": 2, "ok": True, "power": {"volts_in": 5.1, "temp": 71.5}}]
    checks = diag.analyze({"health": {"ok": True, "app_mode": "voice"}, "kernel_events": {},
                           "power": {}, "samples": samples})
    rng = [c for c in checks if "טווחי" in c["title"]][0]
    assert rng["level"] == "info" and "4.60–5.10V" in rng["detail"]
    assert diag.worst(c["level"] for c in checks) == "ok"


def test_timeline_and_summary_render():
    rows = [{"ev": "start", "t": 10, "label": "קול כפול"},
            {"ev": "sample", "t": 11, "mode": "off", "ok": True, "services": {"rtl_airband": "inactive"}},
            {"ev": "request", "t": 12, "method": "POST", "path": "/api/mode", "status": 500,
             "body": '{"mode": "voice2"}', "error": "נכשל"},
            {"ev": "sample", "t": 16, "mode": "voice2", "ok": True, "services": {"rtl_airband": "active"}},
            {"ev": "sample", "t": 21, "mode": "voice2", "ok": True, "services": {"rtl_airband": "active"}},
            {"ev": "mark", "t": 22, "label": "ניתקתי את המשני"}]
    tl, dropped = diag.build_timeline(rows, [(13, "rtl_airband2", "failed", "Failed with result")],
                                      {"undervoltage": [(14, "Undervoltage detected!")]})
    srcs = [s for _t, s, _x in tl]
    assert dropped == 0 and srcs.count("דגימה") == 2   # רק שינויים, לא כל דגימה
    assert "פעולה ❌" in srcs and "קרנל" in srcs and "📍 סימון" in srcs
    md = diag.render_summary({"version": "9.9.9", "start": 0, "end": 600, "created": 600,
                              "app_mode": "voice2", "test_label": "קול כפול", "test_active": False},
                             [{"level": "fail", "title": "a|b", "detail": "x"}], tl, 0,
                             [("summary.md", "הדוח")])
    assert md.startswith("# AIR-AM — דוח בדיקה (❌ תקלה)")
    assert "a\\|b" in md and "ניתקתי את המשני" in md and "9.9.9" in md


# --- app.py: הרשם -------------------------------------------------------------

@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(appstate, "STATE_PATH", tmp_path / "state.json")
    for name in ("DIAG_PATH", "ACARS_LOG_PATH", "VDL2_LOG_PATH", "SATCOM_LOG_PATH",
                 "ACTIVITY_PATH", "CONFIG_PATH", "VOICE2_CONFIG_PATH", "ACARS_ENV_PATH",
                 "ACARS2_ENV_PATH", "VDL2_ENV_PATH", "VDL2B_ENV_PATH", "SATCOM_ENV_PATH",
                 "SDR_ENV_PATH", "DENOISE_ENV_PATH", "RFLOG_PATH"):
        monkeypatch.setattr(app, name, tmp_path / getattr(app, name).name)
    monkeypatch.setattr(app, "_health_snapshot", lambda: {
        "ok": True, "app_mode": "voice", "services": {"rtl_airband": "active"},
        "sdr_present": True, "stats_age": 1.0, "denoise": {"state": "off"}})
    monkeypatch.setattr(app, "_power_cached", lambda: {"throttled": "0x0", "undervolt_now": False,
                                                       "volts_in": 5.08, "temp": 55.0})
    monkeypatch.setattr(app, "_diag_fsync", lambda fd: None)
    yield tmp_path
    app._diag_stop("test-teardown")
    with app._diag_lock:
        app._diag.update(active=False, started_at=None, until=None, label="", samples=0, marks=0,
                         requests=0, write_errors=0, thread=None, stop=None, last_run=None)


def _rows(paths):
    f = paths / "diag.jsonl"
    return [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines()] if f.exists() else []


def _wait_first_sample(monkeypatch):
    got = threading.Event()
    real = app._diag_sample

    def sample():
        try:
            return real()
        finally:
            got.set()
    monkeypatch.setattr(app, "_diag_sample", sample)
    return got


def test_start_sample_mark_stop(client, paths, monkeypatch):
    got = _wait_first_sample(monkeypatch)
    r = client.post("/api/diag", json={"action": "start", "label": "קול כפול על power bank"}, headers=ORIGIN)
    assert r.status_code == 200 and r.json["active"] is True
    assert got.wait(5)
    assert client.post("/api/diag", json={"action": "start"}, headers=ORIGIN).status_code == 409
    r = client.post("/api/diag/mark", json={"label": "ניתקתי את המשני"}, headers=ORIGIN)
    assert r.status_code == 200 and r.json["marks"] == 1
    r = client.post("/api/diag", json={"action": "stop"}, headers=ORIGIN)
    assert r.json["active"] is False and r.json["last_run"]["label"] == "קול כפול על power bank"
    evs = [x["ev"] for x in _rows(paths)]
    assert evs[0] == "start" and "sample" in evs and "mark" in evs and evs[-1] == "stop"
    s = next(x for x in _rows(paths) if x["ev"] == "sample")
    assert s["mode"] == "voice" and s["power"]["volts_in"] == 5.08


def test_mark_without_test_is_409(client):
    assert client.post("/api/diag/mark", json={"label": "x"}, headers=ORIGIN).status_code == 409
    assert client.post("/api/diag", json={"action": "bogus"}, headers=ORIGIN).status_code == 400


def test_requests_recorded_only_while_active_without_secrets(client, paths, monkeypatch):
    monkeypatch.setattr(app, "_diag_worker", lambda evt: None)       # בלי דגימות — רק בקשות
    client.post("/api/rflog/mark", json={"label": "לפני"}, headers=ORIGIN)   # לא פעיל ⇒ לא נרשם
    client.post("/api/diag", json={"action": "start"}, headers=ORIGIN)
    client.post("/api/rflog/mark", json={"label": "אחרי"}, headers={**ORIGIN, "X-AIRAM-PIN": "1234"})
    client.post("/api/xcheck/verify", json={"src": "acars", "trace_b64": "A" * 5000}, headers=ORIGIN)
    req = [x for x in _rows(paths) if x["ev"] == "request"]
    assert [x["path"] for x in req] == ["/api/rflog/mark", "/api/xcheck/verify"]
    assert req[0]["status"] == 409 and req[0]["error"] and "אחרי" in req[0]["body"]
    assert "1234" not in json.dumps(req, ensure_ascii=False)               # header לא נרשם
    assert "AAAA" not in req[1]["body"] and "5000" in req[1]["body"]      # trace מוחלף בגודלו
    assert not any(x.get("path", "").startswith("/api/diag") for x in req)


def test_boot_resumes_unfinished_test(paths, monkeypatch):
    monkeypatch.setattr(app, "_diag_worker", lambda evt: None)
    t0 = time.time() - 600
    (paths / "diag.jsonl").write_text(json.dumps({"ev": "start", "t": t0, "label": "reboot test"}) + "\n"
                                      + json.dumps({"ev": "sample", "t": t0 + 5}) + "\n")
    app._diag_boot()
    st = app._diag_status()
    assert st["active"] and st["label"] == "reboot test" and st["started_at"] == t0
    assert _rows(paths)[-1]["ev"] == "resume"


def test_boot_keeps_last_run_for_finished_or_expired(paths):
    t0 = time.time() - app.DIAG_MAX_SEC - 100          # לא הסתיים, אבל פג — לא ממשיכים
    (paths / "diag.jsonl").write_text(json.dumps({"ev": "start", "t": t0, "label": "ישן"}) + "\n"
                                      + json.dumps({"ev": "sample", "t": t0 + 50}) + "\n")
    app._diag_boot()
    st = app._diag_status()
    assert not st["active"] and st["last_run"] == {"started_at": t0, "ended_at": t0 + 50, "label": "ישן"}


# --- ייצוא ----------------------------------------------------------------------

def _fake_cmd(now):
    def cmd(args, timeout=15):
        if args[0] == "journalctl":
            if "_TRANSPORT=kernel" in args:
                return _j(now - 30, "hwmon hwmon1: Undervoltage detected!") + "\n", "", 0
            return "\n".join([
                _j(now - 40, "Started rtl_airband2.service", unit="rtl_airband2", ident="systemd"),
                _j(now - 20, "INFO mode -> VOICE2", unit="airam-web", ident="python3"),
            ]) + "\n", "", 0
        if args[0] == "systemctl":
            return "Id=rtl_airband.service\nActiveState=active\nNRestarts=0\n", "", 0
        return "Bus 003 Device 004: ID 1df7:3010\n", "", 0
    return cmd


def test_export_zip_contents(client, paths, monkeypatch):
    now = time.time()
    monkeypatch.setattr(app, "_diag_cmd", _fake_cmd(now))
    monkeypatch.setattr(app, "_sdr_status", lambda: {"state": "ours", "usb_count": 2})
    monkeypatch.setattr(app, "_read_power", lambda: {"undervolt_ever": True, "throttled": "0x50000"})
    (paths / "state.json").write_text(json.dumps(
        {"app_mode": "voice2", "station": {"lat": 32.123456, "lon": 34.987654, "alt_m": 50}}))
    (paths / "airband2.conf").write_text("devices: ()\n")
    (paths / "acars.jsonl").write_text(json.dumps({"t": now - 10, "tail": "4X-EHD"}) + "\n"
                                       + json.dumps({"t": now - 99999, "tail": "OLD"}) + "\n")
    r = client.get("/api/diag/export?minutes=10")
    assert r.status_code == 200 and r.mimetype == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.data))
    names = {n.split("/", 1)[1] for n in z.namelist()}
    assert {"summary.md", "snapshot.json", "logs/services.log", "logs/kernel.log", "logs/airam-web.log",
            "logs/rtl_airband2.log", "test-log.jsonl", "messages.json", "system.txt",
            "config/airband2.conf"} <= names
    assert not any("airam.env" in n for n in names)
    root = z.namelist()[0].split("/")[0]
    summary = z.read(f"{root}/summary.md").decode()
    assert "❌" in summary and "undervoltage" in summary
    snap = json.loads(z.read(f"{root}/snapshot.json"))
    assert snap["state"]["station"]["lat"] == 32.12                       # ~1 ק"מ, לא מדויק
    msgs = json.loads(z.read(f"{root}/messages.json"))
    assert msgs["counts"]["acars"] == 1


def test_export_default_window_is_last_test(client, paths, monkeypatch):
    t0 = time.time() - 3000
    with app._diag_lock:
        app._diag["last_run"] = {"started_at": t0, "ended_at": t0 + 600, "label": "בדיקה"}
    s, e, test = app._diag_window({})
    assert s == t0 - app.DIAG_PRE_SEC and e == t0 + 600 + app.DIAG_PRE_SEC and test["label"] == "בדיקה"
    s, e, test = app._diag_window({"minutes": "99999"})
    assert test is None and round(e - s) == app.DIAG_EXPORT_MAX_MIN * 60


def test_export_survives_missing_tools(client, paths, monkeypatch):
    # בלי journalctl/systemctl (או בלי הרשאה) — עדיין חבילה, והדוח אומר מה לא נבדק
    monkeypatch.setattr(app, "_diag_cmd", lambda args, timeout=15: (None, f"{args[0]} לא מותקן", None))
    monkeypatch.setattr(app, "_sdr_status", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(app, "_read_power", lambda: None)
    r = client.get("/api/diag/summary?minutes=5")
    assert r.status_code == 200
    titles = " ".join(c["title"] for c in r.json["checks"])
    assert "הקרנל לא נקרא" in titles and "חלק מהנתונים לא נאסף" in titles
    assert r.json["overall"] == "warn"
