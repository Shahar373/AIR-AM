# ============================================================================
#  AIR-AM - בדיקות יחידה לשרת בורר התדרים
# ----------------------------------------------------------------------------
#  רץ בלי חומרה: כל נתיבי הקבצים מנותבים ל-tmp, ו-restart של systemd ממוקף.
#  הרצה: pytest tests/
# ============================================================================
import json
import os
import threading
import time

import pytest

import app


@pytest.fixture
def paths(tmp_path, monkeypatch):
    """מנתב את כל קבצי המערכת ל-tmp כדי שהבדיקות לא יגעו ב-/etc ו-/var."""
    monkeypatch.setattr(app, "CONFIG_PATH", tmp_path / "airband.conf")
    monkeypatch.setattr(app, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(app, "STATS_PATH", tmp_path / "stats.txt")
    monkeypatch.setattr(app, "PRESETS_PATH", tmp_path / "presets.json")
    monkeypatch.setattr(app, "ACTIVITY_PATH", tmp_path / "activity.jsonl")
    rec = tmp_path / "recordings"
    rec.mkdir()
    monkeypatch.setattr(app, "REC_DIR", rec)
    return tmp_path


@pytest.fixture
def client(paths):
    return app.app.test_client()


@pytest.fixture(autouse=True)
def _clean_power_cache():
    """‏/api/power מוגש מ-cache גלובלי בן 2 שניות (מכווץ 3 תהליכי vcgencmd × כל
    טאב). בלי איפוס, בדיקה שמחליפה את _vcgencmd הייתה מקבלת את התשובה של
    הבדיקה הקודמת — לכן מאפסים לפני *ואחרי* כל בדיקה בקובץ."""
    app._reset_power_cache()
    yield
    app._reset_power_cache()


@pytest.fixture(autouse=True)
def _clean_state_corrupt_flag():
    """‏load_state מזהיר על state.json פגום פעם אחת לאירוע (flag גלובלי) —
    בלי איפוס, בדיקה שמשאירה state פגום הייתה מדכאת בשקט את אזהרת הבדיקה הבאה."""
    app._reset_state_corrupt_warned()
    yield
    app._reset_state_corrupt_warned()


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(app.time, "sleep", lambda *a, **k: None)


# --- render_config ----------------------------------------------------------

def test_render_config_basic_structure():
    cfg = app.render_config(134.6, "am", True, 40, 4)
    assert 'freq = 134.6000;' in cfg
    assert f'centerfreq = {134.6 + app.DC_OFFSET:.4f};' in cfg   # הסטת DC-spike
    assert 'modulation = "am";' in cfg
    assert 'localtime = true;' in cfg
    assert 'stats_filepath' in cfg
    assert 'mountpoint = "live.mp3";' in cfg
    # AGC פעיל => אין שורת gain (השמטה = AGC חומרתי של SDRplay)
    assert "gain =" not in cfg
    # סוגריים מאוזנים (תחביר libconfig)
    assert cfg.count("(") == cfg.count(")") and cfg.count("{") == cfg.count("}")


def test_render_config_manual_gain():
    cfg = app.render_config(120.5, "nfm", False, 38, 6)
    assert 'gain = "IFGR=38,RFGR=6";' in cfg
    assert 'modulation = "nfm";' in cfg


def test_render_config_squelch_modes():
    assert "squelch_snr_threshold" not in app.render_config(120.5, "am", True, 40, 4, "auto")
    assert "squelch_snr_threshold = 0;" in app.render_config(120.5, "am", True, 40, 4, "open")
    assert "squelch_snr_threshold = 12.0;" in app.render_config(120.5, "am", True, 40, 4, "manual", 12)


def test_render_config_recording_only_when_squelch_closes():
    # סקוולץ' שנסגר => מקליטים כל שידור
    rec = app.render_config(120.5, "am", True, 40, 4, "auto")
    assert 'type = "file";' in rec
    assert "split_on_transmission = true;" in rec
    assert "include_freq = true;" in rec
    # "פתוח" (ATIS) => הסקוולץ' לעולם לא נסגר => קובץ אינסופי => אין הקלטה
    open_cfg = app.render_config(132.5, "am", True, 40, 4, "open")
    assert 'type = "file";' not in open_cfg
    assert open_cfg.count("(") == open_cfg.count(")")


# --- state ------------------------------------------------------------------

def test_load_state_defaults_and_merge(paths):
    assert app.load_state() == app.DEFAULT_STATE          # אין קובץ => ברירת מחדל
    app.STATE_PATH.write_text(json.dumps({"freq": 121.5}))
    st = app.load_state()
    assert st["freq"] == 121.5
    assert st["mod"] == app.DEFAULT_STATE["mod"]          # מפתחות חסרים מושלמים
    app.STATE_PATH.write_text("{corrupt")
    assert app.load_state() == app.DEFAULT_STATE          # קובץ פגום => ברירת מחדל


def test_load_state_missing_file_no_warning_logged(paths, caplog):
    """קובץ חסר = התקנה טרייה (לא אירוע) — בניגוד ל-JSON פגום, אסור שיילוג
    אזהרה או ייכתב עותק .corrupt."""
    assert not app.STATE_PATH.exists()
    with caplog.at_level("WARNING", logger="airam"):
        assert app.load_state() == app.DEFAULT_STATE
    assert caplog.records == []
    assert not app.STATE_PATH.with_suffix(".json.corrupt").exists()


def test_load_state_corrupt_warns_and_saves_copy_once_per_incident(paths, caplog):
    """load_state נקרא מראוטים רבים כולל כאלה שב-polling תכוף (/api/metrics) —
    בלי flag חד-פעמי, state.json פגום יחיד היה מייצר לוג-ספאם וכתיבת .corrupt
    חוזרת *בכל קריאה*, לא רק פעם אחת לאירוע (כפי שה-docstring של load_state
    מבטיחה)."""
    app.STATE_PATH.write_text("{corrupt")
    corrupt_copy = app.STATE_PATH.with_suffix(".json.corrupt")
    with caplog.at_level("WARNING", logger="airam"):
        for _ in range(3):
            assert app.load_state() == app.DEFAULT_STATE
    warnings = [r for r in caplog.records if "state.json פגום" in r.message]
    assert len(warnings) == 1                      # לא 3 — פעם אחת לאירוע
    assert corrupt_copy.read_text() == "{corrupt"

    # התאוששות: רק *קריאה* תקינה מאפסת את ה-flag (לא עצם הכתיבה) => אירוע
    # פגימה *עתידי* עדיין יתועד
    app.save_state(dict(app.DEFAULT_STATE))
    assert app.load_state() == app.DEFAULT_STATE
    caplog.clear()
    app.STATE_PATH.write_text("{corrupt again")
    with caplog.at_level("WARNING", logger="airam"):
        app.load_state()
    warnings2 = [r for r in caplog.records if "state.json פגום" in r.message]
    assert len(warnings2) == 1


# --- פרסור stats (פורמט Prometheus של rtl_airband) ---------------------------

STATS = """# comment line
channel_dbfs_signal_level{freq="132.500"}\t-42.3
channel_dbfs_noise_level{device="0",freq="132.500"} -55.1
channel_squelch_counter{mode="am", freq="132.500"} 12
channel_squelch_counter{freq="118.050"} 7
"""


def test_parse_stats_label_order_independent():
    vals = app.parse_stats(STATS, "132.500")
    # ה-label freq מזוהה גם כשאינו ראשון ברשימה
    assert vals == {"channel_dbfs_signal_level": -42.3,
                    "channel_dbfs_noise_level": -55.1,
                    "channel_squelch_counter": 12.0}
    assert app.parse_stats(STATS, "118.050") == {"channel_squelch_counter": 7.0}
    assert app.parse_stats(STATS, "999.999") == {}


def test_api_metrics(client, paths):
    app.STATE_PATH.write_text(json.dumps({**app.DEFAULT_STATE, "freq": 132.5}))
    app.STATS_PATH.write_text(STATS)
    m = client.get("/api/metrics").get_json()
    assert m["fresh"] and m["snr"] == pytest.approx(12.8)
    # אין קובץ stats (אחרי אתחול) => לא טרי, בלי שגיאה
    app.STATS_PATH.unlink()
    assert client.get("/api/metrics").get_json()["fresh"] is False


# --- /api/tune ---------------------------------------------------------------

@pytest.fixture
def tuned_ok(monkeypatch):
    """ממקף את ה-restart => בדיקת הלוגיקה בלי systemd/SDR."""
    monkeypatch.setattr(app, "_restart_and_verify", lambda: (None, None, False))


def test_tune_validation(client):
    assert client.post("/api/tune", json={"freq": "abc"}).status_code == 400
    assert client.post("/api/tune", json={}).status_code == 400
    assert client.post("/api/tune", json={"freq": 5000}).status_code == 400


def test_tune_success_persists_state(client, paths, tuned_ok):
    r = client.post("/api/tune", json={"freq": 134.6, "mod": "am", "agc": False,
                                       "if_gain": 30, "rf_gain": 2,
                                       "squelch_mode": "manual", "squelch_snr": 11})
    assert r.status_code == 200 and r.get_json()["ok"]
    st = app.load_state()
    assert st["freq"] == 134.6 and st["if_gain"] == 30 and st["rf_gain"] == 2
    assert st["squelch_mode"] == "manual"
    cfg = app.CONFIG_PATH.read_text()
    assert "freq = 134.6000;" in cfg and 'gain = "IFGR=30,RFGR=2";' in cfg


def test_tune_sanitizes_inputs(client, paths, tuned_ok):
    r = client.post("/api/tune", json={"freq": 120.5, "mod": "weird", "agc": "false",
                                       "if_gain": 999, "rf_gain": -5,
                                       "squelch_mode": "nope", "squelch_snr": -5})
    body = r.get_json()
    assert body["mod"] == "am"                    # אפנון לא מוכר => AM
    assert body["agc"] is False                   # "false" טקסטואלי מזוהה
    assert body["if_gain"] == app.IFGR_MAX        # clamp לתקרה
    assert body["rf_gain"] == app.RFGR_MIN        # clamp לרצפה
    assert body["squelch_mode"] == "auto"         # מצב לא מוכר => auto
    assert body["squelch_snr"] == 0.0             # clamp לרצפה


def test_tune_failure_rolls_back(client, paths, monkeypatch):
    app.save_state({**app.DEFAULT_STATE, "freq": 132.5})
    monkeypatch.setattr(app, "_restart_and_verify", lambda: ("נכשל", "detail", False))
    rolled = []
    # _rollback מאומת: True = הקונפיג הקודם עלה בהצלחה => נשארים בקול
    monkeypatch.setattr(app, "_rollback", lambda prev: rolled.append(prev["freq"]) or True)
    r = client.post("/api/tune", json={"freq": 134.6})
    assert r.status_code == 500
    assert rolled == [132.5]
    assert r.get_json()["state"]["app_mode"] == "voice"   # רולבק בתוך קול, לא נפילה ל-off
    assert app.load_state()["freq"] == 132.5          # state נשאר על האחרון שעבד


def test_tune_rollback_failure_falls_to_off(client, paths, no_sleep, monkeypatch):
    # גם הרולבק לתדר הקודם נכשל => נופלים ל-off (לא משאירים שירות בלולאת קריסה)
    app.save_state({**app.DEFAULT_STATE, "freq": 132.5, "app_mode": "voice"})
    monkeypatch.setattr(app, "_restart_and_verify", lambda: ("נכשל", "detail", False))
    monkeypatch.setattr(app, "_rollback", lambda prev: False)
    monkeypatch.setattr(app, "_sysctl", lambda action, svc, timeout=45: None)
    monkeypatch.setattr(app, "_is_active", lambda svc: False)
    r = client.post("/api/tune", json={"freq": 134.6})
    assert r.status_code == 500
    body = r.get_json()
    assert body["app_mode"] == "off" and body["state"]["app_mode"] == "off"
    assert app.load_state()["app_mode"] == "off"


def test_api_mode_voice_failure_falls_to_off(client, paths, no_sleep, monkeypatch):
    # כניסה לקול ממצב דאטה שנכשלת (וגם הרולבק) => off, בדיוק כמו כל מצב אחר
    app.save_state({**app.DEFAULT_STATE, "app_mode": "acars"})
    monkeypatch.setattr(app, "_restart_and_verify", lambda: ("נכשל", None, False))
    monkeypatch.setattr(app, "_rollback", lambda prev: False)
    monkeypatch.setattr(app, "_sysctl", lambda action, svc, timeout=45: None)
    monkeypatch.setattr(app, "_is_active", lambda svc: False)
    r = client.post("/api/mode", json={"mode": "voice"})
    assert r.status_code == 500
    assert r.get_json()["state"]["app_mode"] == "off"
    st = app.load_state()
    assert st["app_mode"] == "off" and st["prev_mode"] == "acars"


def test_tune_sdr_down_keeps_new_config(client, paths, monkeypatch):
    # SDR מנותק: אין רולבק (ייתקע באותה המתנה) => ה-state עוקב אחרי הדיסק
    monkeypatch.setattr(app, "_restart_and_verify", lambda: ("נתקע", None, True))
    r = client.post("/api/tune", json={"freq": 134.6})
    assert r.status_code == 500
    assert app.load_state()["freq"] == 134.6


# --- presets ------------------------------------------------------------------

def test_presets_default_then_edit(client, paths):
    r = client.get("/api/presets").get_json()
    assert len(r["presets"]) == len(app.DEFAULT_PRESETS)
    new = [{"name": "מגדל חיפה", "freq": 122.7}, {"name": "ATIS", "freq": 132.5, "sq": "open"}]
    r = client.put("/api/presets", json=new)
    assert r.status_code == 200
    assert json.loads(app.PRESETS_PATH.read_text())[0]["name"] == "מגדל חיפה"
    # /api/state מגיש את הרשימה הערוכה
    assert len(client.get("/api/state").get_json()["presets"]) == 2


@pytest.mark.parametrize("bad", [
    {"not": "a list"},
    [{"name": "", "freq": 120}],            # שם ריק
    [{"name": "x", "freq": "abc"}],         # תדר לא מספרי
    [{"name": "x", "freq": 120, "sq": "weird"}],
    [{"name": "x" * 41, "freq": 120}],      # שם ארוך מדי
    [{"name": "x", "freq": 0.01}],          # מחוץ לטווח
])
def test_presets_rejects_invalid(client, paths, bad):
    assert client.put("/api/presets", json=bad).status_code == 400


def test_presets_corrupt_file_falls_back(client, paths):
    app.PRESETS_PATH.write_text("{broken")
    assert len(client.get("/api/presets").get_json()["presets"]) == len(app.DEFAULT_PRESETS)


def test_atomic_write_concurrent_threads_dont_corrupt_file(paths):
    """שני threads שכותבים לאותו path במקביל (כמו שתי בקשות PUT /api/presets)
    לא אמורים להתנגש על אותו קובץ tmp — tmp ייחודי לפר-thread. התוצאה הסופית
    היא תמיד אחד משני התכנים המלאים (last-write-wins), לא קובץ פגום/מעורבב."""
    target = paths / "concurrent.json"
    texts = ["A" * 50000, "B" * 50000]     # ארוך מספיק שה-write לא יהיה אטומי-כרונית ב-OS
    errors = []

    def writer(text):
        try:
            for _ in range(20):
                app._atomic_write(target, text)
        except Exception as e:                    # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(t,)) for t in texts]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=10)

    assert errors == []
    content = target.read_text()
    assert content in texts                        # לא קובץ פגום/מעורבב מהשתיים
    # לא נשארו קובצי tmp דלוקים
    assert list(paths.glob("concurrent.json.tmp*")) == []


def test_atomic_write_fsyncs_file_and_directory(paths, monkeypatch):
    """‏fsync (לא רק flush+rename) הוא מה שהופך את הכתיבה לעמידה בניתוק חשמל
    פתאומי (תרחיש אמיתי בהפעלה מסוללה) — לא רק אטומית מול קוראים מקבילים.
    בלעדיו הנתונים יכולים לשבת ב-page cache בלבד ולהיעלם בכיבוי בלתי-צפוי."""
    fsync_calls = []
    real_fsync = os.fsync

    def spy_fsync(fd):
        fsync_calls.append(fd)
        return real_fsync(fd)
    monkeypatch.setattr(app.os, "fsync", spy_fsync)

    target = paths / "durable.json"
    app._atomic_write(target, "content")

    assert target.read_text() == "content"
    assert len(fsync_calls) == 2      # ה-tmp file וגם ה-fd של התיקייה
    assert list(paths.glob("durable.json.tmp*")) == []


def test_cleanup_orphan_tmp_removes_only_old_matching_files(paths):
    """ניקוי tmp יתומים חייב להיות זהיר: ישן+תואם-תבנית נמחק, אבל קובץ טרי
    (כתיבה חיה אפשרית) וקובץ שלא תואם את התבנית המדויקת נשארים."""
    old = time.time() - app._TMP_ORPHAN_AGE_SEC - 10

    old_orphan = paths / "state.json.tmp111-1"
    old_orphan.write_text("x")
    os.utime(old_orphan, (old, old))

    fresh_orphan = paths / "state.json.tmp222-2"
    fresh_orphan.write_text("x")           # לא ישן => עלול להיות כתיבה חיה

    unrelated = paths / "state.json.bak"   # לא תואם את *.tmp*
    unrelated.write_text("x")
    os.utime(unrelated, (old, old))

    removed = app._cleanup_orphan_tmp(dirs=[paths])

    assert removed == 1
    assert not old_orphan.exists()
    assert fresh_orphan.exists()
    assert unrelated.exists()


def test_cleanup_orphan_tmp_default_dirs_cover_etc_airam_and_recordings(paths, monkeypatch):
    """‏_atomic_write נכתב גם על ACARS/VDL2/SATCOM env (תחת /etc/airam) ועל
    קבצי תמלול תחת REC_DIR (_transcript_path) — לא רק STATE_PATH/CONFIG_PATH.
    ברירת המחדל של dirs חייבת לכסות את כל היעדים האמיתיים, אחרת tmp יתום
    ב-/etc/airam או ב-REC_DIR לעולם לא ינוקה."""
    etc_airam = paths / "etc_airam"
    etc_airam.mkdir()
    monkeypatch.setattr(app, "ACARS_ENV_PATH", etc_airam / "acars.env")

    old = time.time() - app._TMP_ORPHAN_AGE_SEC - 10
    state_orphan = app.STATE_PATH.parent / "state.json.tmp10-1"
    env_orphan = etc_airam / "acars.env.tmp20-1"
    rec_orphan = app.REC_DIR / "airam_x.mp3.txt.tmp30-1"
    for p in (state_orphan, env_orphan, rec_orphan):
        p.write_text("x")
        os.utime(p, (old, old))

    removed = app._cleanup_orphan_tmp()      # ברירת המחדל, לא dirs מפורש

    assert removed == 3
    assert not state_orphan.exists()
    assert not env_orphan.exists()
    assert not rec_orphan.exists()


# --- יומן שידורים והקלטות -----------------------------------------------------

def _mk_rec(paths, name, size=6000, age=60):
    p = app.REC_DIR / name
    p.write_bytes(b"\0" * size)
    t = time.time() - age
    os.utime(p, (t, t))
    return p


def test_rec_freq_parsing():
    assert app._rec_freq_mhz("airam_20260611_120001_134600000.mp3") == 134.6
    assert app._rec_freq_mhz("not_a_recording.mp3") is None


def test_activity_log_and_api(client, paths):
    _mk_rec(paths, "airam_20260611_120001_134600000.mp3", size=42000, age=300)
    _mk_rec(paths, "airam_20260611_120203_121500000.mp3", age=120)
    rows = []
    for p in sorted(app.REC_DIR.glob("*.mp3"), key=lambda p: p.stat().st_mtime):
        st = p.stat()
        rows.append({"ts": st.st_mtime, "freq": app._rec_freq_mhz(p.name),
                     "file": p.name, "dur": round(st.st_size / app.REC_BYTES_PER_SEC, 1)})
    app._append_activity(rows)
    evs = client.get("/api/activity").get_json()["events"]
    assert [e["freq"] for e in evs] == [121.5, 134.6]      # חדש => ישן
    assert evs[1]["dur"] == 7.0 and evs[1]["exists"] is True
    # הקלטה שנמחקה ב-retention => האירוע נשאר עם exists=False
    (app.REC_DIR / evs[0]["file"]).unlink()
    evs = client.get("/api/activity").get_json()["events"]
    assert evs[0]["exists"] is False


def test_activity_trim(paths):
    app._append_activity([{"ts": i, "file": f"f{i}.mp3"} for i in range(app.ACTIVITY_KEEP * 2 + 5)])
    lines = app.ACTIVITY_PATH.read_text().splitlines()
    assert len(lines) == app.ACTIVITY_KEEP                  # קוצץ לגודל היעד
    assert json.loads(lines[-1])["ts"] == app.ACTIVITY_KEEP * 2 + 4   # החדשים נשמרו


def test_scan_no_duplicate_after_restart(paths):
    # mtime עם שבר עשיריות שמתעגל כלפי מטה - המקרה שגרם לכפילות:
    # ה-ts הנשמר ביומן מעוגל, אבל פעם השוואנו מולו את ה-mtime הלא-מעוגל.
    p = _mk_rec(paths, "airam_20260611_120001_134600000.mp3")
    os.utime(p, (1000.04, 1000.04))   # מתעגל ל-1000.0
    rows, newest = app._scan_new_recordings(0.0)
    assert len(rows) == 1 and rows[0]["ts"] == 1000.0
    app._append_activity(rows)
    # restart: ממשיכים מה-ts שנכתב ביומן => אותה הקלטה לא נסרקת שוב
    rows2, _ = app._scan_new_recordings(app._last_logged_ts())
    assert rows2 == []


def test_sweep_retention(paths):
    for i in range(app.REC_MAX_FILES + 10):
        _mk_rec(paths, f"airam_20260611_13{i:04d}_134600000.mp3", size=10, age=i)
    stale_tmp = _mk_rec(paths, "airam_x.mp3.tmp", age=7200)   # שידור שנקטע בקריסה
    fresh_tmp = _mk_rec(paths, "airam_y.mp3.tmp", age=10)     # שידור בכתיבה כרגע
    app._sweep_recordings()
    assert len(list(app.REC_DIR.glob("*.mp3"))) == app.REC_MAX_FILES
    assert not stale_tmp.exists() and fresh_tmp.exists()


# --- transcription (תמלול ATC) -----------------------------------------------

def test_activity_includes_transcript(client, paths):
    p = _mk_rec(paths, "airam_20260611_120001_134600000.mp3")
    app._transcript_path(p).write_text("cleared for takeoff runway 26\n")
    app._append_activity([{"ts": p.stat().st_mtime, "freq": 134.6,
                           "file": p.name, "dur": 1.0}])
    ev = client.get("/api/activity").get_json()["events"][0]
    assert ev["text"] == "cleared for takeoff runway 26"


def test_activity_text_none_without_sidecar(client, paths):
    p = _mk_rec(paths, "airam_20260611_120001_134600000.mp3")
    app._append_activity([{"ts": p.stat().st_mtime, "freq": 134.6, "file": p.name}])
    assert client.get("/api/activity").get_json()["events"][0]["text"] is None


def test_sweep_removes_transcript_sidecar(paths, monkeypatch):
    monkeypatch.setattr(app, "REC_MAX_FILES", 1)
    for i in range(3):
        p = _mk_rec(paths, f"airam_2026061{i}_120001_134600000.mp3", size=10, age=30 - i)
        app._transcript_path(p).write_text("t")
    app._sweep_recordings()
    assert len(list(app.REC_DIR.glob("*.mp3"))) == 1
    assert len(list(app.REC_DIR.glob("*.txt"))) == 1   # קובץ-הצד של הנמחק הוסר איתו


def test_transcribe_file_handles_failure(paths, monkeypatch):
    p = _mk_rec(paths, "airam_20260611_120001_134600000.mp3")
    def boom(*a, **k):
        raise FileNotFoundError("ffmpeg")
    monkeypatch.setattr(app.subprocess, "run", boom)
    assert app._transcribe_file(p) is None             # כשל => None, בלי לזרוק


# --- stream proxy (same-origin עבור HTTPS) -----------------------------------

def test_stream_proxy_passthrough(client, monkeypatch):
    class FakeUp:
        def __init__(self): self._chunks = [b"ID3 audio bytes", b""]
        def read(self, n): return self._chunks.pop(0)
        def close(self): pass
    monkeypatch.setattr(app.urllib.request, "urlopen", lambda *a, **k: FakeUp())
    r = client.get("/stream")
    assert r.status_code == 200 and r.mimetype == "audio/mpeg"
    assert r.get_data() == b"ID3 audio bytes"


def test_stream_proxy_upstream_down(client, monkeypatch):
    def boom(*a, **k):
        raise OSError("connection refused")
    monkeypatch.setattr(app.urllib.request, "urlopen", boom)
    assert client.get("/stream").status_code == 502


def test_recordings_served_and_traversal_blocked(client, paths):
    _mk_rec(paths, "airam_20260611_120001_134600000.mp3")
    (paths / "secret.txt").write_text("x")
    assert client.get("/recordings/airam_20260611_120001_134600000.mp3").status_code == 200
    assert client.get("/recordings/..%2Fsecret.txt").status_code == 404


# --- health -------------------------------------------------------------------

def test_api_health(client, paths, monkeypatch):
    def fake_run(cmd, **kw):
        class R:
            returncode = 0
            stdout = "active\n" if cmd[1] == "is-active" else ""
            stderr = ""
        return R()
    monkeypatch.setattr(app.subprocess, "run", fake_run)
    app.STATS_PATH.write_text("x")
    h = client.get("/api/health").get_json()
    assert h["ok"] is True
    assert h["services"]["rtl_airband"] == "active"
    assert h["stats_age"] is not None


# --- power (vcgencmd) ---------------------------------------------------------

def test_api_power_absent_when_not_pi(client, monkeypatch):
    monkeypatch.setattr(app, "_vcgencmd", lambda *a: None)   # אין vcgencmd => לא Pi
    assert client.get("/api/power").get_json() == {"ok": False}


def test_api_power_parses_flags_volts_temp(client, monkeypatch):
    def fake_vc(*args):
        if args[0] == "get_throttled":
            return "throttled=0x50005"          # ביטים 0,2,16,18 => now+ever
        if args[0] == "pmic_read_adc":
            return "EXT5V_V volt(24)=5.07V\nVDD_CORE_V volt(1)=0.8V"
        if args[0] == "measure_temp":
            return "temp=47.2'C"
        return None
    monkeypatch.setattr(app, "_vcgencmd", fake_vc)
    p = client.get("/api/power").get_json()
    assert p["ok"] is True
    assert p["undervolt_now"] and p["undervolt_ever"]
    assert p["throttle_now"] and p["throttle_ever"]
    assert p["volts_in"] == 5.07 and p["temp"] == 47.2


def test_api_power_ok_clean_flags(client, monkeypatch):
    monkeypatch.setattr(app, "_vcgencmd",
                        lambda *a: "throttled=0x0" if a[0] == "get_throttled" else None)
    p = client.get("/api/power").get_json()
    assert p["ok"] is True
    assert not any(p[k] for k in ("undervolt_now", "undervolt_ever",
                                  "throttle_now", "throttle_ever"))
    assert p["volts_in"] is None   # אין pmic (לא Pi 5)


def test_api_power_cache_avoids_repeat_vcgencmd_calls(client, monkeypatch):
    """‏pollPower רץ כל 5 שניות בכל טאב פתוח; בלי cache, N טאבים = 3N תהליכי
    vcgencmd כל 5 שניות. TTL של _POWER_TTL שניות אמור לכווץ שתי קריאות
    רצופות (בטווח ה-TTL) לדגימת vcgencmd אחת בפועל."""
    calls = []

    def fake_vc(*args):
        calls.append(args[0])
        return "throttled=0x0" if args[0] == "get_throttled" else None
    monkeypatch.setattr(app, "_vcgencmd", fake_vc)

    p1 = client.get("/api/power").get_json()
    p2 = client.get("/api/power").get_json()

    assert p1 == p2 and p1["ok"] is True
    assert calls == ["get_throttled", "pmic_read_adc", "measure_temp"]   # לא כפול


# --- PWA root assets ----------------------------------------------------------

def test_pwa_assets_served_from_root(client):
    r = client.get("/manifest.webmanifest")
    assert r.status_code == 200 and "manifest" in r.headers["Content-Type"]
    sw = client.get("/sw.js")
    assert sw.status_code == 200 and sw.headers.get("Service-Worker-Allowed") == "/"
    assert client.get("/icon-192.png").status_code == 200
    assert client.get("/nope-asset.foo").status_code == 404   # catch-all לא מגיש זבל
