# ============================================================================
#  AIR-AM - בדיקות להקלטות: סימון בכוכב (⭐) ותמלול ATC
# ----------------------------------------------------------------------------
#  רץ בלי חומרה ובלי whisper: הבינארי/המודל ממוקפים, subprocess מוחלף.
#  שני הפיצ'רים קשורים (מסומנת => מתומללת אוטומטית) ולכן נבדקים יחד.
#  הרצה: pytest tests/test_recordings.py
# ============================================================================
import json
import os
import time

import pytest

import app


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(app, "ACTIVITY_PATH", tmp_path / "activity.jsonl")
    monkeypatch.setattr(app, "STARRED_PATH", tmp_path / "starred.json")
    rec = tmp_path / "recordings"
    rec.mkdir()
    monkeypatch.setattr(app, "REC_DIR", rec)
    return tmp_path


@pytest.fixture
def client(paths):
    return app.app.test_client()


@pytest.fixture(autouse=True)
def _clean_tx_queue():
    """התור והדגל 'מתמלל כרגע' הם מצב גלובלי (‏module-level) — בלי איפוס,
    בדיקה שמכניסה לתור הייתה מדליפה ל-בדיקה הבאה."""
    with app._TX_LOCK:
        app._TX_QUEUE.clear()
        app._TX_BUSY["file"] = None
    yield
    with app._TX_LOCK:
        app._TX_QUEUE.clear()
        app._TX_BUSY["file"] = None


def _mk_rec(paths, name, size=4096, age=0):
    p = app.REC_DIR / name
    p.write_bytes(b"\0" * size)
    if age:
        t = time.time() - age
        os.utime(p, (t, t))
    return p


NAME = "airam_20260611_120001_134600000.mp3"


# --- סימון בכוכב -------------------------------------------------------------

def test_star_and_unstar_roundtrip(client, paths):
    _mk_rec(paths, NAME)
    r = client.post("/api/recordings/star", json={"file": NAME}).get_json()
    assert r["ok"] and r["starred"] and r["count"] == 1
    assert NAME in app._load_starred()
    r = client.post("/api/recordings/star",
                    json={"file": NAME, "starred": False}).get_json()
    assert r["ok"] and not r["starred"] and r["count"] == 0
    assert app._load_starred() == {}


def test_star_rejects_bad_filename(client, paths):
    """‏_REC_NAME_RE הוא גם הגנת ה-path traversal: שם שאינו airam_...mp3 נדחה."""
    for bad in ("../../etc/passwd", "airam_x.mp3", "", "airam_20260611_120001_1.txt"):
        assert client.post("/api/recordings/star", json={"file": bad}).status_code == 400


def test_star_missing_recording_is_404(client, paths):
    assert client.post("/api/recordings/star", json={"file": NAME}).status_code == 404


def test_starred_recording_survives_retention(client, paths, monkeypatch):
    """⭐ הלב של הפיצ'ר: הקלטה מסומנת לא נמחקת גם כשהיא הכי ישנה והמכסה עברה."""
    monkeypatch.setattr(app, "REC_MAX_FILES", 2)
    old = _mk_rec(paths, NAME, age=9999)                      # הישנה ביותר
    for i in range(5):
        _mk_rec(paths, f"airam_2026061{i}_130000_134600000.mp3", age=100 - i)
    client.post("/api/recordings/star", json={"file": NAME})
    app._sweep_recordings()
    assert old.exists(), "הקלטה מסומנת נמחקה ב-retention"
    # והמסומנת גם לא 'גנבה' מקום מהמכסה של הרגילות
    assert len(list(app.REC_DIR.glob("*.mp3"))) == app.REC_MAX_FILES + 1


def test_starred_does_not_consume_normal_quota_bytes(client, paths, monkeypatch):
    """מסומנת לא נספרת במכסת הבתים — אחרת סימון 100MB היה מאפס את החלון החי."""
    monkeypatch.setattr(app, "REC_MAX_BYTES", 10_000)
    big = _mk_rec(paths, NAME, size=9_000, age=9999)
    client.post("/api/recordings/star", json={"file": NAME})
    keep = [_mk_rec(paths, f"airam_2026061{i}_130000_134600000.mp3",
                    size=3_000, age=10 - i) for i in range(3)]
    app._sweep_recordings()
    assert big.exists()
    assert all(p.exists() for p in keep), "המסומנת דחקה החוצה הקלטות חיות"


def test_star_quota_refuses_instead_of_deleting(client, paths, monkeypatch):
    """⚠ כשהמכסה מלאה **מסרבים לסמן**, ולא מוחקים מסומנת ותיקה: מחיקת קובץ
    שהמשתמש הגן עליו במפורש היא בדיוק מה שהפיצ'ר נועד למנוע."""
    monkeypatch.setattr(app, "REC_STAR_MAX_FILES", 2)
    names = [f"airam_2026061{i}_130000_134600000.mp3" for i in range(3)]
    for n in names:
        _mk_rec(paths, n)
    assert client.post("/api/recordings/star", json={"file": names[0]}).status_code == 200
    assert client.post("/api/recordings/star", json={"file": names[1]}).status_code == 200
    r = client.post("/api/recordings/star", json={"file": names[2]})
    assert r.status_code == 409 and "מכסת" in r.get_json()["error"]
    assert len(app._load_starred()) == 2                  # אף אחת לא הוסרה
    assert all((app.REC_DIR / n).exists() for n in names)


def test_star_quota_counts_bytes(client, paths, monkeypatch):
    monkeypatch.setattr(app, "REC_STAR_MAX_BYTES", 5_000)
    a = "airam_20260611_130000_134600000.mp3"
    b = "airam_20260612_130000_134600000.mp3"
    _mk_rec(paths, a, size=4_000)
    _mk_rec(paths, b, size=4_000)
    assert client.post("/api/recordings/star", json={"file": a}).status_code == 200
    assert client.post("/api/recordings/star", json={"file": b}).status_code == 409


def test_restar_same_file_is_idempotent(client, paths, monkeypatch):
    """סימון חוזר של קובץ שכבר מסומן לא נחשב לתפיסת מכסה נוספת."""
    monkeypatch.setattr(app, "REC_STAR_MAX_FILES", 1)
    _mk_rec(paths, NAME)
    assert client.post("/api/recordings/star", json={"file": NAME}).status_code == 200
    assert client.post("/api/recordings/star", json={"file": NAME}).status_code == 200
    assert len(app._load_starred()) == 1


def test_activity_reports_starred_flag(client, paths):
    p = _mk_rec(paths, NAME)
    app._append_activity([{"ts": p.stat().st_mtime, "freq": 134.6, "file": NAME}])
    assert client.get("/api/activity").get_json()["events"][0]["starred"] is False
    client.post("/api/recordings/star", json={"file": NAME})
    assert client.get("/api/activity").get_json()["events"][0]["starred"] is True


def test_starred_view_survives_activity_log_trim(client, paths):
    """⚠ הסיבה ש-starred.json שומר את הרשומה המלאה ולא רק את השם: היומן מקוצץ
    ל-ACTIVITY_KEEP, והקלטה שמורה מלפני חודש הייתה נעלמת מה-UI למרות שהקובץ קיים."""
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
    assert ev["freq"] == 134.6                              # המטא-דאטה נשמרה


def test_sweep_prunes_star_entry_when_file_gone(client, paths):
    """הקלטה שנמחקה ידנית לא תשאיר רשומת-כוכב שתופסת מכסה לנצח."""
    p = _mk_rec(paths, NAME)
    client.post("/api/recordings/star", json={"file": NAME})
    p.unlink()
    app._sweep_recordings()
    assert app._load_starred() == {}


def test_corrupt_starred_file_does_not_delete_recordings(paths):
    """‏starred.json פגום => מתייחסים כאל 'אין מסומנים', בלי לזרוק. ההקלטות
    עצמן לא נפגעות (retention רגיל ימשיך לעבוד)."""
    app.STARRED_PATH.write_text("{ this is not json")
    _mk_rec(paths, NAME)
    assert app._load_starred() == {}
    app._sweep_recordings()
    assert (app.REC_DIR / NAME).exists()


# --- תמלול -------------------------------------------------------------------

def _fake_whisper(paths, monkeypatch, model=True):
    bin_p = paths / "whisper-cli"
    bin_p.write_text("#!/bin/sh\n")
    monkeypatch.setattr(app, "WHISPER_BIN", str(bin_p))
    mdl = paths / "ggml-small.en.bin"
    if model:
        mdl.write_bytes(b"\0")
    monkeypatch.setattr(app, "WHISPER_MODEL", str(mdl))
    monkeypatch.setattr(app, "WHISPER_MODEL_FALLBACKS", (str(mdl),))
    return bin_p, mdl


def test_tx_state_none_is_not_empty(client, paths):
    """⚠ התיקון המרכזי: 'לא ניסינו' (none) ≠ 'ניסינו ואין דיבור' (empty).
    בגרסה הקודמת שניהם היו text=None וזהים לחלוטין למשתמש."""
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


def test_tx_pending_shown_while_queued(client, paths):
    p = _mk_rec(paths, NAME)
    app._append_activity([{"ts": p.stat().st_mtime, "file": NAME}])
    app._tx_enqueue(NAME)
    assert client.get("/api/activity").get_json()["events"][0]["tx"]["state"] == "pending"


def test_legacy_txt_sidecar_still_read(client, paths):
    """התקנות שכבר תמללו לקובצי .txt לא מאבדות את מה שיש להן."""
    p = _mk_rec(paths, NAME)
    app._transcript_path(p).write_text("cleared for takeoff runway 26\n")
    app._append_activity([{"ts": p.stat().st_mtime, "file": NAME}])
    ev = client.get("/api/activity").get_json()["events"][0]
    assert ev["text"] == "cleared for takeoff runway 26"
    assert ev["tx"]["state"] == "ok" and ev["tx"]["legacy"]


def test_legacy_empty_txt_reads_as_empty_not_none(paths):
    """‏.txt ריק = 'נוסה ולא יצא' (כך כתב ה-worker הישן), לא 'לא ניסינו'."""
    p = _mk_rec(paths, NAME)
    app._transcript_path(p).write_text("\n")
    assert app._read_tx(p)["state"] == "empty"


def test_transcribe_status_reports_missing_whisper(client, paths, monkeypatch):
    """⚠ בלי זה, 'whisper לא מותקן' נראה ב-UI בדיוק כמו 'אין מה לתמלל' —
    הסיבה המרכזית לכך שהפיצ'ר נראה כאילו הוא לא קיים."""
    monkeypatch.setattr(app, "WHISPER_BIN", str(paths / "nope"))
    monkeypatch.setattr(app, "WHISPER_MODEL", str(paths / "nope.bin"))
    monkeypatch.setattr(app, "WHISPER_MODEL_FALLBACKS", ())
    tx = client.get("/api/transcribe").get_json()["tx"]
    assert tx["available"] is False and tx["bin_ok"] is False
    assert "INSTALL_WHISPER" in tx["install_hint"]


def test_transcribe_status_reports_model_in_use(client, paths, monkeypatch):
    _fake_whisper(paths, monkeypatch)
    tx = client.get("/api/transcribe").get_json()["tx"]
    assert tx["available"] and tx["model_name"] == "ggml-small.en.bin"


def test_model_falls_back_to_base_when_small_missing(paths, monkeypatch):
    """שדרוג ל-small.en לא שובר התקנה קיימת שיש בה רק base.en."""
    base = paths / "ggml-base.en.bin"
    base.write_bytes(b"\0")
    monkeypatch.setattr(app, "WHISPER_MODEL", str(paths / "ggml-small.en.bin"))
    monkeypatch.setattr(app, "WHISPER_MODEL_FALLBACKS",
                        (str(paths / "ggml-small.en.bin"), str(base)))
    assert app._whisper_model() == str(base)


def test_transcribe_on_demand_enqueues(client, paths, monkeypatch):
    _fake_whisper(paths, monkeypatch)
    _mk_rec(paths, NAME)
    r = client.post("/api/recordings/transcribe", json={"file": NAME}).get_json()
    assert r["ok"] and r["queued"] == 1 and r["tx"]["state"] == "pending"
    assert app._tx_pending(NAME)


def test_transcribe_on_demand_without_whisper_is_501(client, paths, monkeypatch):
    monkeypatch.setattr(app, "WHISPER_BIN", str(paths / "nope"))
    monkeypatch.setattr(app, "WHISPER_MODEL_FALLBACKS", ())
    _mk_rec(paths, NAME)
    r = client.post("/api/recordings/transcribe", json={"file": NAME})
    assert r.status_code == 501 and "INSTALL_WHISPER" in r.get_json()["error"]


def test_transcribe_on_demand_skips_done_unless_forced(client, paths, monkeypatch):
    _fake_whisper(paths, monkeypatch)
    p = _mk_rec(paths, NAME)
    app._write_tx(p, "ok", text="line up and wait")
    r = client.post("/api/recordings/transcribe", json={"file": NAME}).get_json()
    assert r["tx"]["state"] == "ok" and not app._tx_pending(NAME)
    client.post("/api/recordings/transcribe", json={"file": NAME, "force": True})
    assert app._tx_pending(NAME)


def test_transcribe_auto_toggle_persists(client, paths):
    assert client.post("/api/transcribe", json={"auto": True}).get_json()["tx"]["auto"]
    assert app.load_state()["transcribe_auto"] is True
    assert not client.post("/api/transcribe", json={"auto": False}).get_json()["tx"]["auto"]
    assert app.load_state()["transcribe_auto"] is False


def test_transcribe_toggle_requires_field(client, paths):
    assert client.post("/api/transcribe", json={}).status_code == 400


# --- סדר העדיפויות של המודל ההיברידי -----------------------------------------

def test_next_target_prefers_on_demand_queue(paths, monkeypatch):
    _mk_rec(paths, NAME, age=5)
    other = "airam_20260612_130000_134600000.mp3"
    _mk_rec(paths, other, age=1)
    app._tx_enqueue(NAME)
    assert app._tx_next_target(auto=True).name == NAME   # לפני החדשה יותר


def test_next_target_picks_starred_before_others(client, paths):
    """אוטומטי כבוי => רק מסומנות מתומללות. זה החיבור בין שני הפיצ'רים."""
    _mk_rec(paths, NAME, age=999)
    fresh = "airam_20260612_130000_134600000.mp3"
    _mk_rec(paths, fresh, age=1)
    client.post("/api/recordings/star", json={"file": NAME})
    assert app._tx_next_target(auto=False).name == NAME
    assert app._tx_next_target(auto=True).name == NAME    # גם עם auto — קודמת


def test_next_target_none_when_auto_off_and_nothing_starred(paths):
    _mk_rec(paths, NAME)
    assert app._tx_next_target(auto=False) is None
    assert app._tx_next_target(auto=True).name == NAME


def test_next_target_skips_already_transcribed(paths):
    p = _mk_rec(paths, NAME)
    app._write_tx(p, "failed", err="rc=1")
    # sidecar קיים (גם 'נכשל') => לא נבחר שוב לבד; ניסיון חוזר הוא פעולה מפורשת
    assert app._tx_next_target(auto=True) is None


def test_next_target_drops_queued_file_that_vanished(paths):
    app._tx_enqueue("airam_20260611_120001_134600000.mp3")
    assert app._tx_next_target(auto=False) is None
    assert not app._tx_pending(NAME)


# --- סינון הזיות של whisper ---------------------------------------------------

@pytest.mark.parametrize("text", [
    "Thank you.", "thank you", "  Thanks for watching! ", "[BLANK_AUDIO]",
    "(silence)", "You", "Bye.", "*music*", "",
])
def test_noise_phrases_detected(text):
    assert app._tx_is_noise(text)


@pytest.mark.parametrize("text", [
    "cleared for takeoff runway 26",
    "El Al 385 contact tower 134.6",
    "hold short runway 03",
    "thank you for the vectors, El Al 385",   # מכיל 'thank you' אבל אינו רק זה
])
def test_real_atc_text_not_filtered(text):
    assert not app._tx_is_noise(text)


def test_transcribe_file_filters_hallucination_but_keeps_raw(paths, monkeypatch):
    """⚠ §12: מסננים את ההזיה מהתצוגה, אבל **לא מסתירים** מה המפענח פלט —
    הגולמי נשמר ב-raw והרשומה מסומנת filtered."""
    _fake_whisper(paths, monkeypatch)
    p = _mk_rec(paths, NAME)

    class R:
        stdout = "Thank you.\n"
    monkeypatch.setattr(app.subprocess, "run", lambda *a, **k: R())
    state, text, raw, err = app._transcribe_file(p)
    assert state == "empty" and text is None and raw == "Thank you."
    rec = app._write_tx(p, state, text=text, raw=raw, err=err, filtered=True)
    assert rec["raw"] == "Thank you." and rec["filtered"] is True


def test_transcribe_file_returns_ok_text(paths, monkeypatch):
    _fake_whisper(paths, monkeypatch)
    p = _mk_rec(paths, NAME)

    class R:
        stdout = "  El Al 385   cleared to land runway 26  \n"
    monkeypatch.setattr(app.subprocess, "run", lambda *a, **k: R())
    state, text, raw, err = app._transcribe_file(p)
    assert state == "ok" and text == "El Al 385 cleared to land runway 26" and err is None


def test_transcribe_file_timeout_is_failed_with_reason(paths, monkeypatch):
    _fake_whisper(paths, monkeypatch)
    p = _mk_rec(paths, NAME)

    def boom(*a, **k):
        raise app.subprocess.TimeoutExpired("whisper", 300)
    monkeypatch.setattr(app.subprocess, "run", boom)
    state, text, raw, err = app._transcribe_file(p)
    assert state == "failed" and "חריגת זמן" in err


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


def test_sweep_keeps_tx_json_of_starred(client, paths, monkeypatch):
    """קובץ-הצד של הקלטה מסומנת שורד יחד איתה."""
    monkeypatch.setattr(app, "REC_MAX_FILES", 1)
    p = _mk_rec(paths, NAME, size=10, age=999)
    app._write_tx(p, "ok", text="cleared for takeoff")
    client.post("/api/recordings/star", json={"file": NAME})
    for i in range(3):
        _mk_rec(paths, f"airam_2026061{i}_130000_134600000.mp3", size=10, age=10 - i)
    app._sweep_recordings()
    assert p.exists() and app._tx_path(p).exists()
    assert app._read_tx(p)["text"] == "cleared for takeoff"
