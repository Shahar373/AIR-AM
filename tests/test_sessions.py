# ============================================================================
#  AIR-AM - בדיקות לשחזור-סשן שלב 2 (docs/session-replay-design.md §4.3/§8)
# ----------------------------------------------------------------------------
#  POST/GET /api/sessions, GET/DELETE /api/sessions/<id>, /track, /clips/<name>,
#  /export.zip. רץ בלי חומרה: track.jsonl/REC_DIR/SESSIONS_DIR ממוקפים ל-tmp_path,
#  אותו דפוס כמו test_replay_buffer.py (adsb כותב) + test_recordings.py (קבצים).
# ============================================================================
import gzip
import json
import os
import time
import zipfile

import pytest

import adsb
import app


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(adsb, "TRACK_PATH", tmp_path / "track.jsonl")
    monkeypatch.setattr(app, "SESSIONS_DIR", tmp_path / "sessions")
    rec = tmp_path / "recordings"
    rec.mkdir()
    monkeypatch.setattr(app, "REC_DIR", rec)
    return tmp_path


@pytest.fixture
def client(paths):
    return app.app.test_client()


def _write_track(rows):
    adsb.TRACK_PATH.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")


def _ac_row(t, aircraft):
    """aircraft: [(reg, lat, lon, alt, track, gs, nic), ...]"""
    return {"t": t, "ac": [list(a) for a in aircraft]}


def _mk_rec(name, size=4096, age=0, saved=False):
    d = app._saved_dir() if saved else app.REC_DIR
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_bytes(b"\0" * size)
    t = time.time() - age
    os.utime(p, (t, t))
    return p


NAME1 = "airam_20260817_120000_134600000.mp3"
NAME2 = "airam_20260817_120500_132500000.mp3"


# --- POST /api/sessions (יצירה) -----------------------------------------------

def test_create_session_basic(client, paths):
    now = time.time()
    _write_track([
        _ac_row(now - 120, [("4X-EHD", 32.2, 34.7, 3200, 90.0, 180.0, 8)]),
        _ac_row(now - 60, [("4X-EHD", 32.21, 34.71, 3200, 90.0, 180.0, 8)]),
    ])
    r = client.post("/api/sessions", json={"minutes": 5, "note": "בדיקה"}).get_json()
    assert r["ok"] and r["id"]
    meta = r["session"]
    assert meta["aircraft"] == ["4X-EHD"]
    assert meta["note"] == "בדיקה"
    assert meta["counts"]["samples"] == 2
    assert (app.SESSIONS_DIR / r["id"] / "meta.json").is_file()
    assert (app.SESSIONS_DIR / r["id"] / "track.jsonl.gz").is_file()


def test_create_session_no_data_is_400(client, paths):
    r = client.post("/api/sessions", json={"minutes": 5})
    assert r.status_code == 400
    assert r.get_json()["ok"] is False


def test_create_session_rejects_bad_minutes(client, paths):
    _write_track([_ac_row(time.time(), [])])
    for bad in (0, -5, "abc"):
        r = client.post("/api/sessions", json={"minutes": bad})
        assert r.status_code == 400


def test_create_session_clamps_minutes_to_buffer(client, paths, monkeypatch):
    """⚠ אי אפשר לשמור מה שכבר נגזם מ-track.jsonl — minutes נחתך ל-TRACK_BUFFER_MIN."""
    monkeypatch.setattr(adsb, "TRACK_BUFFER_MIN", 5.0)
    now = time.time()
    _write_track([_ac_row(now - 60, [("4X-EHD", 32.2, 34.7, 3200, 90.0, 180.0, 8)])])
    r = client.post("/api/sessions", json={"minutes": 999}).get_json()
    assert r["ok"]
    meta = r["session"]
    # t_start נחתך ל-5 דקות אחורה, לא 999
    assert meta["t_end"] - meta["t_start"] == pytest.approx(5 * 60, abs=1)


def test_create_session_default_minutes_is_full_buffer(client, paths, monkeypatch):
    monkeypatch.setattr(adsb, "TRACK_BUFFER_MIN", 3.0)
    now = time.time()
    _write_track([_ac_row(now - 60, [])])
    r = client.post("/api/sessions", json={}).get_json()
    assert r["ok"]
    assert r["session"]["t_end"] - r["session"]["t_start"] == pytest.approx(3 * 60, abs=1)


def test_create_session_includes_gap_rows(client, paths):
    now = time.time()
    _write_track([
        _ac_row(now - 120, [("4X-EHD", 32.2, 34.7, 3200, 90.0, 180.0, 8)]),
        {"t": now - 60, "gap": "no_adsb", "detail": "timeout"},
    ])
    r = client.post("/api/sessions", json={"minutes": 5}).get_json()
    assert r["session"]["gaps"] == [{"t": now - 60, "reason": "no_adsb", "detail": "timeout"}]


def test_create_session_keeps_spoofed_aircraft_with_null_position(client, paths):
    """§7.1: מטוס עם lat/lon=None עדיין מופיע ברשימת המטוסים — לא נעלם."""
    now = time.time()
    _write_track([_ac_row(now - 30, [("4X-EKS", None, None, 11000, 95.0, 440.0, 0)])])
    r = client.post("/api/sessions", json={"minutes": 5}).get_json()
    assert r["session"]["aircraft"] == ["4X-EKS"]


def test_create_session_moves_unstarred_clip_into_session(client, paths):
    now = time.time()
    _write_track([_ac_row(now - 60, [])])
    _mk_rec(NAME1, age=60)
    r = client.post("/api/sessions", json={"minutes": 5}).get_json()
    assert r["ok"]
    sid = r["id"]
    assert not (app.REC_DIR / NAME1).exists()
    assert (app.SESSIONS_DIR / sid / app.SESSION_CLIPS_DIRNAME / NAME1).is_file()
    assert [c["file"] for c in r["session"]["clips"]] == [NAME1]


def test_create_session_copies_starred_clip_keeps_original(client, paths):
    """⚠ שמורה (★) מ*עתיקה* לסשן, לא מועברת — נשארת מוגנת ב-saved/ גם אחרי
    שהיא חלק מסשן, בדיוק כמו שהוחלט: שני מנגנוני-הגנה לא מתחרים."""
    now = time.time()
    _write_track([_ac_row(now - 60, [])])
    _mk_rec(NAME1, age=60, saved=True)
    r = client.post("/api/sessions", json={"minutes": 5}).get_json()
    assert r["ok"]
    sid = r["id"]
    assert (app._saved_dir() / NAME1).is_file()          # המקור נשאר
    assert (app.SESSIONS_DIR / sid / app.SESSION_CLIPS_DIRNAME / NAME1).is_file()


def test_create_session_excludes_clips_outside_window(client, paths):
    now = time.time()
    _write_track([_ac_row(now - 60, [])])
    _mk_rec(NAME1, age=3600)   # שעה אחורה — מחוץ לחלון 5 דק'
    r = client.post("/api/sessions", json={"minutes": 5}).get_json()
    assert r["session"]["clips"] == []
    assert (app.REC_DIR / NAME1).is_file()                # לא זזה


# --- GET /api/sessions (רשימה) -------------------------------------------------

def test_list_sessions_empty(client, paths):
    r = client.get("/api/sessions").get_json()
    assert r["ok"] and r["sessions"] == []


def test_list_sessions_sorted_newest_first(client, paths):
    for sid, created in (("20260101-0000", 100), ("20260102-0000", 200)):
        d = app.SESSIONS_DIR / sid
        (d / app.SESSION_CLIPS_DIRNAME).mkdir(parents=True)
        (d / "meta.json").write_text(json.dumps({"id": sid, "created_at": created}))
    r = client.get("/api/sessions").get_json()
    assert [s["id"] for s in r["sessions"]] == ["20260102-0000", "20260101-0000"]


def test_list_sessions_skips_corrupt_meta(client, paths):
    d = app.SESSIONS_DIR / "20260101-0000"
    d.mkdir(parents=True)
    (d / "meta.json").write_text("not json")
    r = client.get("/api/sessions").get_json()
    assert r["ok"] and r["sessions"] == []


# --- GET/DELETE /api/sessions/<id> ---------------------------------------------

def test_get_session_detail(client, paths):
    now = time.time()
    _write_track([_ac_row(now - 30, [("4X-EHD", 32.2, 34.7, 3200, 90.0, 180.0, 8)])])
    sid = client.post("/api/sessions", json={"minutes": 5}).get_json()["id"]
    r = client.get(f"/api/sessions/{sid}").get_json()
    assert r["ok"] and r["session"]["id"] == sid


def test_get_session_detail_404_for_unknown_id(client, paths):
    assert client.get("/api/sessions/20260101-0000").status_code == 404


def test_get_session_detail_404_for_invalid_id_shape(client, paths):
    """‏SESSION_ID_RE הוא גם הגנת path traversal — מזהה לא-תקני נדחה."""
    for bad in ("..", "abc", "2026-01-01", "20260101-0000-../../etc"):
        assert client.get(f"/api/sessions/{bad}").status_code == 404


def test_delete_session_removes_everything(client, paths):
    now = time.time()
    _write_track([_ac_row(now - 30, [])])
    _mk_rec(NAME1, age=30)
    sid = client.post("/api/sessions", json={"minutes": 5}).get_json()["id"]
    sdir = app.SESSIONS_DIR / sid
    assert sdir.is_dir()
    r = client.delete(f"/api/sessions/{sid}").get_json()
    assert r["ok"]
    assert not sdir.exists()


def test_delete_unknown_session_is_404(client, paths):
    assert client.delete("/api/sessions/20260101-0000").status_code == 404


# --- GET /api/sessions/<id>/track ----------------------------------------------

def test_get_session_track_matches_saved_rows(client, paths):
    now = time.time()
    rows = [_ac_row(now - 30, [("4X-EHD", 32.2, 34.7, 3200, 90.0, 180.0, 8)])]
    _write_track(rows)
    sid = client.post("/api/sessions", json={"minutes": 5}).get_json()["id"]
    r = client.get(f"/api/sessions/{sid}/track").get_json()
    assert r["ok"] and r["rows"] == rows


def test_get_session_track_404_for_unknown_id(client, paths):
    assert client.get("/api/sessions/20260101-0000/track").status_code == 404


# --- GET /api/sessions/<id>/clips/<name> ---------------------------------------

def test_get_session_clip_serves_file(client, paths):
    now = time.time()
    _write_track([_ac_row(now - 30, [])])
    _mk_rec(NAME1, age=30, size=777)
    sid = client.post("/api/sessions", json={"minutes": 5}).get_json()["id"]
    r = client.get(f"/api/sessions/{sid}/clips/{NAME1}")
    assert r.status_code == 200 and len(r.data) == 777


def test_get_session_clip_rejects_bad_name(client, paths):
    now = time.time()
    _write_track([_ac_row(now - 30, [])])
    sid = client.post("/api/sessions", json={"minutes": 5}).get_json()["id"]
    assert client.get(f"/api/sessions/{sid}/clips/..%2F..%2Fetc%2Fpasswd").status_code in (400, 404)
    assert client.get(f"/api/sessions/{sid}/clips/not-a-recording.mp3").status_code == 404


# --- GET /api/sessions/<id>/export.zip -----------------------------------------

def test_export_zip_contains_meta_track_and_clips(client, paths):
    now = time.time()
    _write_track([_ac_row(now - 30, [("4X-EHD", 32.2, 34.7, 3200, 90.0, 180.0, 8)])])
    _mk_rec(NAME1, age=30)
    sid = client.post("/api/sessions", json={"minutes": 5}).get_json()["id"]
    r = client.get(f"/api/sessions/{sid}/export.zip")
    assert r.status_code == 200
    tmp = paths / "export.zip"
    tmp.write_bytes(r.data)
    with zipfile.ZipFile(tmp) as z:
        names = z.namelist()
        assert "meta.json" in names
        assert "track.jsonl.gz" in names
        assert f"{app.SESSION_CLIPS_DIRNAME}/{NAME1}" in names
        meta = json.loads(z.read("meta.json"))
        assert meta["id"] == sid


def test_export_zip_404_for_unknown_session(client, paths):
    assert client.get("/api/sessions/20260101-0000/export.zip").status_code == 404


# --- תמלול של קליפי סשן (שלב 3+: "התמלול תחת הנגן") --------------------------

def test_session_detail_exposes_live_transcript_from_sidecar(client, paths):
    """⚠ התמלול נקרא **חי** מה-sidecar ולא מוקפא ל-meta.json: קליפ יכול
    להתמלל *אחרי* שהסשן נשמר, וערך קפוא היה נשאר ריק לנצח."""
    now = time.time()
    _write_track([_ac_row(now - 30, [])])
    _mk_rec(NAME1, age=30)
    sid = client.post("/api/sessions", json={"minutes": 5}).get_json()["id"]
    clip = app.SESSIONS_DIR / sid / app.SESSION_CLIPS_DIRNAME / NAME1
    assert clip.is_file()
    # לפני תמלול — "לא ניסינו" (none), לא היעדר שדה
    r = client.get(f"/api/sessions/{sid}").get_json()
    assert r["session"]["clips"][0]["tx"]["state"] == "none"
    # התמלול נוצר *אחרי* השמירה — חייב להופיע בלי לגעת ב-meta.json
    app._write_tx(clip, "ok", text="cleared to land", lang="en")
    r = client.get(f"/api/sessions/{sid}").get_json()
    tx = r["session"]["clips"][0]["tx"]
    assert tx["state"] == "ok" and tx["text"] == "cleared to land"


def test_session_clips_are_queued_for_transcription(client, paths):
    """קליפ ש*הועבר* לסשן יצא מ-_iter_recordings — בלי _session_clip_paths הוא
    לא היה מתומלל לעולם (הפיצ'ר 'תמלול תחת הנגן' היה ריד תמיד)."""
    now = time.time()
    _write_track([_ac_row(now - 30, [])])
    _mk_rec(NAME1, age=30)
    sid = client.post("/api/sessions", json={"minutes": 5}).get_json()["id"]
    assert not (app.REC_DIR / NAME1).exists()      # הועבר, לא נשאר ביומן החי
    target = app._tx_next_target(auto=False)       # auto כבוי — עדיין נבחר
    assert target is not None and target.name == NAME1
    assert app.SESSION_CLIPS_DIRNAME in target.parts


def test_session_clip_pending_request_is_picked_first(client, paths):
    now = time.time()
    _write_track([_ac_row(now - 30, [])])
    _mk_rec(NAME1, age=30)
    sid = client.post("/api/sessions", json={"minutes": 5}).get_json()["id"]
    clip = app.SESSIONS_DIR / sid / app.SESSION_CLIPS_DIRNAME / NAME1
    app._write_tx(clip, "pending", lang="en")
    target = app._tx_next_target(auto=False)
    assert target is not None and target.name == NAME1


def test_saving_new_session_does_not_steal_clips_from_existing_sessions(client, paths):
    """⚠ regression guard: `_session_clip_paths` **חייב** להישאר נפרד
    מ-`_iter_recordings`. אם קליפי-סשן היו נכנסים ל-_iter_recordings, שמירת
    סשן חדש (שמשתמשת בו לבחירת קליפים) הייתה *מעבירה אליו* את הקליפים של
    הסשנים הקיימים — אובדן נתונים שקט."""
    now = time.time()
    _write_track([_ac_row(now - 60, []), _ac_row(now - 30, [])])
    _mk_rec(NAME1, age=60)
    sid1 = client.post("/api/sessions", json={"minutes": 5}).get_json()["id"]
    clip1 = app.SESSIONS_DIR / sid1 / app.SESSION_CLIPS_DIRNAME / NAME1
    assert clip1.is_file()
    # סשן שני על אותו חלון זמן — אסור שייקח את הקליפ של הראשון
    _mk_rec(NAME2, age=30)
    r2 = client.post("/api/sessions", json={"minutes": 5}).get_json()
    assert clip1.is_file(), "הקליפ של הסשן הראשון נגנב ע\"י שמירת סשן חדש"
    assert [c["file"] for c in r2["session"]["clips"]] == [NAME2]
