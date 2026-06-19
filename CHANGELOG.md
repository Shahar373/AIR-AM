# CHANGELOG

כל גרסה מתויגת ב-git (`vX.Y.Z`) ומוצגת בממשק (בכותרת). הפורמט לפי
[Keep a Changelog](https://keepachangelog.com/) ו-[Semantic Versioning](https://semver.org/):
**MAJOR** לשינוי שובר · **MINOR** לפיצ'ר · **PATCH** לתיקון.

מוסכמה: כל PR מעדכן את `VERSION` ומוסיף שורות תחת `[Unreleased]`; בעת מיזוג ל-main
מקדמים לכותרת גרסה ומתייגים את ה-commit.

## [Unreleased]

## [1.0.0] - 2026-06-19
גרסה ראשונה מתויגת — לוכדת את המצב היציב הקיים של המערכת.

### היכולות העיקריות
- **בורר תדרים וובי** (Flask) לבחירת תדר/פריסט מהטלפון; כתיבת `airband.conf` והפעלה
  מחדש מאומתת של `rtl_airband` עם רולבק.
- **השהיה נמוכה** (CBR 48kbps + Icecast burst=0) ו-**מדדי RF חיים** (SNR/signal/noise,
  רענון ~1s) לכיוון אנטנה.
- **עריכת פריסטים** מהממשק, **סקוולץ׳** (auto/open/manual), **רווח IFGR/RFGR** + חיווי עומס.
- **הקלטות ויומן שידורים**, **METAR** של נתב״ג, **מסלול נחיתה/המראה פעיל + חיווי שיבוש GPS**
  מ-ADS-B, ו-**חיווי הספקה** (vcgencmd).
- **PWA** (התקנה למסך-הבית) + שמע ברקע, ו-**`/api/health`** לאבחון.
- **חיזוק אבטחה**: שרת הווב רץ כמשתמש לא-root (`airam`) עם sudoers ממוקד, אימות PIN
  אופציונלי (`AIRAM_PIN`), והגנת Origin/CSRF.
- **בדיקות + CI** (pytest + `bash -n`).

[Unreleased]: https://github.com/Shahar373/AIR-AM/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/Shahar373/AIR-AM/releases/tag/v1.0.0
