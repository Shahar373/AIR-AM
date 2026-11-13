"""מנועי הסינון (tools/airam-rnnoise.c, tools/airam-speexdn.c) והשרשרת (scripts/airam-denoise)
מול מעבדת ההאזנה — docs/audio-comfort-design.md.

העיקרון: מה שהמשתמש שמע ודירג במעבדה הוא מה שרץ במוצר. לכן:
  * airam-speexdn נבנה מול libspeexdsp אמיתי, ופלטו משווה **בבייט** לנתיב של המעבדה.
  * airam-rnnoise נבנה מול RNNoise *מזויף* (tests/stubs — השהיה טהורה): כך נבדק קו
    ההשהיה והערבוב בלי המודל. אם הגולמי מיושר נכון, ערבוב של אות עם עצמו המושהה
    מחזיר את האות בדיוק; יישור שגוי = מסנן מסרק = פלט שונה.
  * פרמטרי הסקריפט (ערבוב, השהיה, תקרת Speex) זהים לאלה של המועמדים שעברו.
"""
import importlib.machinery
import importlib.util
import math
import random
import re
import shutil
import subprocess
from array import array
from pathlib import Path

import pytest

import app

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = (ROOT / "scripts" / "airam-denoise").read_text(encoding="utf-8")

_loader = importlib.machinery.SourceFileLoader("airam_listening_lab_t",
                                               str(ROOT / "scripts" / "airam-listening-lab"))
_spec = importlib.util.spec_from_loader("airam_listening_lab_t", _loader)
lab = importlib.util.module_from_spec(_spec)
_loader.exec_module(lab)

CC = shutil.which("cc") or shutil.which("gcc")
needs_cc = pytest.mark.skipif(not CC, reason="אין מהדר C")
STUB_DELAY = 959


def _script_var(name):
    m = re.search(rf'^{name}="([^"]*)"$', SCRIPT, re.M)
    assert m, name
    return m.group(1)


def _signal(n, seed=2):
    rng = random.Random(seed)
    return array("f", [0.4 * math.sin(i * 0.031) * math.sin(i * 0.0007) + 0.05 * rng.uniform(-1, 1)
                       for i in range(n)])


def _run(binary, args, data):
    return subprocess.run([str(binary)] + args, input=data, capture_output=True, timeout=30)


# --- פרמטרי הסקריפט = המועמדים שעברו במעבדה ----------------------------------

def test_script_params_match_lab_winners():
    assert float(_script_var("RN_MIX")) == lab.VARIANT_BY_ID["rn50"]["dry"]
    assert int(_script_var("SPEEX_DB")) == lab.VARIANT_BY_ID["sp15"]["db"]
    # ההשהיה שנמדדה במעבדה על ה-Pi (results.txt, 13.11.2026: "השהיית RNNoise: 959")
    assert int(_script_var("RN_DELAY")) == 959


def test_script_engines_are_the_closed_list():
    m = re.search(r'^\s*([a-z|]+)\) ;;$', SCRIPT, re.M)
    assert m and set(m.group(1).split("|")) == set(app.DENOISE_ENGINES)
    assert f'AIRAM_DENOISE_ENGINE:-{app.DENOISE_ENGINE_DEFAULT}}}' in SCRIPT


def test_rejected_chain_is_gone():
    """הגרסה שנפסלה (100% + dynaudnorm) לא חוזרת בדלת האחורית."""
    code = "\n".join(l for l in SCRIPT.splitlines() if not l.lstrip().startswith("#"))
    assert "dynaudnorm" not in code
    assert "--mix" in code


def test_unit_reads_engine_env_and_installer_builds_speexdn():
    unit = (ROOT / "systemd" / "airam-denoise.service").read_text(encoding="utf-8")
    inst = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert f"EnvironmentFile=-{app.DENOISE_ENV_PATH}" in unit
    assert f"/usr/local/bin/airam-speexdn" == str(app.DENOISE_SPEEXDN)
    assert 'install -m755 "$BUILD_DIR/airam-speexdn" /usr/local/bin/airam-speexdn' in inst
    assert '"$REPO_DIR/tools/airam-speexdn.c"' in inst
    assert "--mix 0.5 --delay 959" in inst     # בדיקת העשן מריצה את מצב המוצר


# --- airam-rnnoise: קו ההשהיה והערבוב (מול RNNoise מזויף) ---------------------

@pytest.fixture(scope="module")
def rnnoise_stub_bin(tmp_path_factory):
    if not CC:
        pytest.skip("אין מהדר C")
    out = tmp_path_factory.mktemp("rn") / "airam-rnnoise"
    r = subprocess.run([CC, "-O2", "-Wall", "-Werror", f"-DSTUB_DELAY={STUB_DELAY}",
                        "-I", str(ROOT / "tests" / "stubs"), "-o", str(out),
                        str(ROOT / "tools" / "airam-rnnoise.c"),
                        str(ROOT / "tests" / "stubs" / "rnnoise_stub.c"), "-lm"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return out


def test_rnnoise_mix_aligned_is_exact(rnnoise_stub_bin):
    sig = _signal(480 * 20)
    r = _run(rnnoise_stub_bin, ["--mix", "0.5", "--delay", str(STUB_DELAY)], sig.tobytes())
    assert r.returncode == 0
    out = array("f")
    out.frombytes(r.stdout)
    assert len(out) == len(sig)
    assert all(v == 0.0 for v in out[:STUB_DELAY])
    assert list(out[STUB_DELAY:]) == list(sig[:len(sig) - STUB_DELAY])


def test_rnnoise_mix_misaligned_differs(rnnoise_stub_bin):
    """ההוכחה שהיישור עושה משהו: השהיה שגויה ⇒ פלט אחר (מסנן מסרק)."""
    sig = _signal(480 * 20)
    out = array("f")
    out.frombytes(_run(rnnoise_stub_bin, ["--mix", "0.5", "--delay", "0"], sig.tobytes()).stdout)
    assert list(out[STUB_DELAY:]) != list(sig[:len(sig) - STUB_DELAY])


def test_rnnoise_default_is_wet_only(rnnoise_stub_bin):
    """בלי ארגומנטים — ההתנהגות המקורית (100% מסונן), תואם לאחור."""
    sig = _signal(480 * 10)
    out = array("f")
    out.frombytes(_run(rnnoise_stub_bin, [], sig.tobytes()).stdout)
    assert list(out[STUB_DELAY:]) == list(sig[:len(sig) - STUB_DELAY])


def test_rnnoise_partial_frame_dropped_and_nan_zeroed(rnnoise_stub_bin):
    sig = _signal(480 * 3 + 100)
    sig[5] = float("nan")
    r = _run(rnnoise_stub_bin, ["--mix", "0.5", "--delay", str(STUB_DELAY)], sig.tobytes())
    out = array("f")
    out.frombytes(r.stdout)
    assert len(out) == 480 * 3 and all(math.isfinite(v) for v in out)


@pytest.mark.parametrize("args", [["--mix", "1"], ["--mix", "-0.1"], ["--mix", "x"],
                                  ["--delay", "-1"], ["--delay", "99999"], ["--bogus"], ["--mix"]])
def test_rnnoise_bad_args(rnnoise_stub_bin, args):
    assert _run(rnnoise_stub_bin, args, b"").returncode == 2


# --- airam-speexdn: זהה בבייט למעבדה ---------------------------------------------

def _speex_cflags():
    if not CC or not shutil.which("pkg-config"):
        return None
    r = subprocess.run(["pkg-config", "--cflags", "--libs", "speexdsp"], capture_output=True, text=True)
    return r.stdout.split() if r.returncode == 0 else None


@pytest.fixture(scope="module")
def speexdn_bin(tmp_path_factory):
    flags = _speex_cflags()
    if flags is None:
        pytest.skip("libspeexdsp-dev/pkg-config חסרים")
    out = tmp_path_factory.mktemp("sp") / "airam-speexdn"
    r = subprocess.run([CC, "-O2", "-Wall", "-Werror", "-o", str(out),
                        str(ROOT / "tools" / "airam-speexdn.c")] + flags + ["-lm"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return out


@pytest.mark.parametrize("db", [-15, -8])
def test_speexdn_matches_lab_bytes(speexdn_bin, db):
    speex = lab.load_speex()
    if speex is None:
        pytest.skip("libspeexdsp לא נטען ב-ctypes")
    sig = _signal(16000 * 2)
    sig[7] = 1.7          # מעבר ל-±1 — החיתוך חייב להיות זהה
    sig[8] = -1.7
    r = _run(speexdn_bin, [str(db)], sig.tobytes())
    assert r.returncode == 0
    assert r.stdout == lab.speex_denoise(speex, sig.tobytes(), db)


def test_speexdn_default_is_minus_15(speexdn_bin):
    sig = _signal(16000).tobytes()
    assert _run(speexdn_bin, [], sig).stdout == _run(speexdn_bin, ["-15"], sig).stdout


@pytest.mark.parametrize("args", [["0"], ["5"], ["-61"], ["x"], ["-15", "extra"]])
def test_speexdn_bad_args(speexdn_bin, args):
    assert _run(speexdn_bin, args, b"").returncode == 2
