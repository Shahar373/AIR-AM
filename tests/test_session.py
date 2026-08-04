# ============================================================================
#  AIR-AM - בדיקות יחידה לדוח הסשן (/api/session, /api/session/ack) ולציון
#  המעניינוּת (_interest_score) — ר' docs/field-station-roadmap.md.
# ----------------------------------------------------------------------------
#  קורא מהדיסק (jsonl), לא מהזיכרון — כמו ?day= בארכיון. רץ בלי חומרה.
# ============================================================================
import time

import pytest

import app
import adsb


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(app, "ACARS_LOG_PATH", tmp_path / "acars.jsonl")
    monkeypatch.setattr(app, "VDL2_LOG_PATH", tmp_path / "vdl2.jsonl")
    monkeypatch.setattr(app, "SATCOM_LOG_PATH", tmp_path / "satcom.jsonl")
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
def _clean_adsb_series():
    with adsb._LOCK:
        adsb._S["session_series"].clear()
    yield
    with adsb._LOCK:
        adsb._S["session_series"].clear()


def _msg(t, tail=None, label=None, group="comm", decoded=None, pos_src=None):
    return {"t": t, "tail": tail, "flight": None, "icao": None, "label": label,
            "group": group, "category": "x", "decoded": decoded, "pos_src": pos_src,
            "notable": app._interest_score({"group": group, "decoded": decoded,
                                            "pos_src": pos_src, "label": label})}


# --- _interest_score ---------------------------------------------------------

def test_interest_score_generic_group_not_notable():
    assert app._interest_score({"group": "comm", "decoded": None, "pos_src": None, "label": None}) is False
    assert app._interest_score({"group": "text", "decoded": None, "pos_src": None, "label": None}) is False


def test_interest_score_real_category_is_notable():
    assert app._interest_score({"group": "oooi", "decoded": None, "pos_src": None, "label": None}) is True


def test_interest_score_decoded_text_is_notable():
    assert app._interest_score({"group": "comm", "decoded": "המראה 26", "pos_src": None, "label": None}) is True


def test_interest_score_adsc_position_is_notable():
    assert app._interest_score({"group": "text", "decoded": None, "pos_src": "adsc", "label": None}) is True


def test_interest_score_rich_label_is_notable():
    assert app._interest_score({"group": "clearance", "decoded": None, "pos_src": None, "label": "A3"}) is True


def test_normalize_acars_includes_notable_field():
    n = app._normalize_acars({"timestamp": 1.0, "label": "A3", "tail": "4X-A", "text": "PDC"})
    assert n["notable"] is True
    ack = app._normalize_acars({"timestamp": 1.0, "label": "Q0"})
    assert ack["notable"] is False


# --- GET /api/session ---------------------------------------------------------

def test_session_no_marker_defaults_to_one_hour_back(client, paths):
    now = time.time()
    app._append_acars_log(_msg(now - 30 * 60, tail="4X-A", group="oooi"))          # בתוך השעה
    app._append_acars_log(_msg(now - 2 * 3600, tail="4X-B", group="oooi"))         # מחוץ לחלון (ישן מדי)
    r = client.get("/api/session")
    data = r.get_json()
    assert data["ok"] is True
    assert data["counts"]["acars"] == 1
    assert data["duration_sec"] == pytest.approx(3600, abs=2)


def test_session_explicit_since_overrides_stored_marker(client, paths):
    now = time.time()
    st = app.load_state()
    st["last_session_view_at"] = now - 10  # אם היה נלקח, החלון היה כמעט ריק
    app.save_state(st)
    app._append_acars_log(_msg(now - 1800, tail="4X-A", group="oooi"))
    r = client.get("/api/session?since=" + str(now - 3600))
    data = r.get_json()
    assert data["counts"]["acars"] == 1


def test_session_new_vs_known_aircraft(client, paths):
    now = time.time()
    since = now - 3600
    # 4X-OLD כבר נראה *לפני* החלון (לא "חדש"); 4X-NEW מופיע לראשונה בתוך החלון.
    app._append_acars_log(_msg(since - 100, tail="4X-OLD", group="oooi"))
    app._append_acars_log(_msg(since + 100, tail="4X-OLD", group="oooi"))
    app._append_acars_log(_msg(since + 200, tail="4X-NEW", group="oooi"))
    r = client.get("/api/session?since=" + str(since))
    data = r.get_json()
    assert data["aircraft_count"] == 2       # שני זנבות נראו בתוך החלון
    assert data["new_aircraft_count"] == 1   # רק 4X-NEW לא נראה קודם


def test_session_highlights_only_notable_sorted_newest_first_and_capped(client, paths):
    now = time.time()
    since = now - 3600
    for i in range(3):
        app._append_acars_log(_msg(since + 10 + i, tail="4X-ACK"))                       # comm, לא notable
    for i in range(12):
        app._append_acars_log(_msg(since + 100 + i, tail=f"4X-N{i}", group="oooi"))      # notable
    r = client.get("/api/session?since=" + str(since))
    data = r.get_json()
    assert len(data["highlights"]) == app.SESSION_HIGHLIGHTS_MAX
    ts = [h["t"] for h in data["highlights"]]
    assert ts == sorted(ts, reverse=True)
    assert all(h["mode"] == "acars" for h in data["highlights"])


def test_session_counts_split_by_mode(client, paths):
    now = time.time()
    since = now - 3600
    app._append_acars_log(_msg(since + 10, tail="4X-A", group="oooi"))
    app._append_vdl2_log(_msg(since + 10, tail="4X-B", group="oooi"))
    app._append_satcom_log(_msg(since + 10, tail="4X-C", group="oooi"))
    r = client.get("/api/session?since=" + str(since))
    data = r.get_json()
    assert data["counts"] == {"acars": 1, "vdl2": 1, "satcom": 1}
    assert data["total"] == 3


def test_session_future_clock_does_not_produce_negative_window(client, paths):
    st = app.load_state()
    st["last_session_view_at"] = time.time() + 10_000   # שעון שהוזז אחורה בפועל (הסמן "בעתיד")
    app.save_state(st)
    r = client.get("/api/session")
    data = r.get_json()
    assert data["ok"] is True
    assert data["duration_sec"] >= 0


def test_session_ack_advances_marker(client, paths):
    assert app.load_state()["last_session_view_at"] is None
    r = client.post("/api/session/ack")
    assert r.get_json()["ok"] is True
    marker = app.load_state()["last_session_view_at"]
    assert marker is not None
    assert abs(marker - time.time()) < 2

    # אחרי ה-ack, דוח חדש (בלי since מפורש) מתחיל כמעט מ"עכשיו" — לא מהשעה שעברה
    r2 = client.get("/api/session")
    assert r2.get_json()["duration_sec"] < 2


# --- adsb.session_series ------------------------------------------------------

def test_adsb_session_series_filters_by_since_and_never_raises():
    now = time.time()
    with adsb._LOCK:
        adsb._S["session_series"].append((now - 100, 0.1, "26"))
        adsb._S["session_series"].append((now - 10, 0.5, "30"))
    all_series = adsb.session_series()
    assert len(all_series) == 2
    recent = adsb.session_series(since=now - 50)
    assert len(recent) == 1
    assert recent[0]["runway"] == "30"


def test_adsb_session_series_bounded_length():
    with adsb._LOCK:
        adsb._S["session_series"].clear()
        for i in range(adsb.SESSION_SERIES_MAX + 50):
            adsb._S["session_series"].append((float(i), None, None))
    assert len(adsb.session_series()) == adsb.SESSION_SERIES_MAX
