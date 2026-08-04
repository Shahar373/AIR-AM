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


def test_normalize_acars_level_kept_no_snr_without_noise():
    """acarsdec לא מספק רצפת רעש לכל הודעה => level (dBFS) תמיד נשמר, אבל snr
    חייב להישאר None — לעולם לא מוערך, כדי לא להציג ערך לא-אמין."""
    n = app._normalize_acars({"timestamp": 1.0, "freq": 131.55, "level": -24.3, "error": 0})
    assert n["level"] == -24.3
    assert n["snr"] is None


def test_normalize_acars_snr_computed_when_noise_present():
    """כש-noise קיים בקלט (כמו שקורה במסלול A של VDL2) — snr מחושב כהפרש אמיתי,
    לא מוערך."""
    n = app._normalize_acars({"timestamp": 1.0, "freq": 131.55, "level": -20.0,
                              "noise": -50.0, "error": 0})
    assert n["snr"] == 30.0


# --- listener + /api/acars roundtrip ----------------------------------------

def test_acars_listener_and_api(client, monkeypatch):
    monkeypatch.setattr(app, "_is_active", lambda svc: True)
    with app._acars_lock:                          # מאפסים את ה-buffer הגלובלי
        app._acars_msgs.clear()
        app._acars_seq = 0
    threading.Thread(target=app._acars_listener, daemon=True).start()
    time.sleep(0.2)

    now = time.time()                              # היום (מסנן "היום בלבד" ב-/api/acars)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.sendto(json.dumps({"timestamp": now, "freq": 131.725, "tail": "4X-EKF",
                         "flight": "LY1", "label": "H1", "text": "hi"}).encode(),
             (app.ACARS_UDP_HOST, app.ACARS_UDP_PORT))
    s.sendto(json.dumps({"timestamp": now + 1, "freq": 131.55, "error": 0}).encode(),
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


def test_acars_listener_survives_normalize_exception(client, monkeypatch):
    """דאטהגרם עם שדה מטיפוס לא-צפוי (label כרשימה => unhashable ב-ACARS_LABELS.get)
    לא אמור להפיל את ה-thread לצמיתות — הפיד ממשיך לזרום להודעות הבאות.
    פורט ייעודי (לא ה-port הגלובלי) — כדי לא להתנגש עם ה-listener הקבוע-חי
    שכבר נפתח ב-test_acars_listener_and_api באותו תהליך pytest."""
    free_port = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    free_port.bind(("127.0.0.1", 0))
    port = free_port.getsockname()[1]
    free_port.close()
    monkeypatch.setattr(app, "ACARS_UDP_PORT", port)

    monkeypatch.setattr(app, "_is_active", lambda svc: True)
    with app._acars_lock:
        app._acars_msgs.clear()
        app._acars_seq = 0
    th = threading.Thread(target=app._acars_listener, daemon=True)
    th.start()
    time.sleep(0.2)

    now = time.time()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    # label כרשימה => TypeError (unhashable) בתוך _normalize_acars
    s.sendto(json.dumps({"timestamp": now, "label": ["A9"], "text": "boom"}).encode(),
             (app.ACARS_UDP_HOST, port))
    s.sendto(json.dumps({"timestamp": now + 1, "freq": 131.55, "tail": "4X-OK",
                         "label": "H1", "text": "still alive"}).encode(),
             (app.ACARS_UDP_HOST, port))

    deadline = time.time() + 3
    data = {"messages": []}
    while time.time() < deadline:
        data = client.get("/api/acars?since=0").get_json()
        if len(data["messages"]) >= 1:
            break
        time.sleep(0.05)

    assert th.is_alive()                            # ה-thread לא מת
    assert len(data["messages"]) == 1                # ההודעה התקינה עברה
    assert data["messages"][0]["tail"] == "4X-OK"


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
    # נכתב env תקין (בנק ברירת המחדל)
    assert ("ACARS_FREQS=" + " ".join(app.ACARS_FREQS_DEFAULT)) in app.ACARS_ENV_PATH.read_text()


def test_api_mode_enter_acars_failure_falls_to_off(client, paths, no_sleep, monkeypatch):
    # אין fallback לקול: כישלון כניסה למצב נופל ל-off (standby) — המצבים שווי-מעמד
    app.save_state({**app.DEFAULT_STATE, "freq": 121.5, "app_mode": "voice"})
    calls = []
    monkeypatch.setattr(app, "_sysctl",
                        lambda action, svc, timeout=45: calls.append((action, svc)) or _ok())
    monkeypatch.setattr(app, "_is_active", lambda svc: False)  # acarsdec לא עלה => כישלון
    r = client.post("/api/mode", json={"mode": "acars"})
    assert r.status_code == 500
    body = r.get_json()
    assert body["app_mode"] == "off"                           # החוזה ל-UI: נחיתה בבית
    assert body["state"]["app_mode"] == "off"
    assert body["state"]["prev_mode"] == "voice"               # מה היה לפני הכישלון
    assert app.load_state()["app_mode"] == "off"               # נשמר => שורד reboot
    assert ("restart", "rtl_airband") not in calls             # שום ניסיון "לחזור לקול"
    # standby עצר את כל הצרכנים
    for svc in (app.ACARS_SERVICE, app.VDL2_SERVICE, "rtl_airband"):
        assert ("stop", svc) in calls


def test_api_mode_failure_response_shape(client, paths, no_sleep, monkeypatch):
    # חוזה תשובת הכישלון של /api/mode — ה-UI מסתמך על המפתחות האלה
    monkeypatch.setattr(app, "_sysctl", lambda action, svc, timeout=45: _ok())
    monkeypatch.setattr(app, "_is_active", lambda svc: False)
    body = client.post("/api/mode", json={"mode": "acars"}).get_json()
    assert set(body) >= {"ok", "error", "detail", "app_mode", "state"}
    assert body["ok"] is False and body["app_mode"] == "off"


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
    # אף צרכן לא פעיל => הכוונה השמורה (בלי state שמור: ברירת המחדל הניטרלית off)
    monkeypatch.setattr(app, "_is_active", lambda svc: False)
    body = client.get("/api/state").get_json()
    assert body["app_mode"] == "off"
    assert body["mode_ok"] is True                 # standby מכוון אינו תקלה
    assert body["acars_freqs"] == list(app.ACARS_FREQS_DEFAULT)


def test_api_state_idle_reports_saved_intent(client, paths, monkeypatch):
    # המצב השמור אמור לרוץ אבל אף צרכן לא פעיל => מדווח את הכוונה + mode_ok=False
    # (תקלה גלויה, לא "voice" שקט כמו פעם)
    app.save_state({**app.DEFAULT_STATE, "app_mode": "acars"})
    monkeypatch.setattr(app, "_is_active", lambda svc: False)
    body = client.get("/api/state").get_json()
    assert body["app_mode"] == "acars"
    assert body["mode_ok"] is False
    # מציאות-תחילה עדיין מנצחת: שירות פעיל דורס כוונה שמורה אחרת
    monkeypatch.setattr(app, "_is_active", lambda svc: svc == app.VDL2_SERVICE)
    body = client.get("/api/state").get_json()
    assert body["app_mode"] == "vdl2" and body["mode_ok"] is True


# --- התמדה: acars.jsonl + טעינה בעלייה --------------------------------------

def _reset_buffer():
    with app._acars_lock:
        app._acars_msgs.clear()
        app._acars_seq = 0


def test_acars_log_append_and_load_history(paths):
    _reset_buffer()
    base = app._today_start() + 10                  # היום (אחרת מסנן "היום בלבד" יחסום)
    for dt in (3.0, 1.0, 2.0):                      # סדר כתיבה לא-ממוין בזמן
        app._append_acars_log({"t": base + dt, "freq": 131.55, "tail": "4X-A%d" % int(dt)})
    app._load_acars_history()
    with app._acars_lock:
        msgs = list(app._acars_msgs)
    assert [m["t"] for m in msgs] == [base + 1.0, base + 2.0, base + 3.0]   # ממוין עולה לפי t
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
    base = app._today_start() + 10                  # היום (מסנן "היום בלבד")
    app.ACARS_LOG_PATH.write_text(
        '{"t": %f, "freq": 131.5}\nnot-json\n{"t": %f}\n' % (base + 1, base + 2))
    app._load_acars_history()
    with app._acars_lock:
        msgs = list(app._acars_msgs)
    assert [m["t"] for m in msgs] == [base + 1, base + 2]   # השורה הפגומה דולגה


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


def test_normalize_cpdlc_does_not_leak_embedded_latlon_as_position():
    """מיקום מיוחס *רק* מ-ADS-C: CPDLC (clearance) עלול לשאת נ"צ מוטבע (waypoint
    ב-route, למשל 'PROCEED DIRECT') שאינו מיקום המטוס עצמו — לא מייחסים אותו
    כמיקום כדי לא להטעות במפה. אותה הגנה בדיוק כבר קיימת ב-_normalize_vdl2
    מסלול B (is_adsc); זה הפער המקביל ב-_normalize_acars המשותף (גם ל-ACARS/
    VDL2-A "רגילים" עם arinc622, וגם ל-SATCOM דרכו)."""
    n = app._normalize_acars({"timestamp": 1.0, "libacars": {
        "cpdlc": {"msg": "PROCEED DIRECT", "route": {"lat": 32.1, "lon": 34.9}}}})
    assert n["category"] == "CPDLC" and n["group"] == "clearance"
    assert n["lat"] is None and n["lon"] is None and n["pos_src"] is None


def test_normalize_rejects_bad_latlon():
    # "adsc" במפתח => kind="ADS-C" (כמו label15/-16 אמיתיים), כדי שהבדיקה תעבור
    # בפועל דרך _scan_latlon (ולא תדלג עליו בגלל הגידור ל-ADS-C בלבד, ר' #7).
    assert app._normalize_acars({"timestamp": 1.0, "libacars": {"adsc": {"lat": 0, "lon": 0}}})["lat"] is None
    assert app._normalize_acars({"timestamp": 1.0, "libacars": {"adsc": {"lat": 999, "lon": 34}}})["lat"] is None


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


def test_text_latlon_rejects_waypoint_chain():
    """regression: הודעת H1 עם תוכנית טיסה (‎#M3FPN/.../F:IVAKI,N32558E015065..
    LUMED,N34200E014420..) מכילה *שרשרת* waypoints בפורמט מיקום קומפקטי — לא
    דיווח מיקום בודד. לפני התיקון: ה-waypoint הראשון (IVAKI) היה מתפרש כמיקום
    המטוס בפועל. טקסט אמיתי מקליטת SATCOM (33 דק', 465 הודעות)."""
    text = ("- #M3FPN/RP:DA:HLMS:AA:LIEO:F:IVAKI,N32558E015065..LUMED,N34200E014420"
            "..SENTI,N37103E012330..LOPKO,N37400E012108..GERMO,N39150E011214"
            "..ATNET,N40459E010081:A:ATNE2R903B")
    assert app._text_latlon(text) is None


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


def test_normalize_acars_ground_station_tail_gets_no_heuristic_position():
    """regression (קליטת SATCOM אמיתית, 33 דק'/465 הודעות): tail=".TCARC" הוא
    כתובת תחנת-קרקע (נקודה מובילה — אותו דפוס כמו _UPLINK_HEADER_RE), לא מטוס.
    לפני התיקון: הודעת H1 שלה עם תוכנית טיסה קיבלה 📍 שגוי (waypoint הראשון
    במסלול טופל כאילו הוא מיקום ה"מטוס" .TCARC בפועל). שני שכבות ההגנה
    (guard לפי tail + guard לפי ריבוי-קואורדינטות ב-_text_latlon) עובדות גם
    כל אחת בנפרד: כאן דווקא ה-FPN לא מפוענח מבנית (sub-label MD לא במילון)
    אז זה test למקרה שבו ה-heuristic הטקסטואלי הוא הקו ההגנה היחיד שנשאר."""
    n = app._normalize_acars({
        "timestamp": 1.0, "label": "H1", "tail": ".TCARC", "mode": "2",
        "text": "- #M3FPN/RP:DA:HLMS:AA:LIEO:F:IVAKI,N32558E015065..LUMED,N34200E014420"
                "..SENTI,N37103E012330..LOPKO,N37400E012108..GERMO,N39150E011214"
                "..ATNET,N40459E010081:A:ATNE2R903B",
    })
    assert n["lat"] is None and n["lon"] is None and n["pos_src"] is None
    assert n["group"] != "position"


def test_normalize_acars_ground_station_tail_guard_is_specific_not_global():
    """ה-guard חוסם *רק* tail דמוי-תחנת-קרקע — לא הופך את text_latlon ללא-פעיל
    בכלל. אותו טקסט עם נ"צ בודד (לא שרשרת): תחנת-קרקע לא מקבלת מיקום, מטוס
    אמיתי (tail רגיל) כן — ‏A ו-C (ר' _text_latlon) הם שני guards עצמאיים."""
    single_coord_text = "POSN32016E034538,VELOX,110451"
    station = app._normalize_acars({"timestamp": 1.0, "label": "H1",
                                    "tail": ".TCARC", "text": single_coord_text})
    assert station["lat"] is None
    aircraft = app._normalize_acars({"timestamp": 1.0, "label": "H1",
                                     "tail": "4X-EKF", "text": single_coord_text})
    assert aircraft["lat"] is not None and abs(aircraft["lat"] - 32.02667) < 0.001


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


def test_parse_pos_report_rejects_invalid_minutes():
    """regression: דקות מחוץ ל-00–59 נדחות (כמו _L15_RE). קריטי כי /.POS/ נחלץ
    גם עם error — ספרת דקות שהתהפכה (80.0') הזיזה בעבר את המטוס ~1.3° בשקט."""
    bad_lat = "/.POS/TS104451,260626N32800E034539,,104451,1,VELOX,110451"
    assert app._parse_pos_report(bad_lat) is None
    bad_lon = "/.POS/TS104451,260626N32006E034939,,104451,1,VELOX,110451"
    assert app._parse_pos_report(bad_lon) is None
    good = "/.POS/TS104451,260626N32006E034539,,104451,1,VELOX,110451"
    assert app._parse_pos_report(good) is not None


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


def test_api_acars_serializes_copies(client, monkeypatch):
    """/api/acars מסדרל *עותקים* של ההודעות — לא references ל-ring buffer.
    אחרת עדכון retry_count של ה-listener באמצע איטרציית ה-JSON encoder (שרץ
    אחרי שחרור _acars_lock) היה מפיל את הבקשה ב-RuntimeError."""
    monkeypatch.setattr(app, "_is_active", lambda svc: True)
    with app._acars_lock:
        app._acars_msgs.clear()
        app._acars_seq = 1
        rec = {"id": 1, "t": time.time(), "tail": "OO-ACF", "label": "H1", "text": "hi"}
        app._acars_msgs.append(rec)

    captured = {}
    real_jsonify = app.jsonify
    monkeypatch.setattr(app, "jsonify",
                        lambda **kw: (captured.update(kw), real_jsonify(**kw))[1])
    data = client.get("/api/acars?since=0").get_json()
    assert data["messages"][0]["tail"] == "OO-ACF"
    sent = captured["messages"][0]
    assert sent is not rec                 # עותק, לא אותו אובייקט
    assert sent == rec                     # אבל תוכן זהה


# --- חבילת פענוח עמוק: SA / H1 / FPN / label 15 / SQ / autotune --------------

def test_parse_sa_media_established():
    d = app._parse_sa_media("0EV093425VS")
    assert "VHF" in d and "נוצר" in d
    assert "09:34:25" in d
    assert "זמין" in d and "SATCOM" in d


def test_parse_sa_media_lost():
    d = app._parse_sa_media("0LS121200V")
    assert "SATCOM" in d and "אבד" in d
    assert "12:12:00" in d


def test_parse_sa_media_rejects_garbage():
    assert app._parse_sa_media("0Z093425") is None      # אות אירוע לא מוכרת
    assert app._parse_sa_media("0EV936425") is None     # שעה לא חוקית (93)
    assert app._parse_sa_media("0EV293425VS") is None   # שעה 29 (בעבר עברה — [0-2]\d)
    assert app._parse_sa_media("0EV233425VS") is not None   # 23 חוקית
    assert app._parse_sa_media("") is None
    assert app._parse_sa_media(None) is None


def test_sa_in_normalize():
    n = app._normalize_acars({"timestamp": 1.0, "label": "SA", "tail": "4X-EKF",
                              "text": "0EV093425VS"})
    assert n["dir"] == "downlink"
    assert n["decoded"] and "VHF" in n["decoded"]


def test_parse_h1_sublabel():
    assert "מקליט נתונים" in app._parse_h1("#DFB737-800 REPORT DATA")
    assert "FMC" in app._parse_h1("#M1BREQPD")
    assert "מסוף תא" in app._parse_h1("#T2BFREE TEXT FROM CABIN")
    assert app._parse_h1("PLAIN TEXT WITHOUT HEADER") is None
    assert app._parse_h1(None) is None


def test_parse_h1_sublabel_satcom_dash_prefix():
    """regression: כל הודעות H1 שנצפו בקליטת SATCOM אמיתית (12/12 בקליטה של 33
    דק') מגיעות עם "- #" (מקף+רווח לפני ה-#), לא "#" בתחילת הטקסט ממש —
    ‏_H1_SUB_RE הישן (^#...) לא תפס אף אחת מהן. גם אמצע-טקסט אחרי \n (SATCOM
    login-style header) חייב לעבוד. הפורמט המקורי (בלי prefix) נשאר תקין."""
    assert "FMC 3" in app._parse_h1("- #M3FPN/RP:DA:HLMS:AA:LIEO:F:IVAKI,N32558E015065")
    assert app._parse_h1("- #MDREQPOS037B") is None      # sub "MD" לא במילון => אין decoded, לא ניחוש
    d = app._parse_h1(".HELASAY 040956\nDFD\nAN OH-LXC/FI AY1806/GL XXF/MA 963I\n- #DFREQ02")
    assert "מקליט נתונים" in d                            # '#' אחרי \n ולא בתחילת המחרוזת
    assert "מקליט נתונים" in app._parse_h1("#DFB737-800 REPORT DATA")   # פורמט VHF המקורי — עדיין עובד


def test_parse_h1_pos_report():
    """#M1B + POS מיד אחרי ההדר => 'דיווח מיקום'; הנ"צ הקומפקטי נחלץ (frame נקי)."""
    text = "#M1BPOSN32016E034538,VELOX,110451"
    d = app._parse_h1(text)
    assert "FMC" in d and "דיווח מיקום" in d
    n = app._normalize_acars({"timestamp": 1.0, "label": "H1", "tail": "4X-EKF",
                              "error": 0, "text": text})
    assert n["lat"] is not None and abs(n["lat"] - 32.02667) < 0.001
    assert n["decoded"] and "FMC" in n["decoded"]


def test_parse_fpn_route():
    d = app._parse_h1("#M1B/FPN/RI:DA:LLBG:AA:LGAV:F:PURLA..SOLIN..NIKAS")
    assert "LLBG→LGAV" in d
    assert "PURLA" in d and "SOLIN" in d and "NIKAS" in d


def test_parse_fpn_satcom_no_leading_slash():
    """regression: SATCOM אמיתי (inmarsat-sniffer) שולח "M3FPN/..." — ה-sub-label
    (M3) מודבק ישירות ל-FPN בלי '/' מפריד, בניגוד לפורמט VHF "/FPN/". תוספת,
    לא מחליפה: "/FPN/" (הפורמט המדויק יותר) עדיין נבדק ראשון."""
    d = app._parse_fpn("- #M3FPN/RP:DA:HLMS:AA:LIEO:F:IVAKI,N32558E015065..LUMED,N34200E014420")
    assert d is not None and "HLMS→LIEO" in d and "IVAKI" in d and "LUMED" in d
    # ‏"/FPN/" המקורי (עם קו נטוי פותח) נשאר עובד ולא נדרס ע"י הנפילה החדשה
    d2 = app._parse_fpn("#M1B/FPN/RI:DA:LLBG:AA:LGAV:F:PURLA..SOLIN")
    assert d2 is not None and "LLBG→LGAV" in d2


def test_parse_fpn_waypoint_coord_trimmed():
    """נ"צ צמוד ל-waypoint אחרי פסיק נחתך; מעל 8 נקודות => (+N)."""
    wpts = "..".join(f"WPT{i:02d},N32016E034538" for i in range(10))
    d = app._parse_fpn(f"/FPN/RI:AA:LGAV:F:{wpts}")
    assert "WPT00" in d and "N32016" not in d
    assert "(+2)" in d


def test_label15_position_in_normalize():
    n = app._normalize_acars({"timestamp": 1.0, "label": "15", "tail": "4X-EKF",
                              "error": 0, "text": "(2N32016E034538ELY315"})
    assert n["pos_src"] == "label15"
    assert abs(n["lat"] - 32.02667) < 0.001
    assert abs(n["lon"] - 34.89667) < 0.001
    assert n["group"] == "position"
    assert n["dir"] == "downlink"


def test_label15_extracted_even_with_error():
    """label 15 הוא פורמט מעוגן-מבני (כמו /.POS/) => נחלץ גם עם error>0."""
    n = app._normalize_acars({"timestamp": 1.0, "label": "15", "tail": "4X-EKF",
                              "error": 2, "text": "(2N32016E034538ELY315"})
    assert n["lat"] is not None and n["pos_src"] == "label15"


def test_label15_rejects_bad_minutes():
    assert app._parse_label15("(2N32916E034538") is None   # דקות 91 לא חוקיות
    assert app._parse_label15("N32016E034538") is None     # בלי הדר (2
    assert app._parse_label15(None) is None


def test_parse_sq_ground_station():
    """SQ squitter אמיתי מהקליטה (596b4ef0...): תחנה + תדר, *בלי* נ"צ (נ"צ התחנה)."""
    text = "02XSTLVLLBG03200N03452EV136975/"
    d = app._parse_sq(text)
    assert "TLV" in d and "LLBG" in d
    assert "136.975" in d
    n = app._normalize_acars({"timestamp": 1.0, "label": "SQ", "text": text})
    assert n["lat"] is None and n["pos_src"] is None       # לקח 1.7.1 נשמר
    assert n["dir"] == "uplink"                            # squitter משודר מהקרקע
    assert n["decoded"] and "LLBG" in n["decoded"]


def test_parse_sq_rejects_garbage():
    assert app._parse_sq("HELLO WORLD") is None
    assert app._parse_sq("0") is None
    assert app._parse_sq(None) is None


def test_autotune_label():
    n = app._normalize_acars({"timestamp": 1.0, "label": ":;", "text": "131550"})
    assert n["decoded"] == "כוונון אוטומטי ל-131.550MHz"
    assert n["dir"] == "uplink"
    assert app._parse_autotune("999999") is None           # מחוץ ל-air band
    assert app._parse_autotune("") is None


def test_voice_go_ahead_label():
    n = app._normalize_acars({"timestamp": 1.0, "label": "54", "text": ""})
    assert "קול" in n["category"]
    assert n["dir"] == "uplink"


# --- באג: תג-סוג פנימי של libacars ("adsc_msg") מוצג כאילו הוא תוכן ------------

def test_libacars_decode_filters_internal_type_tag():
    """regression: 'msg_type':'adsc_msg' (מתוך קליטה אמיתית) הוצג בעבר כ-decoded
    כאילו זה תוכן ההודעה — זהו תג-סוג snake_case פנימי, לא טקסט. תוקן: מסונן."""
    kind, text = app._libacars_decode({"adsc": {"msg_type": "adsc_msg",
                                                 "basic_report": {"lat": 32.1, "lon": 34.9}}})
    assert kind == "ADS-C" and text is None


def test_libacars_decode_keeps_real_text():
    """תוכן אמיתי (כולל מילה בודדת כמו CPDLC WILCO) לא נפגע מהסינון — רק תגי-סוג
    snake_case (lowercase) מסוננים, לא טקסט אנושי (uppercase / עם רווחים)."""
    kind, text = app._libacars_decode({"cpdlc": {"msg_type": "cpdlc_msg", "msg_text": "WILCO"}})
    assert kind == "CPDLC" and text == "WILCO"
    kind, text = app._libacars_decode({"cpdlc": {"msg_data": {"msg_text": "CLIMB TO FL350"}}})
    assert text == "CLIMB TO FL350"


# --- פרסרים נוספים שנבנו מקליטה אמיתית (C1 loadsheet, 16, 1L, A3 PDC) ----------
# כל הווקטורים הבאים הם טקסטים מדויקים מתוך קליטה אמיתית ב-131.725/131.825 MHz
# (LLBG, יוני 2026) — לא סינתטיים.

def test_parse_loadsheet_real_capture():
    text = (".UTCKM1P IZ/271346\nAGM\nFI IZ1843/AN 4X-EMF\n-  LOADSHEET\n"
            "FINAL01 IZ1843/27   TLVETM 4XEMF 27JUN26\n"
            "CREW    2/3  PAX  60             TTL  61\n"
            "ZFW   33937  MAX    42600\nTOF    6650\nTOW   40587  MAX    52290\nTIF    1645")
    d = app._parse_loadsheet(text)
    assert "ZFW 33937kg" in d
    assert "TOW 40587kg" in d
    assert "TOF 6650kg" in d
    assert "נוסעים 60" in d and "צוות 2/3" in d
    assert 'סה"כ 61' in d


def test_parse_loadsheet_ignores_mac_prefixed_fields():
    """MACZFW/MACTOW/LIZFW לא אמורים להתבלבל עם ZFW/TOW (בדיקת \b)."""
    text = "LOADSHEET\nMACZFW  23.7  MACTOW  19.6  LIZFW 60.1"
    assert app._parse_loadsheet(text) is None    # אין ZFW/TOW אמיתיים, רק שדות MAC*/LI*


def test_parse_loadsheet_requires_keyword():
    assert app._parse_loadsheet("ZFW 33937 TOW 40587") is None   # בלי 'LOADSHEET' => לא מזוהה
    assert app._parse_loadsheet(None) is None


def test_loadsheet_in_normalize():
    n = app._normalize_acars({"timestamp": 1.0, "label": "C1", "tail": "4X-EMF",
                              "text": "LOADSHEET\nZFW   33937  MAX    42600"})
    assert n["decoded"] == "ZFW 33937kg"
    assert n["dir"] == "downlink"                 # C1 כבר ממופה downlink


def test_parse_label16_real_capture():
    """label 16: דיווח מיקום עשרוני שכלל לא זוהה קודם (lat=null בקליטה המקורית)."""
    text = "BAL-14 ,N 35.676,E  34.264,35001,0501,2034,063\\TS180539,010726"
    n = app._normalize_acars({"timestamp": 1.0, "label": "16", "error": 0, "text": text})
    assert n["pos_src"] == "label16"
    assert abs(n["lat"] - 35.676) < 0.001
    assert abs(n["lon"] - 34.264) < 0.001
    assert n["group"] == "position"
    assert "BAL-14" in n["decoded"] and "35001ft" in n["decoded"]
    assert n["dir"] == "downlink"


def test_parse_label16_gated_by_error():
    """label 16 פחות נוקשה-פורמט מ-DDMM המבני => מגודר כמו heuristic, לא נחלץ עם error."""
    text = "BAL-14 ,N 35.676,E  34.264,35001,0501,2034,063\\TS180539,010726"
    n = app._normalize_acars({"timestamp": 1.0, "label": "16", "error": 1, "text": text})
    assert n["lat"] is None and n["pos_src"] is None


def test_parse_label16_rejects_garbage():
    assert app._parse_label16("not a position report") is None
    assert app._parse_label16(None) is None


def test_parse_nav_fuel_real_capture():
    text = "00178220200 N 31.432/E 30.998/UTC 1842/FOB     4.9/ALT  15000/CAS 279.7/ETA 1902"
    n = app._normalize_acars({"timestamp": 1.0, "label": "1L", "error": 0, "text": text})
    assert n["pos_src"] == "nav-fuel"
    assert abs(n["lat"] - 31.432) < 0.001
    assert abs(n["lon"] - 30.998) < 0.001
    assert "18:42" in n["decoded"] and "4.9t" in n["decoded"] and "19:02" in n["decoded"]


def test_parse_nav_fuel_short_variant_not_matched():
    """הווריאנט הקצר של 1L (בלי נ"צ) לא אמור להתפרש בטעות — נופל ל-None, לא ניחוש."""
    assert app._parse_nav_fuel("00177214200HECA,1902") is None


def test_parse_nav_fuel_extracted_even_with_error():
    """עוגן ארוך וספציפי (7 שדות ברצף) => מבני מספיק לחילוץ גם עם error, כמו /.POS/."""
    text = "N 31.432/E 30.998/UTC 1842/FOB 4.9/ALT 15000/CAS 279.7/ETA 1902"
    n = app._normalize_acars({"timestamp": 1.0, "label": "1L", "error": 2, "text": text})
    assert n["lat"] is not None and n["pos_src"] == "nav-fuel"


def test_parse_pdc_real_capture():
    """PDC (A3): אישור טרום-המראה אמיתי — היעד שהיה הכי קרוב למה שביקשנו מ-CPDLC."""
    text = ("/TLVCDYA.DC1/CLD 1452 260627 LLBG PDC 678\n"
            "AIZ1805 CLRD TO LLER OFF 26 VIA TOMAL4E\n"
            "SQUAWK 4504 NEXT FREQ 121.750 ATIS W\n"
            "CLIMB INIT ALT 4000  PLEASE ACK DC7482")
    n = app._normalize_acars({"timestamp": 1.0, "label": "A3", "text": text})
    d = n["decoded"]
    assert "ל-LLER" in d and "המראה 26" in d and "SID TOMAL4E" in d
    assert "Squawk 4504" in d and "תדר הבא 121.750" in d and "טפס ל-4000ft" in d
    assert n["group"] == "clearance"
    assert n["dir"] == "uplink"


def test_parse_pdc_partial_fields():
    d = app._parse_pdc("CLRD TO LLBG SQUAWK 1200")
    assert "ל-LLBG" in d and "Squawk 1200" in d
    assert "SID" not in d                          # לא נמצא => לא מוצג


def test_parse_pdc_rejects_garbage():
    assert app._parse_pdc("HELLO WORLD") is None
    assert app._parse_pdc(None) is None


# ============================================================================
#  פיצ'רים חדשים: בנקי תדרים + ולידציית חלון, תצוגת "היום בלבד", standby
# ============================================================================

# --- בנקי תדרים + ולידציית חלון דגימה ---------------------------------------

def test_acars_banks_default():
    # ברירת המחדל היא הבנק הראשון (131.x מורחב), והוא נכנס בחלון אחד
    assert app.ACARS_FREQS_DEFAULT == app.ACARS_BANKS[0]["freqs"]
    for bank in app.ACARS_BANKS:
        assert app._acars_window_error(bank["freqs"]) is None   # כל בנק חוקי
        assert len(bank["freqs"]) <= app.ACARS_MAX_CHANNELS


def test_acars_window_error_too_wide():
    # 131.x + 136.x מרוחקים ~5MHz => חורגים מחלון יחיד
    assert app._acars_window_error(["131.550", "136.900"]) is not None
    # תקין בתוך החלון
    assert app._acars_window_error(["131.525", "131.825"]) is None


def test_acars_window_error_too_many_channels():
    nine = ["131.5%02d" % i for i in range(9)]      # 9 ערוצים צמודים => חוקי-span אך >8
    assert app._acars_window_error(nine) is not None


def test_acars_window_error_empty():
    assert app._acars_window_error([]) is not None


def test_api_mode_acars_rejects_wide_window(client, paths, no_sleep, monkeypatch):
    # בקשת מעבר ל-ACARS עם תדרים שלא נכנסים בחלון => 400 לפני נגיעה ב-SDR
    called = []
    monkeypatch.setattr(app, "_sysctl",
                        lambda action, svc, timeout=45: called.append((action, svc)) or _ok())
    r = client.post("/api/mode", json={"mode": "acars", "freqs": ["131.550", "136.900"]})
    assert r.status_code == 400
    assert called == []                              # לא נגענו בשירותים


# --- תצוגת "היום בלבד" ------------------------------------------------------

def test_load_acars_history_today_only(paths):
    _reset_buffer()
    base = app._today_start()
    app._append_acars_log({"t": base - 90000, "tail": "OLD"})    # אתמול
    app._append_acars_log({"t": base + 100, "tail": "NEW"})      # היום
    app._load_acars_history()
    with app._acars_lock:
        tails = [m.get("tail") for m in app._acars_msgs]
    assert tails == ["NEW"]                          # רק היום נטען לזיכרון


def test_api_acars_today_filter_and_all(client, paths, monkeypatch):
    monkeypatch.setattr(app, "_is_active", lambda svc: False)
    _reset_buffer()
    base = app._today_start()
    with app._acars_lock:
        for t, tail in ((base - 90000, "OLD"), (base + 100, "NEW")):
            app._acars_seq += 1
            app._acars_msgs.append({"id": app._acars_seq, "t": t, "tail": tail})
    # ברירת מחדל => היום בלבד
    msgs = client.get("/api/acars?since=0").get_json()["messages"]
    assert [m["tail"] for m in msgs] == ["NEW"]
    # ?all=1 => כל מה שבזיכרון (כולל אתמול)
    allmsgs = client.get("/api/acars?since=0&all=1").get_json()["messages"]
    assert [m["tail"] for m in allmsgs] == ["OLD", "NEW"]


def test_acars_export_keeps_all_history(client, paths):
    # הייצוא לא מסונן לפי תאריך (ההיסטוריה המלאה נשמרת) — גם t ישן נכלל
    app.ACARS_LOG_PATH.write_text(
        json.dumps({"t": 1.0, "tail": "OLD"}) + "\n"
        + json.dumps({"t": 2.0, "tail": "OLD2"}) + "\n")
    data = json.loads(client.get("/api/acars/export?format=json").data)
    assert [d["tail"] for d in data] == ["OLD", "OLD2"]


# --- כיבוי/הפעלת מקלט (standby) ---------------------------------------------

def test_api_mode_off_stops_all_consumers(client, paths, no_sleep, monkeypatch):
    calls = []
    monkeypatch.setattr(app, "_sysctl",
                        lambda action, svc, timeout=45: calls.append((action, svc)) or _ok())
    monkeypatch.setattr(app, "_is_active", lambda svc: False)   # כל הצרכנים כבויים אחרי stop
    r = client.post("/api/mode", json={"mode": "off"})
    j = r.get_json()
    assert r.status_code == 200 and j["ok"] and j["app_mode"] == "off"
    # standby עוצר את *שלושת* צרכני ה-SDR (קול + ACARS + VDL2)
    assert ("stop", "rtl_airband") in calls and ("stop", app.ACARS_SERVICE) in calls
    assert ("stop", app.VDL2_SERVICE) in calls
    assert all(svc != "sdrplay" for _, svc in calls)           # sdrplay לא נגעו בו
    assert app.load_state()["app_mode"] == "off"


def test_api_state_reports_off(client, paths, monkeypatch):
    app.save_state({**app.DEFAULT_STATE, "app_mode": "off"})
    monkeypatch.setattr(app, "_is_active", lambda svc: False)   # שני הצרכנים כבויים
    assert client.get("/api/state").get_json()["app_mode"] == "off"


def test_api_health_off_not_fault(client, paths, monkeypatch):
    app.save_state({**app.DEFAULT_STATE, "app_mode": "off"})

    def fake_run(cmd, **kw):
        svc = cmd[-1]
        active = "active" if svc == "sdrplay" else "inactive"
        return types.SimpleNamespace(returncode=0, stdout=active, stderr="")
    monkeypatch.setattr(app.subprocess, "run", fake_run)
    monkeypatch.setattr(app, "_sdr_present", lambda: True)
    j = client.get("/api/health").get_json()
    assert j["ok"] is True and j["app_mode"] == "off"           # standby = תקין, לא תקלה


def test_api_mode_off_then_voice(client, paths, monkeypatch):
    app.save_state({**app.DEFAULT_STATE, "freq": 118.1, "app_mode": "off"})
    monkeypatch.setattr(app, "_restart_and_verify", lambda: (None, None, False))
    monkeypatch.setattr(app, "_is_active", lambda svc: False)   # יוצאים מ-standby
    monkeypatch.setattr(app, "_sysctl", lambda action, svc, timeout=45: _ok())
    r = client.post("/api/mode", json={"mode": "voice"})
    j = r.get_json()
    assert r.status_code == 200 and j["ok"] and j["app_mode"] == "voice"
    assert app.load_state()["app_mode"] == "voice"
