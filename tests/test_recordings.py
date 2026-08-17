# ============================================================================
#  AIR-AM - בדיקות להקלטות: שמירה (★, תת-תיקייה saved/) ותמלול ATC
# ----------------------------------------------------------------------------
#  רץ בלי חומרה ובלי whisper: הבינארי/המודל ממוקפים, subprocess מוחלף.
#  שני הפיצ'רים קשורים (שמורה => מתומללת אוטומטית) ולכן נבדקים יחד.
#  הרצה: pytest tests/test_recordings.py
# ============================================================================
import json
import os
import threading
import time
import zipfile

import pytest

import app


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(app, "ACTIVITY_PATH", tmp_path / "activity.jsonl")
    rec = tmp_path / "recordings"
    rec.mkdir()
    monkeypatch.setattr(app, "REC_DIR", rec)
    return tmp_path


@pytest.fixture
def client(paths):
    return app.app.test_client()


@pytest.fixture(autouse=True)
def _clean_tx_state():
    """‏_TX_BUSY/_TX_FAILS הם מצב גלובלי (module-level) — בלי איפוס, בדיקה
    שמפעילה אותם הייתה מדליפה לבדיקה הבאה."""
    with app._TX_LOCK:
        app._TX_BUSY["file"] = None
    app._TX_FAILS.clear()
    yield
    with app._TX_LOCK:
        app._TX_BUSY["file"] = None
    app._TX_FAILS.clear()


def _mk_rec(paths, name, size=4096, age=0, saved=False):
    d = app._saved_dir() if saved else app.REC_DIR
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_bytes(b"\0" * size)
    if age:
        t = time.time() - age
        os.utime(p, (t, t))
    return p


NAME = "airam_20260611_120001_134600000.mp3"


# --- שמירה (★) ----------------------------------------------------------------

def test_star_and_unstar_roundtrip(client, paths):
    _mk_rec(paths, NAME)
    r = client.post("/api/recordings/star", json={"file": NAME}).get_json()
    assert r["ok"] and r["starred"] and r["starred_count"] == 1
    assert (app._saved_dir() / NAME).is_file() and not (app.REC_DIR / NAME).exists()
    r = client.post("/api/recordings/star",
                    json={"file": NAME, "starred": False}).get_json()
    assert r["ok"] and not r["starred"] and r["starred_count"] == 0
    assert (app.REC_DIR / NAME).is_file() and not (app._saved_dir() / NAME).exists()


def test_star_rejects_bad_filename(client, paths):
    """‏_REC_NAME_RE הוא גם הגנת ה-path traversal: שם שאינו airam_...mp3 נדחה."""
    for bad in ("../../etc/passwd", "airam_x.mp3", "", "airam_20260611_120001_1.txt"):
        assert client.post("/api/recordings/star", json={"file": bad}).status_code == 400


def test_star_missing_recording_is_404(client, paths):
    assert client.post("/api/recordings/star", json={"file": NAME}).status_code == 404


def test_starred_recording_survives_retention(client, paths, monkeypatch):
    """★ הלב של הפיצ'ר: הקלטה שמורה לא נמחקת — כי `_sweep_recordings` בכלל
    לא רואה את `saved/` (glob אינו רקורסיבי), לא כי מישהו סימן אותה בפטור."""
    monkeypatch.setattr(app, "REC_MAX_FILES", 2)
    old = _mk_rec(paths, NAME, age=9999)                      # הישנה ביותר
    for i in range(5):
        _mk_rec(paths, f"airam_2026061{i}_130000_134600000.mp3", age=100 - i)
    client.post("/api/recordings/star", json={"file": NAME})
    app._sweep_recordings()
    assert (app._saved_dir() / NAME).exists(), "הקלטה שמורה נמחקה ב-retention"
    assert old is not None
    # והשמורה גם לא "גנבה" מקום מהמכסה של הרגילות (glob לא רקורסיבי => לא נספרת)
    assert len(list(app.REC_DIR.glob("*.mp3"))) == app.REC_MAX_FILES


def test_star_quota_refuses_instead_of_deleting(client, paths, monkeypatch):
    """⚠ כשהמכסה מלאה **מסרבים לשמור**, ולא מוחקים שמורה ותיקה: מחיקת קובץ
    שהמשתמש הגן עליו במפורש היא בדיוק מה שהפיצ'ר נועד למנוע."""
    monkeypatch.setattr(app, "REC_STAR_MAX_FILES", 2)
    names = [f"airam_2026061{i}_130000_134600000.mp3" for i in range(3)]
    for n in names:
        _mk_rec(paths, n)
    assert client.post("/api/recordings/star", json={"file": names[0]}).status_code == 200
    assert client.post("/api/recordings/star", json={"file": names[1]}).status_code == 200
    r = client.post("/api/recordings/star", json={"file": names[2]})
    assert r.status_code == 409 and "מקום" in r.get_json()["error"]
    assert app._saved_usage()[0] == 2                      # אף אחת לא הוסרה
    assert (app._saved_dir() / names[0]).exists() and (app._saved_dir() / names[1]).exists()
    assert (app.REC_DIR / names[2]).exists()                # השלישית נשארה במקומה


def test_star_quota_counts_bytes(client, paths, monkeypatch):
    monkeypatch.setattr(app, "REC_STAR_MAX_BYTES", 5_000)
    a = "airam_20260611_130000_134600000.mp3"
    b = "airam_20260612_130000_134600000.mp3"
    _mk_rec(paths, a, size=4_000)
    _mk_rec(paths, b, size=4_000)
    assert client.post("/api/recordings/star", json={"file": a}).status_code == 200
    assert client.post("/api/recordings/star", json={"file": b}).status_code == 409


def test_restar_same_file_is_idempotent(client, paths, monkeypatch):
    """שמירה חוזרת של קובץ שכבר שמור לא נחשבת לתפיסת מכסה נוספת, ולא נכשלת."""
    monkeypatch.setattr(app, "REC_STAR_MAX_FILES", 1)
    _mk_rec(paths, NAME)
    assert client.post("/api/recordings/star", json={"file": NAME}).status_code == 200
    assert client.post("/api/recordings/star", json={"file": NAME}).status_code == 200
    assert app._saved_usage()[0] == 1


def test_activity_reports_starred_flag(client, paths):
    p = _mk_rec(paths, NAME)
    app._append_activity([{"ts": p.stat().st_mtime, "freq": 134.6, "file": NAME}])
    assert client.get("/api/activity").get_json()["events"][0]["starred"] is False
    client.post("/api/recordings/star", json={"file": NAME})
    ev = client.get("/api/activity").get_json()["events"][0]
    assert ev["starred"] is True and ev["exists"] is True   # עדיין "קיימת" — רק זזה


def test_starred_view_survives_activity_log_trim(client, paths):
    """⚠ ?starred=1 נסרק מ-`saved/` על הדיסק, לא מרשומה שיכולה להיפגם או
    להיקצץ עם היומן — היומן מקוצץ ל-ACTIVITY_KEEP, השמורה לא מושפעת בכלל."""
    p = _mk_rec(paths, NAME)
    client.post("/api/recordings/star", json={"file": NAME})
    app._append_activity([{"ts": 1000.0 + i, "file": f"airam_2026061{i % 9}_1300"
                           f"0{i % 9}_134600000.mp3"}
                          for i in range(app.ACTIVITY_KEEP * 2 + 10)])
    live = client.get("/api/activity").get_json()["events"]
    assert NAME not in [e.get("file") for e in live]        # קוצץ מהיומן
    saved = client.get("/api/activity?starred=1").get_json()
    assert saved["starred_only"] and len(saved["events"]) == 1
    ev = saved["events"][0]
    assert ev["file"] == NAME and ev["exists"] and ev["starred"]
    assert ev["freq"] == 134.6                              # נגזר מהקובץ, לא מרשומה


def test_star_events_share_event_builder(paths):
    """‏_rec_event הוא מקור-האמת היחיד לשורה — משמש גם את היומן החי (דרך
    _scan_new_recordings) וגם את ?starred=1, כדי שלא יהיו שתי גרסאות."""
    p = _mk_rec(paths, NAME)
    ev = app._rec_event(p)
    assert ev == {"ts": round(p.stat().st_mtime, 1), "freq": 134.6,
                  "file": NAME, "dur": round(4096 / app.REC_BYTES_PER_SEC, 1)}


# --- מרוצים (הכשלים שהוכחו בביקורת) ------------------------------------------

def test_star_concurrent_requests_do_not_overshoot_quota(client, paths, monkeypatch):
    """⚠ המכסה חייבת להיאכף גם תחת עומס-מקביל: 20 בקשות על מכסה-5 לא יכולות
    לקבל 20 אישורים כשרק 5 בפועל נשמרים (עקיפת מכסה עם ok:true שקרי, שהוכחה
    בגרסה הקודמת שהשתמשה ב-starred.json בלי נעילה)."""
    monkeypatch.setattr(app, "REC_STAR_MAX_FILES", 5)
    names = [f"airam_2026061{i}_130000_134600000.mp3" for i in range(20)]
    for n in names:
        _mk_rec(paths, n)
    results = []
    lock = threading.Lock()
    def worker(n):
        r = client.post("/api/recordings/star", json={"file": n}).get_json()
        with lock:
            results.append(r["ok"])
    threads = [threading.Thread(target=worker, args=(n,)) for n in names]
    for t in threads: t.start()
    for t in threads: t.join()
    assert sum(results) == 5                    # לא יותר מהמכסה מקבלות ok:true
    assert app._saved_usage()[0] == 5            # ותואם למה שבאמת נשמר בדיסק


def test_sweep_never_touches_saved_dir(paths, monkeypatch):
    """הוכחה ישירה שהפטור אינו תלוי-לוגיקה: גם עם REC_MAX_FILES=0, שום קובץ
    ב-saved/ לא נמחק — כי _sweep_recordings לא סורק לשם בכלל."""
    monkeypatch.setattr(app, "REC_MAX_FILES", 0)
    monkeypatch.setattr(app, "REC_MAX_BYTES", 0)
    p = _mk_rec(paths, NAME, saved=True, age=99999)
    app._sweep_recordings()
    assert p.exists()


# --- תמלול: מצבים ---------------------------------------------------------------

def test_tx_state_none_is_not_empty(client, paths):
    """⚠ 'לא ניסינו' (none) ≠ 'ניסינו ואין דיבור' (empty). בגרסה הקודמת
    שניהם הציגו text=None וזהו — עכשיו יש state נפרד לכל אחד."""
    p = _mk_rec(paths, NAME)
    app._append_activity([{"ts": p.stat().st_mtime, "file": NAME}])
    ev = client.get("/api/activity").get_json()["events"][0]
    assert ev["tx"]["state"] == "none" and ev["text"] is None
    app._write_tx(p, "empty")
    ev = client.get("/api/activity").get_json()["events"][0]
    assert ev["tx"]["state"] == "empty" and ev["text"] is None


def test_tx_failed_state_carries_reason(client, paths):
    p = _mk_rec(paths, NAME)
    app._write_tx(p, "failed", err="חריגת זמן (300ש')")
    app._append_activity([{"ts": p.stat().st_mtime, "file": NAME}])
    tx = client.get("/api/activity").get_json()["events"][0]["tx"]
    assert tx["state"] == "failed" and "חריגת זמן" in tx["err"]


def test_tx_pending_distinguishes_running_from_queued(client, paths):
    """⚠ 'מתמלל עכשיו' ו'ממתין בתור' הם שני מצבים שונים למשתמש —
    tx.running מבדיל ביניהם בלי לשנות את tx.state."""
    p = _mk_rec(paths, NAME)
    app._append_activity([{"ts": p.stat().st_mtime, "file": NAME}])
    app._write_tx(p, "pending")
    ev = client.get("/api/activity").get_json()["events"][0]
    assert ev["tx"]["state"] == "pending" and ev["tx"]["running"] is False
    with app._TX_LOCK:
        app._TX_BUSY["file"] = NAME
    ev = client.get("/api/activity").get_json()["events"][0]
    assert ev["tx"]["running"] is True


def test_legacy_txt_sidecar_still_read(client, paths):
    """התקנות שכבר תמללו לקובצי .txt לא מאבדות את מה שיש להן."""
    p = _mk_rec(paths, NAME)
    app._transcript_path(p).write_text("cleared for takeoff runway 26\n")
    app._append_activity([{"ts": p.stat().st_mtime, "file": NAME}])
    ev = client.get("/api/activity").get_json()["events"][0]
    assert ev["text"] == "cleared for takeoff runway 26"
    assert ev["tx"]["state"] == "ok"


def test_legacy_empty_txt_reads_as_empty_not_none(paths):
    """‏.txt ריק = 'נוסה ולא יצא' (כך כתב ה-worker הישן), לא 'לא ניסינו'."""
    p = _mk_rec(paths, NAME)
    app._transcript_path(p).write_text("\n")
    assert app._read_tx(p)["state"] == "empty"


def test_legacy_txt_with_bad_utf8_does_not_crash_activity(client, paths):
    """⚠ רגרסיה שהוכחה: .txt ישן עם בייט UTF-8 פגום זרק UnicodeDecodeError
    שהפיל את /api/activity ב-500 (כל 15 שניות, כי זה פולינג). errors="replace"
    חייב לכסות גם את מסלול-התאימות הישן, לא רק את ה-sidecar החדש."""
    p = _mk_rec(paths, NAME)
    app._transcript_path(p).write_bytes(b"\xff\xfe not valid utf8")
    app._append_activity([{"ts": p.stat().st_mtime, "file": NAME}])
    r = client.get("/api/activity")
    assert r.status_code == 200


def test_read_tx_with_corrupt_json_sidecar_does_not_crash(paths):
    p = _mk_rec(paths, NAME)
    app._tx_path(p).write_text("{ not json")
    assert app._read_tx(p) == {"state": "none", "text": None}


# --- תמלול: התקנה/מודלים/שפה --------------------------------------------------

def _fake_whisper(paths, monkeypatch, langs=("en",)):
    bin_p = paths / "whisper-cli"
    bin_p.write_text("#!/bin/sh\n")
    monkeypatch.setattr(app, "WHISPER_BIN", str(bin_p))
    mdir = paths / "models"; mdir.mkdir(exist_ok=True)
    monkeypatch.setattr(app, "WHISPER_MODEL_DIR", mdir)
    if "en" in langs:
        (mdir / "ggml-small.en.bin").write_bytes(b"\0")
    if "he" in langs:
        (mdir / "ggml-small.bin").write_bytes(b"\0")
    return bin_p, mdir


def test_transcribe_status_reports_missing_whisper(client, paths, monkeypatch):
    """⚠ בלי זה, 'whisper לא מותקן' נראה ב-UI בדיוק כמו 'אין מה לתמלל' —
    הסיבה המרכזית לכך שהפיצ'ר נראה כאילו הוא לא קיים."""
    monkeypatch.setattr(app, "WHISPER_BIN", str(paths / "nope"))
    monkeypatch.setattr(app, "WHISPER_MODEL_DIR", paths / "no_models")
    tx = client.get("/api/transcribe").get_json()["tx"]
    assert tx["available"] is False and tx["bin_ok"] is False
    assert "install.sh" in tx["install_hint"]


def test_install_hint_puts_env_var_before_sudo(client, paths):
    """⚠ `INSTALL_WHISPER=1 sudo ./install.sh` בולע את המשתנה: sudo מאפס את
    ה-env כברירת מחדל בדביאן. הצורה הנכונה היא `sudo INSTALL_WHISPER=1 ...`."""
    hint = client.get("/api/transcribe").get_json()["tx"]["install_hint"]
    assert hint.index("sudo") < hint.index("INSTALL_WHISPER")


def test_transcribe_status_reports_model_in_use(client, paths, monkeypatch):
    _fake_whisper(paths, monkeypatch)
    tx = client.get("/api/transcribe").get_json()["tx"]
    assert tx["available"] and tx["model_name"] == "ggml-small.en.bin"
    assert tx["langs"] == {"en": True, "he": False}


def test_hebrew_lang_unavailable_without_multilingual_model(client, paths, monkeypatch):
    """⚠ מודל .en לא יכול לתמלל עברית — לא 'פחות טוב', לא נתמך בכלל.
    הבורר חייב לדווח את זה במפורש ולא להריץ ולקבל ג'יבריש."""
    _fake_whisper(paths, monkeypatch, langs=("en",))
    client.post("/api/transcribe", json={"lang": "he"})
    tx = client.get("/api/transcribe").get_json()["tx"]
    assert tx["lang"] == "he" and tx["available"] is False


def test_hebrew_lang_available_with_multilingual_model(client, paths, monkeypatch):
    _fake_whisper(paths, monkeypatch, langs=("en", "he"))
    client.post("/api/transcribe", json={"lang": "he"})
    tx = client.get("/api/transcribe").get_json()["tx"]
    assert tx["lang"] == "he" and tx["available"] is True
    assert tx["model_name"] == "ggml-small.bin"


def test_model_falls_back_along_chain(paths, monkeypatch):
    """שדרוג ל-small.en לא שובר התקנה קיימת שיש בה רק base.en."""
    mdir = paths / "models"; mdir.mkdir()
    (mdir / "ggml-base.en.bin").write_bytes(b"\0")
    monkeypatch.setattr(app, "WHISPER_MODEL_DIR", mdir)
    assert app._whisper_model("en") == str(mdir / "ggml-base.en.bin")


def test_transcribe_lang_toggle_persists(client, paths):
    assert client.post("/api/transcribe", json={"lang": "he"}).get_json()["tx"]["lang"] == "he"
    assert app.load_state()["transcribe_lang"] == "he"


def test_transcribe_bad_lang_rejected(client, paths):
    assert client.post("/api/transcribe", json={"lang": "fr"}).status_code == 400


# --- תמלול: תור מבוסס-sidecar (שורד restart) ----------------------------------

def test_transcribe_on_demand_marks_pending_sidecar(client, paths, monkeypatch):
    """⚠ הבקשה חייבת להיכתב לדיסק, לא לזיכרון: תור בזיכרון (הגרסה הקודמת)
    התאפס ב-restart, וההקלטה חזרה להיראות 'לא ניסינו'."""
    _fake_whisper(paths, monkeypatch)
    p = _mk_rec(paths, NAME)
    r = client.post("/api/recordings/transcribe", json={"file": NAME}).get_json()
    assert r["ok"] and r["tx"]["state"] == "pending"
    assert app._read_tx(p)["state"] == "pending"          # על הדיסק, לא רק בתשובה


def test_pending_sidecar_survives_worker_restart_simulation(client, paths, monkeypatch):
    """מדמה restart: מטהרים כל מצב-זיכרון (_TX_BUSY וכו') ובודקים שהבקשה
    עדיין נבחרת ע"י _tx_next_target — כי המקור-אמת הוא הדיסק."""
    _fake_whisper(paths, monkeypatch)
    p = _mk_rec(paths, NAME)
    client.post("/api/recordings/transcribe", json={"file": NAME})
    with app._TX_LOCK:
        app._TX_BUSY["file"] = None          # "restart" — הזיכרון מתאפס
    target = app._tx_next_target(auto=False)
    assert target is not None and target.name == NAME


def test_transcribe_on_demand_without_whisper_is_501(client, paths, monkeypatch):
    monkeypatch.setattr(app, "WHISPER_BIN", str(paths / "nope"))
    monkeypatch.setattr(app, "WHISPER_MODEL_DIR", paths / "no_models")
    _mk_rec(paths, NAME)
    r = client.post("/api/recordings/transcribe", json={"file": NAME})
    assert r.status_code == 501 and "install.sh" in r.get_json()["error"]


def test_transcribe_on_demand_retries_empty_not_just_failed(client, paths, monkeypatch):
    """⚠ קודם `force` נשלח רק על 'failed', ולכן לחיצה חוזרת על 'empty' לא
    עשתה כלום — הכפתור נראה שבור. עכשיו כל לחיצה מפורשת יוצרת 'pending'."""
    _fake_whisper(paths, monkeypatch)
    p = _mk_rec(paths, NAME)
    app._write_tx(p, "empty")
    r = client.post("/api/recordings/transcribe", json={"file": NAME}).get_json()
    assert r["tx"]["state"] == "pending"


def test_transcribe_on_demand_missing_file_is_404(client, paths, monkeypatch):
    _fake_whisper(paths, monkeypatch)
    r = client.post("/api/recordings/transcribe", json={"file": NAME})
    assert r.status_code == 404


def test_transcribe_on_demand_already_pending_is_idempotent(client, paths, monkeypatch):
    _fake_whisper(paths, monkeypatch)
    _mk_rec(paths, NAME)
    r1 = client.post("/api/recordings/transcribe", json={"file": NAME}).get_json()
    r2 = client.post("/api/recordings/transcribe", json={"file": NAME}).get_json()
    assert r1["ok"] and r2["ok"] and r2["tx"]["state"] == "pending"


def test_transcribe_auto_toggle_persists(client, paths):
    assert client.post("/api/transcribe", json={"auto": True}).get_json()["tx"]["auto"]
    assert app.load_state()["transcribe_auto"] is True
    assert not client.post("/api/transcribe", json={"auto": False}).get_json()["tx"]["auto"]


def test_transcribe_auto_defaults_to_off(paths):
    """⚠ ברירת המחדל היא כבוי, גם אם AIRAM_TRANSCRIBE=1 (כפי ש-install.sh
    כותב עם INSTALL_WHISPER=1) — אחרת כל מי שהתקין whisper מקבל תמלול-הכול
    בלי שביקש, וזה קפיצה משמעותית בעומס עם המודל האיטי יותר."""
    assert app.DEFAULT_STATE["transcribe_auto"] is False


def test_transcribe_toggle_requires_field(client, paths):
    assert client.post("/api/transcribe", json={}).status_code == 400


# --- סדר העדיפויות ------------------------------------------------------------

def test_next_target_prefers_pending_over_everything(paths, monkeypatch):
    _mk_rec(paths, NAME, age=5)
    other = "airam_20260612_130000_134600000.mp3"
    p2 = _mk_rec(paths, other, age=1, saved=True)
    app._write_tx(app.REC_DIR / NAME, "pending")
    assert app._tx_next_target(auto=True).name == NAME   # לפני השמורה


def test_next_target_picks_saved_before_others(paths):
    """שמורה כבויה-אוטומטית מתומללת בכל זאת — זה החיבור בין שני הפיצ'רים."""
    _mk_rec(paths, NAME, age=999, saved=True)
    fresh = "airam_20260612_130000_134600000.mp3"
    _mk_rec(paths, fresh, age=1)
    assert app._tx_next_target(auto=False).name == NAME
    assert app._tx_next_target(auto=True).name == NAME    # גם עם auto — קודמת


def test_next_target_none_when_auto_off_and_nothing_saved(paths):
    _mk_rec(paths, NAME)
    assert app._tx_next_target(auto=False) is None
    assert app._tx_next_target(auto=True).name == NAME


def test_next_target_skips_already_transcribed(paths):
    p = _mk_rec(paths, NAME)
    app._write_tx(p, "failed", err="rc=1")
    # sidecar קיים (גם 'נכשל') => לא נבחר שוב לבד; ניסיון חוזר הוא פעולה מפורשת
    assert app._tx_next_target(auto=True) is None


def test_next_target_skips_file_after_max_fails(paths, monkeypatch):
    """⚠ בלי זה, כשל *מתמשך* בכתיבת ה-sidecar (ENOSPC) גורם ל-worker לבחור
    את אותו קובץ שוב ושוב ולהריץ עליו whisper לנצח (הוכח)."""
    p = _mk_rec(paths, NAME)
    app._TX_FAILS[NAME] = app.TX_MAX_FAILS
    assert app._tx_next_target(auto=True) is None


# --- לולאת ה-worker: כשל כתיבה חוזר לא נלכד באינסוף ----------------------------

def test_worker_gives_up_after_max_write_failures(paths, monkeypatch):
    _fake_whisper(paths, monkeypatch)
    p = _mk_rec(paths, NAME)
    app._tx_path(p).write_text('{"state": "pending"}')   # בקשה מפורשת, בלי דרך _write_tx
    monkeypatch.setattr(app, "WATCH_INTERVAL", 0.01)
    monkeypatch.setattr(app.subprocess, "run",
                        lambda *a, **k: type("R", (), {"stdout": "hi"})())
    def boom_write(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(app, "_write_tx", boom_write)
    t = threading.Thread(target=app._transcribe_worker, daemon=True)
    t.start()
    for _ in range(200):
        if app._TX_FAILS.get(NAME, 0) >= app.TX_MAX_FAILS:
            break
        time.sleep(0.01)
    assert app._TX_FAILS.get(NAME, 0) >= app.TX_MAX_FAILS
    # thread דמון — לא מחכים לו; רק מוודאים שהוא לא נעל את התהליך


# --- אין עוד סינון-הזיות (הוסר אחרי שהוכח שהוא אוכל תוכן אמיתי) ----------------

@pytest.mark.parametrize("text", [
    "Thank you, 385", "Okay, 03", "Thanks, 26", "Ok.", "Uh, 26",
])
def test_short_atc_acknowledgements_are_not_filtered(paths, monkeypatch, text):
    """⚠ רגרסיה: הייתה כאן רשימת-הזיות שמחקה ספרות בנרמול ולכן חסמה בדיוק
    את סוג התשדורות האלה — מסירות תדר ואישורים קצרים, שהם רוב ATC. אין
    יותר סינון-תוכן; אם whisper פלט טקסט, הוא מוצג כמו שהוא."""
    _fake_whisper(paths, monkeypatch)
    p = _mk_rec(paths, NAME)

    class R:
        stdout = text + "\n"
    monkeypatch.setattr(app.subprocess, "run", lambda *a, **k: R())
    state, out, err = app._transcribe_file(p)
    assert state == "ok" and out == text


def test_transcribe_file_returns_ok_text(paths, monkeypatch):
    _fake_whisper(paths, monkeypatch)
    p = _mk_rec(paths, NAME)

    class R:
        stdout = "  El Al 385   cleared to land runway 26  \n"
    monkeypatch.setattr(app.subprocess, "run", lambda *a, **k: R())
    state, text, err = app._transcribe_file(p)
    assert state == "ok" and text == "El Al 385 cleared to land runway 26" and err is None


def test_transcribe_file_ffmpeg_gets_explicit_wav_format(paths, monkeypatch):
    """⚠ רגרסיה אמיתית שקרתה בשטח: הקובץ הזמני נקרא `<name>.mp3.wav.tmp` —
    הסיומת שffmpeg *באמת* רואה היא `.tmp`, לא `.wav`. בלי `-f wav` מפורש,
    ffmpeg מנחש פורמט-פלט לפי סיומת ונכשל על *כל* הקלטה עם "Unable to choose
    an output format", בלי שום קשר לתוכן (נצפה: rc=234, לא שגיאת whisper-cli
    כפי שההודעה הטעתה להניח בהתחלה). מוודאים את הדגל בפועל, לא רק שהקוד "עובד"."""
    _fake_whisper(paths, monkeypatch)
    p = _mk_rec(paths, NAME)
    calls = []
    def fake_run(cmd, **kw):
        calls.append(cmd)
        return type("R", (), {"stdout": "cleared for takeoff"})()
    monkeypatch.setattr(app.subprocess, "run", fake_run)
    app._transcribe_file(p)
    ffmpeg_cmd = calls[0]
    assert "ffmpeg" in ffmpeg_cmd
    assert "-f" in ffmpeg_cmd and ffmpeg_cmd[ffmpeg_cmd.index("-f") + 1] == "wav"


def test_transcribe_file_empty_output_is_empty_state(paths, monkeypatch):
    _fake_whisper(paths, monkeypatch)
    p = _mk_rec(paths, NAME)

    class R:
        stdout = "   \n"
    monkeypatch.setattr(app.subprocess, "run", lambda *a, **k: R())
    state, text, err = app._transcribe_file(p)
    assert state == "empty" and text is None


def test_transcribe_file_timeout_is_failed_with_reason(paths, monkeypatch):
    _fake_whisper(paths, monkeypatch)
    p = _mk_rec(paths, NAME)

    def boom(*a, **k):
        raise app.subprocess.TimeoutExpired("whisper", 300)
    monkeypatch.setattr(app.subprocess, "run", boom)
    state, text, err = app._transcribe_file(p)
    assert state == "failed" and "חריגת זמן" in err


def test_transcribe_file_uses_nice_for_both_subprocesses(paths, monkeypatch):
    """⚠ הגבלת threads לבדה לא נותנת עדיפות לרדיו — nice הוא מה שקובע תזמון."""
    _fake_whisper(paths, monkeypatch)
    p = _mk_rec(paths, NAME)
    calls = []
    def fake_run(cmd, **k):
        calls.append(cmd)
        return type("R", (), {"stdout": "ok"})()
    monkeypatch.setattr(app.subprocess, "run", fake_run)
    app._transcribe_file(p)
    assert all(cmd[0] == "nice" and cmd[1] == "-n" and cmd[2] == app.WHISPER_NICE
              for cmd in calls)


def test_transcribe_file_hebrew_skips_atc_prompt(paths, monkeypatch):
    """רמז ATC אנגלי לא שייך לתמלול עברי — מטה את המודל לשפה הלא-נכונה."""
    _fake_whisper(paths, monkeypatch, langs=("he",))
    p = _mk_rec(paths, NAME)
    calls = []
    def fake_run(cmd, **k):
        calls.append(cmd)
        return type("R", (), {"stdout": "שלום"})()
    monkeypatch.setattr(app.subprocess, "run", fake_run)
    app._transcribe_file(p, lang="he")
    whisper_call = calls[-1]
    assert "--prompt" not in whisper_call
    assert "-l" in whisper_call and whisper_call[whisper_call.index("-l") + 1] == "he"


# --- retention של קובצי הצד ---------------------------------------------------

def test_sweep_removes_tx_json_sidecar_with_recording(paths, monkeypatch):
    monkeypatch.setattr(app, "REC_MAX_FILES", 1)
    for i in range(3):
        p = _mk_rec(paths, f"airam_2026061{i}_120001_134600000.mp3", size=10, age=30 - i)
        app._write_tx(p, "ok", text="t")
    app._sweep_recordings()
    assert len(list(app.REC_DIR.glob("*.mp3"))) == 1
    assert len(list(app.REC_DIR.glob("*.tx.json"))) == 1


def test_sweep_removes_orphaned_tx_json(paths):
    orphan = app.REC_DIR / (NAME + ".tx.json")
    orphan.write_text(json.dumps({"state": "ok", "text": "x"}))
    app._sweep_recordings()
    assert not orphan.exists()


def test_sweep_survives_broken_stat_on_one_file(paths, monkeypatch):
    """⚠ רגרסיה שהוכחה: symlink שבור/EACCES על קובץ *אחד* היה מבטל את
    ה-retention **כולו** בשקט (כרטיס ה-SD מתמלא בלי שום שגיאה גלויה)."""
    monkeypatch.setattr(app, "REC_MAX_FILES", 2)
    good = [_mk_rec(paths, f"airam_2026061{i}_120001_134600000.mp3", age=10 - i)
           for i in range(5)]
    broken = app.REC_DIR / "airam_20260619_120001_134600000.mp3"
    broken.symlink_to(app.REC_DIR / "does_not_exist.mp3")   # stat() יזרוק OSError
    app._sweep_recordings()
    assert len(list(app.REC_DIR.glob("*.mp3"))) <= app.REC_MAX_FILES + 1  # לא 6


# --- ייצוא ZIP ------------------------------------------------------------------

def test_starred_zip_export_contains_recordings_and_transcripts(client, paths):
    p1 = _mk_rec(paths, NAME, saved=True)
    app._write_tx(p1, "ok", text="cleared for takeoff runway 26")
    other = "airam_20260612_130000_134600000.mp3"
    _mk_rec(paths, other, saved=True)          # בלי תמלול
    r = client.get("/api/recordings/starred.zip")
    assert r.status_code == 200
    assert r.mimetype == "application/zip"
    import io
    z = zipfile.ZipFile(io.BytesIO(r.data))
    names = set(z.namelist())
    assert NAME in names and other in names
    assert NAME + ".txt" in names and other + ".txt" not in names
    assert z.read(NAME + ".txt").decode() == "cleared for takeoff runway 26\n"


def test_starred_zip_export_empty_is_404(client, paths):
    r = client.get("/api/recordings/starred.zip")
    assert r.status_code == 404


def test_recordings_route_serves_from_saved_dir(client, paths):
    """‏/recordings/<name> חייב למצוא הקלטה גם אחרי שהיא זזה ל-saved/."""
    _mk_rec(paths, NAME, saved=True)
    assert client.get(f"/recordings/{NAME}").status_code == 200
