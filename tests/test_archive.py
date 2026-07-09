# ============================================================================
#  AIR-AM - בדיקות ארכיון החיפוש הרב-יומי (?day=YYYY-MM-DD ב-/api/acars ו-/api/vdl2)
# ----------------------------------------------------------------------------
#  קורא מהדיסק (jsonl), לא מהזיכרון — עצמאי מ"היום בלבד" ומה-ring buffer.
# ============================================================================
import time

import pytest

import app


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "ACARS_LOG_PATH", tmp_path / "acars.jsonl")
    monkeypatch.setattr(app, "VDL2_LOG_PATH", tmp_path / "vdl2.jsonl")
    return tmp_path


@pytest.fixture
def client(paths):
    return app.app.test_client()


def _day_epoch(date_str, hour=12):
    lt = time.strptime(date_str + (" %02d:00:00" % hour), "%Y-%m-%d %H:%M:%S")
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, lt.tm_hour, 0, 0, 0, 0, -1))


def test_day_bounds_valid():
    bounds = app._day_bounds("2026-07-01")
    assert bounds is not None
    start, end = bounds
    assert end - start == 86400


def test_day_bounds_rejects_bad_format():
    assert app._day_bounds("01/07/2026") is None
    assert app._day_bounds("not-a-date") is None
    assert app._day_bounds("") is None
    assert app._day_bounds(None) is None


def test_day_bounds_handles_dst_transition_days():
    """ישראל (וארה"ב) עוברות שעון קיץ/חורף => לא כל יום הוא 86400 שניות.
    end-start חייב להיות הפרש-הזמן-האמיתי (23h/25h), לא +86400 קבוע, אחרת
    שעה שלמה של הודעות נעלמת/מוכפלת בארכיון החיפוש סביב המעבר.
    משתמשים ב-America/New_York (כלל DST פשוט וקבוע) כדי שהבדיקה תהיה
    דטרמיניסטית בכל סביבת CI, בלי תלות בחוק הישראלי המשתנה."""
    import os
    old_tz = os.environ.get("TZ")
    os.environ["TZ"] = "America/New_York"
    time.tzset()
    try:
        # 2024-03-10: קפיצה קדימה לשעון קיץ (יום של 23 שעות)
        start, end = app._day_bounds("2024-03-10")
        assert end - start == 23 * 3600
        assert end == time.mktime((2024, 3, 11, 0, 0, 0, 0, 0, -1))
        # 2024-11-03: חזרה לשעון חורף (יום של 25 שעות)
        start, end = app._day_bounds("2024-11-03")
        assert end - start == 25 * 3600
        assert end == time.mktime((2024, 11, 4, 0, 0, 0, 0, 0, -1))
    finally:
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        time.tzset()


def test_api_acars_day_returns_only_that_day(client, paths):
    app._append_acars_log({"t": _day_epoch("2026-07-01"), "tail": "4X-OLD", "text": "yesterday"})
    app._append_acars_log({"t": _day_epoch("2026-07-02"), "tail": "4X-TODAY", "text": "today"})
    r = client.get("/api/acars?day=2026-07-01")
    j = r.get_json()
    assert r.status_code == 200 and j["ok"] and j["day"] == "2026-07-01"
    tails = [m["tail"] for m in j["messages"]]
    assert tails == ["4X-OLD"]


def test_api_acars_day_rejects_bad_format(client, paths):
    r = client.get("/api/acars?day=bogus")
    assert r.status_code == 400
    assert r.get_json()["ok"] is False


def test_api_acars_day_empty_when_no_data(client, paths):
    r = client.get("/api/acars?day=2026-01-01")
    j = r.get_json()
    assert j["ok"] and j["messages"] == []


def test_api_acars_day_no_cursor_or_adsb_fields(client, paths):
    """מצב ארכיון הוא snapshot סטטי — לא נושא cursor/adsb של הפיד החי."""
    app._append_acars_log({"t": _day_epoch("2026-07-01"), "tail": "4X-A"})
    j = client.get("/api/acars?day=2026-07-01").get_json()
    assert "cursor" not in j and "adsb" not in j


def test_api_acars_day_ignores_since_and_all(client, paths):
    app._append_acars_log({"t": _day_epoch("2026-07-01"), "tail": "4X-A"})
    j = client.get("/api/acars?day=2026-07-01&since=999&all=1").get_json()
    assert len(j["messages"]) == 1   # since/all לא רלוונטיים במצב day


def test_api_vdl2_day_returns_only_that_day(client, paths):
    app._append_vdl2_log({"t": _day_epoch("2026-07-01"), "tail": "4X-OLD", "icao": "AAAAAA"})
    app._append_vdl2_log({"t": _day_epoch("2026-07-02"), "tail": "4X-TODAY", "icao": "BBBBBB"})
    r = client.get("/api/vdl2?day=2026-07-01")
    j = r.get_json()
    assert r.status_code == 200 and j["ok"] and j["day"] == "2026-07-01"
    tails = [m["tail"] for m in j["messages"]]
    assert tails == ["4X-OLD"]


def test_api_vdl2_day_rejects_bad_format(client, paths):
    r = client.get("/api/vdl2?day=2026-13-40")
    assert r.status_code == 400


def test_api_acars_day_boundary_exclusive(client, paths):
    """הודעה בדיוק בתחילת היום הבא לא נכללת ביום הקודם (חצות היא הגבול, [start,end))."""
    start, end = app._day_bounds("2026-07-01")
    app._append_acars_log({"t": start, "tail": "4X-START"})
    app._append_acars_log({"t": end, "tail": "4X-NEXTDAY"})
    j = client.get("/api/acars?day=2026-07-01").get_json()
    tails = [m["tail"] for m in j["messages"]]
    assert tails == ["4X-START"]
