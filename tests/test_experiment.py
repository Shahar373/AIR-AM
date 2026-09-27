# ============================================================================
#  AIR-AM - ניסוי כיול אוטומטי (/api/experiment) — docs/antenna-calibration-experiment.md
# ----------------------------------------------------------------------------
#  "SDR מזויף": thread שכותב קובץ stats כמו rtl_airband, עם רצפת רעש שתלויה
#  בתדר/AGC/רווח ובמצב האנטנה (מחוברת/מנותקת). כך נבדק הניסוי מקצה לקצה —
#  כולל שתי הפעולות הפיזיות — בלי חומרה. סנכרון דרך Event/join, לא sleep קבוע.
# ============================================================================
import threading
import time
import types

import pytest

import app


@pytest.fixture
def paths(tmp_path, monkeypatch):
    for name, fn in (("CONFIG_PATH", "airband.conf"), ("STATE_PATH", "state.json"),
                     ("STATS_PATH", "stats.txt"), ("RFLOG_PATH", "rf_log.jsonl"),
                     ("ACARS_ENV_PATH", "acars.env"), ("VDL2_ENV_PATH", "vdl2.env"),
                     ("SATCOM_ENV_PATH", "satcom.env")):
        monkeypatch.setattr(app, name, tmp_path / fn)
    return tmp_path


@pytest.fixture
def client(paths):
    return app.app.test_client()


@pytest.fixture(autouse=True)
def _clean(paths):
    # ⚠ תלוי ב-paths — הניקוי חייב להתפרק לפני ש-monkeypatch מחזיר את הנתיבים
    # האמיתיים (ר' _rflog_clean ב-test_rflog.py: אחרת כותבים ל-/var/lib/airam).
    yield
    with app._exp_lock:
        th, stop = app._exp["thread"], app._exp["stop"]
    if stop:
        stop.set()
    if th:
        th.join(timeout=10)
    with app._exp_lock:
        app._exp.update(running=False, id=None, plan=[], i=-1, waiting=None, error=None,
                        result=None, stop=None, confirm=None, thread=None, step_started_at=None)
    app._rflog_stop("test-teardown")
    if app.TUNE_LOCK.locked():
        app.TUNE_LOCK.release()


class FakeSDR:
    """רצפת רעש לפי תרחיש משטר B: ברווח קבוע ניתוק מוריד 12dB; ב-AGC בלי ATIS
    בחלון — 12dB; ב-AGC עם ATIS בחלון — ה-AGC "מסתיר" ונשארים 2dB."""

    def __init__(self, paths):
        self.paths = paths
        self.connected = True
        self.params = None
        self.proc_start = None
        self.enters = []
        self._stop = threading.Event()
        self._th = threading.Thread(target=self._loop, daemon=True)

    def noise(self):
        p = self.params
        if p["agc"] and abs(p["freq"] - app.EXP_ATISWIN_FREQ) < 1e-3:
            return -58.0 if self.connected else -60.0
        return -60.0 if self.connected else -72.0

    def signal(self):
        if abs(self.params["freq"] - app.EXP_ATIS_FREQ) < 1e-3:
            return -15.0 if self.connected else -68.0
        return self.noise() + 1.0

    def _write(self):
        lbl = f"{self.params['freq']:.3f}"
        (self.paths / "stats.txt").write_text(
            f'channel_dbfs_signal_level{{freq="{lbl}"}} {self.signal()}\n'
            f'channel_dbfs_noise_level{{freq="{lbl}"}} {self.noise()}\n')

    def _loop(self):
        while not self._stop.wait(0.02):
            if self.params:
                self._write()

    def enter_voice(self, params):
        app.write_config(params["freq"], params["mod"], params["agc"], params["if_gain"],
                         params["rf_gain"], params["squelch_mode"], params["squelch_snr"])
        self.params = dict(params)
        self.proc_start = time.time()
        self._write()
        self.enters.append(dict(params))
        return None, None, False

    def start(self):
        self._th.start()

    def stop(self):
        self._stop.set()
        self._th.join(timeout=2)


@pytest.fixture
def fast(monkeypatch):
    for k, v in (("EXP_REF_SEC", 0.25), ("EXP_FIXED_SEC", 0.25), ("EXP_AGC_SEC", 0.3),
                 ("EXP_AFTER_PROMPT_SEC", 0.25), ("EXP_FINAL_AGC_SEC", 0.25),
                 ("EXP_FIXED_SETTLE_SEC", 0.05), ("EXP_AGC_TAIL_SEC", 0.2), ("EXP_EARLY_SEC", 0.08),
                 ("EXP_CREEP_HEAD_SEC", 0.08), ("EXP_CREEP_TAIL_SEC", 0.1),
                 ("EXP_REF_SETTLE_SEC", 0.05), ("EXP_STALE_WINDOW_SEC", 0.1),
                 ("ANTENNA_CHECK_SAMPLE_SEC", 0.5), ("RFLOG_POLL_SEC", 0.01)):
        monkeypatch.setattr(app, k, v)


@pytest.fixture
def world(paths, fast, monkeypatch):
    sdr = FakeSDR(paths)
    restored = []
    monkeypatch.setattr(app, "_enter_voice", sdr.enter_voice)
    monkeypatch.setattr(app, "_rtl_airband_start_wall", lambda: sdr.proc_start)
    monkeypatch.setattr(app, "_live_mode", lambda: "acars")
    monkeypatch.setattr(app, "_enter_acars", lambda freqs: restored.append(("acars", freqs)) or (None, None))
    sdr.restored = restored
    sdr.start()
    yield sdr
    sdr.stop()


def _wait(pred, timeout=20):
    """המתנה מבוססת-מצב עם תקרה — לא sleep קבוע."""
    evt = threading.Event()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        evt.wait(0.01)
    return False


# --- תוכנית ---------------------------------------------------------------------

def test_plan_needs_exactly_two_physical_actions():
    """כל הטעם: שתי פעולות פיזיות בלבד (ניתוק, חיבור), לא אחת לכל תנאי."""
    plan = app._experiment_plan(130.45)
    prompts = [s for s in plan if s["kind"] == "prompt"]
    assert [p["action"] for p in prompts] == ["disconnect", "reconnect"]
    # אותם תנאים בשני המחזורים — אחרת אין מה להשוות
    keys = lambda ph: [s["key"] for s in plan if s.get("phase") == ph and s["kind"] != "prompt"]
    assert keys(1) == keys(2)
    # הניתוק קורה כשה-Pi יושב על 132.000 AGC — המצב שבו ה-AGC אמור להסתיר (משטר B)
    before = plan[plan.index(prompts[0]) - 1]
    assert before["key"] == "atiswin_agc" and before["agc"] is True
    assert any(s["kind"] == "probe" and s["freq"] == 130.45 for s in plan)   # מסלול המוצר עצמו
    assert [s["i"] for s in plan] == list(range(len(plan)))


# --- מקצה לקצה ------------------------------------------------------------------

def test_full_run_two_prompts_summary_and_restore(client, world):
    r = client.post("/api/experiment", json={"action": "start"})
    assert r.status_code == 200 and r.get_json()["running"] is True
    # בזמן הניסוי כל כוונון נחסם (409), כדי שלא ישבשו אותו מהטלפון
    assert client.post("/api/antenna/check", json={"freq": 131.55}).status_code == 409
    assert client.post("/api/rflog", json={"active": False}).status_code == 409

    assert _wait(lambda: (app._exp["waiting"] or {}).get("action") == "disconnect")
    st = client.get("/api/experiment").get_json()
    assert st["waiting"]["action"] == "disconnect" and st["eta_prompt_sec"] == 0
    world.connected = False
    assert client.post("/api/experiment", json={"action": "confirm"}).status_code == 200

    assert _wait(lambda: (app._exp["waiting"] or {}).get("action") == "reconnect")
    world.connected = True
    client.post("/api/experiment", json={"action": "confirm"})

    app._exp["thread"].join(timeout=30)
    st = client.get("/api/experiment").get_json()
    assert st["running"] is False and st["error"] is None
    res = st["result"]
    by = {c["key"]: c for c in res["conditions"]}
    # רווח קבוע: 12dB ירידה — הסף של המוצר (10dB) היה מזהה
    assert by["clean_f20"]["drop"] == pytest.approx(12.0) and by["clean_f20"]["detects"] is True
    # AGC עם ATIS בחלון: 2dB — הסף לא היה מזהה (בדיוק מה שהניסוי נועד לחשוף)
    assert by["atiswin_agc"]["drop"] == pytest.approx(2.0) and by["atiswin_agc"]["detects"] is False
    assert res["atis"]["gone"] is True and res["atis"]["drop"] == pytest.approx(53.0)
    probes = {p["key"]: p for p in res["probes"]}
    assert probes["probe_product"]["detects"] is True
    assert probes["probe_atiswin"]["detects"] is False
    assert res["stale_probes"] == 0
    assert res["after_disconnect"]["instant"] == pytest.approx(-60.0)
    # שוחזר ל-ACARS עם הבנק השמור, והנעילה שוחררה
    assert world.restored and world.restored[-1][0] == "acars"
    assert app.TUNE_LOCK.acquire(blocking=False)
    app.TUNE_LOCK.release()
    assert app._rflog["active"] is False          # הרשם הופעל ע"י הניסוי — ונכבה בסופו


def test_abort_restores_and_releases(client, world):
    client.post("/api/experiment", json={"action": "start"})
    assert _wait(lambda: app._exp["i"] >= 1)
    client.post("/api/experiment", json={"action": "abort"})
    app._exp["thread"].join(timeout=10)
    st = client.get("/api/experiment").get_json()
    assert st["running"] is False and "בוטל" in st["error"]
    assert world.restored                          # גם בביטול — חוזרים למצב הקודם
    assert app.TUNE_LOCK.acquire(blocking=False)
    app.TUNE_LOCK.release()


def test_prompt_timeout_stops_and_restores(client, world, monkeypatch):
    monkeypatch.setattr(app, "EXP_PROMPT_TIMEOUT_SEC", 0.3)
    client.post("/api/experiment", json={"action": "start"})
    app._exp["thread"].join(timeout=30)
    st = client.get("/api/experiment").get_json()
    assert st["running"] is False and "לא התקבל אישור" in st["error"]
    assert world.restored


def test_confirm_without_prompt_is_409(client, world):
    assert client.post("/api/experiment", json={"action": "confirm"}).status_code == 409


# --- תנאי פתיחה -----------------------------------------------------------------

def test_refuses_while_satcom_live(client, paths, monkeypatch):
    """אנטנת L-band מחוברת — מדידת VHF חסרת משמעות (ו-bias-T דולק)."""
    monkeypatch.setattr(app, "_live_mode", lambda: "satcom")
    r = client.post("/api/experiment", json={"action": "start"})
    assert r.status_code == 409 and "SATCOM" in r.get_json()["error"]
    assert not app.TUNE_LOCK.locked()


def test_refuses_while_scanning(client, paths, monkeypatch):
    monkeypatch.setattr(app, "_scan_thread", types.SimpleNamespace(is_alive=lambda: True))
    r = client.post("/api/experiment", json={"action": "start"})
    assert r.status_code == 409 and "סריקה" in r.get_json()["error"]


def test_refuses_when_tune_lock_busy(client, paths, monkeypatch):
    monkeypatch.setattr(app, "_live_mode", lambda: None)
    assert app.TUNE_LOCK.acquire(blocking=False)
    try:
        r = client.post("/api/experiment", json={"action": "start"})
        assert r.status_code == 409
    finally:
        app.TUNE_LOCK.release()


# --- ניתוח (פונקציה טהורה) -------------------------------------------------------

def _row(t, freq, agc, noise, ifgr=None, mtime=None, signal=None):
    return {"t": t, "freq": freq, "agc": agc, "ifgr": ifgr, "noise": noise, "signal": signal,
            "stats_mtime": t if mtime is None else mtime, "conf_mtime": 0}


def test_summary_flags_probe_that_read_previous_process():
    """בדיקת אנטנה שקראה כתיבה עם mtime *לפני* הפעלת התהליך הנוכחי = קראה את
    ה-flush של התהליך הקודם — נספר ב-stale_probes, לא נבלע."""
    rows = [{"ev": "probe", "key": "probe_product", "label": "x", "freq": 130.45, "phase": 1,
             "noise": -61.0, "stats_mtime": 99.0, "proc_start": 100.0},
            {"ev": "probe", "key": "probe_product", "label": "x", "freq": 130.45, "phase": 2,
             "noise": -73.0, "stats_mtime": 201.0, "proc_start": 200.0}]
    res = app._experiment_summary(rows)
    assert res["stale_probes"] == 1
    p = res["probes"][0]
    assert p["p1"]["stale"] is True and p["p2"]["stale"] is False and p["drop"] == 12.0


def test_summary_excludes_previous_process_rows_and_incomplete_steps():
    st = {"ev": "step", "i": 0, "kind": "dwell", "phase": 1, "key": "clean_f20", "label": "x",
          "freq": 122.6, "agc": False, "ifgr": 20, "t_enter": 0.0, "t_ready": 1.0, "proc_start": 0.5}
    rows = [st,
            _row(1.2, 122.6, False, -40.0, 20, mtime=0.4),   # flush של התהליך הקודם — לא נספר
            *[_row(t, 122.6, False, -60.0, 20) for t in (15, 20, 30)],
            {"ev": "step_end", "i": 0, "t": 46.0},
            {**st, "i": 1, "phase": 2, "t_enter": 50.0, "t_ready": 51.0, "proc_start": 50.5}]
    res = app._experiment_summary(rows)
    c = res["conditions"][0]
    assert c["p1"]["steady"] == -60.0
    assert "p2" not in c and c["drop"] is None and c["detects"] is None   # לא ניחוש
    assert res["stale_rows"] == 1


def test_summary_empty_is_all_none():
    res = app._experiment_summary([])
    assert res["conditions"] == [] and res["probes"] == []
    assert res["atis"]["drop"] is None and res["atis"]["gone"] is None


def test_unwritable_log_does_not_leave_experiment_stuck_running(client, world, paths, monkeypatch):
    """⚠ רגרסיה: כשל כתיבה לרשם בתוך ה-finally דילג על סימון הסיום — הניסוי
    נשאר running=True לנצח, וכל התחלה חדשה קיבלה 409 "כבר רץ"."""
    blocker = paths / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setattr(app, "RFLOG_PATH", blocker / "rf_log.jsonl")
    monkeypatch.setattr(app, "EXP_PROMPT_TIMEOUT_SEC", 0.3)
    assert client.post("/api/experiment", json={"action": "start"}).status_code == 200
    app._exp["thread"].join(timeout=30)
    st = client.get("/api/experiment").get_json()
    assert st["running"] is False and st["error"]
    assert world.restored
    assert not app.TUNE_LOCK.locked()

