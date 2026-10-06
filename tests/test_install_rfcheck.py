# ============================================================================
#  AIR-AM - install.sh: מה ש-🩺 בדיקת RF (PR 2) דורשת מההתקנה — בדיקה סטטית
# ----------------------------------------------------------------------------
#  install.sh לא רץ ב-CI (דורש root, רשת, Pi). מה שנבדק כאן הוא החוזה הטקסטואלי שלו,
#  כי כל אחד מהפריטים האלה הוא גבול אבטחה או זמינות:
#    * sudoers: *בדיוק* שתי שורות חדשות (restart/stop של airam-rfcheck) — לא start, לא
#      wildcard. sudoers פגום נועל את sudo למערכת כולה, ולכן גם visudo אמיתי כשקיים.
#    * היחידה מועתקת ולעולם לא enabled (אף צרכן SDR לא עולה באתחול — §2).
#    * python3-numpy/python3-soapysdr בקריאה נפרדת וסובלנית: חבילה חסרה לא עוצרת התקנה.
#    * הבודק (רץ כ-root) שייך ל-root ולא ניתן לכתיבה ע"י airam.
#    * כלי האבחון מותקן 0755.
#  ‏bash -n/shellcheck על הכלי — כמו ב-CI (shellcheck רק כשהוא מותקן).
# ============================================================================
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
INSTALL = (ROOT / "install.sh").read_text(encoding="utf-8")
DIAG = ROOT / "scripts" / "airam-rfcheck-diag"
RFCHECK_LINES = ["airam ALL=(root) NOPASSWD: /usr/bin/systemctl restart airam-rfcheck",
                 "airam ALL=(root) NOPASSWD: /usr/bin/systemctl stop airam-rfcheck"]


def _sudoers_lines():
    m = re.search(r"cat > \"\$SUDOERS_TMP\" <<'EOF'\n(.*?)\nEOF\n", INSTALL, re.S)
    assert m, "בלוק ה-sudoers לא נמצא ב-install.sh"
    return m.group(1).splitlines()


def _code_lines():
    """שורות קוד בלבד (בלי הערות) — כדי שהערה שמזכירה פקודה לא תיחשב כפקודה."""
    return [ln for ln in INSTALL.splitlines() if not ln.lstrip().startswith("#")]


def test_sudoers_exactly_two_new_rfcheck_lines_eleven_total():
    lines = _sudoers_lines()
    assert len(lines) == 11, lines
    assert [ln for ln in lines if "airam-rfcheck" in ln] == RFCHECK_LINES


def test_sudoers_no_start_no_wildcards_only_exact_commands():
    for ln in _sudoers_lines():
        assert "*" not in ln and "ALL=(ALL)" not in ln, ln
        assert re.fullmatch(
            r"airam ALL=\(root\) NOPASSWD: /usr/bin/systemctl (restart|stop|reset-failed) "
            r"(rtl_airband|airam-acars|airam-vdl2|airam-satcom|airam-rfcheck)", ln), ln
        assert not re.search(r"systemctl (start|enable|reset-failed) airam-rfcheck", ln), ln


def test_sudoers_block_is_valid_for_visudo(tmp_path):
    visudo = shutil.which("visudo") or ("/usr/sbin/visudo" if os.path.exists("/usr/sbin/visudo") else None)
    if not visudo:
        pytest.skip("visudo לא מותקן")
    f = tmp_path / "airam"
    f.write_text("\n".join(_sudoers_lines()) + "\n")
    r = subprocess.run([visudo, "-cf", str(f)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stdout + r.stderr


def test_rfcheck_unit_copied_before_daemon_reload_and_never_enabled():
    cp = 'cp "$REPO_DIR/systemd/airam-rfcheck.service" /etc/systemd/system/'
    assert cp in INSTALL
    assert INSTALL.index(cp) < INSTALL.index("systemctl daemon-reload")
    for ln in _code_lines():
        if re.search(r"systemctl\s+(enable|start)\b", ln):
            assert "rfcheck" not in ln, ln


def test_numpy_soapysdr_apt_separate_and_tolerant():
    m = re.search(r"^apt-get install -y python3-numpy python3-soapysdr \\\n\s*\|\| warn ",
                  INSTALL, re.M)
    assert m, "חסרה קריאת apt נפרדת וסובלנית (|| warn) ל-numpy/soapysdr"
    # הקריאה הראשית (כשל => set -e עוצר את ההתקנה) לא כוללת אותן. שורות-המשך עד השורה
    # בלי "\\" בסופה.
    lines = INSTALL.splitlines()
    i = lines.index("apt-get install -y \\")
    main = []
    while True:
        main.append(lines[i])
        if not lines[i].endswith("\\"):
            break
        i += 1
    main = " ".join(main)
    assert "libsoapysdr-dev" in main and "python3-numpy" not in main and "python3-soapysdr" not in main
    assert INSTALL.index("python3-numpy") < INSTALL.index("# 2. SDRplay API")


def test_probe_owned_by_root_after_webtune_copy():
    i_cp = INSTALL.index('cp -r "$REPO_DIR/webtune/." /opt/airam/webtune/')
    chown = "chown root:root /opt/airam/webtune/rfcheck_probe.py"
    chmod = "chmod 0644 /opt/airam/webtune/rfcheck_probe.py"
    assert chown in INSTALL and chmod in INSTALL
    assert INSTALL.index(chown) > i_cp
    # ‏/opt/airam/webtune לא עובר chown ל-airam בשום מקום (airam => root דרך הבודק)
    for ln in _code_lines():
        if "chown" in ln and "airam:airam" in ln:
            assert "/opt/airam/webtune" not in ln and not re.search(r"/opt/airam\b(?!/models)", ln), ln


def test_diag_script_installed_0755():
    assert 'install -m755 "$REPO_DIR/scripts/airam-rfcheck-diag" /usr/local/bin/airam-rfcheck-diag' in INSTALL
    assert DIAG.is_file() and os.access(DIAG, os.X_OK)


def test_diag_script_bash_syntax():
    if not shutil.which("bash"):
        pytest.skip("bash לא מותקן")
    r = subprocess.run(["bash", "-n", str(DIAG)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    r = subprocess.run(["bash", "-n", str(ROOT / "install.sh")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_diag_script_shellcheck_clean():
    sc = shutil.which("shellcheck")
    if not sc:
        pytest.skip("shellcheck לא מותקן (ב-CI הוא רץ כצעד נפרד)")
    r = subprocess.run([sc, "-S", "warning", str(DIAG)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout


def test_diag_script_contract():
    """הכלי עוטף את ה-API (לא נוגע ב-SDR), דורש root (PIN), ולא שולח Origin."""
    text = DIAG.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash") and "set -euo pipefail" in text
    assert '{"action":"start","phase":"diagnose"}' in text
    assert "/api/rfcheck/export?kind=diagnose" in text
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    assert "X-AIRAM-PIN" in code and "Origin" not in code
    assert "AIRAM-RFDIAG v1" in text and "/var/lib/airam/rfcheck_diagnose.json" in code
    assert "systemctl" not in code.replace("systemctl status airam-web", "")
