# ============================================================================
#  AIR-AM - רשם ניסוי RF (/api/rflog) — docs/antenna-calibration-experiment.md
# ----------------------------------------------------------------------------
#  הרשם קיים כדי שניסוי כיול האנטנה בשטח יפיק נתונים שאפשר לסמוך עליהם:
#  כל שורה = כתיבה חדשה של stats, עם הקונפיג *שבאמת רץ* (airband.conf).
#  סנכרון עם ה-thread דרך join/Event בלבד — בלי sleep (ר' no_sleep ב-CHANGELOG).
# ============================================================================
import json
import os
import time

import pytest

import app


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "CONFIG_PATH", tmp_path / "airband.conf")
    monkeypatch.setattr(app, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(app, "STATS_PATH", tmp_path / "stats.txt")
    monkeypatch.setattr(app, "RFLOG_PATH", tmp_path / "rf_log.jsonl")
    return tmp_path


@pytest.fixture
def client(paths):
    return app.app.test_client()


@pytest.fixture(autouse=True)
def _rflog_clean():
    yield
    app._rflog_stop("test-teardown")
    with app._rflog_lock:
        app._rflog.update(active=False, started_at=None, until=None, rows=0,
                          marks=0, thread=None, stop=None)


def _write_conf(paths, freq=121.5, agc=True, if_gain=20, rf_gain=0, sq="open"):
    (paths / "airband.conf").write_text(
        app.render_config(freq, "am", agc, if_gain, rf_gain, sq, 9.0))


def _write_stats(paths, freq, sig, noise, mtime=None):
    lbl = f"{freq:.3f}"
    p = paths / "stats.txt"
    p.write_text(f'channel_dbfs_signal_level{{freq="{lbl}"}} {sig}\n'
                 f'channel_dbfs_noise_level{{freq="{lbl}"}} {noise}\n')
    if mtime is not None:
        os.utime(p, (mtime, mtime))


def _rows(paths):
    f = paths / "rf_log.jsonl"
    return [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines()] if f.exists() else []


# --- הקונפיג שבאמת רץ ---------------------------------------------------------

def test_parse_conf_agc_has_no_gain_line():
    c = app._parse_airband_conf(app.render_config(132.5, "am", True, 40, 4, "open", 9.0))
    assert c["freq"] == 132.5             # שורת ה-channel, לא centerfreq (=132.8)
    assert c["agc"] is True and c["ifgr"] is None and c["rfgr"] is None
    assert c["squelch_snr"] == 0.0 and c["mod"] == "am"


def test_parse_conf_manual_gain_and_auto_squelch():
    c = app._parse_airband_conf(app.render_config(131.55, "am", False, 20, 0, "auto", 9.0))
    assert c["agc"] is False and c["ifgr"] == 20 and c["rfgr"] == 0
    assert c["squelch_snr"] is None       # auto = אין שורה, לא ניחוש של ערך


# --- דגימה ---------------------------------------------------------------------

def test_sample_writes_only_on_new_stats(paths):
    _write_conf(paths, 121.5, agc=False)
    _write_stats(paths, 121.5, -40.0, -61.5, mtime=1000.0)
    row, sig = app._rflog_sample(None)
    assert row["freq"] == 121.5 and row["agc"] is False and row["ifgr"] == 20
    assert row["noise"] == -61.5 and row["signal"] == -40.0
    assert row["stats_mtime"] == 1000.0
    # אותה חתימה => אין שורה (stats קפוא במצבים שאינם קול לא ממלא את הקובץ)
    row2, sig2 = app._rflog_sample(sig)
    assert row2 is None and sig2 == sig
    _write_stats(paths, 121.5, -40.0, -72.0, mtime=1001.0)
    row3, _ = app._rflog_sample(sig)
    assert row3["noise"] == -72.0


def test_sample_other_freq_stats_are_none_not_guessed(paths):
    """stats של תהליך קודם על תדר אחר => None, לא הערך שלו (§12)."""
    _write_conf(paths, 131.55)
    _write_stats(paths, 121.5, -40.0, -61.5, mtime=1000.0)
    row, _ = app._rflog_sample(None)
    assert row["freq"] == 131.55 and row["noise"] is None and row["signal"] is None


def test_sample_exposes_previous_process_flush_via_mtimes(paths):
    """אותו תדר, stats שנכתב *לפני* הקונפיג הנוכחי => חשוד כ-flush-יציאה של
    התהליך הקודם. הרשם לא מסנן — הוא חושף את שני ה-mtime כדי שהניתוח יכריע."""
    _write_stats(paths, 121.5, -40.0, -61.5, mtime=1000.0)
    _write_conf(paths, 121.5, agc=True)
    os.utime(paths / "airband.conf", (1005.0, 1005.0))
    row, _ = app._rflog_sample(None)
    assert row["stats_mtime"] < row["conf_mtime"]


# --- API --------------------------------------------------------------------------

def test_mark_rejected_when_off(client):
    r = client.post("/api/rflog/mark", json={"label": "מנותק"})
    assert r.status_code == 409 and r.get_json()["ok"] is False


def test_start_mark_stop_roundtrip(client, paths, monkeypatch):
    monkeypatch.setattr(app, "RFLOG_POLL_SEC", 0.05)
    r = client.post("/api/rflog", json={"active": True})
    assert r.get_json()["active"] is True
    # "נותרו" מחושב בשרת (שעון הטלפון בשטח לא בהכרח מסונכרן עם ה-Pi)
    assert 0 < r.get_json()["remaining"] <= app.RFLOG_MAX_SEC
    r = client.post("/api/rflog/mark", json={"label": "  מנותק  "})
    assert r.status_code == 200 and r.get_json()["label"] == "מנותק"
    assert r.get_json()["marks"] == 1
    th = app._rflog["thread"]
    r = client.post("/api/rflog", json={"active": False})
    assert r.get_json()["active"] is False and r.get_json()["remaining"] is None
    assert not th.is_alive()
    evs = [row.get("ev") for row in _rows(paths)]
    assert evs[0] == "start" and "mark" in evs and evs[-1] == "stop"
    assert _rows(paths)[-1]["reason"] == "user"


def test_start_is_idempotent(client, paths):
    client.post("/api/rflog", json={"active": True})
    first = app._rflog["thread"]
    client.post("/api/rflog", json={"active": True})
    assert app._rflog["thread"] is first
    assert [r["ev"] for r in _rows(paths)].count("start") == 1


def test_label_is_truncated(client, paths):
    client.post("/api/rflog", json={"active": True})
    r = client.post("/api/rflog/mark", json={"label": "א" * 200})
    assert len(r.get_json()["label"]) == app.RFLOG_LABEL_MAX


def test_auto_stop_after_max_duration(client, paths, monkeypatch):
    """לא כותבים ל-SD לנצח אם שכחו לכבות — כיבוי אוטומטי עם סיבה מפורשת."""
    monkeypatch.setattr(app, "RFLOG_MAX_SEC", 0)
    client.post("/api/rflog", json={"active": True})
    app._rflog["thread"].join(timeout=3)
    assert app._rflog["active"] is False
    last = _rows(paths)[-1]
    assert last["ev"] == "stop" and last["reason"] == "timeout"


def test_worker_records_live_rows(client, paths, monkeypatch):
    monkeypatch.setattr(app, "RFLOG_POLL_SEC", 0.02)
    _write_conf(paths, 121.5, agc=True)
    _write_stats(paths, 121.5, -35.0, -58.0, mtime=2000.0)
    client.post("/api/rflog", json={"active": True})
    # המתנה מבוססת-מצב (לא sleep קבוע): עד שנרשמה שורת מדידה, תקרה 3ש'
    deadline = time.time() + 3
    while time.time() < deadline and not any("noise" in r for r in _rows(paths)):
        app._rflog["stop"].wait(0.02)
    client.post("/api/rflog", json={"active": False})
    data = [r for r in _rows(paths) if "noise" in r]
    assert data and data[0]["noise"] == -58.0 and data[0]["agc"] is True


def test_export_404_then_file(client, paths):
    assert client.get("/api/rflog/export").status_code == 404
    client.post("/api/rflog", json={"active": True})
    client.post("/api/rflog", json={"active": False})
    r = client.get("/api/rflog/export")
    assert r.status_code == 200
    assert "attachment" in r.headers.get("Content-Disposition", "")
    assert b'"ev": "start"' in r.data


def test_antenna_check_result_is_logged_when_recording(client, paths, monkeypatch):
    """תוצאת בדיקת האנטנה (כולל verdict) היא בדיוק מה שהניסוי בודק — נרשמת."""
    monkeypatch.setattr(app, "_live_mode", lambda: "acars")
    monkeypatch.setattr(app, "ANTENNA_CHECK_SAMPLE_SEC", 1.0)

    def fake_enter_voice(params):
        _write_stats(paths, params["freq"], -30.0, -70.0)
        return None, None, False
    monkeypatch.setattr(app, "_enter_voice", fake_enter_voice)
    monkeypatch.setattr(app, "_enter_acars", lambda freqs: (None, None))
    client.post("/api/rflog", json={"active": True})
    r = client.post("/api/antenna/check", json={"freq": 131.55, "calibrate": True})
    assert r.status_code == 200
    probes = [row for row in _rows(paths) if row.get("ev") == "probe"]
    assert probes and probes[0]["calibrate"] is True and probes[0]["noise"] == -70.0
    assert probes[0]["verdict"] == "ok" and probes[0]["baseline_noise"] == -70.0


def test_antenna_check_not_logged_when_off(client, paths, monkeypatch):
    monkeypatch.setattr(app, "_live_mode", lambda: "acars")
    monkeypatch.setattr(app, "ANTENNA_CHECK_SAMPLE_SEC", 1.0)
    monkeypatch.setattr(app, "_enter_voice",
                        lambda p: (_write_stats(paths, p["freq"], -30.0, -70.0) or (None, None, False)))
    monkeypatch.setattr(app, "_enter_acars", lambda freqs: (None, None))
    client.post("/api/antenna/check", json={"freq": 131.55})
    assert _rows(paths) == []
