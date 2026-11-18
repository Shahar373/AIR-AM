"""ניסוי מסנן ה-IF של VDL2 (scripts/airam-vdl2-ifbw) — docs/vdl2-if-filter-design.md.

בלי SDR: המודל מהמקור (dumpvdl2 calc_bandwidth/calc_centerfreq, SoapySDRPlay3
getBwEnumForRate) נבדק כמספרים, הניתוח על ריצות סינתטיות עם תוצאה ידועה, ולולאת
ה-A/B כולה מקצה לקצה מול dumpvdl2 מזויף ששולח פריימים בסכמה האמיתית ב-UDP.
"""
import argparse
import importlib.machinery
import importlib.util
import json
import math
import random
import re
import socket
import stat
import sys
from pathlib import Path

import pytest

import app
import config  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "airam-vdl2-ifbw"
UNIT = ROOT / "systemd" / "airam-vdl2.service"

_loader = importlib.machinery.SourceFileLoader("airam_vdl2_ifbw", str(SCRIPT))
_spec = importlib.util.spec_from_loader("airam_vdl2_ifbw", _loader)
lab = importlib.util.module_from_spec(_spec)
_loader.exec_module(lab)

DEFAULT_HZ = [136725000, 136775000, 136825000, 136875000, 136975000]


# --- המודל מהמקור --------------------------------------------------------------
def test_default_bank_is_the_product_default():
    assert list(lab.DEFAULT_FREQS_MHZ) == config.VDL2_FREQS_DEFAULT
    assert lab.mhz_to_hz(lab.DEFAULT_FREQS_MHZ) == DEFAULT_HZ
    env = lab.read_env(ROOT / "config" / "vdl2.env")
    assert [int(x) for x in env["VDL2_FREQS"].split()] == DEFAULT_HZ


def test_dumpvdl2_requests_271k_and_soapy_rounds_down_to_200k():
    """הלב של השאלה: 250kHz פיזור + 2×10500 = 271000 ⇒ BW_0_200 (‏<300000)."""
    assert lab.SAMPLE_RATE == 2100000
    assert lab.dumpvdl2_bandwidth(DEFAULT_HZ) == 271000
    assert lab.dumpvdl2_center(DEFAULT_HZ) == 136850000
    assert lab.soapy_sdrplay_bw(271000) == 200000
    # setSampleRate(2.1M) לבד היה נותן 1.536MHz — setBandwidth הוא שמצר
    assert lab.soapy_sdrplay_bw(lab.SAMPLE_RATE) == 1536000
    # גבולות getBwEnumForRate: "קטן מ-", לא "קטן או שווה"
    assert [lab.soapy_sdrplay_bw(x) for x in (299999, 300000, 1535999, 1536000, 9e6)] == \
        [200000, 300000, 600000, 1536000, 8000000]
    # בנק CSC-בלבד: ערוץ יחיד במרכז — המסנן הצר לא נוגע בו
    assert lab.soapy_sdrplay_bw(lab.dumpvdl2_bandwidth([136975000])) == 200000
    assert lab.dumpvdl2_center([136975000]) == 136975000


def test_classify_default_bank():
    c = lab.classify(DEFAULT_HZ)
    assert {f for f, (o, g) in c.items() if g == "outer"} == {136725000, 136975000}
    assert {f for f, (o, g) in c.items() if g == "inner"} == {136825000, 136875000}
    assert c[136775000] == (-75000, "mid")
    assert c[136975000][0] == 125000 and c[136725000][0] == -125000


def test_unit_arm_a_is_the_production_command():
    """זרוע A חייבת להיות *בדיוק* קלט היחידה — אחרת לא משווים מול מה שרץ בשטח."""
    text = UNIT.read_text(encoding="utf-8")
    exec_line = next(l for l in text.splitlines() if l.startswith("ExecStart="))
    assert "--soapysdr driver=sdrplay${VDL2_SERIAL} --oversample %d " % lab.OVERSAMPLE in exec_line
    cmd = lab.build_cmd("A", DEFAULT_HZ, None, port=5999, dumpvdl2="/x/dumpvdl2")
    assert cmd[:5] == ["/x/dumpvdl2", "--soapysdr", "driver=sdrplay", "--oversample", "20"]
    # עם serial — בדיוק מה ש-${VDL2_SERIAL} נותן (",serial=XXX", ר' _serial_arg ב-app.py)
    assert lab.build_cmd("A", DEFAULT_HZ, None, serial="2305ABC")[2] == "driver=sdrplay" + app._serial_arg("2305ABC")
    assert cmd[-5:] == [str(f) for f in DEFAULT_HZ]
    assert "decoded:json:udp:address=127.0.0.1,port=5999" in cmd
    assert "--soapy-gain" not in cmd                    # AGC = בלי דגל, כמו VDL2_GAIN ריק


def test_build_cmd_gain_mapping_is_exact():
    """IFGR=gRdB ו-RFGR=LNAstate ב-SoapySDRPlay3 ⇒ ‎--ifgr/--lna-state בנייטיבי."""
    a = lab.build_cmd("A", DEFAULT_HZ, (40, 2))
    b = lab.build_cmd("B", DEFAULT_HZ, (40, 2), serial="SN123")
    assert a[a.index("--soapy-gain") + 1] == "IFGR=40,RFGR=2"
    assert b[1:3] == ["--sdrplay3", "SN123"]
    assert b[b.index("--ifgr") + 1] == "40" and b[b.index("--lna-state") + 1] == "2"
    assert "--gr" not in b                              # דגל של הדרייבר הישן (API v2) בלבד
    nb = lab.build_cmd("B", DEFAULT_HZ, None)
    assert "--ifgr" not in nb and "--lna-state" not in nb
    with pytest.raises(ValueError):
        lab.build_cmd("C", DEFAULT_HZ, None)


def test_unit_fallback_comment_uses_flags_that_exist():
    """‎--gr קיים רק תחת WITH_SDRPLAY (API v2, לא מותקן) — עם ‎--sdrplay3 הוא 'unrecognized
    option' ⇒ usage ⇒ היחידה לא עולה. הרווח הנייטיבי הוא ‎--ifgr/--lna-state."""
    text = UNIT.read_text(encoding="utf-8")
    assert not re.search(r"--gr\s+\d", text)          # מוזכר רק כאזהרה, לא כהוראה
    assert "--sdrplay3 <serial>" in text and "--ifgr 40 --lna-state 0" in text


def test_install_checks_dumpvdl2_inputs_via_help_not_version():
    """‎--version מדפיס רק 'dumpvdl2 X (libacars Y)' ⇒ grep soapysdr עליו נכשל תמיד."""
    text = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert not re.search(r"dumpvdl2 --version.*\n?.*soapysdr|soapysdr.*dumpvdl2 --version", text)
    assert "dumpvdl2 --help" in text


# --- רווח מ-vdl2.env ------------------------------------------------------------
def test_parse_gain_roundtrips_write_vdl2_env(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "VDL2_ENV_PATH", tmp_path / "vdl2.env")
    app.write_vdl2_env(config.VDL2_FREQS_DEFAULT)
    env = lab.read_env(tmp_path / "vdl2.env")
    assert lab.parse_gain(env["VDL2_GAIN"]) is None
    assert [int(x) for x in env["VDL2_FREQS"].split()] == DEFAULT_HZ
    app.write_vdl2_env(config.VDL2_FREQS_DEFAULT, ifgr=35, rfgr=3)
    assert lab.parse_gain(lab.read_env(tmp_path / "vdl2.env")["VDL2_GAIN"]) == (35, 3)


def test_parse_gain_refuses_to_guess():
    for bad in ("--gain 30", "--soapy-gain IFGR=40", "IFGR=40,RFGR=0", "--soapy-gain LNA=1,IFGR=2"):
        with pytest.raises(ValueError):
            lab.parse_gain(bad)
    assert lab.parse_gain_arg("AGC") is None
    assert lab.parse_gain_arg("40,0") == (40, 0)


def test_serial_from_env_matches_write_vdl2_env(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "VDL2_ENV_PATH", tmp_path / "vdl2.env")
    app.write_vdl2_env(config.VDL2_FREQS_DEFAULT, serial="2305ABC", path=app.VDL2_ENV_PATH)
    assert lab.serial_from_env(lab.read_env(tmp_path / "vdl2.env")) == "2305ABC"
    assert lab.serial_from_env({}) is None
    assert lab.serial_from_env({"VDL2_SERIAL": ""}) is None
    assert lab.serial_from_env({"VDL2_SERIAL": ",serial=a;rm -rf"}) is None   # נכנס ל-argv של root


def test_patched_dumpvdl2_is_detected(tmp_path):
    """הטלאי של install.sh מדפיס את הסימן הזה ⇒ ab מסרב (שתי הזרועות כבר 1.536MHz)."""
    patch = (ROOT / "tools" / "dumpvdl2-2.6.0-soapysdr-no-narrow-bw.patch").read_text()
    assert lab.PATCH_MARK.decode() in patch
    b = tmp_path / "patched"
    b.write_bytes(b"\x7fELF..." + lab.PATCH_MARK + b"...")
    plain = tmp_path / "plain"
    plain.write_bytes(b"\x7fELF...Bandwidth set to %d Hz...")
    assert lab.dumpvdl2_patched(str(b)) is True
    assert lab.dumpvdl2_patched(str(plain)) is False
    assert lab.dumpvdl2_patched(str(tmp_path / "missing")) is None


def test_schedule_is_abba():
    assert "".join(lab.schedule(8)) == "ABBAABBA"


# --- פריימים ---------------------------------------------------------------------
def _frame(freq, sig=-30.0, noise=-45.0, src="Aircraft", fec=0, t=1700000000):
    return {"vdl2": {
        "app": {"name": "dumpvdl2", "ver": "2.6.0"}, "t": {"sec": t, "usec": 500000},
        "freq": freq, "burst_len_octets": 40, "hdr_bits_fixed": 0,
        "octets_corrected_by_fec": fec, "idx": 0, "sig_level": sig, "noise_level": noise,
        "freq_skew": 0.4,
        "avlc": {"src": {"addr": "738065", "type": src},
                 "dst": {"addr": "10917A", "type": "Ground station"}, "frame_type": "I"}}}


def test_parse_frame_real_schema():
    r = lab.parse_frame(_frame(136975000, fec=2))
    assert r == {"t": 1700000000.5, "f": 136975000, "sig": -30.0, "noise": -45.0,
                 "fec": 2.0, "src": "air"}
    assert lab.parse_frame(_frame(136975000, src="Ground station"))["src"] == "gnd"
    assert lab.parse_frame({"vdl2": {"freq": "x"}}) is None
    assert lab.parse_frame({"acars": {}}) is None
    assert lab.parse_frame([1, 2]) is None
    weird = _frame(136725000)
    weird["vdl2"]["sig_level"] = True                  # bool אינו מספר כאן
    assert lab.parse_frame(weird)["sig"] is None


# --- probe: צורת המסנן -------------------------------------------------------------
def test_channel_profile_reads_a_known_filter():
    axis = [i * 500.0 for i in range(-2100, 2101)]
    psd = [1.0 if abs(f) <= 100000 else 0.1 for f in axis]
    p = lab.channel_profile(axis, psd, [25000, -25000, 125000, -125000, 75000])
    assert p[25000] == 0.0 and p[-25000] == 0.0 and p[75000] == 0.0
    assert p[125000] == -10.0 and p[-125000] == -10.0
    assert lab.channel_profile([0.0, 500.0], [1.0, 1.0], [125000]) == {125000: None}


def test_median_psd_ignores_bursts():
    np = pytest.importorskip("numpy")
    rng = np.random.default_rng(1)
    fs, nfft = 2100000, 4200
    iq = (rng.standard_normal(nfft * 200) + 1j * rng.standard_normal(nfft * 200)).astype(np.complex64)
    t = np.arange(len(iq)) / fs
    burst = 30 * np.exp(2j * np.pi * 125000 * t)       # "שידור" חזק ב-+125k ב-20% מהזמן
    iq[: len(iq) // 5] += burst[: len(iq) // 5].astype(np.complex64)
    axis, psd = lab.median_psd(iq, fs, nfft)
    prof = lab.channel_profile(axis, psd, [125000, -125000])
    assert abs(prof[-125000]) < 0.5                    # רעש לבן = שטוח
    assert abs(prof[125000]) < 0.5                     # החציון לא ראה את הפרץ


# --- ניתוח ------------------------------------------------------------------------
def _write_run(d, per_block, settle=10.0, block_sec=100.0, noise=None, statuses=None):
    """per_block(arm) ⇒ {freq: n}. מוסיף גם פריימי-רעש שאסור לספור (settle, b=None)."""
    sched = lab.schedule(len(statuses) if statuses else 8)
    groups = lab.classify(DEFAULT_HZ)
    meta = {"freqs_hz": DEFAULT_HZ, "groups": {str(f): g for f, (o, g) in groups.items()},
            "offsets_hz": {str(f): o for f, (o, g) in groups.items()}, "gain": "agc",
            "block_sec": block_sec, "settle_sec": settle, "requested_bw_hz": 271000,
            "bw_hz": {"A": 200000, "B": 1536000}, "started": "x"}
    (d / "meta.json").write_text(json.dumps(meta))
    blocks, frames, t0 = [], [], 1000.0
    for i, arm in enumerate(sched):
        start = t0 + i * (block_sec + 5)
        blocks.append({"i": i, "arm": arm, "start": start, "end": start + block_sec,
                       "settle": settle, "status": (statuses or {}).get(i, "ok") if statuses else "ok"})
        for f, n in per_block(arm).items():
            for k in range(n):
                rx = start + settle + (k + 0.5) * (block_sec - settle) / n
                nz = (noise or {}).get((arm, f), -40.0)
                frames.append({"b": i, "rx": rx, "f": f, "sig": nz + 15, "noise": nz,
                               "fec": float(k % 2), "src": "air" if k % 3 else "gnd"})
            frames.append({"b": i, "rx": start + 1, "f": f, "sig": 0, "noise": 0, "fec": 0, "src": "air"})
        frames.append({"b": None, "rx": start - 2, "f": DEFAULT_HZ[0], "sig": 0, "noise": 0})
    (d / "blocks.json").write_text(json.dumps(blocks))
    (d / "frames.jsonl").write_text("".join(json.dumps(f) + "\n" for f in frames) + '{"b": 0, "rx"')


def _counts(outer_a, outer_b, inner=20, mid=5):
    def per(arm):
        o = outer_a if arm == "A" else outer_b
        return {136725000: o, 136975000: o, 136775000: mid, 136825000: inner, 136875000: inner}
    return per


def test_analyze_detects_outer_loss_with_inner_control(tmp_path):
    noise = {("A", 136725000): -50.0, ("A", 136975000): -52.0}
    _write_run(tmp_path, _counts(10, 20), noise=noise)
    rep = lab.analyze(tmp_path)
    assert rep["blocks_ok"] == 8 and rep["arms"]["A"]["blocks"] == 4
    a = rep["arms"]["A"]["channels"]
    assert a["136975000"]["n"] == 40 and a["136825000"]["n"] == 80   # settle/b=None/שורה קטועה — לא נספרו
    assert a["136975000"]["snr_med"] == 15.0
    assert a["136975000"]["per_hour"] == round(40 * 3600 / 360, 1)
    pooled = rep["did"]["all"]["pooled"]
    assert pooled["rr"] == pytest.approx(2.0) and pooled["lo"] > 1
    assert set(rep["did_per_channel"]) == {"136725000", "136975000"}
    p = rep["paired_outer_share"]
    assert p["pairs"] == 4 and p["lo"] > 0                          # B: 40/80 מול A: 20/60
    assert rep["noise_outer_minus_inner_db"]["A"] == {"136725000": -10.0, "136975000": -12.0}
    assert rep["noise_outer_minus_inner_db"]["B"] == {"136725000": 0.0, "136975000": 0.0}
    assert "✓ לא כולל 1" in lab.format_report(rep)


def test_analyze_null_result_includes_one(tmp_path):
    _write_run(tmp_path, _counts(15, 15))
    rep = lab.analyze(tmp_path)
    pooled = rep["did"]["all"]["pooled"]
    assert pooled["rr"] == pytest.approx(1.0) and pooled["lo"] < 1 < pooled["hi"]
    assert rep["paired_outer_share"]["mean"] == pytest.approx(0.0)
    assert "~ כולל 1" in lab.format_report(rep)


def test_analyze_without_inner_traffic_has_no_control(tmp_path):
    _write_run(tmp_path, _counts(10, 20, inner=0, mid=0))
    rep = lab.analyze(tmp_path)
    d = rep["did"]["all"]
    assert d["no_control"] and "pooled" not in d
    assert d["rate_ratio"]["rr"] == pytest.approx(2.0)
    assert "אין בקרה" in lab.format_report(rep)


def test_analyze_skips_incomplete_blocks(tmp_path):
    _write_run(tmp_path, _counts(10, 20), statuses={i: "ok" for i in range(8)} | {6: "exited", 7: "interrupted"})
    rep = lab.analyze(tmp_path)
    assert rep["blocks_ok"] == 6 and rep["blocks_total"] == 8
    assert rep["arms"]["A"]["blocks"] == 3 and rep["arms"]["B"]["blocks"] == 3


def test_ratio_of_ratios_zero_cells_and_t_table():
    r = lab.ratio_of_ratios(0, 10, 5, 10)
    assert math.isfinite(r["rr"]) and r["lo"] < r["rr"] < r["hi"]
    assert lab.t975(1) == 12.706 and lab.t975(5) == 2.571 and lab.t975(500) == 1.96


# --- בדיקות פתיחה ---------------------------------------------------------------
def test_preflight_refuses_saved_mode_and_busy_consumer(monkeypatch):
    monkeypatch.setattr(lab, "active_consumers", lambda: [])
    monkeypatch.setattr(lab, "saved_mode", lambda: "off")
    assert lab.preflight(need_root=False) == []
    monkeypatch.setattr(lab, "saved_mode", lambda: None)            # אין state = התקנה טרייה
    assert lab.preflight(need_root=False) == []
    monkeypatch.setattr(lab, "saved_mode", lambda: "scan")
    assert any("scan" in p for p in lab.preflight(need_root=False))
    monkeypatch.setattr(lab, "saved_mode", lambda: "off")
    monkeypatch.setattr(lab, "active_consumers", lambda: ["airam-vdl2 (active)"])
    assert any("airam-vdl2" in p for p in lab.preflight(need_root=False))


def test_saved_mode_reads_state(tmp_path):
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"app_mode": "vdl2"}))
    assert lab.saved_mode(p) == "vdl2"
    p.write_text("{nope")
    assert lab.saved_mode(p) is None


# --- dumpvdl2 מזויף: לולאת ה-A/B מקצה לקצה ---------------------------------------
FAKE = r'''#!PYTHON
import json, signal, socket, sys, time
argv = sys.argv[1:]
if "--help" in argv:
    sys.stderr.write("dumpvdl2 2.6.0 (libacars 2.2.0)\nUsage:\n  --soapysdr <device_string>\nHELP_EXTRA\n")
    sys.exit(0)
out = argv[argv.index("--output") + 1]
port = int(out.split("port=")[1])
freqs = [int(a) for a in argv if a.isdigit() and len(a) == 9]
arm_b = "--sdrplay3" in argv
center = min(freqs) + (max(freqs) - min(freqs)) // 2
stop = []
signal.signal(signal.SIGTERM, lambda *a: stop.append(1))
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
tick = 0
sys.stderr.write("Bandwidth set to %d Hz\n" % (max(freqs) - min(freqs) + 21000) if not arm_b else "Device started\n")
sys.stderr.flush()
while not stop:
    tick += 1
    for f in freqs:
        outer = abs(f - center) > 100000
        if outer and not arm_b and tick % 2:
            continue          # "המסנן הצר" מאבד חצי מהפריימים בקצוות
        t = time.time()
        m = {"vdl2": {"t": {"sec": int(t), "usec": int((t % 1) * 1e6)}, "freq": f,
                      "octets_corrected_by_fec": 0, "sig_level": -30.0, "noise_level": -45.0,
                      "avlc": {"src": {"type": "Aircraft", "addr": "738065"}}}}
        s.sendto(json.dumps(m).encode(), ("127.0.0.1", port))
    time.sleep(0.01)
'''


def _fake_dumpvdl2(tmp_path, with_sdrplay3=True):
    p = tmp_path / "dumpvdl2"
    extra = "  --sdrplay3 <device_id>" if with_sdrplay3 else ""
    p.write_text(FAKE.replace("PYTHON", sys.executable).replace("HELP_EXTRA", extra))
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return str(p)


def _free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_dumpvdl2_inputs_from_help(tmp_path):
    assert lab.dumpvdl2_inputs(_fake_dumpvdl2(tmp_path)) == {"--soapysdr", "--sdrplay3"}
    other = tmp_path / "other"
    other.mkdir()
    assert lab.dumpvdl2_inputs(_fake_dumpvdl2(other, with_sdrplay3=False)) == {"--soapysdr"}
    assert lab.dumpvdl2_inputs(str(tmp_path / "missing")) == set()


def test_run_ab_end_to_end_with_fake_dumpvdl2(tmp_path):
    fake = _fake_dumpvdl2(tmp_path)
    args = argparse.Namespace(minutes=4 * 0.6 / 60, block=0.6 / 60, settle=0.15, gap=0.0,
                              port=_free_port(), serial=None)
    run = tmp_path / "run"
    run.mkdir()
    blocks = lab.run_ab(args, DEFAULT_HZ, None, run, dumpvdl2=fake,
                        check=lambda: [], ready=lambda: True)
    assert [b["arm"] for b in blocks] == ["A", "B", "B", "A"]
    assert all(b["status"] == "ok" for b in blocks)
    assert "Bandwidth set to 271000 Hz" in (run / "block00-A.log").read_text()
    meta = json.loads((run / "meta.json").read_text())
    assert meta["cmd"]["B"][1:3] == ["--sdrplay3", "0"] and meta["bw_hz"]["A"] == 200000
    rep = lab.analyze(run)
    a, b = rep["did"]["all"]["counts"], rep["did"]["aircraft"]["counts"]
    assert a["A_inner"] > 0 and a["B_inner"] > 0
    assert a["B_outer"] / a["B_inner"] > 1.5 * a["A_outer"] / a["A_inner"]
    assert rep["did"]["all"]["pooled"]["lo"] > 1
    assert b == a                                          # כל הפריימים המזויפים ממטוסים


def test_run_ab_stops_when_dumpvdl2_dies(tmp_path):
    bad = tmp_path / "dumpvdl2"
    bad.write_text(f"#!{sys.executable}\nimport sys\nsys.stderr.write('Unable to select device\\n')\nsys.exit(1)\n")
    bad.chmod(bad.stat().st_mode | stat.S_IXUSR)
    args = argparse.Namespace(minutes=0.04, block=0.01, settle=0.1, gap=0.0,
                              port=_free_port(), serial=None)
    run = tmp_path / "run"
    run.mkdir()
    blocks = lab.run_ab(args, DEFAULT_HZ, None, run, dumpvdl2=str(bad),
                        check=lambda: [], ready=lambda: True)
    assert len(blocks) == 1 and blocks[0]["status"] == "exited" and blocks[0]["rc"] == 1


def test_run_ab_aborts_when_someone_takes_the_sdr(tmp_path):
    fake = _fake_dumpvdl2(tmp_path)
    calls = []

    def check():
        calls.append(1)
        return [] if len(calls) < 2 else ["צרכן SDR פעיל: rtl_airband (active)"]

    args = argparse.Namespace(minutes=4 * 0.4 / 60, block=0.4 / 60, settle=0.1, gap=0.0,
                              port=_free_port(), serial=None)
    run = tmp_path / "run"
    run.mkdir()
    blocks = lab.run_ab(args, DEFAULT_HZ, None, run, dumpvdl2=fake, check=check, ready=lambda: True)
    assert len(blocks) == 1


def test_cmd_ab_refusals(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(lab, "preflight", lambda need_root=True: [])
    assert lab.main(["ab", "--port", "5557"]) == 2
    assert "5557" in capsys.readouterr().out
    assert lab.main(["ab", "--freqs", "136.975"]) == 2              # CSC בלבד: אין חיצוני
    assert lab.main(["ab", "--dumpvdl2", _fake_dumpvdl2(tmp_path, with_sdrplay3=False)]) == 2
    assert "--sdrplay3" in capsys.readouterr().out
    patched = tmp_path / "p"
    patched.mkdir()
    fake = _fake_dumpvdl2(patched)
    with open(fake, "a") as fh:
        fh.write("# AIR-AM patch\n")
    assert lab.main(["ab", "--dumpvdl2", fake]) == 2
    assert "הטלאי" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        lab.main(["ab", "--serial", "a;b"])
