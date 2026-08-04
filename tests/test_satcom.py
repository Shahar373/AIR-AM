# ============================================================================
#  AIR-AM - בדיקות יחידה למצב SATCOM (מצב רביעי: ACARS דרך לוויין Inmarsat)
# ----------------------------------------------------------------------------
#  רץ בלי חומרה: SATCOM_ENV_PATH מנותב ל-tmp, ו-systemctl/SDR ממוקפים.
#  וקטורי הבדיקה בנויים לפי סכמת ה-JSON האמיתית של inmarsat-sniffer (JAERO
#  JSONdump-compatible), כפי שאומתה ישירות מ-feed_aero_message ב-feed.c של
#  alphafox02/inmarsat-sniffer (commit 2827b3a) — *לא* מתמצות README (ר'
#  docs/satcom-feasibility.md §2 להסבר למה זה חשוב).
# ============================================================================
import json
import socket
import subprocess
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
    monkeypatch.setattr(app, "VDL2_ENV_PATH", tmp_path / "vdl2.env")
    monkeypatch.setattr(app, "SATCOM_ENV_PATH", tmp_path / "satcom.env")
    monkeypatch.setattr(app, "SATCOM_LOG_PATH", tmp_path / "satcom.jsonl")
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
    with app._satcom_lock:
        app._satcom_msgs.clear()
        app._satcom_seq = 0


def _satcom(acars, t_sec=None, usec=250000, src_type="Aircraft Earth Station",
            dst_type="Ground Earth Station", src_addr="738065", dst_addr="10"):
    """בונה הודעת inmarsat-sniffer JSON מלאה (סכמת JAERO JSONdump, כפי שנפלטת
    מ-feed_aero_message — ר' feed.c: app.name="JAERO", isu.acars.*, isu.src/dst,
    t.sec/usec). ברירת המחדל: downlink (AES->GES), כמו רוב ה-ACARS הלוויני."""
    return {
        "app": {"name": "JAERO", "ver": "inmarsat-sniffer VFO01"},
        "isu": {
            "acars": acars,
            "refno": "01", "qno": "02",
            "src": {"addr": src_addr, "type": src_type},
            "dst": {"addr": dst_addr, "type": dst_type},
        },
        "t": {"sec": int(t_sec if t_sec is not None else app._today_start() + 100),
              "usec": usec},
        "station": "airam",
    }


# --- _normalize_satcom --------------------------------------------------------

def test_normalize_satcom_label15_position():
    """הודעת ACARS לוויני עם דיווח מיקום label 15 — כל הפרסרים הקיימים חלים
    (זורם דרך _normalize_acars בדיוק כמו מסלול A של VDL2)."""
    t0 = app._today_start() + 100
    m = _satcom({
        "mode": "2", "ack": "!", "blk_id": "8", "label": "15", "reg": ".4X-EKF",
        "flight": "LY0315", "msg_text": "(2N32016E034538ELY315",
    }, t_sec=t0)
    n = app._normalize_satcom(m)
    assert n is not None
    assert abs(n["t"] - (t0 + 0.25)) < 1e-6        # t.sec + t.usec/1e6
    assert (n["tail"], n["flight"], n["label"]) == (".4X-EKF", "LY0315", "15")
    assert n["error"] == 0
    assert n["dir"] == "downlink"                  # src=Aircraft Earth Station (מבני)
    assert n["pos_src"] == "label15" and n["group"] == "position"
    assert abs(n["lat"] - 32.02667) < 0.001 and abs(n["lon"] - 34.89667) < 0.001


def test_normalize_satcom_never_fabricates_level_or_snr():
    """⚠ מוסכמת הפרויקט (§12 ב-CLAUDE.md): לעולם לא ממציאים ערך. inmarsat-sniffer
    לא חושף level/noise ברמת ההודעה ב---feed/--udp (אומת מהמקור, ר' feed.c) —
    לכן level/snr חייבים להישאר None תמיד, כמו ACARS רגיל (לא כמו VDL2 שכן
    מספק noise אמיתי)."""
    n = app._normalize_satcom(_satcom({
        "mode": "2", "label": "H1", "reg": ".4X-EKF", "msg_text": "hello",
    }))
    assert n["level"] is None and n["snr"] is None
    assert n["freq"] is None                       # גם freq לא נחשף (ר' feed.c)
    assert n["msgno"] is None                      # isu.refno הוא רצף-לוויין, לא MSN — לא ממופה


def test_normalize_satcom_arinc622_adsc_nested():
    """יישום מפוענח (ADS-C) מקונן בתוך isu.acars.arinc622 — אך בשונה מ-VDL2
    מסלול A, inmarsat-sniffer עוטף שם מחדש את *כל* עץ ה-ACARS (אומת מהמקור:
    main.c:889-897 מפעיל la_proto_tree_format_json על ה-tree המושרש בצומת
    ה-ACARS עצמו). הפיקסצ'ר הזה בונה את המבנה הכפול *האמיתי* — לא גרסה "נקייה"
    לפי הנחת המימוש — כדי לתפוס רגרסיה בפירוק המעטפת: בלי _VDL2_ACARS_FIELDS
    (ר' _normalize_satcom), decoded היה מציג את msg_text המשוכפל מתוך המעטפת
    כאילו הוא תוכן מפוענח."""
    n = app._normalize_satcom(_satcom({
        "mode": "2", "label": "H1", "blk_id": "3", "ack": "!",
        "reg": ".4X-EDA", "flight": "LY0027", "msg_text": "#DFB...",
        "arinc622": {"acars": {                      # מעטפת ACARS כפולה (אמיתית)
            "err": False, "crc_ok": True, "more": False,
            "mode": "2", "label": "H1", "blk_id": "3", "ack": "!",
            "reg": ".4X-EDA", "flight": "LY0027", "msg_text": "#DFB...",
            "arinc622": {"msg_type": "adsc_msg", "adsc": {
                "tags": [{"basic_report": {"lat": 32.1234, "lon": 34.5678, "alt": 35000}}]}},
        }},
    }))
    assert n["category"] == "ADS-C"
    assert n["pos_src"] == "adsc" and n["group"] == "position"
    assert abs(n["lat"] - 32.1234) < 1e-4 and abs(n["lon"] - 34.5678) < 1e-4
    # הממצא המרכזי: decoded לא יזלוג את msg_text הגולמי מהמעטפת המשוכפלת
    assert n["decoded"] != "#DFB..."
    assert not (n["decoded"] and "#DFB" in n["decoded"])


def test_normalize_satcom_arinc622_unwrap_falls_back_on_unexpected_shape():
    """הגנתי לשינויי סכמה (כמו _scan_latlon/_libacars_decode): אם arinc622 אי-פעם
    *לא* עטוף ב-"acars" (למשל גרסה עתידית של inmarsat-sniffer, או --jaero-format
    שונה) — לא קורסים, פשוט נופלים חזרה להתייחסות ל-arinc622 כמות שהוא."""
    n = app._normalize_satcom(_satcom({
        "mode": "2", "label": "H1", "reg": ".4X-EDA", "flight": "LY0027",
        "msg_text": "#DFB...",
        "arinc622": {"msg_type": "adsc_msg", "adsc": {
            "tags": [{"basic_report": {"lat": 32.1234, "lon": 34.5678, "alt": 35000}}]}},
    }))
    assert n["category"] == "ADS-C"
    assert n["pos_src"] == "adsc" and n["group"] == "position"
    assert abs(n["lat"] - 32.1234) < 1e-4 and abs(n["lon"] - 34.5678) < 1e-4


def test_normalize_satcom_error_unknown_without_arinc622():
    """אין isu.acars.arinc622 (הודעת טקסט רגילה, רוב תעבורת ה-SATCOM) => אין
    שום איתות CRC בשום שכבה (אומת מהמקור: feed_aero_message לא פולט err/
    crc_ok ברמה החיצונית) — error נשאר 0 (לא מומצא, פשוט לא ידוע), כמו היום."""
    n = app._normalize_satcom(_satcom({
        "mode": "2", "label": "H1", "reg": ".4X-EKF", "msg_text": "hello",
    }))
    assert n["error"] == 0


def test_normalize_satcom_error_reflects_real_crc_when_arinc622_present():
    """כשיש יישום ARINC-622/ADS-C מקונן, המעטפת הפנימית הכפולה (ר' באג עטיפה
    כפולה) כן נושאת err/crc_ok אמיתיים (הסריאליזציה הגנרית של libacars) —
    error חייב לשקף אותם, לא תמיד 0."""
    n_bad = app._normalize_satcom(_satcom({
        "mode": "2", "label": "H1", "reg": ".4X-EDA", "flight": "LY0027",
        "msg_text": "#DFB...",
        "arinc622": {"acars": {
            "err": False, "crc_ok": False, "more": False,   # CRC כושל
            "mode": "2", "label": "H1", "reg": ".4X-EDA", "flight": "LY0027",
            "msg_text": "#DFB...",
            "arinc622": {"msg_type": "adsc_msg", "adsc": {
                "tags": [{"basic_report": {"lat": 32.1234, "lon": 34.5678, "alt": 35000}}]}},
        }},
    }))
    assert n_bad["error"] == 1

    n_ok = app._normalize_satcom(_satcom({
        "mode": "2", "label": "H1", "reg": ".4X-EDA", "flight": "LY0027",
        "msg_text": "#DFB...",
        "arinc622": {"acars": {
            "err": False, "crc_ok": True, "more": False,
            "mode": "2", "label": "H1", "reg": ".4X-EDA", "flight": "LY0027",
            "msg_text": "#DFB...",
            "arinc622": {"msg_type": "adsc_msg", "adsc": {
                "tags": [{"basic_report": {"lat": 32.1234, "lon": 34.5678, "alt": 35000}}]}},
        }},
    }))
    assert n_ok["error"] == 0


def test_normalize_satcom_uplink_structural():
    """dst=Aircraft Earth Station => uplink (קרקע->מטוס), עובדה מבנית של הכלי —
    לא heuristic, דורסת את _acars_direction (כמו VDL2)."""
    n = app._normalize_satcom(_satcom(
        {"mode": "2", "label": "A9", "reg": ".4X-EKF", "msg_text": "ATIS INFO"},
        src_type="Ground Earth Station", dst_type="Aircraft Earth Station"))
    assert n["dir"] == "uplink"


def test_normalize_satcom_empty_ack_tolerated():
    """ACK ריק (בלי טקסט) — נסבל בלי קריסה, כמו ב-acarsdec/VDL2."""
    n = app._normalize_satcom(_satcom({
        "mode": "2", "label": "_d", "ack": "^", "reg": ".4X-EKF", "msg_text": "",
    }))
    assert n is not None
    assert n["label"] == "_d" and not n["text"]


def test_normalize_satcom_missing_isu_acars_returns_none():
    """הודעה בלי isu.acars (STD-C/EGC, לא מופעל כרגע — --mode=aero בלבד) =>
    None (לא בת-הצגה), כמו _normalize_vdl2 עם פריים בלי avlc."""
    assert app._normalize_satcom({"isu": {}}) is None
    assert app._normalize_satcom({"foo": 1}) is None
    assert app._normalize_satcom({"isu": "not-a-dict"}) is None


# --- write_satcom_env (פורמט EnvironmentFile) --------------------------------

def test_write_satcom_env_format(paths):
    app.write_satcom_env(["AF1"])
    txt = app.SATCOM_ENV_PATH.read_text()
    assert "SATCOM_SATELLITE=AF1" in txt
    assert "SATCOM_GAIN=\n" in txt                  # ריק => AGC (כמו VDL2), הדגל נעלם ב-ExecStart
    assert "SATCOM_BIAS_TEE=-B" in txt              # ברירת מחדל: bias-T דולק


def test_write_satcom_env_manual_gain(paths):
    # רווח ידני => --sdrplay-gain (gRdB), *לא* --soapy-gain (שמתעלמים ממנו בדרייבר הנייטיבי)
    app.write_satcom_env(["AF1"], gain=40)
    assert "SATCOM_GAIN=--sdrplay-gain=40" in app.SATCOM_ENV_PATH.read_text()


def test_write_satcom_env_bias_tee_off(paths):
    app.write_satcom_env(["AF1"], bias_tee=False)
    assert "SATCOM_BIAS_TEE=\n" in app.SATCOM_ENV_PATH.read_text()   # ריק => הדגל נעלם


def test_write_satcom_env_sanitizes_junk_falls_to_default(paths):
    app.write_satcom_env(["evil; rm -rf /", "$(reboot)"])   # ג'אנק (לא פורמט טוקן) — לא XYZ
    assert "SATCOM_SATELLITE=AF1" in app.SATCOM_ENV_PATH.read_text()   # נופל לברירת מחדל


def test_write_satcom_env_single_satellite_only(paths):
    app.write_satcom_env(["AF1", "4F3"])             # שני לוויינים — לוקח רק את הראשון
    assert "SATCOM_SATELLITE=AF1" in app.SATCOM_ENV_PATH.read_text()


# --- _satcom_window_error (ולידציית לוויין, לא חלון דגימה) -------------------

def test_satcom_window_error_cases():
    assert app._satcom_window_error([]) is not None              # ריק
    assert app._satcom_window_error(["AF1", "4F3"]) is not None   # יותר מאחד
    assert app._satcom_window_error(["XYZ"]) is not None          # לא מוכר
    assert app._satcom_window_error(["AF1"]) is None
    assert app._satcom_window_error(["4F3"]) is None


def test_satcom_default_is_valid():
    assert app._satcom_window_error(list(app.SATCOM_FREQS_DEFAULT)) is None


# --- listener + /api/satcom roundtrip -----------------------------------------

def test_satcom_listener_and_api(client, monkeypatch):
    monkeypatch.setattr(app, "_is_active", lambda svc: True)
    _reset_buffer()
    threading.Thread(target=app._satcom_listener, daemon=True).start()
    time.sleep(0.2)

    now = int(time.time())                          # היום (מסנן "היום בלבד" ב-/api/satcom)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.sendto(json.dumps(_satcom({
        "mode": "2", "label": "H1", "reg": ".4X-EKF", "flight": "LY1",
        "msg_text": "hi satcom"}, t_sec=now)).encode(),
        (app.ACARS_UDP_HOST, app.SATCOM_UDP_PORT))
    s.sendto(json.dumps(_satcom({
        "mode": "2", "label": "80", "reg": ".4X-EDA", "msg_text": "OOOI"},
        t_sec=now + 1)).encode(), (app.ACARS_UDP_HOST, app.SATCOM_UDP_PORT))
    s.sendto(b"not-json-garbage", (app.ACARS_UDP_HOST, app.SATCOM_UDP_PORT))  # יתעלם

    deadline = time.time() + 3
    data = {"messages": []}
    while time.time() < deadline:
        data = client.get("/api/satcom?since=0").get_json()
        if len(data["messages"]) >= 2:
            break
        time.sleep(0.05)

    assert data["ok"] and data["active"] is True
    assert len(data["messages"]) == 2               # ה-garbage לא נכנס
    assert data["messages"][0]["tail"] == ".4X-EKF"
    cursor = data["cursor"]
    assert cursor == 2
    assert client.get("/api/satcom?since=%d" % cursor).get_json()["messages"] == []


def test_satcom_listener_survives_normalize_exception(client, monkeypatch):
    """שדה עם טיפוס בלתי-צפוי לא אמור להפיל את ה-thread לצמיתות — הפיד ממשיך
    לזרום להודעות הבאות. פורט ייעודי — כמו במקבילה של VDL2."""
    free_port = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    free_port.bind(("127.0.0.1", 0))
    port = free_port.getsockname()[1]
    free_port.close()
    monkeypatch.setattr(app, "SATCOM_UDP_PORT", port)

    monkeypatch.setattr(app, "_is_active", lambda svc: True)
    _reset_buffer()
    th = threading.Thread(target=app._satcom_listener, daemon=True)
    th.start()
    time.sleep(0.2)

    now = int(time.time())
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    bad = _satcom({"mode": "2", "label": "H1", "reg": ".4X-BAD", "msg_text": "boom"},
                   t_sec=now)
    bad["t"] = "not-a-dict-and-not-numeric"          # יגרום לחריגה בפענוח t
    s.sendto(json.dumps(bad).encode(), (app.ACARS_UDP_HOST, port))
    s.sendto(json.dumps(_satcom({
        "mode": "2", "label": "H1", "reg": ".4X-OK", "msg_text": "still alive"},
        t_sec=now + 1)).encode(), (app.ACARS_UDP_HOST, port))

    deadline = time.time() + 3
    data = {"messages": []}
    while time.time() < deadline:
        data = client.get("/api/satcom?since=0").get_json()
        if len(data["messages"]) >= 1:
            break
        time.sleep(0.05)

    assert th.is_alive()
    assert len(data["messages"]) == 1
    assert data["messages"][0]["tail"] == ".4X-OK"


# --- /api/mode: כניסה/יציאה מ-SATCOM -----------------------------------------

def test_api_mode_enter_satcom(client, paths, no_sleep, monkeypatch):
    calls = []
    monkeypatch.setattr(app, "_sysctl",
                        lambda action, svc, timeout=45: calls.append((action, svc)) or _ok())
    monkeypatch.setattr(app, "_is_active", lambda svc: True)
    r = client.post("/api/mode", json={"mode": "satcom"})
    j = r.get_json()
    assert r.status_code == 200 and j["ok"] and j["app_mode"] == "satcom"
    assert j["satcom_freqs"] == list(app.SATCOM_FREQS_DEFAULT)
    assert app.load_state()["app_mode"] == "satcom"
    # שחרר את *שלושת* הצרכנים האחרים והרים את inmarsat-sniffer
    assert ("stop", "rtl_airband") in calls
    assert ("stop", app.ACARS_SERVICE) in calls
    assert ("stop", app.VDL2_SERVICE) in calls
    assert ("restart", app.SATCOM_SERVICE) in calls
    assert "SATCOM_SATELLITE=AF1" in app.SATCOM_ENV_PATH.read_text()


def test_enter_satcom_resets_failed_before_restart(paths, no_sleep, monkeypatch):
    """airam-satcom.service מוגדר עם StartLimitBurst סופי (בניגוד לשלושת
    הצרכנים האחרים) כדי לעצור קריסה חוזרת שהייתה מדליקה bias-T ללא פיקוח —
    לכן _enter_satcom חייב לנקות תקרה קודמת (reset-failed) *לפני* ה-restart,
    אחרת כניסה ידנית מחדש אחרי כמה קריסות הייתה נכשלת בשקט."""
    calls = []
    monkeypatch.setattr(app, "_sysctl",
                        lambda action, svc, timeout=45: calls.append((action, svc)) or _ok())
    monkeypatch.setattr(app, "_is_active", lambda svc: True)
    err, detail = app._enter_satcom(["AF1"])
    assert err is None
    assert ("reset-failed", app.SATCOM_SERVICE) in calls
    assert calls.index(("reset-failed", app.SATCOM_SERVICE)) < calls.index(("restart", app.SATCOM_SERVICE))


def test_enter_satcom_reset_failed_is_best_effort(paths, no_sleep, monkeypatch):
    """reset-failed לא אמור להפיל את הכניסה אם הוא עצמו נכשל (למשל אין מה לאפס)."""
    def flaky(action, svc, timeout=45):
        if action == "reset-failed":
            raise subprocess.TimeoutExpired(cmd="systemctl", timeout=timeout)
        return _ok()
    monkeypatch.setattr(app, "_sysctl", flaky)
    monkeypatch.setattr(app, "_is_active", lambda svc: True)
    err, detail = app._enter_satcom(["AF1"])
    assert err is None


def test_api_mode_enter_satcom_failure_falls_to_off(client, paths, no_sleep, monkeypatch):
    # אין fallback לקול: כישלון כניסה למצב נופל ל-off (standby) — המצבים שווי-מעמד
    app.save_state({**app.DEFAULT_STATE, "app_mode": "vdl2"})
    monkeypatch.setattr(app, "_sysctl",
                        lambda action, svc, timeout=45: _ok())
    monkeypatch.setattr(app, "_is_active", lambda svc: False)  # inmarsat-sniffer לא עלה => כישלון
    r = client.post("/api/mode", json={"mode": "satcom"})
    assert r.status_code == 500
    body = r.get_json()
    assert body["app_mode"] == "off" and body["state"]["app_mode"] == "off"
    assert body["state"]["prev_mode"] == "vdl2"
    assert app.load_state()["app_mode"] == "off"


def test_api_mode_satcom_rejects_unknown_satellite(client, paths, monkeypatch):
    calls = []
    monkeypatch.setattr(app, "_sysctl",
                        lambda action, svc, timeout=45: calls.append((action, svc)) or _ok())
    r = client.post("/api/mode", json={"mode": "satcom", "freqs": ["XYZ"]})
    assert r.status_code == 400
    assert calls == []                              # 400 *לפני* נגיעה ב-SDR


def test_api_mode_satcom_custom_satellite_saved(client, paths, no_sleep, monkeypatch):
    monkeypatch.setattr(app, "_sysctl", lambda action, svc, timeout=45: _ok())
    monkeypatch.setattr(app, "_is_active", lambda svc: True)
    r = client.post("/api/mode", json={"mode": "satcom", "freqs": ["4F3"]})
    assert r.status_code == 200
    assert app.load_state()["satcom_freqs"] == ["4F3"]
    # כניסה חוזרת בלי freqs => משתמש בלוויין השמור
    r = client.post("/api/mode", json={"mode": "satcom"})
    assert r.get_json()["satcom_freqs"] == ["4F3"]


# --- bias_tee: הזנת LNA חיצונית (ר' _enter_satcom) ---------------------------

def test_api_mode_satcom_default_bias_tee_is_on(client, paths, no_sleep, monkeypatch):
    """התנהגות היסטורית (state חדש, בלי bias_tee בבקשה) => bias-T דולק, כמו לפני
    שהתווסף המתג — לא שובר התקנות קיימות."""
    monkeypatch.setattr(app, "_sysctl", lambda action, svc, timeout=45: _ok())
    monkeypatch.setattr(app, "_is_active", lambda svc: True)
    r = client.post("/api/mode", json={"mode": "satcom"})
    assert r.status_code == 200
    assert r.get_json()["satcom_bias_tee"] is True
    assert "SATCOM_BIAS_TEE=-B" in app.SATCOM_ENV_PATH.read_text()
    assert app.load_state()["satcom_bias_tee"] is True


def _env_lines(path):
    return dict(ln.split("=", 1) for ln in path.read_text().splitlines() if "=" in ln and not ln.startswith("#"))


def test_api_mode_satcom_bias_tee_false_writes_empty_flag(client, paths, no_sleep, monkeypatch):
    """הזנת LNA חיצונית: bias_tee=False בבקשה => SATCOM_BIAS_TEE ריק (בלי -B),
    ה-RSP1B לא מזין את ה-LNA. מונע הזרמה כפולה מול המקור החיצוני."""
    monkeypatch.setattr(app, "_sysctl", lambda action, svc, timeout=45: _ok())
    monkeypatch.setattr(app, "_is_active", lambda svc: True)
    r = client.post("/api/mode", json={"mode": "satcom", "bias_tee": False})
    assert r.status_code == 200
    assert r.get_json()["satcom_bias_tee"] is False
    assert _env_lines(app.SATCOM_ENV_PATH)["SATCOM_BIAS_TEE"] == ""
    assert app.load_state()["satcom_bias_tee"] is False


def test_api_mode_satcom_bias_tee_remembered_when_omitted(client, paths, no_sleep, monkeypatch):
    """כניסה חוזרת בלי bias_tee בבקשה => משתמשת בבחירה השמורה (כמו freqs),
    לא נופלת בחזרה ל-True בטעות."""
    monkeypatch.setattr(app, "_sysctl", lambda action, svc, timeout=45: _ok())
    monkeypatch.setattr(app, "_is_active", lambda svc: True)
    client.post("/api/mode", json={"mode": "satcom", "bias_tee": False})
    r = client.post("/api/mode", json={"mode": "satcom"})   # בלי bias_tee
    assert r.get_json()["satcom_bias_tee"] is False
    assert _env_lines(app.SATCOM_ENV_PATH)["SATCOM_BIAS_TEE"] == ""


# --- skip_c: דילוג דמודולטורי C-channel (חיסכון CPU, ר' §12) ------------------

def test_write_satcom_env_skip_c_default_on(paths):
    """ברירת המחדל חוסכת: --skip-c-channel נכתב בלי שביקשו. ‏AIR-AM צורך ACARS
    בלבד וה-C-channels כמעט לא נושאים אותו, בעוד שהם ~50% מה-CPU (options.c)."""
    app.write_satcom_env(["AF1"])
    assert _env_lines(app.SATCOM_ENV_PATH)["SATCOM_SKIP_C"] == "--skip-c-channel"


def test_write_satcom_env_skip_c_off_writes_empty_flag(paths):
    """כבוי => הערך ריק לגמרי (ולא המחרוזת "false"/"0"): ‏$SATCOM_SKIP_C
    לא-מצוטט ב-ExecStart נעלם כשריק, בדיוק כמו SATCOM_GAIN/SATCOM_BIAS_TEE."""
    app.write_satcom_env(["AF1"], skip_c=False)
    assert _env_lines(app.SATCOM_ENV_PATH)["SATCOM_SKIP_C"] == ""


def test_api_mode_satcom_default_skip_c_is_on(client, paths, no_sleep, monkeypatch):
    monkeypatch.setattr(app, "_sysctl", lambda action, svc, timeout=45: _ok())
    monkeypatch.setattr(app, "_is_active", lambda svc: True)
    r = client.post("/api/mode", json={"mode": "satcom"})
    assert r.status_code == 200
    assert r.get_json()["satcom_skip_c"] is True
    assert _env_lines(app.SATCOM_ENV_PATH)["SATCOM_SKIP_C"] == "--skip-c-channel"
    assert app.load_state()["satcom_skip_c"] is True


def test_api_mode_satcom_skip_c_false_decodes_all_channels(client, paths, no_sleep, monkeypatch):
    monkeypatch.setattr(app, "_sysctl", lambda action, svc, timeout=45: _ok())
    monkeypatch.setattr(app, "_is_active", lambda svc: True)
    r = client.post("/api/mode", json={"mode": "satcom", "skip_c": False})
    assert r.status_code == 200
    assert r.get_json()["satcom_skip_c"] is False
    assert _env_lines(app.SATCOM_ENV_PATH)["SATCOM_SKIP_C"] == ""
    assert app.load_state()["satcom_skip_c"] is False


def test_api_mode_satcom_skip_c_remembered_when_omitted(client, paths, no_sleep, monkeypatch):
    """כמו bias_tee/freqs: כניסה חוזרת בלי skip_c משתמשת בבחירה השמורה ולא
    חוזרת בשקט לברירת המחדל."""
    monkeypatch.setattr(app, "_sysctl", lambda action, svc, timeout=45: _ok())
    monkeypatch.setattr(app, "_is_active", lambda svc: True)
    client.post("/api/mode", json={"mode": "satcom", "skip_c": False})
    r = client.post("/api/mode", json={"mode": "satcom"})    # בלי skip_c
    assert r.get_json()["satcom_skip_c"] is False
    assert _env_lines(app.SATCOM_ENV_PATH)["SATCOM_SKIP_C"] == ""


def test_api_mode_satcom_skip_c_and_bias_tee_are_independent(client, paths, no_sleep, monkeypatch):
    """שני המתגים נוגעים באותו תקציב חשמל אבל דרך מנגנונים שונים (זרם מול CPU)
    — בקשה אחת עם שניהם חייבת לכבד את שניהם, בלי שאחד ידרוס את השני."""
    monkeypatch.setattr(app, "_sysctl", lambda action, svc, timeout=45: _ok())
    monkeypatch.setattr(app, "_is_active", lambda svc: True)
    r = client.post("/api/mode", json={"mode": "satcom", "bias_tee": False, "skip_c": True})
    body = r.get_json()
    assert body["satcom_bias_tee"] is False and body["satcom_skip_c"] is True
    env = _env_lines(app.SATCOM_ENV_PATH)
    assert env["SATCOM_BIAS_TEE"] == "" and env["SATCOM_SKIP_C"] == "--skip-c-channel"


def test_api_mode_off_stops_satcom_too(client, paths, no_sleep, monkeypatch):
    calls = []
    monkeypatch.setattr(app, "_sysctl",
                        lambda action, svc, timeout=45: calls.append((action, svc)) or _ok())
    monkeypatch.setattr(app, "_is_active", lambda svc: False)
    r = client.post("/api/mode", json={"mode": "off"})
    assert r.status_code == 200 and r.get_json()["app_mode"] == "off"
    for svc in (app.ACARS_SERVICE, app.VDL2_SERVICE, app.SATCOM_SERVICE, "rtl_airband"):
        assert ("stop", svc) in calls                # standby עוצר ארבעה צרכנים


def test_enter_standby_reports_stuck_service_journal(paths, no_sleep, monkeypatch):
    """כשל בעצירת standby חייב לדווח journal של השירות שבאמת עדיין פעיל — לא
    rtl_airband קשיח. קריטי כש-satcom הוא התקוע: זה בדיוק הרגע שבו bias-T
    עדיין דלוק והאבחון הנכון הכי חשוב."""
    monkeypatch.setattr(app, "_sysctl", lambda action, svc, timeout=45: _ok())
    monkeypatch.setattr(app, "_is_active", lambda svc: svc == app.SATCOM_SERVICE)
    tailed = []
    monkeypatch.setattr(app, "_journal_tail", lambda svc, lines=8: tailed.append(svc) or "")
    err, detail = app._enter_standby()
    assert err is not None
    assert tailed == [app.SATCOM_SERVICE]


def test_api_mode_acars_stops_satcom(client, paths, no_sleep, monkeypatch):
    calls = []
    monkeypatch.setattr(app, "_sysctl",
                        lambda action, svc, timeout=45: calls.append((action, svc)) or _ok())
    monkeypatch.setattr(app, "_is_active", lambda svc: True)
    r = client.post("/api/mode", json={"mode": "acars"})
    assert r.status_code == 200
    assert ("stop", app.SATCOM_SERVICE) in calls      # כניסת ACARS עוצרת SATCOM


def test_api_mode_vdl2_stops_satcom(client, paths, no_sleep, monkeypatch):
    calls = []
    monkeypatch.setattr(app, "_sysctl",
                        lambda action, svc, timeout=45: calls.append((action, svc)) or _ok())
    monkeypatch.setattr(app, "_is_active", lambda svc: True)
    r = client.post("/api/mode", json={"mode": "vdl2"})
    assert r.status_code == 200
    assert ("stop", app.SATCOM_SERVICE) in calls      # כניסת VDL2 עוצרת SATCOM


def test_api_mode_voice_stops_satcom(client, paths, no_sleep, monkeypatch):
    """בטיחות bias-T: _enter_voice חייב לעצור satcom כמו כל צרכן אחר — זה
    בדיוק ה-invariant שעליו נשענת הטענה "bias-T כבוי בכל מצב VHF, מובטח
    מבנית" (§12 ב-CLAUDE.md). _restart_and_verify עצמו לא עובר דרך _sysctl
    (subprocess.run ישיר) — ממוקף בנפרד, כמו ב-test_boot.py."""
    calls = []
    monkeypatch.setattr(app, "_sysctl",
                        lambda action, svc, timeout=45: calls.append((action, svc)) or _ok())
    monkeypatch.setattr(app, "_is_active", lambda svc: True)
    monkeypatch.setattr(app, "_restart_and_verify", lambda: (None, None, False))
    r = client.post("/api/mode", json={"mode": "voice"})
    assert r.status_code == 200
    assert ("stop", app.SATCOM_SERVICE) in calls      # כניסת קול עוצרת SATCOM


# --- רוסטר מאוחד --------------------------------------------------------------

def test_satcom_joins_unified_roster(paths):
    _reset_buffer()
    with app._satcom_lock:
        app._satcom_seq += 1
        app._satcom_msgs.append({"id": app._satcom_seq, "t": time.time(),
                                 "tail": "4X-EKF", "flight": "LY1", "category": "H1",
                                 "group": "text", "dir": "downlink"})
    roster = app._build_roster()
    assert any("satcom" in c["sources"] for c in roster)
    _reset_buffer()


# --- התמדה: satcom.jsonl ------------------------------------------------------

def test_satcom_log_append_and_load_history(paths):
    _reset_buffer()
    base = app._today_start() + 10
    for i in range(3):
        app._append_satcom_log({"t": base + i, "tail": "4X-EKF", "text": f"m{i}"})
    app._load_satcom_history()
    with app._satcom_lock:
        assert len(app._satcom_msgs) == 3
        assert [m["id"] for m in app._satcom_msgs] == [1, 2, 3]
        assert app._satcom_msgs[0]["text"] == "m0"
    _reset_buffer()


def test_satcom_history_today_only(paths):
    _reset_buffer()
    app._append_satcom_log({"t": app._today_start() - 3600, "text": "אתמול"})
    app._append_satcom_log({"t": app._today_start() + 60, "text": "היום"})
    app._load_satcom_history()
    with app._satcom_lock:
        assert len(app._satcom_msgs) == 1
        assert app._satcom_msgs[0]["text"] == "היום"
    _reset_buffer()


def test_satcom_log_trim(paths, monkeypatch):
    monkeypatch.setattr(app, "SATCOM_LOG_KEEP", 5)
    for i in range(9):
        app._append_satcom_log({"t": i, "text": f"m{i}"})
    app._trim_satcom_log()
    lines = app.SATCOM_LOG_PATH.read_text().splitlines()
    assert len(lines) == 5
    assert json.loads(lines[0])["text"] == "m4"


# --- ייצוא ---------------------------------------------------------------------

def test_satcom_export_csv(client, paths):
    app._append_satcom_log({"t": 1750000000.0, "label": "H1",
                            "category": "הודעת מערכת/חברה (H1)", "group": "text",
                            "tail": "4X-EKF", "flight": "LY1", "text": "שלום\nעולם"})
    r = client.get("/api/satcom/export?format=csv")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert body.startswith("﻿")                    # BOM ל-Excel
    assert "4X-EKF" in body
    assert "שלום עולם" in body


def test_satcom_export_json_keeps_all_history(client, paths):
    app._append_satcom_log({"t": app._today_start() - 86400, "text": "אתמול"})
    app._append_satcom_log({"t": app._today_start() + 60, "text": "היום"})
    r = client.get("/api/satcom/export?format=json")
    recs = json.loads(r.get_data(as_text=True))
    assert len(recs) == 2
    assert recs[0]["text"] == "אתמול"


# --- GET /api/satcom/health (proxy ל---web dashboard של inmarsat-sniffer) ------

class _FakeWebResp:
    """מדמה את אובייקט התגובה של urllib.request.urlopen (context manager + .read())."""
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_satcom_health_unavailable_when_not_active(client, paths, monkeypatch):
    """satcom כבוי => אין אפילו ניסיון HTTP (connection-refused מיותר בכל poll)."""
    monkeypatch.setattr(app, "_is_active", lambda svc: False)
    called = []
    monkeypatch.setattr(app.urllib.request, "urlopen",
                        lambda *a, **k: called.append(1) or _FakeWebResp(b"{}"))
    r = client.get("/api/satcom/health")
    j = r.get_json()
    assert r.status_code == 200 and j["ok"] is True and j["available"] is False
    assert called == []


def test_satcom_health_returns_channel_summary_when_available(client, paths, monkeypatch):
    monkeypatch.setattr(app, "_is_active", lambda svc: True)
    payload = json.dumps({
        "total_acars": 42, "feed_drops": 1,
        "channels": [
            {"ch": 0, "baud": 600, "msgs": 10, "age": 3.0, "mse": 0.02, "ebno": 12.5, "lock": 1},
            {"ch": 1, "baud": 1200, "msgs": 0, "age": -1, "mse": 0.0, "ebno": 0.0, "lock": 0},
        ],
    }).encode()
    monkeypatch.setattr(app.urllib.request, "urlopen", lambda url, timeout=None: _FakeWebResp(payload))
    r = client.get("/api/satcom/health")
    j = r.get_json()
    assert j["ok"] and j["available"] is True
    assert j["total_acars"] == 42 and j["feed_drops"] == 1
    assert j["channels_total"] == 2 and j["channels_locked"] == 1
    assert j["channels"][0]["lock"] is True and j["channels"][1]["lock"] is False


def test_satcom_health_unavailable_on_connection_failure(client, paths, monkeypatch):
    """dashboard לא עלה עדיין / קרס — נפילה חיננית, לא 500."""
    monkeypatch.setattr(app, "_is_active", lambda svc: True)

    def boom(url, timeout=None):
        raise ConnectionRefusedError("no one home")
    monkeypatch.setattr(app.urllib.request, "urlopen", boom)
    r = client.get("/api/satcom/health")
    j = r.get_json()
    assert r.status_code == 200 and j["ok"] is True and j["available"] is False


def test_satcom_health_unavailable_on_malformed_response(client, paths, monkeypatch):
    monkeypatch.setattr(app, "_is_active", lambda svc: True)
    monkeypatch.setattr(app.urllib.request, "urlopen",
                        lambda url, timeout=None: _FakeWebResp(b"not json at all"))
    r = client.get("/api/satcom/health")
    assert r.get_json()["available"] is False

    # מבנה תקין אך לא dict (למשל מערך) - גם זה "לא זמין", לא קריסה
    monkeypatch.setattr(app.urllib.request, "urlopen",
                        lambda url, timeout=None: _FakeWebResp(b"[1,2,3]"))
    r = client.get("/api/satcom/health")
    assert r.get_json()["available"] is False


# --- spectrum: "יש RF בכלל?" — האבחון שמפריד אנטנה מתה מכיוון שגוי ------------
# ‏lock/ebno לבדם מציגים בדיוק אותו חיווי (אין נעילה) עבור אנטנה מנותקת, LNA
# בלי מתח, וכיוון שגוי. רצפת הרעש מפרידה ביניהם — ר' §12 ו-api_satcom_spectrum.

def test_write_satcom_env_spectrum_default_on(paths):
    app.write_satcom_env(["AF1"])
    assert _env_lines(app.SATCOM_ENV_PATH)["SATCOM_SPECTRUM"] == "--spectrum"


def test_write_satcom_env_spectrum_off_writes_empty_flag(paths):
    """כבוי => ריק לגמרי (לא "false"): $SATCOM_SPECTRUM לא-מצוטט ב-ExecStart
    נעלם כשריק, בדיוק כמו SATCOM_GAIN/SATCOM_BIAS_TEE/SATCOM_SKIP_C."""
    app.write_satcom_env(["AF1"], spectrum=False)
    assert _env_lines(app.SATCOM_ENV_PATH)["SATCOM_SPECTRUM"] == ""


def test_api_mode_satcom_default_spectrum_is_on(client, paths, no_sleep, monkeypatch):
    monkeypatch.setattr(app, "_sysctl", lambda action, svc, timeout=45: _ok())
    monkeypatch.setattr(app, "_is_active", lambda svc: True)
    r = client.post("/api/mode", json={"mode": "satcom"})
    assert r.status_code == 200 and r.get_json()["satcom_spectrum"] is True
    assert _env_lines(app.SATCOM_ENV_PATH)["SATCOM_SPECTRUM"] == "--spectrum"
    assert app.load_state()["satcom_spectrum"] is True


def test_api_mode_satcom_spectrum_false_and_remembered(client, paths, no_sleep, monkeypatch):
    """מפורש גובר, ואז נזכר בכניסה הבאה — אותו דפוס כמו bias_tee/skip_c."""
    monkeypatch.setattr(app, "_sysctl", lambda action, svc, timeout=45: _ok())
    monkeypatch.setattr(app, "_is_active", lambda svc: True)
    r = client.post("/api/mode", json={"mode": "satcom", "spectrum": False})
    assert r.get_json()["satcom_spectrum"] is False
    assert _env_lines(app.SATCOM_ENV_PATH)["SATCOM_SPECTRUM"] == ""
    r = client.post("/api/mode", json={"mode": "satcom"})     # בלי spectrum
    assert r.get_json()["satcom_spectrum"] is False
    assert _env_lines(app.SATCOM_ENV_PATH)["SATCOM_SPECTRUM"] == ""


def test_api_mode_satcom_three_switches_are_independent(client, paths, no_sleep, monkeypatch):
    """שלושת המתגים (זרם / CPU / אבחון) בבקשה אחת — אף אחד לא דורס את האחרים."""
    monkeypatch.setattr(app, "_sysctl", lambda action, svc, timeout=45: _ok())
    monkeypatch.setattr(app, "_is_active", lambda svc: True)
    r = client.post("/api/mode", json={"mode": "satcom", "bias_tee": False,
                                       "skip_c": False, "spectrum": True})
    body = r.get_json()
    assert (body["satcom_bias_tee"], body["satcom_skip_c"], body["satcom_spectrum"]) \
        == (False, False, True)
    env = _env_lines(app.SATCOM_ENV_PATH)
    assert env["SATCOM_BIAS_TEE"] == "" and env["SATCOM_SKIP_C"] == ""
    assert env["SATCOM_SPECTRUM"] == "--spectrum"


def test_satcom_health_reports_spectrum_flag_from_tool(client, paths, monkeypatch):
    """‏spectrum מגיע מ-spectrum_enabled של הכלי עצמו, לא מה-state שלנו — ה-UI
    צריך לדעת אם /api/satcom/spectrum יעבוד *עכשיו*."""
    monkeypatch.setattr(app, "_is_active", lambda svc: True)
    payload = json.dumps({"spectrum_enabled": 1, "channels": []}).encode()
    monkeypatch.setattr(app.urllib.request, "urlopen", lambda url, timeout=None: _FakeWebResp(payload))
    assert client.get("/api/satcom/health").get_json()["spectrum"] is True

    payload = json.dumps({"spectrum_enabled": 0, "channels": []}).encode()
    monkeypatch.setattr(app.urllib.request, "urlopen", lambda url, timeout=None: _FakeWebResp(payload))
    assert client.get("/api/satcom/health").get_json()["spectrum"] is False


def test_satcom_spectrum_not_active_makes_no_http_call(client, paths, monkeypatch):
    monkeypatch.setattr(app, "_is_active", lambda svc: False)
    called = []
    monkeypatch.setattr(app.urllib.request, "urlopen",
                        lambda *a, **k: called.append(1) or _FakeWebResp(b"{}"))
    j = client.get("/api/satcom/spectrum").get_json()
    assert j["ok"] is True and j["available"] is False and called == []


def test_satcom_spectrum_returns_mags(client, paths, monkeypatch):
    monkeypatch.setattr(app, "_is_active", lambda svc: True)
    urls = []
    payload = json.dumps({"ok": True, "ch": 2, "baud": 1200, "afc": True,
                          "mixer_hz": 12.5, "freq_center_hz": 1545e6, "fs": 12000.0,
                          "lockingbw": 3000.0, "bins": 4,
                          "mags_db": [-90.0, -88.5, -40.25, -91.0]}).encode()

    def fake(url, timeout=None):
        urls.append(url)
        return _FakeWebResp(payload)
    monkeypatch.setattr(app.urllib.request, "urlopen", fake)
    j = client.get("/api/satcom/spectrum?ch=2&bins=64").get_json()
    assert j["available"] is True and j["ch"] == 2 and j["baud"] == 1200
    assert j["mags_db"] == [-90.0, -88.5, -40.25, -91.0] and j["bins"] == 4
    assert j["afc"] is True and j["freq_center_hz"] == 1545e6
    assert "ch=2" in urls[0] and "bins=64" in urls[0]


def test_satcom_spectrum_clamps_bins_like_the_tool(client, paths, monkeypatch):
    """web.c מגביל 32..1024 — אנחנו מגבילים באותם ערכים כדי שבקשה חריגה לא
    תיפול על הכלי בשקט ותחזור כ"לא זמין"."""
    monkeypatch.setattr(app, "_is_active", lambda svc: True)
    urls = []

    def fake(url, timeout=None):
        urls.append(url)
        return _FakeWebResp(json.dumps({"ok": True, "mags_db": [-90.0, -90.0]}).encode())
    monkeypatch.setattr(app.urllib.request, "urlopen", fake)
    client.get("/api/satcom/spectrum?bins=99999")
    assert "bins=1024" in urls[-1]
    client.get("/api/satcom/spectrum?bins=1")
    assert "bins=32" in urls[-1]
    client.get("/api/satcom/spectrum?ch=-5&bins=abc")           # ג'אנק => ברירות מחדל שפויות
    assert "ch=0" in urls[-1] and ("bins=%d" % app.SATCOM_SPECTRUM_BINS) in urls[-1]


def test_satcom_spectrum_unavailable_when_flag_off(client, paths, monkeypatch):
    """‏--spectrum כבוי => הכלי מחזיר ok:false + reason; אנחנו מגישים את זה
    כ-available:false (לא שגיאה) ומעבירים את ה-reason כמות שהוא."""
    monkeypatch.setattr(app, "_is_active", lambda svc: True)
    payload = json.dumps({"ok": False, "ch": 0, "reason": "channel unavailable"}).encode()
    monkeypatch.setattr(app.urllib.request, "urlopen", lambda url, timeout=None: _FakeWebResp(payload))
    j = client.get("/api/satcom/spectrum").get_json()
    assert j["ok"] is True and j["available"] is False and j["reason"] == "channel unavailable"


def test_satcom_spectrum_survives_garbage_mags(client, paths, monkeypatch):
    """ערכים לא-מספריים במערך מסוננים במקום להפיל את הראוט (או להגיע ל-UI)."""
    monkeypatch.setattr(app, "_is_active", lambda svc: True)
    payload = json.dumps({"ok": True, "mags_db": [-90.0, None, "x", -80.5]}).encode()
    monkeypatch.setattr(app.urllib.request, "urlopen", lambda url, timeout=None: _FakeWebResp(payload))
    j = client.get("/api/satcom/spectrum").get_json()
    assert j["mags_db"] == [-90.0, -80.5] and j["bins"] == 2


def test_satcom_spectrum_unavailable_on_connection_failure(client, paths, monkeypatch):
    monkeypatch.setattr(app, "_is_active", lambda svc: True)

    def boom(url, timeout=None):
        raise ConnectionRefusedError("no one home")
    monkeypatch.setattr(app.urllib.request, "urlopen", boom)
    r = client.get("/api/satcom/spectrum")
    assert r.status_code == 200 and r.get_json()["available"] is False


def test_satcom_log_returns_journal_tail(client, paths, monkeypatch):
    """שורות הפתיחה של המפענח הן האבחון החד-משמעי ל-bias-T; בשטח אין SSH."""
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return types.SimpleNamespace(returncode=0, stdout="sdrplay: bias tee enabled\n", stderr="")
    monkeypatch.setattr(app.subprocess, "run", fake_run)
    j = client.get("/api/satcom/log").get_json()
    assert j["ok"] is True and "bias tee enabled" in j["log"]
    assert app.SATCOM_SERVICE in seen["cmd"] and "journalctl" in seen["cmd"][0]


def test_satcom_log_failure_is_graceful(client, paths, monkeypatch):
    def boom(cmd, **kw):
        raise OSError("no journalctl here")
    monkeypatch.setattr(app.subprocess, "run", boom)
    r = client.get("/api/satcom/log")
    assert r.status_code == 500 and r.get_json()["ok"] is False and r.get_json()["log"] == ""


# --- gain: AGC מול gRdB ידני -------------------------------------------------
# ‏sdrplay.c: ‏gain>=0 => gRdB נחתך ל-20..59 + LNAstate=0 + AGC_DISABLE;
# אחרת AGC_5HZ עם setpoint -30dBfs. ר' SATCOM_GAIN_DEFAULT ב-app.py.

def test_sanitize_satcom_gain_agc_forms():
    """‏None ו"agc"/"auto"/"" הם כולם AGC מפורש — לא ג'אנק, לא ברירת מחדל."""
    for v in (None, "", "  ", "agc", "AGC", "auto"):
        assert app._sanitize_satcom_gain(v, default=35) is None


def test_sanitize_satcom_gain_clamps_to_tool_range():
    """חותכים בדיוק כמו הכלי (20..59) ולא דוחים: 400 על ערך שהחומרה מקבלת
    בשקט הוא הבחנה בלי הבדל."""
    assert app._sanitize_satcom_gain(0) == app.IFGR_MIN
    assert app._sanitize_satcom_gain(19) == app.IFGR_MIN
    assert app._sanitize_satcom_gain(999) == app.IFGR_MAX
    assert app._sanitize_satcom_gain(40) == 40
    assert app._sanitize_satcom_gain("35") == 35      # מחרוזת מספרית — תקינה
    assert app._sanitize_satcom_gain(40.7) == 40      # float נחתך ל-int


def test_sanitize_satcom_gain_junk_falls_to_default():
    """ג'אנק => הבחירה השמורה, כמו _sanitize_freqs/_sanitize_satellite —
    לא מפיל בקשה ולא קופץ ל-AGC בשקט."""
    for junk in ("$(reboot)", "abc", {}, [], object()):
        assert app._sanitize_satcom_gain(junk, default=35) == 35
    assert app._sanitize_satcom_gain("abc") is None   # בלי default => AGC


def test_write_satcom_env_gain_none_is_agc(paths):
    app.write_satcom_env(["AF1"], gain=None)
    assert _env_lines(app.SATCOM_ENV_PATH)["SATCOM_GAIN"] == ""


def test_api_mode_satcom_default_gain_is_agc(client, paths, no_sleep, monkeypatch):
    monkeypatch.setattr(app, "_sysctl", lambda action, svc, timeout=45: _ok())
    monkeypatch.setattr(app, "_is_active", lambda svc: True)
    r = client.post("/api/mode", json={"mode": "satcom"})
    assert r.status_code == 200 and r.get_json()["satcom_gain"] is None
    assert _env_lines(app.SATCOM_ENV_PATH)["SATCOM_GAIN"] == ""
    assert app.load_state()["satcom_gain"] is None


def test_api_mode_satcom_manual_gain_writes_sdrplay_flag(client, paths, no_sleep, monkeypatch):
    """הדגל הוא --sdrplay-gain (הדרייבר הנייטיבי), *לא* --soapy-gain."""
    monkeypatch.setattr(app, "_sysctl", lambda action, svc, timeout=45: _ok())
    monkeypatch.setattr(app, "_is_active", lambda svc: True)
    r = client.post("/api/mode", json={"mode": "satcom", "gain": 35})
    assert r.status_code == 200 and r.get_json()["satcom_gain"] == 35
    env = _env_lines(app.SATCOM_ENV_PATH)
    assert env["SATCOM_GAIN"] == "--sdrplay-gain=35"
    assert "soapy" not in env["SATCOM_GAIN"]
    assert app.load_state()["satcom_gain"] == 35


def test_api_mode_satcom_gain_out_of_range_is_clamped(client, paths, no_sleep, monkeypatch):
    monkeypatch.setattr(app, "_sysctl", lambda action, svc, timeout=45: _ok())
    monkeypatch.setattr(app, "_is_active", lambda svc: True)
    r = client.post("/api/mode", json={"mode": "satcom", "gain": 5})
    assert r.get_json()["satcom_gain"] == app.IFGR_MIN
    assert _env_lines(app.SATCOM_ENV_PATH)["SATCOM_GAIN"] == "--sdrplay-gain=%d" % app.IFGR_MIN


def test_api_mode_satcom_gain_remembered_when_omitted(client, paths, no_sleep, monkeypatch):
    """כניסה חוזרת בלי gain => הבחירה השמורה, כמו bias_tee/skip_c/freqs."""
    monkeypatch.setattr(app, "_sysctl", lambda action, svc, timeout=45: _ok())
    monkeypatch.setattr(app, "_is_active", lambda svc: True)
    client.post("/api/mode", json={"mode": "satcom", "gain": 30})
    r = client.post("/api/mode", json={"mode": "satcom"})       # בלי gain
    assert r.get_json()["satcom_gain"] == 30
    assert _env_lines(app.SATCOM_ENV_PATH)["SATCOM_GAIN"] == "--sdrplay-gain=30"


def test_api_mode_satcom_explicit_null_gain_returns_to_agc(client, paths, no_sleep, monkeypatch):
    """⚠ ההבחנה הקריטית: `gain:null` הוא **חזרה מפורשת ל-AGC**, לא "לא נשלח".
    בלי בדיקת-מפתח (`"gain" in data`) המשתמש היה נתקע ברווח הידני לנצח."""
    monkeypatch.setattr(app, "_sysctl", lambda action, svc, timeout=45: _ok())
    monkeypatch.setattr(app, "_is_active", lambda svc: True)
    client.post("/api/mode", json={"mode": "satcom", "gain": 30})
    assert app.load_state()["satcom_gain"] == 30
    r = client.post("/api/mode", json={"mode": "satcom", "gain": None})
    assert r.get_json()["satcom_gain"] is None
    assert _env_lines(app.SATCOM_ENV_PATH)["SATCOM_GAIN"] == ""
    assert app.load_state()["satcom_gain"] is None


def test_api_mode_satcom_junk_gain_keeps_saved_choice(client, paths, no_sleep, monkeypatch):
    monkeypatch.setattr(app, "_sysctl", lambda action, svc, timeout=45: _ok())
    monkeypatch.setattr(app, "_is_active", lambda svc: True)
    client.post("/api/mode", json={"mode": "satcom", "gain": 30})
    r = client.post("/api/mode", json={"mode": "satcom", "gain": "$(reboot)"})
    assert r.status_code == 200 and r.get_json()["satcom_gain"] == 30
    assert _env_lines(app.SATCOM_ENV_PATH)["SATCOM_GAIN"] == "--sdrplay-gain=30"


def test_api_mode_satcom_gain_independent_of_other_switches(client, paths, no_sleep, monkeypatch):
    """ארבעת המתגים בבקשה אחת — אף אחד לא דורס את האחרים."""
    monkeypatch.setattr(app, "_sysctl", lambda action, svc, timeout=45: _ok())
    monkeypatch.setattr(app, "_is_active", lambda svc: True)
    r = client.post("/api/mode", json={"mode": "satcom", "gain": 25,
                                       "bias_tee": False, "skip_c": False, "spectrum": False})
    b = r.get_json()
    assert (b["satcom_gain"], b["satcom_bias_tee"], b["satcom_skip_c"], b["satcom_spectrum"]) \
        == (25, False, False, False)
    env = _env_lines(app.SATCOM_ENV_PATH)
    assert env["SATCOM_GAIN"] == "--sdrplay-gain=25"
    assert env["SATCOM_BIAS_TEE"] == "" and env["SATCOM_SKIP_C"] == "" and env["SATCOM_SPECTRUM"] == ""


def test_api_state_exposes_satcom_gain(client, paths, monkeypatch):
    monkeypatch.setattr(app, "_is_active", lambda svc: False)
    j = client.get("/api/state").get_json()
    assert "satcom_gain" in j and j["satcom_gain"] is None
