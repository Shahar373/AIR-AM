# ============================================================================
#  AIR-AM - עמידות בפני קובצי JSONL פגומים
# ----------------------------------------------------------------------------
#  הרקע: כל קובצי הדאטה (acars/vdl2/satcom/activity) הם JSONL שנכתב ב-append
#  רגיל, בלי fsync — במכוון (ר' §12: איבוד השורה האחרונה בכיבוי פתאומי מקובל).
#  הקוראים כבר סבלו *שורה חלקית* (‏json.loads נכשל => דילוג), אבל **לא** שורה
#  שהיא JSON **תקין** שאינו אובייקט: ‏`null`, ‏`0`, מחרוזת או מערך.
#
#  ‏json.loads מצליח עליהן, ולכן ה-`except ValueError` לא תפס — והקוד ניגש מיד
#  ל-`r.get("t")` ונפל ב-AttributeError/TypeError.
#
#  ⚠ החומרה האמיתית: `_load_*_history` נקראות ב-`__main__` **בלי try** לפני
#  `app.run()`. שורה אחת כזאת מנעה מ-airam-web לעלות — וזה המתזמר שמשחזר את
#  מצב ה-SDR באתחול. עם `Restart=always` זו לולאת קריסה: התחנה כולה מתה,
#  ובשטח אין SSH כדי לאבחן.
#
#  תרחיש היווצרות: כיבוי פתאומי (הפעלה מסוללה/power bank — תרחיש מתועד
#  בפרויקט) משאיר ב-ext4 בלוק שהוקצה אך לא נכתב.
# ============================================================================
import json

import pytest

import app

# שורות JSON *תקינות* שאינן אובייקט — כולן עברו את except ValueError
NON_DICT_LINES = ["null", "0", '"str"', "[]", "[1,2]", "true"]


@pytest.fixture
def logs(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "ACARS_LOG_PATH", tmp_path / "acars.jsonl")
    monkeypatch.setattr(app, "VDL2_LOG_PATH", tmp_path / "vdl2.jsonl")
    monkeypatch.setattr(app, "SATCOM_LOG_PATH", tmp_path / "satcom.jsonl")
    monkeypatch.setattr(app, "ACTIVITY_PATH", tmp_path / "activity.jsonl")
    monkeypatch.setattr(app, "REC_DIR", tmp_path / "recordings")
    (tmp_path / "recordings").mkdir()
    return tmp_path


def _write(path, good_t):
    """רשומה תקינה + כל שורות ה-JSON-שאינו-אובייקט + שורה חלקית."""
    lines = [json.dumps({"t": good_t, "label": "10", "text": "ok"})]
    lines += NON_DICT_LINES
    lines.append('{"t": 123, "partial"')            # כתיבה שנקטעה
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_read_jsonl_log_skips_non_dict_lines(logs):
    _write(app.ACARS_LOG_PATH, 1_700_000_000)
    recs = app._read_acars_log()
    assert [r["text"] for r in recs] == ["ok"]      # רק הרשומה התקינה שרדה


@pytest.mark.parametrize("loader,log_attr,buf,seq", [
    ("_load_acars_history", "ACARS_LOG_PATH", "_acars_msgs", "_acars_seq"),
    ("_load_vdl2_history", "VDL2_LOG_PATH", "_vdl2_msgs", "_vdl2_seq"),
    ("_load_satcom_history", "SATCOM_LOG_PATH", "_satcom_msgs", "_satcom_seq"),
])
def test_history_load_survives_corrupt_lines(logs, monkeypatch, loader, log_attr, buf, seq):
    """⚠ הבדיקה הקריטית: אלה נקראות ב-__main__ בלי try. כישלון כאן = airam-web
    לא עולה = התחנה מתה (המתזמר הוא זה שמשחזר את מצב ה-SDR)."""
    monkeypatch.setattr(app, buf, type(getattr(app, buf))(maxlen=500))
    monkeypatch.setattr(app, seq, 0)
    _write(getattr(app, log_attr), app._today_start() + 100)
    getattr(app, loader)()                          # לא זורק
    msgs = getattr(app, buf)
    assert len(msgs) == 1 and msgs[0]["text"] == "ok"


def test_api_activity_survives_corrupt_lines(logs):
    """‏/api/activity נקרא בפולינג כל 15 שניות במצב קול — שורה פגומה אחת
    הפכה אותו ל-500 חוזר, לא לתקלה חד-פעמית."""
    _write(app.ACTIVITY_PATH, 1_700_000_000)
    r = app.app.test_client().get("/api/activity")
    assert r.status_code == 200
    assert r.get_json()["ok"] is True


def test_export_survives_corrupt_lines(logs):
    """הייצוא קורא את אותו קובץ — ולא אמור להיכשל בגללו."""
    _write(app.ACARS_LOG_PATH, 1_700_000_000)
    r = app.app.test_client().get("/api/acars/export?format=json")
    assert r.status_code == 200
    assert len(json.loads(r.data)) == 1


def test_archive_day_query_survives_corrupt_lines(logs):
    """ארכיון (?day=) קורא מהדיסק ישירות — אותו מסלול, אותו סיכון."""
    t = app._today_start() + 100
    _write(app.ACARS_LOG_PATH, t)
    import time as _t
    day = _t.strftime("%Y-%m-%d", _t.localtime(t))
    r = app.app.test_client().get(f"/api/acars?day={day}")
    assert r.status_code == 200
    assert len(r.get_json()["messages"]) == 1


# --- קריאה קרועה של קובץ המדדים (tmpfs, נכתב ~1Hz בזמן שקוראים) --------------

@pytest.mark.parametrize("bad", [".", "..", "-.", "1.2.3"])
def test_parse_stats_survives_unparsable_value(bad):
    """⚠ התבנית (-?[0-9.]+) מקבלת מחרוזות שאינן מספר, ו-float עליהן זרק
    ValueError לא-מטופל. הקובץ ב-tmpfs ונכתב פעם בשנייה בזמן שקוראים אותו =>
    קריאה קרועה היא תרחיש אמיתי, לא תיאורטי. הנפילה הפילה שלושה נתיבים:
    ‏/api/metrics (פולינג כל שנייה), /api/signal, ו-_sample_probe_stats —
    כלומר גם בדיקת האנטנה."""
    text = (f'channel_dbfs_signal_level{{freq="132.500"}} {bad}\n'
            'channel_dbfs_noise_level{freq="132.500"} -72.5\n')
    vals = app.parse_stats(text, "132.500")          # לא זורק
    assert vals["channel_dbfs_noise_level"] == -72.5  # המדד התקין עדיין נקרא
    assert "channel_dbfs_signal_level" not in vals    # הפגום מדולג, לא מומצא


def test_api_metrics_survives_torn_stats_file(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "STATS_PATH", tmp_path / "stats.txt")
    monkeypatch.setattr(app, "STATE_PATH", tmp_path / "state.json")
    app.save_state({**app.DEFAULT_STATE, "freq": 132.5, "app_mode": "voice"})
    app.STATS_PATH.write_text('channel_dbfs_noise_level{freq="132.500"} .\n')
    r = app.app.test_client().get("/api/metrics")
    assert r.status_code == 200


# --- כיבוי פתאומי באמצע כתיבה: תו UTF-8 קטוע וזנב בלי \n ---------------------
#
#  הרשומות נכתבות עם ensure_ascii=False ומכילות עברית (קטגוריה, decoded) —
#  כל תו עברי הוא 2 בייטים. כיבוי פתאומי (power bank שקוטע) יכול לחתוך באמצע
#  תו, ו-read_text קפדני זורק UnicodeDecodeError. זה ValueError, לא OSError,
#  ולכן ה-except של הקוראים לא תפס אותו — בדיוק המשפחה של NON_DICT_LINES למעלה,
#  רק בשכבת הקידוד: ‏_load_*_history נפלה => airam-web לא עלה => התחנה מתה.

def _cut_mid_hebrew(path, good_t):
    """רשומה תקינה עם עברית + רשומה שנחתכה באמצע תו עברי (UTF-8 לא תקין)."""
    good = json.dumps({"t": good_t, "label": "10", "text": "ok",
                       "decoded": "לא פוענח"}, ensure_ascii=False).encode("utf-8")
    cut = good[:good.index("פוענח".encode("utf-8")) + 1]   # בייט אחד מתוך תו בן 2
    path.write_bytes(good + b"\n" + cut)
    with pytest.raises(UnicodeDecodeError):                # מוודאים שהשחזור אמיתי
        path.read_bytes().decode("utf-8")


@pytest.mark.parametrize("loader,log_attr,buf,seq", [
    ("_load_acars_history", "ACARS_LOG_PATH", "_acars_msgs", "_acars_seq"),
    ("_load_vdl2_history", "VDL2_LOG_PATH", "_vdl2_msgs", "_vdl2_seq"),
    ("_load_satcom_history", "SATCOM_LOG_PATH", "_satcom_msgs", "_satcom_seq"),
])
def test_history_load_survives_cut_utf8(logs, monkeypatch, loader, log_attr, buf, seq):
    """⚠ הקריטית: לפני התיקון זה זרק UnicodeDecodeError מתוך __main__ — airam-web
    בלולאת קריסה, ובשטח אין SSH. עכשיו: הרשומה השלמה נטענת, הקטועה מדולגת."""
    monkeypatch.setattr(app, buf, type(getattr(app, buf))(maxlen=500))
    monkeypatch.setattr(app, seq, 0)
    _cut_mid_hebrew(getattr(app, log_attr), app._today_start() + 100)
    getattr(app, loader)()                          # לא זורק
    msgs = getattr(app, buf)
    assert len(msgs) == 1 and msgs[0]["decoded"] == "לא פוענח"


def test_export_and_trim_survive_cut_utf8(logs):
    """אותו קובץ נקרא גם בייצוא/ארכיון/דוח סשן (_read_jsonl_log), ומקוצץ מתוך
    thread ה-listener (_trim_jsonl_log) — שם חריגה הייתה הורגת את ה-listener
    בשקט, והפיד היה מפסיק בלי שום חיווי."""
    _cut_mid_hebrew(app.ACARS_LOG_PATH, 1_700_000_000)
    r = app.app.test_client().get("/api/acars/export?format=json")
    assert r.status_code == 200 and len(json.loads(r.data)) == 1
    p = app.ACARS_LOG_PATH
    p.write_bytes(b'{"t": 1}\n' + p.read_bytes())    # שורה עודפת => הקיצוץ באמת כותב
    app._trim_jsonl_log(p, keep=2)                   # לא זורק
    assert [r.get("decoded") for r in app._read_acars_log()] == ["לא פוענח"]
    p.read_bytes().decode("utf-8")                   # הכתיבה מחדש ניקתה את הקידוד


@pytest.mark.parametrize("tail", [
    b'{"t": 2, "te',                # שורה שנחתכה
    b"\x00" * 16,                   # זנב NUL — ext4 אחרי delayed allocation
])
def test_append_after_power_cut_keeps_next_record(logs, tail):
    """⚠ append רגיל הדביק את הרשומה הראשונה אחרי האתחול לשארית בלי ה-\\n,
    והקורא דילג על השורה המשולבת כולה — רשומה אחת אבדה בכל כיבוי פתאומי.
    (שוחזר לפני התיקון: התקבל [1, 4] במקום [1, 3, 4].)"""
    app.ACARS_LOG_PATH.write_bytes(b'{"t": 1}\n' + tail)
    app._append_jsonl_log(app.ACARS_LOG_PATH, {"t": 3})
    app._append_jsonl_log(app.ACARS_LOG_PATH, {"t": 4})
    assert [r["t"] for r in app._read_acars_log()] == [1, 3, 4]


def test_append_to_empty_or_missing_file_adds_no_blank_line(logs):
    app._append_jsonl_log(app.ACARS_LOG_PATH, {"t": 1})
    app._append_jsonl_log(app.ACARS_LOG_PATH, {"t": 2})
    assert app.ACARS_LOG_PATH.read_bytes() == b'{"t": 1}\n{"t": 2}\n'


def test_rflog_append_after_power_cut_keeps_next_row(tmp_path, monkeypatch):
    """רשם ה-RF כותב עם fsync בדיוק בגלל power bank שקוטע — אבל fsync לא עוזר
    לשורה שנכתבה *לפני* הקטיעה. השורה הראשונה אחרי האתחול חייבת לשרוד."""
    monkeypatch.setattr(app, "RFLOG_PATH", tmp_path / "rf_log.jsonl")
    monkeypatch.setattr(app, "_rflog_fsync", lambda fd: None)
    app.RFLOG_PATH.write_bytes(b'{"kind": "x"}\n{"kind": "y", "a')
    assert app._rflog_write({"kind": "after"}) is True
    lines = app._read_log_lines(app.RFLOG_PATH)
    assert json.loads(lines[-1]) == {"kind": "after"}


def test_track_append_after_power_cut_keeps_next_row(tmp_path, monkeypatch):
    import adsb
    monkeypatch.setattr(adsb, "TRACK_PATH", tmp_path / "track.jsonl")
    adsb.TRACK_PATH.write_bytes(b'{"t": 1, "ac": []}\n{"t": 2, "a')
    adsb._append_track({"t": 3, "ac": []})
    rows = [json.loads(ln) for ln in adsb.TRACK_PATH.read_text().splitlines()
            if ln.startswith("{") and ln.endswith("}")]
    assert [r["t"] for r in rows] == [1, 3]
