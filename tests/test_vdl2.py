# ============================================================================
#  AIR-AM - בדיקות יחידה למצב VDL2 (מצב שלישי: SDR אחד בהחלפה)
# ----------------------------------------------------------------------------
#  רץ בלי חומרה: VDL2_ENV_PATH מנותב ל-tmp, ו-systemctl/SDR ממוקפים.
#  וקטורי הבדיקה בנויים לפי סכמת ה-JSON של dumpvdl2 v2.6.0 כפי שאומתה מהמקור
#  (fmtr-json.c + avlc.c + xid.c/x25.c ב-dumpvdl2, acars.c ב-libacars).
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
    monkeypatch.setattr(app, "VDL2_ENV_PATH", tmp_path / "vdl2.env")
    monkeypatch.setattr(app, "VDL2_LOG_PATH", tmp_path / "vdl2.jsonl")
    return tmp_path


@pytest.fixture
def client(paths):
    return app.app.test_client()


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(app.time, "sleep", lambda *a, **k: None)


def _ok(**kw):
    return types.SimpleNamespace(returncode=0, stdout="", stderr="", **kw)


def _reset_buffer():
    with app._vdl2_lock:
        app._vdl2_msgs.clear()
        app._vdl2_seq = 0


def _vdl2(avlc, t_sec=None, usec=250000, freq=136975000, sig=-22.1):
    """עוטף שכבת AVLC בפריים dumpvdl2 מלא (מטא-דאטה כפי שנפלט ב-fmtr-json.c)."""
    return {"vdl2": {
        "app": {"name": "dumpvdl2", "ver": "2.6.0"},
        "t": {"sec": int(t_sec if t_sec is not None else app._today_start() + 100),
              "usec": usec},
        "freq": freq, "burst_len_octets": 116, "hdr_bits_fixed": 0,
        "octets_corrected_by_fec": 0, "idx": 0,
        "sig_level": sig, "noise_level": -44.4, "freq_skew": 1.2,
        "avlc": avlc,
    }}


def _avlc_downlink(extra):
    """שלד AVLC של downlink (מטוס => תחנת קרקע) + התוכן שב-extra."""
    return {"src": {"addr": "738065", "type": "Aircraft", "status": "Airborne"},
            "dst": {"addr": "10917A", "type": "Ground station"},
            "cr": "Command", "frame_type": "I", "rseq": 0, "sseq": 2, "poll": False,
            **extra}


# --- _normalize_vdl2: מסלול A (ACARS-over-VDL2) ------------------------------

def test_normalize_vdl2_acars_label15_position():
    """הודעת ACARS-over-VDL2 עם דיווח מיקום label 15 — כל הפרסרים הקיימים חלים."""
    t0 = app._today_start() + 100
    m = _vdl2(_avlc_downlink({"acars": {
        "err": False, "crc_ok": True, "more": False, "reg": ".4X-EKF",
        "mode": "2", "label": "15", "blk_id": "8", "ack": "!",
        "flight": "LY0315", "msg_num": "M55", "msg_num_seq": "A",
        "msg_text": "(2N32016E034538ELY315",
    }}), t_sec=t0)
    n = app._normalize_vdl2(m)
    assert n is not None
    assert abs(n["t"] - (t0 + 0.25)) < 1e-6        # t.sec + t.usec/1e6
    assert n["freq"] == 136.975                    # Hz => MHz
    assert n["level"] == -22.1
    assert (n["tail"], n["flight"], n["label"]) == (".4X-EKF", "LY0315", "15")
    assert n["msgno"] == "M55A"                    # msg_num + msg_num_seq
    assert n["error"] == 0
    assert n["icao"] == "738065"                   # כתובת ה-AVLC של המטוס
    assert n["dir"] == "downlink"                  # מבני (src=Aircraft)
    # הפרסר הקיים של label 15 עבד דרך _normalize_acars
    assert n["pos_src"] == "label15" and n["group"] == "position"
    assert abs(n["lat"] - 32.02667) < 0.001 and abs(n["lon"] - 34.89667) < 0.001


def test_normalize_vdl2_acars_empty_ack_tolerated():
    """ACK ריק (label _d, בלי טקסט) — נסבל בלי קריסה, כמו ב-acarsdec."""
    n = app._normalize_vdl2(_vdl2(_avlc_downlink({"acars": {
        "err": False, "crc_ok": True, "more": False, "reg": ".4X-EKF",
        "mode": "2", "label": "_d", "blk_id": "3", "ack": "^", "msg_text": "",
    }})))
    assert n is not None
    assert n["label"] == "_d" and not n["text"]
    assert n["category"] == app.ACARS_LABELS["_d"][0]
    assert n["icao"] == "738065"


def test_normalize_vdl2_acars_arinc622_nested():
    """יישום מפוענח (ADS-C) מקונן *בתוך* אובייקט ה-acars (מבנה libacars) =>
    מוזרם כ-libacars ל-_normalize_acars: מיקום מ-_scan_latlon, קטגוריית ADS-C."""
    n = app._normalize_vdl2(_vdl2(_avlc_downlink({"acars": {
        "err": False, "crc_ok": True, "more": False, "reg": ".4X-EDA",
        "mode": "2", "label": "H1", "blk_id": "2", "ack": "!",
        "flight": "LY0027", "msg_num": "D64", "msg_num_seq": "A",
        "msg_text": "#DFB...",
        "arinc622": {"msg_type": "adsc_msg", "adsc": {
            "tags": [{"basic_report": {"lat": 32.1234, "lon": 34.5678, "alt": 35000}}]}},
    }})))
    assert n["category"] == "ADS-C"
    assert n["pos_src"] == "adsc" and n["group"] == "position"
    assert abs(n["lat"] - 32.1234) < 1e-4 and abs(n["lon"] - 34.5678) < 1e-4


def test_normalize_vdl2_crc_error_blocks_text_heuristic():
    """crc_ok=False => error=1 => שומר ה-error חוסם חילוץ נ"צ מטקסט חופשי
    (בדיוק כמו acarsdec error>0)."""
    n = app._normalize_vdl2(_vdl2(_avlc_downlink({"acars": {
        "err": False, "crc_ok": False, "more": False, "reg": ".4X-EKF",
        "mode": "2", "label": "H1", "blk_id": "1", "ack": "!",
        "msg_text": "POS N3206.0,E03450.0",
    }})))
    assert n["error"] == 1
    assert n["lat"] is None and n["pos_src"] is None


def test_normalize_vdl2_structural_dir_overrides_label():
    """uplink מבני (src=Ground station) דורס את ה-heuristic של label ‏80
    (שממופה downlink) — עובדה משכבת AVLC אמינה יותר מכל ניחוש."""
    avlc = {"src": {"addr": "10917A", "type": "Ground station"},
            "dst": {"addr": "738065", "type": "Aircraft", "status": "Airborne"},
            "cr": "Command", "frame_type": "I",
            "acars": {"err": False, "crc_ok": True, "reg": ".4X-EKF",
                      "mode": "2", "label": "80", "blk_id": "A", "ack": "!",
                      "msg_text": "TEST"}}
    n = app._normalize_vdl2(_vdl2(avlc))
    assert n["dir"] == "uplink"
    assert n["icao"] == "738065"                   # צד-המטוס הוא ה-dst


# --- _normalize_vdl2: מסלול B (שאינו ACARS) ----------------------------------

def test_normalize_vdl2_cpdlc_generic_card():
    avlc = {"src": {"addr": "10917A", "type": "Ground station"},
            "dst": {"addr": "738065", "type": "Aircraft", "status": "Airborne"},
            "cr": "Command", "frame_type": "I",
            "x25": {"err": False, "pkt_type_name": "Data",
                    "clnp": {"cotp": {"cpdlc": {
                        "atc_uplink_msg": {"msg_data": {"msg_text": "CLIMB TO FL350"}}}}}}}
    n = app._normalize_vdl2(_vdl2(avlc))
    assert n["category"] == "CPDLC (VDL2)" and n["group"] == "clearance"
    assert "CLIMB TO FL350" in (n["decoded"] or "")
    assert n["dir"] == "uplink" and n["icao"] == "738065"
    assert n["tail"] is None and n["label"] is None


def test_normalize_vdl2_adsc_x25_position():
    n = app._normalize_vdl2(_vdl2(_avlc_downlink({
        "x25": {"err": False, "pkt_type_name": "Data",
                "clnp": {"cotp": {"adsc": {
                    "basic_report": {"lat": 31.9, "lon": 35.1, "alt": 37000}}}}}})))
    assert n["category"] == "ADS-C (VDL2)"
    assert n["group"] == "position" and n["pos_src"] == "adsc"
    assert abs(n["lat"] - 31.9) < 1e-6 and abs(n["lon"] - 35.1) < 1e-6


def test_normalize_vdl2_xid_generic_card():
    n = app._normalize_vdl2(_vdl2(_avlc_downlink({
        "frame_type": "U", "cmd": "XID",
        "xid": {"err": False, "type": "XID_CMD_LE",
                "type_descr": "Link Establishment", "vdl_params": {}}})))
    assert "XID" in n["category"] and n["group"] == "comm"
    assert n["decoded"] == "Link Establishment"    # type_descr קריא עדיף על type
    assert n["tail"] is None and n["icao"] == "738065" and n["dir"] == "downlink"


def test_normalize_vdl2_bare_frame_and_garbage():
    # פריים AVLC בלי תוכן מוכר => כרטיס גנרי לפי frame_type
    n = app._normalize_vdl2(_vdl2(_avlc_downlink({"frame_type": "S", "cmd": "RR"})))
    assert n["category"] == "VDL2 · S" and n["group"] == "comm"
    # בלי עטיפת vdl2 / בלי avlc => None (לא בר-הצגה)
    assert app._normalize_vdl2({"foo": 1}) is None
    assert app._normalize_vdl2({"vdl2": {"t": {"sec": 1}, "freq": 136975000}}) is None
    assert app._normalize_vdl2({"vdl2": "not-a-dict"}) is None


# --- write_vdl2_env (פורמט EnvironmentFile + המרת Hz) ------------------------

def test_write_vdl2_env_format(paths):
    app.write_vdl2_env(["136.725", "136.975"])
    txt = app.VDL2_ENV_PATH.read_text()
    # MHz => Hz; ערך לא מצוטט => ‎$VDL2_FREQS ב-ExecStart מתפצל לארגומנטים
    assert "VDL2_FREQS=136725000 136975000" in txt
    assert "VDL2_GAIN=\n" in txt                   # ריק => הדגל נעלם => AGC של הדרייבר
    assert f"VDL2_MSG_FILTER={app.VDL2_MSG_FILTER}" in txt


def test_write_vdl2_env_manual_gain(paths):
    app.write_vdl2_env(["136.975"], ifgr=40, rfgr=0)
    assert "VDL2_GAIN=--soapy-gain IFGR=40,RFGR=0" in app.VDL2_ENV_PATH.read_text()


def test_write_vdl2_env_sanitizes(paths):
    app.write_vdl2_env(["136.975", "evil; rm -rf /", "$(reboot)"])
    line = [l for l in app.VDL2_ENV_PATH.read_text().splitlines()
            if l.startswith("VDL2_FREQS")][0]
    assert line == "VDL2_FREQS=136975000"          # הערך הזדוני סונן


def test_write_vdl2_env_empty_falls_to_default(paths):
    app.write_vdl2_env([])
    expect = " ".join(str(int(round(float(f) * 1e6))) for f in app.VDL2_FREQS_DEFAULT)
    assert f"VDL2_FREQS={expect}" in app.VDL2_ENV_PATH.read_text()


# --- בנקים + ולידציית חלון ---------------------------------------------------

def test_vdl2_banks_all_valid():
    assert app.VDL2_FREQS_DEFAULT == app.VDL2_BANKS[0]["freqs"]
    for bank in app.VDL2_BANKS:
        assert app._vdl2_window_error(bank["freqs"]) is None
        assert len(bank["freqs"]) <= app.VDL2_MAX_CHANNELS


def test_vdl2_window_error_cases():
    assert app._vdl2_window_error([]) is not None                  # ריק
    assert app._vdl2_window_error(["131.550", "136.975"]) is not None   # רחב מדי
    assert app._vdl2_window_error(["136.%d" % i for i in range(100, 1000, 100)]) is not None  # >8
    assert app._vdl2_window_error(["136.975"]) is None


def test_generic_window_error_shared_with_acars():
    # ה-wrapper של ACARS ממשיך לעבוד דרך הפונקציה המשותפת
    assert app._acars_window_error(list(app.ACARS_FREQS_DEFAULT)) is None
    assert app._acars_window_error(["130.450", "136.975"]) is not None


# --- listener + /api/vdl2 roundtrip ------------------------------------------

def test_vdl2_listener_and_api(client, monkeypatch):
    monkeypatch.setattr(app, "_is_active", lambda svc: True)
    _reset_buffer()
    threading.Thread(target=app._vdl2_listener, daemon=True).start()
    time.sleep(0.2)

    now = int(time.time())                         # היום (מסנן "היום בלבד" ב-/api/vdl2)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.sendto(json.dumps(_vdl2(_avlc_downlink({"acars": {
        "err": False, "crc_ok": True, "reg": ".4X-EKF", "mode": "2", "label": "H1",
        "blk_id": "1", "ack": "!", "flight": "LY1", "msg_num": "M01",
        "msg_num_seq": "A", "msg_text": "hi vdl2"}}), t_sec=now)).encode(),
        (app.ACARS_UDP_HOST, app.VDL2_UDP_PORT))
    s.sendto(json.dumps(_vdl2(_avlc_downlink({
        "xid": {"type": "XID_CMD_LE", "type_descr": "Link Establishment"}}),
        t_sec=now + 1)).encode(), (app.ACARS_UDP_HOST, app.VDL2_UDP_PORT))
    s.sendto(b"not-json-garbage", (app.ACARS_UDP_HOST, app.VDL2_UDP_PORT))  # יתעלם

    deadline = time.time() + 3
    while time.time() < deadline:
        data = client.get("/api/vdl2?since=0").get_json()
        if len(data["messages"]) >= 2:
            break
        time.sleep(0.05)

    assert data["ok"] and data["active"] is True
    assert len(data["messages"]) == 2              # ה-garbage לא נכנס
    assert data["messages"][0]["tail"] == ".4X-EKF"
    assert data["messages"][1]["decoded"] == "Link Establishment"
    cursor = data["cursor"]
    assert cursor == 2
    assert client.get("/api/vdl2?since=%d" % cursor).get_json()["messages"] == []


# --- /api/mode: כניסה/יציאה מ-VDL2 -------------------------------------------

def test_api_mode_enter_vdl2(client, paths, no_sleep, monkeypatch):
    calls = []
    monkeypatch.setattr(app, "_sysctl",
                        lambda action, svc, timeout=45: calls.append((action, svc)) or _ok())
    monkeypatch.setattr(app, "_is_active", lambda svc: True)   # dumpvdl2 "active" אחרי start
    r = client.post("/api/mode", json={"mode": "vdl2"})
    j = r.get_json()
    assert r.status_code == 200 and j["ok"] and j["app_mode"] == "vdl2"
    assert j["vdl2_freqs"] == list(app.VDL2_FREQS_DEFAULT)
    assert app.load_state()["app_mode"] == "vdl2"
    # שחרר את *שני* הצרכנים האחרים והרים את dumpvdl2
    assert ("stop", "rtl_airband") in calls and ("stop", app.ACARS_SERVICE) in calls
    assert ("restart", app.VDL2_SERVICE) in calls
    # נכתב env תקין (בנק ברירת המחדל, ב-Hz)
    expect = " ".join(str(int(round(float(f) * 1e6))) for f in app.VDL2_FREQS_DEFAULT)
    assert f"VDL2_FREQS={expect}" in app.VDL2_ENV_PATH.read_text()


def test_api_mode_enter_vdl2_failure_falls_to_off(client, paths, no_sleep, monkeypatch):
    # אין fallback לקול: כישלון כניסה למצב נופל ל-off (standby) — המצבים שווי-מעמד
    app.save_state({**app.DEFAULT_STATE, "freq": 121.5, "app_mode": "acars"})
    calls = []
    monkeypatch.setattr(app, "_sysctl",
                        lambda action, svc, timeout=45: calls.append((action, svc)) or _ok())
    monkeypatch.setattr(app, "_is_active", lambda svc: False)  # dumpvdl2 לא עלה => כישלון
    r = client.post("/api/mode", json={"mode": "vdl2"})
    assert r.status_code == 500
    body = r.get_json()
    assert body["app_mode"] == "off" and body["state"]["app_mode"] == "off"
    assert body["state"]["prev_mode"] == "acars"               # המצב שממנו ניסינו לעבור
    assert app.load_state()["app_mode"] == "off"
    assert ("restart", "rtl_airband") not in calls             # שום ניסיון "לחזור לקול"


def test_api_mode_vdl2_rejects_wide_window(client, paths, monkeypatch):
    calls = []
    monkeypatch.setattr(app, "_sysctl",
                        lambda action, svc, timeout=45: calls.append((action, svc)) or _ok())
    r = client.post("/api/mode", json={"mode": "vdl2", "freqs": ["131.550", "136.975"]})
    assert r.status_code == 400
    assert calls == []                             # 400 *לפני* נגיעה ב-SDR


def test_api_mode_vdl2_custom_freqs_saved(client, paths, no_sleep, monkeypatch):
    monkeypatch.setattr(app, "_sysctl", lambda action, svc, timeout=45: _ok())
    monkeypatch.setattr(app, "_is_active", lambda svc: True)
    r = client.post("/api/mode", json={"mode": "vdl2", "freqs": ["136.975"]})
    assert r.status_code == 200
    assert app.load_state()["vdl2_freqs"] == ["136.975"]
    # כניסה חוזרת בלי freqs => משתמש בתדרים השמורים
    r = client.post("/api/mode", json={"mode": "vdl2"})
    assert r.get_json()["vdl2_freqs"] == ["136.975"]


def test_api_mode_voice_stops_vdl2(client, paths, monkeypatch):
    app.save_state({**app.DEFAULT_STATE, "freq": 121.5, "app_mode": "vdl2"})
    monkeypatch.setattr(app, "_restart_and_verify", lambda: (None, None, False))
    monkeypatch.setattr(app, "_is_active", lambda svc: True)   # vdl2 פעיל כרגע
    calls = []
    monkeypatch.setattr(app, "_sysctl",
                        lambda action, svc, timeout=45: calls.append((action, svc)) or _ok())
    r = client.post("/api/mode", json={"mode": "voice"})
    assert r.status_code == 200 and r.get_json()["app_mode"] == "voice"
    assert ("stop", app.VDL2_SERVICE) in calls                # dumpvdl2 נעצר


def test_api_tune_exits_vdl2_mode(client, paths, monkeypatch):
    monkeypatch.setattr(app, "_restart_and_verify", lambda: (None, None, False))
    monkeypatch.setattr(app, "_is_active", lambda svc: True)
    calls = []
    monkeypatch.setattr(app, "_sysctl",
                        lambda action, svc, timeout=45: calls.append((action, svc)) or _ok())
    r = client.post("/api/tune", json={"freq": 134.6})
    assert r.get_json()["ok"]
    assert ("stop", app.VDL2_SERVICE) in calls                # כיוונון קולי עוצר גם VDL2


def test_api_mode_acars_stops_vdl2(client, paths, no_sleep, monkeypatch):
    calls = []
    monkeypatch.setattr(app, "_sysctl",
                        lambda action, svc, timeout=45: calls.append((action, svc)) or _ok())
    monkeypatch.setattr(app, "_is_active", lambda svc: True)
    r = client.post("/api/mode", json={"mode": "acars"})
    assert r.status_code == 200
    assert ("stop", app.VDL2_SERVICE) in calls                # כניסת ACARS עוצרת VDL2


def test_api_mode_off_stops_all_three(client, paths, no_sleep, monkeypatch):
    calls = []
    monkeypatch.setattr(app, "_sysctl",
                        lambda action, svc, timeout=45: calls.append((action, svc)) or _ok())
    monkeypatch.setattr(app, "_is_active", lambda svc: False)
    r = client.post("/api/mode", json={"mode": "off"})
    assert r.status_code == 200 and r.get_json()["app_mode"] == "off"
    for svc in (app.ACARS_SERVICE, app.VDL2_SERVICE, "rtl_airband"):
        assert ("stop", svc) in calls                          # standby עוצר שלושה צרכנים


# --- /api/state + /api/health -----------------------------------------------

def test_api_state_reports_vdl2(client, paths, monkeypatch):
    monkeypatch.setattr(app, "_is_active", lambda svc: svc == app.VDL2_SERVICE)
    body = client.get("/api/state").get_json()
    assert body["app_mode"] == "vdl2"
    assert body["vdl2_freqs"] == list(app.VDL2_FREQS_DEFAULT)
    assert body["vdl2_banks"] == app.VDL2_BANKS


def test_api_state_vdl2_wins_over_saved(client, paths, monkeypatch):
    """מציאות-תחילה: state שמור 'acars' אבל dumpvdl2 הוא שרץ => vdl2."""
    app.save_state({**app.DEFAULT_STATE, "app_mode": "acars"})
    monkeypatch.setattr(app, "_is_active", lambda svc: svc == app.VDL2_SERVICE)
    assert client.get("/api/state").get_json()["app_mode"] == "vdl2"


def test_api_health_vdl2_mode(client, paths, monkeypatch):
    def run(cmd, **kw):
        active = "active" if cmd[-1] in ("sdrplay", "airam-vdl2", "icecast2") else "inactive"
        return types.SimpleNamespace(returncode=0, stdout=active, stderr="")
    monkeypatch.setattr(app.subprocess, "run", run)
    monkeypatch.setattr(app, "_sdr_present", lambda: True)
    body = client.get("/api/health").get_json()
    assert body["ok"] and body["app_mode"] == "vdl2"
    assert body["services"]["airam-vdl2"] == "active"


def test_api_health_off_not_fault_with_vdl2(client, paths, monkeypatch):
    app.save_state({**app.DEFAULT_STATE, "app_mode": "off"})
    def run(cmd, **kw):
        active = "active" if cmd[-1] in ("sdrplay", "icecast2") else "inactive"
        return types.SimpleNamespace(returncode=0, stdout=active, stderr="")
    monkeypatch.setattr(app.subprocess, "run", run)
    monkeypatch.setattr(app, "_sdr_present", lambda: True)
    body = client.get("/api/health").get_json()
    assert body["ok"] and body["app_mode"] == "off"            # standby ≠ תקלה


def test_api_health_fault_when_saved_mode_not_running(client, paths, monkeypatch):
    # המצב השמור (vdl2) אמור לרוץ אבל אף צרכן לא פעיל => תקלה מדווחת עם המצב
    # המקורי — לא טענת-"voice" שקטה כמו פעם
    app.save_state({**app.DEFAULT_STATE, "app_mode": "vdl2"})
    def run(cmd, **kw):
        active = "active" if cmd[-1] in ("sdrplay", "icecast2") else "inactive"
        return types.SimpleNamespace(returncode=0, stdout=active, stderr="")
    monkeypatch.setattr(app.subprocess, "run", run)
    monkeypatch.setattr(app, "_sdr_present", lambda: True)
    body = client.get("/api/health").get_json()
    assert body["ok"] is False and body["app_mode"] == "vdl2"


# --- התמדה: vdl2.jsonl -------------------------------------------------------

def test_vdl2_log_append_and_load_history(paths):
    _reset_buffer()
    base = app._today_start() + 10                 # היום (אחרת מסנן "היום בלבד" יחסום)
    for i in range(3):
        app._append_vdl2_log({"t": base + i, "tail": "4X-EKF", "text": f"m{i}", "icao": "738065"})
    app._load_vdl2_history()
    with app._vdl2_lock:
        assert len(app._vdl2_msgs) == 3
        assert [m["id"] for m in app._vdl2_msgs] == [1, 2, 3]
        assert app._vdl2_msgs[0]["text"] == "m0"   # ממוין לפי t עולה
    _reset_buffer()


def test_vdl2_history_today_only(paths):
    _reset_buffer()
    app._append_vdl2_log({"t": app._today_start() - 3600, "text": "אתמול"})
    app._append_vdl2_log({"t": app._today_start() + 60, "text": "היום"})
    app._load_vdl2_history()
    with app._vdl2_lock:
        assert len(app._vdl2_msgs) == 1
        assert app._vdl2_msgs[0]["text"] == "היום"
    _reset_buffer()


def test_vdl2_log_trim(paths, monkeypatch):
    monkeypatch.setattr(app, "VDL2_LOG_KEEP", 5)
    for i in range(9):
        app._append_vdl2_log({"t": i, "text": f"m{i}"})
    app._trim_vdl2_log()
    lines = app.VDL2_LOG_PATH.read_text().splitlines()
    assert len(lines) == 5
    assert json.loads(lines[0])["text"] == "m4"    # הזנב נשמר


def test_vdl2_history_tolerates_garbage(paths):
    _reset_buffer()
    app.VDL2_LOG_PATH.write_text(
        '{"t": %f, "text": "ok"}\n{broken json\n' % (app._today_start() + 5))
    app._load_vdl2_history()
    with app._vdl2_lock:
        assert len(app._vdl2_msgs) == 1
    _reset_buffer()


# --- ייצוא -------------------------------------------------------------------

def test_vdl2_export_csv_has_icao(client, paths):
    app._append_vdl2_log({"t": 1750000000.0, "freq": 136.975, "label": "H1",
                          "category": "הודעת מערכת/חברה (H1)", "group": "text",
                          "tail": "4X-EKF", "flight": "LY1", "icao": "738065",
                          "text": "שלום\nעולם"})
    r = client.get("/api/vdl2/export?format=csv")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert body.startswith("﻿")               # BOM ל-Excel
    header = body.lstrip("﻿").splitlines()[0]
    assert "icao" in header.split(",")
    assert "738065" in body
    assert "שלום עולם" in body                     # שורות טקסט מקופלות לרווח


def test_vdl2_export_json_keeps_all_history(client, paths):
    app._append_vdl2_log({"t": app._today_start() - 86400, "text": "אתמול"})
    app._append_vdl2_log({"t": app._today_start() + 60, "text": "היום"})
    r = client.get("/api/vdl2/export?format=json")
    recs = json.loads(r.get_data(as_text=True))
    assert len(recs) == 2                          # הייצוא לא מסונן לפי יום
    assert recs[0]["text"] == "אתמול"              # ממוין לפי t


# --- dedup (retry-absorb) ----------------------------------------------------

def test_vdl2_api_serializes_copies(client, paths):
    """/api/vdl2 מחזיר עותקים — לא references שה-listener עלול לשנות תוך סדרול."""
    _reset_buffer()
    with app._vdl2_lock:
        app._vdl2_seq = 1
        app._vdl2_msgs.append({"id": 1, "t": time.time(), "tail": "4X-EKF",
                               "text": "x", "retry_count": 1})
    data = client.get("/api/vdl2?since=0").get_json()
    assert data["messages"][0]["retry_count"] == 1
    with app._vdl2_lock:
        app._vdl2_msgs[0]["retry_count"] = 7       # מוטציה אחרי הקריאה
    assert data["messages"][0]["retry_count"] == 1  # העותק לא הושפע
    _reset_buffer()
