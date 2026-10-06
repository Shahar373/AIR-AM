# ============================================================================
#  AIR-AM - בדיקות הבודק (webtune/rfcheck_probe.py) מול FakeRadio (spec §10.3)
# ----------------------------------------------------------------------------
#  הבודק רץ כ-root ומחזיק את ה-SDR; כאן הוא רץ מול tests/rfsim.FakeRadio על שעון
#  וירטואלי (אותה מכניקת טבעת כמו SoapySDRPlay3). כל בדיקה ≤ ~6 שניות וירטואליות
#  עם ‎≤3 מצבים. בדיקות הפרמטרים, ה-selftest וחוזה הטלמטריה לא צריכות numpy.
# ============================================================================
import io
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import rfcheck_probe as rp

try:
    import numpy as np
    import rfsim
except ImportError:  # pragma: no cover - CI בלי numpy
    np = None
    rfsim = None

needs_np = pytest.mark.skipif(np is None, reason="numpy לא מותקן")

ROOT = Path(__file__).resolve().parent.parent
PROBE = ROOT / "webtune" / "rfcheck_probe.py"
PATCH = ROOT / "patches" / "soapysdrplay3-airam-rf.patch"
NB = [-85000, -70000, -55000, -40000, 40000, 55000, 70000, 85000]
RID = "0123456789abcdef"
CANARY = "SECRET-CANARY-7f3a"


def params(**kw):
    p = {"v": 1, "run_id": RID, "phase": "lna", "ref": "tower",
         "freq_hz": 132_500_000, "center_hz": 132_800_000, "rate": 2_560_000,
         "states": [0, 4, 7], "ifgr_start": {"0": 59, "4": 40, "7": 22},
         "notch_base": False, "notch_alternate": False, "guard_buffers": 1,
         "notch_guard_buffers": 10, "measure_buffers": 7, "ratchet_db": 10,
         "nb_offsets_hz": list(NB), "rail_code": 32767, "max_sec": 180,
         "ovl_margin_buffers": 1}
    p.update(kw)
    return p


class NoDeviceBackend:
    """backend שלא אמור להיפתח לעולם (בדיקות פרמטרים בלי numpy)."""

    def __init__(self):
        self.open_calls = 0
        self.loaded = 0

    def load(self):
        self.loaded += 1

    def open(self, notch_base):  # pragma: no cover - אם נקרא, הבדיקה נכשלת
        self.open_calls += 1
        raise AssertionError("המכשיר נפתח למרות פרמטרים לא תקינים")


def run_fake(tmp_path, p, fake, **kw):
    return rfsim.simulate(p, fake, str(tmp_path), **kw)


def quiet_scene():
    return rfsim.Scene(noise_dbfs=-55.0)


# ============================================================================
#  פרמטרים: פתיחה בטוחה + whitelist (לא צריך numpy)
# ============================================================================
def _write(tmp_path, obj, name="rfcheck-params.json"):
    f = tmp_path / name
    f.write_text(json.dumps(obj) if not isinstance(obj, str) else obj)
    return f


def _main(tmp_path, f, **kw):
    out = rp.OutDir(tmp_path / "out")
    be = NoDeviceBackend()
    err = io.StringIO()
    code = rp.main_with(str(f), be, out, allowed_uids=kw.pop("allowed_uids", {os.getuid()}),
                        stderr=err, **kw)
    end = json.loads((tmp_path / "out" / "end.json").read_text())
    return code, be, end, err.getvalue()


def test_valid_params_roundtrip(tmp_path):
    f = _write(tmp_path, params())
    assert rp.load_params(str(f), allowed_uids={os.getuid()}) == params()


def _mut(**kw):
    def f(p):
        p.update(kw)
    return f


def _del(key):
    def f(p):
        del p[key]
    return f


def _ig(**kw):
    def f(p):
        p["ifgr_start"] = dict(p["ifgr_start"], **kw)
    return f


BAD = [
    ("unknown_key", lambda p: p.__setitem__(CANARY, CANARY), "unknown_key"),
    ("missing", _del("rate"), "missing:rate"),
    ("float_int", _mut(freq_hz=132.5e6), "type:freq_hz"),
    ("bool_int", _mut(guard_buffers=True), "type:guard_buffers"),
    ("int_bool", _mut(notch_base=1), "type:notch_base"),
    ("str_states", _mut(states=CANARY), "type:states"),
    ("str_center", _mut(center_hz=CANARY), "type:center_hz"),
    ("version", _mut(v=2), "version"),
    ("run_id", _mut(run_id=CANARY), "type:run_id"),
    ("phase", _mut(phase=CANARY), "range:phase"),
    ("ref", _mut(ref="ground"), "range:ref"),
    ("freq", _mut(freq_hz=150_000_000, center_hz=150_300_000), "range:freq_hz"),
    ("rate", _mut(rate=2_048_000), "range:rate"),
    ("center_far", _mut(center_hz=132_500_000 + 1_005_000), "range:center_hz"),
    ("center_bin", _mut(center_hz=132_802_500), "center_bin"),
    ("bins_nyquist", _mut(center_hz=131_500_000, nb_offsets_hz=[600_000]), "bins"),
    ("state_10", _mut(states=[0, 4, 10], ifgr_start={"0": 59, "4": 40, "10": 22}), "range:states"),
    ("unsorted", _mut(states=[4, 0], ifgr_start={"0": 59, "4": 40}), "range:states"),
    ("dup", _mut(states=[4, 4], ifgr_start={"4": 40}), "range:states"),
    ("eight_states", _mut(states=list(range(8)), ifgr_start={str(s): 40 for s in range(8)}),
     "range:states"),
    ("ifgr_60", _ig(**{"0": 60}), "range:ifgr_start"),
    ("ifgr_missing", _mut(ifgr_start={"0": 59, "4": 40}), "type:ifgr_start"),
    ("guard", _mut(guard_buffers=9), "range:guard_buffers"),
    ("notch_guard", _mut(notch_guard_buffers=21), "range:notch_guard_buffers"),
    ("measure", _mut(measure_buffers=1), "range:measure_buffers"),
    ("ratchet", _mut(ratchet_db=0), "range:ratchet_db"),
    ("rail", _mut(rail_code=1000), "range:rail_code"),
    ("max_sec", _mut(max_sec=181), "range:max_sec"),
    ("diag_max_sec", _mut(phase="diagnose", ref="atis", max_sec=151), "range:max_sec"),
    ("margin", _mut(ovl_margin_buffers=5), "range:ovl_margin_buffers"),
    ("nb_near", _mut(nb_offsets_hz=[30_000]), "range:nb_offsets_hz"),
    ("nb_grid", _mut(nb_offsets_hz=[42_000]), "range:nb_offsets_hz"),
    ("nb_many", _mut(nb_offsets_hz=NB + [100_000]), "type:nb_offsets_hz"),
    ("notch_alt_lna", _mut(notch_alternate=True), "range:notch_alternate"),
    ("notch_two_states", _mut(phase="notch", notch_alternate=True), "range:states"),
]


@pytest.mark.parametrize("name,mutate,code", BAD, ids=[b[0] for b in BAD])
def test_bad_params_exit_2_without_opening_device(tmp_path, name, mutate, code):
    p = params()
    mutate(p)
    f = _write(tmp_path, p)
    rc, be, end, err = _main(tmp_path, f)
    assert rc == rp.EXIT_PARAMS
    assert be.open_calls == 0 and be.loaded == 0       # המכשיר (וגם SoapySDR) לא נגעו
    assert end["ended"] == "error" and end["error"] == "params:" + code
    blob = json.dumps(end) + err
    assert CANARY not in blob                           # קודים בלבד — התוכן לא מהודהד
    # run_id נישא רק כשהוא עצמו תקין (כדי ש-airam-web יזהה את הכשל כשלו)
    assert end["run_id"] == (RID if name != "run_id" else None)


def test_symlink_rejected_by_o_nofollow(tmp_path):
    real = _write(tmp_path, params(), "real.json")
    link = tmp_path / "rfcheck-params.json"
    link.symlink_to(real)
    with pytest.raises(rp.ParamError) as e:
        rp.load_params(str(link), allowed_uids={os.getuid()})
    assert e.value.code == "open:ELOOP"
    rc, be, end, _ = _main(tmp_path, link)
    assert rc == 2 and be.open_calls == 0 and end["error"] == "params:open:ELOOP"


def test_oversize_rejected(tmp_path):
    f = _write(tmp_path, json.dumps(params()) + " " * 5000)
    with pytest.raises(rp.ParamError) as e:
        rp.load_params(str(f), allowed_uids={os.getuid()})
    assert e.value.code == "too_large"


def test_fifo_rejected_without_blocking(tmp_path):
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(rp.ParamError) as e:      # O_NONBLOCK: open לא נתקע בלי כותב
        rp.load_params(str(fifo), allowed_uids={os.getuid()})
    assert e.value.code == "not_regular"


def test_directory_rejected(tmp_path):
    d = tmp_path / "dir"
    d.mkdir()
    with pytest.raises(rp.ParamError) as e:
        rp.load_params(str(d), allowed_uids={os.getuid()})
    assert e.value.code == "not_regular"


def test_wrong_owner_rejected(tmp_path):
    f = _write(tmp_path, params())
    rc, be, end, _ = _main(tmp_path, f, allowed_uids={os.getuid() + 4242})
    assert rc == 2 and be.open_calls == 0 and end["error"] == "params:owner"


def test_default_owner_allows_root_and_airam_only(monkeypatch):
    class PW:
        pw_uid = 997
    monkeypatch.setattr(rp.pwd, "getpwnam", lambda n: PW if n == "airam" else None)
    assert rp._default_allowed_uids() == {0, 997}

    def missing(_n):
        raise KeyError("airam")
    monkeypatch.setattr(rp.pwd, "getpwnam", missing)
    assert rp._default_allowed_uids() == {0}


@pytest.mark.parametrize("text,code", [
    ("{not json", "json"),
    ('{"v": 1, "v": 1}', "json"),                         # מפתח כפול
    ('{"v": NaN}', "json"),
    ("[1, 2]", "not_object"),
    (b"\xff\xfe".decode("latin-1"), "json"),
])
def test_malformed_json(tmp_path, text, code):
    f = tmp_path / "p.json"
    f.write_text(text, encoding="latin-1" if "\xff" in text else "utf-8")
    with pytest.raises(rp.ParamError) as e:
        rp.load_params(str(f), allowed_uids={os.getuid()})
    assert e.value.code == code


def test_missing_file(tmp_path):
    rc, be, end, _ = _main(tmp_path, tmp_path / "nope.json")
    assert rc == 2 and end["error"] == "params:open:ENOENT" and end["run_id"] is None


def test_states_limit_is_seven_per_user_decision():
    p = params(states=[0, 2, 4, 5, 6, 7, 8],
               ifgr_start={str(s): 40 for s in (0, 2, 4, 5, 6, 7, 8)})
    assert rp.validate_params(p)["states"] == [0, 2, 4, 5, 6, 7, 8]


def test_diagnose_and_notch_params_valid():
    rp.validate_params(params(phase="diagnose", ref="atis", max_sec=150))
    rp.validate_params(params(phase="notch", notch_alternate=True, states=[4],
                              ifgr_start={"4": 40}))


def test_main_rejects_extra_args():
    assert rp.main(["--params", "/tmp/x"]) == rp.EXIT_PARAMS


# ============================================================================
#  ‎--selftest
# ============================================================================
def _soapy_importable():
    # find_spec לא מספיק: SoapySDR.py עשוי להימצא בלי ההרחבה _SoapySDR (נבנתה לגרסת
    # פייתון אחרת) — בדיוק המצב שבו ה-selftest אמור לדווח null
    return subprocess.run([sys.executable, "-I", "-c", "import SoapySDR"],
                          capture_output=True, timeout=60).returncode == 0


@pytest.mark.skipif(_soapy_importable(), reason="SoapySDR מותקן כאן — הבדיקה היא על היעדרו")
def test_selftest_subprocess_without_soapysdr():
    """בדיוק כמו ש-airam-web יריץ: ‎-I (מבודד), בלי SoapySDR => soapysdr:null, יציאה 1."""
    r = subprocess.run([sys.executable, "-I", str(PROBE), "--selftest"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 1, r.stderr
    out = json.loads(r.stdout)
    assert set(out) == {"python", "numpy", "soapysdr", "sdrplay_module", "log_handler_api"}
    assert out["soapysdr"] is None and out["sdrplay_module"] is False
    assert out["python"] == "%d.%d.%d" % sys.version_info[:3]
    assert out["numpy"] is None or isinstance(out["numpy"], str)   # numpy לבדו לא מספיק ל-ok


class _FakeSoapyMod:
    def __init__(self, modules):
        self._m = modules

    def getAPIVersion(self):
        return "0.8.0"

    def listModules(self):
        return self._m

    def registerLogHandler(self, cb):  # pragma: no cover - רק hasattr נבדק
        pass


def test_selftest_detects_sdrplay_module():
    res, ok = rp.selftest(importer=lambda: _FakeSoapyMod(
        ["/usr/local/lib/SoapySDR/modules0.8/libsdrPlaySupport.so"]))
    assert res["soapysdr"] == "0.8.0" and res["sdrplay_module"] is True
    assert res["log_handler_api"] is True
    assert ok == (np is not None)
    res, ok = rp.selftest(importer=lambda: _FakeSoapyMod(["/x/librtlsdrSupport.so"]))
    assert res["sdrplay_module"] is False and ok is False


# ============================================================================
#  חוזה הטלמטריה מול ה-patch (כמו tests/test_rf_contract.py, אבל למפענח של הבודק)
# ============================================================================
def _patch_formats():
    import re
    call = re.compile(r'SoapySDR_logf?\(\s*SOAPY_SDR_\w+\s*,\s*"(AIRAM_RF[^"]*)"')
    fmts = []
    for ln in PATCH.read_text().splitlines():
        if ln.startswith("+") and not ln.startswith("+++"):
            fmts += call.findall(ln)
    return fmts


@pytest.mark.parametrize("wrap", ["", "[INFO] ", "\x1b[1m\x1b[33m[WARNING] "])
def test_probe_parses_patch_formats(wrap):
    fmts = _patch_formats()
    assert sorted(fmts) == sorted(["AIRAM_RF stream=start", "AIRAM_RF overload=1",
                                   "AIRAM_RF overload=0", "AIRAM_RF gain grdb=%u lna_grdb=%u"])
    tm = rp.Telemetry(0.0)
    assert tm.state_over(0.0, 0.5) is None                  # לפני stream=start: לא ידוע
    for t, f in enumerate(fmts, start=1):
        tm.feed(float(t), 6, wrap + f.replace("%u", "41", 1).replace("%u", "20", 1))
    assert tm.stream_start and tm.lines == 4 and tm.gain_lines == 1 and tm.ovl_lines == 2
    assert tm.lna_grdb_seen == {20}


def test_telemetry_timeline_semantics():
    tm = rp.Telemetry(0.0)
    tm.feed(1.0, 6, "AIRAM_RF stream=start")
    tm.feed(3.0, 6, "AIRAM_RF overload=1")
    tm.feed(4.0, 6, "AIRAM_RF overload=0")
    tm.feed(3.5, 6, "something else")
    assert tm.state_over(0.2, 0.9) is None          # לפני הגבול — לא ידוע, לא "תקין"
    assert tm.state_over(1.5, 2.9) is False
    assert tm.state_over(2.5, 3.1) is True
    assert tm.state_over(4.1, 9.0) is False
    tm.mark_unknown(5.0)                            # גלישת לוג => לא ידוע עד הקצה הבא
    assert tm.state_over(5.5, 6.0) is None
    tm.feed(7.0, 6, "AIRAM_RF overload=0")
    assert tm.state_over(7.5, 8.0) is False
    tm.prune(4.5)
    assert tm.state_over(4.6, 4.9) is False and tm.timeline[0][0] <= 4.5
    tm.feed(9.0, 4, "Gain reduction update timeout.")
    assert tm.gr_timeout_between(8.5, 9.5) and not tm.gr_timeout_between(9.1, 9.5)
    tm.feed(10.0, 3, "Device has been removed. Stopping.")
    assert tm.removed


def test_ovl_flags_margin_and_edge():
    B = rp.BUF_S
    tm = rp.Telemetry(0.0)
    tm.feed(0.5, 6, "AIRAM_RF stream=start")
    sw, t_first, t_last = 10.0, 10.2, 10.4
    win_a = t_first - 2 * B                         # margin=1 => מאגר אחד לפני הראשון
    # עומס שהוגבל לחלון ההחלפה/ההשלכה => edge בלבד
    tm.feed(sw + 0.01, 6, "AIRAM_RF overload=1")
    tm.feed(win_a - 0.001, 6, "AIRAM_RF overload=0")
    assert rp.ovl_flags(tm, True, sw, t_first, t_last, 1) == (False, True)
    # אותו אירוע, אבל נגמר בתוך ה-margin => מיוחס לסלוט (שמרני)
    tm2 = rp.Telemetry(0.0)
    tm2.feed(0.5, 6, "AIRAM_RF stream=start")
    tm2.feed(sw + 0.01, 6, "AIRAM_RF overload=1")
    tm2.feed(win_a + 0.001, 6, "AIRAM_RF overload=0")
    assert rp.ovl_flags(tm2, True, sw, t_first, t_last, 1) == (True, False)
    # ובלי margin — כבר לא
    assert rp.ovl_flags(tm2, True, sw, t_first, t_last, 0)[0] is False
    # אין handler/סימן-בנייה => לא ידוע, לעולם לא False
    assert rp.ovl_flags(tm, False, sw, t_first, t_last, 1) == (None, None)
    # בלי stream=start (מודול לא מתוקן) => לא ידוע
    assert rp.ovl_flags(rp.Telemetry(0.0), True, sw, t_first, t_last, 1) == (None, None)


def test_log_sink_never_raises_and_counts_drops():
    clock = iter(range(100)).__next__
    s = rp.LogSink(clock, cap=2)
    assert s.handler(object(), "x") is None         # int(object()) זורק — נבלע
    s.handler(6, "a")
    s.handler(6, "b")
    s.handler(6, "c")                               # מעבר ל-cap: נספר, לא נשמר
    assert s.dropped == 1
    assert [m for _, _, m in s.drain()] == ["a", "b"]
    assert s.drain() == []


def test_settle_index():
    flat = [0.0, 0.01, -0.01, 0.0, 0.01, 0.0, -0.01, 0.0, 0.01, 0.0]
    step = [5.0, 2.0] + flat[2:]
    assert rp.settle_index(step, 4) == 2
    assert rp.settle_index(flat, 4) == 0
    assert rp.settle_index(step, 4, expand=False) >= 2
    assert rp.settle_index([1.0, None, 2.0, 2.0, 2.0, 2.0], 4) is None


# ============================================================================
#  ריצות מול FakeRadio
# ============================================================================
@needs_np
def test_output_discipline_and_meta(tmp_path):
    fake = rfsim.FakeRadio(quiet_scene())
    r = run_fake(tmp_path, params(), fake, max_slots=6)
    assert r["code"] == 0 and r["end"]["ended"] == "stopped"
    out = tmp_path / "out"
    names = set(os.listdir(out))
    assert names <= set(rp.OUT_FILES) and names >= {"meta.json", "status.json", "rows.jsonl", "end.json"}
    for n in names:
        assert stat.S_IMODE(os.stat(out / n).st_mode) == 0o644
    assert set(os.listdir(tmp_path)) == {"out", "build-sig"}      # לא נכתב שום דבר אחר
    for k in ("meta", "status", "end"):
        assert r[k]["run_id"] == RID
    assert len(r["rows"]) == 6 and all(row["run_id"] == RID for row in r["rows"])
    m = r["meta"]
    assert m["fullscale"] == 32767 and m["fullscale_source"] == "driver_source"
    assert m["gr_table"] == [0, 6, 12, 18, 20, 26, 32, 38, 57, 62]
    assert m["handler"] and m["telemetry_marker"] and m["stream_start"]
    assert m["soapy_build_sig"] == "ab" * 32
    assert m["rate_set"] == 2_560_000 and m["bw_set"] == 1_536_000
    assert m["notch_readback"] == "false" and m["bias_t_forced_off"] is False
    assert r["status"]["phase"] == "stopping"
    end = r["end"]
    assert end["slots"] == 6 and end["overflows"] == 0 and end["telemetry_lines"] >= 1
    assert end["rate_measured"] == pytest.approx(2_560_000, rel=1e-3)
    assert r["backend"].unregistered and fake.closed
    row = r["rows"][0]
    for k in ("i", "cyc", "dir", "lna", "ifgr", "epoch", "notch", "t0", "t1", "wall", "valid",
              "inv", "sw_ms", "drained", "n", "c_tot", "c_tot_h1", "c_tot_h2", "c_car", "b_tot",
              "n_nb", "p_wb", "clip", "peak", "ovl", "ovl_edge", "e_mean", "e_std", "e_p99",
              "proc_ms"):
        assert k in row, k
    assert row["n"] == 7 * rfsim.BUF and len(row["b_tot"]) == 7


@needs_np
def test_device_open_kwargs_and_setup_order(tmp_path):
    fake = rfsim.FakeRadio(quiet_scene())
    run_fake(tmp_path, params(notch_base=True), fake, max_slots=1)
    kinds = [c[0] for c in fake.calls]
    assert kinds[0] == "open" and fake.calls[0][1] is True
    i_stream = kinds.index("start_stream")
    pre = [c[:-1] for c in fake.calls[1:i_stream] if c[0] != "read_setting"]   # בלי חותמת הזמן
    # הסדר של spec §4.4 (ושל rtl_airband, input-soapysdr.cpp:227-241); AGC כבוי לפני IFGR
    assert pre == [("set_sample_rate", 2_560_000), ("set_frequency", 132_800_000),
                   ("set_freq_correction", 0), ("set_agc", False),
                   ("set_gain", "IFGR", 59), ("set_gain", "RFGR", 0)]


def test_soapy_backend_never_passes_rfgain_sel():
    seen = {}

    class Mod:
        def Device(self, kw):
            seen.update(kw)
            return object()
    be = rp.SoapyBackend()
    be.mod = Mod()
    be.open(False)
    assert seen == {"driver": "sdrplay", "rfnotch_ctrl": "false"}
    be.open(True)
    assert seen == {"driver": "sdrplay", "rfnotch_ctrl": "true"}


def test_soapy_radio_binding_signatures():
    """החתימות של python3-soapysdr (python/SoapySDR.in.i): readStream(st, [buf], n,
    flags, timeoutUs) מחזיר StreamResult עם ret; setGain(dir, ch, name, double)."""
    calls = []

    class SR:
        ret = 65520

    class Dev:
        def __getattr__(self, name):
            def f(*a):
                calls.append((name,) + a)
                if name == "readStream":
                    return SR()
                if name == "activateStream":
                    return 0
                if name == "setupStream":
                    return "ST"
                return None
            return f
    r = rp.SoapyRadio(object(), Dev())
    r.set_gain("RFGR", 4)
    r.start_stream()
    buf = [0] * 4
    assert r.read(buf, 65536, 0) == 65520
    r.close()
    assert calls[0] == ("setGain", rp.SOAPY_SDR_RX, 0, "RFGR", 4.0)
    assert calls[1] == ("setupStream", rp.SOAPY_SDR_RX, "CS16", [0])
    assert calls[2] == ("activateStream", "ST")
    assert calls[3] == ("readStream", "ST", [buf], 65536, 0, 0)
    assert [c[0] for c in calls[4:]] == ["deactivateStream", "closeStream", "close"]


@needs_np
def test_rfsim_probe_params_are_valid():
    assert rp.validate_params(rfsim.probe_params()) == rfsim.probe_params()


@needs_np
def test_fake_ring_mirrors_soapysdrplay3():
    """המודל של הטבעת: 65 חבילות של 1008 למאגר (Streaming.cpp:108), ‏7 מאגרים מלאים
    נשמרים, והמאגר ה-8 שנסגר בלי קריאה => OVERFLOW בקריאה הבאה + ריקון (‎:101-118,
    ‏505-521); אחריו הזרם ממשיך מהחבילה הבאה."""
    assert rfsim.PKTS_PER_BUF == 65 and rfsim.BUF == 65520
    buf = np.empty(2 * 65536, dtype=np.int16)
    f = rfsim.FakeRadio(marker=True)
    f._open(False)
    f.start_stream()
    assert f.read(buf, 65536, 0) == rfsim.TIMEOUT          # עוד אין מאגר מלא
    f.advance(7.5 * rfsim.BUF / rfsim.RATE)
    assert [f.read(buf, 65536, 0) for _ in range(8)] == [65520] * 7 + [rfsim.TIMEOUT]
    f.advance(8.2 * rfsim.BUF / rfsim.RATE)                # 8 מאגרים נסגרו בלי קריאה
    assert f.read(buf, 65536, 0) == rfsim.OVERFLOW
    assert f.read(buf, 65536, 0) == rfsim.TIMEOUT          # הכול רוקן
    assert f.read(buf, 65536, 200_000) == 65520            # ממתין למאגר הבא (זמן וירטואלי)
    with pytest.raises(NotImplementedError):
        f.advance(0.1)
        f.read(buf, 1000, 0)                               # קריאה חלקית — הבודק לא עושה


@needs_np
def test_abba_order(tmp_path):
    fake = rfsim.FakeRadio(quiet_scene())
    r = run_fake(tmp_path, params(), fake, max_slots=9)
    rows = r["rows"]
    assert [x["lna"] for x in rows] == [0, 4, 7, 7, 4, 0, 0, 4, 7]
    assert [x["dir"] for x in rows] == list("fffrrrfff")
    assert [x["cyc"] for x in rows] == [0, 0, 0, 1, 1, 1, 2, 2, 2]
    assert r["end"]["cycles"] == 3           # שלושה סבבים שלמים


@needs_np
def test_gain_call_order_and_no_op_skips(tmp_path):
    """הגדלת רווח כולל => IFGR ואז RFGR; הקטנה => RFGR ואז IFGR; רכיב שלא השתנה — בלי קריאה."""
    fake = rfsim.FakeRadio(quiet_scene())
    p = params(states=[0, 2, 7], ifgr_start={"0": 59, "2": 59, "7": 40})
    run_fake(tmp_path, p, fake, max_slots=6)
    i_stream = [c[0] for c in fake.calls].index("start_stream")
    seq = [(c[1], c[2]) for c in fake.calls[i_stream:] if c[0] == "set_gain"]
    assert seq == [
        ("RFGR", 2),                  # 0→2: הקטנת רווח; IFGR זהה (59) — לא נקרא
        ("RFGR", 7), ("IFGR", 40),    # 2→7: הקטנה => RFGR קודם
        # 7→7 בגבול ה-ABBA: אין שום קריאה
        ("IFGR", 59), ("RFGR", 2),    # 7→2: הגדלה => IFGR קודם
        ("RFGR", 0),                  # 2→0: הגדלה; IFGR זהה
    ]
    pre = [(c[1], c[2]) for c in fake.calls[:i_stream] if c[0] == "set_gain"]
    assert pre == [("IFGR", 59), ("RFGR", 0)]


@needs_np
@pytest.mark.parametrize("proc_s,expect_overflow", [(0.0, False), (0.17, False), (0.26, True)])
def test_drain_marker_no_pre_change_sample(tmp_path, proc_s, expect_overflow):
    """הלב (spec §11 קריטריון 5): גם עם backlog של עד 8 מאגרים (וגם כשהוא גולש), שום
    דגימה מלפני ההחלפה לא נכנסת לחלון הנמדד. מצב marker: Q=100·LNA+IFGR, I=1000+מזהה-שינוי."""
    fake = rfsim.FakeRadio(marker=True, settle_tau_s=0.0)
    seen = []

    def hook(raw, info):
        I, Q = raw[0::2], raw[1::2]
        assert set(np.unique(Q).tolist()) == {100 * info["lna"] + info["ifgr"]}, info
        assert set(np.unique(I).tolist()) == {1000 + fake.cfg["cid"]}, info
        seen.append(info["i"])

    r = run_fake(tmp_path, params(), fake, max_slots=9, measure_hook=hook,
                 process_hook=lambda: fake.advance(proc_s))
    assert seen == list(range(9))
    assert all(row["valid"] for row in r["rows"])
    if proc_s:
        assert max(row["drained"] for row in r["rows"]) > 0 or expect_overflow
    assert (r["end"]["drain_overflows"] > 0) == expect_overflow


@needs_np
def test_gr_timeout_invalidates_slot(tmp_path):
    fake = rfsim.FakeRadio(quiet_scene(), gr_timeout_on={0})
    r = run_fake(tmp_path, params(), fake, max_slots=4)
    rows = r["rows"]
    assert rows[1]["valid"] is False and rows[1]["inv"] == "gr_timeout"     # 0→4: העדכון הראשון
    assert rows[1]["c_tot"] is not None                                      # הנתונים מלאים, רק פסולים
    assert all(x["valid"] for i, x in enumerate(rows) if i != 1)
    assert r["end"]["gr_timeouts"] == 1


@needs_np
def test_overflow_in_measure_invalidates_and_counts(tmp_path):
    # סלוט 0: קריאה 1 = ניקוז (TIMEOUT), 2-3 = השלכה, 4-10 = מדידה
    fake = rfsim.FakeRadio(quiet_scene(), overflow_on_reads={6})
    r = run_fake(tmp_path, params(), fake, max_slots=3)
    assert r["rows"][0]["valid"] is False and r["rows"][0]["inv"] == "overflow"
    assert r["rows"][0]["c_tot"] is None
    assert r["end"]["overflows"] >= 1
    assert r["rows"][1]["valid"] and r["rows"][2]["valid"]


@needs_np
def test_not_supported_ends_device_lost(tmp_path):
    fake = rfsim.FakeRadio(quiet_scene(), not_supported_at_read=25)
    r = run_fake(tmp_path, params(), fake)
    assert r["code"] == rp.EXIT_DEVICE_LOST
    assert r["end"]["ended"] == "device_lost" and r["end"]["error"] == "not_supported"
    assert fake.closed


@needs_np
def test_device_removed_log_ends_device_lost(tmp_path):
    """הסרה אמיתית: Streaming.cpp רושם "Device has been removed" ומאגרים נפסקים —
    acquire מחזיר TIMEOUT ולא NOT_SUPPORTED כשהטבעת ריקה."""
    fake = rfsim.FakeRadio(quiet_scene(), remove_at=1001.0)
    r = run_fake(tmp_path, params(), fake)
    assert r["code"] == rp.EXIT_DEVICE_LOST and r["end"]["error"] == "removed"


@needs_np
def test_stream_stall_without_log_ends_device_lost(tmp_path):
    fake = rfsim.FakeRadio(quiet_scene(), remove_at=1001.0)
    r = run_fake(tmp_path, params(), fake, backend_kw={"handler_ok": False})
    assert r["code"] == rp.EXIT_DEVICE_LOST and r["end"]["error"] == "stream_stall"


@needs_np
def test_sigterm_flag_stops_cleanly(tmp_path):
    fake = rfsim.FakeRadio(quiet_scene())
    stop = rp.StopFlag()

    def hook(raw, info):
        if info["i"] == 3:
            stop.set()
    r = run_fake(tmp_path, params(), fake, stop=stop, measure_hook=hook)
    assert r["code"] == 0 and r["end"]["ended"] == "stopped"
    assert fake.closed and r["backend"].unregistered
    assert len(r["rows"]) == 4 and r["status"]["phase"] == "stopping"


@needs_np
def test_max_sec(tmp_path):
    fake = rfsim.FakeRadio(quiet_scene())
    r = run_fake(tmp_path, params(max_sec=5), fake)
    assert r["code"] == 0 and r["end"]["ended"] == "max_sec"
    assert r["end"]["duration_sec"] >= 5.0
    assert 15 <= r["end"]["slots"] <= 25


@needs_np
def test_open_failure_exit_3(tmp_path):
    fake = rfsim.FakeRadio(quiet_scene())
    r = run_fake(tmp_path, params(), fake, backend_kw={"open_error": True})
    assert r["code"] == rp.EXIT_OPEN and r["end"]["error"] == "open"
    assert not fake.streaming and r["rows"] == [] and r["meta"] is None


@needs_np
def test_import_failure_exit_3(tmp_path):
    fake = rfsim.FakeRadio(quiet_scene())
    r = run_fake(tmp_path, params(), fake, backend_kw={"load_error": True})
    assert r["code"] == rp.EXIT_OPEN and r["end"]["error"] == "import:soapysdr"
    assert r["backend"].open_calls == 0


@needs_np
def test_bias_t_forced_off(tmp_path):
    fake = rfsim.FakeRadio(quiet_scene(), bias_t="true")
    r = run_fake(tmp_path, params(), fake, max_slots=1)
    writes = [c for c in fake.calls if c[0] == "write_setting" and c[1] == "biasT_ctrl"]
    assert writes and writes[0][2] == "false"
    kinds = [c[0] for c in fake.calls]
    assert kinds.index("write_setting") < kinds.index("start_stream")
    assert r["meta"]["bias_t_forced_off"] is True and fake.bias_t == "false"
    fake2 = rfsim.FakeRadio(quiet_scene())
    (tmp_path / "b").mkdir()
    run_fake(tmp_path / "b", params(), fake2, max_slots=1)
    assert not [c for c in fake2.calls if c[0] == "write_setting"]       # לעולם לא *מדליק*


@needs_np
def test_ratchet_per_state_up_only(tmp_path):
    """חיתוך במצב 0 => IFGR שלו ‎+10 בכל סבב עד 59, תקופה חדשה בכל צעד; לא יורד לעולם;
    מצב 7 לא מושפע (spec §4.5 צעד 9)."""
    sc = rfsim.Scene(noise_dbfs=-70.0, carriers=[rfsim.Carrier(-8.0, m_peak=0.0)])
    fake = rfsim.FakeRadio(sc, rfsim.FrontEnd(adc_noise_dbfs=None))
    p = params(ifgr_start={"0": 30, "4": 40, "7": 40})
    r = run_fake(tmp_path, p, fake, max_slots=15)
    by = {s: [(x["ifgr"], x["epoch"], x["clip"]) for x in r["rows"] if x["lna"] == s] for s in (0, 4, 7)}
    assert [g for g, _, _ in by[0]] == [30, 40, 50, 59, 59]
    assert [e for _, e, _ in by[0]] == [0, 1, 2, 3, 3]
    assert [c > 0 for _, _, c in by[0]] == [True, True, True, False, False]
    assert {g for g, _, _ in by[7]} == {40} and {e for _, e, _ in by[7]} == {0}
    assert {g for g, _, _ in by[4]} == {40}
    assert r["end"]["ratchets"] == {"0": 3, "4": 0, "7": 0}
    assert r["end"]["ifgr_final"]["0"] == 59


@needs_np
def test_overload_attribution_with_margin_and_edge(tmp_path):
    """אירוע עומס מהחומרה (מודמה לפי שיא לפני ה-ADC) מיוחס לסלוט שבו היה; קצה ה"תוקן"
    שנחת בחלון ההחלפה של המצב הבא => ovl_edge בלבד שם."""
    sc = rfsim.Scene(noise_dbfs=-70.0, carriers=[rfsim.Carrier(-15.0, m_peak=0.0)])
    fe = rfsim.FrontEnd(adc_noise_dbfs=None, ovl_dbfs=-3.0)
    fake = rfsim.FakeRadio(sc, fe)
    p = params(ifgr_start={"0": 45, "4": 40, "7": 40})
    r = run_fake(tmp_path, p, fake, max_slots=4)
    rows = r["rows"]
    assert rows[0]["lna"] == 0 and rows[0]["ovl"] is True
    assert rows[1]["lna"] == 4 and rows[1]["ovl"] is False and rows[1]["ovl_edge"] is True
    assert rows[2]["ovl"] is False and rows[2]["ovl_edge"] is False
    assert r["end"]["telemetry_overload_lines"] >= 2


@needs_np
@pytest.mark.parametrize("case", ["unpatched", "no_marker", "no_handler"])
def test_overload_unknown_without_telemetry(tmp_path, case):
    """§12: בלי ראיה מהדרייבר — ovl=None ("לא נבדק"), לעולם לא False."""
    sc = rfsim.Scene(noise_dbfs=-70.0, carriers=[rfsim.Carrier(-15.0, m_peak=0.0)])
    fe = rfsim.FrontEnd(adc_noise_dbfs=None, ovl_dbfs=-3.0)
    fake = rfsim.FakeRadio(sc, fe, patched=(case != "unpatched"))
    r = run_fake(tmp_path, params(ifgr_start={"0": 45, "4": 40, "7": 40}), fake, max_slots=3,
                 marker=(case != "no_marker"),
                 backend_kw={"handler_ok": case != "no_handler"})
    assert all(x["ovl"] is None and x["ovl_edge"] is None for x in r["rows"])
    assert r["end"]["stream_start_seen"] == (case == "no_marker")


@needs_np
def test_light_mode_tower_only(tmp_path):
    def go(ref, sub):
        d = tmp_path / sub
        d.mkdir()
        fake = rfsim.FakeRadio(quiet_scene())
        return run_fake(d, params(ref=ref), fake, max_slots=14,
                        process_hook=lambda: fake.advance(0.15))
    r = go("tower", "t")
    assert r["end"]["light_mode"] is True
    late = [x for x in r["rows"] if x["i"] >= 9]
    assert all((x["n_nb"] is not None) == (x["i"] % 4 == 0) for x in late)
    assert all(x["valid"] for x in r["rows"])           # בלי overflow ב-150ms
    r = go("atis", "a")
    assert r["end"]["light_mode"] is False and all(x["n_nb"] is not None for x in r["rows"])


@needs_np
def test_notch_phase_abba_readback_and_guard(tmp_path):
    fake = rfsim.FakeRadio(marker=True)
    p = params(phase="notch", notch_alternate=True, states=[4], ifgr_start={"4": 40},
               notch_guard_buffers=10)

    def hook(raw, info):
        I, Q = raw[0::2], raw[1::2]
        assert set(np.unique(I).tolist()) == {1000 + fake.cfg["cid"]}     # אף דגימה מלפני ההחלפה
        assert set(np.unique(Q).tolist()) == {100 * 4 + 40}
    r = run_fake(tmp_path, p, fake, max_slots=8, measure_hook=hook)
    rows = r["rows"]
    assert [x["notch"] for x in rows] == [False, True, True, False] * 2
    assert [x["pos"] for x in rows] == [0, 1, 2, 3] * 2
    assert [x["dir"] for x in rows] == list("ffrr") * 2
    assert [x["notch_rb"] for x in rows] == ["false", "true", "true", "false"] * 2
    writes = [c for c in fake.calls if c[0] == "write_setting" and c[1] == "rfnotch_ctrl"]
    assert [w[2] for w in writes] == ["true", "false", "true", "false"]   # רק בהחלפות בפועל
    # אחרי החלפה: לפחות notch_guard מאגרים נזרקים לפני המדידה
    for w in writes:
        nxt = min(x["t0"] for x in rows if x["t0"] > w[3])
        assert nxt - w[3] >= 10 * rfsim.BUF / rfsim.RATE * 0.99
    assert {x["epoch"] for x in rows} == {0}


@needs_np
def test_notch_readback_mismatch_is_recorded(tmp_path):
    fake = rfsim.FakeRadio(quiet_scene(), notch_readback="false")
    p = params(phase="notch", notch_alternate=True, states=[4], ifgr_start={"4": 40})
    r = run_fake(tmp_path, p, fake, max_slots=2)
    assert [x["notch"] for x in r["rows"]] == [False, True]
    assert [x["notch_rb"] for x in r["rows"]] == ["false", "false"]       # נרשם, לא מוסתר


# ============================================================================
#  אבחון (D1–D8)
# ============================================================================
SMALL_DIAG = {"d3_reps": 1, "d3_buffers": 6, "settle_buffers": 2,
              "d4_ifgr": (59, 47), "d4_sec": 0.03,
              "d5_ifgr": (59, 38, 20), "d5_sec": 0.03,
              "d6_toggles": 2, "d6_buffers": 6,
              "d7_agc_sec": 0.15, "d7_step_sec": 0.06, "d8_sec": 0.5}


def diag_params(**kw):
    return params(phase="diagnose", ref="atis", max_sec=150, **kw)


def atis_scene(level=-25.0):
    return rfsim.Scene(noise_dbfs=-60.0, carriers=[rfsim.Carrier(level, m_peak=0.85)])


@needs_np
def test_diagnose_produces_all_steps_and_summary(tmp_path):
    fe = rfsim.FrontEnd(ovl_dbfs=-1.0)
    fake = rfsim.FakeRadio(atis_scene(), fe)
    r = run_fake(tmp_path, diag_params(), fake, diag_timing=SMALL_DIAG)
    assert r["code"] == 0 and r["end"]["ended"] == "stopped"
    d = r["diagnose"]
    assert d["run_id"] == RID and d["complete"] is True
    assert list(d["steps"]) == ["D1", "D2", "D3", "D4", "D5", "D6", "D7", "D8"]
    assert not [k for k, v in d["steps"].items() if "error" in v], d["steps"]
    s = d["suggested"]
    for k in ("guard_buffers", "notch_guard_buffers", "rail_code", "overload_reported_with_agc_off",
              "cpu_ok", "gainchange_on_manual", "peak_code_max", "rate_measured"):
        assert k in s, k
    assert d["summary_text"].startswith("AIRAM-RFDIAG v1\n")
    assert "guard_buffers=" in d["summary_text"] and "errors=none" in d["summary_text"]
    # D5: LNA 0 עם IFGR יורד עד 20 => ה-ADC נחתך, והחומרה (מודמה) דיווחה עומס
    assert s["overload_reported_with_agc_off"] is True
    assert s["rail_code"] in (32767, 32768) and s["peak_code_max"] == 32768
    assert d["steps"]["D1"]["gain_ranges"]["IFGR"] == [20.0, 59.0, 0.0]
    assert d["steps"]["D1"]["modules"][0].endswith("libsdrPlaySupport.so")
    assert d["steps"]["D2"]["gainchange_on_manual"] is False     # ידני => אין GainChange מודמה
    assert d["steps"]["D6"]["readback_ok"] is True
    assert d["steps"]["D8"]["slots"] >= 3 and s["cpu_ok"] is True
    # AGC: הדמיית ה-gain lines (AIRAM_RF gain) נרשמה, ו-AGC כובה בסוף
    assert d["steps"]["D7"]["gain_lines"]
    assert fake.cfg["agc"] is False
    assert r["rows"] == []                                     # אבחון לא כותב rows.jsonl


@needs_np
def test_diagnose_overload_semantics_false_and_null(tmp_path):
    # חיתוך בלי שום אירוע עומס => False (החומרה לא מדווחת כש-AGC כבוי)
    fake = rfsim.FakeRadio(atis_scene(), rfsim.FrontEnd())
    r = run_fake(tmp_path, diag_params(), fake, diag_timing=SMALL_DIAG)
    assert r["diagnose"]["suggested"]["overload_reported_with_agc_off"] is False
    # בלי טלמטריה => null
    d2 = tmp_path / "x"
    d2.mkdir()
    fake = rfsim.FakeRadio(atis_scene(), rfsim.FrontEnd(), patched=False)
    r = run_fake(d2, diag_params(), fake, diag_timing=SMALL_DIAG)
    assert r["diagnose"]["suggested"]["overload_reported_with_agc_off"] is None
    assert r["diagnose"]["suggested"]["gainchange_on_manual"] is None


@needs_np
def test_diagnose_failing_step_does_not_stop_others(tmp_path, monkeypatch):
    def boom(self, sug):
        raise ValueError("x")
    monkeypatch.setattr(rp.Probe, "_d4", boom)
    fake = rfsim.FakeRadio(atis_scene(), rfsim.FrontEnd())
    r = run_fake(tmp_path, diag_params(), fake, diag_timing=SMALL_DIAG)
    st = r["diagnose"]["steps"]
    assert st["D4"] == {"error": "exception:ValueError"}
    assert all("error" not in st[k] for k in ("D1", "D2", "D3", "D5", "D6", "D7", "D8"))
    assert "errors=D4:exception:ValueError" in r["diagnose"]["summary_text"]
    assert r["code"] == 0


@needs_np
def test_diagnose_device_lost_marks_remaining(tmp_path):
    fake = rfsim.FakeRadio(atis_scene(), rfsim.FrontEnd(), not_supported_at_read=40)
    r = run_fake(tmp_path, diag_params(), fake, diag_timing=SMALL_DIAG)
    assert r["code"] == rp.EXIT_DEVICE_LOST and r["end"]["ended"] == "device_lost"
    st = r["diagnose"]["steps"]
    lost = [k for k, v in st.items() if v.get("error") == "device_lost"]
    assert lost and lost[-1] == "D8" and r["diagnose"]["complete"] is False
    assert r["diagnose"]["summary_text"].startswith("AIRAM-RFDIAG v1")
