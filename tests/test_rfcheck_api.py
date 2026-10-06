# ============================================================================
#  AIR-AM - 🩺 בדיקת RF: המתזמר ב-app.py (/api/rfcheck) — docs/rf-check-design.md
# ----------------------------------------------------------------------------
#  בלי חומרה ובלי numpy: systemd ממוקף ע"י "עולם" מדומה, ו"בודק" מדומה (thread) כותב
#  את קובצי הפלט (meta/status/rows/end/diagnose) לתיקיית run זמנית — בדיוק החוזה של
#  rfcheck_probe.py (spec §4.3). שכבת הניתוח מוחלפת ב-stub שליטה (כך שכל מסלול-יציאה של
#  הבקר נבדק בנפרד מהסטטיסטיקה), חוץ מבדיקת-עשן אחת מול rfcheck_analysis האמיתי.
#
#  האינווריאנטה המרכזית (spec §11.2): **בכל** מסלול יציאה — הבודק נעצר,
#  `_restore_after_probe` נקרא בדיוק פעם אחת, TUNE_LOCK משוחרר, ו-app_mode ב-state.json
#  לא משתנה. סנכרון דרך join/Event, לא sleep קבוע.
# ============================================================================
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

import app

ROOT = Path(__file__).resolve().parent.parent
RUN_A = "0123456789abcdef"


def _ok(rc=0, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)


# --- עולם מדומה: systemd + הבודק ---------------------------------------------------

class FakeProbe:
    """thread שמתנהג כמו rfcheck_probe.py מול OUT_DIR: מוחק פלטים ישנים, status "opening",
    meta, שורות + status (פעימה) לכל סלוט, ו-end.json בעצירה/יציאה."""

    OUT = ("meta.json", "status.json", "rows.jsonl", "end.json", "diagnose.json")

    def __init__(self, world, params):
        self.w, self.p = world, params
        self.run_id = params["run_id"]
        self.dir = world.run_dir
        self.rows = 0
        self.alive = True
        self._stop = threading.Event()
        self.th = threading.Thread(target=self._run, daemon=True)

    def _write(self, name, obj):
        tmp = self.dir / (name + ".tmp")
        tmp.write_text(json.dumps(obj))
        os.replace(tmp, self.dir / name)

    def start(self):
        for n in self.OUT:
            try:
                (self.dir / n).unlink()
            except FileNotFoundError:
                pass
        self.th.start()

    def _row(self, i):
        states = self.p["states"]
        cyc = i // len(states)
        order = states if cyc % 2 == 0 else list(reversed(states))
        lna = order[i % len(states)]
        return {"run_id": self.run_id, "i": i, "cyc": cyc, "dir": "f" if cyc % 2 == 0 else "r",
                "lna": lna, "ifgr": self.p["ifgr_start"][str(lna)], "epoch": 0,
                "notch": self.p["notch_base"], "valid": True, "inv": None}

    def _run(self):
        w = self.w
        try:
            self._write("status.json", {"run_id": self.run_id, "t_wall": time.time(), "phase": "opening"})
            if w.open_hang:
                self._stop.wait(30)
                return self._end("stopped")
            self._write("meta.json", {"run_id": self.run_id, "phase": self.p["phase"],
                                      "ref": self.p["ref"], "freq_hz": self.p["freq_hz"],
                                      "states": self.p["states"], "handler": True,
                                      "telemetry_marker": False, "gr_table": None})
            self._write("status.json", {"run_id": self.run_id, "t_wall": time.time(),
                                        "phase": "streaming", "slot": 0, "telemetry_lines": 0})
            with open(self.dir / "rows.jsonl", "a") as f:
                while not self._stop.wait(w.row_interval):
                    if w.exit_after is not None and self.rows >= w.exit_after:
                        return self._end(*w.exit_with)
                    if self.p["phase"] == "diagnose" and self.rows >= w.diag_rows:
                        self._write("diagnose.json", {
                            "run_id": self.run_id, "complete": True, "meta": {}, "steps": {},
                            "suggested": {}, "summary_text": "AIRAM-RFDIAG v1\nrun_id=%s\nok=true"
                                                             % self.run_id})
                        return self._end("stopped")
                    if self.p["phase"] != "diagnose" and self.rows < w.max_rows:
                        f.write(json.dumps(self._row(self.rows)) + "\n")
                        f.flush()
                    self.rows += 1
                    if not w.stall:
                        self._write("status.json", {"run_id": self.run_id, "t_wall": time.time(),
                                                    "phase": "streaming", "slot": self.rows,
                                                    "telemetry_lines": 0,
                                                    "diag_step": "D3" if self.p["phase"] == "diagnose"
                                                    else None})
            self._end("stopped")
        finally:
            self.alive = False

    def _end(self, ended, error=None):
        self._write("end.json", {"run_id": self.run_id, "ended": ended, "error": error,
                                 "slots": self.rows, "telemetry_lines": 0})
        self.alive = False

    def stop(self):
        self._stop.set()
        self.th.join(timeout=5)


class World:
    """systemd מדומה: rtl_airband (קול) ו-airam-rfcheck (הבודק). Conflicts: הפעלת הבודק
    עוצרת את הקול. ‏_restore_after_probe ממוקף וסופר קריאות."""

    def __init__(self, run_dir, params_path):
        self.run_dir, self.params_path = run_dir, params_path
        self.live = "voice"
        self.calls, self.params_seen, self.params_modes, self.restores = [], [], [], []
        self.probe, self.restart_rc = None, 0
        self.restore_ok = True
        self.row_interval, self.max_rows = 0.004, 10 ** 6
        self.exit_after, self.exit_with = None, ("error", "device_lost")
        self.stall = self.open_hang = False
        self.diag_rows = 5
        self.start_wall = None
        self.lock_held_at_restart = []

    # systemd
    def sysctl(self, action, svc, timeout=45):
        self.calls.append((action, svc))
        if svc == "rtl_airband" and action == "stop" and self.live == "voice":
            self.live = None
        if svc == app.RFCHECK_SERVICE and action == "restart":
            self.lock_held_at_restart.append(app.TUNE_LOCK.locked())
            if self.probe:
                self.probe.stop()
            self.params_modes.append(stat.S_IMODE(os.stat(self.params_path).st_mode))
            params = json.loads(self.params_path.read_text())
            self.params_seen.append(params)
            if self.restart_rc:
                return _ok(self.restart_rc, stderr="Job failed")
            if self.live == "voice":
                self.live = None                   # Conflicts=rtl_airband
            self.probe = FakeProbe(self, params)
            self.probe.start()
        if svc == app.RFCHECK_SERVICE and action == "stop" and self.probe:
            self.probe.stop()
        return _ok()

    def services(self, names):
        return {n: ("active" if (n == app.RFCHECK_SERVICE and self.probe and self.probe.alive)
                    or (n == "rtl_airband" and self.live == "voice") else "inactive")
                for n in names}

    def is_active(self, svc):
        return self.services((svc,))[svc] == "active"

    def restore(self, prev, prev_live):
        self.restores.append((prev, prev_live))
        self.live = prev_live if self.restore_ok else None


class StubAnalysis:
    """stub לשכבת הניתוח: ממשק זהה (analyze/live_summary/stop_reason/offer_atis +
    הקבועים שהבקר קורא), התנהגות בשליטת הבדיקה."""
    GR_RSP1B_60_420 = (0, 6, 12, 18, 20, 26, 32, 38, 57, 62)
    ATIS_MAX_SEC = 30

    def __init__(self):
        self.target_rows = 6
        self.offer = False
        self.level = "stat"
        self.rec = {"rf_gain": 6, "if_gain": None, "fm_notch": None, "basis": "stat",
                    "reasons": [], "changes": True}
        self.apply_allowed = True
        self.analyze_calls = []
        self.raise_in_live = False

    def live_summary(self, rows, ctx):
        if self.raise_in_live:
            raise RuntimeError("boom")
        return {"phase": ctx["phase"], "ref": ctx["ref"], "rows": len(rows), "valid": len(rows),
                "n_carrier_rows": 0, "states": [{"lna": s, "label": f"{9 - s}/9"} for s in ctx["states"]],
                "tx_captured": 0, "tx_target": 3, "full_blocks": 0, "blocks_target": 12,
                "last_slot": "silence", "current_state": rows[-1]["lna"] if rows else None,
                "progress": 0.0, "level": "none", "no_traffic_sec": 0.0}

    def offer_atis(self, summary, ctx, elapsed):
        return self.offer and ctx["ref"] == "tower"

    def stop_reason(self, summary, ctx, elapsed):
        return "target" if summary["rows"] >= self.target_rows else None

    def analyze(self, rows, meta, end, ctx):
        self.analyze_calls.append({"rows": list(rows), "ctx": dict(ctx), "end": end})
        tower = ctx.get("tower") or {}
        return {"v": 1, "run_id": ctx["run_id"], "parent_id": ctx.get("parent_id"),
                "phase": ctx["phase"], "ref": "tower+atis" if tower else ctx["ref"],
                "freq": ctx["freq"], "atis_freq": ctx.get("atis_freq"),
                "config_at_start": ctx["config_at_start"], "ended": ctx.get("ended"),
                "level": self.level, "headline": "reduce_gain_tie",
                "recommendation": dict(self.rec) if self.rec else None,
                "apply_allowed": self.apply_allowed, "states": ctx["states"],
                "per_state": [{"lna": s, "ifgr_final": 45} for s in ctx["states"]],
                "error": ctx.get("error"), "rows_seen": len(rows),
                "tower_rows": len(tower.get("rows") or []),
                "selfcheck": {"telemetry": "absent", "verified": False}}


AVAILABLE = {"available": True, "reasons": [], "install_hint": "sudo ./install.sh",
             "telemetry_expected": False, "verified": False, "selftest": {"numpy": "2.0"}}


@pytest.fixture
def paths(tmp_path, monkeypatch):
    for name, fn in (("CONFIG_PATH", "airband.conf"), ("STATE_PATH", "state.json"),
                     ("STATS_PATH", "stats.txt"), ("RFLOG_PATH", "rf_log.jsonl"),
                     ("ACARS_ENV_PATH", "acars.env"), ("VDL2_ENV_PATH", "vdl2.env"),
                     ("SATCOM_ENV_PATH", "satcom.env"),
                     ("RFCHECK_PARAMS_PATH", "rfcheck-params.json"),
                     ("RFCHECK_LAST_PATH", "rfcheck_last.json"),
                     ("RFCHECK_LAST_ROWS_PATH", "rfcheck_last_rows.jsonl"),
                     ("RFCHECK_HISTORY_PATH", "rfcheck_history.jsonl"),
                     ("RFCHECK_DIAG_PATH", "rfcheck_diagnose.json"),
                     ("SOAPY_RF_MARK", "no-such-build-sig"),
                     ("SDRPLAY_API_VERSION_PATH", "sdrplay-api.version")):
        monkeypatch.setattr(app, name, tmp_path / fn)
    run = tmp_path / "run"
    run.mkdir()
    monkeypatch.setattr(app, "RFCHECK_RUN_DIR", run)
    return tmp_path


@pytest.fixture
def client(paths):
    return app.app.test_client()


@pytest.fixture(autouse=True)
def _clean(paths):
    yield
    with app._rfc_lock:
        th, stop, wake = app._rfc["thread"], app._rfc["stop_evt"], app._rfc["wake"]
    if stop:
        stop.set()
    if wake:
        wake.set()
    if th:
        th.join(timeout=10)
    with app._rfc_lock:
        app._rfc.update(running=False, run_id=None, parent_id=None, kind=None, stage=None,
                        ref=None, freq=None, states=[], result=None, diagnose=None, error=None,
                        error_code=None, ended=None, restore=None, live=None, probe=None,
                        stop_evt=None, finish_evt=None, atis_evt=None, wake=None, thread=None,
                        started_at=None, finished_at=None, config_at_start=None)
    app._rfc_last_cache.update(sig=None, result=None)
    app._rfc_avail.update(t=None, result=None)
    app._rflog_stop("test-teardown")
    with app._PIN_FAIL_LOCK:
        app._pin_fails.clear()          # בדיקת PIN שגוי לא דולפת ל-rate-limit של בדיקות אחרות
    if app.TUNE_LOCK.locked():
        app.TUNE_LOCK.release()


@pytest.fixture
def stub(monkeypatch):
    s = StubAnalysis()
    monkeypatch.setattr(app, "rfcheck_analysis", s)
    return s


@pytest.fixture
def world(paths, stub, monkeypatch):
    w = World(app.RFCHECK_RUN_DIR, app.RFCHECK_PARAMS_PATH)
    monkeypatch.setattr(app, "_sysctl", w.sysctl)
    monkeypatch.setattr(app, "_services_status", w.services)
    monkeypatch.setattr(app, "_is_active", w.is_active)
    monkeypatch.setattr(app, "_live_mode", lambda: w.live)
    monkeypatch.setattr(app, "_restore_after_probe", w.restore)
    monkeypatch.setattr(app, "_journal_tail", lambda svc="rtl_airband", lines=8: "JOURNAL " + svc)
    monkeypatch.setattr(app, "_rtl_airband_start_wall", lambda: w.start_wall)
    monkeypatch.setattr(app, "_sdr_present", lambda: True)
    monkeypatch.setattr(app, "_rfcheck_availability", lambda force=False: dict(AVAILABLE))
    for k, v in (("RFCHECK_POLL_SEC", 0.01), ("RFCHECK_STOP_WAIT_SEC", 0.2)):
        monkeypatch.setattr(app, k, v)
    app.save_state({**app.DEFAULT_STATE, "app_mode": "voice", "freq": 134.6, "mod": "am",
                    "agc": True, "rf_gain": 4, "if_gain": 40, "fm_notch": False,
                    "squelch_mode": "auto"})
    return w


def _post(client, **body):
    r = client.post("/api/rfcheck", json=body)
    return r.status_code, r.get_json()


def _wait_done(timeout=10):
    with app._rfc_lock:
        th = app._rfc["thread"]
    if th:
        th.join(timeout=timeout)
        assert not th.is_alive(), "ה-thread של הבדיקה לא הסתיים"


def _wait_for(pred, timeout=5):
    t_end = time.time() + timeout
    while time.time() < t_end:
        if pred():
            return True
        time.sleep(0.01)
    return False


def _assert_clean_exit(world, prev_live="voice", app_mode="voice"):
    """האינווריאנטה של spec §11.2 — בכל מסלול: הבודק נעצר, שחזור *בדיוק פעם אחת* אל מה
    שרץ קודם, TUNE_LOCK משוחרר, הכוונה השמורה (app_mode) לא השתנתה."""
    _wait_done()
    assert len(world.restores) == 1, world.restores
    assert world.restores[0][1] == prev_live
    assert not app.TUNE_LOCK.locked()
    assert ("stop", app.RFCHECK_SERVICE) in world.calls
    assert app.load_state()["app_mode"] == app_mode
    assert not app.RFCHECK_PARAMS_PATH.exists(), "קובץ הפרמטרים נשאר אחרי הריצה"
    with app._rfc_lock:
        assert app._rfc["running"] is False


# --- סירובים (spec §6.5) ---------------------------------------------------------

def test_refuse_when_already_running(client, world, stub):
    stub.target_rows = 10 ** 9
    code, _ = _post(client, action="start")
    assert code == 200
    code, body = _post(client, action="start")
    assert code == 409 and "כבר רצה" in body["error"]
    _post(client, action="abort")
    _assert_clean_exit(world)


def test_refuse_unavailable_501_with_reasons(client, world, monkeypatch):
    monkeypatch.setattr(app, "_rfcheck_availability", lambda force=False: {
        **AVAILABLE, "available": False, "reasons": ["numpy_missing"]})
    code, body = _post(client, action="start")
    assert code == 501
    assert body["reasons"] == ["numpy_missing"] and body["install_hint"] == "sudo ./install.sh"
    assert world.calls == [] and not app.TUNE_LOCK.locked()


def test_refuse_when_experiment_running(client, world):
    with app._exp_lock:
        app._exp["running"] = True
    try:
        code, body = _post(client, action="start")
    finally:
        with app._exp_lock:
            app._exp["running"] = False
    assert code == 409 and "ניסוי" in body["error"]


def test_refuse_when_scan_thread_alive(client, world, monkeypatch):
    evt = threading.Event()
    th = threading.Thread(target=evt.wait, daemon=True)
    th.start()
    monkeypatch.setattr(app, "_scan_thread", th)
    try:
        code, body = _post(client, action="start")
    finally:
        evt.set()
        th.join()
    assert code == 409 and "סריקה" in body["error"]


@pytest.mark.parametrize("live", [None, "acars", "vdl2", "satcom"])
def test_refuse_lna_when_voice_not_live(client, world, live):
    world.live = live
    code, body = _post(client, action="start")
    assert code == 409 and body["error"] == "בדיקת RF זמינה במצב קול בלבד"
    assert world.calls == []


def test_refuse_diagnose_from_acars(client, world):
    world.live = "acars"
    app.save_state({**app.load_state(), "app_mode": "acars"})
    code, body = _post(client, action="start", phase="diagnose")
    assert code == 409 and body["error"] == "אבחון זמין ממצב קול או כבוי בלבד"


@pytest.mark.parametrize("freq,mod", [(150.0, "am"), (100.0, "am"), (134.6, "nfm")])
def test_refuse_outside_airband_am(client, world, freq, mod):
    app.save_state({**app.load_state(), "freq": freq, "mod": mod})
    code, body = _post(client, action="start")
    assert code == 409 and "Air band" in body["error"]


def test_refuse_when_tune_lock_busy(client, world):
    app.TUNE_LOCK.acquire()
    try:
        code, body = _post(client, action="start")
    finally:
        app.TUNE_LOCK.release()
    assert code == 409 and "פעולה אחרת" in body["error"]
    assert world.calls == []


@pytest.mark.parametrize("states", [[0, 0], [10], [-1], "x", [1.5], [True],
                                    [0, 1, 2, 3, 4, 5, 6, 7], []])
def test_refuse_bad_states_400(client, world, states):
    code, _ = _post(client, action="start", states=states)
    assert code == 400 and not app.TUNE_LOCK.locked()


@pytest.mark.parametrize("body", [{"phase": "bogus"}, {"phase": "notch", "from_run": "../x"},
                                  {"phase": "notch", "from_run": 5}])
def test_refuse_bad_phase_or_from_run_400(client, world, body):
    code, _ = _post(client, action="start", **body)
    assert code == 400


def test_extend_is_gone(client, world):
    """החלטת משתמש 1: אין "הארך" — הבדיקה רצה עד שיש מספיק נתונים, עד 3 דקות."""
    code, body = _post(client, action="extend")
    assert code == 400 and "אין הארכה" in body["error"]
    st = client.get("/api/rfcheck").get_json()
    assert st["can_extend"] is False and st["soft_max"] == st["hard_max"] == 180


def test_actions_refused_when_not_running(client, world):
    for action in ("atis", "finish", "abort"):
        code, body = _post(client, action=action)
        assert code == 409 and body["ok"] is False


# --- פרמטרים ---------------------------------------------------------------------

def _start_and_capture(client, world, stub, **body):
    stub.target_rows = 10 ** 9
    code, resp = _post(client, action="start", **body)
    assert code == 200, resp
    assert _wait_for(lambda: world.params_seen)
    params = world.params_seen[0]
    _post(client, action="abort")
    _wait_done()
    return params, resp


def test_params_match_probe_schema_and_are_atomic_0640(client, world, stub):
    params, _ = _start_and_capture(client, world, stub)
    probe = pytest.importorskip("rfcheck_probe")
    assert set(params) == set(probe._PARAM_KEYS)
    assert probe.validate_params(dict(params))["run_id"] == params["run_id"]   # לא זורק
    assert re.fullmatch(r"[0-9a-f]{16}", params["run_id"])
    assert params["phase"] == "lna" and params["ref"] == "tower"
    assert params["freq_hz"] == 134_600_000
    # ‏center דרך _voice_centerfreq — אותו חלון כמו rtl_airband
    assert params["center_hz"] == round(app._voice_centerfreq(134.6) * 1e6) == 134_900_000
    assert params["rate"] == 2_560_000 and params["max_sec"] == 180
    assert params["notch_alternate"] is False and params["notch_base"] is False
    assert world.params_modes == [0o640]
    assert not list(app.RFCHECK_PARAMS_PATH.parent.glob("*.tmp*"))


@pytest.mark.parametrize("cur,expected", [
    (4, [0, 2, 4, 6, 7, 8]),            # הנוכחי כבר בברירת המחדל
    (5, [0, 2, 4, 5, 6, 7, 8]),         # ∪ {נוכחי} => 7 (התקרה)
    (9, [0, 2, 4, 6, 7, 8, 9]),
])
def test_states_default_union_current(client, world, stub, cur, expected):
    app.save_state({**app.load_state(), "rf_gain": cur})
    params, _ = _start_and_capture(client, world, stub)
    assert params["states"] == expected


def test_requested_states_union_current_capped_at_7(client, world, stub):
    params, _ = _start_and_capture(client, world, stub, states=[1, 3, 5, 7, 9, 2, 8])
    # 8 מצבים (∪ 4) => נשמט הקרוב ביותר לנוכחי (5 — בשוויון מול 3, הגבוה)
    assert params["states"] == [1, 2, 3, 4, 7, 8, 9]


def test_ifgr_start_compensated_and_clamped_manual(client, world, stub):
    app.save_state({**app.load_state(), "agc": False, "if_gain": 40, "rf_gain": 4})
    params, _ = _start_and_capture(client, world, stub)
    gr = StubAnalysis.GR_RSP1B_60_420
    for s in params["states"]:
        assert params["ifgr_start"][str(s)] == max(20, min(59, 40 + gr[4] - gr[s]))
    assert params["ifgr_start"]["0"] == 59 and params["ifgr_start"]["8"] == 20   # חיתוך משני הצדדים
    with app._rfc_lock:
        assert app._rfc["ifgr_ref"] == {"value": 40, "source": "manual"}


def _set_rf(**kw):
    with app._rf_lock:
        app._rf.update(**kw)


@pytest.fixture
def rf_restore():
    with app._rf_lock:
        saved = dict(app._rf)
    yield
    with app._rf_lock:
        app._rf.clear()
        app._rf.update(saved)


@pytest.mark.parametrize("rf,start_wall,expected", [
    ({"ifgr": 35, "follower_since": 1.0, "session_start": 1000.0}, 999.5, (35, "agc_live")),
    # gRdB של סשן קודם (לפני ההפעלה הנוכחית של rtl_airband) — לא נקודת העבודה של עכשיו
    ({"ifgr": 35, "follower_since": 1.0, "session_start": 900.0}, 999.5, (40, "assumed")),
    ({"ifgr": None, "follower_since": 1.0, "session_start": 1000.0}, 999.5, (40, "assumed")),
    ({"ifgr": 35, "follower_since": None, "session_start": 1000.0}, 999.5, (40, "assumed")),
    ({"ifgr": 70, "follower_since": 1.0, "session_start": 1000.0}, 999.5, (40, "assumed")),  # מחוץ ל-spec
    ({"ifgr": 35, "follower_since": 1.0, "session_start": 1000.0}, None, (40, "assumed")),
])
def test_ifgr_ref_sources_under_agc(client, world, stub, rf_restore, rf, start_wall, expected):
    _set_rf(**rf)
    world.start_wall = start_wall
    _start_and_capture(client, world, stub)
    with app._rfc_lock:
        ir = app._rfc["ifgr_ref"]
    assert (ir["value"], ir["source"]) == expected


def test_notch_phase_uses_recommended_state_from_previous_run(client, world, stub):
    prev_run = "a" * 16
    app.RFCHECK_LAST_PATH.write_text(json.dumps({
        "run_id": prev_run, "phase": "lna", "level": "stat", "freq": 134.6,
        "recommendation": {"rf_gain": 6, "if_gain": None, "fm_notch": None},
        "per_state": [{"lna": 6, "ifgr_final": 37}]}))
    params, _ = _start_and_capture(client, world, stub, phase="notch", from_run=prev_run)
    assert params["phase"] == "notch" and params["notch_alternate"] is True
    assert params["states"] == [6] and params["ifgr_start"] == {"6": 37}


def test_notch_phase_falls_back_to_current_state(client, world, stub):
    # from_run לא תואם (ריצה אחרת / רמת indication) => ה-LNA הנוכחי, לא ניחוש
    app.RFCHECK_LAST_PATH.write_text(json.dumps({
        "run_id": "b" * 16, "phase": "lna", "level": "indication", "freq": 134.6,
        "recommendation": None, "per_state": []}))
    params, _ = _start_and_capture(client, world, stub, phase="notch", from_run="b" * 16)
    assert params["states"] == [4]


# --- זרימה מוצלחת ----------------------------------------------------------------

def test_success_persists_result_rows_history_and_restores_once(client, world, stub):
    app._rflog_start()
    hist = app.RFCHECK_HISTORY_PATH
    hist.write_text("".join(json.dumps({"run_id": f"old{i}"}) + "\n" for i in range(55)))
    code, body = _post(client, action="start")
    assert code == 200 and body["running"] is True and body["kind"] == "lna"
    run_id = body["run_id"]
    _assert_clean_exit(world)
    res = json.loads(app.RFCHECK_LAST_PATH.read_text())
    assert res["run_id"] == run_id and res["restore"] == {"ok": True, "error": None}
    assert res["ended"] == "target"
    # השורות נשמרות מתויגות + meta/end/ctx — אפשר לשחזר את פסק הדין
    lines = [json.loads(x) for x in app.RFCHECK_LAST_ROWS_PATH.read_text().splitlines()]
    assert lines[0]["kind"] == "ctx" and lines[0]["run_id"] == run_id
    rows = [x for x in lines if x["kind"] == "row"]
    assert rows and all(x["tag"] == "tower" and x["run_id"] == run_id for x in rows)
    assert {x["kind"] for x in lines} == {"ctx", "meta", "row", "end"}
    h = hist.read_text().splitlines()
    assert len(h) == app.RFCHECK_HISTORY_KEEP and json.loads(h[-1])["run_id"] == run_id
    # ‏rflog: אירוע rfcheck נרשם כשהרשם פעיל
    ev = [json.loads(x) for x in app.RFLOG_PATH.read_text().splitlines()]
    rc = [e for e in ev if e.get("ev") == "rfcheck"]
    assert rc and rc[0]["run_id"] == run_id and rc[0]["restore_ok"] is True
    st = client.get("/api/rfcheck").get_json()
    assert st["running"] is False and st["phase"] == "done" and st["result"]["run_id"] == run_id
    assert world.lock_held_at_restart == [True]


def test_rtl_airband_stopped_before_probe_started(client, world, stub):
    _post(client, action="start")
    _wait_done()
    i_stop = world.calls.index(("stop", "rtl_airband"))
    i_restart = world.calls.index(("restart", app.RFCHECK_SERVICE))
    assert i_stop < i_restart


def test_result_survives_reload_from_disk(client, world, stub):
    _post(client, action="start")
    _wait_done()
    run_id = json.loads(app.RFCHECK_LAST_PATH.read_text())["run_id"]
    with app._rfc_lock:
        app._rfc["result"] = None            # "airam-web הופעל מחדש"
    assert client.get("/api/rfcheck").get_json()["result"]["run_id"] == run_id


# --- מסלולי כישלון: כל אחד עוצר, משחזר פעם אחת ומשחרר את הנעילה ----------------------

def test_abort_persists_nothing(client, world, stub):
    stub.target_rows = 10 ** 9
    _post(client, action="start")
    assert _wait_for(lambda: world.probe and world.probe.rows > 3)
    code, _ = _post(client, action="abort")
    assert code == 200
    _assert_clean_exit(world)
    assert not app.RFCHECK_LAST_PATH.exists() and not app.RFCHECK_HISTORY_PATH.exists()
    st = client.get("/api/rfcheck").get_json()
    assert st["ended"] == "abort" and st["result"] is None and stub.analyze_calls == []


def test_finish_analyzes_partial_result(client, world, stub):
    stub.target_rows = 10 ** 9
    _post(client, action="start")
    assert _wait_for(lambda: world.probe and world.probe.rows > 3)
    _post(client, action="finish")
    _assert_clean_exit(world)
    res = json.loads(app.RFCHECK_LAST_PATH.read_text())
    assert res["ended"] == "finish" and res["rows_seen"] > 0


def test_unit_start_failure(client, world, stub):
    world.restart_rc = 1
    _post(client, action="start")
    _assert_clean_exit(world)
    st = client.get("/api/rfcheck").get_json()
    assert st["error_code"] == "start_failed" and st["error"] == "ה-SDR לא נפתח לבדיקה"
    assert "JOURNAL airam-rfcheck" in st["detail"] and st["phase"] == "error"
    assert not app.RFCHECK_LAST_PATH.exists()        # כישלון לא דורס תוצאה טובה קודמת


def test_probe_exit_with_end_error(client, world, stub):
    stub.target_rows = 10 ** 9
    world.exit_after = 4
    _post(client, action="start")
    _assert_clean_exit(world)
    st = client.get("/api/rfcheck").get_json()
    assert st["error_code"] == "probe:device_lost" and "device_lost" in st["error"]
    assert st["result"]["error"]                      # התוצאה (עובדות) מוצגת, לא נשמרת
    assert not app.RFCHECK_LAST_PATH.exists()


def test_heartbeat_stale_watchdog(client, world, stub, monkeypatch):
    stub.target_rows = 10 ** 9
    world.stall = True
    monkeypatch.setattr(app, "RFCHECK_HEARTBEAT_STALE_SEC", 0.15)
    _post(client, action="start")
    _assert_clean_exit(world)
    assert client.get("/api/rfcheck").get_json()["error_code"] == "watchdog"


def test_open_timeout(client, world, stub, monkeypatch):
    world.open_hang = True
    monkeypatch.setattr(app, "RFCHECK_OPEN_TIMEOUT_SEC", 0.15)
    _post(client, action="start")
    _assert_clean_exit(world)
    assert client.get("/api/rfcheck").get_json()["error_code"] == "open_timeout"


def test_deadline(client, world, stub, monkeypatch):
    stub.target_rows = 10 ** 9            # כלל העצירה לא עוצר — רק כלב-השמירה
    monkeypatch.setattr(app, "RFCHECK_HARD_MAX_SEC", 0.1)
    monkeypatch.setattr(app, "RFCHECK_DEADLINE_GRACE_SEC", 0.1)
    _post(client, action="start")
    _assert_clean_exit(world)
    assert client.get("/api/rfcheck").get_json()["error_code"] == "deadline"


def test_exception_inside_loop(client, world, stub, monkeypatch):
    stub.target_rows = 10 ** 9

    def boom(names):
        if tuple(names) == (app.RFCHECK_SERVICE,):
            raise RuntimeError("systemctl exploded")
        return world.services(names)
    monkeypatch.setattr(app, "_services_status", boom)
    _post(client, action="start")
    _assert_clean_exit(world)
    assert client.get("/api/rfcheck").get_json()["error_code"] == "internal"


def test_live_summary_failure_does_not_kill_run(client, world, stub):
    stub.raise_in_live = True
    _post(client, action="start")
    assert _wait_for(lambda: world.probe and world.probe.rows > 3)
    _post(client, action="finish")
    _assert_clean_exit(world)
    assert client.get("/api/rfcheck").get_json()["error"] is None


def test_restore_failure_reported(client, world, stub):
    world.restore_ok = False
    _post(client, action="start")
    _assert_clean_exit(world)
    st = client.get("/api/rfcheck").get_json()
    assert st["restore"]["ok"] is False and "השמע לא חזר" in st["restore"]["error"]
    assert app.load_state()["app_mode"] == "voice"   # הכוונה נשמרת — reconcile יחזיר את הקול


# --- ATIS -------------------------------------------------------------------------

def test_atis_switch_keeps_lock_tower_rows_and_parent(client, world, stub):
    stub.target_rows = 10 ** 9
    stub.offer = True
    code, body = _post(client, action="start")
    tower_run = body["run_id"]
    assert _wait_for(lambda: world.probe and world.probe.rows > 4)
    assert _wait_for(lambda: client.get("/api/rfcheck").get_json()["offer_atis"] is True)
    code, _ = _post(client, action="atis")
    assert code == 200
    assert _wait_for(lambda: len(world.params_seen) == 2)
    assert app.TUNE_LOCK.locked()                       # הנעילה לא שוחררה במעבר
    p2 = world.params_seen[1]
    assert p2["freq_hz"] == 132_500_000 and p2["ref"] == "atis" and p2["run_id"] != tower_run
    assert p2["center_hz"] == round(app._voice_centerfreq(132.5) * 1e6)
    assert p2["max_sec"] == StubAnalysis.ATIS_MAX_SEC + app.RFCHECK_ATIS_PROBE_EXTRA_SEC
    assert p2["ifgr_start"] == world.params_seen[0]["ifgr_start"]   # אותו IFGR_ref
    st = client.get("/api/rfcheck").get_json()
    assert st["phase"] == "atis" and st["parent_id"] == tower_run and st["offer_atis"] is False
    code, body = _post(client, action="atis")           # פעם שנייה — כבר ב-ATIS
    assert code == 409
    assert _wait_for(lambda: world.probe and world.probe.run_id == p2["run_id"] and world.probe.rows > 3)
    _post(client, action="finish")
    _assert_clean_exit(world)
    assert world.lock_held_at_restart == [True, True]
    call = stub.analyze_calls[-1]
    assert call["ctx"]["ref"] == "atis" and call["ctx"]["parent_id"] == tower_run
    assert call["ctx"]["tower"]["rows"] and all(r["run_id"] == tower_run
                                                for r in call["ctx"]["tower"]["rows"])
    assert all(r["run_id"] == p2["run_id"] for r in call["rows"])
    tags = {json.loads(x)["tag"] for x in app.RFCHECK_LAST_ROWS_PATH.read_text().splitlines()
            if json.loads(x)["kind"] == "row"}
    assert tags == {"tower", "atis"}


def test_atis_refused_in_notch_phase(client, world, stub):
    stub.target_rows = 10 ** 9
    _post(client, action="start", phase="notch")
    code, body = _post(client, action="atis")
    assert code == 409 and "ATIS" in body["error"]
    _post(client, action="abort")
    _assert_clean_exit(world)


# --- run_id זר ---------------------------------------------------------------------

def test_stale_run_id_files_are_ignored(client, world, stub):
    run = app.RFCHECK_RUN_DIR
    # שרידים של ריצה קודמת — end.json "error" שלה לא יכול לעצור את הריצה שלנו
    (run / "end.json").write_text(json.dumps({"run_id": "f" * 16, "ended": "error",
                                              "error": "device_lost"}))
    (run / "meta.json").write_text(json.dumps({"run_id": "f" * 16}))
    rd = app._RfcReader(run, RUN_A)
    rd.poll()
    assert rd.end is None and rd.meta is None and rd.rows == []
    # שורות של run_id אחר באותו קובץ מסוננות; שורה חלקית ממתינה להשלמה
    (run / "meta.json").write_text(json.dumps({"run_id": RUN_A}))
    with open(run / "rows.jsonl", "w") as f:
        f.write(json.dumps({"run_id": "f" * 16, "i": 0}) + "\n")
        f.write(json.dumps({"run_id": RUN_A, "i": 0}) + "\n")
        f.write('{"run_id": "%s", "i"' % RUN_A)
    rd.poll()
    assert [r["i"] for r in rd.rows] == [0] and rd.meta == {"run_id": RUN_A}
    with open(run / "rows.jsonl", "a") as f:
        f.write(': 1}\n')
    rd.poll()
    assert [r["i"] for r in rd.rows] == [0, 1]


def test_reader_waits_for_own_meta_before_reading_rows(paths):
    """שריד rows.jsonl של ריצה קודמת לא נקרא בכלל (גם לא כדי לקדם offset): הקריאה מתחילה
    רק כש-meta.json *שלנו* מופיע — והבודק מוחק את הישנים לפני שהוא כותב אותו. ⚠ inode
    לא יכול לשמש לזה: גרסה קודמת של הבדיקה (unlink+יצירה מחדש באותו נתיב) הראתה ש-tmpfs
    ממחזר אותו מיד — offset ישן הוחל על קובץ חדש ודילג על 5 שורות."""
    run = app.RFCHECK_RUN_DIR
    (run / "meta.json").write_text(json.dumps({"run_id": "f" * 16}))
    (run / "rows.jsonl").write_text("".join(json.dumps({"run_id": "f" * 16, "i": i}) + "\n"
                                            for i in range(50)))
    rd = app._RfcReader(run, RUN_A)
    rd.poll()
    assert rd.rows == [] and rd._off == 0
    for n in ("meta.json", "rows.jsonl"):             # הבודק החדש עלה: מחיקה, ואז meta שלו
        (run / n).unlink()
    (run / "rows.jsonl").write_text("".join(json.dumps({"run_id": RUN_A, "i": 10 + i}) + "\n"
                                            for i in range(9)))
    (run / "meta.json").write_text(json.dumps({"run_id": RUN_A}))
    rd.poll()
    assert [r["i"] for r in rd.rows] == list(range(10, 19))


def test_success_ignores_stale_end_from_previous_run(client, world, stub):
    (app.RFCHECK_RUN_DIR / "end.json").write_text(json.dumps(
        {"run_id": "f" * 16, "ended": "error", "error": "device_lost"}))
    _post(client, action="start")
    _assert_clean_exit(world)
    assert client.get("/api/rfcheck").get_json()["error"] is None
    assert json.loads(app.RFCHECK_LAST_PATH.read_text())["ended"] == "target"


# --- אבחון -------------------------------------------------------------------------

def test_diagnose_from_off_saves_file_and_keeps_last_result(client, world, stub):
    world.live = None
    app.save_state({**app.load_state(), "app_mode": "off"})
    app.RFCHECK_LAST_PATH.write_text(json.dumps({"run_id": "c" * 16, "level": "stat"}))
    code, body = _post(client, action="start", phase="diagnose")
    assert code == 200 and body["kind"] == "diagnose"
    _assert_clean_exit(world, prev_live=None, app_mode="off")
    p = world.params_seen[0]
    assert p["phase"] == "diagnose" and p["ref"] == "atis" and p["max_sec"] == 150
    assert p["freq_hz"] == 132_500_000 and p["center_hz"] == 132_800_000
    probe = pytest.importorskip("rfcheck_probe")
    probe.validate_params(dict(p))
    d = json.loads(app.RFCHECK_DIAG_PATH.read_text())
    assert d["run_id"] == body["run_id"] and d["summary_text"].startswith("AIRAM-RFDIAG v1")
    st = client.get("/api/rfcheck").get_json()
    assert st["diagnose_available"] and st["diagnose"]["run_id"] == body["run_id"]
    assert json.loads(app.RFCHECK_LAST_PATH.read_text())["run_id"] == "c" * 16   # לא נדרס
    assert app.load_state()["app_mode"] == "off" and stub.analyze_calls == []


def test_diagnose_abort_saves_nothing(client, world, stub):
    """ביטול אבחון = "קובץ האבחון לא יישמר" (כך ה-UI מבטיח) — גם לא קובץ חלקי."""
    world.diag_rows = 10 ** 9
    world.live = None
    app.save_state({**app.load_state(), "app_mode": "off"})
    _post(client, action="start", phase="diagnose")
    assert _wait_for(lambda: world.probe and world.probe.rows > 3)
    (app.RFCHECK_RUN_DIR / "diagnose.json").write_text(json.dumps(
        {"run_id": world.probe.run_id, "complete": False, "summary_text": "AIRAM-RFDIAG v1"}))
    _post(client, action="abort")
    _assert_clean_exit(world, prev_live=None, app_mode="off")
    assert not app.RFCHECK_DIAG_PATH.exists()
    st = client.get("/api/rfcheck").get_json()
    assert st["ended"] == "abort" and st["diagnose"] is None and st["diagnose_available"] is False


def test_export_routes(client, world, stub):
    assert client.get("/api/rfcheck/export?kind=rows").status_code == 404
    assert client.get("/api/rfcheck/export?kind=bogus").status_code == 400
    _post(client, action="start")
    _wait_done()
    r = client.get("/api/rfcheck/export?kind=rows")
    assert r.status_code == 200 and r.mimetype == "application/x-ndjson"
    r = client.get("/api/rfcheck/export?kind=result")
    assert r.status_code == 200 and r.get_json()["run_id"]


# --- שילוב עם שאר המערכת -----------------------------------------------------------

def test_boot_restore_stops_orphan_probe_before_voice(paths, monkeypatch):
    calls = []
    monkeypatch.setattr(app, "_sysctl", lambda a, s, timeout=45: calls.append((a, s)) or _ok())
    monkeypatch.setattr(app, "_services_status", lambda names: {
        n: ("activating" if n == app.RFCHECK_SERVICE else "inactive") for n in names})
    monkeypatch.setattr(app, "_live_mode", lambda: None)
    monkeypatch.setattr(app, "_sdr_present", lambda: True)
    monkeypatch.setattr(app, "_rf_telemetry_available", lambda: False)
    monkeypatch.setattr(app, "_enter_voice", lambda params: calls.append(("enter_voice",)) or
                        (None, None, False))
    app.save_state({**app.DEFAULT_STATE, "app_mode": "voice"})
    app._boot_restore()
    assert calls[0] == ("stop", app.RFCHECK_SERVICE)
    assert ("enter_voice",) in calls and calls.index(("enter_voice",)) > 0


def test_boot_restore_stops_orphan_probe_in_off(paths, monkeypatch):
    calls = []
    monkeypatch.setattr(app, "_sysctl", lambda a, s, timeout=45: calls.append((a, s)) or _ok())
    monkeypatch.setattr(app, "_services_status", lambda names: {
        n: ("active" if n == app.RFCHECK_SERVICE else "inactive") for n in names})
    monkeypatch.setattr(app, "_live_mode", lambda: None)
    app.save_state({**app.DEFAULT_STATE, "app_mode": "off"})
    app._boot_restore()
    assert calls == [("stop", app.RFCHECK_SERVICE)]


def test_mode_reconcile_noop_while_check_holds_lock(paths, monkeypatch):
    entered = []
    monkeypatch.setattr(app, "_live_mode", lambda: None)       # בזמן בדיקה: אין "מצב" חי
    monkeypatch.setattr(app, "_sdr_present", lambda: True)
    monkeypatch.setattr(app, "_enter_voice", lambda p: entered.append(p) or (None, None, False))
    app.save_state({**app.DEFAULT_STATE, "app_mode": "voice"})
    app.TUNE_LOCK.acquire()
    try:
        app._mode_reconcile_once()
    finally:
        app.TUNE_LOCK.release()
    assert entered == []


def test_enter_standby_stops_rfcheck_first(paths, monkeypatch):
    calls = []
    monkeypatch.setattr(app, "_sysctl", lambda a, s, timeout=45: calls.append((a, s)) or _ok())
    monkeypatch.setattr(app, "_is_active", lambda svc: False)
    monkeypatch.setattr(app.time, "sleep", lambda *_: None)
    assert app._enter_standby() == (None, None)
    assert calls[0] == ("stop", app.RFCHECK_SERVICE)
    assert {s for _a, s in calls} == {app.RFCHECK_SERVICE, app.ACARS_SERVICE, app.VDL2_SERVICE,
                                      app.SATCOM_SERVICE, "rtl_airband"}


def test_enter_voice_stops_running_rfcheck(paths, monkeypatch):
    calls = []
    monkeypatch.setattr(app, "_sysctl", lambda a, s, timeout=45: calls.append((a, s)) or _ok())
    monkeypatch.setattr(app, "_is_active", lambda svc: svc == app.RFCHECK_SERVICE)
    monkeypatch.setattr(app, "_restart_and_verify", lambda: (None, None, False))
    params, _ = app._parse_tune({"freq": 132.5})
    app._enter_voice({**params, "fm_notch": False})
    assert ("stop", app.RFCHECK_SERVICE) in calls


def test_rfcheck_is_not_a_mode(paths):
    """במכוון: לא ב-MODE_SERVICE (לא "אפליקציה" ששורדת reboot) — אבל כן צרכן SDR."""
    assert app.RFCHECK_SERVICE not in app.MODE_SERVICE.values()
    assert app.RFCHECK_SERVICE in app.SDR_CONSUMERS
    assert app._SDR_CONSUMER_MODE[app.RFCHECK_SERVICE] == "rfcheck"
    assert app.RFCHECK_SERVICE in app.HEALTH_SERVICES


def test_state_and_health_ok_during_run(client, world, stub):
    stub.target_rows = 10 ** 9
    _post(client, action="start")
    assert _wait_for(lambda: world.probe and world.probe.rows > 2)
    st = client.get("/api/state").get_json()
    assert st["mode_ok"] is True and st["app_mode"] == "voice"
    assert st["rf_check"]["running"] is True and st["rf_check"]["run_id"]
    h = client.get("/api/health").get_json()
    assert h["ok"] is True and h["rf_check"]["running"] is True
    assert h["services"][app.RFCHECK_SERVICE] == "active"
    _post(client, action="abort")
    _assert_clean_exit(world)
    st = client.get("/api/state").get_json()
    assert st["rf_check"] == {"running": False, "phase": None, "kind": None, "run_id": None}


def test_health_orphan_probe_in_standby_is_not_ok(client, paths, monkeypatch):
    monkeypatch.setattr(app, "_services_status", lambda names: {
        n: ("active" if n in (app.RFCHECK_SERVICE, "sdrplay", "icecast2") else "inactive")
        for n in names})
    monkeypatch.setattr(app, "_sdr_present", lambda: True)
    app.save_state({**app.DEFAULT_STATE, "app_mode": "off"})
    assert client.get("/api/health").get_json()["ok"] is False


def test_api_sdr_reports_rfcheck_as_ours(client, paths, monkeypatch):
    monkeypatch.setattr(app, "_sdr_usb", lambda: (True, "SDRplay RSP1B"))
    monkeypatch.setattr(app, "_services_status", lambda names: {
        n: ("active" if n in (app.RFCHECK_SERVICE, "sdrplay") else "inactive") for n in names})
    s = client.get("/api/sdr").get_json()
    assert s["state"] == "ours" and s["mode"] == "rfcheck"


def test_get_status_contract_for_ui(client, world, stub):
    """השדות שה-UI (index.html) והכלי airam-rfcheck-diag קוראים — כולם קיימים תמיד."""
    st = client.get("/api/rfcheck").get_json()
    for k in ("ok", "available", "reasons", "install_hint", "telemetry_expected", "experimental",
              "verified", "running", "run_id", "parent_id", "kind", "phase", "ref", "freq",
              "atis_freq", "elapsed", "phase_elapsed", "phase_max", "soft_max", "hard_max",
              "can_extend", "states", "current_state", "production_state", "last_slot",
              "tx_captured", "tx_target", "full_blocks", "blocks_target", "no_traffic_sec",
              "progress", "level", "offer_atis", "probe", "result", "diagnose",
              "diagnose_available", "error", "error_code", "restore", "ended", "ifgr_ref"):
        assert k in st, k
    assert st["experimental"] is True                  # RFCHECK_VERIFIED=None
    stub.target_rows = 10 ** 9
    _post(client, action="start")
    st = client.get("/api/rfcheck").get_json()
    assert st["running"] and [x["lna"] for x in st["states"]] == [0, 2, 4, 6, 7, 8]
    assert all(x["label"] == f"{9 - x['lna']}/9" for x in st["states"])
    assert st["result"] is None                         # בזמן ריצה — לא התוצאה הקודמת
    _post(client, action="abort")
    _wait_done()


# --- זמינות -------------------------------------------------------------------------

@pytest.fixture
def avail_env(paths, monkeypatch):
    probe = paths / "rfcheck_probe.py"
    probe.write_text("# stub\n")
    unit = paths / "airam-rfcheck.service"
    unit.write_text("[Unit]\n")
    monkeypatch.setattr(app, "RFCHECK_PROBE_PATH", probe)
    monkeypatch.setattr(app, "RFCHECK_UNIT_PATH", unit)
    calls = []

    def run(out, rc=0):
        def fake(cmd, **kw):
            calls.append(cmd)
            return _ok(rc, stdout=out)
        monkeypatch.setattr(app.subprocess, "run", fake)
    return types.SimpleNamespace(probe=probe, unit=unit, calls=calls, run=run)


GOOD = json.dumps({"python": "3.11.2", "numpy": "1.24.2", "soapysdr": "0.8",
                   "sdrplay_module": True, "log_handler_api": True})


def test_availability_ok_and_cached(avail_env):
    avail_env.run(GOOD)
    a = app._rfcheck_availability()
    assert a["available"] is True and a["reasons"] == [] and a["install_hint"] == "sudo ./install.sh"
    assert avail_env.calls[0] == ["/usr/bin/python3", "-I", str(avail_env.probe), "--selftest"]
    app._rfcheck_availability()
    assert len(avail_env.calls) == 1                     # cache
    app._rfcheck_availability(force=True)
    assert len(avail_env.calls) == 2


@pytest.mark.parametrize("st,rc,reasons", [
    ({"numpy": None, "soapysdr": "0.8", "sdrplay_module": True}, 1, ["numpy_missing"]),
    ({"numpy": "1", "soapysdr": None, "sdrplay_module": False}, 1, ["soapysdr_missing"]),
    ({"numpy": "1", "soapysdr": "0.8", "sdrplay_module": False}, 1, ["sdrplay_module_missing"]),
    ({"numpy": "1", "soapysdr": "0.8", "sdrplay_module": True}, 1, ["selftest_failed"]),
])
def test_availability_selftest_reasons(avail_env, st, rc, reasons):
    avail_env.run(json.dumps(st), rc)
    a = app._rfcheck_availability()
    assert a["available"] is False and a["reasons"] == reasons


def test_availability_garbage_selftest(avail_env):
    avail_env.run("Traceback ...\n")
    assert app._rfcheck_availability()["reasons"] == ["selftest_failed"]


def test_availability_missing_files_and_analysis(avail_env, monkeypatch):
    avail_env.run(GOOD)
    avail_env.probe.unlink()
    avail_env.unit.unlink()
    monkeypatch.setattr(app, "rfcheck_analysis", None)
    a = app._rfcheck_availability()
    assert a["reasons"] == ["analysis_missing", "probe_missing", "unit_missing"]
    assert avail_env.calls == []                         # בלי בודק אין selftest


def test_availability_bad_result_rechecked_sooner(avail_env, monkeypatch):
    avail_env.run(json.dumps({"numpy": None}), 1)
    app._rfcheck_availability()
    monkeypatch.setattr(app, "RFCHECK_AVAIL_TTL_BAD", 0.0)
    avail_env.run(GOOD)
    assert app._rfcheck_availability()["available"] is True


def test_verified_requires_matching_build_sig_and_api(paths, monkeypatch):
    app.SOAPY_RF_MARK.write_text("sig-1\n")
    app.SDRPLAY_API_VERSION_PATH.write_text("3.15.2\n")
    monkeypatch.setattr(app, "RFCHECK_VERIFIED", {"soapy_build_sig": "sig-1", "sdrplay_api": "3.15.2",
                                                  "guard_buffers": 2, "rail_code": 32700})
    assert app._rfcheck_verified()["guard_buffers"] == 2
    p = app._rfcheck_params(RUN_A, "lna", "tower", 134.6, [0, 4], {"0": 50, "4": 40}, False, 180,
                            app._rfcheck_verified())
    assert p["guard_buffers"] == 2 and p["rail_code"] == 32700
    app.SOAPY_RF_MARK.write_text("sig-2\n")              # בנייה מחדש => שוב "ניסיוני"
    assert app._rfcheck_verified() is None
    app.SOAPY_RF_MARK.write_text("sig-1\n")
    app.SDRPLAY_API_VERSION_PATH.write_text("3.15.3\n")
    assert app._rfcheck_verified() is None


def test_start_rechecks_stale_negative_availability(client, world, monkeypatch):
    seen = []

    def avail(force=False):
        seen.append(force)
        return dict(AVAILABLE) if force else {**AVAILABLE, "available": False, "reasons": ["x"]}
    monkeypatch.setattr(app, "_rfcheck_availability", avail)
    code, _ = _post(client, action="start")
    assert code == 200 and True in seen
    _post(client, action="abort")
    _wait_done()


# --- החלה ---------------------------------------------------------------------------

def _last(**kw):
    res = {"v": 1, "run_id": RUN_A, "phase": "lna", "level": "stat", "freq": 134.6,
           "config_at_start": {"freq": 134.6, "agc": True, "fm_notch": False, "if_gain": 40,
                               "rf_gain": 4},
           "recommendation": {"rf_gain": 6, "if_gain": None, "fm_notch": None},
           "apply_allowed": True}
    res.update(kw)
    app.RFCHECK_LAST_PATH.write_text(json.dumps(res))
    return res


@pytest.fixture
def tune(paths, monkeypatch):
    entered = []
    monkeypatch.setattr(app, "_enter_voice", lambda p: entered.append(dict(p)) or (None, None, False))
    monkeypatch.setattr(app, "_rollback", lambda prev: True)
    app.save_state({**app.DEFAULT_STATE, "app_mode": "voice", "freq": 134.6, "mod": "am",
                    "agc": True, "rf_gain": 4, "if_gain": 40, "fm_notch": False})
    return entered


def test_apply_agc_sets_rf_gain_only(client, tune):
    _last()
    code, body = _post(client, action="apply", run_id=RUN_A)
    assert code == 200 and body["ok"] is True
    assert tune[-1]["rf_gain"] == 6 and tune[-1]["if_gain"] == 40
    st = app.load_state()
    assert st["rf_gain"] == 6 and st["if_gain"] == 40
    assert st["rf_check_applied"]["run_id"] == RUN_A and st["rf_check_applied"]["if_gain"] is None


def test_apply_manual_gain_sets_rf_and_if(client, tune):
    app.save_state({**app.load_state(), "agc": False})
    _last(config_at_start={"freq": 134.6, "agc": False, "fm_notch": False},
          recommendation={"rf_gain": 7, "if_gain": 33, "fm_notch": None})
    code, _ = _post(client, action="apply", run_id=RUN_A)
    assert code == 200
    assert tune[-1]["rf_gain"] == 7 and tune[-1]["if_gain"] == 33
    assert app.load_state()["rf_check_applied"]["if_gain"] == 33


def test_apply_notch_result_sets_fm_notch(client, tune):
    _last(phase="notch", config_at_start={"freq": 134.6, "agc": True, "fm_notch": False},
          recommendation={"rf_gain": 4, "if_gain": None, "fm_notch": True})
    code, _ = _post(client, action="apply", run_id=RUN_A)
    assert code == 200 and tune[-1]["fm_notch"] is True
    assert app.load_state()["fm_notch"] is True


@pytest.mark.parametrize("kw,state_kw,msg", [
    ({"level": "indication"}, {}, "אין המלצה ברמה"),
    ({"recommendation": None}, {}, "אין המלצה ברמה"),
    ({}, {"app_mode": "acars"}, "עבור לקול"),
    ({}, {"freq": 120.5}, "ההגדרות השתנו"),
    ({}, {"agc": False}, "ההגדרות השתנו"),
    ({}, {"fm_notch": True}, "ההגדרות השתנו"),
    ({"apply_allowed": False}, {}, "אין מה להחיל"),
    ({}, {"rf_gain": 6}, "כבר בתוקף"),          # הוחלה כבר (או כוונה ידנית) — בלי restart סרק
])
def test_apply_refusals(client, tune, kw, state_kw, msg):
    _last(**kw)
    app.save_state({**app.load_state(), **state_kw})
    code, body = _post(client, action="apply", run_id=RUN_A)
    assert code == 409 and msg in body["error"]
    assert tune == []


def test_apply_refused_for_other_run(client, tune):
    _last()
    code, body = _post(client, action="apply", run_id="b" * 16)
    assert code == 409 and tune == []


def test_apply_refused_while_running(client, tune):
    _last()
    with app._rfc_lock:
        app._rfc["running"] = True
    try:
        code, body = _post(client, action="apply", run_id=RUN_A)
    finally:
        with app._rfc_lock:
            app._rfc["running"] = False
    assert code == 409 and tune == []


def test_apply_goes_through_voice_tune_rollback(client, paths, monkeypatch):
    """החלה שנכשלה => המסלול הקיים של /api/tune: רולבק לקונפיג האחרון שעבד, state לא משתנה."""
    rolled = []
    monkeypatch.setattr(app, "_enter_voice", lambda p: ("rtl_airband נכשל", "log", False))
    monkeypatch.setattr(app, "_rollback", lambda prev: rolled.append(prev) or True)
    app.save_state({**app.DEFAULT_STATE, "app_mode": "voice", "freq": 134.6, "mod": "am",
                    "agc": True, "rf_gain": 4, "fm_notch": False})
    _last()
    code, body = _post(client, action="apply", run_id=RUN_A)
    assert code == 500 and "חזרתי" in body["error"] and rolled
    st = app.load_state()
    assert st["rf_gain"] == 4 and "rf_check_applied" not in st


# --- app.py לא תלוי ב-numpy/SoapySDR ולא מייבא את הבודק --------------------------------

def test_app_never_imports_probe_numpy_or_soapysdr():
    src = (ROOT / "webtune" / "app.py").read_text(encoding="utf-8")
    for pat in (r"^\s*(import|from)\s+rfcheck_probe\b", r"^\s*(import|from)\s+numpy\b",
                r"^\s*(import|from)\s+SoapySDR\b"):
        assert not re.search(pat, src, re.M), pat


def test_app_imports_without_numpy():
    """spec §3.5: airam-web עולה גם כש-numpy חסר (חבילה סובלנית ב-install.sh)."""
    code = ("import sys; sys.modules['numpy'] = None; sys.path.insert(0, 'webtune'); import app; "
            "assert app.rfcheck_analysis is not None, 'analysis needs numpy?'; "
            "assert 'SoapySDR' not in sys.modules; print('ok')")
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == 0 and r.stdout.strip().endswith("ok"), r.stderr


# --- עשן מול שכבת הניתוח האמיתית ----------------------------------------------------

def _realistic_row(run_id, i, cyc, lna, carrier):
    base = -30.0 if carrier else -62.0
    return {"run_id": run_id, "i": i, "cyc": cyc, "dir": "f" if cyc % 2 == 0 else "r",
            "lna": lna, "ifgr": 40, "epoch": 0, "notch": False, "t0": i * 0.25,
            "t1": i * 0.25 + 0.18, "wall": 1e9 + i * 0.25, "valid": True, "inv": None,
            "sw_ms": 3.0, "drained": 2, "n": 7 * 65536, "c_tot": base, "c_tot_h1": base,
            "c_tot_h2": base, "c_car": base - 0.5 - lna * 0.05, "b_tot": [base] * 7,
            "n_nb": -62.5, "p_wb": -40.0, "clip": 0, "peak": 2000, "ovl": None,
            "ovl_edge": None, "e_mean": 0.01, "e_std": 0.001, "e_p99": 0.02, "proc_ms": 6.0}


def test_smoke_with_real_analysis_module(client, world, monkeypatch):
    real = pytest.importorskip("rfcheck_analysis")
    monkeypatch.setattr(app, "rfcheck_analysis", real)
    states = [0, 2, 4, 6, 7, 8]

    def row(self, i):
        cyc = i // len(states)
        order = states if cyc % 2 == 0 else list(reversed(states))
        return _realistic_row(self.run_id, i, cyc, order[i % len(states)], 3 <= cyc <= 7)
    monkeypatch.setattr(FakeProbe, "_row", row)
    world.max_rows = 12 * len(states)
    _post(client, action="start")
    assert _wait_for(lambda: world.probe and world.probe.rows >= world.max_rows, timeout=10)
    st = client.get("/api/rfcheck").get_json()      # הסיכום החי עובר JSON
    assert st["running"] in (True, False)
    if st["running"]:
        _post(client, action="finish")
    _assert_clean_exit(world)
    st = client.get("/api/rfcheck").get_json()
    assert st["error"] is None, st["error"]
    res = st["result"]
    assert isinstance(res, dict) and res.get("level") in ("fact", "stat", "indication", "none")
    assert json.loads(app.RFCHECK_LAST_PATH.read_text())["run_id"] == res["run_id"]
    assert res["config_at_start"]["rf_gain"] == 4 and res["ifgr_ref"]["source"] == "assumed"


# --- הכלי airam-rfcheck-diag מקצה לקצה מול airam-web האמיתי ---------------------------

def _run_diag(tmp_path, env_text):
    """מריץ את scripts/airam-rfcheck-diag מול app.app אמיתי (שרת werkzeug על פורט זמני)."""
    if not shutil.which("bash") or not shutil.which("curl"):
        pytest.skip("bash/curl חסרים")
    from werkzeug.serving import make_server
    env_file = tmp_path / "airam.env"
    env_file.write_text(env_text)
    srv = make_server("127.0.0.1", 0, app.app, threaded=True)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        env = {**os.environ, "AIRAM_API_URL": f"http://127.0.0.1:{srv.server_port}",
               "AIRAM_ENV_FILE": str(env_file), "AIRAM_POLL_SEC": "0.1"}
        return subprocess.run(["bash", str(ROOT / "scripts" / "airam-rfcheck-diag")], env=env,
                              capture_output=True, text=True, timeout=60)
    finally:
        srv.shutdown()
        th.join(timeout=5)


def test_diag_script_end_to_end(paths, world, stub, monkeypatch, tmp_path):
    monkeypatch.setattr(app, "AIRAM_PIN", "4321")
    world.live = None
    app.save_state({**app.load_state(), "app_mode": "off"})
    r = _run_diag(tmp_path, '# x\nAIRAM_PIN = "4321" \n')
    assert r.returncode == 0, r.stdout + r.stderr
    assert "AIRAM-RFDIAG v1" in r.stdout and "/var/lib/airam/rfcheck_diagnose.json" in r.stdout
    assert world.params_seen[0]["phase"] == "diagnose"
    assert "4321" not in r.stdout + r.stderr          # ה-PIN לא מודפס


def test_diag_script_wrong_pin(paths, world, stub, monkeypatch, tmp_path):
    monkeypatch.setattr(app, "AIRAM_PIN", "4321")
    world.live = None
    app.save_state({**app.load_state(), "app_mode": "off"})
    r = _run_diag(tmp_path, "AIRAM_PIN=1111\n")
    assert r.returncode == 1 and "401" in r.stderr and world.calls == []


def test_diag_script_reports_server_refusal(paths, world, stub, tmp_path):
    world.live = "acars"
    app.save_state({**app.load_state(), "app_mode": "acars"})
    r = _run_diag(tmp_path, "")
    assert r.returncode == 1 and "409" in r.stderr and "אבחון זמין" in r.stderr
