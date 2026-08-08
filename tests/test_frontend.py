# ============================================================================
#  AIR-AM - בדיקות ה-UI (static/index.html)
# ----------------------------------------------------------------------------
#  הרקע: ה-UI כולו סקריפט inline יחיד (~3,000 שורות JS) בלי build step — בחירה
#  מכוונת (ר' §7 ב-CLAUDE.md), אבל המשמעות היא ש**שגיאת תחביר אחת משביתה את
#  כל הממשק** ואין שום שלב שתופס אותה לפני שהמשתמש פותח את הדף בשטח.
#  זה היה הצד היחיד בפרויקט בלי רשת ביטחון כלשהי (ר' docs/field-station-
#  roadmap.md §2.5: שבעה מהבאגים בשלוש הגרסאות האחרונות היו frontend-בלבד).
#
#  הבדיקות כאן זולות בכוונה — בלי דפדפן, בלי npm, בלי build:
#    1. שער תחביר דרך `node --check` (מדלגים אם node לא מותקן).
#    2. בדיקות סטטיות על דפוסים שכבר נשברו בפועל (pollers בלי document.hidden).
#  בדיקות התנהגות אמיתיות (DOM, פיד, ארכיון) דורשות Playwright — שלב נפרד.
# ============================================================================
import re
import shutil
import subprocess
from pathlib import Path

import pytest

INDEX = Path(__file__).resolve().parent.parent / "webtune" / "static" / "index.html"


def _inline_js():
    """מחלץ את הסקריפט ה-inline היחיד מ-index.html."""
    html = INDEX.read_text(encoding="utf-8")
    blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert len(blocks) == 1, f"ציפינו לסקריפט inline יחיד, נמצאו {len(blocks)}"
    return blocks[0]


def test_index_html_exists_and_has_single_inline_script():
    js = _inline_js()
    assert len(js.splitlines()) > 1000     # שמירה מפני חילוץ ריק/שגוי


@pytest.mark.skipif(shutil.which("node") is None, reason="node לא מותקן")
def test_inline_js_syntax_is_valid(tmp_path):
    """⚠ הבדיקה הכי חשובה בקובץ: בלי build step, שגיאת תחביר בסקריפט ה-inline
    לא מתגלה בשום שלב — הדף פשוט נטען ריק/שבור אצל המשתמש. `node --check`
    מפרסר בלי להריץ, כך שאין צורך ב-DOM או בכל תלות אחרת."""
    js = tmp_path / "inline.js"
    js.write_text(_inline_js(), encoding="utf-8")
    r = subprocess.run(["node", "--check", str(js)],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"שגיאת תחביר ב-JS של index.html:\n{r.stderr}"


# --- רגרסיה: poller רשת בלי document.hidden --------------------------------

# ‏intervals שלא פונים לרשת ולכן לא חייבים את השומר (חישוב/ציור מקומי בלבד).
_LOCAL_ONLY = {"_updateConnChip", "renderHome", "refreshAtisStale"}


def test_network_pollers_are_guarded_by_document_hidden():
    """⚠ רגרסיה אמיתית: pollGlobalState היה ה-poller היחיד בלי `document.hidden`,
    כלומר PWA שנשארת פתוחה ברקע בכיס (בדיוק תרחיש השטח) המשיכה לתשאל את ה-Pi
    לנצח — וכל קריאה שלו היא /api/health. בפרויקט שמנהל תקציב חשמל עד כדי
    ויתור על מחצית מדמודולטורי ה-SATCOM (skip_c), ניטור עצמי ברקע הוא בזבוז
    שקט. הבדיקה סורקת את *כל* קריאות setInterval שמפעילות פונקציית poll."""
    js = _inline_js()
    unguarded = []
    # כל setInterval שמזכיר בגוף שלו poller רשת חייב לבדוק document.hidden.
    # ⚠ תופסים גם הפניה חשופה (setInterval(pollX, 10000)) וגם קריאה
    # (setInterval(() => pollX(), 10000)) — הבאג האמיתי היה דווקא בצורה
    # החשופה, וגרסה ראשונה של הבדיקה הזאת פספסה אותו כי דרשה סוגריים.
    for m in re.finditer(r"setInterval\((.+?),\s*[A-Za-z0-9_]+\s*\)", js, re.S):
        body = m.group(1)
        named = set(re.findall(r"\b(poll[A-Za-z0-9_]*)\b", body)) - _LOCAL_ONLY
        if named and "document.hidden" not in body:
            unguarded.append(sorted(named))
    assert not unguarded, (
        "‏setInterval שמפעיל poller רשת בלי בדיקת document.hidden: " + repr(unguarded))


def test_too_old_for_map_guards_missing_timestamp():
    """⚠ רגרסיה: `tooOldForMap` חישב `Date.now() - c.lastT * 1000` בלי לבדוק
    ש-lastT קיים. ב-JS ‏`null * 1000` הוא 0 (=1970), כך שכל מטוס שהודעותיו
    הגיעו בלי חותמת זמן מספרית — מקרה ש-updateCraft מגן עליו במפורש — נחשב
    מיד "ישן מדי" והסמן שלו נמחק מהמפה מיד אחרי שנוצר. ה-peer שלו, isStale,
    כן בדק `lastT != null`; זו הייתה אי-עקביות בין שני שומרים תאומים."""
    js = _inline_js()
    body = re.search(r"const tooOldForMap = \(id\) => \{(.*?)\n      \};", js, re.S)
    assert body, "tooOldForMap לא נמצא — עודכן שמו/מבנהו?"
    assert "lastT == null" in body.group(1), "tooOldForMap חייב לבדוק lastT חסר"


def test_auth_failure_does_not_fake_standby_in_applyMode():
    """⚠ רגרסיה: תשובת כישלון-אימות (401 בלי PIN, 429 בחריגה מהקצב) נעצרת
    ב-_guard ולכן **אין בה `state`** — ו-applyMode נפל ל-`back = "off"`,
    כלומר הכריז "המקלט בכיבוי (Standby)" בזמן שה-SDR ממשיך לשדר כרגיל.
    הבקשה מעולם לא הגיעה ללוגיקת המצבים; שום דבר בחומרה לא נגע."""
    js = _inline_js()
    assert "if (data.auth) {" in js, "applyMode חייב לצאת מוקדם בכישלון אימות"
    # היציאה חייבת לקרות *לפני* גזירת back מ-data.state
    auth_at = js.index("if (data.auth) {")
    back_at = js.index('const back = (data.state && data.state.app_mode)')
    assert auth_at < back_at, "בדיקת data.auth חייבת לקדום לגזירת back"
