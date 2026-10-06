# ============================================================================
#  AIR-AM - בדיקות 🩺 בדיקת RF (מעבר על מצבי LNA מעל ATIS) — PR 2 בגרסה הפשוטה,
#  docs/voice-rf-quality-plan.md. בלי חומרה: _enter_voice/_restore_after_probe/
#  קריאת ה-stats/הטלמטריה ממוקפים.
# ============================================================================
import threading
import time

import pytest

import app


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "CONFIG_PATH", tmp_path / "airband.conf")
    monkeypatch.setattr(app, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(app, "STATS_PATH", tmp_path / "stats.txt")
    return tmp_path


@pytest.fixture
def client(paths):
    return app.app.test_client()


@pytest.fixture(autouse=True)
def _reset_rfc():
    with app._rfc_lock:
        app._rfc.update(running=False, started_at=None, finished_at=None, step=None,
                        rows=[], error=None, stop=None)
    yield
    with app._rfc_lock:
        stop = app._rfc.get("stop")
    if stop:
        stop.set()


def _row(lna, snr, spread=0.5, overload=False, events=0, error=None):
    return {"lna": lna, "snr": snr, "snr_spread": spread, "overload": overload,
            "overload_events": events, "error": error}


# --- ההמלצה (פונקציה טהורה) --------------------------------------------------

def test_recommend_overload_disqualifies_even_with_best_snr():
    rows = [_row(0, 30.0, overload=True, events=4), _row(2, 25.0), _row(4, 20.0)]
    rec = app._rfcheck_recommend(rows)
    assert rec["rf_gain"] == 2 and rec["overloaded"] == [0]
    assert rec["confidence"] == "full" and rec["reason"] == "best_snr"


def test_recommend_tie_goes_to_more_attenuation():
    # 4 ו-6 בתוך פיזור המדידה של המיטבי (2) => בוחרים את המוחלש ביותר בשוויון (6)
    rows = [_row(2, 24.0, spread=1.5), _row(4, 23.2), _row(6, 22.8), _row(8, 15.0)]
    rec = app._rfcheck_recommend(rows)
    assert rec["rf_gain"] == 6 and rec["reason"] == "tie_more_attenuation"
    assert rec["best_snr_lna"] == 2


def test_recommend_unknown_overload_is_partial_not_clean():
    rows = [_row(0, 26.0, overload=None), _row(4, 20.0, overload=None)]
    rec = app._rfcheck_recommend(rows)
    assert rec["rf_gain"] == 0 and rec["confidence"] == "partial"


def test_recommend_all_overloaded_and_no_data():
    assert app._rfcheck_recommend([_row(0, 20.0, overload=True)])["reason"] == "all_overloaded"
    assert app._rfcheck_recommend([_row(0, None, error="x")])["reason"] == "no_data"
    assert app._rfcheck_recommend([])["rf_gain"] is None


def test_spread_is_iqr_or_range():
    assert app._spread([5.0]) is None
    assert app._spread([1.0, 3.0]) == 2.0
    assert app._spread([1.0, 2.0, 3.0, 4.0, 100.0]) < 50     # IQR עמיד לחריג בודד


# --- מדידת מצב בודד ------------------------------------------------------------

def test_measure_ignores_previous_process_stats(paths, monkeypatch):
    """flush של התהליך הקודם (mtime לפני העלייה) לא נספר — רק כתיבות של התהליך החדש."""
    monkeypatch.setattr(app, "RFCHECK_SETTLE_SEC", 0.0)
    monkeypatch.setattr(app, "RFCHECK_MEASURE_SEC", 0.8)
    monkeypatch.setattr(app, "_enter_voice", lambda p: (None, None, False))
    born = 1000.0
    monkeypatch.setattr(app, "_rtl_airband_start_wall", lambda: born)
    seq = iter([(born - 1, {"channel_dbfs_noise_level": -90, "channel_dbfs_signal_level": -10}),
                (born + 1, {"channel_dbfs_noise_level": -60, "channel_dbfs_signal_level": -40}),
                (born + 2, {"channel_dbfs_noise_level": -61, "channel_dbfs_signal_level": -40}),
                (born + 3, {"channel_dbfs_noise_level": -59, "channel_dbfs_signal_level": -40})])
    last = [None]

    def snap(want):
        try:
            last[0] = next(seq)
        except StopIteration:
            pass
        return last[0]

    monkeypatch.setattr(app, "_read_stats_snapshot", snap)
    monkeypatch.setattr(app, "_rf_window_summary",
                        lambda a, b: {"overload": False, "overload_events": 0,
                                      "ifgr_min": 40, "ifgr_max": 42})
    row = app._rfcheck_measure(4, False, threading.Event())
    assert row["error"] is None and row["samples"] >= 1
    assert row["noise"] > -70                     # הדגימה של -90 (התהליך הקודם) לא נספרה
    assert row["overload"] is False and row["ifgr_max"] == 42


def test_measure_unknown_telemetry_stays_none(paths, monkeypatch):
    monkeypatch.setattr(app, "RFCHECK_SETTLE_SEC", 0.0)
    monkeypatch.setattr(app, "RFCHECK_MEASURE_SEC", 0.4)
    monkeypatch.setattr(app, "_enter_voice", lambda p: (None, None, False))
    monkeypatch.setattr(app, "_rtl_airband_start_wall", lambda: 0.0)
    monkeypatch.setattr(app, "_read_stats_snapshot",
                        lambda w: (time.time(), {"channel_dbfs_noise_level": -60,
                                                 "channel_dbfs_signal_level": -35}))
    monkeypatch.setattr(app, "_rf_window_summary", lambda a, b: None)
    row = app._rfcheck_measure(0, False, threading.Event())
    assert row["snr"] == 25.0 and row["overload"] is None   # "לא ידוע", לא "תקין"


def test_measure_enter_failure_is_reported(paths, monkeypatch):
    monkeypatch.setattr(app, "_enter_voice", lambda p: ("rtl_airband נכשל", "x", False))
    row = app._rfcheck_measure(2, False, threading.Event())
    assert row["error"] == "rtl_airband נכשל" and row["snr"] is None


# --- ה-API: ריצה מלאה, סירובים, ביטול, החלה ----------------------------------

@pytest.fixture
def fake_run(monkeypatch):
    """מדידה מזויפת: עומס ב-LNA 0, SNR יורד עם ההנחתה. מקליט את קריאות השחזור."""
    restores = []
    monkeypatch.setattr(app, "_rfcheck_measure",
                        lambda lna, notch, stop: _row(lna, 30.0 - 2 * lna, overload=(lna == 0)))
    monkeypatch.setattr(app, "_restore_after_probe", lambda prev, live: restores.append(live))
    monkeypatch.setattr(app, "_live_mode", lambda: "voice")
    return restores


def _wait_done(timeout=5.0):
    t = time.time()
    while time.time() - t < timeout:
        with app._rfc_lock:
            if not app._rfc["running"]:
                return
        time.sleep(0.02)
    raise AssertionError("הבדיקה לא הסתיימה")


def test_full_run_restores_once_releases_lock_and_saves(client, fake_run):
    r = client.post("/api/rfcheck", json={"action": "start"})
    assert r.status_code == 200 and r.get_json()["running"] is True
    _wait_done()
    assert fake_run == ["voice"]                      # שחזור בדיוק פעם אחת
    assert app.TUNE_LOCK.acquire(blocking=False)      # הנעילה שוחררה
    app.TUNE_LOCK.release()
    d = client.get("/api/rfcheck").get_json()
    res = d["result"]
    assert [x["lna"] for x in res["rows"]] == list(app.RFCHECK_STATES)
    assert res["recommendation"]["rf_gain"] == 2      # 0 בעומס ⇒ המיטבי הבא
    assert res["freq"] == app.RFCHECK_FREQ and d["error"] is None


def test_refusals(client, fake_run, monkeypatch):
    monkeypatch.setattr(app, "_live_mode", lambda: "satcom")
    assert client.post("/api/rfcheck", json={"action": "start"}).status_code == 409
    monkeypatch.setattr(app, "_live_mode", lambda: "voice")
    app.TUNE_LOCK.acquire()
    try:
        assert client.post("/api/rfcheck", json={"action": "start"}).status_code == 409
    finally:
        app.TUNE_LOCK.release()
    assert client.post("/api/rfcheck", json={"action": "bogus"}).status_code == 400


def test_abort_restores_and_saves_nothing(client, monkeypatch):
    restores = []
    monkeypatch.setattr(app, "_live_mode", lambda: "off")
    monkeypatch.setattr(app, "_restore_after_probe", lambda prev, live: restores.append(live))

    def slow(lna, notch, stop):
        stop.wait(5)
        return _row(lna, 20.0)

    monkeypatch.setattr(app, "_rfcheck_measure", slow)
    client.post("/api/rfcheck", json={"action": "start"})
    client.post("/api/rfcheck", json={"action": "abort"})
    _wait_done()
    d = client.get("/api/rfcheck").get_json()
    assert restores == ["off"] and d["error"] == "בוטל" and d["result"] is None


def test_exception_still_restores_and_releases(client, monkeypatch):
    restores = []
    monkeypatch.setattr(app, "_live_mode", lambda: "voice")
    monkeypatch.setattr(app, "_restore_after_probe", lambda prev, live: restores.append(live))
    monkeypatch.setattr(app, "_rfcheck_measure", lambda *a: 1 / 0)
    client.post("/api/rfcheck", json={"action": "start"})
    _wait_done()
    assert restores == ["voice"]
    assert app.TUNE_LOCK.acquire(blocking=False)
    app.TUNE_LOCK.release()
    assert client.get("/api/rfcheck").get_json()["error"]


def test_apply_in_voice_goes_through_voice_tune(client, fake_run, monkeypatch):
    app.save_state({**app.DEFAULT_STATE, "app_mode": "voice", "rf_gain": 4,
                    "rf_check_last": {"recommendation": {"rf_gain": 6}}})
    calls = []
    monkeypatch.setattr(app, "_voice_tune", lambda p: (calls.append(p) or {"ok": True}, 200))
    r = client.post("/api/rfcheck", json={"action": "apply"})
    assert r.status_code == 200 and calls[0]["rf_gain"] == 6
    assert calls[0]["freq"] == app.DEFAULT_STATE["freq"]   # שאר ההגדרות נשמרות


def test_apply_already_in_effect_and_not_voice(client, monkeypatch):
    app.save_state({**app.DEFAULT_STATE, "app_mode": "voice", "rf_gain": 6,
                    "rf_check_last": {"recommendation": {"rf_gain": 6}}})
    assert client.post("/api/rfcheck", json={"action": "apply"}).status_code == 409
    app.save_state({**app.DEFAULT_STATE, "app_mode": "off", "rf_gain": 4,
                    "rf_check_last": {"recommendation": {"rf_gain": 8}}})
    monkeypatch.setattr(app, "_live_mode", lambda: None)
    r = client.post("/api/rfcheck", json={"action": "apply"})
    assert r.status_code == 200 and app.load_state()["rf_gain"] == 8


def test_apply_without_recommendation(client):
    app.save_state({**app.DEFAULT_STATE, "rf_check_last": {"recommendation": {"rf_gain": None}}})
    assert client.post("/api/rfcheck", json={"action": "apply"}).status_code == 409


def test_health_ok_while_running(client, monkeypatch):
    monkeypatch.setattr(app, "_services_status",
                        lambda svcs: {s: "inactive" for s in svcs})
    monkeypatch.setattr(app, "_sdr_present", lambda: True)
    app.save_state({**app.DEFAULT_STATE, "app_mode": "voice"})
    with app._rfc_lock:
        app._rfc["running"] = True
    try:
        h = client.get("/api/health").get_json()
        assert h["rf_check"] is True and h["ok"] is True   # rtl_airband בהחלפה ≠ תקלה
    finally:
        with app._rfc_lock:
            app._rfc["running"] = False
