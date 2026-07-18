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

**תפיסת ההפעלה — אין "מצב ראשי", airam-web הוא המתזמר:** קול/ACARS/VDL2 הם
"אפליקציות" שוות-מעמד על משאב ה-SDR, ו-`off` (standby) הוא המצב הניטרלי היחיד.
**אף צרכן SDR אינו enabled ב-systemd** (גם לא rtl_airband!) — `airam-web` (שעולה
תמיד) קורא את `state.json` באתחול ומשחזר את המצב השמור דרך `_boot_restore` =>
**המצב שורד reboot, כולל off**. כישלון כניסה לכל מצב נופל ל-`off` עם שגיאה ב-UI
(`_fail_to_off`) — **לעולם אין fallback לקול**. עצירה מכל מצב = standby (מסך הבית).

---

## 3. מבנה המאגר (file-by-file)

```
install.sh                  # מתקין-על אחד (פקודה אחת). אידמפוטנטי. 8 שלבים — ראה §10.
VERSION                     # מספר הגרסה (SemVer). מוצג בכותרת ה-UI. מתעדכן בכל PR.
CHANGELOG.md                # Keep a Changelog. כל PR מוסיף תחת [Unreleased]; מיזוג → גרסה.
README.md                   # תיעוד למשתמש הקצה (התקנה + שימוש מלא). עברית.
CLAUDE.md                   # ← המסמך הזה: ארכיטקטורה + פיתוח.

webtune/
  app.py                    # ★ הליבה: שרת Flask. בורר תדרים, ACARS, VDL2, SATCOM, סריקה,
                            #   רוסטר מאוחד, REST API, יומן, הקלטות, תמלול, METAR, מדדי RF,
                            #   מעבר מצבים, ארכיון חיפוש. ~3000 שורות.
  adsb.py                   # ניתוח ADS-B עצמאי: מסלול פעיל + שיבוש GPS. thread נפרד.
                            #   ניתן להרצה ידנית: `python3 adsb.py [--selftest]`.
  static/
    index.html              # ה-UI כולו (HTML+CSS+JS inline, ~3550 שורות). PWA. 4 תצוגות:
                            #   🏠 מרכז (בית/standby/scan) + קול + ACARS + VDL2. ACARS ו-VDL2
                            #   שני מופעים סימטריים של אותו פקטורי createDataView (ר' §7).
    manifest.webmanifest    # PWA manifest (התקנה כאפליקציה).
    sw.js                   # Service Worker (נדרש HTTPS).
    icon-*.png, apple-touch-icon.png
    vendor/leaflet/         # Leaflet vendored (מפת ACARS/VDL2, בלי CDN).

config/
  airband.conf             # קונפיג ברירת-מחדל ל-rtl_airband (ATIS 132.5). ⚠ נדרס ע"י app.py בכל tune.
  acars.env               # ברירת-מחדל ל-acarsdec (EnvironmentFile). ⚠ נדרס ע"י app.py בכל מעבר ACARS.
  vdl2.env                # ברירת-מחדל ל-dumpvdl2 (EnvironmentFile). ⚠ נדרס ע"י app.py בכל מעבר VDL2.
                          #   ⚠ התדרים ב-Hz (dumpvdl2), בעוד state/UI ב-MHz.
  satcom.env              # ברירת-מחדל ל-inmarsat-sniffer (EnvironmentFile). ⚠ נדרס ע"י app.py
                          #   בכל מעבר SATCOM. לוויין (AF1 ברירת מחדל), gain, bias-tee.

systemd/
  sdrplay.service          # שירות SDRplay API. enabled.
  rtl_airband.service      # קול. Requires=sdrplay, בלי [Install] — *לא* enabled. root. Restart=always.
  airam-acars.service      # ACARS. Conflicts=rtl_airband. *לא* enabled. root.
  airam-vdl2.service       # VDL2 (dumpvdl2). Conflicts=rtl_airband+airam-acars. *לא* enabled. root.
  airam-satcom.service     # SATCOM (inmarsat-sniffer). Conflicts=שלושת האחרים. *לא* enabled. root.
                           #   אף צרכן SDR לא עולה באתחול — airam-web (המתזמר) משחזר את המצב השמור.
  airam-web.service        # שרת הווב + המתזמר. enabled. User=airam (לא-root). Restart=always.

scripts/
  airam-wait-sdrplay       # שער מוכנות (ExecStartPre): מחכה שה-API *באמת* יענה, ומרים
                           # מחדש את sdrplay אם הוא "active" אבל ServiceNotResponding.

udev/
  99-airam.rules           # חיבור RSP1B (Vendor 1df7) → restart אוטומטי לשירותי SDR.

tests/                     # pytest. רצים ב-CI ללא חומרה (SDR/systemd ממוקפים).
  conftest.py              # מוסיף webtune/ ל-sys.path.
  test_app.py              # render_config, parse, presets, מדדים, יומן, רולבק/נפילה-ל-off.
  test_acars.py            # נרמול ACARS, latlon, labels, ATIS, OOOI, actype, מעברי מצב.
  test_vdl2.py             # נרמול VDL2 (מסלול A/B), env, מעברי מצב, ייצוא, health.
  test_satcom.py           # נרמול SATCOM (inmarsat-sniffer JSON, מאומת מהמקור), env, מעברי מצב, ייצוא.
  test_boot.py             # _boot_restore: שחזור המצב באתחול (המתזמר) — כולל SATCOM.
  test_scan.py             # מצב סריקה: validate_scan_plan, _scan_loop, /api/mode, /api/scan, boot restore.
  test_roster.py           # רוסטר מטוסים מאוחד: היתוך זהות ACARS/VDL2/ADS-B, מיון, /api/aircraft.
  test_archive.py          # ארכיון חיפוש רב-יומי: _day_bounds, ?day= ב-/api/acars ו-/api/vdl2.
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
| `/etc/airam/satcom.env` | לוויין נבחר (`AF1`=Alphasat וכו'), gain, bias-tee (`-B`) | app.py בכל מעבר ל-SATCOM |
| `/etc/airam/airam.env` | env אופציונלי (PIN, whisper) — `EnvironmentFile=-` | install.sh / ידני |
| `/var/lib/airam/state.json` | מצב אחרון (תדר, mod, gain, squelch, app_mode: voice/acars/vdl2/satcom/off, acars_freqs, vdl2_freqs, satcom_freqs) | app.py |
| `/var/lib/airam/presets.json` | פריסטים (נערכים מה-UI) | app.py |
| `/var/lib/airam/acars.jsonl` | היסטוריית ACARS (שורדת restart, retention 5000) | _acars_listener |
| `/var/lib/airam/vdl2.jsonl` | היסטוריית VDL2 (שורדת restart, retention 5000) | _vdl2_listener |
| `/var/lib/airam/satcom.jsonl` | היסטוריית SATCOM (שורדת restart, retention 5000) | _satcom_listener |
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
  `_rollback` מחזיר לקונפיג קודם אם נכשל **ומאומת** (מחזיר bool; כישלון ⇒ `_fail_to_off`).
  כיוונון אחד בכל רגע (`TUNE_LOCK`).
- **רגיסטרי מצבים (שוויון מצבים):** `MODE_SERVICE` (מצב→שירות), `_live_mode()` (המצב
  שרץ בפועל או None), `_enter_voice` (peer סימטרי של `_enter_acars`/`_enter_vdl2`:
  עצירת צרכני דאטה + write_config + verify), `_fail_to_off` (כישלון כניסה ⇒ standby +
  state off+prev_mode + payload 500 — **לעולם לא fallback לקול**). `/api/mode` הוא
  dispatcher מאוחד לחמשת המצבים (voice/acars/vdl2/off/**scan**). `_boot_restore`
  (thread ב-startup) משחזר את המצב השמור באתחול — ה-orchestration שמאפשר לאף צרכן
  לא להיות enabled ב-systemd.
- **מצב סריקה/סבב (scan) — "אפליקציית-על" מעל שלושת המצבים, לא צרכן/שירות רביעי:**
  `_validate_scan_plan` (מאמת לוח: 1–8 "רגלים" `{mode,dwell_sec,freqs?,active_from?,
  active_to?}` — שני שדות חלון-השעות חייבים להופיע ביחד, "HH:MM"),
  `_leg_active_now` (האם הרגל בחלון השעות שלה כרגע — שעון מקומי, תומך בחלון
  שחוצה חצות; בלי חלון בכלל = תמיד פעילה), `_scan_enter_leg` (כניסה לרגל בודדת
  דרך `_enter_voice`/`_enter_acars`/`_enter_vdl2` — לא נועל TUNE_LOCK, כמו שאר
  ה-`_enter_*`), `_scan_activate` (מוצא את הרגל הראשונה שבחלון השעות שלה כרגע
  ונכנס אליה סינכרונית + מתחיל thread לשאר הלוח; אם אף רגל לא בחלון — **לא כשל**,
  ה-SDR נשאר כבוי ו-thread ממתין), `_scan_loop` (thread: מסתובב בין הרגלים, נועל
  TUNE_LOCK רק בזמן מעבר; רגל מחוץ לחלון מדולגת מיד — לא כשל; סבב שלם בלי אף
  רגל בחלון ⇒ **מכבה בפועל את הצרכן שרץ** (`_enter_standby`, לא רק מסתיר את
  החיווי) ומחכה `SCAN_WINDOW_RECHECK_SEC`=30 שניות לפני שבודק שוב, במקום
  busy-loop; רגל שזהה בדיוק (מצב+תדרים) לרגל שכבר רצה לא נכנסת מחדש — נמנעים
  מ-`systemctl restart` מיותר בלוח עם רגל יחידה/חוזרת; כשל *כניסה* ברגל ⇒
  דילוג לבאה; כשל של *כל* הרגלים ברצף ⇒ `off`, כמו כל מצב), `_scan_stop_thread`
  (עוצר את הסבב הפעיל — נקרא ב-`/api/mode` **רק אחרי** שהבקשה עברה ולידציה
  סטטית ותפיסת TUNE_LOCK, לא לפני — אחרת בקשה שנכשלת ב-400/409 הייתה עוצרת
  סבב תקין בחינם ("scan זומבי": משאירה צרכן רץ בלי thread שממשיך אותו).
  `api_state`/`api_health` מיוחדים ל-scan: ה"מצב"
  הוא `scan` עצמו (לא הרגל הנוכחית) — הרגל/הספירה-לאחור מגיעות מ-`GET /api/scan`;
  `mode_ok`/`ok` נשארים `True` גם כש"ממתין לחלון" (אף רגל לא אמורה לרוץ כרגע) —
  לא רק כש-off מכוון.
- **ACARS:** `_acars_listener` (thread, מאזין UDP 5556), `_normalize_acars` (הלב —
  ממיר JSON גולמי לכרטיס אחיד: label→קטגוריה+כיוון, חילוץ נ"צ, ARINC-622, actype,
  **מדדי איכות קליטה** — `level`=dBFS מקורי מהמפענח (נשמר כמות שהוא); `snr` מחושב
  **רק** כש-`noise` קיים בקלט (acarsdec עצמו לא מספק רצפת רעש לכל הודעה — ראו
  bullet הבא — כך שב-ACARS אמיתי `snr` תמיד None; ה-dBm נדחה במכוון, ראו §12),
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
  span ≤ `ACARS_WINDOW_MHZ`), `ACARS_BANKS` (בנקי תדרים, כל בנק בחלון אחד), `_today_start`,
  `_day_bounds` (גבולות יום מקומי לארכיון החיפוש — `?day=YYYY-MM-DD` ב-`/api/acars`,
  קורא מהדיסק דרך `_read_acars_log`, לא מהזיכרון; משותף גם ל-VDL2).
- **VDL2:** `_vdl2_listener` (thread, מאזין UDP 5557), `_normalize_vdl2` (הלב — סכמת
  dumpvdl2 v2.6.0: **מסלול A** — `avlc.acars` קיים ⇒ מסנתז dict בסגנון acarsdec (כולל
  `noise=sig_level`/`noise_level` ⇒ SNR אמיתי) ומזרים דרך `_normalize_acars` ⇒ *כל*
  הפרסרים הקיימים חלים בחינם; **מסלול B** — CPDLC/ADS-C
  (`avlc.x25`, תקציר `_libacars_decode`) / XID / פריים גנרי (גם כאן `level`+`snr`
  ישירות מ-`sig_level`/`noise_level`). שדה `icao` חדש = כתובת
  ה-AVLC של צד-המטוס; `dir` מבני מסוג הכתובת דורס heuristics), `write_vdl2_env` (**ממיר
  MHz→Hz**, `VDL2_GAIN` מכיל את הדגל כולו או ריק), `_enter_vdl2` (עוצר rtl_airband+acars,
  מרים dumpvdl2, verify), `_vdl2_window_error`, `_vdl2_adsb`, `VDL2_BANKS`. התמדה:
  `_append_vdl2_log`/`_trim_vdl2_log`/`_load_vdl2_history` (clones של צמד ה-ACARS).
- **רוסטר מטוסים מאוחד:** `_aircraft_identity` (מפתח זהות מהודעה מנורמלת — רישום
  מנורמל קודם, אחרת icao, אחרת מספר טיסה), `_build_roster` (מהתך `_acars_msgs`+
  `_vdl2_msgs` לפי הזהות, מעשיר ב-`adsb.aircraft_snapshot`, ממוין lastT יורד,
  גזור ל-`ROSTER_MAX`) — **חי בכל מצב** (לא תלוי SDR הפעיל, ר' §12), `GET /api/aircraft`.
- **REST API** (ראה §8). **יומן/הקלטות:** `_activity_watcher` (thread סורק MP3 חדשים),
  `_transcribe_worker` (thread whisper אופציונלי), `_sweep_recordings` (retention).
- **`__main__`:** מרים את thread השחזור `_boot_restore` (מחזיר את המצב השמור, כולל
  שכתוב קונפיג ישן בשדרוג — `_config_stale`) + threads (activity, acars, **vdl2**,
  transcribe, adsb), `app.run(threaded=True)` — threaded **חובה** כי `/stream` הוא
  חיבור ארוך-טווח.

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
מתג תצוגות 🏠/📻/📡/🛰️ בראש — **🏠 מרכז (מסך הבית)** הוא ברירת המחדל והנחיתה של
מצב off/scan: כרטיס סטטוס, **ארבעה** כרטיסי הפעלה שווי-מעמד (📻/📡/🛰️/🔁 — `renderHome`/
`showHomeError`), פאנלי המסלולים/METAR (מערכתיים — עברו לכאן מתצוגת הקול), ופאנל
**רוסטר מטוסים מאוחד** (`renderRoster`/`pollRoster`, כל 20ש' — מציג היתוך ACARS+VDL2+
ADS-B מ-`GET /api/aircraft`, כולל כשה-SDR ב-standby). כרטיס
הסריקה (`#homeCardScan`) כולל עורך לוח (`renderScanEditor` — הוספה/הסרה/עריכת רגלים,
כולל שני שדות `<input type=time>` אופציונליים פר-רגל לחלון שעות (`active_from`/
`active_to`, ריק=תמיד); `scanLegs` בזיכרון בלבד עד לחיצת "התחל") וסטטוס חי (רגל
נוכחית + ספירה-לאחור מקומית כל שנייה מול `scanStatus.next_switch_at` שמגיע
מ-`GET /api/scan`; "ממתין לחלון הזמן הבא" כשאף רגל לא בחלון שלה כרגע). **אין תצוגה ייעודית
לסריקה** — `showView`/`applyMode` נשארים מרובעים (home/voice/acars/vdl2 בלבד); `scan`
תמיד ממופה ל-`"home"` (הוא "אפליקציית-על", לא תוכן-תצוגה). הדף מושך מצב מ-`/api/*`
ב-polling; ה-pollers של airspace/power/METAR/roster רצים בכל תצוגה, metrics/activity רק
בקול, ו-`pollGlobalState` (10ש', `/api/health` + `/api/scan` כשסורקים) מיישר את חיווי
המצב למציאות בלי לחטוף את הטאב. כפתורי עצירה ⇒ `applyMode("off")` (גם עצירת סריקה);
כפתור ⏻ מחזיר את `prev_mode` (יכול להיות `"scan"`). עיצוב responsive (multi-column
בטאבלט/דסקטופ; `.mode-cards` הוא `auto-fit` כדי לזרום נכון גם עם 4 כרטיסים).
**אין build step** — עורכים את הקובץ ישירות. Leaflet vendored תחת `static/vendor/`
(בלי CDN, עובד גם בלי אינטרנט).

**טוקני עיצוב ב-`:root`:** גיאומטריה (`--r-card`/`--r-ctrl`/`--r-sm`), מרווח (`--sp-1..5`,
בסיס 4px), הצללה (`--sh-card`/`--sh-1`/`--sh-blue`), משטחים (`--panel`/`--panel-2`/`--hover`)
וקווים (`--border`/`--border-strong`). **השתמש בטוקנים בקוד חדש** במקום ערכים קשיחים —
כך הקצב והגיאומטריה נשמרים עקביים, ושינוי במקום אחד מתפשט לכל הרכיבים.

**תצוגת ACARS ו-VDL2 — סימטריה מלאה:** שני מופעים עצמאיים של אותו **פקטורי
`createDataView(opts)`** — `var acars = createDataView({prefix:"acars", mode:"acars",
label:"ACARS", onMessage, onReset})` ו-`var vdl2 = createDataView({prefix:"vdl2",
mode:"vdl2", label:"VDL2"})`. כל מופע הוא closure סגור לגמרי (state/מפה/buffers/
cursor/filters משלו — כלום לא משותף בזיכרון בין השניים; ה-DOM של `#acarsView`/
`#vdl2View` משתמש חוזר במחלקות ה-CSS `.acars-*`/`.dl-*`, אפס CSS כפול). `opts.prefix`
קובע גם את ה-endpoint (`/api/`+prefix) וגם את פענוח ה-DOM ids (`E(suf) => $(prefix+suf)`);
`opts.label` קובע את טקסט הסטטוס ("ACARS כבוי"/"מאזין · VDL2" וכו'). שני hooks
אופציונליים (no-op כברירת מחדל, VDL2 לא מעביר אותם): `opts.onMessage(m)` — נקרא
לכל הודעה חדשה ב-`poll()` (ACARS: מזין את לוח ה-ATIS sticky ב-label A9); `opts.onReset()`
— נקרא כשה-cursor מתאפס (ACARS: מנקה את לוח ה-ATIS מהסשן הקודם). `opts.emptyHint`
— טקסט מותאם למצב "אין הודעות עדיין" (VDL2 בלבד: הפניה לתדר 136.975; ACARS מקבל
ברירת מחדל גנרית). הפקטורי עושה שימוש חוזר בעוזרים ה*טהורים* הגלובליים בלבד
(`fmtTime`/`mkSpan`/`dirBadge`/`normReg`/`trackColor`/`CAT_GROUPS`/`DIR_INFO`/
`MULTIBLOCK_RE`/`RETRANS_WINDOW_S`/`msgSig`/`patchMsgTime`/`reconcileFeed`/`segSet`/
`qualityCls` — מיון dBFS/SNR לשלוש רמות צבע). `showView` מרובע (home/voice/acars/
vdl2); `applyMode` מחומש (voice/acars/vdl2/off/scan — off ו-scan שניהם נוחתים
בתצוגת home).

**ארכיון חיפוש רב-יומי** (בתוך אותו פקטורי, שני התצוגות מקבלות בחינם): כרטיס
`.acars-archive` (בורר `<input type=date>` + כפתור "🔎 חפש בארכיון" + "◀ חזרה
לשידור חי") מעל תיבת החיפוש. `enterArchiveDay(day)` שולף `GET /api/<prefix>?day=`,
שומר את המצב החי הנוכחי ב-`liveSnapshot` (msgs/lastId/feedMax/feedCache — פעם
אחת, לא נדרס בביקורים חוזרים בארכיון), מחליף את `msgs` בתוכן היום שנבחר (מזהה
`id` סינתטי-רציף, אין ל-jsonl), עוצר polling, ובונה מחדש markers/craft/roster/
detail/feed מהנתונים הארכיוניים — **משתמש באותם renderStats/renderRoster/
renderFeed/renderDetail בדיוק**, בלי מסלול קוד נפרד. `exitArchive()` משחזר את
`liveSnapshot` ומחדש polling. `show()` **לא** מחדש polling אם `archiveDay` עדיין
מוגדר (מעבר בין תצוגות תוך כדי עיון בארכיון לא "שובר" אותו בטעות בחזרה).

> בעריכת ה-UI: שמור על polling קל, על נפילה חיננית בלי רשת, ועל RTL/עברית נכונה.
> שינוי שנוגע בשתי התצוגות — ערוך את הפקטורי `createDataView` (מקום אחד, שני המופעים
> יורשים); שינוי ACARS-only/VDL2-only בלבד — דרך `opts` (label/prefix/emptyHint) או
> hook חדש (`onMessage`/`onReset`), לא קוד מיוחד מחוץ לפקטורי.

---

## 8. REST API (כל ה-routes)

| Method | Route | תיאור |
|--------|-------|------|
| GET | `/` | הדף הראשי |
| GET | `/<path>` | נכסים סטטיים |
| GET | `/live.m3u` | playlist לנגן חיצוני |
| GET | `/stream` | proxy same-origin ל-Icecast (נדרש כש-HTTPS, mixed-content) |
| GET | `/api/state` | המצב הנוכחי (תדר, mod, gain, squelch, app_mode, `mode_ok` — המצב השמור באמת רץ?, `prev_mode`, `scan_plan`, `acars_banks`, `vdl2_banks`, `vdl2_freqs`) |
| GET/PUT | `/api/presets` | קריאה/עדכון פריסטים |
| POST | `/api/tune` | **כיוונון תדר** (קול). דרך `_guard`. |
| POST | `/api/mode` | **מעבר מצב** voice/acars/vdl2/satcom/off (standby)/**scan** (סבב). דרך `_guard`. `mode:"scan"` מקבל גם `plan` (רשימת רגלים; ברירת מחדל — הלוח השמור). `mode:"satcom"` מקבל `freqs` כרשימה בת-איבר-יחיד עם דגל הלוויין (למשל `["AF1"]`, ברירת מחדל — geostationary, לא בנק ערוצים). כישלון ⇒ נפילה ל-off: `{ok:false, error, detail, app_mode:"off", state}` + 500 |
| GET | `/api/scan` | סטטוס סבב הסריקה החי: `active`, `idx`, `leg`, `next_switch_at`, `plan` (ל-UI — רגל נוכחית + ספירה לאחור) |
| GET | `/api/acars` | הודעות ACARS אחרונות (**היום בלבד**; `?all=1` לכל מה שבזיכרון; `?day=YYYY-MM-DD` ארכיון מהדיסק, snapshot סטטי) + שדה `adsb` (העשרת ADS-B לזנבות שבפיד; `{}` בלי אינטרנט; לא ב-`?day=`). כל הודעה כוללת `level` (dBFS) ו-`snr` (None ב-ACARS — ראו §12) |
| GET | `/api/acars/export?format=csv\|json` | ייצוא (CSV עם BOM, עמודות `level`+`snr`) |
| GET | `/api/vdl2` | הודעות VDL2 אחרונות (**היום בלבד**; `?all=1`; `?day=YYYY-MM-DD` ארכיון) + שדה `adsb`. אותה סכמת כרטיס כמו ACARS + `icao`; `snr` תמיד אמיתי (dumpvdl2 מספק רצפת רעש) |
| GET | `/api/vdl2/export?format=csv\|json` | ייצוא VDL2 (CSV עם BOM, עמודות `icao`+`level`+`snr`) |
| GET | `/api/satcom` | הודעות SATCOM (Inmarsat, inmarsat-sniffer) אחרונות — אותם `?since=`/`?all=1`/`?day=`. אותה סכמת כרטיס כמו ACARS; **בלי** `level`/`snr`/`freq`/`adsb` (המפענח לא חושף אותם ב---feed/--udp — לעולם לא מומצאים, ראו §12) |
| GET | `/api/satcom/export?format=csv\|json` | ייצוא SATCOM (אותן עמודות כמו ACARS export) |
| GET | `/api/aircraft` | רוסטר מטוסים מאוחד — היתוך ACARS+VDL2+SATCOM+ADS-B לפי זהות (רישום/icao/טיסה). חי בכל מצב |
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
`INSTALL_WHISPER=1`). 8. שירותי systemd — **enabled רק `sdrplay`+`airam-web`**; אף צרכן
SDR (כולל rtl_airband) לא enabled, ובשדרוג `disable rtl_airband` אידמפוטנטי. המצב
משוחזר באתחול ע"י `_boot_restore` של airam-web.

הסקריפט **בונה מחדש רק כשצריך** (חתימת בנייה פר-רכיב) ובסוף מרים את `sdrplay`
(שמרים דרך PartOf את הצרכן *הפעיל*) ואת `airam-web` → אין reboot ואין העפה של
משתמשי דאטה לקול. דגלים: `INSTALL_WHISPER=1` (תמלול), עדכון `SDRPLAY_VER`/`DUMPVDL2_VER` בגרסה חדשה.

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
- **שישה מצבי `app_mode`, שווי-מעמד:** `voice` (rtl_airband) · `acars` (acarsdec) ·
  `vdl2` (dumpvdl2) · `satcom` (inmarsat-sniffer — ACARS דרך לוויין Inmarsat, ר' §5/
  docs/satcom-feasibility.md) · `off` (standby — **ארבעת** הצרכנים עצורים, ה-SDR פנוי
  ליישום אחר) · `scan` (סבב אוטומטי — **לא** צרכן/שירות נוסף, אלא thread שמסתובב בין
  קריאות ל-`_enter_voice`/`_enter_acars`/`_enter_vdl2` הקיימים; ראו §5. `satcom` **אינו**
  scannable כרגע — MVP מכוון).
  **אין "מצב ראשי"**: כל המצבים (כולל `off`/`scan`) **שורדים reboot** — אף צרכן לא
  enabled, `_boot_restore` של airam-web משחזר את המצב השמור באתחול (עבור `scan`:
  מוצא מחדש את הרגל הראשונה שבחלון השעות שלה — לא ממשיך מהאינדקס שבו נעצר, פשטות
  מכוונת). **כישלון כניסה למצב ⇒ נפילה ל-`off`** (`_fail_to_off`), לעולם לא fallback
  לקול; חריגים: (1) רולבק *בתוך* כיוונון קול לקונפיג האחרון שעבד (retry, לא
  עליונות-מצב), וגם הוא מאומת; (2) כשל ברגל *בודדת* בסריקה מדלג לרגל הבאה — רק
  כשל של *כל* הרגלים ברצף נופל ל-off; (3) רגל מחוץ לחלון השעות שלה (`active_from`/
  `active_to`) מדולגת בשקט — **גם זו לא תקלה**, ואם אף רגל לא בחלון כרגע ה-SDR
  נשאר כבוי ו-`mode_ok`/`ok` נשארים `True` ("ממתין", כמו standby מכוון).
  `api_state`/`api_health` גוזרים את המצב מהמציאות, ובאין צרכן פעיל — מהכוונה
  השמורה; מצב שמור שלא רץ = תקלה (`mode_ok=False`/`ok=False`), standby ≠ תקלה.
  ב-`scan` ה"מצב" המדווח הוא `scan` עצמו (לא הרגל הנוכחית) — ראו `/api/scan`.
  ברירת המחדל של state חסר היא `off` (התקנה טרייה נוחתת במסך הבית).
  `_enter_standby`/`_voice_tune` עוצרים את *שלושת* הצרכנים האחרים; `_scan_stop_thread`
  נקרא ב*כל* מעבר `/api/mode` (גם למעבר בין תוכן-לוח שונה של scan עצמו).
- **⚠ satcom דורש החלפת אנטנה פיזית *ידנית*** (VHF airband ↔ L-band+LNA) —
  ר' docs/satcom-feasibility.md §3. הבחירה במצב מהטלפון **לא** מבצעת את ההחלפה
  עצמה; ה-`satcom_freqs` ב-state הוא רשימה בת-איבר-יחיד עם דגל לוויין (geostationary,
  למשל `["AF1"]`) ולא בנק ערוצים כמו ACARS/VDL2 — `_sanitize_satellite`/
  `_satcom_window_error` מחליפים את `_sanitize_freqs`/`_window_error` עבורו.
- **מדדי איכות קליטה — לעולם לא ממציאים ערך:** `level` (dBFS) הוא **תמיד** הערך הגולמי
  מהמפענח, בלי עיבוד. `snr` מחושב **רק** כשיש רצפת רעש אמינה בקלט (VDL2 — `dumpvdl2`
  מספק `sig_level`+`noise_level` לכל פריים); **ACARS לעולם לא מקבל SNR** כי `acarsdec`
  עצמו לא מודד רצפת רעש לכל הודעה (נבדק במקור, לא מגבלת יישום שלנו). **SATCOM (inmarsat-
  sniffer) גם לעולם לא מקבל level/snr/freq** — אומת ישירות מ-`feed_aero_message` במקור
  (`feed.c`): הכלי לא חושף אותם ב---feed/--udp כלל, בניגוד ל-README (ר' §2 ב-
  docs/satcom-feasibility.md — דוגמה למה בדיקת מקור > תמצות משני). dBm **לא מומש**
  (נדחה במכוון) — ACARS/VDL2/SATCOM רצים כברירת מחדל עם AGC (רווח משתנה, לא ידוע לנו
  לכל הודעה), כך שהמרת dBFS→dBm חסרת בסיס אמין בלי מעבר לרווח קבוע + כיול חד-פעמי.
  אם מוסיפים dBm בעתיד — ודאו שהוא נשאר אופציונלי/כבוי-כברירת-מחדל ולעולם לא מוערך.
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
