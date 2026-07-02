# CLAUDE.md — מדריך הפרויקט ל-Claude (וכל מפתח)

מסמך זה הוא מקור-האמת לארכיטקטורה, למוסכמות ולזרימת העבודה של **AIR-AM**.
קרא אותו לפני כל פיצ'ר חדש או תיקון באג — הוא נועד לתת לך את כל ההקשר הדרוש
כדי לעבוד בפרויקט בלי לקרוא מחדש את כל המאגר. כשמוסיפים יכולת מהותית או משנים
ארכיטקטורה — **עדכן גם את המסמך הזה** (וגם את `README.md` ו-`CHANGELOG.md`).

> שפה: הקוד, התיעוד והממשק בעברית (עם מונחים טכניים באנגלית). שמור על הסגנון הזה
> בקוד חדש, בהערות, ב-commit messages וב-UI.

---

## 1. מהות הפרויקט ומטרותיו

**AIR-AM** הופך **Raspberry Pi 5 + SDRplay RSP1B** לתחנת האזנה לתעופה שנשלטת
**כולה מהטלפון דרך דפדפן** — בלי אפליקציה ייעודית ובלי סיסמאות.

המטרות המנחות (כל החלטת עיצוב נמדדת מולן):

1. **שליטה מלאה מהטלפון בדפדפן** — דף ווב אחד (`http://<IP>:8080`) הוא משטח הבקרה
   היחיד: בחירת תדר, מצב (קול/ACARS), gain, squelch, יומן שידורים, מפה ומדדים.
2. **אפס-קונפיגורציה למשתמש הקצה** — `install.sh` בודד עושה הכול אוטומטית
   (כולל אישור רישיון SDRplay, בנייה, שירותי systemd). אידמפוטנטי: `git pull && sudo ./install.sh`.
3. **headless ועמיד** — השירותים עולים באתחול, מתאוששים מניתוק USB/קריסה לבד,
   ושורדים reboot. הרדיו לא נופל בגלל תקלת רשת/אינטרנט (מקורות חיצוניים מבודדים ב-thread).
4. **פרטיות ומקומיות** — הכול רץ על ה-Pi: פענוח, הקלטות, תמלול (whisper.cpp מקומי),
   ניתוח ADS-B. בלי ענן, בלי חשבונות. מיועד **לרשת פרטית מהימנה בלבד**.
5. **בטיחות בלי חיכוך** — שרת הווב רץ כמשתמש לא-root עם sudoers ממוקד; אין סיסמאות
   למאזין; PIN אופציונלי. פשטות מנצחת על "תכונות ארגוניות".

**מה הפרויקט עושה בפועל:**
- **מצב קול (📻):** בורר תדרים ל-Air band (AM, 118–137 MHz) דרך `rtl_airband` →
  `Icecast2` → נגן הדפדפן. פריסטים לנתב"ג + תדר חופשי, בקרת gain/squelch, מדדי RF
  חיים (SNR/signal/noise), יומן שידורים + הקלטות MP3, תמלול ATC אופציונלי, METAR.
- **מצב ACARS (📡):** פענוח הודעות נתונים דיגיטליות של מטוסים דרך `acarsdec`,
  הצגה חיה בכרטיסים אחידים, שמירה ל-JSONL, ייצוא CSV/JSON, מפת Leaflet עם שובל מסלול.
- **מודעוּת מרחבית (תמיד פעיל):** ניתוח ADS-B מקהילה (adsb.lol/adsb.fi) מסיק את
  **המסלול הפעיל** בנתב"ג ומחשב אינדיקציית **שיבוש GPS** — עמיד לשיבוש מקומי.

---

## 2. ארכיטקטורה — זרימת הנתונים

```
                   ┌────────────────────────────────────────────────────┐
   אנטנה ─►RSP1B─USB─►                Raspberry Pi 5                      │
                   │      SDRplay API service (sdrplay.service)          │
                   │                       │                             │
                   │      ┌────────────────┼────────────────┐            │
   קול 📻          │ rtl_airband       acarsdec         dumpvdl2 │ VDL2 🛰️
   / ACARS 📡      │ (AM/NFM→MP3)    (ACARS→JSON)     (VDL2→JSON) │  (Conflicts
   (Conflicts) ◄───┤      │           │ UDP 5556       │ UDP 5557 │   3-כיווני)
                   │ Icecast2 :8000   └──── airam-web :8080 ──────┘            │
                   │      │                       │                            │
                   └──────┼───────────────────────┼────────────────────────────┘
                          ▼                        ▼
                   נגן הדפדפן (סטרים)      דף הבקרה (REST/JSON)
                          ▲                        │
                          └──── /stream proxy ─────┘ (כש-HTTPS: same-origin)

   thread ברקע ב-airam-web:  adsb.py ─HTTP─► adsb.lol / adsb.fi  (מסלול פעיל + GPS)
```

**העיקרון המכריע — SDR אחד, בהחלפה:** ל-RSP1B יכול לגשת **תהליך אחד בלבד** בכל רגע.
`rtl_airband` (קול), `acarsdec` (ACARS) ו-`dumpvdl2` (VDL2) הם שלושה תהליכים נפרדים
שמתחרים על אותו מקלט. לכן יחידות ה-systemd מוגדרות `Conflicts` (`airam-vdl2` מצהיר
Conflicts מול *שני* האחרים => כל הזוגות מכוסים, דו-כיווני) — הפעלת אחת עוצרת אוטומטית
את השאר. **אי אפשר שניים מהם בו-זמנית עם SDR אחד.** מעבר מצב = ~3 שניות.

---

## 3. מבנה המאגר (file-by-file)

```
install.sh                  # מתקין-על אחד (פקודה אחת). אידמפוטנטי. 8 שלבים — ראה §10.
VERSION                     # מספר הגרסה (SemVer). מוצג בכותרת ה-UI. מתעדכן בכל PR.
CHANGELOG.md                # Keep a Changelog. כל PR מוסיף תחת [Unreleased]; מיזוג → גרסה.
README.md                   # תיעוד למשתמש הקצה (התקנה + שימוש מלא). עברית.
CLAUDE.md                   # ← המסמך הזה: ארכיטקטורה + פיתוח.

webtune/
  app.py                    # ★ הליבה: שרת Flask. בורר תדרים, ACARS, VDL2, REST API, יומן,
                            #   הקלטות, תמלול, METAR, מדדי RF, מעבר מצבים. ~2200 שורות.
  adsb.py                   # ניתוח ADS-B עצמאי: מסלול פעיל + שיבוש GPS. thread נפרד.
                            #   ניתן להרצה ידנית: `python3 adsb.py [--selftest]`.
  static/
    index.html              # ה-UI כולו (HTML+CSS+JS inline, ~3200 שורות). PWA. תצוגת VDL2
                            #   בפקטורי createDataView (מופע נפרד מ-ACARS — אפס רגרסיה).
    manifest.webmanifest    # PWA manifest (התקנה כאפליקציה).
    sw.js                   # Service Worker (נדרש HTTPS).
    icon-*.png, apple-touch-icon.png
    vendor/leaflet/         # Leaflet vendored (מפת ACARS/VDL2, בלי CDN).

config/
  airband.conf             # קונפיג ברירת-מחדל ל-rtl_airband (ATIS 132.5). ⚠ נדרס ע"י app.py בכל tune.
  acars.env               # ברירת-מחדל ל-acarsdec (EnvironmentFile). ⚠ נדרס ע"י app.py בכל מעבר ACARS.
  vdl2.env                # ברירת-מחדל ל-dumpvdl2 (EnvironmentFile). ⚠ נדרס ע"י app.py בכל מעבר VDL2.
                          #   ⚠ התדרים ב-Hz (dumpvdl2), בעוד state/UI ב-MHz.

systemd/
  sdrplay.service          # שירות SDRplay API.
  rtl_airband.service      # קול. Requires=sdrplay, Conflicts=airam-acars. root. Restart=always.
  airam-acars.service      # ACARS. Conflicts=rtl_airband. *לא* enabled (מופעל לפי המצב ב-UI). root.
  airam-vdl2.service       # VDL2 (dumpvdl2). Conflicts=rtl_airband+airam-acars. *לא* enabled. root.
  airam-web.service        # שרת הווב. User=airam (לא-root). Restart=always.

scripts/
  airam-wait-sdrplay       # שער מוכנות (ExecStartPre): מחכה שה-API *באמת* יענה, ומרים
                           # מחדש את sdrplay אם הוא "active" אבל ServiceNotResponding.

udev/
  99-airam.rules           # חיבור RSP1B (Vendor 1df7) → restart אוטומטי לשירותי SDR.

tests/                     # pytest. רצים ב-CI ללא חומרה (SDR/systemd ממוקפים).
  conftest.py              # מוסיף webtune/ ל-sys.path.
  test_app.py              # render_config, parse, presets, מדדים, יומן (389 שורות).
  test_acars.py            # נרמול ACARS, latlon, labels, ATIS, OOOI, actype (453 שורות).
  test_vdl2.py             # נרמול VDL2 (מסלול A/B), env, מעברי מצב, ייצוא (35 טסטים).
  test_security.py         # _guard: Origin/CSRF, PIN (55 שורות).

.github/workflows/ci.yml   # pytest + `bash -n` על install.sh ו-airam-wait-sdrplay.
```

> **`app.py` ו-`index.html` הם הקבצים שתיגע בהם הכי הרבה.** ה-UI כולו inline ב-HTML
> אחד (אין build step ל-frontend) — זו בחירה מכוונת לפשטות פריסה.

---

## 4. נתיבי runtime על ה-Pi (לא במאגר)

הקוד נפרס ל-`/opt/airam/`. מצב ונתונים ב-`/var/lib/airam/`. קונפיג ב-`/etc/`.

| נתיב | תוכן | נכתב ע"י |
|------|------|----------|
| `/opt/airam/webtune/` | הקוד הפרוס (app.py, adsb.py, static) | install.sh |
| `/etc/rtl_airband/airband.conf` | קונפיג קול חי (תדר נבחר) | app.py בכל `/api/tune` |
| `/etc/airam/acars.env` | תדרי ACARS חיים | app.py בכל מעבר ל-ACARS |
| `/etc/airam/vdl2.env` | תדרי VDL2 חיים (**ב-Hz**), gain, msg-filter | app.py בכל מעבר ל-VDL2 |
| `/etc/airam/airam.env` | env אופציונלי (PIN, whisper) — `EnvironmentFile=-` | install.sh / ידני |
| `/var/lib/airam/state.json` | מצב אחרון (תדר, mod, gain, squelch, app_mode: voice/acars/vdl2/off, acars_freqs, vdl2_freqs) | app.py |
| `/var/lib/airam/presets.json` | פריסטים (נערכים מה-UI) | app.py |
| `/var/lib/airam/acars.jsonl` | היסטוריית ACARS (שורדת restart, retention 5000) | _acars_listener |
| `/var/lib/airam/vdl2.jsonl` | היסטוריית VDL2 (שורדת restart, retention 5000) | _vdl2_listener |
| `/var/lib/airam/activity.jsonl` | יומן שידורים (retention 500) | _activity_watcher |
| `/var/lib/airam/recordings/` | הקלטות MP3 (200 קבצים / 100MB) | rtl_airband, נמחק ע"י app.py |
| `/run/rtl_airband_stats.txt` | מדדי RF (tmpfs, ~1Hz) | rtl_airband |

---

## 5. `webtune/app.py` — מפת הקוד

הקובץ מאורגן בבלוקים מסומנים `# --- ... ---`. נקודות עיקריות:

- **קבועים (ל~145):** נתיבים, gain של SDRplay (IFGR 20–59 / RFGR 0–9, **קטן=רווח גדול**),
  ספי squelch, קבועי ACARS, מילוני `ACARS_LABELS` ו-`_ACARS_DIR_BY_LABEL`, הקלטות, whisper.
- **`_guard` (before_request):** אכיפת אבטחה לכל בקשה משנת-מצב — בדיקת `Origin==Host`
  (CSRF/DNS-rebind) + PIN אופציונלי. **כל route שמשנה מצב חייב לעבור דרכו.**
- **בניית קונפיג קול:** `render_config` → `write_config` (כתיבה אטומית), `_squelch_line`
  (מקור-אמת יחיד לשורת ה-squelch). תמיד ערוץ יחיד ממורכז (centerfreq מוסט ב-DC_OFFSET).
- **restart מאומת + רולבק:** `_restart_and_verify` בודק שה-SDR נוכח ושהשירות עלה;
  `_rollback` מחזיר לקונפיג קודם אם נכשל. כיוונון אחד בכל רגע (`TUNE_LOCK`).
- **ACARS:** `_acars_listener` (thread, מאזין UDP 5556), `_normalize_acars` (הלב —
  ממיר JSON גולמי לכרטיס אחיד: label→קטגוריה+כיוון, חילוץ נ"צ, ARINC-622, actype),
  `_text_latlon`/`_scan_latlon` (חילוץ מיקום), פרסרים לפי label: `_parse_atis` (A9),
  `_parse_oooi_80` (80), `_parse_wx_alternates` (WX), `_parse_sa_media` (SA),
  `_parse_h1`+`_parse_fpn` (H1 sub-labels + תוכנית טיסה), `_parse_label15` (נ"צ, גם עם
  error — מבני), `_parse_sq` (squitter תחנה, בלי נ"צ), `_parse_autotune` (`:;`),
  `_parse_loadsheet` (C1, ZFW/TOW/נוסעים), `_parse_pdc` (A3, אישור טרום-המראה),
  `_parse_label16`/`_parse_nav_fuel` (16/1L, נ"צ עשרוני — לא-מתועדים ב-ARINC, זוהו
  מקליטה אמיתית; **CPDLC נבדק ונמצא ללא תעבורה בפועל** בקליטה שנבדקה — לא מומש),
  `_acars_adsb` (העשרת ADS-B לזנבות שבזיכרון — ראה §6),
  `_enter_acars` (כתיבת env + מעבר שירות), `_enter_standby` (כיבוי **שלושת** הצרכנים, משאיר
  sdrplay חי), `_acars_window_error` (wrapper דק מעל `_window_error` הגנרי — ≤8 ערוצים,
  span ≤ `ACARS_WINDOW_MHZ`), `ACARS_BANKS` (בנקי תדרים, כל בנק בחלון אחד), `_today_start`.
- **VDL2:** `_vdl2_listener` (thread, מאזין UDP 5557), `_normalize_vdl2` (הלב — סכמת
  dumpvdl2 v2.6.0: **מסלול A** — `avlc.acars` קיים ⇒ מסנתז dict בסגנון acarsdec ומזרים
  דרך `_normalize_acars` ⇒ *כל* הפרסרים הקיימים חלים בחינם; **מסלול B** — CPDLC/ADS-C
  (`avlc.x25`, תקציר `_libacars_decode`) / XID / פריים גנרי. שדה `icao` חדש = כתובת
  ה-AVLC של צד-המטוס; `dir` מבני מסוג הכתובת דורס heuristics), `write_vdl2_env` (**ממיר
  MHz→Hz**, `VDL2_GAIN` מכיל את הדגל כולו או ריק), `_enter_vdl2` (עוצר rtl_airband+acars,
  מרים dumpvdl2, verify), `_vdl2_window_error`, `_vdl2_adsb`, `VDL2_BANKS`. התמדה:
  `_append_vdl2_log`/`_trim_vdl2_log`/`_load_vdl2_history` (clones של צמד ה-ACARS).
- **REST API** (ראה §8). **יומן/הקלטות:** `_activity_watcher` (thread סורק MP3 חדשים),
  `_transcribe_worker` (thread whisper אופציונלי), `_sweep_recordings` (retention).
- **`__main__`:** מבטיח קונפיג עדכני, מרים threads (activity, acars, **vdl2**, transcribe,
  adsb), `app.run(threaded=True)` — threaded **חובה** כי `/stream` הוא חיבור ארוך-טווח.

---

## 6. `webtune/adsb.py` — מסלול פעיל + שיבוש GPS

מודול עצמאי (אפשר להריץ ולבדוק לבד). thread מושך פעם בדקה מטוסים סביב נתב"ג
ממקור ADS-B קהילתי (adsb.lol, גיבוי adsb.fi) ומסיק:

- **מסלול פעיל:** מזהה מטוסים בגישה סופית (יורדים, מהירות גישה, track בכיוון מסלול)
  ובטיפוס (המראות). המסלול עם האירועים הטריים ביותר (דעיכה מעריכית) הוא הפעיל.
- **עמידות לשיבוש GPS:** באזור נתב"ג ה-GPS משובש כרונית. `track` והגובה הברומטרי
  **שורדים** את השיבוש → מטוס עם `NIC<2` (מיקום מזויף) מסווג **לפי כיוון בלבד**
  (הקורסים בנתב"ג רחוקים >40°). אינדיקציית שיבוש = % מטוסים מעל 5000ft עם NIC<7
  (שיטת gpsjam.org, בזמן אמת).
- **בידוד:** רץ ב-thread; `/api/airspace` מגיש מ-snapshot בזיכרון בלבד → **תקלת רשת
  לא נוגעת בנתיב הרדיו**. כל הפונקציות הגאומטריות טהורות ונבדקות ב-`--selftest` (בלי רשת).
- **snapshot פר-מטוס (היתוך ACARS↔ADS-B):** `process()` שומר רשומה לכל מטוס עם רישום
  (`_S["aircraft"]`, מפתח `norm_reg` — מנרמל `.4X-EHD`↔`4X-EHD`; גיזום אחרי
  `AC_KEEP_SEC`=10 דק'). `aircraft_snapshot(regs)` מגיש עותקים (עם `age`) ל-`/api/acars`
  להעשרת ה-roster/מפה. עמידות שיבוש: `nic<SPOOF_NIC` ⇒ נ"צ מדוכא (`spoofed=True`),
  גובה/track/מהירות נשמרים.

החלפת מיקום השדה: ערוך `ARP_LAT/ARP_LON/RUNWAYS` בראש הקובץ.

---

## 7. ה-UI (`static/index.html`)

HTML יחיד עם CSS+JS inline. PWA (manifest + sw.js + MediaSession לשמע ברקע).
מתג מצב 📻/📡/🛰️ בראש. הדף מושך מצב מ-`/api/*` ב-polling. עיצוב responsive (multi-column
בטאבלט/דסקטופ). **אין build step** — עורכים את הקובץ ישירות. Leaflet vendored תחת
`static/vendor/` (בלי CDN, עובד גם בלי אינטרנט — אריחי OSM יורדים חיננית בלי רשת).

**תצוגת ACARS מול VDL2:** ACARS ממומש ישירות (globals `acars*` + `renderAcarsMsg`
וכו'). תצוגת VDL2 ממומשת ב-**פקטורי `createDataView({prefix:"vdl2", ...})`** — מופע סגור
(closures) עם state/מפה/buffers משלו, לגמרי נפרד מ-ACARS (⇒ אפס רגרסיה ל-ACARS; ה-DOM
של `#vdl2View` משתמש חוזר במחלקות ה-CSS `.acars-*`, אפס CSS חדש). הפקטורי עושה שימוש
חוזר בעוזרים ה*טהורים* הגלובליים בלבד (`fmtTime`/`mkSpan`/`dirBadge`/`normReg`/`trackColor`/
`CAT_GROUPS`/`MULTIBLOCK_RE`/`segSet`). `showView`/`applyMode` מרובעים (voice/acars/vdl2/off).

> בעריכת ה-UI: שמור על polling קל, על נפילה חיננית בלי רשת, ועל RTL/עברית נכונה.
> שינוי שנוגע בשתי התצוגות — עדכן גם את קוד ה-ACARS וגם את הפקטורי (הם אינם משתפים קוד stateful).

---

## 8. REST API (כל ה-routes)

| Method | Route | תיאור |
|--------|-------|------|
| GET | `/` | הדף הראשי |
| GET | `/<path>` | נכסים סטטיים |
| GET | `/live.m3u` | playlist לנגן חיצוני |
| GET | `/stream` | proxy same-origin ל-Icecast (נדרש כש-HTTPS, mixed-content) |
| GET | `/api/state` | המצב הנוכחי (תדר, mod, gain, squelch, app_mode, `acars_banks`, `vdl2_banks`, `vdl2_freqs`) |
| GET/PUT | `/api/presets` | קריאה/עדכון פריסטים |
| POST | `/api/tune` | **כיוונון תדר** (קול). דרך `_guard`. |
| POST | `/api/mode` | **מעבר מצב** voice/acars/**vdl2**/**off** (standby). דרך `_guard`. |
| GET | `/api/acars` | הודעות ACARS אחרונות (**היום בלבד**; `?all=1` לכל מה שבזיכרון) + שדה `adsb` (העשרת ADS-B לזנבות שבפיד; `{}` בלי אינטרנט) |
| GET | `/api/acars/export?format=csv\|json` | ייצוא (CSV עם BOM) |
| GET | `/api/vdl2` | הודעות VDL2 אחרונות (**היום בלבד**; `?all=1`) + שדה `adsb`. אותה סכמת כרטיס כמו ACARS + `icao` |
| GET | `/api/vdl2/export?format=csv\|json` | ייצוא VDL2 (CSV עם BOM, עמודת `icao`) |
| GET | `/api/activity` | יומן שידורים |
| GET | `/recordings/<name>` | קובץ הקלטה MP3 |
| GET | `/api/metrics` | מדדי RF (SNR/signal/noise מ-stats_filepath) |
| GET | `/api/airspace` | מסלול פעיל + שיבוש GPS (מ-adsb.py) |
| GET | `/api/power` | מתח/טמפ' ה-Pi (`vcgencmd`) |
| GET | `/api/metar` | METAR נתב"ג (LLBG) |
| GET | `/api/health` | בריאות השירותים |

**כלל:** כל route שמשנה מצב חומרה/קונפיג = `POST` + עובר `_guard` + (אם רלוונטי) `TUNE_LOCK`.

---

## 9. מודל האבטחה (אל תשבור אותו)

- **`airam-web` רץ כמשתמש לא-root (`airam`).** גישתו ל-root מוגבלת ל-sudoers ממוקד:
  *רק* פקודות `systemctl` ספציפיות (restart rtl_airband, start/stop של המצבים).
- **`acars.env`/`vdl2.env` מנותחים ע"י systemd (`EnvironmentFile`), לא ע"י shell** → קובץ
  שכותב `airam` לא יכול להסליל הרצת קוד כ-root. **אל תעביר את הקבצים האלה דרך bash source.**
- **מאזינים בלי סיסמה;** סיסמת ה-source ל-Icecast פנימית קבועה (`airam`).
- **PIN אופציונלי** דרך `AIRAM_PIN` ב-`/etc/airam/airam.env` (כבוי כברירת מחדל).
- **הגנת CSRF/DNS-rebind:** `_guard` דוחה בקשות משנות-מצב כש-`Origin != Host`.
- מיועד **לרשת פרטית בלבד**. אל תחשוף 8080/8000 לאינטרנט; לגישה מרחוק — VPN/Tailscale.

---

## 10. `install.sh` — 8 שלבים (אידמפוטנטי)

1. תלויות מערכת (Python/Flask, `libglib2.0-dev` ל-dumpvdl2 וכו'). 2. **SDRplay API**
(הורדת `.run`, חילוץ והתקנה ללא אישור רישיון ידני — `SDRPLAY_VER` בראש הקובץ).
3. בניית `SoapySDRPlay3`. 4. בניית `rtl_airband` (4b: `libacars` ≥2.1.0 + `acarsdec`
ל-ACARS; **4c: `dumpvdl2` ל-VDL2, נעוץ ל-`DUMPVDL2_VER=v2.6.0`, חתימת בנייה**).
5. `Icecast2` (מאזין בלי סיסמה). 6. קונפיג התחלתי + state (6b: יצירת משתמש `airam` +
sudoers ממוקד — **6 פקודות systemctl**: restart/stop × rtl_airband/airam-acars/airam-vdl2;
seeding של `acars.env`+`vdl2.env`). 7. שרת הווב (7b: תמלול whisper אופציונלי,
`INSTALL_WHISPER=1`). 8. שירותי systemd (`airam-acars` ו-`airam-vdl2` מותקנים אך **לא**
enabled — מופעלים לפי המצב ב-UI).

הסקריפט **בונה מחדש רק כשצריך** (חתימת בנייה פר-רכיב) ובסוף **מפעיל מחדש את כל השירותים**
→ אין reboot. דגלים: `INSTALL_WHISPER=1` (תמלול), עדכון `SDRPLAY_VER`/`DUMPVDL2_VER` בגרסה חדשה.

---

## 11. זרימת פיתוח, גרסאות ובדיקות

- **גרסאות (SemVer):** כל PR מעדכן את `VERSION` ומוסיף שורות תחת `[Unreleased]`
  ב-`CHANGELOG.md`. במיזוג ל-main מקדמים לכותרת גרסה ומתייגים את ה-commit `vX.Y.Z`.
  MAJOR=שובר · MINOR=פיצ'ר · PATCH=תיקון. **מספר הגרסה מוצג בכותרת ה-UI.**
- **בדיקות:** `python -m pytest tests/ -v`. כל מה שתלוי ב-SDR/systemd **ממוקף**
  בבדיקות עצמן → רצות בלי חומרה. הוסף בדיקה לכל שינוי backend (זו מוסכמה בפרויקט —
  ראה ה-CHANGELOG: כל גרסה כמעט הוסיפה בדיקות).
- **CI** (`.github/workflows/ci.yml`): pytest על Python 3.11 (כמו Pi OS Bookworm)
  + `bash -n` (בדיקת תחביר) על `install.sh` ו-`airam-wait-sdrplay`. **שמור את שניהם ירוקים.**
- **בדיקה ידנית של adsb:** `python3 webtune/adsb.py --selftest` (בלי רשת).

---

## 12. מוסכמות וגוצ'אות (קרא לפני שינוי)

- **כיוונון אחד בכל רגע:** RSP1B יחיד = תדר/מצב אחד פעיל. אל תנסה ריבוי ערוצים בו-זמני.
- **ארבעה מצבי `app_mode`:** `voice` (rtl_airband) · `acars` (acarsdec) · `vdl2` (dumpvdl2) ·
  `off` (standby — **שלושת** הצרכנים עצורים, ה-SDR פנוי ליישום אחר). `off` **אינו שורד
  reboot** (רק rtl_airband enabled). `api_state`/`api_health` גוזרים את המצב מהמציאות
  (מציאות-תחילה, מרובע) + intent; standby ≠ תקלה. `_enter_standby`/`_voice_tune` עוצרים
  את *שני* הצרכנים האחרים.
- **בנקי ACARS/VDL2 = חלון אחד כל אחד:** acarsdec/dumpvdl2 מפענחים ערוצים מרובים בתוך
  חלון דגימה אחד (~2MHz). צביר 131.x ו-136.x רחוקים ~5MHz ⇒ לעולם לא יחד. בנק חדש חייב
  לעבור `_acars_window_error`/`_vdl2_window_error` (שניהם wrappers מעל `_window_error`).
  הצבא/תדלוק אמריקאי = רשת ARINC/SITA אזרחית (בפועל 131.550), **אין** תדר צבאי נפרד.
  תדרי VDL2 כולם ב-136.7–137.0 (span 250kHz) ⇒ תמיד חלון אחד; 136.975 הוא ה-CSC העולמי.
- **⚠ VDL2 env ב-Hz, state/UI ב-MHz:** dumpvdl2 מקבל תדרים ב-Hz. `write_vdl2_env` הוא
  **המקום היחיד** שממיר MHz→Hz; בכל שאר המקומות (state, `VDL2_BANKS`, UI, `_sanitize_freqs`)
  התדרים הם מחרוזות MHz — כמו ACARS. אל תערבב.
- **`config/airband.conf` · `config/acars.env` · `config/vdl2.env` נדרסים** ע"י `app.py`
  בזמן ריצה. לשנות ברירת מחדל קבועה — ערוך גם את הדיפולט בקוד (`ACARS_BANKS`/`VDL2_BANKS`/
  `ACARS_FREQS_DEFAULT`/`VDL2_FREQS_DEFAULT` וכו').
- **msg-filter של VDL2** (`VDL2_MSG_FILTER`): מסנן בצד המפענח רעש שהיה מציף את הפיד
  (supervisory, ACK ריקים, **GSIF squitters** שמשודרים כל כמה שניות), ושומר acars +
  x25-data (CPDLC/ADS-C) + xid. `_normalize_vdl2` עדיין סובל כל סוג פריים (הסינון קונפיג, לא הבטחה).
- **gain של SDRplay הפוך:** ערך **קטן יותר = רווח גדול יותר** (IFGR/RFGR הן *הפחתות*).
- **בידוד מקורות חיצוניים:** כל קריאת רשת (ADS-B, METAR) חייבת לרוץ ב-thread ולהיכשל
  חיננית — **הרדיו לעולם לא תלוי באינטרנט.**
- **כתיבה אטומית** לקבצי קונפיג/state (`_atomic_write`) — אסור להשאיר קובץ חצי-כתוב.
- **threaded=True חובה** ל-Flask (סטרים ארוך-טווח לא חוסם בקשות).
- **שמע ברקע = MediaSession** (אין API ל"שיחת טלפון" בדפדפן). אל תחפש חלופה.
- **עברית ב-RTL** ב-UI; CSV עם BOM ל-Excel.

---

## 13. צ'קליסט: הוספת פיצ'ר / תיקון באג

1. **הבן את ההקשר:** קרא את §2 (ארכיטקטורה) ואת הבלוק הרלוונטי ב-§3/§5/§6.
2. **שנה את הקוד** במקום הנכון (רוב השינויים: `webtune/app.py` ו/או `static/index.html`;
   ניתוח אווירי: `adsb.py`; פריסה/בנייה: `install.sh`+`systemd/`).
3. **שמור על המודל:** בידוד רשת, אבטחת `_guard`/sudoers, כתיבה אטומית, SDR-אחד.
4. **הוסף/עדכן בדיקות** ב-`tests/` (מקף את ה-SDR/systemd). ודא `pytest` ירוק.
5. **עדכן `VERSION`** (SemVer) ו**הוסף שורות ל-`CHANGELOG.md`** תחת `[Unreleased]`.
6. **עדכן תיעוד למשתמש** ב-`README.md` אם ההתנהגות/השימוש משתנים.
7. **עדכן את `CLAUDE.md` הזה** אם הוספת מודול, route, נתיב runtime או מוסכמה.
8. **commit + push** לענף המיועד. הודעות commit בעברית, תיאוריות.

> **כשמשהו לא ברור** (מיקום שדה, תדרים, התנהגות חומרה) — README ו-CHANGELOG הם
> מקור-האמת ההיסטורי; קרא את ה-CHANGELOG כדי להבין *למה* קוד קיים בנוי כפי שהוא.
