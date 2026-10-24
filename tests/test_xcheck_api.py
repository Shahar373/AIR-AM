# ============================================================================
#  AIR-AM - בדיקות שילוב ההצלבה ב-app.py: תחנה, תור/worker, "אמת מול ADS-B",
#  שמירת ראיות, סינון וייצוא — מקרה הבדיקה: G-ZBKM ‏(23.10.2026, ‏131.825).
# ----------------------------------------------------------------------------
#  בלי רשת ובלי חומרה: adsb.lookup/fetch_trace/fetch_opensky_track ממוקפים,
#  ו-_http_get_json חסום (כל ניסיון רשת לא-ממוקף מכשיל). ⚠ מסלולי ה-ADS-B כאן
#  *סינתטיים* — בודקים את הצנרת, לא טוענים דבר על המטוס האמיתי.
# ============================================================================
import calendar
import json
import math
import time

import pytest

import adsb
import app
import xcheck

T_RX = calendar.timegm((2026, 10, 23, 13, 45, 40)) + 0.0
ZBKM_LAT, ZBKM_LON = 33.17888, 27.65087
STATION = {"lat": 31.968, "lon": 34.935, "alt_m": 150.0, "name": "תל חדיד (משוער)"}
B6_TEXT = "/QUKAXBA.ADS.G-ZBKM071798089D4D8946EACD9F84D7"
# JSON בסגנון libacars (מפתחות מ-adsc.c) — הערכים מהפענוח של ההודעה האמיתית
LIBACARS = {"arinc622": {"msg_type": "adsc_msg", "crc_ok": True, "gs_addr": "QUKAXBA",
                         "air_addr": ".G-ZBKM",
                         "adsc": {"err": False, "tags": [{"basic_report": {
                             "lat": 33.17888, "lon": 27.65087, "alt": 37996, "ts_sec": 2739.375,
                             "pos_accuracy_nm": 0.05, "nav_redundancy": True, "tcas_avail": True}}]}}}


def _raw_b6(**kw):
    return {"timestamp": T_RX, "freq": 131.825, "level": -46.9, "label": "B6",
            "tail": ".G-ZBKM", "flight": "BA0060", "error": 0, "text": B6_TEXT,
            "libacars": LIBACARS, **kw}


def _dest(lat, lon, brg_deg, km):
    d = km / xcheck.R_EARTH_KM
    p1, l1, b = math.radians(lat), math.radians(lon), math.radians(brg_deg)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
    l2 = l1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), math.degrees(l2)


def _synthetic(lat0, lon0, t0, times, brg=120.0, gs=480.0, src_type="adsb_icao"):
    pts = []
    for dt in times:
        km = gs * xcheck.KT_KMS * dt
        la, lo = _dest(lat0, lon0, brg if km >= 0 else brg + 180, abs(km))
        pts.append(adsb.make_point(t0 + dt, la, lo, alt_baro=38000, alt_geom=38600, gs=gs,
                                   track=brg, src_type=src_type, nic=8, nac_p=9,
                                   provider="adsb.lol", origin="trace"))
    return pts


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    for name, fname in (("STATE_PATH", "state.json"), ("ACARS_LOG_PATH", "acars.jsonl"),
                        ("VDL2_LOG_PATH", "vdl2.jsonl"), ("SATCOM_LOG_PATH", "satcom.jsonl"),
                        ("XC_EVENTS_PATH", "xcheck_events.jsonl"), ("CONFIG_PATH", "airband.conf")):
        monkeypatch.setattr(app, name, tmp_path / fname)
    monkeypatch.setattr(adsb, "TRACK_PATH", tmp_path / "track.jsonl")
    monkeypatch.setattr(adsb, "REG_HEX_PATH", tmp_path / "reg_hex.json")
    monkeypatch.setattr(adsb, "_http_get_json",
                        lambda *a, **k: pytest.fail("ניסיון רשת לא-ממוקף בבדיקה"))
    monkeypatch.setattr(app, "_run_libacars", lambda *a, **k: None)
    with adsb._LOCK:
        adsb._S["aircraft"].clear()
        adsb._S["by_hex"].clear()
        adsb._S["reg_hex"].clear()
        adsb._S["reg_hex_loaded"] = True
    for lock, buf in ((app._acars_lock, app._acars_msgs), (app._vdl2_lock, app._vdl2_msgs),
                      (app._satcom_lock, app._satcom_msgs)):
        with lock:
            buf.clear()
    with app._xc_cv:
        app._xc_heap.clear()
        for k in app._xc_stats:
            app._xc_stats[k] = 0
    with app._xc_res_lock:
        app._xc_results.clear()
    yield tmp_path


@pytest.fixture
def client():
    return app.app.test_client()


def _set_station(client, st=STATION):
    r = client.post("/api/station", json=st)
    assert r.status_code == 200, r.get_json()
    return r.get_json()["station"]


def _store_jsonl(rec):
    app._append_acars_log(rec)


def _mock_net(monkeypatch, lookup=None, trace=None, opensky=None):
    calls = {"lookup": [], "trace": [], "opensky": []}

    def lk(kind, value):
        calls["lookup"].append((kind, value))
        return lookup(kind, value) if lookup else {"status": "error", "aircraft": [], "errors": ["down"]}

    def tr(hx, t):
        calls["trace"].append((hx, t))
        return trace(hx, t) if trace else {"status": "error", "points": [], "errors": ["down"]}

    def osk(hx, t):
        calls["opensky"].append((hx, t))
        return opensky(hx, t) if opensky else {"status": "auth_required", "points": [],
                                               "errors": ["OpenSky: נדרשים פרטי OAuth2"]}

    monkeypatch.setattr(adsb, "lookup", lk)
    monkeypatch.setattr(adsb, "fetch_trace", tr)
    monkeypatch.setattr(adsb, "fetch_opensky_track", osk)
    return calls


def _lookup_found_now(kind, value):
    """lookup חי: G-ZBKM נמצא *עכשיו* (בטיסה אחרת) — מקור ה-hex להודעה הישנה."""
    return {"status": "ok", "provider": "adsb.lol", "aircraft": [{
        "reg": "G-ZBKM", "hex": "406f79", "flight": "BAW107", "type": "B789",
        "lat": 51.4, "lon": -0.4, "t_pos": time.time() - 3, "fetched_at": time.time(),
        "provider": "adsb.lol", "src_type": "adsb_icao", "nic": 8}]}


def _trace_zbkm(hx, t):
    return {"status": "ok", "provider": "adsb.lol", "kind": "live",
            "url": "https://globe.adsb.lol/data/traces/79/trace_full_406f79.json",
            "points": _synthetic(ZBKM_LAT, ZBKM_LON, T_RX - 0.625, (-600, -40, 45, 600)),
            "meta": {"r": "G-ZBKM", "t": "B789"}, "t_first": T_RX - 600, "t_last": T_RX + 600,
            "covers": True}


# --- תחנה --------------------------------------------------------------------------

def test_station_roundtrip_and_validation(client):
    assert client.get("/api/station").get_json()["station"] is None    # אין ברירת מחדל
    st = _set_station(client)
    assert st["alt_m"] == 150.0 and st["source"] == "manual" and st["set_at"]
    assert client.get("/api/station").get_json()["station"]["lat"] == 31.968
    for bad in ({"lat": 95, "lon": 34, "alt_m": 1}, {"lat": 0, "lon": 0, "alt_m": 1},
                {"lat": 32, "lon": 34}, {"lat": 32, "lon": 34, "alt_m": 20000},
                {"lat": "x", "lon": 34, "alt_m": 1}):
        assert client.post("/api/station", json=bad).status_code == 400, bad
    assert client.post("/api/station", json={"clear": True}).get_json()["station"] is None


def test_station_post_requires_same_origin(client):
    r = client.post("/api/station", json=STATION, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403


# --- נרמול: פרטי ADS-C ו-AES ------------------------------------------------------

def test_normalize_acars_extracts_adsc_report_details():
    n = app._normalize_acars(_raw_b6())
    assert n["dir"] == "downlink" and n["pos_src"] == "adsc"
    assert n["adsc"]["ts_sec"] == 2739.375 and n["adsc"]["alt_ft"] == 37996
    assert n["adsc"]["accuracy_nm"] == 0.05 and n["adsc"]["tcas_avail"] is True
    # הודעה בלי ADS-C — השדה קיים ו-None (תאימות: שדה חדש, לא שינוי קיים)
    assert app._normalize_acars({"timestamp": 1.0, "label": "H1", "text": "hi"})["adsc"] is None


def test_adsc_details_earth_reference_keys():
    d = app._adsc_details({"tags": [{"basic_report": {"lat": 1.0, "lon": 2.0, "alt": 100}},
                                    {"earth_ref_data": {"true_trk_deg": 121.5, "true_trk_valid": True,
                                                        "gnd_spd_kts": 478.0, "vspd_ftmin": 0}}]})
    assert d["true_trk_deg"] == 121.5 and d["gnd_spd_kts"] == 478.0 and d["vspd_ftmin"] == 0
    assert app._adsc_details({"x": 1}) is None


def test_satcom_card_carries_aes_not_icao():
    card = app._normalize_satcom({
        "t": {"sec": 1_792_763_140, "usec": 0},
        "isu": {"src": {"addr": "406F79", "type": "Aircraft Earth Station"},
                "dst": {"addr": "42", "type": "Ground Earth Station"},
                "acars": {"label": "H1", "reg": "G-ZBKM", "msg_text": "hello"}}})
    assert card["aes"] == "406F79" and card.get("icao") is None and card["dir"] == "downlink"


# --- "אמת מול ADS-B" — G-ZBKM --------------------------------------------------------

def test_verify_zbkm_historical_blos_supported(client, monkeypatch):
    """ההודעה הישנה (רק ב-jsonl, לא בזיכרון) => lookup לפי רישום מגלה את ה-hex
    (מהטיסה הנוכחית) => trace היסטורי => אינטרפולציה בזמן הדוח => ראיות חזקות."""
    _set_station(client)
    _store_jsonl(app._normalize_acars(_raw_b6()))
    calls = _mock_net(monkeypatch, lookup=_lookup_found_now, trace=_trace_zbkm)
    r = client.post("/api/xcheck/verify", json={"src": "acars", "t": T_RX, "tail": ".G-ZBKM",
                                                "label": "B6"})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    res = body["result"]
    assert calls["lookup"] == [("reg", ".G-ZBKM")]
    assert calls["trace"] == [("406F79", T_RX)] and calls["opensky"] == []   # ה-trace כיסה
    assert res["match"]["by"] == "reg" and res["match"]["hex"] == "406F79"
    assert res["match"]["hex_src"] == "adsb_db"
    assert res["verdict"] == "blos_supported" and res["level"] == "strong"
    assert res["rx_at"]["status"] == "interpolated"
    assert res["adsc"]["report_age_s"] == 0.6
    assert 690 < res["horizon"]["distance_km"] < 705
    assert res["station"]["name"] == STATION["name"]
    # ראיות: ההודעה המקורית + נקודות ADS-B מקוריות + מקורות שנוסו
    assert res["msg"]["text"] == B6_TEXT and res["msg"]["level"] == -46.9
    assert res["adsb_points"] and all(p["provider"] == "adsb.lol" for p in res["adsb_points"])
    assert {s["name"] for s in res["sources"]} >= {"snapshot", "buffer", "lookup", "trace"}
    assert res["track"] and body["saved"] == res["event_id"]


def _saved_zbkm(client, monkeypatch):
    _set_station(client)
    _store_jsonl(app._normalize_acars(_raw_b6()))
    _mock_net(monkeypatch, lookup=_lookup_found_now, trace=_trace_zbkm)
    return client.post("/api/xcheck/verify", json={"src": "acars", "t": T_RX}).get_json()["saved"]


def test_events_list_filters_and_exports(client, monkeypatch):
    eid = _saved_zbkm(client, monkeypatch)
    ev = client.get("/api/xcheck/events").get_json()
    assert ev["total"] == 1 and ev["events"][0]["id"] == eid
    row = ev["events"][0]
    assert row["tail"] == ".G-ZBKM" and row["signal_dbfs"] == -46.9 and row["freq"] == 131.825
    assert row["verdict"] == "blos_supported" and row["evidence_level"] == "strong"
    day = time.strftime("%Y-%m-%d", time.localtime(T_RX))
    for q, n in (("min_km=600", 1), ("min_km=800", 0), ("max_km=500", 0),
                 ("min_dbfs=-50", 1), ("max_dbfs=-50", 0), (f"day={day}", 1),
                 ("day=2026-10-22", 0), ("evidence=strong", 1), ("evidence=weak,conflict", 0),
                 ("verdict=blos_supported", 1)):
        assert client.get("/api/xcheck/events?" + q).get_json()["total"] == n, q
    csv_body = client.get("/api/xcheck/events/export?format=csv").get_data(as_text=True)
    assert csv_body.startswith("﻿") and "distance_km" in csv_body and "G-ZBKM" in csv_body
    full = json.loads(client.get(f"/api/xcheck/events/{eid}/export?format=json").get_data(as_text=True))
    assert full[0]["msg"]["text"] == B6_TEXT and full[0]["horizon"]["radio_km"] > 0
    assert client.get(f"/api/xcheck/events/{eid}").get_json()["event"]["id"] == eid


def test_reverify_updates_same_event_and_delete(client, monkeypatch):
    eid = _saved_zbkm(client, monkeypatch)
    again = client.post("/api/xcheck/verify", json={"src": "acars", "t": T_RX, "save": True}).get_json()
    assert again["saved"] == eid
    assert client.get("/api/xcheck/events").get_json()["total"] == 1     # האחרון מנצח
    assert client.get(f"/api/xcheck/events/{eid}").get_json()["event"]["reason"] == "user"
    assert client.delete(f"/api/xcheck/events/{eid}").status_code == 200
    assert client.get("/api/xcheck/events").get_json()["total"] == 0
    assert client.get("/api/xcheck/events/zz").status_code == 404


def test_verify_without_adsb_still_answers_weak(client, monkeypatch):
    """ספקי ADS-B לא זמינים => תשובה תקינה (200) עם "אין נתונים" לכל מקור —
    המיקום מה-ADS-C לבדו: חריגה מאופק, ראיה חלשה. אירוע נשמר (חריג)."""
    _set_station(client)
    _store_jsonl(app._normalize_acars(_raw_b6()))
    _mock_net(monkeypatch)
    body = client.post("/api/xcheck/verify", json={"src": "acars", "t": T_RX}).get_json()
    res = body["result"]
    assert res["verdict"] == "exceeds_unverified" and res["level"] == "weak"
    assert res["rx_at"]["status"] == "none"
    srcs = {s["name"]: s for s in res["sources"]}
    assert srcs["lookup"]["status"] == "error" and srcs["trace"]["status"] == "no_hex"
    assert body["saved"]


def test_verify_old_record_redecodes_and_recomputes_direction(client, monkeypatch):
    """הודעה שנשמרה *לפני* v2.26.0: ‏dir="uplink" (באג ה-B6) ובלי שדה adsc. הכיוון
    מחושב מחדש לפי ה-label, ופרטי הדוח מפוענחים מחדש ב-decode_acars_apps."""
    _set_station(client)
    old = app._normalize_acars(_raw_b6())
    old.update(dir="uplink", adsc=None)
    _store_jsonl(old)
    seen = []

    def fake_run(dir_arg, label, text, as_json):
        seen.append((dir_arg, label, as_json))
        return text + "\n" + json.dumps(LIBACARS)        # decode_acars_apps מדפיס הד + JSON

    monkeypatch.setattr(app, "_run_libacars", fake_run)
    _mock_net(monkeypatch, lookup=_lookup_found_now, trace=_trace_zbkm)
    res = client.post("/api/xcheck/verify", json={"src": "acars", "t": T_RX}).get_json()["result"]
    assert seen == [("d", "B6", True)]
    assert res["direction"] == "downlink" and "חושב מחדש" in res["direction_basis"]
    assert res["adsc"]["report_age_s"] == 0.6
    assert res["verdict"] == "blos_supported"


def test_xc_direction_stored_acars_prefers_structure():
    """הצלבה על הודעה שמורה: block ID (מ-v2.31.0) ⇐ MSN (רק downlink נושא אחד) ⇐
    label. ‏loadsheet של C1 שנשמר כ-downlink נבחן קודם כשידור *מהמטוס* מול האופק."""
    c1 = {"label": "C1", "text": ".UTCKM1P IZ/241150\nAGM", "block_id": "C", "dir": "downlink"}
    d, basis = app._xc_direction("acars", c1)
    assert d == "uplink" and basis.startswith("מבני — block ID") and "חושב מחדש" in basis
    # לפני v2.31.0 לא נשמר block ID — ‏MSN מכריע (‏B3 שה-header סיווג uplink)
    b3 = {"label": "B3", "text": "/LLBG.DC1/RCD 024", "msgno": "M34A", "dir": "uplink"}
    d, basis = app._xc_direction("acars", b3)
    assert d == "downlink" and "MSN" in basis and "חושב מחדש" in basis
    # בלי שניהם — הטבלה (C1 תוקן ל-uplink)
    assert app._xc_direction("acars", {"label": "C1", "text": "X"})[0] == "uplink"
    # ‏MSN לא מכריע ב-VDL2 (שם יש כתובת AVLC, ובלעדיה לא מניחים את כלל ה-acarsdec)
    assert app._xc_direction("vdl2", {"label": "H1", "text": "X", "msgno": "M01A"})[0] is None


def test_verify_user_hex_contradicted_by_trace_registration(client, monkeypatch):
    """hex שהמשתמש הזין שייך (לפי ה-trace) לרישום אחר => סתירת זהות; נקודות ה-trace
    לא משמשות, ואין פסק-דין BLOS מחוזק."""
    _set_station(client)
    _store_jsonl(app._normalize_acars(_raw_b6()))
    _mock_net(monkeypatch, trace=lambda hx, t: {**_trace_zbkm(hx, t), "meta": {"r": "EI-XYZ"}})
    res = client.post("/api/xcheck/verify",
                      json={"src": "acars", "t": T_RX, "hex": "4ca123"}).get_json()["result"]
    assert res["match"]["conflict"] == "reg_mismatch" and res["level"] == "conflict"
    assert res["rx_at"]["status"] == "none"
    assert [s for s in res["sources"] if s["name"] == "trace"][0]["status"] == "conflict"


def test_verify_uplink_label_is_not_applicable(client, monkeypatch):
    _set_station(client)
    up = app._normalize_acars({"timestamp": T_RX, "label": "A6", "tail": ".G-ZBKM",
                               "text": "/QUKAXBA.ADS.G-ZBKM0000"})
    _store_jsonl(up)
    _mock_net(monkeypatch)
    res = client.post("/api/xcheck/verify", json={"src": "acars", "t": T_RX}).get_json()["result"]
    assert res["verdict"] == "na_uplink" and res["level"] == "n/a"


def test_verify_input_errors(client, monkeypatch):
    _mock_net(monkeypatch)
    assert client.post("/api/xcheck/verify", json={"src": "x", "t": 1}).status_code == 400
    assert client.post("/api/xcheck/verify", json={"src": "acars"}).status_code == 400
    assert client.post("/api/xcheck/verify", json={"src": "acars", "t": T_RX, "hex": "nothex"}).status_code == 400
    assert client.post("/api/xcheck/verify",
                       json={"src": "acars", "t": T_RX, "station": {"lat": 99}}).status_code == 400
    assert client.post("/api/xcheck/verify", json={"src": "acars", "t": T_RX}).status_code == 404


def test_verify_station_override_is_recorded(client, monkeypatch):
    """תחנה ניידת: אימות בדיעבד מול המיקום שבו התחנה *הייתה* — נשמר בראיה."""
    _store_jsonl(app._normalize_acars(_raw_b6()))
    _mock_net(monkeypatch)
    near = {"lat": 33.0, "lon": 28.0, "alt_m": 10.0}
    res = client.post("/api/xcheck/verify",
                      json={"src": "acars", "t": T_RX, "station": near}).get_json()["result"]
    assert res["station"]["lat"] == 33.0 and res["verdict"] == "line_of_sight"


def test_corrupt_events_file_is_tolerated(client, monkeypatch, env):
    (env / "xcheck_events.jsonl").write_text('not json\n[1,2]\n{"id": 5}\n')
    assert client.get("/api/xcheck/events").get_json()["total"] == 0
    _saved_zbkm(client, monkeypatch)
    assert client.get("/api/xcheck/events").get_json()["total"] == 1


# --- worker: תור לא-חוסם, שלב 1/2, תקציר לפיד ----------------------------------------

def test_submit_skips_messages_without_identity_and_never_raises(monkeypatch):
    app._xc_submit("acars", {"t": 1.0, "label": "_d"})                 # ACK ריק
    assert app._xc_heap == []
    app._xc_submit("acars", {"t": 1.0, "tail": ".TCARC", "tail_is_station": True})
    assert app._xc_heap == []
    monkeypatch.setattr(app, "_xc_push", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    app._xc_submit("acars", {"t": 1.0, "tail": "G-ZBKM"})               # לא זורק לנתיב הרדיו


def test_queue_bound_drops_instead_of_blocking(monkeypatch):
    monkeypatch.setattr(app, "XC_QUEUE_MAX", 2)
    for i in range(4):
        app._xc_submit("acars", {"t": float(i), "tail": "G-ZBKM"})
    assert len(app._xc_heap) == 2
    assert app._xc_stats["dropped"] == 2 and app._xc_stats["submitted"] == 2


def test_live_job_uses_snapshot_and_feeds_summary(client, monkeypatch):
    """הודעה חיה, המטוס ב-snapshot (נקודה 3ש' לפני הקליטה) => measured, בלי רשת,
    בלי שלב 2; התקציר מגיע עם /api/acars הקיים (‏xcheck + xcheck_cursor)."""
    monkeypatch.setattr(app, "_is_active", lambda svc: True)
    _set_station(client)
    now = time.time()
    la, lo = _dest(STATION["lat"], STATION["lon"], 300.0, 120.0)
    with adsb._LOCK:
        adsb.process([{"hex": "738065", "r": "4X-EHD", "flight": "ELY315", "lat": la, "lon": lo,
                       "alt_baro": 30000, "alt_geom": 30500, "gs": 450.0, "track": 300.0,
                       "nic": 8, "nac_p": 9, "seen_pos": 3.0, "type": "adsb_icao"}],
                     resp_now=now)
    rec = {"id": 7, "t": now, "tail": ".4X-EHD", "label": "H1", "text": "hi", "error": 0}
    with app._acars_lock:
        app._acars_msgs.append(rec)
    calls = _mock_net(monkeypatch)
    app._xc_run_job({"src": "acars", "rec": rec, "stage": 1, "prior": []})
    assert calls["lookup"] == [] and app._xc_heap == []               # ב-snapshot => בלי רשת/שלב 2
    data = client.get("/api/acars?since=0&xseq=0").get_json()
    s = data["xcheck"]["7"]
    assert s["adsb"] == "measured" and s["by"] == "reg" and s["v"] == "line_of_sight"
    assert data["xcheck_cursor"] >= 1
    assert client.get(f"/api/acars?xseq={data['xcheck_cursor']}").get_json()["xcheck"] == {}


def test_out_of_range_aircraft_two_stage_lookup_saves_event(client, monkeypatch):
    """מחוץ לרדיוס ה-poll: שלב 1 — lookup ממוקד (נקודה 25ש' לפני => "לא מאומת")
    => שלב 2 מתוזמן; שלב 2 — lookup נוסף (נקודה 35ש' אחרי) => אינטרפולציה,
    חריגה מאופק, ואירוע נשמר אוטומטית."""
    _set_station(client)
    now = time.time()
    t_rx = now - 1.0
    track = _synthetic(ZBKM_LAT, ZBKM_LON, t_rx, (-25, 35))
    state = {"n": 0}

    def lk(kind, value):
        p = track[min(state["n"], 1)]
        state["n"] += 1
        return {"status": "ok", "provider": "adsb.lol", "aircraft": [{
            "reg": "G-ZBKM", "hex": "406f79", "flight": "BAW60", "lat": p["lat"], "lon": p["lon"],
            "alt_baro": 38000, "alt_geom": 38600, "gs": 480.0, "track": 120.0, "nic": 8,
            "nac_p": 9, "src_type": "adsb_icao", "t_pos": p["t"], "fetched_at": now,
            "provider": "adsb.lol"}]}

    calls = _mock_net(monkeypatch, lookup=lk)
    rec = app._normalize_acars({**_raw_b6(), "timestamp": t_rx, "libacars": None})
    rec.update(id=11, lat=ZBKM_LAT, lon=ZBKM_LON)                       # בלי ts_sec של ADS-C
    app._xc_run_job({"src": "acars", "rec": rec, "stage": 1, "prior": []})
    assert calls["lookup"] == [("reg", ".G-ZBKM")]
    assert len(app._xc_heap) == 1                                       # שלב 2 מתוזמן
    _due, _tie, job2 = app._xc_heap.pop()
    assert job2["stage"] == 2 and len(job2["prior"]) == 1
    app._xc_run_job(job2)
    assert len(calls["lookup"]) == 2
    ev = client.get("/api/xcheck/events").get_json()["events"]
    assert len(ev) == 1 and ev[0]["adsb_status"] == "interpolated"
    assert ev[0]["verdict"] in ("blos_supported", "blos_possible") and ev[0]["reason"] == "auto"


def test_xcheck_status_route(client):
    st = client.get("/api/xcheck/status").get_json()
    assert st["ok"] and st["queue"] == 0 and "providers" in st["net"]
    assert st["auto_lookup"] is True and st["station"] is None


def test_live_worker_never_reads_track_buffer_and_interpolates_across_stages(client, monkeypatch):
    """⚠ ביצועים: ה-worker לא קורא את track.jsonl על כל הודעה. נקודת ה-snapshot
    משלב 1 (לפני הקליטה) + נקודת ה-snapshot משלב 2 (אחרי) => אינטרפולציה."""
    monkeypatch.setattr(adsb, "local_track_points",
                        lambda *a, **k: pytest.fail("ה-worker קרא את ה-buffer"))
    _set_station(client)
    _mock_net(monkeypatch)
    now = time.time()
    t_rx = now - 1.0
    la, lo = _dest(STATION["lat"], STATION["lon"], 300.0, 150.0)
    before, after = _synthetic(la, lo, t_rx, (-30, 30), brg=300.0, gs=450.0)

    def snap(p, resp_now):
        with adsb._LOCK:
            adsb.process([{"hex": "738065", "r": "4X-EHD", "lat": p["lat"], "lon": p["lon"],
                           "alt_baro": 30000, "gs": 450.0, "track": 300.0, "nic": 8,
                           "seen_pos": 0.0, "type": "adsb_icao"}], resp_now=resp_now)

    snap(before, before["t"])
    rec = {"id": 3, "t": t_rx, "tail": ".4X-EHD", "label": "H1", "text": "x", "error": 0}
    app._xc_run_job({"src": "acars", "rec": rec, "stage": 1, "prior": []})
    _due, _tie, job2 = app._xc_heap.pop()
    assert [p["origin"] for p in job2["prior"]] == ["live"]
    snap(after, after["t"])
    app._xc_run_job(job2)
    with app._xc_res_lock:
        summ = app._xc_results[("acars", 3)][1]
    assert summ["adsb"] == "interpolated" and summ["v"] == "line_of_sight"


# --- ייבוא trace ידני (ארכיון globe_history של adsb.lol) ------------------------

def _trace_file(icao="406f79", reg="G-ZBKM", gz=True):
    """קובץ trace_full בפורמט readsb (⚠ נקודות *סינתטיות* — בודקות את הצנרת)."""
    import base64
    import gzip as _gz
    base = T_RX - 700.0
    pts = _synthetic(ZBKM_LAT, ZBKM_LON, T_RX - 0.625, (-40, 45))
    trace = [[p["t"] - base, p["lat"], p["lon"], 38000, 480.0, 120.0, 0, 0,
              {"nic": 8, "nac_p": 9}, "adsb_icao", 38600] for p in pts]
    body = json.dumps({"icao": icao, "r": reg, "t": "B789", "timestamp": base, "trace": trace}).encode()
    return base64.b64encode(_gz.compress(body) if gz else body).decode()


def test_verify_with_imported_trace_replaces_network_history(client, monkeypatch):
    """globe.adsb.lol החזיר 403 על ה-Pi => המשתמש מעלה את הקובץ מהארכיון. הקובץ
    משמש במקום trace/OpenSky מהרשת (לא נקראים בכלל), ונשמר בראיה כמקור "import"."""
    _set_station(client)
    _store_jsonl(app._normalize_acars(_raw_b6()))
    calls = _mock_net(monkeypatch, lookup=_lookup_found_now, trace=_trace_zbkm)
    r = client.post("/api/xcheck/verify", json={"src": "acars", "t": T_RX, "trace_b64": _trace_file(),
                                                "trace_name": "trace_full_406f79.json"})
    assert r.status_code == 200, r.get_json()
    res = r.get_json()["result"]
    assert calls["trace"] == [] and calls["opensky"] == []
    imp = [s for s in res["sources"] if s["name"] == "import"][0]
    assert imp["status"] == "ok" and imp["file"] == "trace_full_406f79.json" and imp["covers"]
    assert res["rx_at"]["status"] == "interpolated" and res["rx_at"]["provider"] == "import"
    assert res["verdict"] == "blos_supported" and res["match"]["by"] == "reg"
    assert all(p["origin"] == "import" for p in res["adsb_points"])


def test_imported_trace_of_another_aircraft_is_a_conflict(client, monkeypatch):
    _set_station(client)
    _store_jsonl(app._normalize_acars(_raw_b6()))
    _mock_net(monkeypatch)
    res = client.post("/api/xcheck/verify", json={
        "src": "acars", "t": T_RX, "trace_b64": _trace_file(icao="4ca123", reg="EI-XYZ")}).get_json()["result"]
    imp = [s for s in res["sources"] if s["name"] == "import"][0]
    assert imp["status"] == "conflict" and imp["points"] == 0
    assert res["match"]["conflict"] == "reg_mismatch" and res["level"] == "conflict"
    assert res["rx_at"]["status"] == "none"


def test_imported_trace_without_comparable_identity_is_not_used(client, monkeypatch):
    """הודעה עם מספר טיסה בלבד וקובץ בלי רישום — אין מה להשוות => לא בשימוש."""
    _set_station(client)
    _store_jsonl(app._normalize_acars(_raw_b6(tail=None)))
    _mock_net(monkeypatch)
    res = client.post("/api/xcheck/verify", json={
        "src": "acars", "t": T_RX, "trace_b64": _trace_file(reg=None)}).get_json()["result"]
    imp = [s for s in res["sources"] if s["name"] == "import"][0]
    assert imp["status"] == "unmatched" and res["rx_at"]["status"] == "none"


def test_imported_trace_input_errors(client, monkeypatch):
    import base64
    _store_jsonl(app._normalize_acars(_raw_b6()))
    _mock_net(monkeypatch)
    post = lambda b64: client.post("/api/xcheck/verify", json={"src": "acars", "t": T_RX, "trace_b64": b64})
    assert post("!!!not base64!!!").status_code == 400
    assert post(base64.b64encode(b"not json").decode()).status_code == 400
    assert post(_trace_file(gz=False)).status_code == 200                   # לא דחוס — גם תקין
    monkeypatch.setattr(adsb, "TRACE_MAX_BYTES", 100)
    assert post(_trace_file()).status_code == 413
