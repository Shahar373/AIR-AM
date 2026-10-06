# ============================================================================
#  AIR-AM - בדיקות חוזה חוצות-רכיבים לטלמטריית ה-RF (PR 1, v2.26.0)
# ----------------------------------------------------------------------------
#  שלושה רכיבים נכתבו במקביל מול חוזה משותף: ה-patch ל-SoapySDRPlay3 (מייצר את
#  שורות היומן), webtune/app.py (מפענח אותן ומגיש /api/metrics + sidecar), ו-
#  index.html (מציג). כל רכיב נבדק לבד בקובץ משלו — כאן בודקים שהם *מסכימים*:
#  מחרוזות הפורמט נשלפות מה-patch עצמו (לא מועתקות לבדיקה), מרונדרות כפי ש-
#  SoapySDR_logf היה מרנדר אותן, ומוזנות למפענח של ה-backend. שינוי בצד אחד
#  בלי השני => הבדיקה נופלת, במקום "אין עומס" שקט בשטח.
# ============================================================================
import re
import time
from pathlib import Path

import pytest

import app

ROOT = Path(__file__).resolve().parent.parent
PATCH = ROOT / "patches" / "soapysdrplay3-airam-rf.patch"
INSTALL = ROOT / "install.sh"
INDEX = ROOT / "webtune" / "static" / "index.html"
UNIT = ROOT / "systemd" / "rtl_airband.service"

# ‏SoapySDR_log/logf(SOAPY_SDR_<LEVEL>, "AIRAM_RF ...") בשורות שה-patch *מוסיף*
_LOG_CALL_RE = re.compile(r'SoapySDR_logf?\(\s*SOAPY_SDR_\w+\s*,\s*"(AIRAM_RF[^"]*)"')


def _patch_formats():
    fmts = []
    for ln in PATCH.read_text().splitlines():
        if ln.startswith("+") and not ln.startswith("+++"):
            fmts += _LOG_CALL_RE.findall(ln)
    return fmts


def _render(fmt, *vals):
    """רינדור printf מינימלי ל-%u — מספיק לפורמטים של ה-patch (נבדק שאין אחרים)."""
    assert set(re.findall(r"%[a-z]", fmt)) <= {"%u"}, fmt
    out = fmt
    for v in vals:
        out = out.replace("%u", str(v), 1)
    assert "%u" not in out
    return out


# כל העטיפות שבהן שורה יכולה להגיע מ-journalctl -o cat: בלי prefix, ה-"[INFO] "
# של defaultLogHandler ב-SoapySDR 0.8.1 (LoggerC.cpp:63), "[WARNING] " עם קודי ANSI
# (LoggerC.cpp:61 — אם רמת ה-log ב-patch תשתנה אי-פעם), ושורה שנדבקה לזבל קודם.
_WRAPS = [
    ("", "\n"),
    ("[INFO] ", "\n"),
    ("[WARNING] ", "\n"),
    ("\x1b[1m\x1b[33m[WARNING] ", "\x1b[0m\n"),
    ("\x1b[3;0f 132.500 -40/-60  ", "\n"),
]


@pytest.fixture(autouse=True)
def _clean_rf():
    def reset():
        with app._rf_lock:
            app._rf.update(follower_since=None, session_start=None, overload=None,
                           overload_events=0, last_overload_t=None, ifgr=None, lna_grdb=None)
            app._rf_events.clear()
    reset()
    with app._rf_lock:
        app._rf["follower_since"] = time.time()
    app._rf_session_reset("contract-test")
    yield
    reset()


def test_patch_has_exactly_the_contract_formats():
    fmts = _patch_formats()
    assert sorted(fmts) == sorted(["AIRAM_RF stream=start", "AIRAM_RF overload=1",
                                   "AIRAM_RF overload=0",
                                   "AIRAM_RF gain grdb=%u lna_grdb=%u"]), fmts


@pytest.mark.parametrize("pre,post", _WRAPS)
def test_backend_parses_rendered_stream_start_line(pre, post):
    """stream=start היא הראיה היחידה שמעבירה overload מ-None ל-False."""
    (fmt,) = [f for f in _patch_formats() if "stream=" in f]
    app._rf_handle_line(pre + _render("AIRAM_RF overload=1") + post, now=5.0)
    assert app._rf_handle_line(pre + _render(fmt) + post, now=6.0) == "start"
    assert app._rf["overload"] is False and app._rf["overload_events"] == 0


def test_install_greps_the_same_strings_the_patch_emits():
    """install.sh מאמת אחרי git apply שהטקסט קיים ב-Streaming.cpp — אותן מחרוזות בדיוק."""
    src = INSTALL.read_text()
    m = re.search(r"for pat in ((?:'[^']*'\s*)+); do", src)
    assert m, "לולאת האימות של ה-patch לא נמצאה ב-install.sh"
    grepped = re.findall(r"'([^']*)'", m.group(1))
    assert sorted(grepped) == sorted(_patch_formats())


@pytest.mark.parametrize("pre,post", _WRAPS)
def test_backend_parses_rendered_overload_lines(pre, post):
    on, off = (_render(f) for f in _patch_formats() if "overload" in f)
    if on.endswith("0"):
        on, off = off, on
    assert app._rf_handle_line(pre + on + post, now=10.0) == "overload"
    assert app._rf["overload"] is True and app._rf["overload_events"] == 1
    assert app._rf_handle_line(pre + off + post, now=11.0) == "overload"
    assert app._rf["overload"] is False and app._rf["overload_events"] == 1


@pytest.mark.parametrize("pre,post", _WRAPS)
def test_backend_parses_rendered_gain_line(pre, post):
    (fmt,) = [f for f in _patch_formats() if "gain" in f]
    assert app._rf_handle_line(pre + _render(fmt, 43, 24) + post) == "gain"
    assert (app._rf["ifgr"], app._rf["lna_grdb"]) == (43, 24)
    # ‏%u ללא הגבלה: ערך גדול (ה-patch לא מסנן טווח) עדיין מפוענח, כמות שהוא
    assert app._rf_handle_line(pre + _render(fmt, 4294967295, 0) + post) == "gain"
    assert app._rf["ifgr"] == 4294967295


def test_marker_path_install_equals_backend():
    m = re.search(r'^AIRAM_SOAPY_MARK="([^"]+)"', INSTALL.read_text(), re.M)
    assert m and Path(m.group(1)) == app.SOAPY_RF_MARK


def test_journal_follower_reads_the_unit_that_runs_rtl_airband():
    cmd = app.RF_JOURNAL_CMD
    unit = cmd[cmd.index("-u") + 1]
    assert (ROOT / "systemd" / f"{unit}.service").is_file()
    assert "-f" in cmd and cmd[cmd.index("-o") + 1] == "cat"


def test_rtl_airband_unit_runs_without_textual_waterfall():
    """‏-f מצייר waterfall ל-stdout בלי '\\n' (rtl_airband.cpp:657-667), באותו זרם journald
    כמו ה-stderr שבו שורות AIRAM_RF מגיעות — ‏-F הוא מצב החזית בלי waterfall (:768-773)."""
    # מאז v2.30.0 ה-argv נבנה ב-airam_launch.py (ה-ExecStart מריץ את ה-launcher)
    import airam_launch
    m = re.search(r"^ExecStart=(.*)$", UNIT.read_text(), re.M)
    assert m.group(1).split()[-1] == "voice"
    args = airam_launch.build_argv("voice", airam_launch.VOICE_CONF_OUT)
    assert "-F" in args and "-f" not in args


def test_render_config_device_string_contract():
    """פריט 4 בחוזה: rfnotch_ctrl בשני המצבים, rfgain_sel רק ב-AGC."""
    agc = app.render_config(132.5, "am", True, 40, 6, fm_notch=True)
    assert 'device_string = "driver=sdrplay,rfnotch_ctrl=true,rfgain_sel=6";' in agc
    man = app.render_config(132.5, "am", False, 40, 6)
    assert 'device_string = "driver=sdrplay,rfnotch_ctrl=false";' in man
    assert 'gain = "IFGR=40,RFGR=6"' in man and "rfgain_sel" not in man


def _rf_keys_used_by_ui():
    """משתנה JS בשם rf (לא מחלקת CSS ‎.rf.stale ולא ‎.rf.json בהערות)."""
    html = INDEX.read_text()
    return set(re.findall(r"(?<![\w.])rf\.(\w+)", html)) - {"json"}


def test_ui_reads_only_fields_the_backend_emits(tmp_path, monkeypatch):
    """כל ‏rf.<field> שה-UI קורא חייב להופיע ב-/api/metrics.rf או ב-sidecar ‏.rf.json —
    שם שדה שגוי היה undefined => מוצג בשקט כ"לא ידוע" לנצח."""
    monkeypatch.setattr(app, "SOAPY_RF_MARK", tmp_path / "mark")
    metrics_keys = set(app._rf_metrics(True, dict(app.DEFAULT_STATE)))
    assert metrics_keys == {"telemetry", "overload", "overload_events", "last_overload_age",
                            "ifgr", "lna_grdb", "lna_state", "fm_notch", "agc", "unknown_reason"}
    monkeypatch.setattr(app, "CONFIG_PATH", tmp_path / "airband.conf")
    monkeypatch.setattr(app, "STATS_PATH", tmp_path / "stats.txt")
    mp3 = tmp_path / "airam_20260611_120001_134600000.mp3"
    mp3.write_bytes(b"\0" * 10)
    sidecar_keys = set(app._build_rf_sidecar(mp3))
    used = _rf_keys_used_by_ui()
    assert used, "ה-UI לא קורא אף שדה rf — הרגקס בבדיקה כנראה שבור"
    assert used <= metrics_keys | sidecar_keys, used - metrics_keys - sidecar_keys


def test_ui_has_text_for_every_rf_unknown_reason():
    """כל unknown_reason שה-backend מחזיר — טקסט ייעודי ב-RF_UNKNOWN_TEXT (לא "ממתין" גנרי)."""
    import inspect
    reasons = set(re.findall(r'return "(\w+)"', inspect.getsource(app._rf_unknown_reason)))
    assert reasons == {"no_telemetry", "voice_not_live", "follower_down",
                       "joined_mid_session", "no_driver_evidence"}
    m = re.search(r"const RF_UNKNOWN_TEXT = \{(.*?)\};", INDEX.read_text(), re.S)
    assert m and reasons <= set(re.findall(r"^\s*(\w+):", m.group(1), re.M))


def test_ui_handles_every_verdict_reason_the_backend_returns():
    html = INDEX.read_text()
    for reason in ("baseline_config_mismatch", "baseline_untagged", "manual_gain"):
        assert f'"{reason}"' in html, reason
    # ‏no_baseline/no_reading ממופים ל-verdict ‏no_baseline/unknown, שה-UI כבר מטפל בהם
    assert app._signal_verdict(None, None, {"lna": 4, "fm_notch": False}) == "unknown"


def test_ui_sends_tune_fields_the_backend_parses():
    """‏fm_notch/rf_gain — שמות זהים בשני הצדדים, ו-"false" טקסטואלי לא נקרא True."""
    html = INDEX.read_text()
    assert html.count('fm_notch: $("fmNotch").checked') == 1      # רק ב-fmNotchField
    assert html.count("...fmNotchField()") >= 2                   # retune + "האזן למגדל"
    p, err = app._parse_tune({"freq": 132.5, "agc": True, "rf_gain": 7, "fm_notch": "false"})
    assert err is None and p["fm_notch"] is False and p["rf_gain"] == 7
    p, _ = app._parse_tune({"freq": 132.5, "fm_notch": "true"})
    assert p["fm_notch"] is True
    p, _ = app._parse_tune({"freq": 132.5})                       # חסר => _voice_tune משלים מה-state
    assert p["fm_notch"] is None
