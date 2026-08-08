# ============================================================================
#  AIR-AM - אינווריאנטות של יחידות ה-systemd
# ----------------------------------------------------------------------------
#  למה זה ראוי לבדיקה אוטומטית ולא רק לתיעוד:
#
#  העיקרון המכריע בפרויקט הוא "‏SDR אחד, בהחלפה" — ל-RSP1B יכול לגשת **תהליך
#  אחד בלבד** בכל רגע (§2 ב-CLAUDE.md). האכיפה כולה יושבת על `Conflicts=`
#  בקובצי היחידות, ועל כך ש**אף צרכן SDR אינו enabled** (‏airam-web הוא
#  המתזמר שמשחזר את המצב באתחול).
#
#  שתי ההנחות האלה נשמרות היום בעין בלבד. הוספת מצב חמישי (‏HFDL כבר בתור
#  ההמשך — ר' docs/field-station-roadmap.md §6) עם שורת `Conflicts` חסרה
#  תייצר **שני תהליכים על אותו מקלט**, וזו לא תקלה שמתגלה ב-CI או בבדיקה
#  ידנית קצרה — היא נראית כמו "קליטה גרועה".
#
#  ‏`Conflicts=` הוא סימטרי מרומז ב-systemd (הצהרה בכיוון אחד מספיקה), ולכן
#  הבדיקה דורשת כיסוי של כל **זוג** — לא של כל כיוון בנפרד.
# ============================================================================
import itertools
import re
from pathlib import Path

import pytest

UNITS = Path(__file__).resolve().parent.parent / "systemd"

# ארבעת צרכני ה-SDR: מתחרים על אותו RSP1B, ולכן מודרים הדדית.
SDR_CONSUMERS = ["rtl_airband.service", "airam-acars.service",
                 "airam-vdl2.service", "airam-satcom.service"]


def _unit(name):
    return (UNITS / name).read_text(encoding="utf-8")


def _has_install_section(text):
    """‏[Install] כ**מקטע אמיתי**, לא כמחרוזת. ⚠ גרסה ראשונה של הבדיקה חיפשה
    substring ונפלה על ההערה `# אין [Install] בכוונה` ב-rtl_airband.service —
    כלומר האשימה את הקוד בדיוק במקום שבו הוא מתעד שהוא עושה את הדבר הנכון."""
    return any(line.strip() == "[Install]" for line in text.splitlines())


def _directive(text, key):
    """כל הערכים של מפתח (systemd מתיר רשימה מופרדת ברווחים ושורות חוזרות)."""
    out = []
    for line in text.splitlines():
        m = re.match(rf"^{key}=(.*)$", line.strip())
        if m:
            out.extend(m.group(1).split())
    return out


def test_all_sdr_consumer_units_exist():
    for name in SDR_CONSUMERS:
        assert (UNITS / name).is_file(), f"יחידה חסרה: {name}"


@pytest.mark.parametrize("a,b", list(itertools.combinations(SDR_CONSUMERS, 2)))
def test_every_sdr_consumer_pair_conflicts(a, b):
    """⚠ הליבה: כל *זוג* צרכני SDR חייב להיות מודר הדדית. שני תהליכים על
    RSP1B אחד לא נכשלים ברעש — הם נראים כמו קליטה גרועה."""
    a_conflicts = _directive(_unit(a), "Conflicts")
    b_conflicts = _directive(_unit(b), "Conflicts")
    assert b in a_conflicts or a in b_conflicts, (
        f"אין Conflicts בין {a} ל-{b} — שניהם יוכלו לרוץ יחד על אותו SDR")


@pytest.mark.parametrize("name", SDR_CONSUMERS)
def test_sdr_consumers_are_not_enabled_at_boot(name):
    """⚠ אף צרכן SDR אינו enabled — כולל rtl_airband. המצב משוחזר באתחול ע"י
    `_boot_restore` של airam-web (§2/§12). מקטע [Install] היה מחזיר את הפרויקט
    לתפיסת "מצב ראשי" שהוסרה במכוון, ובפרט היה שובר את שרידות `off`."""
    assert not _has_install_section(_unit(name)), (
        f"{name} מכיל מקטע [Install] — הוא ייהפך ל-enabled ויעקוף את המתזמר")


@pytest.mark.parametrize("name", SDR_CONSUMERS)
def test_sdr_consumers_bind_to_sdrplay(name):
    """‏Requires+PartOf על sdrplay: נפילת/עצירת שירות ה-API מורידה גם את הצרכן,
    במקום להשאיר תהליך שמחזיק את ההתקן בלי API חי."""
    text = _unit(name)
    assert "sdrplay.service" in _directive(text, "Requires")
    assert "sdrplay.service" in _directive(text, "PartOf")


def test_only_orchestrator_and_api_are_enabled():
    """היחידות היחידות שעולות באתחול הן sdrplay (שירות ה-API) ו-airam-web
    (המתזמר). כל השאר מופעלים על-פי מצב."""
    enabled = {p.name for p in UNITS.glob("*.service")
               if _has_install_section(p.read_text(encoding="utf-8"))}
    assert enabled == {"sdrplay.service", "airam-web.service"}, enabled


def test_satcom_is_the_only_unit_with_a_start_limit():
    """‏StartLimitBurst סופי ל-airam-satcom בלבד: הוא היחיד שמדליק bias-T
    (‎+4.7V על מחבר האנטנה), ולכן קריסה חוזרת שלו היא סיכון חומרה ולא רק רעש.
    שאר הצרכנים אמורים להתאושש לנצח (StartLimitIntervalSec=0)."""
    limited = []
    for name in SDR_CONSUMERS:
        text = _unit(name)
        burst = _directive(text, "StartLimitBurst")
        interval = _directive(text, "StartLimitIntervalSec")
        if burst or interval not in ([], ["0"]):
            limited.append(name)
    assert limited == ["airam-satcom.service"], limited


@pytest.mark.parametrize("name", SDR_CONSUMERS)
def test_start_limit_directives_are_in_the_unit_section(name):
    """‏StartLimit* חייבים לשבת ב-[Unit], לא ב-[Service] — מפתח במקטע הלא-נכון
    מתקבל בשקט ופשוט לא אוכף כלום (נתפס ותוקן בעבר בפרויקט)."""
    section = None
    for line in _unit(name).splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            section = s
        elif s.startswith("StartLimit"):
            assert section == "[Unit]", f"{name}: {s} נמצא ב-{section} במקום ב-[Unit]"
