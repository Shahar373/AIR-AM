# ============================================================================
#  AIR-AM - בדיקות הרחבת adsb.py להצלבה: זמן מדידה, אינדקס hex, מיפוי רישום↔hex,
#  חיפוש ממוקד (cache/הגבלת קצב/fallback/timeout), trace היסטורי ו-OpenSky.
# ----------------------------------------------------------------------------
#  בלי רשת: _http_get_json (או urlopen עצמו) ממוקף. ⚠ כל תשובות הספקים כאן הן
#  fixtures שנבנו לפי הסכמה המתועדת (readsb README-json.md / OpenSky rest.rst) —
#  לא תשובות אמיתיות שנלכדו.
# ============================================================================
import gzip
import io
import json
import time
import urllib.error

import pytest

import adsb


@pytest.fixture(autouse=True)
def clean(tmp_path, monkeypatch):
    monkeypatch.setattr(adsb, "REG_HEX_PATH", tmp_path / "reg_hex.json")
    monkeypatch.setattr(adsb, "TRACK_PATH", tmp_path / "track.jsonl")
    with adsb._LOCK:
        adsb._S["aircraft"].clear()
        adsb._S["by_hex"].clear()
        adsb._S["reg_hex"].clear()
        adsb._S["reg_hex_loaded"] = True          # לא לקרוא את /var/lib בבדיקות
        adsb._S["reg_hex_dirty"] = False
        adsb._S["reg_hex_saved_mono"] = None
        adsb._S["src_idx"] = 0
    with adsb._NET_LOCK:
        for k in ("last", "window", "stats"):
            adsb._NET[k].clear()
        adsb._NET["cache"].clear()
        adsb._NET["opensky_token"] = None
    yield


AC_ZBKM = {"hex": "406f79", "r": "G-ZBKM", "t": "B789", "flight": "BAW60   ",
           "lat": 33.17, "lon": 27.66, "alt_baro": 38000, "alt_geom": 38625, "gs": 478.0,
           "track": 121.0, "nic": 8, "nac_p": 9, "rc": 186, "seen_pos": 4.5,
           "type": "adsb_icao"}


def _resp(ac_list, now_ms=1_792_763_150_000):
    return {"ac": ac_list, "msg": "No error", "now": now_ms, "total": len(ac_list)}


# --- זמן מדידה / צורת תשובה ----------------------------------------------------

def test_resp_now_units():
    assert adsb._resp_now({"now": 1_792_763_150_000}) == 1_792_763_150.0   # ms (jv2)
    assert adsb._resp_now({"now": 1_792_763_150.5}) == 1_792_763_150.5     # שניות
    t0 = time.time()
    assert adsb._resp_now({}) >= t0                                        # חסר => שעון ה-Pi


def test_ac_list_accepts_documented_and_alias_shapes():
    assert adsb._ac_list({"ac": [1]}) == [1]
    assert adsb._ac_list({"aircraft": [2]}) == [2]
    assert adsb._ac_list({"msg": "error"}) is None and adsb._ac_list([]) is None


def test_process_records_measurement_time_and_source_type():
    with adsb._LOCK:
        adsb.process([AC_ZBKM], resp_now=1_792_763_150.0)
    rec = adsb.local_aircraft(reg="G-ZBKM")
    assert rec["t_pos"] == 1_792_763_145.5          # now − seen_pos, לא זמן קבלת ה-API
    assert rec["src_type"] == "adsb_icao" and rec["nac_p"] == 9 and rec["rc"] == 186
    assert rec["alt_geom"] == 38625 and rec["alt_baro"] == 38000
    assert rec["flight"] == "BAW60"
    assert "t_mono" not in rec and rec["age"] >= 0
    # תאימות לאחור: השדות הקיימים של aircraft_snapshot לא השתנו
    snap = adsb.aircraft_snapshot({"GZBKM"})["GZBKM"]
    for k in ("reg", "hex", "flight", "type", "lat", "lon", "alt", "ground", "gs",
              "track", "nic", "spoofed", "pos_ok", "age"):
        assert k in snap


def test_spoofed_record_has_no_measurement_time():
    with adsb._LOCK:
        adsb.process([{**AC_ZBKM, "nic": 0}], resp_now=1_792_763_150.0)
    rec = adsb.local_aircraft(hex_="406F79")
    assert rec["lat"] is None and rec["t_pos"] is None and rec["spoofed"]


def test_by_hex_includes_aircraft_without_registration():
    """מטוס בלי r (צבאי/לא במאגר) לא נכנס ל-aircraft (הרישום הוא המפתח שם) אבל
    כן לאינדקס ה-hex — VDL2/SATCOM נושאים כתובת ICAO מבנית."""
    noreg = {k: v for k, v in AC_ZBKM.items() if k != "r"}
    noreg["hex"] = "4b8e01"
    with adsb._LOCK:
        adsb.process([noreg])
    assert adsb.aircraft_snapshot() == {}
    assert adsb.local_aircraft(hex_="4B8E01")["reg"] is None
    assert [a["hex"] for a in adsb.all_aircraft()] == ["4b8e01"]
    with adsb._LOCK:                                # גיזום חל גם על האינדקס החדש
        adsb.process([], time.monotonic() + adsb.AC_KEEP_SEC + 5)
    assert adsb.all_aircraft() == []


def test_non_icao_hex_not_indexed():
    with adsb._LOCK:
        adsb.process([{**AC_ZBKM, "hex": "~0abc12", "r": None}])
    assert adsb.all_aircraft() == []


# --- מיפוי רישום↔hex ----------------------------------------------------------------

def test_reg_hex_learned_persisted_and_reloaded(tmp_path):
    with adsb._LOCK:
        adsb.process([AC_ZBKM])
    assert adsb.reg_to_hex(".G-ZBKM") == "406F79"     # ריפוד acarsdec
    adsb._save_reg_hex(force=True)
    saved = json.loads((tmp_path / "reg_hex.json").read_text())
    assert saved == {"GZBKM": "406F79"}
    # "עלייה" חדשה: המטוס כבר לא בטווח, המיפוי נטען מהדיסק
    with adsb._LOCK:
        adsb._S["aircraft"].clear()
        adsb._S["by_hex"].clear()
        adsb._S["reg_hex"].clear()
        adsb._S["reg_hex_loaded"] = False
    assert adsb.reg_to_hex("G-ZBKM") == "406F79"


def test_reg_hex_save_is_throttled_and_only_when_dirty(tmp_path):
    adsb._save_reg_hex()
    assert not (tmp_path / "reg_hex.json").exists()   # לא השתנה => לא נכתב
    with adsb._LOCK:
        adsb.process([AC_ZBKM])
    adsb._save_reg_hex()
    assert (tmp_path / "reg_hex.json").exists()
    with adsb._LOCK:
        adsb.process([{**AC_ZBKM, "hex": "406f7a", "r": "G-ZBKN"}])
    adsb._save_reg_hex()                               # בתוך REG_HEX_SAVE_SEC => לא עכשיו
    assert "GZBKN" not in json.loads((tmp_path / "reg_hex.json").read_text())


def test_reg_hex_corrupt_file_is_ignored(tmp_path):
    (tmp_path / "reg_hex.json").write_text("{not json")
    with adsb._LOCK:
        adsb._S["reg_hex_loaded"] = False
    assert adsb.reg_to_hex("G-ZBKM") is None


def test_reg_hex_lru_cap(monkeypatch):
    monkeypatch.setattr(adsb, "REG_HEX_MAX", 2)
    with adsb._LOCK:
        for i, reg in enumerate(("AAA1", "BBB2", "CCC3")):
            adsb._learn_reg_hex(reg, f"40000{i}")
        assert list(adsb._S["reg_hex"]) == ["BBB2", "CCC3"]


# --- lookup ממוקד ---------------------------------------------------------------------

def test_lookup_by_reg_builds_url_and_caches(monkeypatch):
    calls = []

    def fake(url, timeout, max_bytes, headers=None):
        calls.append(url)
        return _resp([AC_ZBKM])

    monkeypatch.setattr(adsb, "_http_get_json", fake)
    r = adsb.lookup("reg", ".G-ZBKM")
    assert r["status"] == "ok" and r["provider"] == "adsb.lol"
    assert calls == ["https://api.adsb.lol/v2/reg/G-ZBKM"]      # הנקודה הוסרה, המקף נשמר
    rec = r["aircraft"][0]
    assert rec["t_pos"] == 1_792_763_145.5 and rec["provider"] == "adsb.lol"
    assert adsb.reg_to_hex("G-ZBKM") == "406F79"                  # נלמד גם מ-lookup
    again = adsb.lookup("reg", "G-ZBKM")
    assert again["cached"] and len(calls) == 1                    # cache — בלי בקשה נוספת


def test_lookup_miss_is_cached_longer(monkeypatch):
    calls = []
    monkeypatch.setattr(adsb, "_http_get_json",
                        lambda url, *a, **k: calls.append(url) or _resp([]))
    assert adsb.lookup("hex", "406F79")["status"] == "miss"
    assert calls == ["https://api.adsb.lol/v2/hex/406f79"]
    assert adsb.lookup("hex", "406f79")["status"] == "miss" and len(calls) == 1


def test_lookup_falls_back_to_second_provider(monkeypatch):
    calls = []

    def fake(url, *a, **k):
        calls.append(url)
        if "adsb.lol" in url:
            raise TimeoutError("timed out")
        return _resp([AC_ZBKM])

    monkeypatch.setattr(adsb, "_http_get_json", fake)
    r = adsb.lookup("reg", "G-ZBKM")
    assert r["status"] == "ok" and r["provider"] == "adsb.fi"
    assert calls[1] == "https://opendata.adsb.fi/api/v2/registration/G-ZBKM"
    assert any("timed out" in e for e in r["errors"])
    st = adsb.net_status()["providers"]
    assert st["adsb.lol"]["fail"] == 1 and st["adsb.fi"]["ok"] == 1


def test_lookup_rate_limited_without_network(monkeypatch):
    calls = []
    monkeypatch.setattr(adsb, "_http_get_json", lambda url, *a, **k: calls.append(url) or _resp([]))
    adsb.lookup("hex", "406f79")
    adsb.lookup("hex", "406f7a")       # adsb.lol חסום (2ש') => adsb.fi
    r = adsb.lookup("hex", "406f7b")   # שניהם חסומים
    assert r["status"] == "rate_limited" and len(calls) == 2


def test_lookup_bad_shape_is_error_not_miss(monkeypatch):
    monkeypatch.setattr(adsb, "_http_get_json", lambda *a, **k: {"msg": "error"})
    r = adsb.lookup("hex", "406f79")
    assert r["status"] == "error" and r["aircraft"] == []


@pytest.mark.parametrize("kind,value", [("hex", "XYZ"), ("reg", "G ZB/KM"), ("nope", "1"), ("reg", "")])
def test_lookup_invalid_input(kind, value, monkeypatch):
    monkeypatch.setattr(adsb, "_http_get_json", lambda *a, **k: pytest.fail("no network"))
    assert adsb.lookup(kind, value)["status"] == "invalid"


# --- _http_get_json: gzip + חסם גודל ----------------------------------------------

class _FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_http_get_json_gzip_and_limits(monkeypatch):
    payload = json.dumps({"ok": 1}).encode()
    monkeypatch.setattr(adsb.urllib.request, "urlopen",
                        lambda req, timeout: _FakeResp(gzip.compress(payload)))
    assert adsb._http_get_json("https://x.test/a", 1, 1000) == {"ok": 1}
    big = gzip.compress(b"[" + b"0," * 5000 + b"0]")
    monkeypatch.setattr(adsb.urllib.request, "urlopen", lambda req, timeout: _FakeResp(big))
    with pytest.raises(ValueError):
        adsb._http_get_json("https://x.test/a", 1, 2000)          # פצצת gzip


def test_http_get_json_status_code(monkeypatch):
    def boom(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, None)
    monkeypatch.setattr(adsb.urllib.request, "urlopen", boom)
    with pytest.raises(adsb._HTTPStatus) as ei:
        adsb._http_get_json("https://x.test/a", 1, 1000)
    assert ei.value.code == 404


# --- trace היסטורי -------------------------------------------------------------------

T0 = 1_792_760_000.0
TRACE = {"icao": "406f79", "r": "G-ZBKM", "t": "B789", "timestamp": T0,
         "trace": [
             [0.0, 32.5, 29.0, 37000, 470.0, 120.0, 0, 0, {"nic": 8, "nac_p": 9}, "adsb_icao", 37600],
             [3100.0, 33.18, 27.64, 38000, 478.0, 121.0, 0, 0, None, "mlat", None],
             [3190.0, 33.10, 27.80, 38000, 478.0, 121.0, 8, 0, None, "adsb_icao", None],
             [3200.0, None, None, "ground", 0.0, 0.0, 0, 0, None, None, None],
             "garbage",
         ]}


def test_parse_trace_fields_and_flags():
    pts, meta = adsb.parse_trace(TRACE)
    assert meta == {"icao": "406f79", "r": "G-ZBKM", "t": "B789"}
    assert len(pts) == 4
    a, b, c, g = pts
    assert a["t"] == T0 and a["nic"] == 8 and a["nac_p"] == 9 and a["alt_geom"] == 37600
    assert a["src_type"] == "adsb_icao" and a["origin"] == "trace"
    assert b["src_type"] == "mlat" and b["nic"] is None          # חסר = לא ידוע
    assert c["alt_geom"] == 38000 and c["alt_baro"] is None      # flags&8 => גאומטרי
    assert g["alt_baro"] is None and g["lat"] is None
    with pytest.raises(ValueError):
        adsb.parse_trace({"trace": []})


def test_fetch_trace_live_today(monkeypatch):
    calls = []
    now = T0 + 3200
    monkeypatch.setattr(adsb.time, "time", lambda: now)
    monkeypatch.setattr(adsb, "_http_get_json", lambda url, *a, **k: calls.append(url) or TRACE)
    r = adsb.fetch_trace("406F79", T0 + 3150)
    assert r["status"] == "ok" and r["covers"] and r["kind"] == "live"
    assert calls == ["https://globe.adsb.lol/data/traces/79/trace_full_406f79.json"]
    assert r["t_first"] == T0 and r["t_last"] == T0 + 3200
    assert adsb.fetch_trace("406F79", T0 + 3150)["status"] == "ok" and len(calls) == 1   # cache


def test_fetch_trace_older_day_uses_history_path(monkeypatch):
    calls = []
    monkeypatch.setattr(adsb.time, "time", lambda: T0 + 5 * 86400)

    def fake(url, *a, **k):
        calls.append(url)
        raise adsb._HTTPStatus(404)

    monkeypatch.setattr(adsb, "_http_get_json", fake)
    r = adsb.fetch_trace("406f79", T0)
    day = time.strftime("%Y/%m/%d", time.gmtime(T0))
    assert calls == [f"https://globe.adsb.lol/globe_history/{day}/traces/79/trace_full_406f79.json"]
    assert r["status"] == "miss" and r["points"] == []


def test_fetch_trace_network_error_is_error(monkeypatch):
    monkeypatch.setattr(adsb.time, "time", lambda: T0 + 100)
    monkeypatch.setattr(adsb, "_http_get_json", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    r = adsb.fetch_trace("406f79", T0)
    assert r["status"] == "error" and "down" in r["errors"][0]
    assert adsb.fetch_trace("zz", T0)["status"] == "invalid"


# --- OpenSky -----------------------------------------------------------------------

def test_opensky_track_parses_meters_and_marks_source(monkeypatch):
    monkeypatch.delenv("AIRAM_OPENSKY_CLIENT_ID", raising=False)
    path = {"icao24": "406f79", "callsign": "BAW60  ", "startTime": T0, "endTime": T0 + 7200,
            "path": [[T0, 32.5, 29.0, 11582.0, 120.0, False], [T0 + 900, 33.0, 28.0, 11582.0, 121.0, False]]}
    seen = {}

    def fake(url, timeout, max_bytes, headers=None):
        seen["url"], seen["headers"] = url, headers
        return path

    monkeypatch.setattr(adsb, "_http_get_json", fake)
    monkeypatch.setattr(adsb.time, "time", lambda: T0 + 3600)
    r = adsb.fetch_opensky_track("406F79", T0 + 300)
    assert r["status"] == "ok" and r["covers"] and not r["authenticated"]
    assert seen["headers"] is None                                  # אנונימי בלי פרטים
    assert seen["url"].endswith("icao24=406f79&time=1792760300")
    assert abs(r["points"][0]["alt_baro"] - 11582.0 / 0.3048) < 1e-6    # מטרים => רגל
    assert r["points"][0]["src_type"] == "opensky_track" and r["points"][0]["gs"] is None
    assert r["meta"]["callsign"] == "BAW60"


def test_opensky_auth_required_and_out_of_range(monkeypatch):
    monkeypatch.setattr(adsb.time, "time", lambda: T0 + 3600)
    monkeypatch.setattr(adsb, "_http_get_json", lambda *a, **k: (_ for _ in ()).throw(adsb._HTTPStatus(401)))
    assert adsb.fetch_opensky_track("406f79", T0)["status"] == "auth_required"
    assert adsb.fetch_opensky_track("406f79", T0 - 31 * 86400)["status"] == "out_of_range"


def test_opensky_uses_bearer_token_when_configured(monkeypatch):
    monkeypatch.setenv("AIRAM_OPENSKY_CLIENT_ID", "cid")
    monkeypatch.setenv("AIRAM_OPENSKY_CLIENT_SECRET", "sec")
    monkeypatch.setattr(adsb.time, "time", lambda: T0 + 3600)
    posted = {}

    def fake_urlopen(req, timeout):
        posted["url"], posted["body"] = req.full_url, req.data
        return _FakeResp(json.dumps({"access_token": "tok", "expires_in": 1800}).encode())

    monkeypatch.setattr(adsb.urllib.request, "urlopen", fake_urlopen)
    seen = {}
    monkeypatch.setattr(adsb, "_http_get_json",
                        lambda url, t, m, headers=None: seen.update(h=headers) or {"path": []})
    r = adsb.fetch_opensky_track("406f79", T0)
    assert posted["url"] == adsb.OPENSKY_TOKEN_URL and b"grant_type=client_credentials" in posted["body"]
    assert seen["h"] == {"Authorization": "Bearer tok"} and r["authenticated"]
    assert adsb.net_status()["opensky_credentials"] is True
