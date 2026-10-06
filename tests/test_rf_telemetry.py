# ============================================================================
#  AIR-AM - טלמטריית RF מהחומרה (docs/voice-rf-quality-plan.md, PR 1 · 1.5/1.6)
# ----------------------------------------------------------------------------
#  עוקב היומן (journalctl -f) נבדק עם Popen מזויף — אף בדיקה לא מריצה
#  journalctl אמיתי. ה-sidecar ‏<file>.mp3.rf.json נבדק בכל מקום שההקלטה נוסעת
#  אליו (★, retention, סשן, ZIP), כמו ה-sidecar של התמלול.
# ============================================================================
import collections
import io
import json
import os
import time
import zipfile

import pytest

import app
import adsb


NAME = "airam_20260611_120001_134600000.mp3"


@pytest.fixture(autouse=True)
def _clean_rf():
    """מצב הטלמטריה גלובלי (module-level) — מאפסים לפני ואחרי כל בדיקה."""
    def reset():
        with app._rf_lock:
            app._rf.update(follower_since=None, session_start=None, overload=None,
                           overload_events=0, last_overload_t=None, ifgr=None, lna_grdb=None)
            app._rf_events.clear()
    reset()
    yield
    reset()


@pytest.fixture
def paths(tmp_path, monkeypatch):
    for name, fn in (("CONFIG_PATH", "airband.conf"), ("STATE_PATH", "state.json"),
                     ("STATS_PATH", "stats.txt"), ("ACTIVITY_PATH", "activity.jsonl"),
                     ("SOAPY_RF_MARK", "soapysdrplay3.build-sig")):
        monkeypatch.setattr(app, name, tmp_path / fn)
    rec = tmp_path / "recordings"
    rec.mkdir()
    monkeypatch.setattr(app, "REC_DIR", rec)
    monkeypatch.setattr(app, "SESSIONS_DIR", tmp_path / "sessions")
    return tmp_path


@pytest.fixture
def client(paths):
    return app.app.test_client()


@pytest.fixture
def mark(paths):
    (paths / "soapysdrplay3.build-sig").write_text("48bd8b4+airam-rf\n")
    return paths


def _following(t=None):
    with app._rf_lock:
        app._rf["follower_since"] = time.time() - 3600 if t is None else t
        app._rf_events.append({"t": app._rf["follower_since"], "ev": "follow", "up": True})


# --- פענוח שורות ----------------------------------------------------------------

def _stream_start(now=None):
    """שורת "AIRAM_RF stream=start" של ה-patch — גבול-סשן *והוכחת-חיים*."""
    assert app._rf_handle_line("[INFO] AIRAM_RF stream=start\n", now=now) == "start"


def test_handle_line_overload_with_soapy_prefix_and_ansi():
    """regex לא מעוגן: ‏"[INFO] " של SoapySDR, וגם עטיפת ANSI של רמות אחרות."""
    _following()
    _stream_start()
    assert app._rf_handle_line("[INFO] AIRAM_RF overload=1\n", now=100.0) == "overload"
    assert app._rf["overload"] is True and app._rf["overload_events"] == 1
    assert app._rf["last_overload_t"] == 100.0
    assert app._rf_handle_line("\x1b[1m\x1b[33m[WARNING] AIRAM_RF overload=0\x1b[0m") == "overload"
    assert app._rf["overload"] is False and app._rf["overload_events"] == 1   # Corrected אינו אירוע


def test_handle_line_gain_raw_values():
    assert app._rf_handle_line("[INFO] AIRAM_RF gain grdb=37 lna_grdb=24") == "gain"
    assert app._rf["ifgr"] == 37 and app._rf["lna_grdb"] == 24
    # בלי סינון טווח (ה-spec לא מגדיר אחד) — ערך גולמי כמו שהוא
    app._rf_handle_line("AIRAM_RF gain grdb=255 lna_grdb=0")
    assert app._rf["ifgr"] == 255


def test_handle_line_ignores_unrelated_and_malformed():
    for ln in ("", "random waterfall line", "AIRAM_RF overload=2", "AIRAM_RF overload=10",
               "AIRAM_RF gain grdb=x lna_grdb=1", "AIRAM_RF gain grdb=-3 lna_grdb=1"):
        assert app._rf_handle_line(ln) is None
    assert app._rf["overload"] is None and app._rf["ifgr"] is None


def test_stream_start_resets_session_even_without_airam_restart():
    """Restart=always אחרי קריסה: שורת stream=start של התהליך החדש היא גבול הסשן —
    עומס של התהליך שמת לא נשאר דלוק על החדש — והיא גם הראיה ש"אין עומס"."""
    _following()
    app._rf_handle_line("AIRAM_RF overload=1")
    app._rf_handle_line("AIRAM_RF gain grdb=30 lna_grdb=10")
    _stream_start(now=500.0)
    assert app._rf["overload"] is False and app._rf["overload_events"] == 0
    assert app._rf["ifgr"] is None and app._rf["session_start"] == 500.0


def test_rtl_airband_init_line_is_not_a_session_boundary():
    """שורת האתחול של rtl_airband עוברת ב-syslog — מקור אחר בלי הבטחת סדר מול
    ה-stderr של AIRAM_RF. אם היא מגיעה *אחרי* overload=1 של אותו סשן (journald
    בעומס), איפוס היה מציג "אין עומס" ירוק בזמן שהחומרה רוויה."""
    _following()
    _stream_start()
    app._rf_handle_line("AIRAM_RF overload=1")
    assert app._rf_handle_line(
        "SoapySDR: device 'driver=sdrplay,rfnotch_ctrl=false,rfgain_sel=4' initialized") is None
    assert app._rf["overload"] is True and app._rf["overload_events"] == 1


def test_session_reset_is_unknown_not_false_even_while_following():
    """עוקב מחובר אינו ראיה שהוא *רואה* משהו (מודול לא-מתוקן, רמת log, הרשאות
    יומן) — אחרי restart יזום overload=None עד stream=start, לעולם לא False (§12)."""
    app._rf_session_reset("restart")
    assert app._rf["overload"] is None
    _following()
    app._rf_session_reset("restart")
    assert app._rf["overload"] is None


def test_blind_follower_never_reports_no_overload(mark, monkeypatch):
    """העוקב רץ ולא מקבל אף שורה (למשל airam בלי הרשאת יומן — journalctl ממשיך
    בשקט, ה-stderr שלו ב-DEVNULL) => גם /api/metrics וגם ה-sidecar אומרים "לא ידוע"."""
    seen = {}

    def during(_ln):
        # בזמן שהעוקב "קורא" (שורות לא-רלוונטיות בלבד): restart יזום של AIR-AM
        app._rf_session_reset("restart")
        seen["rf"] = app._rf_metrics(True, dict(app.DEFAULT_STATE))
    app._rf_follow_once(popen=lambda cmd, **kw: FakeProc(["-- No entries --\n"], on_line=during))
    assert seen["rf"]["overload"] is None and seen["rf"]["unknown_reason"] == "no_driver_evidence"
    # ואותו מצב, ישירות — גם ה-sidecar של שידור בחלון לא אומר "ללא עומס"
    _following(t=10.0)
    app._rf_session_reset("restart", now=20.0)
    rf = app._rf_metrics(True, dict(app.DEFAULT_STATE))
    assert rf["overload"] is None and rf["unknown_reason"] == "no_driver_evidence"
    s = app._rf_window_summary(30.0, 40.0)
    assert s is not None and s["overload"] is None and s["overload_at_start"] is None


def test_restart_and_verify_resets_session(monkeypatch):
    """נקודת-החנק: כל restart יזום של rtl_airband מאפס את מוני הסשן."""
    _following()
    app._rf_handle_line("AIRAM_RF overload=1")
    monkeypatch.setattr(app.time, "sleep", lambda *a: None)
    monkeypatch.setattr(app.subprocess, "run", lambda cmd, **k: type(
        "R", (), {"returncode": 0, "stdout": "active\n", "stderr": ""})())
    assert app._restart_and_verify() == (None, None, False)
    assert app._rf["overload_events"] == 0 and app._rf["overload"] is None   # עד stream=start
    _stream_start()
    assert app._rf["overload"] is False


# --- העוקב (Popen מזויף) ---------------------------------------------------------

class FakeProc:
    def __init__(self, lines, on_line=None):
        self._lines = lines
        self.on_line = on_line
        self.killed = False
        self.stdout = self._iter()

    def _iter(self):
        for ln in self._lines:
            if self.on_line:
                self.on_line(ln)
            yield ln

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0


def test_follow_once_parses_and_marks_unknown_on_exit():
    seen = {}

    def during(_ln):
        seen.setdefault("since", app._rf["follower_since"])
        seen.setdefault("attached", app._rf_follow_attached.is_set())

    calls = []

    def fake_popen(cmd, **kw):
        calls.append((cmd, kw))
        return FakeProc(["[INFO] AIRAM_RF gain grdb=41 lna_grdb=24\n",
                         "[INFO] AIRAM_RF overload=1\n"], on_line=during)

    app._rf_follow_attached.clear()
    app._rf_follow_once(popen=fake_popen)
    cmd, kw = calls[0]
    assert cmd == app.RF_JOURNAL_CMD
    assert cmd[:2] == ["journalctl", "-f"] and "-n" in cmd and cmd[cmd.index("-n") + 1] == "0"
    assert kw["stdout"] is app.subprocess.PIPE and kw["stderr"] is app.subprocess.DEVNULL
    assert seen["since"] is not None                     # קורא => follower_since מוגדר
    assert seen["attached"] is True                      # מחובר => _boot_restore רשאי להרים צרכן
    assert not app._rf_follow_attached.is_set()          # יצא => השער נסגר שוב
    assert app._rf["overload_events"] == 1               # שורות פוענחו
    # journalctl יצא => לא יודעים יותר (לא False)
    assert app._rf["follower_since"] is None and app._rf["overload"] is None


def test_follow_once_survives_bad_line(monkeypatch):
    def boom(line, now=None):
        raise RuntimeError("bad")
    monkeypatch.setattr(app, "_rf_handle_line", boom)
    app._rf_follow_once(popen=lambda cmd, **kw: FakeProc(["x\n", "y\n"]))
    assert app._rf["follower_since"] is None


def test_follower_loop_waits_for_mark_and_never_spawns_without_it(paths, monkeypatch):
    spawned = []
    monkeypatch.setattr(app, "_rf_follow_once", lambda popen=None: spawned.append(1) or 0.0)

    class OneShot:
        def __init__(self):
            self.n = 0

        def is_set(self):
            return self.n > 0

        def wait(self, t):
            self.n += 1
            return True

    app._rf_follower_loop(OneShot())
    assert spawned == []                                  # אין סימן-בנייה => אין journalctl


def test_follower_loop_restarts_with_backoff_and_never_dies(mark, monkeypatch):
    runs, waits = [], []

    def fake_once(popen=None):
        runs.append(1)
        if len(runs) == 1:
            raise FileNotFoundError("journalctl")
        return 0.5

    monkeypatch.setattr(app, "_rf_follow_once", fake_once)

    class Stop:
        def is_set(self):
            return len(runs) >= 4

        def wait(self, t):
            waits.append(t)
            return False

    app._rf_follower_loop(Stop())
    assert len(runs) == 4                                 # המשיך גם אחרי חריגה
    assert waits[:3] == [app.RF_JOURNAL_BACKOFF_MIN, app.RF_JOURNAL_BACKOFF_MIN * 2,
                         app.RF_JOURNAL_BACKOFF_MIN * 4]


# --- /api/metrics ---------------------------------------------------------------

def _stats(paths, freq, sig, noise):
    lbl = f"{freq:.3f}"
    (paths / "stats.txt").write_text(f'channel_dbfs_signal_level{{freq="{lbl}"}} {sig}\n'
                                     f'channel_dbfs_noise_level{{freq="{lbl}"}} {noise}\n')


def test_metrics_without_telemetry_is_unknown_not_ok(client, paths, monkeypatch):
    """אין סימן-בנייה => overload=None (לא False), וסף ה-‎-3dBFS הישן לא קיים —
    גם אות ערוץ ב-0dBFS לא "ממציא" עומס."""
    monkeypatch.setattr(app, "_is_active", lambda svc: True)
    app.save_state({**app.DEFAULT_STATE, "freq": 121.5})
    _stats(paths, 121.5, 0.0, -60.0)
    d = client.get("/api/metrics").get_json()
    assert "overload_dbfs" not in d
    assert d["overload"] is None
    assert d["rf"]["telemetry"] is False and d["rf"]["overload"] is None
    assert d["rf"]["ifgr"] is None and d["rf"]["lna_grdb"] is None
    assert d["signal"] == 0.0                              # המדדים הרגילים עדיין שם
    assert not hasattr(app, "OVERLOAD_DBFS")


def test_metrics_with_telemetry_reports_hardware_overload(client, mark, monkeypatch):
    monkeypatch.setattr(app, "_is_active", lambda svc: True)
    app.save_state({**app.DEFAULT_STATE, "freq": 121.5, "rf_gain": 6, "fm_notch": True})
    _following()
    app._rf_session_reset("restart")
    d = client.get("/api/metrics").get_json()
    assert d["rf"]["overload"] is None and d["rf"]["unknown_reason"] == "no_driver_evidence"
    _stream_start()
    d = client.get("/api/metrics").get_json()
    assert d["rf"]["overload"] is False and d["overload"] is False
    assert d["rf"]["unknown_reason"] is None
    assert d["rf"]["last_overload_age"] is None
    app._rf_handle_line("[INFO] AIRAM_RF overload=1")
    app._rf_handle_line("[INFO] AIRAM_RF gain grdb=25 lna_grdb=37")
    d = client.get("/api/metrics").get_json()
    rf = d["rf"]
    assert rf["telemetry"] is True and rf["overload"] is True and d["overload"] is True
    assert rf["overload_events"] == 1 and rf["last_overload_age"] is not None
    assert rf["ifgr"] == 25 and rf["lna_grdb"] == 37
    assert rf["lna_state"] == 6 and rf["fm_notch"] is True and rf["agc"] is True
    assert set(rf) == {"telemetry", "overload", "overload_events", "last_overload_age",
                       "ifgr", "lna_grdb", "lna_state", "fm_notch", "agc", "unknown_reason"}


def test_metrics_voice_not_live_is_unknown(client, mark, monkeypatch):
    monkeypatch.setattr(app, "_is_active", lambda svc: False)
    _following()
    app._rf_session_reset("restart")
    app._rf_handle_line("AIRAM_RF overload=1")
    rf = client.get("/api/metrics").get_json()["rf"]
    assert rf["overload"] is None and rf["ifgr"] is None and rf["overload_events"] == 0
    assert rf["unknown_reason"] == "voice_not_live"


def test_metrics_follower_down_is_unknown(client, mark, monkeypatch):
    monkeypatch.setattr(app, "_is_active", lambda svc: True)
    with app._rf_lock:
        app._rf.update(overload=False)                     # ערך ישן — העוקב לא קורא
    rf = client.get("/api/metrics").get_json()["rf"]
    assert rf["overload"] is None and rf["unknown_reason"] == "follower_down"


def test_metrics_unknown_reason_no_telemetry_and_joined_mid_session(client, paths, monkeypatch):
    """בלי סימן-בנייה => no_telemetry. עם סימן: airam-web הופעל מחדש בזמן שהקול רץ
    (העוקב התחיל אחרי תחילת הסשן) => joined_mid_session — גם כשה-GainChange כבר
    זורם (הדרייבר *כן* מדווח; רק מצב העומס בתחילה לא נראה). קצה-מצב פותר."""
    monkeypatch.setattr(app, "_is_active", lambda svc: True)
    assert client.get("/api/metrics").get_json()["rf"]["unknown_reason"] == "no_telemetry"
    (paths / "soapysdrplay3.build-sig").write_text("x\n")
    app._rf_session_reset("restart", now=time.time() - 7200)   # סשן ישן, לפני העוקב
    _following()
    app._rf_handle_line("AIRAM_RF gain grdb=40 lna_grdb=24")
    rf = client.get("/api/metrics").get_json()["rf"]
    assert rf["overload"] is None and rf["ifgr"] == 40
    assert rf["unknown_reason"] == "joined_mid_session"
    app._rf_handle_line("AIRAM_RF overload=0")                 # קצה = ראיה למצב הנוכחי
    rf = client.get("/api/metrics").get_json()["rf"]
    assert rf["overload"] is False and rf["unknown_reason"] is None


def test_signal_endpoint_no_longer_has_overload(client, paths, monkeypatch):
    monkeypatch.setattr(app, "_live_mode", lambda: "voice")
    app.save_state({**app.DEFAULT_STATE, "freq": 121.5})
    _stats(paths, 121.5, -1.0, -60.0)
    d = client.get("/api/signal").get_json()
    assert "overload" not in d


# --- חלון שידור --------------------------------------------------------------------

def test_window_summary_requires_follower_before_start():
    assert app._rf_window_summary(100.0, 110.0) is None    # עוקב לא רץ
    _following(t=105.0)
    assert app._rf_window_summary(100.0, 110.0) is None    # הצטרף באמצע החלון


def test_window_summary_counts_and_state_at_start():
    _following(t=50.0)
    _stream_start(now=60.0)
    for t, ln in ((70.0, "AIRAM_RF gain grdb=40 lna_grdb=24"),
                  (80.0, "AIRAM_RF overload=1"),          # עומס *לפני* החלון, לא תוקן
                  (101.0, "AIRAM_RF gain grdb=33 lna_grdb=24"),
                  (103.0, "AIRAM_RF overload=0"),
                  (104.0, "AIRAM_RF overload=1"),
                  (106.0, "AIRAM_RF gain grdb=45 lna_grdb=24"),
                  (120.0, "AIRAM_RF overload=1")):         # אחרי החלון
        app._rf_handle_line(ln, now=t)
    s = app._rf_window_summary(100.0, 110.0)
    assert s["overload_at_start"] is True and s["overload_events"] == 1 and s["overload"] is True
    assert s["ifgr_at_start"] == 40 and s["ifgr_min"] == 33 and s["ifgr_max"] == 45
    assert s["gain_events"] == 2


def test_window_summary_clean_window_is_false_not_none():
    _following(t=50.0)
    _stream_start(now=60.0)
    s = app._rf_window_summary(100.0, 110.0)
    assert s["overload"] is False and s["overload_events"] == 0
    assert s["ifgr_min"] is None                           # אין אירוע GainChange — לא ממציאים


def test_window_summary_reset_without_stream_start_is_unknown():
    """עוגן "reset" (AIR-AM הפעיל מחדש) בלי stream=start אחריו => מצב תחילת החלון
    לא ידוע, ולכן חלון בלי קצוות הוא None ולא "ללא עומס"."""
    _following(t=50.0)
    app._rf_session_reset("restart", now=60.0)
    s = app._rf_window_summary(100.0, 110.0)
    assert s["overload_at_start"] is None and s["overload"] is None


def test_window_summary_corrected_edge_inside_window_proves_overload():
    """הצטרפות באמצע סשן (מצב התחלה לא ידוע) ואז Overload_Corrected בתוך החלון =>
    העומס היה פעיל בין start לקצה — True, לא "עומס: ?"."""
    _following(t=100.0)
    app._rf_handle_line("AIRAM_RF overload=0", now=205.0)
    s = app._rf_window_summary(200.0, 210.0)
    assert s["overload_at_start"] is None and s["overload_events"] == 0
    assert s["overload"] is True


def test_window_summary_session_boundary_inside_window_is_not_clean():
    """stream=start בתוך החלון (גבול-סשן באמצע שידור) — "לא ראינו קצה" כבר לא
    מוכיח "אין עומס" לכל החלון."""
    _following(t=50.0)
    _stream_start(now=60.0)
    _stream_start(now=105.0)
    assert app._rf_window_summary(100.0, 110.0)["overload"] is None


def test_window_summary_evicted_events_is_unknown(monkeypatch):
    monkeypatch.setattr(app, "_rf_events", collections.deque(maxlen=4))
    _following(t=10.0)
    for i in range(10):
        app._rf_handle_line("AIRAM_RF gain grdb=40 lna_grdb=24", now=200.0 + i)
    assert app._rf_window_summary(100.0, 300.0) is None    # תחילת החלון נדחקה


# --- sidecar ‏.rf.json ------------------------------------------------------------

def _start_ts(name=NAME):
    return app._rec_start_ts(name)


def _mk_rec(paths, name=NAME, dur=4.0, saved=False):
    d = app._saved_dir() if saved else app.REC_DIR
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_bytes(b"\0" * 4096)
    end = _start_ts(name) + dur
    os.utime(p, (end, end))
    return p


def _conf(paths, freq=134.6, agc=True, rf_gain=5, fm_notch=True, mtime=None):
    c = paths / "airband.conf"
    c.write_text(app.render_config(freq, "am", agc, 33, rf_gain, "auto", fm_notch=fm_notch))
    if mtime is not None:
        os.utime(c, (mtime, mtime))


def test_rec_start_ts_from_rtl_airband_filename():
    t = app._rec_start_ts("airam_20260611_120001_134600000.mp3")
    assert time.localtime(t)[:6] == (2026, 6, 11, 12, 0, 1)
    assert app._rec_start_ts("bogus.mp3") is None


@pytest.fixture
def jerusalem_tz():
    old = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Jerusalem"
    time.tzset()
    yield
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


def test_rec_start_ts_dst_repeated_hour_disambiguated_by_mtime(jerusalem_tz):
    """25.10.2026 01:30 קורה פעמיים בישראל (סוף שעון קיץ). mktime לבד בוחר את
    הראשונה; הקלטה שבאמת התחילה בשנייה הייתה מקבלת חלון של שעה וחצי של טלמטריה
    זרה. ה-mtime (סוף השידור) מכריע; בלעדיו — None, לא ניחוש."""
    name = "airam_20261025_013000_132500000.mp3"
    first, second = 1792881000.0, 1792884600.0              # 01:30 IDT / 01:30 IST
    assert time.localtime(first)[:5] == time.localtime(second)[:5] == (2026, 10, 25, 1, 30)
    assert app._rec_start_ts(name) is None                   # אין end => אין הכרעה
    assert app._rec_start_ts(name, end=second + 5) == second
    assert app._rec_start_ts(name, end=first + 5) == first
    # שעה רגילה באותו יום — מועמד יחיד, בלי תלות ב-end
    assert app._rec_start_ts("airam_20261025_120000_132500000.mp3") is not None


def test_sidecar_dst_ambiguous_without_fit_has_no_window(jerusalem_tz, mark):
    """שני המועמדים לא מתיישבים עם ה-mtime => start=None => בלי חלון טלמטריה."""
    name = "airam_20261025_013000_132500000.mp3"
    p = app.REC_DIR / name
    p.write_bytes(b"\0" * 10)
    os.utime(p, (1792881000.0 - 10, 1792881000.0 - 10))     # לפני שני המועמדים
    rec = app._build_rf_sidecar(p, now=1792890000.0)
    assert rec["start"] is None and rec["telemetry_covered"] is False and rec["overload"] is None


def test_sidecar_full_with_config_stats_and_telemetry(mark):
    paths = mark
    mp3 = _mk_rec(paths)
    start = _start_ts()
    end = start + 4.0
    _conf(paths, mtime=start - 30)
    _following(t=start - 100)
    _stream_start(now=start - 50)
    app._rf_handle_line("AIRAM_RF gain grdb=41 lna_grdb=24", now=start - 10)
    app._rf_handle_line("AIRAM_RF overload=1", now=start + 1)
    app._rf_handle_line("AIRAM_RF gain grdb=30 lna_grdb=24", now=start + 2)
    _stats(paths, 134.6, -40.0, -75.0)
    now = end + 3.0
    os.utime(paths / "stats.txt", (now - 0.5, now - 0.5))
    rec = app._write_rf_sidecar(mp3, now=now)
    on_disk = json.loads(app._rf_path(mp3).read_text())
    assert on_disk == rec
    assert rec["freq"] == 134.6 and rec["start"] == start and rec["end"] == pytest.approx(end)
    assert rec["config_known"] is True and rec["agc"] is True and rec["if_gain"] is None
    assert rec["lna_state"] == 5 and rec["fm_notch"] is True and rec["mod"] == "am"
    ps = rec["post_stats"]
    assert ps["signal"] == -40.0 and ps["noise"] == -75.0 and ps["snr"] == 35.0
    assert ps["t"] is not None
    # לא ברמה העליונה — שם הם היו נקראים כ-SNR של השידור עצמו (ייצוא ZIP)
    assert not {"signal", "noise", "snr", "stats_t"} & set(rec)
    assert rec["start_precision_s"] == 1
    assert rec["telemetry"] is True and rec["telemetry_covered"] is True
    assert rec["overload"] is True and rec["overload_events"] == 1
    assert rec["overload_at_start"] is False
    assert rec["ifgr_min"] == 30 and rec["ifgr_max"] == 41


def test_sidecar_manual_gain_reports_configured_if_gain(paths):
    mp3 = _mk_rec(paths)
    _conf(paths, agc=False, rf_gain=2, fm_notch=False, mtime=_start_ts() - 30)
    rec = app._build_rf_sidecar(mp3, now=_start_ts() + 5)
    assert rec["agc"] is False and rec["if_gain"] == 33 and rec["lna_state"] == 2


def test_sidecar_unknowns_stay_none(paths):
    """בלי סימן-בנייה, קונפיג שנכתב *אחרי* השידור (כוונן מאז), ו-stats ישנים —
    כל שדה לא ידוע נשאר None, לעולם לא 0/False."""
    mp3 = _mk_rec(paths)
    _conf(paths, mtime=_start_ts() + 60)                   # כוונן מחדש אחרי השידור
    _stats(paths, 134.6, -40.0, -75.0)
    os.utime(paths / "stats.txt", (1.0, 1.0))
    rec = app._build_rf_sidecar(mp3, now=_start_ts() + 5)
    assert rec["config_known"] is False and rec["lna_state"] is None and rec["fm_notch"] is None
    assert rec["post_stats"] is None
    assert rec["telemetry"] is False and rec["telemetry_covered"] is False
    assert rec["overload"] is None and rec["overload_events"] is None


def test_sidecar_other_freq_config_is_unknown(paths):
    mp3 = _mk_rec(paths)
    _conf(paths, freq=118.3, mtime=_start_ts() - 30)
    assert app._build_rf_sidecar(mp3, now=_start_ts() + 5)["config_known"] is False


def test_sidecar_catchup_after_restart_takes_no_stats_snapshot(paths):
    """השלמה של הקלטה ישנה (airam-web היה כבוי): ה-stats של עכשיו אינם "סוף השידור"."""
    mp3 = _mk_rec(paths)
    _stats(paths, 134.6, -40.0, -75.0)
    rec = app._build_rf_sidecar(mp3, now=_start_ts() + 4 + app.WATCH_INTERVAL + app.STATS_MAX_AGE + 60)
    assert rec["post_stats"] is None


def test_sidecar_write_is_idempotent(paths):
    mp3 = _mk_rec(paths)
    assert app._write_rf_sidecar(mp3) is not None
    app._rf_path(mp3).write_text('{"keep": 1}')
    assert app._write_rf_sidecar(mp3) is None
    assert json.loads(app._rf_path(mp3).read_text()) == {"keep": 1}


class _StopWatcher(BaseException):
    """BaseException — עוקף את ה-except Exception שבתוך הלולאה ועוצר אותה."""


def test_watcher_writes_sidecar_before_activity_row(client, paths, monkeypatch):
    """מריץ סבב אחד של _activity_watcher *עצמו* (לא את שתי הקריאות ביד) — היפוך
    הסדר בלולאה (שורת יומן לפני ה-sidecar) חייב להפיל את הבדיקה."""
    mp3 = _mk_rec(paths)
    order = []
    real_write, real_append = app._write_rf_sidecars, app._append_activity

    def spy_write(rows):
        order.append(("sidecar", app._rf_path(mp3).is_file()))
        real_write(rows)

    def spy_append(rows):
        order.append(("activity", app._rf_path(mp3).is_file()))
        real_append(rows)

    def stop(_s):
        raise _StopWatcher()
    monkeypatch.setattr(app, "_last_logged_ts", lambda: 0.0)
    monkeypatch.setattr(app, "_write_rf_sidecars", spy_write)
    monkeypatch.setattr(app, "_append_activity", spy_append)
    monkeypatch.setattr(app.time, "sleep", stop)
    with pytest.raises(_StopWatcher):
        app._activity_watcher()
    assert [o[0] for o in order] == ["sidecar", "activity"]
    assert order[1][1] is True                                # ה-sidecar כבר על הדיסק בכתיבת השורה
    ev = client.get("/api/activity").get_json()["events"][0]
    assert ev["file"] == NAME and isinstance(ev["rf"], dict) and ev["rf"]["freq"] == 134.6


def test_write_sidecars_survives_failure(paths, monkeypatch):
    _mk_rec(paths)
    monkeypatch.setattr(app, "_build_rf_sidecar", lambda *a, **k: 1 / 0)
    app._write_rf_sidecars([{"file": NAME}, {"file": "missing.mp3"}])   # לא זורק


def test_activity_rf_null_without_sidecar(client, paths):
    _mk_rec(paths)
    rows, _ = app._scan_new_recordings(0.0)
    app._append_activity(rows)
    assert client.get("/api/activity").get_json()["events"][0]["rf"] is None


def test_rf_sidecar_travels_with_star_and_unstar(client, paths):
    mp3 = _mk_rec(paths)
    app._write_rf_sidecar(mp3)
    assert client.post("/api/recordings/star", json={"file": NAME}).get_json()["ok"]
    assert (app._saved_dir() / (NAME + ".rf.json")).is_file()
    assert not (app.REC_DIR / (NAME + ".rf.json")).exists()
    ev = client.get("/api/activity?starred=1").get_json()["events"][0]
    assert ev["rf"]["freq"] == 134.6                       # נקרא מהמיקום החדש
    client.post("/api/recordings/star", json={"file": NAME, "starred": False})
    assert (app.REC_DIR / (NAME + ".rf.json")).is_file()


def test_sweep_deletes_rf_sidecar_with_recording_and_orphans(paths, monkeypatch):
    monkeypatch.setattr(app, "REC_MAX_FILES", 1)
    old = _mk_rec(paths)
    app._write_rf_sidecar(old)
    newer = "airam_20260611_130000_134600000.mp3"
    _mk_rec(paths, newer)
    orphan = app.REC_DIR / "airam_20260101_000000_1.mp3.rf.json"
    orphan.write_text("{}")
    app._sweep_recordings()
    assert not old.exists() and not app._rf_path(old).exists()
    assert not orphan.exists()
    assert (app.REC_DIR / newer).exists()


def test_orphan_sweep_cannot_run_between_sidecar_and_mp3_moves(client, paths, monkeypatch):
    """★ מעביר קודם את קובצי-הצד ואז את ה-mp3. sweep (thread ה-watcher) שרץ בדיוק
    באמצע היה רואה ב-saved/ ‏.rf.json בלי mp3 ומוחק אותו לצמיתות. עם _STAR_LOCK
    הוא נחסם עד שההעברה הושלמה."""
    import threading
    mp3 = _mk_rec(paths)
    app._write_rf_sidecar(mp3)
    real_replace = app.os.replace
    state = {}

    def replace(src, dst):
        real_replace(src, dst)
        if str(src).endswith(".rf.json") and "t" not in state:
            t = threading.Thread(target=app._sweep_recordings)
            state["t"] = t
            t.start()
            t.join(0.3)
            state["blocked"] = t.is_alive()   # חייב להמתין לנעילה
    monkeypatch.setattr(app.os, "replace", replace)
    assert client.post("/api/recordings/star", json={"file": NAME}).get_json()["ok"]
    state["t"].join(5)
    assert state["blocked"] is True
    assert (app._saved_dir() / NAME).is_file()
    assert (app._saved_dir() / (NAME + ".rf.json")).is_file()


def test_starred_zip_contains_rf_sidecar(client, paths):
    mp3 = _mk_rec(paths, saved=True)
    app._write_rf_sidecar(mp3)
    r = client.get("/api/recordings/starred.zip")
    names = zipfile.ZipFile(io.BytesIO(r.data)).namelist()
    assert NAME in names and NAME + ".rf.json" in names


def _session_rows(monkeypatch):
    monkeypatch.setattr(adsb, "read_track_slice", lambda a, b: [{"t": b - 1, "ac": [["4X-EHD"]]}])


def test_session_save_moves_and_copies_rf_sidecar(client, paths, monkeypatch):
    _session_rows(monkeypatch)
    live = _mk_rec(paths)
    saved_name = "airam_20260611_120500_134600000.mp3"
    saved = _mk_rec(paths, saved_name, saved=True)
    now = time.time()
    for p in (live, saved):
        os.utime(p, (now - 30, now - 30))
        app._write_rf_sidecar(p)
    r = client.post("/api/sessions", json={"minutes": 5}).get_json()
    assert r["ok"]
    clips = paths / "sessions" / r["id"] / "clips"
    assert (clips / (NAME + ".rf.json")).is_file()           # לא-שמורה: הועברה
    assert not app._rf_path(live).exists()
    assert (clips / (saved_name + ".rf.json")).is_file()     # שמורה: הועתקה
    assert app._rf_path(saved).is_file()
    detail = client.get(f"/api/sessions/{r['id']}").get_json()["session"]
    assert all(isinstance(c["rf"], dict) for c in detail["clips"])
    z = client.get(f"/api/sessions/{r['id']}/export.zip")
    names = zipfile.ZipFile(io.BytesIO(z.data)).namelist()
    assert f"clips/{NAME}.rf.json" in names and f"clips/{saved_name}.rf.json" in names
