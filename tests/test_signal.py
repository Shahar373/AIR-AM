# ============================================================================
#  AIR-AM - בדיקות יחידה למד השדה המאוחד (/api/signal) ולבדיקת האנטנה
#  (/api/antenna/check) — ר' docs/field-station-roadmap.md.
# ----------------------------------------------------------------------------
#  רץ בלי חומרה: כל נתיבי הקבצים מנותבים ל-tmp, ו-restart/systemctl ממוקפים.
# ============================================================================
import threading
import time
import types

import pytest

import app


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "CONFIG_PATH", tmp_path / "airband.conf")
    monkeypatch.setattr(app, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(app, "STATS_PATH", tmp_path / "stats.txt")
    monkeypatch.setattr(app, "ACARS_ENV_PATH", tmp_path / "acars.env")
    monkeypatch.setattr(app, "VDL2_ENV_PATH", tmp_path / "vdl2.env")
    monkeypatch.setattr(app, "SATCOM_ENV_PATH", tmp_path / "satcom.env")
    return tmp_path


@pytest.fixture
def client(paths):
    return app.app.test_client()


@pytest.fixture(autouse=True)
def _clean_state_corrupt_flag():
    app._reset_state_corrupt_warned()
    yield
    app._reset_state_corrupt_warned()


@pytest.fixture(autouse=True)
def _stop_leftover_scan():
    """מבטיח שאף thread סריקה לא דולף בין בדיקות (כמו ב-test_scan.py)."""
    yield
    app._scan_stop_thread()


def _clear_acars():
    with app._acars_lock:
        app._acars_msgs.clear()


def _clear_vdl2():
    with app._vdl2_lock:
        app._vdl2_msgs.clear()


def _write_stats(path, freq_mhz, sig, noise, age_sec=0.0):
    """כותב שורת stats תואמת rtl_airband לתדר נתון, עם mtime בעבר של age_sec."""
    freq_label = f"{freq_mhz:.3f}"
    text = (
        f'channel_dbfs_signal_level{{freq="{freq_label}"}} {sig}\n'
        f'channel_dbfs_noise_level{{freq="{freq_label}"}} {noise}\n'
        f'channel_squelch_counter{{freq="{freq_label}"}} 0\n'
    )
    path.write_text(text)
    if age_sec:
        old = time.time() - age_sec
        import os
        os.utime(path, (old, old))


# --- _signal_verdict (פונקציה טהורה) ----------------------------------------

def test_verdict_unknown_without_current_reading():
    assert app._signal_verdict(None, {"noise": -70.0}) == "unknown"


def test_verdict_no_baseline():
    assert app._signal_verdict(-80.0, None) == "no_baseline"
    assert app._signal_verdict(-80.0, {"noise": None}) == "no_baseline"


def test_verdict_ok_when_close_to_baseline():
    assert app._signal_verdict(-76.0, {"noise": -74.0}) == "ok"


def test_verdict_below_baseline_at_exact_threshold():
    # DISCONNECT_DROP_DB=10.0 — ירידה בדיוק בגודל הסף כבר נחשבת חריגה (>=)
    assert app._signal_verdict(-84.0, {"noise": -74.0}) == "below_baseline"
    assert app._signal_verdict(-83.9, {"noise": -74.0}) == "ok"


# --- GET /api/signal ---------------------------------------------------------

def test_signal_off_mode_reports_none_kind(client, monkeypatch):
    monkeypatch.setattr(app, "_live_mode", lambda: None)
    r = client.get("/api/signal")
    data = r.get_json()
    assert data["ok"] is True
    assert data["mode"] == "off"
    assert data["kind"] == "none"
    assert data["verdict"] == "unknown"


def test_signal_voice_continuous_with_baseline(client, paths, monkeypatch):
    monkeypatch.setattr(app, "_live_mode", lambda: "voice")
    st = app.load_state()
    st["freq"] = 121.500
    st["agc"] = True
    st["signal_baseline"] = {"noise": -74.0, "freq": 121.5, "ts": 1.0}
    app.save_state(st)
    _write_stats(paths / "stats.txt", 121.500, sig=-40.0, noise=-73.0)

    r = client.get("/api/signal")
    data = r.get_json()
    assert data["mode"] == "voice"
    assert data["kind"] == "continuous"
    assert data["fresh"] is True
    assert data["noise"] == -73.0
    assert data["snr"] == pytest.approx(33.0)
    assert data["verdict"] == "ok"   # -74 - (-73) = -1dB, מתחת לסף


def test_signal_voice_manual_gain_skips_verdict(client, paths, monkeypatch):
    """gain ידני => לא בר-השוואה לבסיס (נמדד תחת AGC) => 'unknown', לא ניחוש."""
    monkeypatch.setattr(app, "_live_mode", lambda: "voice")
    st = app.load_state()
    st["freq"] = 121.500
    st["agc"] = False
    st["signal_baseline"] = {"noise": -74.0, "freq": 121.5, "ts": 1.0}
    app.save_state(st)
    _write_stats(paths / "stats.txt", 121.500, sig=-40.0, noise=-96.0)

    r = client.get("/api/signal")
    data = r.get_json()
    assert data["verdict"] == "unknown"


def test_signal_acars_last_message_no_verdict(client, monkeypatch):
    monkeypatch.setattr(app, "_live_mode", lambda: "acars")
    _clear_acars()
    with app._acars_lock:
        app._acars_msgs.append({"id": 1, "t": time.time() - 5, "level": -22.5,
                                "snr": None, "freq": "131.550"})
    r = client.get("/api/signal")
    data = r.get_json()
    assert data["mode"] == "acars"
    assert data["kind"] == "last-message"
    assert data["signal"] == -22.5
    assert data["snr"] is None            # ACARS לעולם לא מקבל SNR (§12)
    assert data["fresh"] is True
    # אין בסיס רלוונטי ל"הודעה אחרונה" — רק בדיקת אנטנה יזומה נותנת פסק דין
    assert data["verdict"] == "unknown"
    assert data["baseline"] is None
    _clear_acars()


def test_signal_vdl2_last_message_has_snr(client, monkeypatch):
    monkeypatch.setattr(app, "_live_mode", lambda: "vdl2")
    _clear_vdl2()
    with app._vdl2_lock:
        app._vdl2_msgs.append({"id": 1, "t": time.time() - 400, "level": -30.0,
                               "snr": 12.0, "freq": "136.975"})
    r = client.get("/api/signal")
    data = r.get_json()
    assert data["mode"] == "vdl2"
    assert data["snr"] == 12.0
    assert data["fresh"] is False   # מעל SIGNAL_LAST_MSG_MAX_AGE (300s)
    _clear_vdl2()


def test_signal_acars_no_messages_yet(client, monkeypatch):
    monkeypatch.setattr(app, "_live_mode", lambda: "acars")
    _clear_acars()
    r = client.get("/api/signal")
    data = r.get_json()
    assert data["kind"] == "last-message"
    assert data["fresh"] is False
    assert data["verdict"] == "unknown"


def test_signal_satcom_points_to_dedicated_panel(client, monkeypatch):
    monkeypatch.setattr(app, "_live_mode", lambda: "satcom")
    r = client.get("/api/signal")
    data = r.get_json()
    assert data["kind"] == "satcom-panel"
    assert data["signal"] is None and data["noise"] is None


# --- POST /api/antenna/check --------------------------------------------------

def _ok(**kw):
    return types.SimpleNamespace(returncode=0, stdout="", stderr="", **kw)


def test_antenna_check_busy_returns_409(client):
    assert app.TUNE_LOCK.acquire(blocking=False)   # מדמה כיוונון/מעבר מצב אחר שכבר רץ
    try:
        r = client.post("/api/antenna/check", json={"freq": 121.5})
        assert r.status_code == 409
        assert r.get_json()["ok"] is False
    finally:
        app.TUNE_LOCK.release()


def test_antenna_check_switches_samples_and_restores(client, paths, monkeypatch):
    """מדמה בדיקת אנטנה מתוך ACARS: נכנס לקול, דוגם, וחוזר ל-ACARS."""
    monkeypatch.setattr(app, "_live_mode", lambda: "acars")
    calls = []

    def fake_enter_voice(params):
        calls.append(("voice", params["freq"]))
        _write_stats(paths / "stats.txt", params["freq"], sig=-30.0, noise=-72.0)
        return None, None, False

    def fake_enter_acars(freqs):
        calls.append(("acars", freqs))
        return None, None

    monkeypatch.setattr(app, "_enter_voice", fake_enter_voice)
    monkeypatch.setattr(app, "_enter_acars", fake_enter_acars)
    monkeypatch.setattr(app, "ANTENNA_CHECK_SAMPLE_SEC", 1.0)

    st = app.load_state()
    st["acars_freqs"] = ["131.550"]
    app.save_state(st)

    r = client.post("/api/antenna/check", json={"freq": 121.5})
    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    assert data["noise"] == -72.0
    assert data["snr"] == pytest.approx(42.0)
    # שוחזר ל-ACARS אחרי הבדיקה, ובדיוק עם התדרים שהיו שמורים
    assert calls == [("voice", 121.5), ("acars", ["131.550"])]
    # פעולת אבחון בלבד — לא נוגעת ב-app_mode השמור
    assert app.load_state()["app_mode"] == "off"


def test_antenna_check_calibrate_saves_baseline(client, paths, monkeypatch):
    monkeypatch.setattr(app, "_live_mode", lambda: "vdl2")
    monkeypatch.setattr(app, "_enter_voice",
                        lambda params: (_write_stats(paths / "stats.txt", params["freq"],
                                                     sig=-35.0, noise=-70.0) or (None, None, False)))
    monkeypatch.setattr(app, "_enter_vdl2", lambda freqs: (None, None))
    monkeypatch.setattr(app, "ANTENNA_CHECK_SAMPLE_SEC", 1.0)

    r = client.post("/api/antenna/check", json={"freq": 136.975, "calibrate": True})
    assert r.status_code == 200
    data = r.get_json()
    assert data["calibrated"] is True
    assert data["baseline"]["noise"] == -70.0

    saved = app.load_state()["signal_baseline"]
    assert saved["noise"] == -70.0
    assert saved["freq"] == 136.975

    # בדיקה חוזרת בלי כיול: verdict נשווה עכשיו מול הבסיס שנשמר
    monkeypatch.setattr(app, "_enter_voice",
                        lambda params: (_write_stats(paths / "stats.txt", params["freq"],
                                                     sig=-50.0, noise=-95.0) or (None, None, False)))
    r2 = client.post("/api/antenna/check", json={"freq": 136.975})
    data2 = r2.get_json()
    assert data2["calibrated"] is False
    assert data2["verdict"] == "below_baseline"   # -70 - (-95) = 25dB ירידה


def test_antenna_check_already_in_voice_at_same_freq_skips_switch(client, paths, monkeypatch):
    """כבר בקול על אותו תדר => אין restart מיותר, רק קריאה ישירה."""
    monkeypatch.setattr(app, "_live_mode", lambda: "voice")
    st = app.load_state()
    st["freq"] = 121.5
    app.save_state(st)
    _write_stats(paths / "stats.txt", 121.5, sig=-33.0, noise=-71.0)

    def boom(*a, **k):
        raise AssertionError("לא אמור להיכנס מחדש לקול — כבר שם")
    monkeypatch.setattr(app, "_enter_voice", boom)

    r = client.post("/api/antenna/check", json={"freq": 121.5})
    assert r.status_code == 200
    assert r.get_json()["noise"] == -71.0


def test_antenna_check_enter_voice_failure_restores_and_reports_error(client, paths, monkeypatch):
    monkeypatch.setattr(app, "_live_mode", lambda: "acars")
    restored = []
    monkeypatch.setattr(app, "_enter_voice", lambda params: ("נכשל", "detail", False))
    monkeypatch.setattr(app, "_enter_acars", lambda freqs: restored.append(freqs) or (None, None))

    r = client.post("/api/antenna/check", json={"freq": 121.5})
    assert r.status_code == 500
    assert r.get_json()["ok"] is False
    assert restored   # best-effort ניסה לשחזר את ACARS למרות הכשל
    assert app.load_state()["app_mode"] == "off"   # ולא נגע ב-state.json


def test_antenna_check_no_fresh_data_returns_504(client, paths, monkeypatch):
    monkeypatch.setattr(app, "_live_mode", lambda: "acars")
    monkeypatch.setattr(app, "_enter_voice", lambda params: (None, None, False))
    monkeypatch.setattr(app, "_enter_acars", lambda freqs: (None, None))
    monkeypatch.setattr(app, "ANTENNA_CHECK_SAMPLE_SEC", 0.05)   # לא כותבים stats.txt בכלל

    r = client.post("/api/antenna/check", json={"freq": 121.5})
    assert r.status_code == 504
    assert r.get_json()["ok"] is False


def test_antenna_check_restores_active_scan_leg_not_state_bank(client, paths, monkeypatch):
    """תוך כדי סבב סריקה עם רגל שיש לה freqs מפורשים (שונים מהבנק הכללי השמור
    ב-state.json) — בדיקת אנטנה חייבת לשחזר בדיוק את הרגל הרצה בפועל, לא את
    הבנק הכללי (אחרת ה-thread הפעיל נשאר תקוע על בנק שגוי, ר' _restore_after_probe)."""
    scan_leg = {"mode": "acars", "freqs": ["131.550"], "dwell_sec": 600}
    stop_evt = threading.Event()
    th = threading.Thread(target=stop_evt.wait, daemon=True)
    th.start()
    with app._scan_lock:
        app._scan_thread = th
        app._scan_thread_stop = stop_evt
        app._scan_status.update(idx=0, leg=scan_leg, next_switch_at=time.time() + 600, plan=[scan_leg])

    st = app.load_state()
    st["acars_freqs"] = ["136.700"]      # בנק כללי שונה מהרגל הפעילה בפועל
    app.save_state(st)

    monkeypatch.setattr(app, "_live_mode", lambda: "acars")
    calls = []
    monkeypatch.setattr(app, "_enter_voice",
                        lambda params: (_write_stats(paths / "stats.txt", params["freq"],
                                                     sig=-30.0, noise=-72.0) or (None, None, False)))
    monkeypatch.setattr(app, "_enter_acars", lambda freqs: calls.append(freqs) or (None, None))
    monkeypatch.setattr(app, "ANTENNA_CHECK_SAMPLE_SEC", 1.0)

    r = client.post("/api/antenna/check", json={"freq": 121.5})
    assert r.status_code == 200
    assert calls == [["131.550"]]        # שוחזרה הרגל הפעילה בפועל, לא ["136.700"]
