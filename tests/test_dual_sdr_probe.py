"""scripts/airam-dual-sdr-probe — מדידת שלב 0 לשני SDR (docs/dual-sdr-feasibility.md §7).

שני חלקים: פונקציות טהורות (פענוח פלטים, חלון התדרים, הקונפיג, פסק הדין), וריצה
מקצה לקצה מול כלים מזויפים ב-PATH (rtl_airband שכותב stats, acarsdec ששולח JSON
ב-UDP, vcgencmd/SoapySDRUtil/systemctl/lsusb) — בלי חומרה.
"""
import importlib.machinery
import importlib.util
import json
import os
import socket
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "airam-dual-sdr-probe"
_loader = importlib.machinery.SourceFileLoader("airam_dual_sdr_probe", str(SCRIPT))
_spec = importlib.util.spec_from_loader("airam_dual_sdr_probe", _loader)
probe = importlib.util.module_from_spec(_spec)
_loader.exec_module(probe)

FIND_TWO = """\
######################################################
##     Soapy SDR -- the SDR abstraction library     ##
######################################################

Found device 0
  driver = sdrplay
  label = SDRplay Dev0 RSP1A 1234567890
  serial = 1234567890

Found device 1
  driver = sdrplay
  label = SDRplay Dev1 RSP1B 2305ABCDEF
  serial = 2305ABCDEF

"""

PMIC = """\
     3V7_WL_SW_A current(0)=0.00390372A
   VDD_CORE_A current(7)=2.00000000A
     3V7_WL_SW_V volt(8)=3.60000000V
   VDD_CORE_V volt(15)=0.90000000V
      EXT5V_V volt(24)=5.08000000V
"""


# --- פענוח ------------------------------------------------------------------

def test_parse_find_two_devices_with_models():
    devs = probe.parse_find(FIND_TWO)
    assert [d["serial"] for d in devs] == ["1234567890", "2305ABCDEF"]
    assert [d["model"] for d in devs] == ["RSP1A", "RSP1B"]


def test_parse_find_ignores_other_drivers_and_empty():
    assert probe.parse_find("") == []
    assert probe.parse_find("Found device 0\n  driver = rtlsdr\n  serial = 1\n") == []


def test_parse_lsusb_only_sdrplay():
    text = ("Bus 001 Device 002: ID 1df7:3010 SDRplay RSP1A\n"
            "Bus 003 Device 004: ID 1546:01a7 u-blox GPS\n"
            "Bus 002 Device 003: ID 1df7:3050 SDRplay\n")
    assert len(probe.parse_lsusb(text)) == 2


def test_throttled_decode_and_names():
    assert probe.decode_throttled("throttled=0x50005") == 0x50005
    assert probe.decode_throttled("garbage") is None
    names = probe.throttle_names(0x50005)
    assert "undervoltage עכשיו" in names and "undervoltage מאז האתחול" in names
    assert probe.throttle_names(0) == []


def test_pmic_ext5v_and_rails_exclude_ext5v():
    assert probe.parse_ext5v(PMIC) == 5.08
    # 3.6×0.0039 + 0.9×2.0 — EXT5V (אין לו current) לא נכנס
    assert probe.parse_rails_w(PMIC) == pytest.approx(3.6 * 0.00390372 + 1.8, abs=1e-3)
    assert probe.parse_rails_w("") is None


def test_parse_stats_rtl_airband_format():
    text = textwrap.dedent("""\
        # HELP channel_dbfs_noise_level Squelch noise_level as dBFS.
        # TYPE channel_dbfs_noise_level gauge
        channel_dbfs_noise_level{freq="134.600"}\t-38.120
        channel_dbfs_noise_level{freq="120.500",label="APP"}\t-41.500
        channel_dbfs_signal_level{freq="134.600"}\t-30.000
        buffer_overflow_count{device="0"}\t0
        buffer_overflow_count{device="1"}\t3
        output_overrun_count{device="0"}\t0
        output_overrun_count{mixer="0"}\t1
        """)
    st = probe.parse_stats(text)
    assert st["noise"] == {"134.600": -38.12, "120.500": -41.5}
    assert st["overflow"] == {"0": 0, "1": 3}
    assert st["overrun"]["mixer=0"] == 1


# --- חלון התדרים ------------------------------------------------------------

def test_plan_center_single_matches_app_dc_offset():
    c, need = probe.plan_center(["134.600"])
    assert c == pytest.approx(134.9)          # f + DC_OFFSET כמו render_config
    assert need <= probe.IF_BW_KHZ


@pytest.mark.parametrize("freqs", [["120.500", "121.400"], ["121.400", "121.500"],
                                   ["120.500", "121.400", "121.500"]])
def test_plan_center_fits_and_keeps_dc_clear(freqs):
    c, need = probe.plan_center(freqs)
    khz = [round(float(f) * 1000) for f in freqs]
    assert all(abs(f - round(c * 1000)) >= probe.DC_GUARD_KHZ for f in khz)
    assert all(abs(f - round(c * 1000)) <= probe.IF_BW_KHZ / 2 - probe.EDGE_GUARD_KHZ for f in khz)


@pytest.mark.parametrize("freqs", [["134.600", "120.500"], ["134.600", "132.500"], ["120.500", "121.950"]])
def test_plan_center_rejects_wider_than_window(freqs):
    # אותם זוגות שהמסמך (§2) מסווג כ"לא בחלון של 2.56Msps"
    with pytest.raises(ValueError):
        probe.plan_center(freqs)


def test_norm_freqs_and_acars_window():
    assert probe.norm_freqs("120.5, 121.4") == ["120.500", "121.400"]
    for bad in ("", "abc", "50.0", "120.5,120.5"):
        with pytest.raises(ValueError):
            probe.norm_freqs(bad)
    probe.check_acars(probe.ACARS_FREQS_DEFAULT)
    with pytest.raises(ValueError):
        probe.check_acars(["131.550", "136.975"])
    with pytest.raises(ValueError):
        probe.check_acars([f"{131 + i * 0.025:.3f}" for i in range(9)])


# --- קונפיג -----------------------------------------------------------------

def test_render_conf_pins_serial_and_bias_off():
    conf = probe.render_conf([{"serial": "AAA", "freqs": ["134.600"], "balance": -1.0},
                              {"serial": "BBB", "freqs": ["120.500", "121.400"], "balance": 1.0}],
                             "/x/stats.txt", "/x/rec")
    assert 'device_string = "driver=sdrplay,serial=AAA,biasT_ctrl=false";' in conf
    assert 'device_string = "driver=sdrplay,serial=BBB,biasT_ctrl=false";' in conf
    assert "driver=sdrplay\"" not in conf                       # אף מכשיר לא נפתח "במזל"
    assert conf.count('type = "soapysdr";') == 2
    assert conf.count('type = "mixer";') == 3 and conf.count('type = "file";') == 3
    assert "balance = -1.0;" in conf and "balance = 1.0;" in conf
    assert 'stats_filepath = "/x/stats.txt";' in conf
    assert f'mountpoint = "{probe.MOUNT}";' in conf
    # split_on_transmission רק בפלט ה-file של הערוץ, לא במיקסר (config.cpp אוסר)
    mixers_block = conf.split("devices:")[0]
    assert "split_on_transmission" not in mixers_block
    # סוגריים מאוזנים — קונפיג libconfig תקין מבנית
    for o, c in ("()", "{}"):
        assert conf.count(o) == conf.count(c)


def test_render_conf_rejects_out_of_window_device():
    with pytest.raises(ValueError):
        probe.render_conf([{"serial": "A", "freqs": ["134.600", "120.500"]}], "/s", "/r")


def test_assign_roles_rsp1b_is_primary():
    devs = probe.parse_find(FIND_TWO)
    a, b, warn = probe.assign_roles(devs)
    assert a["model"] == "RSP1B" and b["model"] == "RSP1A" and warn is None


def test_assign_roles_explicit_and_errors():
    devs = probe.parse_find(FIND_TWO)
    a, b, _ = probe.assign_roles(devs, "1234567890", "2305ABCDEF")
    assert a["serial"] == "1234567890"
    for sa, sb in (("1234567890", None), ("X", "2305ABCDEF"), ("1234567890", "1234567890")):
        with pytest.raises(ValueError):
            probe.assign_roles(devs, sa, sb)
    with pytest.raises(ValueError):
        probe.assign_roles(devs[:1])


def test_assign_roles_ambiguous_warns():
    devs = [{"serial": "1", "label": "x", "model": None}, {"serial": "2", "label": "y", "model": None}]
    _, _, warn = probe.assign_roles(devs)
    assert warn


# --- פסק הדין ---------------------------------------------------------------

def _s(t, thr=0, v=5.05, noise=None, ovf=None, age=0.5):
    st = None
    if noise is not None:
        st = {"noise": noise, "signal": {}, "overflow": ovf or {"0": 0}, "overrun": {}}
    return {"t_rel": t, "throttled": thr, "v_in": v, "cpu": {"rtl_airband": 20.0},
            "cpu_sys_pct": 10.0, "stats": st, "stats_age": age}


def test_summarize_pass():
    ss = [_s(i, noise={"134.600": -38 - i * 0.1}) for i in range(10)]
    r = probe.summarize(ss, expect_freqs=["134.600"], baseline={"v_in_median": 5.1})
    assert r["verdict"] == "pass", r["fail"]
    assert r["v_in_min_vs_standby"] == pytest.approx(-0.05)


def test_summarize_fails_on_pi_flags_not_on_our_thresholds():
    # מתח נמוך *בלי* דגל של ה-Pi ⇒ לא נכשל (אין לנו סף); דגל ⇒ נכשל
    low = [_s(i, v=4.70, noise={"f": -38 - i}) for i in range(5)]
    assert probe.summarize(low, expect_freqs=["f"])["verdict"] == "pass"
    flagged = [_s(i, thr=0x50005 if i == 3 else 0, noise={"f": -38 - i}) for i in range(5)]
    r = probe.summarize(flagged, expect_freqs=["f"])
    assert r["verdict"] == "fail"
    assert any("undervoltage" in f for f in r["fail"])


def test_summarize_sticky_flag_already_set_is_note_not_fail():
    ss = [_s(i, thr=0x50000, noise={"f": -38 - i}) for i in range(5)]
    r = probe.summarize(ss, expect_freqs=["f"])
    assert r["verdict"] == "pass" and r["notes"]


def test_summarize_overflow_and_frozen_and_stale():
    ss = [_s(i, noise={"a": -38 - i, "b": -40.0}, ovf={"0": 0, "1": i}) for i in range(5)]
    r = probe.summarize(ss, expect_freqs=["a", "b"])
    assert any("overflow" in f for f in r["fail"])
    assert any("קפא" in f and "b" in f for f in r["fail"])
    stale = [_s(i, noise={"a": -38 - i}, age=60) for i in range(5)]
    assert any("לא התעדכן" in f for f in probe.summarize(stale, expect_freqs=["a"])["fail"])


def test_summarize_no_samples_keeps_death_reason():
    r = probe.summarize([], died="rtl_airband")
    assert r["verdict"] == "fail" and "rtl_airband" in r["fail"][0]


def test_summarize_zero_acars_is_note_not_fail():
    ss = [_s(i, noise={"f": -38 - i}) for i in range(5)]
    r = probe.summarize(ss, expect_freqs=["f"], acars={"count": 0, "by_freq": {}, "unparsed": 0})
    assert r["verdict"] == "pass" and any("ACARS" in n or "acarsdec" in n for n in r["notes"])


# --- מקצה לקצה מול כלים מזויפים --------------------------------------------

FAKES = {
    "SoapySDRUtil": f"""\
import os, signal, sys, time
if any(a.startswith("--rate=") for a in sys.argv):
    # בדיקת קצב (SoapyRateTest.cpp): שורת סיכום מצטברת כל "5ש'", יציאה נקייה רק ב-SIGINT
    rate = float(next(a for a in sys.argv if a.startswith("--rate=")).split("=", 1)[1])
    assert any(a.startswith("--args=driver=sdrplay,serial=") for a in sys.argv)
    if os.environ.get("FAKE_RATE_REJECT"):
        print("Error in rate test: setSampleRate failed")
        sys.exit(1)
    open(os.environ.get("FAKE_RATE_ARGV", os.devnull), "w").write(" ".join(sys.argv))
    done = []
    signal.signal(signal.SIGINT, lambda *a: done.append(1))
    print("Begin RX rate test at %g Msps" % (rate / 1e6), flush=True)
    ovf = 0
    while not done:
        time.sleep(0.2)
        if os.environ.get("FAKE_RATE_OVF"):
            ovf += 1
        print("%g Msps\t%g MBps" % (rate / 1e6, rate * 4 / 1e6) + ("\tOverflows %d" % ovf if ovf else ""), flush=True)
    open(os.environ.get("FAKE_RATE_STOPPED", os.devnull), "w").write("SIGINT")
    sys.exit(0)
gate = os.environ.get("FAKE_FIND_GATE")
text = {FIND_TWO!r}
if gate and os.path.exists(gate + ".armed") and not os.path.exists(gate):
    text = text.split("Found device 1")[0]      # אחרי שתהליך יצא: RSP1B "תפוס" עד restart
sys.stdout.write(text)
""",
    "lsusb": """\
import sys
if "-t" in sys.argv:
    print("/:  Bus 001.Port 001: Dev 001, Class=root_hub, Driver=xhci-hcd/1p, 480M")
else:
    print("Bus 001 Device 002: ID 1df7:3010 SDRplay")
    print("Bus 003 Device 002: ID 1df7:3050 SDRplay")
""",
    "systemctl": """\
import os, sys
if "is-active" in sys.argv:
    active = os.environ.get("FAKE_ACTIVE", "")
    print("active" if sys.argv[-1] == active else "inactive")
    sys.exit(0 if sys.argv[-1] == active else 3)
if "show" in sys.argv:
    print("12345")
if "restart" in sys.argv and os.environ.get("FAKE_FIND_GATE"):
    open(os.environ["FAKE_FIND_GATE"], "w").close()
""",
    "vcgencmd": f"""\
import os, sys
a = sys.argv[1:]
if a[:1] == ["get_throttled"]:
    val = os.environ.get("FAKE_THROTTLED", "0x0")
    cnt = os.environ.get("FAKE_UV_CALL")          # "N": רק הקריאה ה-N מחזירה undervoltage עכשיו
    if cnt:
        path = os.environ["FAKE_UV_COUNTER"]
        n = (int(open(path).read()) if os.path.exists(path) else 0) + 1
        open(path, "w").write(str(n))
        val = "0x50005" if n == int(cnt) else "0x50000"
    print("throttled=" + val)
elif a[:1] == ["pmic_read_adc"]:
    sys.stdout.write({PMIC!r})
elif a[:1] == ["get_config"]:
    print("usb_max_current_enable=0")
""",
    "journalctl": """\
import os, sys, time
if os.environ.get("FAKE_UV_JOURNAL"):
    print(f"{time.time() - 1:.6f} oldsdr kernel: hwmon hwmon3: Undervoltage detected!")
    print(f"{time.time():.6f} oldsdr kernel: hwmon hwmon3: Voltage normalised")
""",
    "rtl_airband": """\
import os, random, re, signal, sys, time
conf = open(sys.argv[sys.argv.index("-c") + 1]).read()
stats = re.search(r'stats_filepath = "([^"]+)"', conf).group(1)
freqs = re.findall(r"^\\s*freq = ([0-9.]+);", conf, re.M)
ndev = conf.count('type = "soapysdr"')
if os.environ.get("FAKE_FIND_GATE"):
    open(os.environ["FAKE_FIND_GATE"] + ".armed", "w").close()
if ndev > 1 and os.environ.get("FAKE_ONEPROC_FAIL"):
    print("[ERROR] SelectDevice Error: sdrplay_api_Fail")
    sys.exit(1)
signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
ovf = 0
while True:
    if os.environ.get("FAKE_OVERFLOW"):
        ovf += 1
    lines = [f'channel_dbfs_noise_level{{freq="{float(f):.3f}"}}\\t{-40 + random.random():.3f}' for f in freqs]
    lines += [f'buffer_overflow_count{{device="{d}"}}\\t{ovf if d == ndev - 1 else 0}' for d in range(ndev)]
    open(stats + ".tmp", "w").write("\\n".join(lines) + "\\n")
    os.replace(stats + ".tmp", stats)
    time.sleep(0.1)
""",
    "acarsdec": """\
import json, signal, socket, sys, time
host, port = sys.argv[sys.argv.index("-j") + 1].split(":")
assert any(a.startswith("driver=sdrplay,serial=") and "biasT_ctrl=false" in a for a in sys.argv)
signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
while True:
    s.sendto(json.dumps({"freq": 131.55, "tail": ".4X-EHD", "level": -20.1}).encode(), (host, int(port)))
    time.sleep(0.1)
""",
}


def _free_udp_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture
def fakes(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in FAKES.items():
        p = bindir / name
        p.write_text(f"#!{sys.executable}\n" + body)
        p.chmod(0o755)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"app_mode": "off"}))
    env = dict(os.environ, PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}",
               AIRAM_STATE=str(state), AIRAM_PROBE_NOROOT="1")
    return {"env": env, "state": state, "tmp": tmp_path}


def _run(fakes, *args, extra_env=None):
    env = dict(fakes["env"], **(extra_env or {}))
    out = fakes["tmp"] / "out"
    port = _free_udp_port()
    # ה-port של ACARS קבוע בסקריפט; הבדיקה מריצה אותו ב-subprocess עם port פנוי דרך -c
    code = (f"import importlib.machinery,importlib.util,sys;"
            f"l=importlib.machinery.SourceFileLoader('p',{str(SCRIPT)!r});"
            f"s=importlib.util.spec_from_loader('p',l);m=importlib.util.module_from_spec(s);l.exec_module(m);"
            f"m.ACARS_UDP_PORT={port};m.STARTUP_SEC=10;m.STOP_GRACE_SEC=3;m.STAGGER_SEC=0.6;m.STAGGER_ALIVE_SEC=0.6;"
            f"sys.exit(m.main(sys.argv[1:]))")
    r = subprocess.run([sys.executable, "-c", code, "--out", str(out), "--interval", "0.2", *args],
                       env=env, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
    summary = json.loads((out / "summary.json").read_text()) if (out / "summary.json").exists() else None
    return r, summary, out


def test_end_to_end_both_scenarios_pass(fakes):
    r, summary, out = _run(fakes, "--secs", "2", "--baseline-secs", "1")
    assert r.returncode == 0, r.stdout + r.stderr
    sc = summary["scenarios"]
    assert set(sc) == {"baseline", "dual-voice-2p", "voice-acars"}
    assert all(v["verdict"] == "pass" for v in sc.values()), json.dumps(sc, ensure_ascii=False)
    assert sc["dual-voice-2p"]["live_freqs"] == {"134.600": True, "120.500": True, "121.400": True}
    assert set(sc["dual-voice-2p"]["overflow_delta"]) == {"a:0", "b:0"}    # מקלט לכל תהליך
    assert sc["voice-acars"]["acars"]["count"] > 0
    assert summary["primary"]["model"] == "RSP1B"
    ca = (out / "airband-dual-2p-a.conf").read_text()
    cb = (out / "airband-dual-2p-b.conf").read_text()
    assert "serial=2305ABCDEF" in ca and "serial=1234567890" not in ca
    assert "serial=1234567890" in cb and "serial=2305ABCDEF" not in cb
    assert "airam-probe-a.mp3" in ca and "airam-probe-b.mp3" in cb
    lines = (out / "samples.jsonl").read_text().splitlines()
    assert lines and all(json.loads(l)["scenario"] for l in lines)
    assert not (out / "rec").exists()                       # ההקלטות של הבדיקה נמחקו


def test_refuses_when_airam_not_in_standby(fakes):
    fakes["state"].write_text(json.dumps({"app_mode": "voice"}))
    r, summary, _ = _run(fakes, "--secs", "1")
    assert r.returncode == 2 and summary is None
    assert "standby" in r.stdout


def test_refuses_when_consumer_unit_active(fakes):
    r, summary, _ = _run(fakes, "--secs", "1", extra_env={"FAKE_ACTIVE": "airam-acars"})
    assert r.returncode == 2 and "airam-acars" in r.stdout


def test_info_is_read_only_even_when_not_standby(fakes):
    fakes["state"].write_text(json.dumps({"app_mode": "voice"}))
    r, summary, out = _run(fakes, "--info")
    assert r.returncode == 0 and "2305ABCDEF" in r.stdout
    assert not out.exists()


def test_overflow_fails_dual_voice(fakes):
    r, summary, _ = _run(fakes, "--secs", "2", "--only", "dual-voice", extra_env={"FAKE_OVERFLOW": "1"})
    assert r.returncode == 1
    assert summary["scenarios"]["dual-voice"]["verdict"] == "fail"


def test_undervoltage_aborts_scenario(fakes):
    r, summary, _ = _run(fakes, "--secs", "20", "--only", "dual-voice",
                         extra_env={"FAKE_THROTTLED": "0x50005"})
    assert r.returncode == 1
    d = summary["scenarios"]["dual-voice"]
    assert d["verdict"] == "fail" and d["duration_sec"] < 10
    assert any("נעצר" in f for f in d["fail"])


def test_phase_timeline_unplug_observation():
    # לפני: הראשי זורם, שניהם חיים · מנותק: acarsdec מת, הראשי עדיין זורם ·
    # חובר: sdrplay אותחל (udev) והראשי קפא — בדיוק מה ש-§7.4 במסמך שואל
    def s(t, alive, val):
        return {"t_rel": t, "alive": alive,
                "stats": {"noise": {"134.600": val}, "signal": {}, "overflow": {}, "overrun": {}}}
    both, prim = {"rtl_airband": True, "acarsdec": True}, {"rtl_airband": True, "acarsdec": False}
    samples = ([s(i, both, -40 + i) for i in range(0, 5)] +
               [s(i, prim, -40 + i) for i in range(5, 10)] +
               [s(i, prim, -40.0) for i in range(10, 15)])
    marks = [{"phase": "לפני", "t_rel": 0, "sdrplay_start": 1},
             {"phase": "מנותק", "t_rel": 5, "sdrplay_start": 1},
             {"phase": "חובר", "t_rel": 10, "sdrplay_start": 1},
             {"phase": "end", "t_rel": 15, "sdrplay_start": 2}]
    tl = probe.phase_timeline(samples, marks, ["134.600"])
    assert [p["phase"] for p in tl] == ["לפני", "מנותק", "חובר"]
    assert tl[0]["alive_at_end"]["acarsdec"] is True and tl[1]["alive_at_end"]["acarsdec"] is False
    assert [p["primary_live"]["134.600"] for p in tl] == [True, True, False]
    assert [p["sdrplay_restarted"] for p in tl] == [False, False, True]


def _procs_with(text):
    out = []
    for p in Path("/proc").iterdir():
        if p.name.isdigit():
            try:
                if text in (p / "cmdline").read_bytes().decode(errors="replace"):
                    out.append(int(p.name))
            except OSError:
                pass
    return out


@pytest.mark.parametrize("sig", ["SIGHUP", "SIGTERM"])
def test_hangup_mid_run_stops_children_and_keeps_summary(fakes, sig):
    # SSH שנפל באמצע ריצה של 20 דק': rtl_airband רץ ב-session משלו, ובלי ניקוי
    # הוא היה נשאר ומחזיק את שני המקלטים.
    import signal as _signal
    import time as _time
    env = dict(fakes["env"])
    out = fakes["tmp"] / "out"
    code = (f"import importlib.machinery,importlib.util,sys;"
            f"l=importlib.machinery.SourceFileLoader('p',{str(SCRIPT)!r});"
            f"s=importlib.util.spec_from_loader('p',l);m=importlib.util.module_from_spec(s);l.exec_module(m);"
            f"m.ACARS_UDP_PORT={_free_udp_port()};m.STOP_GRACE_SEC=3;sys.exit(m.main(sys.argv[1:]))")
    p = subprocess.Popen([sys.executable, "-c", code, "--out", str(out), "--interval", "0.2",
                          "--secs", "60", "--only", "dual-voice"],
                         env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    conf = out / "airband-dual-voice.conf"
    deadline = _time.time() + 30
    while _time.time() < deadline and not (conf.exists() and _procs_with(str(conf))):
        _time.sleep(0.2)
    assert _procs_with(str(conf)), "rtl_airband המזויף לא עלה"
    _time.sleep(1.0)
    p.send_signal(getattr(_signal, sig))
    p.wait(30)
    assert p.returncode == 130
    _time.sleep(0.5)
    assert _procs_with(str(conf)) == []                       # לא נשאר תהליך שמחזיק מקלט
    summary = json.loads((out / "summary.json").read_text())
    assert summary["interrupted"] is True


def test_pi_finding_one_process_fails_two_processes_pass(fakes):
    # מה שקרה על ה-Pi ב-17.11.2026: rtl_airband אחד עם שני מקלטים ⇒ sdrplay_api_Fail.
    # שני התרחישים זה לצד זה: האחד נכשל בעלייה, השני (תהליך לכל מקלט) עובר.
    r, summary, _ = _run(fakes, "--secs", "2", "--only", "dual-voice,dual-voice-2p",
                         extra_env={"FAKE_ONEPROC_FAIL": "1"})
    sc = summary["scenarios"]
    assert sc["dual-voice"]["verdict"] == "fail"
    assert "נכשל בעלייה" in sc["dual-voice"]["fail"][0]
    assert sc["dual-voice-2p"]["verdict"] == "pass", sc["dual-voice-2p"]
    assert r.returncode == 1


def test_wait_devices_restarts_sdrplay_when_one_stays_claimed(fakes, monkeypatch):
    # התרחיש הבא קיבל "no sdrplay device matches" — המקלט עוד היה תפוס מהקודם
    gate = fakes["tmp"] / "restarted"
    (fakes["tmp"] / "restarted.armed").touch()
    for k, v in fakes["env"].items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("FAKE_FIND_GATE", str(gate))
    ok, restarted = probe.wait_devices(["1234567890", "2305ABCDEF"], wait=1, after_restart=5)
    assert ok and restarted and gate.exists()


def test_wait_devices_no_restart_when_both_present(fakes, monkeypatch):
    for k, v in fakes["env"].items():
        monkeypatch.setenv(k, v)
    assert probe.wait_devices(["1234567890", "2305ABCDEF"], wait=1, after_restart=1) == (True, False)


def test_stuck_device_before_scenario_is_recovered_and_noted(fakes):
    # בדיוק הרצף מה-Pi: תרחיש ראשון משאיר את RSP1B תפוס, והשני היה נכשל בעלייה
    r, summary, _ = _run(fakes, "--secs", "2", "--only", "dual-voice-2p,voice-acars",
                         extra_env={"FAKE_FIND_GATE": str(fakes["tmp"] / "gate")})
    sc = summary["scenarios"]
    assert not any("sdrplay" in n for n in sc["dual-voice-2p"]["notes"])
    assert sc["voice-acars"]["verdict"] == "pass", sc["voice-acars"]
    assert any("sdrplay אותחל" in n for n in sc["voice-acars"]["notes"])


def test_parse_kernel_uv_window_only():
    text = ("1763401085.100000 oldsdr kernel: hwmon hwmon3: Undervoltage detected!\n"
            "1763401095.200000 oldsdr kernel: hwmon hwmon3: Voltage normalised\n"
            "1763409999.000000 oldsdr kernel: hwmon hwmon3: Undervoltage detected!\n")
    assert probe.parse_kernel_uv(text, 1763401000, 1763401200) == [1763401085.1]
    assert probe.parse_kernel_uv("", 0, 1e12) == []


def test_kernel_log_undervoltage_fails_scenario(fakes):
    # מה שקרה על ה-Pi: נפילה של 10ש' שהקרנל רשם, וה-get_throttled לא תפס בדגימה
    r, summary, _ = _run(fakes, "--secs", "2", "--only", "dual-voice-2p",
                         extra_env={"FAKE_UV_JOURNAL": "1", "FAKE_THROTTLED": "0x50000"})
    d = summary["scenarios"]["dual-voice-2p"]
    assert d["verdict"] == "fail"
    assert any("הקרנל רשם undervoltage" in f for f in d["fail"])
    assert "הקרנל רשם undervoltage בזמן ההרצה" in r.stdout


def test_undervoltage_during_startup_is_caught(fakes):
    # קריאה 1 = --info; קריאה 2 = הדגימה הראשונה — בעלייה. לפני התיקון היא הייתה
    # דגימת-בסיס ל-CPU שלא נרשמת, והתרחיש "עבר".
    r, summary, _ = _run(fakes, "--secs", "2", "--only", "dual-voice-2p",
                         extra_env={"FAKE_UV_CALL": "2",
                                    "FAKE_UV_COUNTER": str(fakes["tmp"] / "uvcount")})
    d = summary["scenarios"]["dual-voice-2p"]
    assert d["verdict"] == "fail", d
    assert any("undervoltage עכשיו" in f for f in d["fail"])


# --- wide-rate: המשני בקצב של מפצל ACARS+VDL2 (§11.3) -------------------------

def test_parse_rate_log():
    text = ("Begin RX rate test at 6.3 Msps\n|6.29871 Msps\t25.1948 MBps\n "
            "\b6.30002 Msps\t25.2001 MBps\tOverflows 3\n ")
    r = probe.parse_rate_log(text)
    assert r == {"msps": 6.3, "mbps": 25.2, "overflows": 3, "lines": 2}
    assert probe.parse_rate_log("Error in rate test: setSampleRate failed") == {"error": "setSampleRate failed"}
    assert probe.parse_rate_log("") is None


def test_wide_rate_staggered_pass_and_sigint(fakes):
    stopped = fakes["tmp"] / "rate-stopped"
    argv_f = fakes["tmp"] / "rate-argv"
    r, summary, out = _run(fakes, "--only", "wide-rate", "--secs", "2",
                           extra_env={"FAKE_RATE_STOPPED": str(stopped), "FAKE_RATE_ARGV": str(argv_f)})
    assert r.returncode == 0, r.stdout + r.stderr
    w = summary["scenarios"]["wide-rate"]
    assert w["verdict"] == "pass", json.dumps(w, ensure_ascii=False)
    assert w["rate_test"]["msps"] == 6.3 and w["rate_test"]["overflows"] == 0
    assert stopped.read_text() == "SIGINT"                   # יציאה נקייה — המקלט משתחרר ב-API
    assert "serial=1234567890" in argv_f.read_text()         # המשני, לא הראשי
    phases = [json.loads(l)["phase"] for l in (out / "samples.jsonl").read_text().splitlines()]
    assert phases.index("stagger") < phases.index("startup-2") < phases.index("run")   # מדורג


def test_wide_rate_overflow_fails(fakes):
    r, summary, _ = _run(fakes, "--only", "wide-rate", "--secs", "2", extra_env={"FAKE_RATE_OVF": "1"})
    w = summary["scenarios"]["wide-rate"]
    assert w["verdict"] == "fail" and any("overflow" in f for f in w["fail"])


def test_wide_rate_rejected_rate_fails_with_reason(fakes):
    r, summary, _ = _run(fakes, "--only", "wide-rate", "--secs", "2", extra_env={"FAKE_RATE_REJECT": "1"})
    w = summary["scenarios"]["wide-rate"]
    assert w["verdict"] == "fail"
    assert any("נכשל בעלייה" in f and "rate-test" in f for f in w["fail"]), w["fail"]
    assert any("דחה את הקצב" in f for f in w["fail"]), w["fail"]
