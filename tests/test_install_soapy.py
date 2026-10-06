# ============================================================================
#  AIR-AM - SoapySDRPlay3 נעוץ + patch טלמטריית RF (‏AIRAM_RF)
# ----------------------------------------------------------------------------
#  בדיקות סטטיות (בלי רשת, בלי בנייה) לחוזה שבין שלושה חלקים שלא רצים יחד
#  באף מקום אחר חוץ מה-Pi עצמו:
#
#    1. ‏patches/soapysdrplay3-airam-rf.patch — מוסיף ל-Streaming.cpp שורות לוג
#       שה-backend מחפש ב-regex. שינוי של תו אחד בפורמט => הטלמטריה "נעלמת"
#       בשקט (ה-UI מציג "לא זמין"), בלי שום כשל בנייה.
#    2. ‏install.sh — נועץ את ה-commit שה-patch נכתב מולו, ומייצר את סימן-הבנייה
#       ‏/usr/local/share/airam/soapysdrplay3.build-sig **רק** כשה-patch הוחל
#       ואומת. ה-marker הוא החוזה מול airam-web: קיים <=> הטלמטריה זמינה.
#       ‏marker שנשאר מבנייה קודמת אחרי בנייה לא-מתוקנת = "אין עומס" שקרי (§12).
#    3. ‏webtune/app.py — ‏SOAPY_RF_MARK חייב להצביע לאותו נתיב בדיוק.
#
#  אימות ההחלה בפועל (git apply --check + קומפילציה) נעשה מול מקור upstream —
#  ר' test_patch_applies_to_pinned_source (רץ רק כשמצביעים על checkout מקומי).
# ============================================================================
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
INSTALL = ROOT / "install.sh"
PATCH = ROOT / "patches" / "soapysdrplay3-airam-rf.patch"
APP = ROOT / "webtune" / "app.py"

PINNED = "48bd8b41072534018de1d74deb3dea5874d9e0e0"
MARK = "/usr/local/share/airam/soapysdrplay3.build-sig"
# מחרוזות הפורמט — מילה במילה כמו בחוזה המשותף (ה-backend מחפש אותן). stream=start
# (גבול-סשן + הוכחת-חיים) נוספה בתיקוני הביקורת של PR 1 — ר' _RF_START_RE ב-app.py.
FORMATS = ['"AIRAM_RF stream=start"', '"AIRAM_RF overload=1"', '"AIRAM_RF overload=0"',
           '"AIRAM_RF gain grdb=%u lna_grdb=%u"']


def _install():
    return INSTALL.read_text(encoding="utf-8")


def _section3():
    """רק מקטע 3 של install.sh — כדי שבדיקות הסדר לא ייתפסו בטעות במקטע אחר."""
    s = _install()
    a = s.index("# 3. SoapySDRPlay3")
    b = s.index("# 4. RTLSDR-Airband")
    return s[a:b]


def _patch():
    return PATCH.read_text(encoding="utf-8")


def _parse_unified(text):
    """פענוח מינימלי אבל *מחמיר* של unified diff: מחזיר {path: [hunks]}, כאשר
    כל hunk = רשימת שורות (עם תו-הקידומת). מאמת שמספרי השורות בכותרת ה-@@
    תואמים לתוכן — זה מה ש-git apply בודק ראשון, ומה ש"עריכה ידנית קטנה"
    של קובץ patch שוברת בדרך כלל."""
    files = {}
    lines = text.split("\n")
    i = 0
    cur = None
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("--- "):
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            assert nxt.startswith("+++ "), f"--- בלי +++ בשורה {i + 1}"
            old = ln[4:].strip()
            new = nxt[4:].strip()
            assert old.startswith("a/") and new.startswith("b/"), (old, new)
            assert old[2:] == new[2:], "patch שמשנה שם קובץ — לא צפוי"
            cur = files.setdefault(new[2:], [])
            i += 2
            continue
        m = re.match(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", ln)
        if m:
            assert cur is not None, "hunk לפני כותרת קובץ"
            old_n = int(m.group(2) or 1)
            new_n = int(m.group(4) or 1)
            body = []
            i += 1
            seen_old = seen_new = 0
            while i < len(lines) and (seen_old < old_n or seen_new < new_n):
                b = lines[i]
                tag = b[:1]
                assert tag in (" ", "+", "-", "\\"), f"שורה לא חוקית ב-hunk: {b!r}"
                if tag in (" ", "-"):
                    seen_old += 1
                if tag in (" ", "+"):
                    seen_new += 1
                body.append(b)
                i += 1
            assert (seen_old, seen_new) == (old_n, new_n), (
                f"ספירת שורות hunk לא תואמת לכותרת: {ln}")
            cur.append(body)
            continue
        i += 1
    return files


# ---------------------------------------------------------------- install.sh

def test_install_pins_soapysdrplay3_commit():
    s = _section3()
    assert f'SOAPYSDRPLAY_COMMIT="{PINNED}"' in s
    # ה-checkout בפועל משתמש במשתנה (לא ב-master / pull)
    assert re.search(r'checkout -q -f "\$SOAPYSDRPLAY_COMMIT"', s)
    assert "pull --ff-only" not in s, "SoapySDRPlay3 חייב להיות נעוץ, לא מ-master"


def test_install_references_patch_from_repo():
    s = _section3()
    assert 'SOAPY_RF_PATCH="$REPO_DIR/patches/soapysdrplay3-airam-rf.patch"' in s
    assert PATCH.is_file()
    assert re.search(r'git apply --check "\$SOAPY_RF_PATCH" && git apply "\$SOAPY_RF_PATCH"', s)


def test_install_marker_path_matches_contract():
    assert f'AIRAM_SOAPY_MARK="{MARK}"' in _section3()


def test_app_marker_constant_matches_install_if_present():
    """‏SOAPY_RF_MARK ב-app.py (מימוש ה-backend) חייב להיות אותו נתיב. נקרא
    כטקסט ולא ב-import — הבדיקה על החוזה בין הקבצים, לא על app.py."""
    src = APP.read_text(encoding="utf-8")
    m = re.search(r'^SOAPY_RF_MARK\s*=\s*(?:Path\()?["\']([^"\']+)["\']', src, re.M)
    if not m:
        pytest.skip("SOAPY_RF_MARK עדיין לא מוגדר ב-app.py")
    assert m.group(1) == MARK


def test_build_signature_covers_commit_patch_and_flags():
    s = _section3()
    m = re.search(r'^SOAPY_BUILD_SIG="\$\(printf \'%s\' "([^"]*)" \| sha256sum', s, re.M)
    assert m, "SOAPY_BUILD_SIG לא נמצא בצורה הצפויה"
    parts = m.group(1)
    for var in ("$SOAPYSDRPLAY_COMMIT", "$SOAPY_PATCH_SUM", "$SOAPY_CMAKE_FLAGS"):
        assert var in parts, f"{var} חסר בחתימת הבנייה"
    # ‏SOAPY_PATCH_SUM נגזר מ*תוכן* קובץ ה-patch
    assert re.search(r'SOAPY_PATCH_SUM="\$\(sha256sum < "\$SOAPY_RF_PATCH"', s)


def test_skip_requires_module_loaded_and_marker_match():
    s = _section3()
    skip = re.search(r'^if SoapySDRUtil --info 2>/dev/null \| grep -qi sdrplay \\\n'
                     r'\s+&& \[\[ "\$\(cat "\$AIRAM_SOAPY_MARK" 2>/dev/null\)" == "\$SOAPY_BUILD_SIG" \]\]; then',
                     s, re.M)
    assert skip, "תנאי הדילוג חייב לדרוש גם מודול טעון וגם marker תואם"


def test_marker_removed_before_build_and_written_only_when_patch_ok():
    s = _section3()
    rm = s.index('rm -f "$AIRAM_SOAPY_MARK"')
    cmake = s.index("cmake $SOAPY_CMAKE_FLAGS ..")
    verify = s.index("SoapySDRUtil --info 2>/dev/null | grep -qi sdrplay \\\n      || die")
    write = s.index('> "$AIRAM_SOAPY_MARK"')
    # ‏marker ישן נמחק לפני שנוגעים במודול; נכתב רק אחרי בנייה + אימות טעינה
    assert rm < cmake < verify < write
    # הכתיבה היחידה ל-marker נמצאת בתוך הענף של SOAPY_PATCH_OK
    assert s.count('> "$AIRAM_SOAPY_MARK"') == 1
    guard = s.rindex("if [[ $SOAPY_PATCH_OK -eq 1 ]]; then", 0, write)
    between = [ln.strip() for ln in s[guard:write].splitlines()[1:]]
    assert not {"fi", "else"} & set(between), \
        "כתיבת ה-marker חייבת להיות ישירות תחת SOAPY_PATCH_OK"
    # ‏SOAPY_PATCH_OK מתחיל ב-0 ועולה רק אחרי git apply מוצלח
    assert "SOAPY_PATCH_OK=0" in s[:s.index("git apply --check")]


def test_patch_verified_by_grepping_contract_strings():
    s = _section3()
    for fmt in FORMATS:
        assert fmt.strip('"') in s, f"install.sh לא מאמת את {fmt} אחרי git apply"
    assert "grep -qF \"$pat\" Streaming.cpp" in s


def test_build_failure_dies():
    s = _section3()
    assert re.search(r'cmake \$SOAPY_CMAKE_FLAGS \.\. && make -j"\$\(nproc\)" && make install && ldconfig \\\n'
                     r'\s+\|\| die', s)


# ---------------------------------------------------------------- the patch

def test_patch_is_valid_unified_diff_touching_expected_files_only():
    """‏Streaming.cpp (הטלמטריה) ו-Settings.cpp (אתחול streamActive בלבד)."""
    files = _parse_unified(_patch())
    assert sorted(files) == ["Settings.cpp", "Streaming.cpp"]
    assert files["Streaming.cpp"], "אין hunks"
    assert "diff --git a/Streaming.cpp b/Streaming.cpp" in _patch()
    assert "diff --git a/Settings.cpp b/Settings.cpp" in _patch()


def test_patch_initializes_stream_active_before_kwargs_loop():
    """הבנאי קורא writeSetting (rfgain_sel/rfnotch_ctrl מה-device_string של AIR-AM) לפני
    "streamActive = false" המקורי — atomic_bool לא-מאותחל ב-C++11 => UB, וב-true
    מזויף sdrplay_api_Update לפני Init. ה-hunk מוסיף השמה בתחילת הבנאי, בלי להסיר."""
    (hunk,) = _parse_unified(_patch())["Settings.cpp"]
    assert not [l for l in hunk if l.startswith("-")]
    added = [l[1:].strip() for l in hunk if l.startswith("+")]
    assert "streamActive = false;" in added
    ctx = "\n".join(l[1:] for l in hunk)
    assert ctx.index("no available RSP devices found") < ctx.index("streamActive = false;")
    assert ctx.index("streamActive = false;") < ctx.index("selectDevice(")


def test_patch_contains_contract_format_strings_in_added_lines():
    added = "\n".join(l[1:] for h in _parse_unified(_patch())["Streaming.cpp"]
                      for l in h if l.startswith("+"))
    for fmt in FORMATS:
        assert fmt in added, f"{fmt} חסר בשורות שה-patch מוסיף"


def test_patch_logs_at_info_level():
    """‏INFO = ברירת המחדל של SoapySDR (מודפס בלי SOAPY_SDR_LOG_LEVEL) ובלי קודי
    ANSI; WARNING היה עוטף את השורה ב-ESC[1m/ESC[33m (LoggerC.cpp ב-0.8.1)."""
    added = [l[1:] for h in _parse_unified(_patch())["Streaming.cpp"]
             for l in h if l.startswith("+")]
    log_calls = [l for l in added if "AIRAM_RF" in l and "SoapySDR_log" in l]
    assert len(log_calls) == 4
    assert all("SOAPY_SDR_INFO" in l for l in log_calls)


def test_patch_keeps_overload_ack():
    """ה-patch מוסיף בלבד — לא מסיר שום שורה (בפרט לא את OverloadMsgAck, שבלעדיו
    ה-API לא שולח את אירוע ה-overload הבא)."""
    for h in _parse_unified(_patch())["Streaming.cpp"]:
        assert not [l for l in h if l.startswith("-")]
        text = "\n".join(h)
        for state in ("1", "0"):
            tok = f'"AIRAM_RF overload={state}"'
            if tok in text:
                assert text.index("OverloadMsgAck") < text.index(tok)


def test_patch_logs_stream_start_before_api_init():
    """stream=start חייבת לקדום לכל אירוע של הסשן: ev_callback נרשם ב-sdrplay_api_Init,
    ולכן הקריאה לפני Init (באותו hunk של activateStream) — אחרת overload=1 מוקדם היה
    מגיע לפניה ו"מתאפס" ל-False."""
    hunks = _parse_unified(_patch())["Streaming.cpp"]
    (h,) = [h for h in hunks if any("sdrplay_api_Init(" in l for l in h)]
    text = "\n".join(l[1:] for l in h)
    assert text.index("airam_rf_stream_start();") < text.index("sdrplay_api_Init(")
    assert '"AIRAM_RF stream=start"' in _patch()


def test_patch_rate_limits_gain_and_flushes_pending():
    p = _patch()
    assert "AIRAM_RF_GAIN_MIN_INTERVAL" in p
    # ה-flush של הערך האחרון של פרץ מתבצע מ-readStream (ת'רד הצרכן)
    assert "airam_rf_pending_flag.load" in p
    assert "airam_rf_gain(false, 0, 0);" in p


@pytest.mark.skipif(not os.environ.get("SOAPYSDRPLAY3_SRC") or not shutil.which("git"),
                    reason="הגדר SOAPYSDRPLAY3_SRC=<checkout של SoapySDRPlay3> לבדיקת החלה אמיתית")
def test_patch_applies_to_pinned_source(tmp_path):
    src = Path(os.environ["SOAPYSDRPLAY3_SRC"])
    work = tmp_path / "SoapySDRPlay3"
    subprocess.run(["git", "clone", "-q", str(src), str(work)], check=True)
    subprocess.run(["git", "-C", str(work), "checkout", "-q", PINNED], check=True)
    subprocess.run(["git", "-C", str(work), "apply", "--check", str(PATCH)], check=True)
