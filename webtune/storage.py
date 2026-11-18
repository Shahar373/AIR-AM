# ============================================================================
#  AIR-AM — אחסון: כתיבה אטומית וקריאה/כתיבה של קובצי JSONL
# ----------------------------------------------------------------------------
#  הוצא מ-app.py (שלב ג2). אין כאן שום תלות ב-app/Flask/נתיבי הפרויקט — רק os/json.
#  הכללים (CLAUDE.md §12): כתיבה אטומית עם fsync על הקובץ *ועל התיקייה*; קובצי JSONL נקראים
#  **רק** דרך read_log_lines (UTF-8 סובלני) וכותבים דרך append_line_on_boundary (משלים \n לשורה קטועה).
#  ⚠ הקוראים ניגשים אליהן דרך `storage.<שם>` (לא `from storage import <שם>`) — כך החלפה בבדיקה
#  (`monkeypatch.setattr(storage, "atomic_write", ...)`) משפיעה על כל הקוד.
# ============================================================================
import json
import logging
import os
import threading

log = logging.getLogger("airam")


def atomic_write(path, text):
    """כתיבה אטומית (tmp + rename): rtl_airband יכול לעלות בכל רגע
    (Restart=always / udev) ואסור שיקרא קובץ חצי-כתוב. tmp ייחודי לפר-thread
    (pid+ident) => שתי בקשות מקבילות (PUT /api/presets וכו') לא דורסות זו את
    קובץ ה-tmp של זו; ה-rename האחרון פשוט מנצח (last-write-wins), בלי קובץ פגום.
    ⚠ ‏fsync (על הקובץ *ועל התיקייה*) הוא מה שהופך את זה גם לעמיד-בניתוק-חשמל,
    לא רק אטומי-מול-קוראים: בלעדיו הנתונים יכולים לשבת ב-page cache ולהיעלם
    בכיבוי פתאומי (תרחיש אמיתי בהפעלה מסוללה — ר' README, אזהרת ספק כוח).
    ה-fsync על התיקייה נדרש כי בלעדיו ה-rename עצמו לא בהכרח שרד."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.tmp{os.getpid()}-{threading.get_ident()}")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    # התיקייה עצמה: best-effort — כשל כאן לא שווה הפלת הכתיבה שכבר הצליחה
    try:
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def append_line_on_boundary(f, data):
    """כותב data (bytes שמסתיים ב-\\n) לקובץ שנפתח ב-"a+b", ומוודא שהוא מתחיל
    בשורה חדשה. ⚠ כיבוי פתאומי משאיר את הקובץ בלי \\n בסוף — שורה קטועה, או
    זנב של בייטי NUL ש-ext4 משאיר אחרי delayed allocation. append רגיל היה
    מדביק את הרשומה הבאה לשארית הזאת, והקוראים מדלגים על השורה המשולבת כולה:
    **הרשומה הראשונה אחרי כל כיבוי פתאומי אבדה**. עם \\n משלים השארית נשארת
    שורה פגומה בודדת, והרשומה החדשה שלמה. ב-"a+b" הכתיבה תמיד לסוף (O_APPEND),
    כך שה-seek לקריאת הבייט האחרון לא משנה לאן נכתב."""
    f.seek(0, os.SEEK_END)
    if f.tell() > 0:
        f.seek(-1, os.SEEK_END)
        if f.read(1) != b"\n":
            data = b"\n" + data
    f.write(data)


def append_jsonl_log(path, rec):
    """מוסיף הודעה מנורמלת לקובץ JSONL (append; thread ה-listener הוא הכותב היחיד).
    נכשל בשקט (דיסק מלא וכו') => הפיד החי ממשיך לפעול. משותף ל-ACARS ול-VDL2."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as f:
            append_line_on_boundary(
                f, (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8"))
    except OSError:
        log.exception("jsonl log append (%s)", path)


def trim_jsonl_log(path, keep):
    """קיצוץ ל-keep שורות (rewrite אטומי). נקרא מדי פעם מ-thread ה-listener
    (הכותב היחיד => אין מרוץ). קוראים (ייצוא) סובלים שורה אחרונה חלקית."""
    try:
        lines = read_log_lines(path)
    except OSError:
        return
    if len(lines) > keep:
        # ⚠ נקרא מחוץ ל-try של ה-listener: OSError (דיסק מלא) היה הורג את thread
        # ה-ACARS/VDL2/SATCOM, והפיד מת עד restart של airam-web.
        try:
            atomic_write(path, "\n".join(lines[-keep:]) + "\n")
        except OSError:
            log.warning("קיצוץ %s נכשל", path, exc_info=True)


def jsonl_records(lines):
    """מפרסר שורות JSONL לרשומות — **רק אובייקטים**. משותף לכל הקוראים
    (ייצוא, ארכיון, וטעינת ההיסטוריה באתחול).

    ⚠ למה בדיקת ה-dict קריטית ולא קוסמטית: הקוראים ניגשים מיד ל-`r.get("t")`.
    שורה שהיא JSON *תקין* אך אינה אובייקט (‏`null`, ‏`0`, מחרוזת, מערך — שריד
    אפשרי של בלוק פגום אחרי כיבוי פתאומי, בדיוק התרחיש ש-atomic_write נבנה
    בשבילו) הפילה את הקוראים ב-AttributeError. ‏json.loads הצליח עליה, ולכן
    ה-`except ValueError` הקיים *לא* תפס אותה.
    החומרה: `_load_*_history` נקראות ב-__main__ **בלי try** לפני `app.run()`,
    כך ששורה אחת כזאת מנעה מ-airam-web לעלות בכלל — וזה המתזמר שמשחזר את מצב
    ה-SDR (‏`_boot_restore`). עם `Restart=always` זו לולאת קריסה: התחנה כולה
    מתה, ובשטח אין SSH כדי לאבחן (ר' §9/§12 ב-CLAUDE.md)."""
    out = []
    for ln in lines:
        try:
            rec = json.loads(ln)
        except ValueError:
            continue                          # שורה פגומה (כתיבה חלקית) => דילוג
        if isinstance(rec, dict):
            out.append(rec)
    return out


def read_log_lines(path):
    """שורות קובץ לוג append-only (JSONL), **סובלני ל-UTF-8 שבור**. זורק OSError
    בלבד (קובץ חסר/הרשאה) — כמו read_text, כך שהקוראים לא משתנים.
    ⚠ למה errors="replace" קריטי ולא קוסמטי: הרשומות נכתבות עם
    ensure_ascii=False ומכילות עברית (קטגוריה, decoded). כיבוי פתאומי באמצע
    כתיבה יכול לחתוך תו עברי באמצע (2 בייטים ב-UTF-8), ואז read_text קפדני
    זורק UnicodeDecodeError — שהוא ValueError, לא OSError, ולכן ה-except
    הקיים לא תפס אותו. ‏_load_*_history נקראות ב-__main__ לפני app.run(), כך
    שזה מנע מ-airam-web (המתזמר) לעלות בכלל: לולאת קריסה עם Restart=always,
    והתחנה כולה מתה עד תיקון ידני ב-SSH. הוכח ב-tests/test_corrupt_logs.py.
    עם replace השארית הופכת לשורה פגומה אחת, ש-jsonl_records ממילא מדלג עליה."""
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def read_jsonl_log(path):
    """כל ההודעות מקובץ JSONL, ממוינות לפי זמן (t עולה). סובל שורות פגומות
    (כתיבה חלקית של ההודעה האחרונה בזמן הקריאה). משותף ל-ACARS ול-VDL2."""
    try:
        lines = read_log_lines(path)
    except OSError:
        return []
    out = jsonl_records(lines)
    out.sort(key=lambda r: r.get("t") or 0)
    return out


CSV_FORMULA_CHARS = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(v):
    """מונע הזרקת נוסחה ב-Excel/LibreOffice/Sheets: תא שמתחיל ב-=/+/-/@/Tab/CR
    מתפרש כנוסחה (או DDE/HYPERLINK חי) עם פתיחת הקובץ. תוכן ה-text/decoded/
    tail מגיע משידור רדיו — לא נתון סטטי-מהימן — אז לא מניחים שהוא 'בטוח' רק
    כי הוא טקסט. מוסיף ' מוביל (המוסכמה הסטנדרטית לניטרול) רק כשצריך."""
    if isinstance(v, str) and v[:1] in CSV_FORMULA_CHARS:
        return "'" + v
    return v
