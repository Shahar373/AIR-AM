# ============================================================================
#  AIR-AM - בדיקות ל-buffer המתגלגל של שחזור-סשן (שלב 1, docs/session-replay-design.md)
# ----------------------------------------------------------------------------
#  רץ בלי רשת ובלי חומרה: adsb.process() מוזן fixtures ישירות, track.jsonl
#  ממוקף ל-tmp_path. שני החלקים (adsb.py כותב, app.py קורא) נבדקים יחד —
#  אותו דפוס כמו test_adsb_enrich.py.
# ============================================================================
import json
import time

import pytest

import adsb
import app


NAME = "airam_20260817_120000_134600000.mp3"


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(adsb, "TRACK_PATH", tmp_path / "track.jsonl")
    monkeypatch.setattr(app, "REC_DIR", tmp_path / "recordings")
    (tmp_path / "recordings").mkdir()
    return tmp_path


@pytest.fixture
def client(paths):
    return app.app.test_client()


@pytest.fixture(autouse=True)
def _clean_adsb_state():
    """‏_S הוא מצב גלובלי (module-level) — בלי איפוס, בדיקה שממלאת aircraft/
    track_appends הייתה מדליפה לבדיקה הבאה."""
    with adsb._LOCK:
        adsb._S["aircraft"].clear()
        adsb._S["track_appends"] = 0
    yield
    with adsb._LOCK:
        adsb._S["aircraft"].clear()
        adsb._S["track_appends"] = 0


def _feed_aircraft(regs_and_pos):
    """ממלא _S['aircraft'] ישירות (בלי HTTP) — (reg, lat, lon, nic) לכל מטוס.
    nic=None משאיר גם lat/lon=None (משבש), כמו process() האמיתי."""
    with adsb._LOCK:
        for reg, lat, lon, nic in regs_and_pos:
            adsb._S["aircraft"][reg] = {
                "reg": reg, "hex": "abc123", "flight": None, "type": None,
                "lat": lat, "lon": lon, "alt": 3200.0, "ground": False,
                "gs": 180.0, "track": 90.0, "nic": nic,
                "spoofed": nic is not None and nic < adsb.SPOOF_NIC,
                "pos_ok": lat is not None,
            }


def _lines(path):
    return [json.loads(ln) for ln in path.read_text().splitlines()]


# --- _build_track_row / _append_track ----------------------------------------

def test_append_track_writes_ac_row(paths):
    _feed_aircraft([("4X-EHD", 32.2, 34.7, 8)])
    with adsb._LOCK:
        row = adsb._build_track_row()
    adsb._append_track(row)
    rows = _lines(adsb.TRACK_PATH)
    assert len(rows) == 1 and "ac" in rows[0]
    assert rows[0]["ac"] == [["4X-EHD", 32.2, 34.7, 3200, 90.0, 180.0, 8]]


def test_track_row_keeps_spoofed_aircraft_with_null_position(paths):
    """⚠ §7.1 בתכנון: lat/lon=None נשמר *במפורש*, לא מדלגים על המטוס — מבדיל
    'לא נראה' מ'נראה, מיקום משובש'. גובה/מהירות/כיוון עדיין נכתבים."""
    _feed_aircraft([("4X-EKS", None, None, 0)])
    with adsb._LOCK:
        row = adsb._build_track_row()
    ac = row["ac"][0]
    assert ac[0] == "4X-EKS" and ac[1] is None and ac[2] is None
    assert ac[3] == 3200 and ac[4] == 90.0 and ac[5] == 180.0 and ac[6] == 0


def test_append_track_rounds_position_for_compactness(paths):
    _feed_aircraft([("4X-ABC", 32.123456789, 34.987654321, 9)])
    with adsb._LOCK:
        row = adsb._build_track_row()
    adsb._append_track(row)
    ac = _lines(adsb.TRACK_PATH)[0]["ac"][0]
    assert ac[1] == round(32.123456789, 5) and ac[2] == round(34.987654321, 5)


def test_append_track_survives_disk_write_failure(paths, monkeypatch):
    """כשל כתיבה (דיסק מלא) לא אמור להפיל את ה-poll — buffer אפמרי."""
    def boom(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(adsb, "open", boom, raising=False)
    import builtins
    monkeypatch.setattr(builtins, "open", boom)
    adsb._append_track({"t": time.time(), "ac": []})   # לא זורק


# --- gap rows (_poll_once על כשל fetch) --------------------------------------

def test_poll_once_writes_gap_row_on_fetch_failure(paths, monkeypatch):
    def boom(src_idx):
        raise TimeoutError("no route to host")
    monkeypatch.setattr(adsb, "_fetch", boom)
    adsb._poll_once()
    rows = _lines(adsb.TRACK_PATH)
    assert len(rows) == 1 and rows[0]["gap"] == "no_adsb"
    assert "no route to host" in rows[0]["detail"]


def test_poll_once_writes_ac_row_on_success(paths, monkeypatch):
    def fake_fetch(src_idx):
        return "adsb.lol", {"ac": [
            {"hex": "738065", "r": "4X-EHD", "lat": 32.2, "lon": 34.7,
             "alt_baro": 12000, "gs": 320.0, "track": 290.0, "nic": 8, "seen_pos": 3},
        ]}
    monkeypatch.setattr(adsb, "_fetch", fake_fetch)
    adsb._poll_once()
    rows = _lines(adsb.TRACK_PATH)
    assert len(rows) == 1 and "ac" in rows[0]
    assert rows[0]["ac"][0][0] == "4XEHD"   # norm_reg, ר' process()


# --- _compact_track -----------------------------------------------------------

def test_compact_track_drops_rows_older_than_buffer_window(paths):
    now = time.time()
    old = now - (adsb.TRACK_BUFFER_MIN + 5) * 60
    adsb.TRACK_PATH.write_text(
        json.dumps({"t": old, "ac": []}) + "\n" +
        json.dumps({"t": now, "ac": []}) + "\n")
    adsb._compact_track()
    rows = _lines(adsb.TRACK_PATH)
    assert len(rows) == 1 and rows[0]["t"] == now


def test_compact_track_skips_corrupt_lines_without_crashing(paths):
    now = time.time()
    adsb.TRACK_PATH.write_text(
        '{"t": ' + str(now) + ', "ac": []}\n'
        'not valid json at all\n'
        '{"broken json\n')
    adsb._compact_track()   # לא זורק
    rows = _lines(adsb.TRACK_PATH)
    assert len(rows) == 1 and rows[0]["t"] == now


def test_compact_track_missing_file_is_noop(paths):
    adsb._compact_track()   # אין קובץ בכלל — לא זורק, לא יוצר קובץ ריק
    assert not adsb.TRACK_PATH.exists()


def test_append_track_triggers_compaction_after_threshold(paths, monkeypatch):
    monkeypatch.setattr(adsb, "TRACK_COMPACT_EVERY", 3)
    old = time.time() - (adsb.TRACK_BUFFER_MIN + 5) * 60
    adsb.TRACK_PATH.write_text(json.dumps({"t": old, "ac": []}) + "\n")
    for _ in range(3):
        adsb._append_track({"t": time.time(), "ac": []})
    # ה-compaction ה-3 קרה => השורה הישנה נגזמה, המונה התאפס
    rows = _lines(adsb.TRACK_PATH)
    assert all(r["t"] != old for r in rows)
    assert adsb._S["track_appends"] == 0


# --- read_track_buffer ---------------------------------------------------------

def test_read_track_buffer_missing_file(paths):
    buf = adsb.read_track_buffer()
    assert buf == {"t_oldest": None, "samples": 0, "gaps": []}


def test_read_track_buffer_reports_oldest_samples_and_gaps(paths):
    t1, t2, t3 = time.time() - 120, time.time() - 60, time.time()
    adsb.TRACK_PATH.write_text(
        json.dumps({"t": t1, "ac": []}) + "\n" +
        json.dumps({"t": t2, "gap": "no_adsb", "detail": "timeout"}) + "\n" +
        json.dumps({"t": t3, "ac": []}) + "\n")
    buf = adsb.read_track_buffer()
    assert buf["t_oldest"] == t1
    assert buf["samples"] == 2                # שתי שורות ac, לא שורת ה-gap
    assert buf["gaps"] == [{"t": t2, "reason": "no_adsb", "detail": "timeout"}]


def test_read_track_buffer_skips_corrupt_lines(paths):
    now = time.time()
    adsb.TRACK_PATH.write_text(
        '{"t": ' + str(now) + ', "ac": []}\n'
        'garbage\n')
    buf = adsb.read_track_buffer()
    assert buf["t_oldest"] == now and buf["samples"] == 1


# --- GET /api/replay/buffer (app.py) -------------------------------------------

def test_api_replay_buffer_empty(client, paths):
    r = client.get("/api/replay/buffer").get_json()
    assert r["ok"] and r["t_oldest"] is None and r["samples"] == 0
    assert r["clips_available"] is False and r["gaps"] == []


def test_api_replay_buffer_reports_samples(client, paths):
    t_oldest = time.time() - 300
    adsb.TRACK_PATH.write_text(json.dumps({"t": t_oldest, "ac": []}) + "\n")
    r = client.get("/api/replay/buffer").get_json()
    assert r["t_oldest"] == t_oldest and r["samples"] == 1


def test_api_replay_buffer_clips_available_when_recording_within_window(client, paths):
    t_oldest = time.time() - 300
    adsb.TRACK_PATH.write_text(json.dumps({"t": t_oldest, "ac": []}) + "\n")
    p = app.REC_DIR / NAME
    p.write_bytes(b"\0" * 1000)   # mtime = עכשיו, אחרי t_oldest
    r = client.get("/api/replay/buffer").get_json()
    assert r["clips_available"] is True


def test_api_replay_buffer_clips_unavailable_when_recording_older_than_buffer(client, paths):
    import os
    t_oldest = time.time() - 60
    adsb.TRACK_PATH.write_text(json.dumps({"t": t_oldest, "ac": []}) + "\n")
    p = app.REC_DIR / NAME
    p.write_bytes(b"\0" * 1000)
    old = time.time() - 3600
    os.utime(p, (old, old))       # ההקלטה ישנה מ-t_oldest של הבאפר
    r = client.get("/api/replay/buffer").get_json()
    assert r["clips_available"] is False


def test_api_replay_buffer_includes_gap_entries(client, paths):
    t = time.time()
    adsb.TRACK_PATH.write_text(
        json.dumps({"t": t, "gap": "no_adsb", "detail": "adsb.lol: timeout"}) + "\n")
    r = client.get("/api/replay/buffer").get_json()
    assert r["gaps"] == [{"t": t, "reason": "no_adsb", "detail": "adsb.lol: timeout"}]
    assert r["samples"] == 0
