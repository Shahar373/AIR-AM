# ============================================================================
#  AIR-AM - בדיקות יחידה למצב ACARS (מצב משולב: SDR אחד בהחלפה)
# ----------------------------------------------------------------------------
#  רץ בלי חומרה: ACARS_ENV_PATH מנותב ל-tmp, ו-systemctl/SDR ממוקפים.
# ============================================================================
import json
import socket
import threading
import time
import types

import pytest

import app


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "CONFIG_PATH", tmp_path / "airband.conf")
    monkeypatch.setattr(app, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(app, "ACARS_ENV_PATH", tmp_path / "acars.env")
    monkeypatch.setattr(app, "ACARS_LOG_PATH", tmp_path / "acars.jsonl")
    return tmp_path


@pytest.fixture
def client(paths):
    return app.app.test_client()


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(app.time, "sleep", lambda *a, **k: None)


def _ok(**kw):
    return types.SimpleNamespace(returncode=0, stdout="", stderr="", **kw)


# --- _sanitize_freqs (הגנת הזרקה) -------------------------------------------

def test_sanitize_freqs_filters_and_defaults():
    assert app._sanitize_freqs(["131.525", "bad; reboot", "136.9"]) == ["131.525", "136.9"]
    assert app._sanitize_freqs([]) == list(app.ACARS_FREQS_DEFAULT)
    assert app._sanitize_freqs(["$(reboot)", "x"]) == list(app.ACARS_FREQS_DEFAULT)
    assert app._sanitize_freqs(["131.55"]) == ["131.55"]   # 2 ספרות עשרוניות גם תקין


# --- write_acars_env (פורמט EnvironmentFile של systemd) ---------------------

def test_write_acars_env_format(paths):
    app.write_acars_env(["131.525", "131.725"], gain=-10, ratemult=160)
    txt = app.ACARS_ENV_PATH.read_text()
    # ערך לא מצוטט: systemd לוקח את שארית השורה; ‎$ACARS_FREQS ב-ExecStart מפצל בחזרה
    assert "ACARS_FREQS=131.525 131.725" in txt
    assert "ACARS_GAIN=-10" in txt
    assert "ACARS_RATEMULT=160" in txt
    assert f"ACARS_UDP={app.ACARS_UDP_HOST}:{app.ACARS_UDP_PORT}" in txt


def test_write_acars_env_sanitizes(paths):
    app.write_acars_env(["131.525", "evil; rm -rf /"], gain=-10)
    line = [l for l in app.ACARS_ENV_PATH.read_text().splitlines() if l.startswith("ACARS_FREQS")][0]
    assert line == "ACARS_FREQS=131.525"          # הערך הזדוני סונן


# --- _normalize_acars -------------------------------------------------------

def test_normalize_acars_full():
    m = {"timestamp": 1750000000.5, "channel": 1, "freq": 131.725, "level": -24.3,
         "error": 0, "mode": "2", "label": "H1", "block_id": "7", "ack": False,
         "tail": "4X-EKF", "flight": "LY315", "msgno": "M01A", "text": "HELLO\r\nWORLD\r"}
    n = app._normalize_acars(m)
    assert (n["tail"], n["flight"], n["freq"], n["label"], n["mode"]) == \
        ("4X-EKF", "LY315", 131.725, "H1", "2")
    assert n["text"] == "HELLO\nWORLD"             # CRLF מנורמל + strip
    assert n["msgno"] == "M01A"


def test_normalize_acars_empty_ack_tolerated():
    n = app._normalize_acars({"timestamp": 1.0, "freq": 131.55, "error": 0})
    assert n["tail"] is None and n["flight"] is None and n["text"] is None
    assert n["freq"] == 131.55


# --- listener + /api/acars roundtrip ----------------------------------------

def test_acars_listener_and_api(client, monkeypatch):
    monkeypatch.setattr(app, "_is_active", lambda svc: True)
    with app._acars_lock:                          # מאפסים את ה-buffer הגלובלי
        app._acars_msgs.clear()
        app._acars_seq = 0
    threading.Thread(target=app._acars_listener, daemon=True).start()
    time.sleep(0.2)

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.sendto(json.dumps({"timestamp": 1.0, "freq": 131.725, "tail": "4X-EKF",
                         "flight": "LY1", "label": "H1", "text": "hi"}).encode(),
             (app.ACARS_UDP_HOST, app.ACARS_UDP_PORT))
    s.sendto(json.dumps({"timestamp": 2.0, "freq": 131.55, "error": 0}).encode(),
             (app.ACARS_UDP_HOST, app.ACARS_UDP_PORT))
    s.sendto(b"not-json-garbage", (app.ACARS_UDP_HOST, app.ACARS_UDP_PORT))  # יתעלם

    deadline = time.time() + 3
    while time.time() < deadline:
        data = client.get("/api/acars?since=0").get_json()
        if len(data["messages"]) >= 2:
            break
        time.sleep(0.05)

    assert data["ok"] and data["active"] is True
    assert len(data["messages"]) == 2              # ה-garbage לא נכנס
    assert data["messages"][0]["tail"] == "4X-EKF"
    cursor = data["cursor"]
    assert cursor == 2
    # since=cursor => אין חדשות
    assert client.get("/api/acars?since=%d" % cursor).get_json()["messages"] == []


# --- /api/mode --------------------------------------------------------------

def test_api_mode_invalid(client, paths):
    assert client.post("/api/mode", json={"mode": "bogus"}).status_code == 400


def test_api_mode_enter_acars(client, paths, no_sleep, monkeypatch):
    calls = []
    monkeypatch.setattr(app, "_sysctl",
                        lambda action, svc, timeout=45: calls.append((action, svc)) or _ok())
    monkeypatch.setattr(app, "_is_active", lambda svc: True)   # acarsdec "active" אחרי start
    r = client.post("/api/mode", json={"mode": "acars"})
    j = r.get_json()
    assert r.status_code == 200 and j["ok"] and j["app_mode"] == "acars"
    assert j["acars_freqs"] == list(app.ACARS_FREQS_DEFAULT)
    assert app.load_state()["app_mode"] == "acars"
    # שחרר את rtl_airband והרים את acarsdec
    assert ("stop", "rtl_airband") in calls and ("restart", app.ACARS_SERVICE) in calls
    # נכתב env תקין
    assert "ACARS_FREQS=131.525 131.550 131.725 131.825" in app.ACARS_ENV_PATH.read_text()


def test_api_mode_enter_acars_failure_recovers_voice(client, paths, no_sleep, monkeypatch):
    app.save_state({**app.DEFAULT_STATE, "freq": 121.5})
    monkeypatch.setattr(app, "_sysctl", lambda action, svc, timeout=45: _ok())
    monkeypatch.setattr(app, "_is_active", lambda svc: False)  # acarsdec לא עלה => כישלון
    r = client.post("/api/mode", json={"mode": "acars"})
    assert r.status_code == 500
    assert r.get_json()["state"]["app_mode"] == "voice"        # חזרה לקול


def test_api_mode_voice_stops_acars_and_tunes(client, paths, monkeypatch):
    app.save_state({**app.DEFAULT_STATE, "freq": 121.5, "app_mode": "acars"})
    monkeypatch.setattr(app, "_restart_and_verify", lambda: (None, None, False))
    monkeypatch.setattr(app, "_is_active", lambda svc: True)   # acars פעיל כרגע
    calls = []
    monkeypatch.setattr(app, "_sysctl",
                        lambda action, svc, timeout=45: calls.append((action, svc)) or _ok())
    r = client.post("/api/mode", json={"mode": "voice"})
    j = r.get_json()
    assert r.status_code == 200 and j["ok"] and j["app_mode"] == "voice"
    assert j["freq"] == 121.5                                  # התדר השמור האחרון
    assert ("stop", app.ACARS_SERVICE) in calls               # acarsdec נעצר


def test_api_tune_exits_acars_mode(client, paths, monkeypatch):
    monkeypatch.setattr(app, "_restart_and_verify", lambda: (None, None, False))
    monkeypatch.setattr(app, "_is_active", lambda svc: True)
    calls = []
    monkeypatch.setattr(app, "_sysctl",
                        lambda action, svc, timeout=45: calls.append((action, svc)) or _ok())
    r = client.post("/api/tune", json={"freq": 134.6})
    assert r.get_json()["ok"]
    st = app.load_state()
    assert st["app_mode"] == "voice" and st["freq"] == 134.6
    assert ("stop", app.ACARS_SERVICE) in calls               # כיוונון קולי עוצר ACARS


# --- /api/state reconciliation ----------------------------------------------

def test_api_state_reports_live_mode(client, paths, monkeypatch):
    monkeypatch.setattr(app, "_is_active", lambda svc: svc == app.ACARS_SERVICE)
    assert client.get("/api/state").get_json()["app_mode"] == "acars"
    monkeypatch.setattr(app, "_is_active", lambda svc: False)
    body = client.get("/api/state").get_json()
    assert body["app_mode"] == "voice"
    assert body["acars_freqs"] == list(app.ACARS_FREQS_DEFAULT)


# --- התמדה: acars.jsonl + טעינה בעלייה --------------------------------------

def _reset_buffer():
    with app._acars_lock:
        app._acars_msgs.clear()
        app._acars_seq = 0


def test_acars_log_append_and_load_history(paths):
    _reset_buffer()
    for t in (3.0, 1.0, 2.0):                       # סדר כתיבה לא-ממוין בזמן
        app._append_acars_log({"t": t, "freq": 131.55, "tail": "4X-A%d" % int(t)})
    app._load_acars_history()
    with app._acars_lock:
        msgs = list(app._acars_msgs)
    assert [m["t"] for m in msgs] == [1.0, 2.0, 3.0]   # ממוין עולה לפי t
    assert [m["id"] for m in msgs] == [1, 2, 3]        # id רץ הוקצה
    assert app._acars_seq == 3


def test_acars_log_trim(paths, monkeypatch):
    monkeypatch.setattr(app, "ACARS_LOG_KEEP", 5)
    for i in range(12):
        app._append_acars_log({"t": float(i), "freq": 131.55})
    app._trim_acars_log()
    lines = app.ACARS_LOG_PATH.read_text().splitlines()
    assert len(lines) == 5                             # נחתך ל-KEEP
    assert json.loads(lines[0])["t"] == 7.0            # נשמר הזנב (7..11)
    assert json.loads(lines[-1])["t"] == 11.0


def test_load_history_tolerates_garbage(paths):
    _reset_buffer()
    app.ACARS_LOG_PATH.write_text('{"t": 1.0, "freq": 131.5}\nnot-json\n{"t": 2.0}\n')
    app._load_acars_history()
    with app._acars_lock:
        msgs = list(app._acars_msgs)
    assert [m["t"] for m in msgs] == [1.0, 2.0]        # השורה הפגומה דולגה


# --- נרמול עשיר: קטגוריה + מיקום --------------------------------------------

def test_normalize_category_from_labels():
    q0 = app._normalize_acars({"timestamp": 1.0, "label": "Q0"})
    assert q0["category"] == "בדיקת קישור (link test)" and q0["group"] == "comm"
    assert app._normalize_acars({"timestamp": 1.0, "label": "QA"})["group"] == "oooi"
    unk = app._normalize_acars({"timestamp": 1.0, "label": "ZZ"})   # לא מוכר => fallback
    assert unk["category"] == "Label ZZ" and unk["group"] == "text"
    assert app._normalize_acars({"timestamp": 1.0})["category"] == "הודעה"   # בלי label


def test_label_3l_uld_category():
    """regression: label 3L (ULD/cargo) — מתוך קליטה אמיתית D-AIDA (596b4ef0...)."""
    n = app._normalize_acars({"timestamp": 1.0, "label": "3L",
                               "tail": "D-AIDA", "text": "531183D1934/12,935/12,936/12"})
    assert "ULD" in n["category"] or "מטען" in n["category"]
    assert n["group"] == "tech"
    assert n["dir"] == "downlink"


def test_label_5v_and_a4_known():
    """regression: 5V (VHF link mgmt) ו-A4 (FSM) לא נופלים ל-'Label X'."""
    n5v = app._normalize_acars({"timestamp": 1.0, "label": "5V", "tail": "YR-ADA"})
    assert "Label 5V" not in n5v["category"]
    na4 = app._normalize_acars({"timestamp": 1.0, "label": "A4", "tail": "4X-EMC",
                                 "text": "/TLVCDYA.FS1/FSM 1452 AIZ1805 RCD RECEIVED"})
    assert "Label A4" not in na4["category"]
    assert na4["dir"] == "uplink"


def test_normalize_position_from_libacars():
    m = {"timestamp": 1.0, "label": "B9",
         "libacars": {"arinc622": {"adsc": {"basic_report": {"lat": 32.1, "lon": 34.9}}}}}
    n = app._normalize_acars(m)
    assert (n["lat"], n["lon"], n["pos_src"]) == (32.1, 34.9, "adsc")
    assert n["group"] == "position"                    # מיקום => תמיד ירוק


def test_normalize_cpdlc_decoded_and_group():
    n = app._normalize_acars({"timestamp": 1.0, "libacars": {"cpdlc": {"msg": "CLIMB TO FL350"}}})
    assert n["category"] == "CPDLC" and n["group"] == "clearance"
    assert n["decoded"] == "CLIMB TO FL350"


def test_normalize_rejects_bad_latlon():
    assert app._normalize_acars({"timestamp": 1.0, "libacars": {"x": {"lat": 0, "lon": 0}}})["lat"] is None
    assert app._normalize_acars({"timestamp": 1.0, "libacars": {"x": {"lat": 999, "lon": 34}}})["lat"] is None


def test_normalize_position_from_text():
    n = app._normalize_acars({"timestamp": 1.0, "text": "POS N3206.0E03450.0 FL350"})
    assert n["pos_src"] == "text"
    assert 31 < n["lat"] < 33 and 34 < n["lon"] < 35


def test_acars_direction_heuristic():
    # label מוכר: OOOI/position/login => downlink; BA (אישור מהקרקע) => uplink
    assert app._acars_direction("QA", None) == "downlink"
    assert app._acars_direction("H1", "HELLO") == "downlink"
    assert app._acars_direction("BA", None) == "uplink"
    assert app._acars_direction("A9", None) == "uplink"   # ATIS מהקרקע
    assert app._acars_direction("Q0", "PING") == "downlink"  # link test ממטוס
    assert app._acars_direction("80", None) == "downlink"    # OOOI report ממטוס
    # header ניתוב בתחילת הטקסט => uplink (גם בלי label מוכר)
    assert app._acars_direction("ZZ", ".ATSXCXA CLEARED TO...") == "uplink"
    assert app._acars_direction(None, "/TLVATYA WX REPORT") == "uplink"
    # עמום => None (לא מנחשים)
    assert app._acars_direction(None, "JUST SOME TEXT") is None


def test_normalize_includes_direction():
    n = app._normalize_acars({"timestamp": 1.0, "label": "QA", "tail": "4X-A"})
    assert n["dir"] == "downlink"
    n2 = app._normalize_acars({"timestamp": 1.0, "label": "BA", "tail": "4X-A"})
    assert n2["dir"] == "uplink"
    assert app._normalize_acars({"timestamp": 1.0, "label": "Q0"})["dir"] == "downlink"


def test_text_latlon_rejects_noise_and_parses_arinc():
    assert app._text_latlon("FUEL 5678 KG PART N1278E56789") is None   # דקות 78>59 => נדחה
    assert app._text_latlon("CODE N32E034") is None                    # בלי DDMM מלא => נדחה
    lat, lon = app._text_latlon("POS S3206.5W03450.0 FL350")           # דרום/מערב
    assert -33 < lat < -31 and -35 < lon < -34


def test_text_latlon_comma_separated():
    """פורמט H1 של B737: N3203.7,E03455.7 (פסיק בין lat ל-lon, בלי רווח)."""
    lat, lon = app._text_latlon("10.17.24,DC,3201,01167,155.6,.240,024.0,027.5,N3203.7,E03455.7,138480")
    assert abs(lat - 32.0617) < 0.001 and abs(lon - 34.9283) < 0.001


def test_text_latlon_compact_no_decimal():
    """פורמט קומפקטי ללא נקודה עשרונית: N32042E034560 = N32°04.2' E034°56.0'."""
    # embedded mid-string with trailing digits (D56A style)
    lat, lon = app._text_latlon("N32042E03456010170135P238245007GXXXX22000HAB,")
    assert abs(lat - 32.07) < 0.001 and abs(lon - 34.9333) < 0.001
    # POS prefix (F50A style)
    lat, lon = app._text_latlon("POSN32010E034540,RW21,103458,2,1000,,GEMDA")
    assert abs(lat - 32.0167) < 0.001 and abs(lon - 34.9) < 0.001
    # FPON prefix, 3-digit degree lon (F00A style — Turkish airspace)
    lat, lon = app._text_latlon("LTAA.AFN/FMHIGT1166,.4L-GIT,,062600/FPON39428E038506,1/FCOADS")
    assert abs(lat - 39.7133) < 0.001 and abs(lon - 38.8433) < 0.001


def test_text_latlon_login_is_not_aircraft_position():
    """regression: ה-DDMM ב-login של LLBG הוא נ"צ *השדה* (משותף לכל מטוס שמתחבר),
    לא מיקום המטוס. אסור שיחולץ — אחרת כל הודעת login מקבלת 📍 מטעה על השדה.
    טקסטים מתוך קליטה אמיתית (596b4ef0...json)."""
    assert app._text_latlon("02XSTLVLLBG03200N03452EV136975/") is None
    assert app._text_latlon("02XATLVLLBG13201N03452EB136975/ARINC") is None
    # end-to-end: הודעת login (label SQ) לא מקבלת מיקום => נשארת בקבוצת comm
    n = app._normalize_acars({"timestamp": 1.0, "label": "SQ",
                              "text": "02XSTLVLLBG03200N03452EV136975/"})
    assert n["lat"] is None and n["pos_src"] is None
    assert n["group"] != "position"


def test_text_position_skipped_on_corrupted_frame():
    """regression: frame עם acarsdec error>0 לא מפיק מיקום טקסטואלי (ספרה שהתהפכה
    בקואורדינטה => מטוס במקום שגוי). ADS-C מוגן-CRC נשמר; ה-heuristic לא."""
    clean = app._normalize_acars({"timestamp": 1.0, "error": 0, "tail": "LY-LOC",
                                  "text": "POSN32010E034540,RW21,103458"})
    assert clean["lat"] is not None and clean["pos_src"] == "text"
    corrupt = app._normalize_acars({"timestamp": 1.0, "error": 2, "tail": "LY-LOC",
                                    "text": "POSN32010E034540,RW21,103458"})
    assert corrupt["lat"] is None and corrupt["pos_src"] is None
    assert corrupt["group"] != "position"


def test_parse_pos_report_basic():
    """regression: /.POS/ (תגובת REQPOS) — קליטה אמיתית N375WB (596b4ef0...).
    פורמט מבני => נ\"צ + WPT + ETA מחולצים."""
    text = "/.POS/TS104451,260626N32006E034539,,104451,1,VELOX,110451,,P31,,147,F566"
    result = app._parse_pos_report(text)
    assert result is not None, "/.POS/ אמור להניב תוצאה"
    lat, lon, decoded = result
    assert abs(lat - 32.01) < 0.001,  f"lat שגוי: {lat}"
    assert abs(lon - 34.8983) < 0.001, f"lon שגוי: {lon}"
    assert "VELOX" in decoded
    assert "11:04" in decoded       # ETA 110451 → 11:04z


def test_pos_report_extracted_even_with_error():
    """regression: /.POS/ נחלץ גם כאשר acarsdec מדווח error — כי הפורמט מבני,
    לא heuristic. מתוך N375WB אמיתי שהיה error=3 (596b4ef0...)."""
    text = "/.POS/TS104451,260626N32006E034539,,104451,1,VELOX,110451,,P31,,147,F566"
    n = app._normalize_acars({"timestamp": 1.0, "error": 3, "tail": "N375WB", "text": text})
    assert n["lat"] is not None,  "/.POS/ עם error אמור לתת מיקום"
    assert n["pos_src"] == "pos-report"
    assert n["group"] == "position"
    assert n["decoded"] and "VELOX" in n["decoded"]


def test_parse_pos_report_rejects_garbage():
    """/.POS/ prefix בלי תוכן תקין => None."""
    assert app._parse_pos_report("/.POS/GARBAGE") is None
    assert app._parse_pos_report(None) is None
    assert app._parse_pos_report("POSN32010E034540") is None  # לא /.POS/


# --- ייצוא ------------------------------------------------------------------

def test_acars_export_csv(client, paths):
    app.ACARS_LOG_PATH.write_text(
        json.dumps({"t": 2.0, "freq": 131.55, "tail": "4X-B", "category": "ADS-C",
                    "group": "position", "dir": "downlink", "lat": 32.1, "lon": 34.9,
                    "text": "line1\nline2"}) + "\n"
        + json.dumps({"t": 1.0, "freq": 131.72, "tail": "4X-A", "category": "Label H1"}) + "\n")
    r = client.get("/api/acars/export?format=csv")
    assert r.status_code == 200
    assert r.headers["Content-Disposition"].startswith("attachment;")
    body = r.data.decode("utf-8-sig")                  # מתעלם מ-BOM
    lines = body.splitlines()
    assert lines[0].startswith("time_iso,timestamp,freq")
    assert "dir" in lines[0].split(",")                # עמודת כיוון בייצוא
    assert "4X-A" in lines[1] and "4X-B" in lines[2]   # ממוין לפי t עולה
    assert len(lines) == 3                             # newline בטקסט לא שובר שורה
    assert "line1 line2" in body


def test_acars_export_json(client, paths):
    app.ACARS_LOG_PATH.write_text(
        json.dumps({"t": 2.0, "tail": "B"}) + "\n" + json.dumps({"t": 1.0, "tail": "A"}) + "\n")
    data = json.loads(client.get("/api/acars/export?format=json").data)
    assert [d["tail"] for d in data] == ["A", "B"]     # ממוין לפי t


# --- label 80 / A9 / C1 classifications ------------------------------------

def test_label_80_group():
    n = app._normalize_acars({"timestamp": 1.0, "label": "80", "tail": "4X-A",
                               "text": "OFFRP LY316/14"})
    assert n["group"] == "oooi"
    assert n["dir"] == "downlink"


def test_label_a9_group():
    n = app._normalize_acars({"timestamp": 1.0, "label": "A9",
                               "text": "LLBG INFO D RWY 12 WIND 080/15KT QNH 1018"})
    assert n["group"] == "comm"
    assert n["dir"] == "uplink"


def test_label_c1_direction():
    n = app._normalize_acars({"timestamp": 1.0, "label": "C1", "tail": "4X-A",
                               "text": "COMPANY MSG"})
    assert n["dir"] == "downlink"


# --- _parse_atis -----------------------------------------------------------

def test_parse_atis_basic():
    text = "LLBG INFO D RWY 12 WIND 080/15KT QNH 1018 TEMP 28"
    r = app._parse_atis(text)
    assert r is not None
    assert "12" in r        # runway
    assert "080" in r       # wind
    assert "1018" in r      # QNH


def test_parse_atis_no_match():
    assert app._parse_atis("HELLO WORLD") is None
    assert app._parse_atis(None) is None


def test_parse_atis_sets_decoded_in_normalize():
    n = app._normalize_acars({"timestamp": 1.0, "label": "A9",
                               "text": "INFO A RWY 03 WIND 020/08KT QNH 1013"})
    assert n["decoded"] is not None
    assert "03" in n["decoded"]


# --- _parse_oooi_80 --------------------------------------------------------

def test_parse_oooi_80_basic():
    text = "OFFRP LY316/14 OUT1420 OFF1432 DEST LLBG ETA1510"
    r = app._parse_oooi_80(text)
    assert r is not None
    assert "OUT" in r and "OFF" in r


def test_parse_oooi_80_no_match():
    assert app._parse_oooi_80("RANDOM TEXT") is None
    assert app._parse_oooi_80(None) is None


def test_parse_oooi_80_sets_decoded_in_normalize():
    n = app._normalize_acars({"timestamp": 1.0, "label": "80", "tail": "4X-A",
                               "text": "OFFRP LY12/05 OUT1400 OFF1415 DEST EGLL"})
    assert n["decoded"] is not None
    assert "OUT" in n["decoded"]


# --- _extract_actype -------------------------------------------------------

def test_extract_actype_h1():
    assert app._extract_actype("H1", "B738 SYSTEMS OK") == "B738"
    assert app._extract_actype("H1", "A320 FUEL REPORT") == "A320"
    assert app._extract_actype("C1", "B777 CHECK") == "B777"


def test_extract_actype_non_h1_ignored():
    assert app._extract_actype("QA", "B738") is None    # label לא H1/C1
    assert app._extract_actype("H1", None) is None


def test_actype_in_normalize():
    n = app._normalize_acars({"timestamp": 1.0, "label": "H1", "tail": "4X-EKF",
                               "text": "10.17.24,DC,B738,01167,155.6"})
    assert n["actype"] == "B738"
    n2 = app._normalize_acars({"timestamp": 1.0, "label": "QA", "tail": "4X-A"})
    assert n2["actype"] is None


# --- ייצוא: עמודת actype --------------------------------------------------

def test_acars_export_csv_has_actype(client, paths):
    app.ACARS_LOG_PATH.write_text(
        json.dumps({"t": 1.0, "tail": "4X-A", "actype": "B738"}) + "\n")
    body = client.get("/api/acars/export?format=csv").data.decode("utf-8-sig")
    header = body.splitlines()[0]
    assert "actype" in header.split(",")


# --- _parse_wx_alternates (label WX / alternate planning) --------------------

def test_parse_wx_alternates_multi():
    """4+ שדות alternate = decoded עם רשימה."""
    text = "METAR LGRP LYBE LIMC LFPG"
    r = app._parse_wx_alternates(text)
    assert r is not None
    assert r.startswith("ALTERNATE:")
    assert "LGRP" in r and "LIMC" in r


def test_parse_wx_alternates_single():
    """שדה בודד שאינו LLBG = WX: CODE (לא alternate planning)."""
    r = app._parse_wx_alternates("METAR LCLK")
    assert r is not None
    assert r.startswith("WX:")
    assert "LCLK" in r


def test_parse_wx_alternates_llbg_ignored():
    """LLBG עצמה לא מסומנת כ-alternate (זהו השדה הביתי)."""
    r = app._parse_wx_alternates("METAR LLBG")
    assert r is None


def test_parse_wx_alternates_no_icao():
    """טקסט בלי קודי ICAO = None."""
    assert app._parse_wx_alternates("NO VALID CODES") is None
    assert app._parse_wx_alternates(None) is None
    assert app._parse_wx_alternates("") is None


def test_wx_label_in_normalize():
    """label WX ב-_normalize_acars: קטגוריה נכונה + decoded עם alternate."""
    n = app._normalize_acars({"timestamp": 1.0, "label": "WX", "tail": "9H-CAC",
                               "text": "METAR LGRP LYBE LIMC LFPG"})
    assert n["group"] == "comm"
    assert n["dir"] == "downlink"
    assert n["decoded"] is not None
    assert "ALTERNATE" in n["decoded"]
    assert "LLBG" not in n["decoded"]


# --- dedup retries -----------------------------------------------------------

def test_dedup_key_identifies_retry():
    """אותו tail+label+text80 = מפתח dedup זהה (retry יהיה מזוהה)."""
    base = {"timestamp": 1000.0, "tail": "OO-ACF", "label": "H1",
            "text": "CFEM-APU-REAL " * 5}
    r1 = app._normalize_acars({**base})
    r2 = app._normalize_acars({**base, "timestamp": 1045.0})

    key1 = (r1.get("tail"), r1.get("label"), (r1.get("text") or "")[:80])
    key2 = (r2.get("tail"), r2.get("label"), (r2.get("text") or "")[:80])
    assert key1 == key2          # מפתחות זהים = תיזוהה כ-retry


def test_dedup_retry_count_update():
    """עדכון retry_count על הרשומה המקורית (בדיקת לוגיקת הזיכרון)."""
    rec = {"tail": "OO-ACF", "label": "H1", "text": "APU FAULT MSG", "t": 1000.0,
           "group": "tech"}
    # מדמה את מה שה-listener עושה: prev_rec הוא אותו dict
    rec["retry_count"] = rec.get("retry_count", 1) + 1
    assert rec["retry_count"] == 2
    rec["retry_count"] = rec.get("retry_count", 1) + 1
    assert rec["retry_count"] == 3
