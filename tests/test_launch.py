# ============================================================================
#  tests/test_launch.py — airam_launch.py (PR 6, v2.30.0): המפעיל של צרכני ה-SDR כ-root.
#  ‏(1) ה-argv זהה בדיוק להרחבת ה-ExecStart הישנה של systemd על הפלט של write_*_env;
#  ‏(2) קונפיג הקול מרונדר מחדש מהמספרים בלבד; (3) קלט זדוני נדחה בקוד — בלי הדפסת התוכן;
#  ‏(4) היחידות: אין EnvironmentFile על יחידת root. בלי חומרה, בלי root.
# ============================================================================
import ast
import itertools
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import airam_launch as L
import app

ROOT = Path(__file__).resolve().parent.parent
LAUNCH_SRC = ROOT / "webtune" / "airam_launch.py"

# ה-ExecStart כפי שהיו לפני v2.30.0 (קפואים — זה החוזה שה-launcher חייב לשמר)
OLD_EXEC = {
    "acars": "/usr/local/bin/acarsdec -g ${ACARS_GAIN} -m ${ACARS_RATEMULT} -o 1 -j ${ACARS_UDP} "
             "-d driver=sdrplay $ACARS_FREQS",
    "vdl2": "/usr/local/bin/dumpvdl2 --soapysdr driver=sdrplay --oversample 20 $VDL2_GAIN "
            "--msg-filter ${VDL2_MSG_FILTER} --output decoded:json:udp:address=127.0.0.1,port=5557 "
            "$VDL2_FREQS",
    "satcom": "/usr/local/bin/inmarsat-sniffer -i sdrplay --satellite=${SATCOM_SATELLITE} --mode=aero "
              "$SATCOM_GAIN $SATCOM_BIAS_TEE $SATCOM_SKIP_C $SATCOM_SPECTRUM --udp=${SATCOM_UDP} "
              "--web=${SATCOM_WEB_PORT} --station-id=airam",
}


def _systemd_env(text):
    env = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith(("#", ";")):
            k, _, v = line.partition("=")
            env[k] = v
    return env


def _systemd_expand(cmd, env):
    """הרחבת ExecStart של systemd (מספיק לצורות שבשימוש): מילה שהיא כולה ‎$VAR ⇒ פיצול
    לפי רווחים (ריק ⇒ נעלם); ‎${VAR} בתוך מילה ⇒ החלפה כארגומנט יחיד."""
    out = []
    for word in cmd.split(" "):
        m = re.fullmatch(r"\$([A-Z0-9_]+)", word)
        if m:
            out += env.get(m.group(1), "").split()
        else:
            out.append(re.sub(r"\$\{([A-Z0-9_]+)\}", lambda mm: env.get(mm.group(1), ""), word))
    return out


@pytest.fixture
def envs(tmp_path, monkeypatch):
    for k in ("ACARS", "VDL2", "SATCOM"):
        monkeypatch.setattr(app, f"{k}_ENV_PATH", tmp_path / f"{k.lower()}.env")
    return tmp_path


def _golden(mode, path):
    text = path.read_text()
    return L.build_argv(mode, L.parse_env(text, mode)), _systemd_expand(OLD_EXEC[mode], _systemd_env(text))


# --- (1) argv זהה להרחבה הישנה ------------------------------------------------
def test_acars_argv_matches_old_execstart_for_every_bank(envs):
    for bank in app.ACARS_BANKS:
        app.write_acars_env(bank["freqs"])
        new, old = _golden("acars", app.ACARS_ENV_PATH)
        assert new == old


def test_vdl2_argv_matches_old_execstart(envs):
    for bank, gain in itertools.product(app.VDL2_BANKS, [(None, None), (40, 0), (20, 9), (59, 4)]):
        app.write_vdl2_env(bank["freqs"], *gain)
        new, old = _golden("vdl2", app.VDL2_ENV_PATH)
        assert new == old
    assert "136975000" in new                       # ההמרה ל-Hz נשארה ב-write_vdl2_env


def test_satcom_argv_matches_old_execstart_all_combinations(envs):
    n = 0
    for sat, gain, bt, sc, sp in itertools.product(sorted(app.SATCOM_SATELLITES), [None, 20, 41, 59],
                                                   [True, False], [True, False], [True, False]):
        app.write_satcom_env([sat], gain=gain, bias_tee=bt, skip_c=sc, spectrum=sp)
        new, old = _golden("satcom", app.SATCOM_ENV_PATH)
        assert new == old
        n += 1
    assert n == 128


@pytest.mark.parametrize("mode", ["acars", "vdl2", "satcom"])
def test_seeded_config_env_files_are_accepted(mode):
    text = (ROOT / "config" / f"{mode}.env").read_text()
    assert L.build_argv(mode, L.parse_env(text, mode)) == _systemd_expand(OLD_EXEC[mode], _systemd_env(text))


def test_launcher_constants_match_app():
    assert L.ACARS_UDP == f"{app.ACARS_UDP_HOST}:{app.ACARS_UDP_PORT}"
    assert L.VDL2_UDP_PORT == app.VDL2_UDP_PORT
    assert L.VDL2_MSG_FILTER == app.VDL2_MSG_FILTER
    assert L.SATCOM_UDP == f"{app.ACARS_UDP_HOST}:{app.SATCOM_UDP_PORT}"
    assert L.SATCOM_WEB_PORT == str(app.SATCOM_WEB_PORT)
    assert set(L.SATCOM_SATELLITES) == app.SATCOM_SATELLITES
    assert L.FREQ_MHZ_RE.pattern == app._FREQ_RE.pattern.strip("^$")
    assert L.MAX_CHANNELS >= max(app.ACARS_MAX_CHANNELS, app.VDL2_MAX_CHANNELS)


# --- (2) קול: רינדור מחדש מהמספרים --------------------------------------------
def test_app_uses_the_launcher_renderer():
    assert app.render_config is L.render_config and app.STATS_PATH == L.STATS_PATH


def test_voice_conf_roundtrip():
    for args in itertools.product([118.0, 132.5, 132.28125, 1999.5], ["am", "nfm"], [True, False],
                                  [20, 59], [0, 9], ["auto", "open", "manual"], [0.0, 60.0],
                                  [True, False], [True, False], [2500, 3000]):
        f, mod, agc, ifg, rf, sq, snr, notch, nar, lp = args
        t = L.render_config(f, mod, agc, ifg, rf, sq, snr, fm_notch=notch, narrow=nar, lowpass=lp)
        assert L.render_config(**L.parse_voice_conf(t)) == t, args


def test_seeded_airband_conf_is_accepted():
    kw = L.parse_voice_conf((ROOT / "config" / "airband.conf").read_text())
    assert kw["freq"] == 132.5 and kw["squelch_mode"] == "open"


def test_pre_v226_conf_still_launches():
    """קונפיג מלפני v2.26.0 (בלי rfnotch_ctrl/rfgain_sel, centerfreq ‎+0.3) — עולה כמו שרץ אז
    (LNA 0 תחת AGC); airam-web ממילא משכתב אותו (_config_stale)."""
    old = L.render_config(132.5, "am", True, 40, 4).replace(
        "driver=sdrplay,rfnotch_ctrl=false,rfgain_sel=4", "driver=sdrplay").replace(
        "centerfreq = 132.7999", "centerfreq = 132.8000")
    kw = L.parse_voice_conf(old)
    assert kw["rf_gain"] == 0 and kw["fm_notch"] is False and kw["agc"] is True


def test_hostile_conf_paths_never_reach_the_rendered_config():
    """stats_filepath/directory/label/פלט נוסף — airam שולט בקובץ, אבל אף נתיב או טקסט ממנו
    לא מגיע לקונפיג שרץ: הפלט הוא render_config של המספרים בלבד."""
    t = L.render_config(132.5, "am", True, 40, 4)
    t = t.replace(str(L.STATS_PATH), "/etc/shadow").replace(str(L.REC_DIR), "/etc/cron.d")
    t = t.replace('name = "AIR-AM 132.500"', 'name = "x\\"; pidfile = \\"/etc/passwd"')
    t += '\npidfile = "/etc/passwd";\n'
    out = L.render_config(**L.parse_voice_conf(t))
    assert "/etc/shadow" not in out and "/etc/cron.d" not in out and "pidfile" not in out
    assert out == L.render_config(132.5, "am", True, 40, 4)


@pytest.mark.parametrize("mutate, code", [
    (lambda t: t.replace("freq = 132.5000;", "freq = 2500.0000;"), "range:freq"),
    (lambda t: t.replace("        freq = 132.5000;", ""), "missing:freq"),
    (lambda t: t + "\n        freq = 133.0000;\n", "dup:freq"),
    (lambda t: t.replace('modulation = "am"', 'modulation = "usb"'), "bad:modulation"),
    (lambda t: t.replace("rfgain_sel=4", "rfgain_sel=4,soapy=1"), "bad:device_string"),
    (lambda t: t.replace("driver=sdrplay", "driver=rtlsdr"), "bad:device_string"),
    (lambda t: t + '\n    gain = 30;\n', "bad:gain"),
    (lambda t: t + '\n        lowpass = 8000;\n', "bad:lowpass"),
    (lambda t: t + '\n        bandwidth = 1;\n', "bad:bandwidth"),
])
def test_voice_conf_rejections(mutate, code):
    with pytest.raises(L.LaunchError, match="^" + re.escape(code) + "$"):
        L.parse_voice_conf(mutate(L.render_config(132.5, "am", True, 40, 4)))


# --- (3) env: מפתחות/ערכים זדוניים ---------------------------------------------
GOOD = {
    "acars": "ACARS_FREQS=131.550 131.725\nACARS_GAIN=-10\nACARS_RATEMULT=160\nACARS_UDP=127.0.0.1:5556\n",
    "vdl2": f"VDL2_FREQS=136975000\nVDL2_GAIN=\nVDL2_MSG_FILTER={L.VDL2_MSG_FILTER}\n",
    "satcom": "SATCOM_SATELLITE=AF1\nSATCOM_GAIN=\nSATCOM_BIAS_TEE=-B\nSATCOM_SKIP_C=\n"
              "SATCOM_SPECTRUM=--spectrum\nSATCOM_UDP=127.0.0.1:5558\nSATCOM_WEB_PORT=8888\n",
}


@pytest.mark.parametrize("mode, text, code", [
    ("acars", GOOD["acars"] + "LD_PRELOAD=/var/lib/airam/x.so\n", "unknown_key"),
    ("vdl2", GOOD["vdl2"] + "SOAPY_SDR_PLUGIN_PATH=/tmp\n", "unknown_key"),
    ("satcom", GOOD["satcom"] + "BASH_ENV=/tmp/x\n", "unknown_key"),
    ("acars", GOOD["acars"] + "ACARS_GAIN=-10\n", "dup_key"),
    ("acars", GOOD["acars"].replace("ACARS_UDP=127.0.0.1:5556\n", ""), "missing_key"),
    ("acars", GOOD["acars"].replace("131.550 131.725", "131.550 -l /etc/x"), "bad:freqs"),
    ("acars", GOOD["acars"].replace("131.550 131.725", "-131.550"), "bad:freqs"),
    ("acars", GOOD["acars"].replace("131.550 131.725", "131.550  131.725"), "bad:freqs"),
    ("acars", GOOD["acars"].replace("131.550 131.725", " ".join(["131.55%d" % i for i in range(9)])), "bad:freqs"),
    ("acars", GOOD["acars"].replace("131.550 131.725", "\u0661\u0663\u0661.\u0665\u0665\u0660"), "bad:freqs"),
    ("vdl2", GOOD["vdl2"].replace("136975000", "\uff11\uff13\uff16975000"), "bad:freqs"),
    ("acars", GOOD["acars"].replace("ACARS_UDP=127.0.0.1:5556", "ACARS_UDP=10.0.0.1:5556"), "bad:udp"),
    ("acars", GOOD["acars"].replace("ACARS_GAIN=-10", 'ACARS_GAIN="-10"'), "bad:gain"),
    ("acars", 'ACARS_FREQS="131.550"\n' + GOOD["acars"].split("\n", 1)[1], "bad:freqs"),
    ("vdl2", GOOD["vdl2"].replace("VDL2_GAIN=", "VDL2_GAIN=--soapy-gain IFGR=40,RFGR=0 --output x"), "bad:gain"),
    ("vdl2", GOOD["vdl2"].replace("VDL2_GAIN=", "VDL2_GAIN=--soapy-gain IFGR=10,RFGR=0"), "bad:gain"),
    ("vdl2", GOOD["vdl2"].replace("136975000", "136.975"), "bad:freqs"),
    ("vdl2", GOOD["vdl2"].replace(L.VDL2_MSG_FILTER, "all"), "bad:msg_filter"),
    ("satcom", GOOD["satcom"].replace("AF1", "XYZ"), "bad:satellite"),
    ("satcom", GOOD["satcom"].replace("SATCOM_GAIN=", "SATCOM_GAIN=--sdrplay-gain=70"), "bad:gain"),
    ("satcom", GOOD["satcom"].replace("SATCOM_BIAS_TEE=-B", "SATCOM_BIAS_TEE=--log=/etc/x"), "bad:satcom_bias_tee"),
    ("satcom", GOOD["satcom"].replace("SATCOM_WEB_PORT=8888", "SATCOM_WEB_PORT=22"), "bad:web_port"),
    ("satcom", GOOD["satcom"] + "export X\n", "syntax"),
])
def test_env_rejections(mode, text, code):
    with pytest.raises(L.LaunchError, match="^" + re.escape(code) + "$"):
        L.build_argv(mode, L.parse_env(text, mode))


def test_duplicate_freqs_still_launch():
    """app.py לא מסיר כפילויות — לפני v2.30.0 הן הגיעו למפענח, ולא ניתן לסרב להן עכשיו."""
    text = GOOD["acars"].replace("131.550 131.725", "131.550 131.550")
    assert L.build_argv("acars", L.parse_env(text, "acars"))[-2:] == ["131.550", "131.550"]


def test_app_freq_regex_is_ascii_only():
    assert not app._FREQ_RE.match("\u0661\u0663\u0661.\u0665\u0665\u0660")


@pytest.mark.parametrize("mode", ["acars", "vdl2", "satcom"])
def test_good_env_accepted(mode):
    argv = L.build_argv(mode, L.parse_env(GOOD[mode], mode))
    assert argv[0] == L.BIN[mode]


# --- קריאת הקלט: symlink / FIFO / hardlink / גודל ------------------------------
def test_read_input_rejects_special_files(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("ROOT-ONLY-SENTINEL\n")
    (tmp_path / "link").symlink_to(secret)
    with pytest.raises(L.LaunchError, match="^open$"):
        L.read_input(tmp_path / "link")
    os.mkfifo(tmp_path / "fifo")
    with pytest.raises(L.LaunchError, match="^not_regular$"):     # לא נתקע (O_NONBLOCK)
        L.read_input(tmp_path / "fifo")
    with pytest.raises(L.LaunchError, match="^not_regular$"):
        L.read_input(tmp_path)
    os.link(secret, tmp_path / "hard")
    with pytest.raises(L.LaunchError, match="^hardlink$"):
        L.read_input(tmp_path / "hard")
    (tmp_path / "big").write_bytes(b"#" * (L.MAX_INPUT_BYTES + 1))
    with pytest.raises(L.LaunchError, match="^too_big$"):
        L.read_input(tmp_path / "big")
    (tmp_path / "bin").write_bytes(b"\xff\xfe")
    with pytest.raises(L.LaunchError, match="^encoding$"):
        L.read_input(tmp_path / "bin")
    with pytest.raises(L.LaunchError, match="^missing$"):
        L.read_input(tmp_path / "nope")


# --- כתיבת הקונפיג המרונדר --------------------------------------------------------
def test_write_voice_conf_mode_and_idempotence(tmp_path):
    d = tmp_path / "voice"
    d.mkdir(mode=0o755)
    old = os.umask(0o077)                       # umask עוין — הקובץ חייב לצאת 0644 בכל זאת
    try:
        out = L.write_voice_conf("abc\n", d)
    finally:
        os.umask(old)
    assert out.read_text() == "abc\n" and (out.stat().st_mode & 0o777) == 0o644
    os.utime(out, (1000, 1000))
    L.write_voice_conf("abc\n", d)
    assert out.stat().st_mtime == 1000          # תוכן זהה ⇒ לא נוגעים (mtime = "מתי הקונפיג השתנה")
    L.write_voice_conf("xyz\n", d)
    assert out.read_text() == "xyz\n" and not list(d.glob(".*.tmp"))


def test_write_voice_conf_refuses_writable_dir(tmp_path):
    d = tmp_path / "voice"
    d.mkdir()
    os.chmod(d, 0o777)
    with pytest.raises(L.LaunchError, match="^voice_dir$"):
        L.write_voice_conf("abc\n", d)
    with pytest.raises(L.LaunchError, match="^voice_dir$"):
        L.write_voice_conf("abc\n", tmp_path / "missing")


# --- main: סירוב בלי exec, exec עם argv/סביבה נקיים ------------------------------
def test_main_refuses_without_exec_and_never_echoes_content(tmp_path, monkeypatch, capsys):
    p = tmp_path / "acars.env"
    p.write_text(GOOD["acars"] + "LD_PRELOAD=SENTINEL-CONTENT-1234\n")
    monkeypatch.setitem(L.ENV_PATHS, "acars", p)
    calls = []
    assert L.main(["acars"], execve=lambda *a: calls.append(a)) == L.EX_CONFIG
    err = capsys.readouterr().err
    assert not calls and "SENTINEL" not in err and "LD_PRELOAD" not in err
    assert "refusing to start (unknown_key)" in err


def test_main_execs_exact_argv_with_clean_env(tmp_path, monkeypatch, capsys):
    p = tmp_path / "vdl2.env"
    p.write_text(GOOD["vdl2"])
    monkeypatch.setitem(L.ENV_PATHS, "vdl2", p)
    monkeypatch.setenv("LD_PRELOAD", "/tmp/evil.so")
    monkeypatch.setenv("INVOCATION_ID", "0123456789abcdef0123456789abcdef")
    calls = []
    assert L.main(["vdl2"], execve=lambda *a: calls.append(a)) == 0
    path, argv, env = calls[0]
    assert path == L.BIN["vdl2"] and argv == L.build_argv("vdl2", L.parse_env(GOOD["vdl2"], "vdl2"))
    assert set(env) == {"PATH", "HOME", "INVOCATION_ID"} and env["PATH"] == L.SAFE_PATH
    # שורות ה-launcher עוברות ביומן של rtl_airband — אסור שייראו כטלמטריית RF
    assert "AIRAM_RF" not in capsys.readouterr().err


def test_main_voice_renders_into_root_dir(tmp_path, monkeypatch):
    src = tmp_path / "airband.conf"
    src.write_text(L.render_config(118.1, "am", False, 30, 2, "manual", 12.0))
    d = tmp_path / "run"
    d.mkdir(mode=0o755)
    monkeypatch.setattr(L, "VOICE_CONF_IN", src)
    monkeypatch.setattr(L, "VOICE_CONF_DIR", d)
    calls = []
    assert L.main(["voice"], execve=lambda *a: calls.append(a)) == 0
    assert calls[0][1] == [L.BIN["voice"], "-F", "-c", str(d / "airband.conf")]
    assert (d / "airband.conf").read_text() == src.read_text()


def test_main_usage():
    assert L.main([]) == L.EX_USAGE and L.main(["shell"]) == L.EX_USAGE


def test_selftest_runs_isolated():
    r = subprocess.run([sys.executable, "-I", "-S", str(LAUNCH_SRC), "--selftest"],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr


def test_launcher_imports_only_stdlib():
    tree = ast.parse(LAUNCH_SRC.read_text())
    mods = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    mods |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert mods <= {"os", "re", "stat", "sys", "pathlib"}


# --- (4) היחידות ---------------------------------------------------------------
UNITS = {"rtl_airband": ("voice", "rtl_airband"), "airam-acars": ("acars", "acarsdec"),
         "airam-vdl2": ("vdl2", "dumpvdl2"), "airam-satcom": ("satcom", "inmarsat-sniffer")}


def _directives(name):
    out = {}
    for line in (ROOT / "systemd" / f"{name}.service").read_text().splitlines():
        if line and not line.startswith(("#", ";", "[")) and "=" in line:
            k, _, v = line.partition("=")
            out.setdefault(k.strip(), []).append(v.strip())
    return out


@pytest.mark.parametrize("unit", sorted(UNITS))
def test_consumer_units_run_the_launcher(unit):
    mode, ident = UNITS[unit]
    d = _directives(unit)
    assert d["ExecStart"] == [f"/usr/bin/python3 -I -S /opt/airam/webtune/airam_launch.py {mode}"]
    assert d["SyslogIdentifier"] == [ident] and d["RestartPreventExitStatus"] == ["78"]


def test_no_root_unit_loads_environment():
    """הכלל (CLAUDE.md §9): יחידה שרצה כ-root לא מקבלת סביבה מקובץ/הגדרה — ובוודאי לא קובץ
    ש-airam כותב. airam-web (User=airam) מותר."""
    for f in sorted((ROOT / "systemd").glob("*.service")):
        d = _directives(f.stem)
        if d.get("User", ["root"])[-1] != "root":
            continue
        for k in ("EnvironmentFile", "Environment", "PassEnvironment"):
            assert k not in d, (f.name, k)
        for k, vals in d.items():
            if k.startswith("Exec"):
                assert not any("/etc/airam" in v or "/var/lib/airam" in v for v in vals), (f.name, k)


def test_rtl_airband_unit_owns_the_rendered_conf_dir():
    d = _directives("rtl_airband")
    assert d["RuntimeDirectory"] == ["airam-voice"] and d["RuntimeDirectoryPreserve"] == ["yes"]
    assert L.VOICE_CONF_DIR == Path("/run/airam-voice")


# --- install.sh (סטטי) -------------------------------------------------------------
INSTALL = (ROOT / "install.sh").read_text()


def test_install_locks_code_dir_and_runs_selftest():
    assert "chown -R root:root /opt/airam/webtune" in INSTALL
    assert "chmod -R go-w /opt/airam/webtune" in INSTALL
    i = INSTALL.index("airam_launch.py --selftest")
    assert "python3 -I -S /opt/airam/webtune/airam_launch.py --selftest" in INSTALL
    assert "die" in INSTALL[i:i + 200]
    assert INSTALL.index("chown -R root:root /opt/airam/webtune") < i


def test_install_rtl_patch_nofollow():
    patch = INSTALL[INSTALL.index("RTL_PATCH='"):INSTALL.index("RTL_CMAKE_FLAGS=")]
    assert "AIRAM_NOFOLLOW" in patch and "O_NOFOLLOW" in patch
    # כשל ה-patch האבטחתי ⇒ die (לא "רדיו עובד עדיף" כמו ב-CBR)
    i = INSTALL.index("grep -qF 'AIRAM_NOFOLLOW' src/output.cpp")
    assert "fopen(fdata->file_path_tmp" in INSTALL[i:i + 200] and "die " in INSTALL[i:i + 400]
    assert "sb.st_nlink != 1" in patch and "ftruncate" in patch and "~O_TRUNC" in patch


def test_rtl_patch_applies_to_real_source(tmp_path):
    """עם RTLSDR_AIRBAND_SRC (עץ v5.2.0) — מריצים את ה-sed האמיתי: שלוש פתיחות הוחלפו, אפס נשארו."""
    src = os.environ.get("RTLSDR_AIRBAND_SRC")
    if not src:
        pytest.skip("RTLSDR_AIRBAND_SRC לא מוגדר")
    out = tmp_path / "output.cpp"
    out.write_text((Path(src) / "src" / "output.cpp").read_text())
    script = INSTALL[INSTALL.index("RTL_PATCH='") + len("RTL_PATCH='"):]
    script = script[:script.index("'\n")]
    subprocess.run(["sed", "-i", script, str(out)], check=True)
    text = out.read_text()
    assert "fopen(fdata->file_path_tmp" not in text
    assert text.count("airam_fopen_nofollow(fdata->file_path_tmp") == 3
