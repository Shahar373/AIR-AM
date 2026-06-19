# CHANGELOG

כל גרסה מתויגת ב-git (`vX.Y.Z`) ומוצגת בממשק (בכותרת). הפורמט לפי
[Keep a Changelog](https://keepachangelog.com/) ו-[Semantic Versioning](https://semver.org/):
**MAJOR** לשינוי שובר · **MINOR** לפיצ'ר · **PATCH** לתיקון.

מוסכמה: כל PR מעדכן את `VERSION` ומוסיף שורות תחת `[Unreleased]`; בעת מיזוג ל-main
מקדמים לכותרת גרסה ומתייגים את ה-commit.

## [Unreleased]

## [1.2.0] - 2026-06-19
### נוסף
- **תמיכת HTTPS + PWA מלא**: כשהדף מוגש ב-HTTPS (למשל מאחורי `tailscale serve`),
  נגן הדפדפן עובר אוטומטית ל-proxy same-origin (`/stream`) שמגיש את סטרים
  ה-Icecast דרך שרת הווב — נמנע חסימת mixed-content. ב-HTTP/LAN הגישה ל-Icecast
  נשארת ישירה (latency נמוך). שרת הווב רץ כעת ב-`threaded` (סטרים ארוך-טווח לא
  חוסם בקשות). תיעוד `tailscale serve` ל-HTTPS מהימן והתקנת אפליקציה בטלפון.

## [1.1.0] - 2026-06-19
### נוסף
- **תמלול ATC (אופציונלי)**: `whisper.cpp` מקומי (מודל `base.en`) מתמלל כל שידור
  מוקלט; הטקסט מוצג מתחת לשידור ביומן וניתן לחיפוש (תיבת חיפוש ביומן). רץ ב-thread
  רקע נפרד וכותב קובץ-צד `<file>.mp3.txt`. **כבוי כברירת מחדל** — הפעלה:
  `INSTALL_WHISPER=1 sudo ./install.sh` (מתקין `ffmpeg`, בונה `whisper.cpp`, מוריד
  מודל, ומפעיל `AIRAM_TRANSCRIBE=1`). התקנות קיימות לא מושפעות.
- **נגן חיצוני**: כפתור "🎧 נגן חיצוני" ונתיב `/live.m3u` לפתיחת השידור בנגן שמע
  חיצוני (ניגון ברקע חסין); באנדרואיד דרך Android Intent (פותח VLC).
- **פקדי מסך נעילה**: מעבר בין פריסטים (previous/next) דרך Media Session.

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

[Unreleased]: https://github.com/Shahar373/AIR-AM/compare/v1.2.0...HEAD
[1.2.0]: https://github.com/Shahar373/AIR-AM/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/Shahar373/AIR-AM/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/Shahar373/AIR-AM/releases/tag/v1.0.0
