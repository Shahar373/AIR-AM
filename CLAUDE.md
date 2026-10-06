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
   קול 📻          │ rtl_airband  acarsdec   dumpvdl2   inmarsat-sniffer │
   / ACARS 📡      │ (AM/NFM→MP3) (ACARS→   (VDL2→     (SATCOM ACARS→    │
   / VDL2 🛰️       │      │       JSON 5556) JSON 5557) JSON 5558) 📶    │
   / SATCOM 📶     │ Icecast2 :8000   └──── airam-web :8080 ──────┘  (Conflicts
   (Conflicts) ◄───┤      │                       │              4-כיווני)   │
                   │      │                       │                            │
                   └──────┼───────────────────────┼────────────────────────────┘
                          ▼                        ▼
                   נגן הדפדפן (סטרים)      דף הבקרה (REST/JSON)
                          ▲                        │
                          └──── /stream proxy ─────┘ (כש-HTTPS: same-origin)

   thread ברקע ב-airam-web:  adsb.py ─HTTP─► adsb.lol / adsb.fi  (מסלול פעיל + GPS)
```

**העיקרון המכריע — SDR אחד, בהחלפה:** ל-RSP1B יכול לגשת **תהליך אחד בלבד** בכל רגע.
`rtl_airband` (קול), `acarsdec` (ACARS), `dumpvdl2` (VDL2) ו-`inmarsat-sniffer`
(SATCOM) הם ארבעה תהליכים נפרדים שמתחרים על אותו מקלט. לכן יחידות ה-systemd מוגדרות
`Conflicts` (כל יחידה מצהירה Conflicts מול *כל* קודמותיה => כל הזוגות מכוסים,
דו-כיווני; `airam-satcom` מצהיר מול שלושת האחרים) — הפעלת אחת עוצרת אוטומטית את השאר.
**אי אפשר שניים מהם בו-זמנית עם SDR אחד.** מעבר מצב = ~3 שניות.

**תפיסת ההפעלה — אין "מצב ראשי", airam-web הוא המתזמר:** קול/ACARS/VDL2/SATCOM הם
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
                            #   רוסטר מאוחד, מד שדה + בדיקת אנטנה, דוח סשן, REST API, יומן,
                            #   הקלטות, תמלול, METAR, מדדי RF + טלמטריית עומס מהחומרה, מעבר מצבים, ארכיון חיפוש. ~7000 שורות.
  adsb.py                   # ניתוח ADS-B עצמאי: מסלול פעיל + שיבוש GPS + סדרת סשן +
                            #   buffer מתגלגל לשחזור-סשן (docs/session-replay-design.md,
                            #   שלב 1). thread נפרד. ניתן להרצה ידנית: `python3 adsb.py [--selftest]`.
  static/
    index.html              # ה-UI כולו (HTML+CSS+JS inline, ~6800 שורות). PWA. 5 תצוגות:
                            #   🏠 מרכז (בית/standby/scan, כולל דוח סשן+התראות) + קול + ACARS +
                            #   VDL2 + SATCOM. ACARS/VDL2/SATCOM שלושה מופעים סימטריים של אותו
                            #   פקטורי createDataView (ר' §7); מד השדה — פקטורי מקביל, שני.
                            #   שחזור-סשן (רשימה+נגן, ר' §7) הוא "תצוגה" עצמאית נוספת שנפתחת
                            #   מעל 🏠 — לא appMode/SDR, ולא נספרת בחמש התצוגות הנ"ל.
    manifest.webmanifest    # PWA manifest (התקנה כאפליקציה).
    sw.js                   # Service Worker (נדרש HTTPS).
    icon-*.png, apple-touch-icon.png
    vendor/leaflet/         # Leaflet vendored (מפת ACARS/VDL2, בלי CDN).

config/
  airband.conf             # קונפיג ברירת-מחדל ל-rtl_airband (ATIS 132.5). ⚠ נדרס ע"י app.py בכל tune.
                           #   חייב להיות זהה לפלט render_config(DEFAULT_STATE) (נבדק) — כולל
                           #   device_string עם rfnotch_ctrl+rfgain_sel (ר' §5, _device_string).
  acars.env               # ברירת-מחדל ל-acarsdec (EnvironmentFile). ⚠ נדרס ע"י app.py בכל מעבר ACARS.
  vdl2.env                # ברירת-מחדל ל-dumpvdl2 (EnvironmentFile). ⚠ נדרס ע"י app.py בכל מעבר VDL2.
                          #   ⚠ התדרים ב-Hz (dumpvdl2), בעוד state/UI ב-MHz.
  satcom.env              # ברירת-מחדל ל-inmarsat-sniffer (EnvironmentFile). ⚠ נדרס ע"י app.py
                          #   בכל מעבר SATCOM. לוויין (AF1 ברירת מחדל), gain, bias-tee.

systemd/
  sdrplay.service          # שירות SDRplay API. enabled.
  rtl_airband.service      # קול. Requires=sdrplay, בלי [Install] — *לא* enabled. root. Restart=always.
                           #   ‏`-F` (חזית בלי waterfall) ולא `-f` — ה-waterfall היה מלכלך את זרם
                           #   ה-journald ששורות AIRAM_RF מגיעות בו (ר' §12).
  airam-acars.service      # ACARS. Conflicts=rtl_airband. *לא* enabled. root.
  airam-vdl2.service       # VDL2 (dumpvdl2). Conflicts=rtl_airband+airam-acars. *לא* enabled. root.
  airam-satcom.service     # SATCOM (inmarsat-sniffer). Conflicts=שלושת האחרים. *לא* enabled. root.
                           #   אף צרכן SDR לא עולה באתחול — airam-web (המתזמר) משחזר את המצב השמור.
  airam-web.service        # שרת הווב + המתזמר. enabled. User=airam (לא-root). Restart=always.

patches/
  soapysdrplay3-airam-rf.patch  # ★ patch מקומי ל-SoapySDRPlay3 (נעוץ ל-SOAPYSDRPLAY_COMMIT): רושם
                           #   אירועי overload/GainChange של ה-API (שה-upstream בולע) כ-"AIRAM_RF ..."
                           #   ל-stderr => journald, ו-"AIRAM_RF stream=start" לפני sdrplay_api_Init
                           #   (גבול-סשן + הוכחת-חיים). הוספות בלבד: Streaming.cpp (INFO, gain מוגבל-קצב)
                           #   + Settings.cpp (streamActive=false בתחילת הבנאי — UB ב-kwargs loop).
                           #   מוחל ע"י install.sh שלב 3 (ר' §10); כשל => בנייה בלעדיו, בלי marker.

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
  test_satcom.py           # נרמול SATCOM (inmarsat-sniffer JSON, מאומת מהמקור), env, מעברי מצב, ייצוא,
                           #   שלושת מתגי ה-satcom (bias_tee/skip_c/spectrum), health/spectrum/log.
  test_boot.py             # _boot_restore: שחזור המצב באתחול (המתזמר) — כולל SATCOM.
  test_scan.py             # מצב סריקה: validate_scan_plan, _scan_loop, /api/mode, /api/scan, boot restore.
  test_roster.py           # רוסטר מטוסים מאוחד: היתוך זהות ACARS/VDL2/ADS-B, מיון, /api/aircraft.
  test_archive.py          # ארכיון חיפוש רב-יומי: _day_bounds, ?day= ב-/api/acars ו-/api/vdl2.
  test_adsb_enrich.py      # היתוך ADS-B↔ACARS: העשרת /api/acars מ-snapshot של adsb.py (בלי רשת).
  test_recordings.py       # ★ שמירת הקלטות (saved/, מכסה תחת מרוץ-מקביל, ?starred=1, ZIP) +
                           #   תמלול (5 מצבי tx.state, תור מבוסס-sidecar ששורד restart, שתי
                           #   שפות/מודלים, fallback מודל, retention עמיד ל-stat שנכשל).
  test_security.py         # _guard: Origin/CSRF, PIN (55 שורות).
  test_signal.py           # מד שדה: _signal_verdict, /api/signal (voice/acars/vdl2/satcom/off), /api/antenna/check,
                           #   תיוג הבסיס ב-LNA/מסנן (verdict_reason), בדיקה בקצה-הקדמי השמור.
  test_rf_telemetry.py     # טלמטריית RF (PR 1 ב-docs/voice-rf-quality-plan.md): פענוח שורות AIRAM_RF,
                           #   stream=start כראיה היחידה ל"אין עומס" (עוקב "עיוור" => לא ידוע), unknown_reason,
                           #   עוקב journalctl (Popen מזויף — אף פעם לא אמיתי), /api/metrics.rf בכל המצבים,
                           #   sidecar ‏.rf.json (חלון, כיסוי, שעה כפולה ב-DST, post_stats) ונסיעתו עם ההקלטה
                           #   (★/retention/סשן/ZIP), sweep מול ★ מקביל (_STAR_LOCK), סדר sidecar⇒יומן בלולאה.
  test_install_soapy.py    # install.sh שלב 3 סטטית: נעיצת SoapySDRPlay3, חתימת בנייה, סדר marker, ה-patch
                           #   כ-unified diff תקין (INFO, ack נשמר, rate-limit); git apply אמיתי רק עם
                           #   SOAPYSDRPLAY3_SRC.
  test_rf_contract.py      # ★ חוזה חוצה-רכיבים: מחרוזות הפורמט נשלפות *מה-patch*, מרונדרות (עם/בלי
                           #   [INFO]/[WARNING]/ANSI/זבל קודם) ומוזנות למפענח; marker install.sh==app.py;
                           #   ExecStart ‏-F; שדות rf.* שה-UI קורא ⊆ מה שה-backend מחזיר.
  test_rfcheck.py          # 🩺 בדיקת RF: כלל ההמלצה (פונקציה טהורה), דחיית stats של התהליך הקודם,
                           #   עומס לא-ידוע נשאר None, ריצה מלאה/ביטול/חריגה ⇒ שחזור פעם אחת + שחרור
                           #   TUNE_LOCK, סירובים, החלה דרך _voice_tune, /api/health בזמן ריצה.
  test_rflog.py            # רשם ניסוי RF: פענוח airband.conf, דגימה רק על כתיבה חדשה, start/mark/stop/
                           #   export, כיבוי אוטומטי, רישום בדיקת-אנטנה.
  test_experiment.py       # ניסוי כיול אוטומטי מקצה לקצה עם SDR מדומה (כולל שתי הפעולות הפיזיות),
                           #   ביטול/timeout ושחזור מצב, תנאי-פתיחה (SATCOM/סריקה/נעילה), וניתוח
                           #   (_experiment_summary) — כולל זיהוי קריאה של התהליך הקודם מול proc_start.
  test_sdr.py              # חיווי SDR (/api/sdr): פענוח lsusb/SoapySDRUtil, כל המצבים, צרכן שעלה באמצע
                           #   ה-probe (=שלנו, לא זר), cache, ובקשה מקבילה שלא ממתינה ל-probe תקוע.
  test_session.py          # דוח סשן: _interest_score, /api/session, /api/session/ack, adsb.session_series.
  test_replay_buffer.py    # שחזור-סשן שלב 1: buffer מתגלגל (append/compaction/gap-rows,
                           #   מטוס משובש נשמר עם lat/lon=None ולא מדולג) + GET /api/replay/buffer.
  test_sessions.py         # שחזור-סשן שלב 2: POST/GET /api/sessions, GET/DELETE
                           #   ‏/api/sessions/<id>, /track, /clips/<name>, /export.zip —
                           #   כולל שמורה (★) מועתקת מול לא-שמורה מועברת, חיתוך minutes
                           #   ל-TRACK_BUFFER_MIN, path-traversal ב-<id>/<name>.

docs/                       # מסמכי תכנון/החלטות. מתעדים *למה* — ומה נדחה ועל סמך מה.
  field-station-roadmap.md  # מפת הדרכים שהובילה למד השדה, דוח הסשן ורוסטר המטוסים.
  satcom-feasibility.md     # היתכנות ואפיון מצב SATCOM (מומש).
  antenna-calibration-experiment.md  # ניסוי: האם "בדוק אנטנה" מזהה ניתוק ב-VHF תחת AGC? מה ידוע
                            #   מהמקור (rtl_airband/SoapySDRPlay3), נוהל ליד נתב"ג, והחלטות לפי תוצאה.
  voice-rf-quality-plan.md  # ★ איכות ההאזנה בקול: ממצאים מהמקור (LNA תקוע תחת AGC, חיווי עומס עיוור,
                            #   היסט bin), החלטות ו-PR 1–5. PR 1 (v2.26.0) מומש — ממתין לאימות שטח.
  session-replay-design.md  # ★ תכנון "שחזור סשן" (מפה+אודיו על ציר זמן). שלבים 0–2 בוצעו
                            #   (buffer מתגלגל + POST /api/sessions) — ר' §11 למימוש שנותר (UI).

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
| `/etc/airam/satcom.env` | לוויין נבחר (`AF1`=Alphasat וכו'), gain (`--sdrplay-gain=N` או ריק=AGC), bias-tee (`-B`), דילוג C-channels (`--skip-c-channel`), ספקטרום אבחוני (`--spectrum`), פורט אבחון (`SATCOM_WEB_PORT`) | app.py בכל מעבר ל-SATCOM |
| `/etc/airam/airam.env` | env אופציונלי (PIN, whisper) — `EnvironmentFile=-` | install.sh / ידני |
| `/var/lib/airam/state.json` | מצב אחרון (תדר, mod, gain — `if_gain`, ו-`rf_gain` = מצב LNA 0–9 שחל **גם תחת AGC** מאז v2.26.0, squelch, `fm_notch` (bool, ברירת מחדל False; היעדרו מסמן state מלפני v2.26.0 ⇒ הגירת `rf_gain`, ר' §12), app_mode: voice/acars/vdl2/satcom/off, acars_freqs, vdl2_freqs, satcom_freqs, `satcom_bias_tee`, `satcom_skip_c`, `satcom_spectrum`, `satcom_gain`, `signal_baseline` — `{noise, freq, ts, lna, fm_notch, agc}` מאז v2.26.0; בלי `lna`/`fm_notch` = מלפני v2.26.0 ⇒ `baseline_untagged`, `last_session_view_at`, `transcribe_auto`, `transcribe_lang`, `rf_check_last` — תוצאת 🩺 האחרונה: `{ts, freq, fm_notch, lna_before, agc, rows, recommendation}`) | app.py |
| `/var/lib/airam/presets.json` | פריסטים (נערכים מה-UI) | app.py |
| `/var/lib/airam/acars.jsonl` | היסטוריית ACARS (שורדת restart, retention 5000) | _acars_listener |
| `/var/lib/airam/vdl2.jsonl` | היסטוריית VDL2 (שורדת restart, retention 5000) | _vdl2_listener |
| `/var/lib/airam/satcom.jsonl` | היסטוריית SATCOM (שורדת restart, retention 5000) | _satcom_listener |
| `/var/lib/airam/activity.jsonl` | יומן שידורים (retention 500) | _activity_watcher |
| `/var/lib/airam/track.jsonl` | buffer מתגלגל של ADS-B ל-שחזור-סשן (90 דק', append גולמי + compaction נדיר — **לא** כתיבה-אטומית-על-כל-שורה, ר' §6) | adsb.py |
| `/var/lib/airam/recordings/` | הקלטות MP3 (500 קבצים / 100MB) + sidecar תמלול `<file>.mp3.tx.json` + sidecar טלמטריית RF `<file>.mp3.rf.json` (קונפיג שרץ, עומס/IFGR בחלון השידור — `None` כשלא ידוע; `post_stats` = תמונת stats אחת *אחרי* סוף השידור, מקוננת בכוונה — לא ה-SNR של השידור; `start_precision_s`; נוסע עם ההקלטה דרך `_rec_sidecars`) | rtl_airband; sidecars ע"י app.py (`_activity_watcher`); נמחק ע"י app.py (`_sweep_recordings`, תחת `_STAR_LOCK`) |
| `/var/lib/airam/recordings/saved/` | הקלטות **שמורות** (★) — **תת-תיקייה, לא רשימה בקובץ צד**. עד 100 קבצים / 100MB, פטורות לגמרי מ-`_sweep_recordings` (‏`glob("*.mp3")` אינו רקורסיבי — הפטור מגיע ב*אפס* שורות לוגיקה, ר' §5/§12) | app.py (`/api/recordings/star`) |
| `/var/lib/airam/sessions/<id>/` | סשן שמור (שחזור-סשן שלב 2) — `meta.json` (אטומי), `track.jsonl.gz` (חתך `track.jsonl` בטווח), `clips/*.mp3` (הקלטות רלוונטיות — שמורה מ*עתיקה*, לא-שמורה מ*ועברת*). אין retention אוטומטי — מחיקה היא `DELETE /api/sessions/<id>` בלבד | app.py (`POST /api/sessions`) |
| `/run/rtl_airband_stats.txt` | מדדי RF (tmpfs, ~1Hz) | rtl_airband |
| `/usr/local/share/airam/soapysdrplay3.build-sig` | סימן-בנייה = **החוזה "יש טלמטריית RF"** (`SOAPY_RF_MARK`): קיים <=> SoapySDRPlay3 הנעוץ נבנה *עם* ה-patch המאומת. נמחק בתחילת כל בנייה מחדש | install.sh (שלב 3) |
| `/var/lib/airam/rf_log.jsonl` | רשם ניסוי RF — שורה לכל כתיבה חדשה של המדדים + הקונפיג שרץ + סימוני משתמש + תוצאות בדיקת-אנטנה. **רק כשמופעל** (כבוי כברירת מחדל, כיבוי אוטומטי אחרי שעתיים); append+`fsync` (תוצאת ניסוי שדה); מעל 5MB בהפעלה ⇒ `.prev` | app.py (`/api/rflog`) |

---

## 5. `webtune/app.py` — מפת הקוד

הקובץ מאורגן בבלוקים מסומנים `# --- ... ---`. נקודות עיקריות:

- **קבועים (ל~145):** נתיבים, gain של SDRplay (IFGR 20–59 / RFGR 0–9, **קטן=רווח גדול**;
  ⚠ `OVERLOAD_DBFS` **הוסר** ב-v2.26.0 — ר' §12),
  ספי squelch, קבועי ACARS, מילוני `ACARS_LABELS` ו-`_ACARS_DIR_BY_LABEL` (חלקם
  התווספו רק מקליטת שטח אמיתית — למשל `A0`/`1B`/`4P`/`2F`, לייבלים שנצפו
  בקליטת SATCOM ראשונה מוצלחת ולא היו מתועדים קודם; ר' §12), הקלטות, whisper.
- **`_guard` (before_request):** אכיפת אבטחה לכל בקשה משנת-מצב — בדיקת `Origin==Host`
  (CSRF/DNS-rebind) + PIN אופציונלי. **כל route שמשנה מצב חייב לעבור דרכו.**
- **בניית קונפיג קול:** `render_config` → `write_config` (כתיבה אטומית), `_squelch_line`
  (מקור-אמת יחיד לשורת ה-squelch), `_device_string(agc, rf_gain, fm_notch)` (מקור-אמת יחיד
  ל-`device_string`: `rfnotch_ctrl=<bool>` תמיד, `rfgain_sel=<rf_gain>` **רק תחת AGC** — ברווח
  ידני ה-LNA הוא `RFGR` בשורת `gain`; ר' §12 *למה*, ו-docstring עם file:line מהמקור).
  `fm_notch` (state, ברירת מחדל False) עובר בכל מסלול שכותב קונפיג קול (tune, `_enter_voice`,
  `_rollback`, `_restore_after_probe`, boot/reconcile, רגלי סריקה, probe, ניסוי). `_parse_bool`
  (‏`"false"` טקסטואלי ≠ True — משותף ל-`agc`/`fm_notch`). `_config_stale` מסמן גם קונפיג בלי
  `rfnotch_ctrl` או AGC בלי `rfgain_sel` (שדרוג ⇒ `_boot_restore` משכתב). תמיד ערוץ יחיד ממורכז (centerfreq מוסט ב-DC_OFFSET).
- **restart מאומת + רולבק:** `_restart_and_verify` בודק שה-SDR נוכח ושהשירות עלה;
  `_rollback` מחזיר לקונפיג קודם אם נכשל **ומאומת** (מחזיר bool; כישלון ⇒ `_fail_to_off`).
  כיוונון אחד בכל רגע (`TUNE_LOCK`).
- **רגיסטרי מצבים (שוויון מצבים):** `MODE_SERVICE` (מצב→שירות), `_live_mode()` (המצב
  שרץ בפועל או None), `_enter_voice` (peer סימטרי של `_enter_acars`/`_enter_vdl2`/
  `_enter_satcom`: עצירת צרכני דאטה + write_config + verify), `_fail_to_off` (כישלון
  כניסה ⇒ standby + state off+prev_mode + payload 500 — **לעולם לא fallback לקול**).
  `/api/mode` הוא dispatcher מאוחד לששת המצבים (voice/acars/vdl2/**satcom**/off/**scan**);
  acars/vdl2/satcom חולקים זנב גנרי `(key, default, sanitize, wcheck, enter)`.
  `_boot_restore` (thread ב-startup) משחזר את המצב השמור באתחול — ה-orchestration
  שמאפשר לאף צרכן לא להיות enabled ב-systemd.
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
  `_text_latlon`/`_scan_latlon` (חילוץ מיקום — `_text_latlon` דורש בדיוק התאמה
  *אחת* בטקסט; 2+ = שרשרת waypoints (מסלול טיסה), לא דיווח מיקום בודד, ר' §12),
  פרסרים לפי label: `_parse_atis` (A9),
  `_parse_oooi_80` (80), `_parse_wx_alternates` (WX), `_parse_sa_media` (SA),
  `_parse_h1`+`_parse_fpn` (H1 sub-labels + תוכנית טיסה — `_H1_SUB_RE` תופס `#`
  גם אחרי prefix כמו `"- "`/`\n` (לא רק בתחילת הטקסט ממש), `_parse_fpn` תופס גם
  `"M3FPN/"` בלי קו-נטוי פותח — שני הפורמטים נצפו בקליטת SATCOM אמיתית, ר' §12),
  `_parse_label15` (נ"צ, גם עם
  error — מבני), `_parse_sq` (squitter תחנה, בלי נ"צ), `_parse_autotune` (`:;`),
  `_parse_loadsheet` (C1, ZFW/TOW/נוסעים), `_parse_pdc` (A3, אישור טרום-המראה),
  `_parse_label16`/`_parse_nav_fuel` (16/1L, נ"צ עשרוני — לא-מתועדים ב-ARINC, זוהו
  מקליטה אמיתית; **CPDLC נבדק ונמצא ללא תעבורה בפועל** בקליטה שנבדקה — לא מומש),
  `_acars_adsb` (העשרת ADS-B לזנבות שבזיכרון — ראה §6),
  `_enter_acars` (כתיבת env + מעבר שירות), `_enter_standby` (כיבוי **ארבעת** הצרכנים, משאיר
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
- **SATCOM (מצב רביעי, ACARS דרך לוויין Inmarsat):** `_satcom_listener` (thread, מאזין
  UDP 5558 מ-`inmarsat-sniffer`), `_normalize_satcom` (מסלול יחיד — הכלי מפיק רק ACARS:
  מסנתז dict בסגנון acarsdec משדות `isu.acars.*` (סכמת JAERO JSONdump, אומתה מ-`feed.c`
  במקור) ומזרים דרך `_normalize_acars` ⇒ כל הפרסרים חלים; `arinc622` מקונן ⇒ ADS-C —
  **אבל מיקום ADS-C נאמן *רק* כש-`decode_failed=False`, לא סתם "יש `libacars`"
  (⚠ תוקן אחרי regression — ר' §12).** ראשונית תועד "מאומת בקליטת שדה אמיתית" על
  סמך הודעת A6 בודדת שהניבה `pos_src="adsc"` עם נ"צ — **זו הייתה מסקנה מוקדמת
  מדי**: לא הושוותה מול מקור-אמת חיצוני (ADS-B). קליטה נוספת חשפה הודעת A6
  אמיתית (C-GHKX) עם `decoded="לא פוענח"` (כלומר `err:true` פנימי) **שעדיין
  הניבה מיקום** — 5.69°N/2.11°E, בעוד המטוס האמיתי (מאומת מול ADS-B חיצוני)
  היה ~45.85°N/‑29.6°W, אלפי ק"מ משם. הסיבה: `_scan_latlon` רץ *ללא תלות*
  ב-`decode_failed` וסרק רקורסיבית כל שדה numeric בשם lat/lon — כולל שריד-
  מפענוח-חלקי במבנה שהמפענח עצמו סימן כנכשל. **תוקן**: מיקום מ-ADS-C (גם
  ב-SATCOM וגם ב-VDL2 מסלול B — אותה משפחת באג בשני המקומות) מותנה עכשיו
  ב-`not decode_failed`; `group="position"` גם הוא מותנה (אחרת כרטיס-שנכשל
  היה מסונן תחת "📍 מיקום" ב-UI בלי מיקום אמיתי — `group` משמש לסינון, לא
  רק לצביעה). `_libacars_decode`
  (משותף ל-SATCOM/VDL2 מסלול B) מחזיר 3-tuple: `(kind, text, decode_failed)` —
  `decode_failed=True` (נגזר מסריקה רקורסיבית אחר `"err":true` כלשהו; §12: לא
  ממציאים טקסט, אבל *כן* חושפים שהיה ניסיון) מציג `decoded="לא פוענח — libacars
  החזיר שגיאת פענוח; הטקסט הגולמי נשמר"` במקום `None` סתמי, כדי להבדיל "לא ניסינו"
  מ"ניסינו ונכשלנו" (⚠ הניסוח הקודם ("כנראה איתות שולי") הוסר — קליטת 14.08.2026
  הוכיחה שזו לא הייתה הסיבה בפועל, ר' הבולט הבא).
  **⚠ המשך: `decode_failed=False` לבד עדיין לא היה מספיק — תוקן פעם שנייה
  (משתמש הצליב מול ADS-B חיצוני, לא אנחנו מצאנו את זה).** ADS-C משתמש ב-tag
  מספרי (7 וכו') ש**פירושו הפוך לגמרי** לפי כיוון ההודעה — מאומת ישירות
  מ-`libacars/adsc.c` במקור: `la_adsc_uplink_tag_descriptor_table` (tag 7 =
  "Periodic contract request", קרקע→מטוס, *אין בו מיקום מטוס בכלל*) מול
  `la_adsc_downlink_tag_descriptor_table` (tag 7 = "Basic report", מטוס→קרקע,
  מיקום אמיתי). אם ה-uplink נותח עם טבלת downlink (או להפך) — הפענוח "מצליח"
  מבנית (**אין שום `err`שמתגלה**, `decode_failed=False`), אבל שולף בייטים
  שהם בכלל פרמטרי-בקשה כאילו הם נ"צ. נצפה בפועל: **גם** ההודעה ש"הוכיחה"
  שה-מנגנון עובד (A7-BBB, 18.34°N/2.11°E) **וגם** C-GHKX (5.69°N/2.11°E)
  היו `dir="uplink"` — כלומר ה"הוכחה" המקורית עצמה הייתה אותו באג, לא ראיה
  נגדית לו. **תוקן**: `_structural_dir` (SATCOM: `isu.src/dst.type`, מחושב
  *לפני* הקריאה ל-`_normalize_acars` ולא אחריה כמו קודם; VDL2: `direction`
  מ-AVLC src/dst.type, כבר זמין מוקדם בפונקציה) — `adsc_dir_ok = dir != "uplink"`
  חוסם מיקום ADS-C כש-uplink, **גם** בלי `err`. חסר-רמז (ACARS-over-VHF רגיל,
  שכמעט ולא מפיק ADS-C בפועל) permissive בכוונה — לא לשנות התנהגות במקום
  שלא נבדק. **המשמעות: אין לנו עדיין ולו הוכחה אחת ש-ADS-C downlink אמיתי
  עובד** — כל קליטה עד כה הייתה 100% uplink (ר' §12, "P channel בלבד"), ולכן
  כל מיקום שהוצג עד עכשיו (כולל ה"הצלחה" המתועדת) היה על תקן שגוי מבנית.
  לוקחים ברצינות שגם התיקון *השני* הזה נבנה בלי לכידת --feed -v גולמית של
  downlink אמיתי — הטסטים המתאימים מתועדים ככאלה שלא-מאומתים-נגד-שדה
  (ר' `tests/test_satcom.py`), רק נגד-הפרוטוקול (מאומת מ-`libacars` במקור).
  **⚠ CPDLC (label AA) נצפה בשפע בקליטה** (14 הודעות ב-33 דק', וקליטה נוספת
  ב-14.08.2026: 12 CPDLC + 11 ADS-C, **כולן uplink**, מעטפות ARINC-622 תקינות)
  אבל ברוב המקרים `decoded` הציג הודעת-כישלון — **וזו הייתה מסקנה שגויה**, לא
  "המפענח נכשל ביישום המקונן" (איתות חלש/מקרי) כמו שתועד קודם כאן. **הסיבה
  האמיתית: `inmarsat-sniffer` פענח את היישום המקונן בכיוון ASN.1 הלא-נכון.**
  ‏CPDLC משתמש במבני-הודעה נפרדים לגמרי לכל כיוון (`FANSATCUplinkMessage` מול
  `FANSATCDownlinkMessage`) — בדיוק אותה משפחת-באג כמו tag 7 ב-ADS-C (הבולט
  הקודם), רק שכאן היא גם *מונעת* פענוח (מחזירה `err`) ולא רק מייצרת תוכן שגוי.
  **תוקן**: `_decode_libacars_app` (ליד `_libacars_decode`) מריץ re-decode
  מקומי עם `decode_acars_apps` — כלי ה-CLI ה**רשמי** של `libacars` (מותקן
  ע"י `install.sh`, ר' §10), לא אקסטרקטור תוצרת-בית — בכיוון האמיתי מ-`structural_dir`
  (‏uplink→`u`/GND2AIR, downlink→`d`/AIR2GND; כיוון לא-ידוע ⇒ לא מריצים, לא
  מנחשים). **שתי הרצות, לא אחת — אומת מהמקור של libacars שזה הכרחי:**
  ‏`LA_JSON=1` **לא** מכיל את הטקסט האנושי (ADS-C: רק מפתחות מספריים כמו
  `contract_num`; CPDLC: הטקסט קיים אבל תחת `choice_label`, ש-`_libacars_decode`
  לא קוצר כי הוא מחפש `text`/`msg`/`message` בלבד) — לכן הרצת **TEXT** (בלי
  `LA_JSON`) היא המקור **היחיד** לטקסט הקריא (`raw["_libacars_text"]`,
  עדיפות ראשונה ב-`_normalize_acars`), במקביל להרצת **JSON** שמוזרמת ל-
  `raw["libacars"]` (עדיפות: re-decode מתוקן > הפענוח המקורי > כלום — כשל לא
  מאבד הודעה) כך שכל מה שתלוי בה (`kind`, `decode_failed`, `adsc_dir_ok`,
  `_scan_latlon`) ממשיך לעבוד ללא שינוי ארכיטקטוני. `error` (CRC המעטפת
  הפנימית) **לא** מושפע מ-re-decode — ערוץ נפרד, אף פעם לא "מתקן" CRC כושל
  אמיתי. `src/dst.type` "Aircraft/Ground Earth Station" קובע
  `dir` מבני. **בלי `level`/`snr`/`freq`**
  — הכלי לא חושף אותם (§12), ו-`isu.refno`≠MSN ⇒ `msgno` לא ממופה), `write_satcom_env`
  (לוויין `--satellite=`, `SATCOM_GAIN`=`--sdrplay-gain` (נייטיבי, לא `--soapy-gain`) או
  ריק=AGC — נבחר מה-UI דרך `_sanitize_satcom_gain` (`None`=AGC / int 20–59, ר' §12), `SATCOM_BIAS_TEE`=`-B`/ריק, `SATCOM_SKIP_C`=`--skip-c-channel`/ריק — ר' §12),
  `_enter_satcom` (עוצר שלושת האחרים, מנקה
  תקרת-הפעלות קודמת עם `reset-failed` לפני ה-restart — ר' §12, מרים
  inmarsat-sniffer, verify), `_sanitize_satellite`/`_satcom_window_error` (לוויין יחיד
  מתוך `SATCOM_BANKS`, לא בנק תדרים), `SATCOM_BANKS` (לוויינים: AF1/4F3/3F5/F1). התמדה:
  `_append_satcom_log`/`_trim_satcom_log`/`_load_satcom_history` (clones של צמד ה-VDL2).
  **אבחון (שלוש שכבות, כל אחת עונה על שאלה שהקודמת לא יכולה):** (1)
  `_fetch_satcom_web_state`/`api_satcom_health` — proxy מקומי (כמו `/stream`)
  ל-dashboard האבחוני המובנה של inmarsat-sniffer (`--web=SATCOM_WEB_PORT`,
  ברירת מחדל 8888): נעילת דמודולטור/ebno/mse לכל ערוץ, גם באפס הודעות — ההבדל
  בין "אין אנטנה" ל"תקין, שקט כרגע" (`_is_active` בלבד לא מבחין ביניהם).
  (2) `api_satcom_spectrum` (`GET /api/satcom/spectrum`, דרך `_fetch_satcom_web`
  הגנרי, דורש `--spectrum`) — **ההבחנה שחסרה בשכבה (1): `lock=false` הוא בדיוק
  אותו חיווי עבור אנטנה מנותקת, LNA בלי מתח, וכיוון שגוי ב-5°.** רצפת הרעש
  ב-`mags_db` מפרידה ביניהם (LNA מוזן מקפיץ אותה בעשרות dB). מוגש גולמי, בלי
  סף (§12) — ה-UI מנסח הוראת-פעולה ("נתק את ה-LNA וראה אם הרצפה זזה"), לא
  פסק-דין. (3) `api_satcom_log` (`GET /api/satcom/log`) — זנב journalctl **על
  דרישה בלבד** (fork; ה-health כבר בקצב 1s): `sdrplay: bias tee enabled` מול
  `bias tee not supported on this model` הוא התשובה הוודאית היחידה לשאלה "האם
  ה-LNA בכלל מקבל מתח", ובשטח מהטלפון אין SSH.
- **רוסטר מטוסים מאוחד:** `_aircraft_identity` (מפתח זהות מהודעה מנורמלת — רישום
  מנורמל קודם, אחרת icao, אחרת מספר טיסה), `_build_roster` (מהתך `_acars_msgs`+
  `_vdl2_msgs`+`_satcom_msgs` לפי הזהות, מעשיר ב-`adsb.aircraft_snapshot`, ממוין lastT
  יורד, גזור ל-`ROSTER_MAX`) — **חי בכל מצב** (לא תלוי SDR הפעיל, ר' §12), `GET /api/aircraft`.
- **ציון מעניינוּת + דוח סשן** (ר' docs/field-station-roadmap.md): `_interest_score`
  (קריטריונים בינאריים על כרטיס מנורמל — קטגוריה לא-גנרית/decoded/pos_src=adsc/
  label עשיר — לא ציון מומצא; מוזרם לשדה `notable` בכל תוצאה של `_normalize_acars`,
  כלומר גם ל-ACARS/VDL2/SATCOM כאחד). `GET /api/session`/`POST /api/session/ack`
  ("מה קרה בזמן שלא הסתכלת" — קורא jsonl מהדיסק, `?since=` דורס את `state
  ["last_session_view_at"]`; idempotent, ה-ack הוא הפעולה היחידה שמקדם אותו).
- **מד שדה מאוחד + בדיקת אנטנה** (ר' docs/field-station-roadmap.md): `_read_voice_metrics`
  (חולץ מ-`api_metrics`, משותף גם ל-`GET /api/signal` במצב voice), `_signal_verdict`
  (פסק דין *רק* מול `state["signal_baseline"]` — `DISCONNECT_DROP_DB`=10dB אינו סף
  "איכות" מומצא, אבל ⚠ גם **אינו "תצפית פיזיקלית"** כפי שתועד כאן בעבר: הנחה שטרם
  אומתה, ר' §12 ו-`docs/antenna-calibration-experiment.md`), `_sample_probe_stats`+
  `_restore_after_probe` (הלב של `POST /api/antenna/check`: מעבר זמני לקול,
  מדידה, שחזור המצב הקודם — best-effort גם בכישלון, לא נוגע ב-`state["app_mode"]`).
  **רשם ניסוי RF** (`docs/antenna-calibration-experiment.md`): `_rflog_start`/`_rflog_stop`/
  `_rflog_worker` (thread רק כשמופעל, `stop_evt.wait` ולא sleep), `_rflog_sample` (שורה רק
  כשחתימת ה-mtime של stats/conf השתנתה), `_parse_airband_conf` (הקונפיג *שבאמת רץ* — לא
  state, כי ה-probe לא כותב state), `_rflog_event` (סימונים + תוצאת כל `/api/antenna/check`,
  רק כשפעיל).
  **ניסוי כיול אוטומטי** (אותו מסמך): `_experiment_plan` (22 צעדים — מחזור מחובר, בקשת ניתוק,
  מחזור מנותק, בקשת חיבור, מחזור אימות; **שתי פעולות פיזיות בלבד**), `_experiment_start`
  (מסרב ל-SATCOM חי/סריקה/`TUNE_LOCK` תפוס; מפעיל את הרשם אם כבוי), `_experiment_run`
  (thread שמחזיק את `TUNE_LOCK` **לכל אורך הניסוי** ומשחרר ב-`finally` אחרי
  `_restore_after_probe` — תמיד, גם בביטול/timeout/שגיאה; בדיקות האנטנה דרך `_probe_params`+
  `_sample_probe_stats` — המסלול של המוצר, **בלי לדרוס את `signal_baseline`**),
  `_rtl_airband_start_wall` (`systemctl show ExecMainStartTimestampMonotonic` ⇒ זמן-קיר;
  מבחין בין כתיבות התהליך הנוכחי ל-flush של הקודם — ⚠ השוואת `stats_mtime` ל-`conf_mtime`
  **לא** יכולה, כי ה-flush נכתב אחרי הקונפיג), `_experiment_summary` (פונקציה טהורה — חציונים,
  ירידה, "מזהה?" = הסף של המוצר עצמו; `None` כשחסר), `_read_rflog_run`.
- **חיווי SDR — מזוהה? פנוי?** (`GET /api/sdr`, ליד `api_health`): `_sdr_usb` (lsusb 1df7 —
  `None` כשאין lsusb, **בכוונה** שונה מ-`_sdr_present` שמניח True כהחלטת רולבק),
  `_sdr_probe_api` (`SoapySDRUtil --find=driver=sdrplay`, אותו probe כמו `airam-wait-sdrplay`;
  פלט `driver = sdrplay` ולא returncode; `sdrplay_api_Open` ב-stderr ⇒ `api_error`, לא "תפוס"),
  `_sdr_suspects` (שמות תהליכים מ-`/proc/<pid>/comm` — רמז בלבד), `_sdr_holder`/`_sdr_status`.
  **היסוד:** `sdrplay_api_GetDevices` לא מחזיר מכשיר שלקוח אחר כבר בחר (אומת במקור
  SoapySDRPlay3 — `SoapySDRPlay_getClaimedSerials`), לכן "ב-USB + find ריק" = תוכנה אחרת
  מחזיקה בו. ה-probe רץ **רק** כשאף צרכן שלנו לא במצב מחזיק (`active`/`activating`/…) ו-`TUNE_LOCK`
  פנוי, עם cache `SDR_PROBE_TTL_SEC`=10 ונעילה לא-חוסמת (בקשה מקבילה מקבלת את האחרון או
  `checking`); אחרי ה-probe בודקים שוב את הצרכנים — צרכן שעלה באמצע הוא "שלנו", לא זר.
- **טלמטריית RF מהחומרה** (PR 1 ב-`docs/voice-rf-quality-plan.md`): `SOAPY_RF_MARK`/
  `_rf_telemetry_available` (סימן-הבנייה של install.sh — ר' §4), `_rf_follower_loop` (thread
  מ-`__main__`: `journalctl -f -u rtl_airband -o cat -n 0` **אחד** ארוך-חיים — `RF_JOURNAL_CMD`;
  רדום בלי marker, backoff מעריכי, **לעולם לא מת**), `_rf_follow_once`, `_rf_handle_line`
  (regex **לא מעוגן** — `[INFO] ` של SoapySDR), `_RF_START_RE`/`_rf_stream_start` (‏"AIRAM_RF
  stream=start" — גבול הסשן **והראיה היחידה** שמעבירה `overload` מ-None ל-False; שורת האתחול של
  rtl_airband **אינה** גבול — syslog, בלי הבטחת סדר מול ה-stderr, ר' §12), `_rf_session_reset`
  (restart שאנחנו יוזמים — `_restart_and_verify`/`_rollback` — ⇒ None, **לא** False), `_rf` (מצב
  תחת `_rf_lock`) + `_rf_events` (deque ‏`RF_EVENTS_MAX`; ‏`reset`/`start`/`follow`/`ovl`/`gain`),
  `_rf_metrics` (‏`rf` ב-`/api/metrics`: `overload`/`ifgr`/`lna_grdb` = **None** כשאין marker / הקול
  לא רץ / העוקב לא קורא / אין ראיה מהדרייבר — לעולם לא False מומצא) + `_rf_unknown_reason` (*למה*:
  ‏`no_telemetry`/`voice_not_live`/`follower_down`/`joined_mid_session`/`no_driver_evidence`),
  `_rf_window_summary` (טלמטריה בחלון שידור; None כשאין כיסוי רציף מלפני start או שהאירועים
  נדחקו; עוגן `reset` ⇒ מצב-התחלה None, רק `start` ⇒ False; קצה `overload=0` בחלון ⇒ True).
  **sidecar להקלטה:** `_build_rf_sidecar`/`_write_rf_sidecar` (idempotent, `_atomic_write`) מ-
  `_write_rf_sidecars` ב-`_activity_watcher` *לפני* שורת היומן, תחת `_STAR_LOCK`; `_rec_start_ts`
  (חלון: שם-הקובץ → mtime, מאומת מ-`output.cpp`; בשעה החוזרת של סוף שעון הקיץ מכריע לפי ה-mtime
  ו-`REC_MAX_SPAN_SEC`, אחרת None), `_read_rf`, `_rec_sidecars` (= `_tx_sidecars` + `.rf.json` —
  מקור-אמת יחיד ל-★/retention/סשן; ה-ZIP-ים כותבים אותו מפורשות). `_sweep_recordings` כולו תחת
  `_STAR_LOCK` (מעבר-היתומים מחק קובצי-צד *באמצע* ★ — `_move_recording` מעביר אותם לפני ה-mp3). **בסיס מתויג:** `_baseline_tag`/
  `_verdict_reason` (`no_reading`/`no_baseline`/`baseline_untagged`/`baseline_config_mismatch`;
  ‏`manual_gain` ב-`api_signal`), `_probe_frontend` (בדיקת האנטנה והניסוי מודדים ב-LNA/מסנן
  **השמורים של הקול**, לא בקבוע), `_conf_device_kwargs` (פענוח `device_string` — `_parse_airband_conf`
  מחזיר גם `lna`/`fm_notch`; קונפיג ישן תחת AGC ⇒ lna=0, ברירת המחדל של ה-API).
- **🩺 בדיקת RF** (PR 2 ב-`docs/voice-rf-quality-plan.md`, גרסה פשוטה): `RFCHECK_FREQ`=132.5
  (ATIS — נשא רציף; ⚠ הנחה מוצהרת: סביבת ה-RF דומה לזו של תדר המגדל), `RFCHECK_STATES`=(0,2,4,6,8),
  `api_rfcheck` (GET מצב/תוצאה; POST start/abort/apply, דרך `_guard`), `_rfcheck_run` (thread שמחזיק
  את `TUNE_LOCK` לכל הבדיקה; `_restore_after_probe` **בדיוק פעם אחת** ב-`finally`, גם בביטול/חריגה;
  תוצאה נשמרת ל-`state["rf_check_last"]` רק בסיום תקין), `_rfcheck_measure` (מצב אחד דרך `_enter_voice`+
  `_probe_params` — מקבל רק כתיבות stats מאחרי `_rtl_airband_start_wall`; עומס מ-`_rf_window_summary`,
  None = לא ידוע), `_rfcheck_recommend` (טהורה: עומס מוכח פוסל; שוויון = הפרש ≤ פיזור המדידה (`_spread`,
  IQR) ⇒ יותר הנחתה; עומס לא-ידוע ⇒ `confidence="partial"`), `_rfcheck_apply` (דרך `_voice_tune` בקול
  חי, אחרת רק state; 409 כשההמלצה כבר בתוקף). `api_health` מחזיר `rf_check` ו-`ok=True` בזמן ריצה.
  ⚠ גרסה גדולה (בודק root ייעודי) נכתבה ונדחתה — `docs/rf-check-design.md`, ‏78eb0a8.
- **REST API** (ראה §8). **יומן/הקלטות:** `_activity_watcher` (thread סורק MP3 חדשים),
  `_sweep_recordings` (retention — **לא רואה `saved/` בכלל**, ר' למטה).
- **הקלטות שמורות (★) + תמלול — שני פיצ'רים מחוברים:** הפטור מ-retention הוא
  **מיקום הקובץ**, לא רשומה במאגר-מצב: `_saved_dir()` (`REC_DIR/saved/`),
  `_rec_path`/`_is_saved` (מחפשים בשתי התיקיות), `_rec_event` (בונה שורת-יומן
  **מהקובץ עצמו** — מקור-אמת יחיד, משותף בין היומן החי ל-`?starred=1`),
  `_saved_usage` (אכיפת המכסה `REC_STAR_MAX_FILES`/`REC_STAR_MAX_BYTES`),
  `_move_recording` (`os.replace` אטומי + קובצי-הצד), `api_star`
  (`POST /api/recordings/star`, תחת `_STAR_LOCK` — **מסרב** ב-409 כשהמכסה מלאה
  במקום למחוק שמורה ותיקה), `api_starred_zip` (`GET /api/recordings/starred.zip`,
  `ZIP_STORED` — הכרטיס SD יכול למות, זו הדרך להוציא את השמורות), `_rec_name_arg`
  (אימות שם מול `_REC_NAME_RE` = גם הגנת path traversal), `_decorate_event`
  (מוסיף לאירוע יומן `exists`/`starred`/`tx`). **תמלול:** `_tx_path` (sidecar
  `<file>.mp3.tx.json`), `_read_tx`/`_write_tx` (חמישה מצבי `state`, ר' §12),
  `_transcript_path` (ה-`.txt` הישן — קריאה בלבד, תאימות), `WHISPER_MODELS`/
  `_whisper_model(lang)` (שני מודלים — `small.en` לאנגלית, `small` הרב-לשוני
  לעברית; `None` כשאין מודל מתאים לשפה, לא ניחוש), `_whisper_ready`/`_tx_status`
  (זמינות **חיה**, כולל `langs` פר-שפה), **תור מבוסס-sidecar** (`state="pending"`,
  לא רשימה בזיכרון — שורד restart; `_tx_queue_len`/`_tx_busy_file`/`_TX_FAILS`),
  `_session_clip_paths` (קליפי כל הסשנים השמורים — **בכוונה נפרד
  מ-`_iter_recordings`**: זה משמש גם את `api_sessions` לבחירת קליפים לסשן
  *חדש*, ואיחוד היה גורם לשמירת סשן לגנוב קליפים מסשנים קיימים; קיים כי
  קליפ ש*הועבר* לסשן יצא מתור התמלול לצמיתות),
  `_tx_next_target` (סדר עדיפויות: `pending` → שמורות (★) **וקליפי סשן**, תמיד → הכול אם
  `state["transcribe_auto"]`; מדלג על קובץ אחרי `TX_MAX_FAILS` כשלונות-כתיבה
  רצופים — מגן מלולאת whisper אינסופית כשהדיסק מלא), `_transcribe_file`
  (מחזיר `(state, text, err)`, `nice -n 19` על שני התהליכים — **לא סינון-תוכן**,
  ר' §12), `_transcribe_worker` (thread יחיד, **לא מת** כשwhisper חסר — ר' §12).
- **שחזור-סשן (שלב 2, `docs/session-replay-design.md`) — שימוש חוזר מכוון בדפוסי
  ★:** `_new_session_id`/`_session_dir` (מזהה=תאריך-שעה, `SESSION_ID_RE` הוא גם
  הגנת path traversal, ממש כמו `_REC_NAME_RE`), `api_sessions` (dispatcher
  ל-`GET`/`POST /api/sessions` — POST חותך `minutes` ל-`adsb.TRACK_BUFFER_MIN`,
  קורא `adsb.read_track_slice`, ובונה את `sessions/<id>/`: קליפ **שמור מ*עתיק***
  (`shutil.copy2` — נשאר גם מוגן ב-`saved/`), קליפ **רגיל מ*ועבר*** (`_move_recording`
  הקיים — משתחרר מ-`_sweep_recordings` בדיוק כמו ★, ר' §4.4 בתכנון), `track.jsonl.gz`
  נכתב עם `gzip.open`+`os.replace`, `meta.json` עם `_atomic_write`; `app_mode`/`freq`
  הם תמונת-מצב *נוכחית* (מ-`load_state()`), לא היסטוריית-מעברים — אין ל-AIR-AM
  יומן כזה, סטייה מתועדת מהתכנון המקורי), `api_session_detail` (dispatcher
  ל-`GET`/`DELETE /api/sessions/<id>` — ה-GET מעשיר כל קליף ב-`tx` **שנקרא
  חי מה-sidecar**, לא מוקפא ל-`meta.json`: קליפ יכול להתמלל *אחרי* שהסשן
  נשמר, וערך קפוא היה נשאר "אין תמלול" לנצח), `api_session_track`
  (מפענח `track.jsonl.gz` ל-JSON), `api_session_clip` (route ייעודי, לא הרחבת
  ‏`/recordings/<name>`), `api_session_export` (`GET .../export.zip`, אותו דפוס
  בדיוק כמו `api_starred_zip`). **בלי retention אוטומטי לסשנים** — מחיקה היא
  ‏`DELETE` מפורש בלבד, עקבי עם "לא מוחקים מה שהמשתמש הגן עליו".
- **`__main__`:** מרים את thread השחזור `_boot_restore` (מחזיר את המצב השמור, כולל
  שכתוב קונפיג ישן בשדרוג — `_config_stale`) + `_mode_reconcile_loop` (thread
  נפרד, רץ לאורך כל הסשן — לא רק פעם אחת באתחול כמו `_boot_restore`: כל
  `MODE_RECONCILE_INTERVAL_SEC` בודק אם המצב השמור voice/acars/vdl2 *אמור*
  להריץ צרכן אבל `_live_mode()` מחזיר `None`, ונכנס מחדש — מכסה קריסת
  `sdrplay.service` שההפעלה-מחדש הפנימית שלה, כקריסה עצמית ולא כ-restart job
  מפורש, לא בהכרח מופצת דרך `PartOf=` לצרכן שהיה פעיל; `off`/`scan`/`satcom`
  לא בטיפולו — scan יש לו thread ייעודי, satcom לא משוחזר אוטומטית בכוונה) +
  threads (**rf follower** — לפני activity, כדי שה-sidecar הראשון יהיה מכוסה —, activity, acars,
  **vdl2**, **satcom**, transcribe, adsb),
  `app.run(threaded=True)` — threaded **חובה** כי `/stream` הוא חיבור ארוך-טווח.

---

## 6. `webtune/adsb.py` — מסלול פעיל + שיבוש GPS

מודול עצמאי (אפשר להריץ ולבדוק לבד). thread מושך כל `POLL_SEC`=15 שניות
מטוסים סביב נתב"ג ממקור ADS-B קהילתי (adsb.lol, גיבוי adsb.fi — שניהם מרשים
‏~1 בקשה/שנייה, אומת מה-README הרשמי של כל אחד; ר' `docs/session-replay-design.md`
§6) ומסיק:

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
- **סדרת מסלול/GPS לדוח הסשן:** `session_series(since=None)` — בניגוד ל-`gps_hist`
  (15 דק' להחלקת היחס הרגעי בלבד), `_S["session_series"]` שומר דגימה אחת לכל
  poll (`POLL_SEC`=15) עד `SESSION_SERIES_MAX`=1440 (6 שעות — עודכן ביחד עם
  ‏`POLL_SEC` כדי שהחלון לא יתכווץ בשקט). **בזיכרון בלבד, לא נכתב לדיסק** —
  עקבי עם עקרון הבידוד (§12): נועד להישכח בין הפעלות, "מה קרה בסשן הנוכחי"
  ולא ארכיון קבוע. משמש את `GET /api/session` ב-`app.py`.
- **buffer מתגלגל לשחזור-סשן** (שלב 1, `docs/session-replay-design.md`): כל poll
  מוסיף שורה ל-`track.jsonl` (`_append_track`) — תמונת-מצב פוזיציונית של
  ‏`_S["aircraft"]` בהצלחה (`_build_track_row`; `lat`/`lon`=`None` נשמר **במפורש**
  למטוס משובש, לא מדולג — אותו עיקרון §7.1 כמו ה-snapshot), או שורת `gap` בכשל-fetch
  (מבדיל "אין מטוסים" מ"אין קליטה"). ⚠ **לא** כתיבה-אטומית-על-כל-שורה כמו `state.json`:
  ‏`_append_track` הוא `open(path,"a")` גולמי (זול, בלי `fsync` — buffer אפמרי,
  אובדן השורה האחרונה בקריסה לא-נורא), ורק `_compact_track` (סינון-לפי-גיל,
  כל `TRACK_COMPACT_EVERY`=240 appends ≈ שעה) כותב מחדש אטומית עם `fsync` —
  שם כשל-חצי-כתיבה היה מאבד את *כל* הבאפר, לא שורה אחת. `read_track_buffer()`
  (קריאה טרייה מהדיסק, לעולם לא זורק) מזין את `GET /api/replay/buffer` ב-`app.py`.
  ‏`read_track_slice(t_start, t_end)` (שלב 2) מגיש חתך-זמן מדויק (ac+gap כמו
  שהם) ל-`POST /api/sessions` ב-`app.py` — `adsb.py` רק קורא וממיין, gzip/כתיבת
  ‏`meta.json`/העברת קליפים הם אחריות `app.py` (הוא היחיד שמכיר `REC_DIR`/`saved/`).

החלפת מיקום השדה: ערוך `ARP_LAT/ARP_LON/RUNWAYS` בראש הקובץ.

---

## 7. ה-UI (`static/index.html`)

HTML יחיד עם CSS+JS inline. PWA (manifest + sw.js + MediaSession לשמע ברקע).
מתג תצוגות 🏠/📻/📡/🛰️/📶 בראש — **🏠 מרכז (מסך הבית)** הוא ברירת המחדל והנחיתה של
מצב off/scan: כרטיס סטטוס, **חמישה** כרטיסי הפעלה שווי-מעמד (📻/📡/🛰️/📶/🔁 — `renderHome`/
`showHomeError`), פאנלי המסלולים/METAR (מערכתיים — עברו לכאן מתצוגת הקול), ופאנל
**רוסטר מטוסים מאוחד** (`renderRoster`/`pollRoster`, כל 20ש' — מציג היתוך ACARS+VDL2+
ADS-B מ-`GET /api/aircraft`, כולל כשה-SDR ב-standby; כולל מקור `satcom` 📶). כרטיס
הסריקה (`#homeCardScan`) כולל עורך לוח (`renderScanEditor` — הוספה/הסרה/עריכת רגלים,
כולל שני שדות `<input type=time>` אופציונליים פר-רגל לחלון שעות (`active_from`/
`active_to`, ריק=תמיד); `scanLegs` בזיכרון בלבד עד לחיצת "התחל") וסטטוס חי (רגל
נוכחית + ספירה-לאחור מקומית כל שנייה מול `scanStatus.next_switch_at` שמגיע
מ-`GET /api/scan`; "ממתין לחלון הזמן הבא" כשאף רגל לא בחלון שלה כרגע). **אין תצוגה ייעודית
לסריקה** — `showView` מחזיק חמש תצוגות (home/voice/acars/vdl2/**satcom**) ו-`applyMode`
שישה מצבים; `scan` תמיד ממופה ל-`"home"` (הוא "אפליקציית-על", לא תוכן-תצוגה). הדף מושך מצב מ-`/api/*`
ב-polling; ה-pollers של airspace/power/METAR/roster רצים בכל תצוגה, metrics/activity רק
בקול. `pollSatcomHealth` (1ש' ב-satcom בלבד, `GET /api/satcom/health`, in-flight guard
בדפוס `metricsInFlight`, קריאה מיידית ב-`showView`+אחרי כניסה מוצלחת — לא ממתינים
למחזור polling ראשון) מזין **פאנל כיוון אנטנה ייעודי** (`#satcomAimPanel`, גלוי תמיד
בתצוגת SATCOM — לא מוסתר ב-standby כמו `#satcomActiveCtrl`): Eb/No מיטבי (המקסימום
בין הערוצים — האות שמכווננים לפיו תוך סיבוב האנטנה) + גרף היסטוריה (`drawMiniSpark`
עם פרמטר חמישי אופציונלי `fixedMax` — סקאלה קבועה 0–15dB כדי שרעש זעיר לא ייראה
כאות חזק; קוראי rate-spark הקיימים של ACARS/VDL2 לא מעבירים אותו ולא מושפעים),
ופירוט פר-ערוץ, **וכן ספקטרום הערוץ הנבחר** (`pollSatcomSpectrum`, 2ש' — איטי
יותר מה-health בכוונה: מטען כבד פי כמה, ומדד "יש RF בכלל" משתנה לאט) עם רצפת
רעש (חציון — עמיד לגבנון האות עצמו) ושיא ב-dB, `drawSpectrum` בסקאלת Y
נגזרת-מהנתונים (סקאלה קבועה הייתה מסתירה בדיוק את מה שמחפשים — תזוזת הרצפה),
וכפתור **📜 לוג המפענח** (`GET /api/satcom/log`, על דרישה בלבד). צביעה **לפי `lock` בלבד** (לא סף Eb/No מומצא — ר' §12); `ebno=0 &&
!lock && mse≈1.0` (חתימת "אין דמודולטור" מהמקור) מוצג כ-"—" ולא כ-"0.0 dB" מטעה.
**כיוון בשמיעה (🔊, `aimAudio` — closure ליד `pollSatcomHealth`):** כפתור-מתג
בפאנל שממפה את אותם `best`/`locked` ש-`pollSatcomHealth` כבר מחשב לצליל Web Audio
(גובה-טון+קצב ∝ Eb/No, טון רציף מובחן בנעילה) — "גלאי-מתכות" לכיוון עם שתי ידיים
על האנטנה. §12: סקאלת-תצוגה (`AIM_SCALE_DB`), לא סף; טון רציף רק ל-`lock`; חתימת
"אין דמוד" ⇒ טיק-חיים שקט ("דלוק, אין עדיין" ≠ "כבוי"); `feed` ישן מ-`AIM_AUDIO_STALE_MS`
(5ש') ⇒ גם טיק-חיים — לא מצפצפים על ערך עבש. `navigator.wakeLock` שומר מסך דולק
בכיוון פעיל (מחודש ב-`visibilitychange`) — ⚠ **secure-context בלבד (HTTPS)**, ולכן
הרמז "המסך יישאר דולק" מוצג רק כשהנעילה נתפסה בפועל (ב-HTTP: "השאר את המסך דולק
בעצמך"); רקע (מסך כבוי) best-effort ומתועד ככזה, ו**מוגבל בקוד** (`AIM_AUDIO_BG_MAX_MS`
10 דק' / `AIM_AUDIO_BG_DEAD_MAX` 15 דגימות "אין מפענח" ⇒ כיבוי אוטומטי; Chrome פוטר
דפים שמיעים מוויסות-timers, אז אין לסמוך על הדפדפן). שלושה צלילים: טון רציף=`lock`,
טיק בודד=מפענח חי בלי אות (`idle()`), **טיק כפול=אין מפענח** (`dead()`, מ-`renderAimIdle`).
iOS: מתג ההשתקה משתיק Web Audio — רמז בלבד; ה-workaround של `<audio loop>` שקט נדחה
במכוון (חושף `nexttrack`⇒`stepPreset` במסך הנעול ⇒ מכוונן החוצה מ-SATCOM).
`aimAudio.feed` נקרא בכל ענף-הצלחה של `pollSatcomHealth`, `renderAimIdle` (⇒`dead()`)
בכשל/לא-זמין ובלולאת ה-1ש' כש-SATCOM לא פעיל (הפאנל והאוזן מסכימים);
`aimAudio.stop()` ב-`showView` (עוזבים SATCOM). ⚠ **חריג יחיד** לשומר `document.hidden`
בלולאת ה-health: כשהאודיו פעיל (`aimAudio.active()`) ממשיכים לתשאל גם מוסתר —
אחרת הצליל קופא (עדיין מכיל את `document.hidden` ⇒ עובר את בדיקת ה-poller-guard).
`pollPower` (בכל תצוגה) מזין גם **הגנת מתח**: `confirmSatcomAntenna()` מוסיף אזהרה
כשזוהתה צניחת מתח/throttling, ושורת הצעה לא-חוסמת בתצוגת SATCOM מציעה בנקישה אחת
לסמן "הזנת LNA חיצונית" — אזהרה/הצעה בלבד, לעולם לא חסימה (ר' §12).
ו-`pollGlobalState` (10ש', `/api/health` + `/api/scan` כשסורקים) מיישר את חיווי
המצב למציאות בלי לחטוף את הטאב. כפתורי עצירה ⇒ `applyMode("off")` (גם עצירת סריקה);
כפתור ⏻ מחזיר את `prev_mode` (יכול להיות `"scan"`). עיצוב responsive (multi-column
בטאבלט/דסקטופ; `.mode-cards` הוא `auto-fit` כדי לזרום נכון גם עם 5 כרטיסים).
**חיווי SDR** (`#sdrChip` ברצועת הסטטוס — גלובלי, כמו `#connChip`; `#homeSdrTxt` במסך הבית):
`pollSdr` (15ש' + `visibilitychange` + סוף כל `applyMode`), `sdrView` (מצב⇒צבע/טקסט/הסבר),
נגיעה ⇒ `showToast` עם ההסבר המלא. טקסט דינמי (שמות תהליכים מ-`/proc`, תיאור USB, label)
**רק** דרך `textContent` ועטוף ב-FSI/PDI (`bidi()`) — אחרת לטינית בתוך עברית נשברת בגלישת שורה.
**רשם ניסוי כיול** (`#rflogBox`, בתוך `#rfPanel` בתצוגת הקול): `pollRflog` (3ש', קול בלבד +
‏`showView`), 🧪 הפעלה/עצירה, שלושה כפתורי סימון (44px — כפפות/שטח) עם אישור גלוי ושעה
("✓ סומן: מנותק · 10:31:05" — בשטח אין דרך אחרת לדעת שהלחיצה נקלטה), וקישור הורדה.
ההקלטה בצד השרת — ממשיכה כשהמסך כבוי.
**ניסוי כיול אוטומטי** (`#expCard` במסך הבית + `#expBar` — סרגל **גלובלי** קבוע בתחתית, מחוץ
ל-`<main>`, כי הבקשה לנתק/לחבר חייבת להופיע בכל תצוגה): `pollExperiment` (2ש' בזמן ריצה, 15ש'
בבית כשלא רץ, ומיד ב-`visibilitychange` — חוזרים לטאב ואולי ממתינים לך), `renderExperiment`
(התקדמות, ETA לבקשה הבאה, בקשה עם כפתור 52px + `expAlert`: שלושה צפצופים Web Audio + רטט;
ה-AudioContext נוצר רק מלחיצה), `renderExpResult` (טבלה ב-`innerHTML` — ⚠ דרך `expEsc`, כי
התוויות מהשרת מכילות מירכאות; נבדקה ב-360px).
**אין build step** — עורכים את הקובץ ישירות. Leaflet vendored תחת `static/vendor/`
(בלי CDN, עובד גם בלי אינטרנט).

**טוקני עיצוב ב-`:root`:** גיאומטריה (`--r-card`/`--r-ctrl`/`--r-sm`), מרווח (`--sp-1..5`,
בסיס 4px), הצללה (`--sh-card`/`--sh-1`/`--sh-blue`), משטחים (`--panel`/`--panel-2`/`--hover`)
וקווים (`--border`/`--border-strong`). **השתמש בטוקנים בקוד חדש** במקום ערכים קשיחים —
כך הקצב והגיאומטריה נשמרים עקביים, ושינוי במקום אחד מתפשט לכל הרכיבים.

**תצוגת ACARS / VDL2 / SATCOM — סימטריה מלאה:** שלושה מופעים עצמאיים של אותו **פקטורי
`createDataView(opts)`** — `var acars = createDataView({prefix:"acars", mode:"acars",
label:"ACARS", onMessage, onReset})`, `var vdl2 = createDataView({prefix:"vdl2",
mode:"vdl2", label:"VDL2"})`, ו-`var satcom = createDataView({prefix:"satcom",
mode:"satcom", label:"SATCOM"})`. כל מופע הוא closure סגור לגמרי (state/מפה/buffers/
cursor/filters משלו — כלום לא משותף בזיכרון בין השלושה; ה-DOM של `#acarsView`/
`#vdl2View`/`#satcomView` משתמש חוזר במחלקות ה-CSS `.acars-*`/`.dl-*`, אפס CSS כפול). `opts.prefix`
קובע גם את ה-endpoint (`/api/`+prefix) וגם את פענוח ה-DOM ids (`E(suf) => $(prefix+suf)`);
`opts.label` קובע את טקסט הסטטוס ("ACARS כבוי"/"מאזין · VDL2" וכו'). שני hooks
אופציונליים (no-op כברירת מחדל): `opts.onMessage(m)` — נקרא לכל הודעה חדשה
ב-`poll()`; שלושת המופעים מעבירים אותו כדי להזין `notifyMessage` (התראות על
`m.notable`, ר' למטה), ו-ACARS בלבד גם מזין את לוח ה-ATIS sticky ב-label A9.
`opts.onReset()` — נקרא כשה-cursor מתאפס (ACARS: מנקה את לוח ה-ATIS מהסשן
הקודם; VDL2/SATCOM לא מעבירים). `opts.emptyHint`
— טקסט מותאם למצב "אין הודעות עדיין" (VDL2: הפניה לתדר 136.975; SATCOM: הפניה
לאנטנת L-band/Alphasat; ACARS מקבל ברירת מחדל גנרית). `renderStats()` (משותף)
מכיל שתי שורות מוגנות-DOM (`E("StUp")`/`E("StDown")`, `null` בתצוגות בלי
האלמנטים) שממלאות שני תגי סטטיסטיקה **SATCOM-only** (`satcomStUp`/
`satcomStDown` — לא קיימים ב-`#acarsView`/`#vdl2View`) לספירת `dir===
"uplink"/"downlink"`. זה לא קוד-מיוחד-מחוץ-לפקטורי (העיקרון בפסקה הבאה) —
זו התאמה דרך *נוכחות ה-DOM* בהתאם ל-HTML של כל תצוגה, בדיוק כמו `opts.emptyHint`.
**הסיבה שזה קיים רק ב-SATCOM:** קליטת שטח ראשונה שהצליחה (16 דק', 206 הודעות,
54 מטוסים) הייתה **100% uplink** — ה-P channel (קרקע→מטוס, שידור-שידור גלובלי
חזק מה-GES) נועל ראשון וקל; ה-R/C channels (מטוס→קרקע, מקור התוכן ה"אמיתי" —
OOOI/דיווחי מיקום/הודעות טייס) חלשים משמעותית ותלויי-כיוון אנטנת המטוס. בלי
פירוט כיווני, "P channel בלבד" ו"קליטה דו-כיוונית מלאה" נראים זהים בפיד —
זו לא תקלה, אלא סימן ברור למה עוד לכוון/לחזק.
**⚠ regression: `renderStats()` לא קורא מ-`msgs` (חלון-נגלל, `MAX`=500) עבור
מספרים שאמורים לגדול כל הסשן.** `msgs` נועד *רק* לפיד/מפה/חיפוש — ברגע שסשן
עובר 500 הודעות, `msgs.length` נתקע על 500 לנצח (הכי-ישן מוחלף בהכי-חדש, לא
מצטבר), ובלי הפרדה `StTotal`/`StCraft`/`StPos`/`StUp`/`StDown` (כולם חושבו
מ-`msgs` בעבר) "נתקעו" יחד איתו — נראה כאילו הקליטה הפסיקה לרשום מעל 500,
כשבפועל הפיד המשיך לזרום כרגיל (נצפה בשטח בפועל, לא רק תיאורטית). התיקון:
`totalMsgs` — משתנה נפרד שרק גדל (מתאפס רק יחד עם `lastId` בזיהוי restart של
השרת, ר' `poll()`) — ו-`craft` (כבר לא נגזם, ר' `updateCraft`) הם המקורות
ל-`StTotal`/`StCraft`/`StPos`; `StUp`/`StDown` מסוכמים מ-`craft[id].up/down`
(שם ותמיד היו נצברים בלי הגבלה — הבאג היה רק בקריאה החוזרת דרך `msgs`
ב-`renderStats`, לא בצבירה עצמה). `StRate` **נשאר** מבוסס-`msgs` בכוונה —
קצב הוא מגמה עדכנית, לא ממוצע-כל-הסשן, וחלון-500 הוא בדיוק מה שרוצים שם.
ארכיון-יום (`enterArchiveDay`/`exitArchive`) משמר/משחזר את `totalMsgs`
כחלק מ-`liveSnapshot`, בדיוק כמו `lastId`/`feedMax`. הפקטורי עושה שימוש חוזר
בעוזרים ה*טהורים* הגלובליים בלבד (`fmtTime`/`mkSpan`/`dirBadge`/`normReg`/`trackColor`/
`CAT_GROUPS`/`DIR_INFO`/`MULTIBLOCK_RE`/`RETRANS_WINDOW_S`/`msgSig`/`patchMsgTime`/
`reconcileFeed`/`segSet`/`qualityCls` — מיון dBFS/SNR לשלוש רמות צבע). `showView`
מחומש (home/voice/acars/vdl2/satcom); `applyMode` משושה (voice/acars/vdl2/satcom/off/
scan — off ו-scan שניהם נוחתים בתצוגת home). ל-SATCOM אין בורר-תדרים אלא בורר-לוויין
(אותו מנגנון `setBanks`, כל "בנק" = לוויין), וב-`#satcomView` באנר-הוראה קבוע על
החלפת אנטנת ה-L-band הידנית + תזכורת toast בעצירה.

**ארכיון חיפוש רב-יומי** (בתוך אותו פקטורי, שלוש התצוגות מקבלות בחינם): כרטיס
`.acars-archive` (בורר `<input type=date>` + כפתור "🔎 חפש בארכיון" + "◀ חזרה
לשידור חי") מעל תיבת החיפוש. `enterArchiveDay(day)` שולף `GET /api/<prefix>?day=`,
שומר את המצב החי הנוכחי ב-`liveSnapshot` (msgs/lastId/feedMax/feedCache — פעם
אחת, לא נדרס בביקורים חוזרים בארכיון), מחליף את `msgs` בתוכן היום שנבחר (מזהה
`id` סינתטי-רציף, אין ל-jsonl), עוצר polling, ובונה מחדש markers/craft/roster/
detail/feed מהנתונים הארכיוניים — **משתמש באותם renderStats/renderRoster/
renderFeed/renderDetail בדיוק**, בלי מסלול קוד נפרד. `exitArchive()` משחזר את
`liveSnapshot` ומחדש polling. `show()` **לא** מחדש polling אם `archiveDay` עדיין
מוגדר (מעבר בין תצוגות תוך כדי עיון בארכיון לא "שובר" אותו בטעות בחזרה).

**יומן השידורים (תצוגת קול):** כל שורה כוללת ★/☆ (סימון/ביטול שמירה — `toggleStar`,
36px, `--amber`/`--amber-rgb` ולא hex קשיח — טוקן מוגדר ל-light *ו*-dark, בניגוד
לגרסה קודמת שהייתה בלתי-נראית בשמש). **כפתור התמלול (📝) אינו בשורה** — הוא חלק
משורת ה-meta שמתחתיה (`txMetaNode`, ר' למטה): זה גם פותר גלישת-שורה בטלפון צר
(‏★+שם-ארוך+תאריך לא נכנסו יחד ב-360px בגרסה קודמת — `.act-row` עם `flex-wrap`
כרשת-ביטחון נוספת) וגם נותן ל-📝 את ההקשר הטבעי שלו (השורה שכבר מסבירה *למה*
אין תמלול). `txNode(ev)` מנסח את `ev.tx.state` — **כולל המצבים שאינם טקסט**
("⏳ מתמלל…"/"⏳ ממתין בתור…" מבדיל `running` על סמך `tx.busy`, "🔇 לא זוהה דיבור",
"⚠ התמלול נכשל — סיבה") — RTL קבוע (שפת המערכת), בעוד תמלול אמיתי (`.act-tx`)
הוא LTR לאנגלית / `dir="rtl"` לעברית (`ev.tx.lang`). `err`/`raw` עוברים דרך
`<bdi>` (`unicode-bidi: isolate`) כדי שטקסט אנגלי/stderr בתוך משפט עברי לא ייקרא
בסדר הפוך. **אין יותר סינון-הזיות** בשרת (§12) — כל מה שwhisper פלט מוצג כפי שהוא.
`renderTxBar` (מ-`GET /api/transcribe`, פולינג 60ש') מציג את פקודת ההתקנה כשwhisper
חסר (`sudo INSTALL_WHISPER=1 ./install.sh` — הסדר קריטי, `sudo` מאפס env),
בורר שפה (`<select>`, מדווח "מודל חסר" כשאין מודל לשפה), מתג "תמלל כל שידור
אוטומטית", וכפתור הורדת-ZIP של השמורות (`GET /api/recordings/starred.zip`)
כשיש לפחות שמורה אחת. `pollActivitySoon` מרענן כל 3ש' למשך ~30ש' אחרי בקשת
תמלול. כפתור ★ ליד החיפוש מחליף את הפיד ל-`GET /api/activity?starred=1`
(`starMeta`/`starMetaVersion` — **גרסה, לא רק ערך**: פולינג שיצא *לפני* סימון
מוצלח אבל מגיע *אחרי* התשובה שלו לא ידרוס אותה — "מנצח לפי סדר יציאה", לא
לפי סדר הגעה; הוכח ב-Playwright עם דחיית-תגובה מכוונת ותוקן).

> בעריכת ה-UI: שמור על polling קל, על נפילה חיננית בלי רשת, ועל RTL/עברית נכונה.
> שינוי שנוגע לכל תצוגות-הדאטה — ערוך את הפקטורי `createDataView` (מקום אחד, שלושת
> המופעים יורשים); שינוי ACARS-only/VDL2-only/SATCOM-only בלבד — דרך `opts`
> (label/prefix/emptyHint) או hook חדש (`onMessage`/`onReset`), לא קוד מיוחד מחוץ לפקטורי.

**שכבת רשת: `fetchTimeout`/`fetchJSON` + חיווי ניתוק גלובלי.** כל בקשת רשת בקליינט
(כל 16 אתרי ה-`fetch` המקוריים, כולל `apiSend` ל-POST) עוברת `AbortController` עם
timeout (`NET_TIMEOUT_GET`=8s / `NET_TIMEOUT_POST`=45s — תואם את חסם ה-45s בצד
השרת ל-restart/rollback). הצלחה (**כל** תשובה מהשרת, כולל 4xx/5xx — יש קשר, גם אם
הפעולה עצמה נכשלה) מרעננת `lastNetOkAt`; `setInterval` נפרד (3s) בודק אם עבר
`NET_DISCONNECT_AFTER`=12s בלי שום הצלחה, ומציג צ'יפ `#connChip` ("אין קשר לשרת")
ברצועת הסטטוס — **גלובלי, לא תלוי תצוגה/מצב**, בניגוד לחיווי התקלה של
`pollGlobalState` שמוצג רק במסך הבית. בלי השכבה הזאת, בקשה תקועה (Wi-Fi בקצה
טווח, Pi שנתקע) הייתה מקפיאה פיד/כפתור בלי הגבלת זמן ובלי שום חיווי — בדיוק המצב
שבו הדף נראה "תקין" בשטח כשהוא לא.

**בקרת רווח הקול — LNA, מסנן FM, עומס חומרה (v2.26.0).** סליידר ה-LNA (`#rfGain`, תווית
"LNA") **פעיל גם תחת AGC** — רק `#ifGain` מושבת (`renderGainUI`); סקאלת התצוגה נשארה "רווח"
(ימינה = יותר, ערך = ‎9 − rf_gain, ה-LNA state הגולמי ב-tooltip) — **בכל מקום** שמוצג LNA (סליידר,
שורת ה-RF, שורות היומן, הבסיס) אותה סקאלה `x/9` (`lnaStep`). `#fmNotch` מאותחל מ-`/api/state`
**בטעינת הדף** דרך `applyGainState(st)` (לא בכניסה לתצוגת הקול — כל כיוונון, כולל "האזן
למגדל", שולח `fm_notch`). `retune` ו-`towerTune` שולחים `rf_gain` תמיד ו-`...fmNotchField()` —
‏`fm_notch` **רק** אחרי ש-`/api/state` אתחל את המתג (אחרת השרת שומר את הערך השמור). כיוונון
שנכשל ⇒ `applyGainState(data.state)` (או `lastGainState` בכשל רשת) מיישר את המתג והסליידרים.
⚠ סליידרי הרווח (`.gain-slider input[type=range]`) הם `direction: ltr` — בדף RTL טווח יורש rtl,
כלומר *שמאלה* = רווח מרבי, בניגוד ל"ימינה = יותר"; ההוראות עצמן מנוסחות בלי כיוון ("הורד את
ה-LNA"). `renderRfHw(m.rf)` (`#rfHw` + צ'יפ `#overload`) מציג **שלושה מצבים, לא שניים**:
‏`rf.overload===true` אדום, `false` ירוק "(לפי החומרה)", `null` ניטרלי — עם הסיבה מ-
`RF_UNKNOWN_TEXT[rf.unknown_reason]`; `telemetry===false` ⇒ "לא זמין — `sudo ./install.sh`";
תגובה בלי `rf` (שרת ישן/כשל) ⇒ "לא ידוע". "ה-AGC בחר IFGR" רק בטווח ה-spec (`ifgrPlausible`,
20–59), מחוצה לו "gRdB גולמי (לא מאומת)". מצויר גם כשה-stats לא טריים (העומס לא תלוי בהם).
ביומן, `rfMetaNode(ev)` (`.act-rf`, מתחת לשורה) מציג מ-`ev.rf`: "LNA x/9 · AGC" רק כש-
`config_known`, "⚠ עומס RF ×N"/"ללא עומס"/"עומס: ?" (גלוי — §12), ו-IFGR min–max; **לא**
מציג את `post_stats` של ה-sidecar (תמונה ברגע הזיהוי, אחרי סוף השידור — ייחוס לישות
הלא-נכונה). `renderExpResult` מציג גם את הקצה הקדמי של ריצת הניסוי (`r.lna`/`r.fm_notch`). מד השדה (`verdictInfo(v, reason)`) מסביר `baseline_config_mismatch`/
`baseline_untagged`/`manual_gain`, ושורת הבסיס מציגה את ה-LNA/מסנן שבהם נמדד.

**🩺 בדיקת RF (`#rfcBox`, בפאנל ה-RF של הקול, *מחוץ* ל-`#rfPanel` כדי ש-`.stale` לא יעמעם אותה):**
`pollRfcheck` (כל 1ש' בזמן ריצה, אחרת כל 15ש' בתצוגת הקול + ב-`showView("voice")`), `renderRfcheck`
(התקדמות "בודק LNA x/9 · i/5", טבלה LNA/SNR±פיזור/עומס — `null` ⇒ "? לא ידוע", לעולם לא ✓; המלצה
בשפת הסליידר `lnaStep` + "✓ החל" רק כשהיא שונה מ-`lastGainState.rf_gain`). בסוף ריצה: `reloadStream()`
רק אם ניגן קודם, ו-`applyGainState` מ-`/api/state`. `pollGlobalState` יוצא מוקדם כש-`h.rf_check`.

**מד שדה מאוחד: `createFieldMeter(mode, prefix, opts)`.** פקטורי שני (לצד
`createDataView`) לשלושה מופעים — `voiceFm` (`{compact:true}`, נספח ל-`#rfPanel`
הקיים: רק בסיס/פסק-דין/כפתור כיול, כי המדדים הרציפים כבר שם), `acarsFm`, `vdl2Fm`
(פאנל `.field-meter` עצמאי: level/snr אחרונים + age, בסיס, פסק-דין, וכפתורי
"🔍 בדוק אנטנה"/"📏 כייל בסיס"). קורא `GET /api/signal` (פולינג 3s, רק בתצוגה
הרלוונטית + מיד ב-`showView`/אחרי כניסה מוצלחת ב-`applyMode` — כמו `pollSatcomHealth`)
ומפעיל `POST /api/antenna/check`. ‏SATCOM **לא** מקבל מופע — יש לו `#satcomAimPanel`
משלו, ואין בו צורך (יש `/api/satcom/health` ייעודי). כל שינוי שנוגע לשלושת
המופעים — בפקטורי, לא בקוד ACARS/VDL2/voice נפרד.

**דוח סשן + התראות (`#sessionCard`, מסך הבית בלבד).** `pollSession()` (20s, רק
במצב home + מיד ב-`showView("home")`) קורא `GET /api/session` ומציג כרטיס רק
כשיש היעדרות משמעותית (`SESSION_MIN_DUR_SEC`=600) ותוכן ממשי; "✓ הבנתי" קורא
`POST /api/session/ack`. "🔔 הפעל התראות" מבקש `Notification.requestPermission()`
(מצב נשמר ב-`localStorage["airam_notify"]`, כמו ה-PIN); `notifyMessage(mode, m)`
— מוזרם דרך `opts.onMessage` של שלושת מופעי `createDataView` — מציג התראה מקומית
לכל הודעה עם `m.notable` (בקצב מוגבל, `NOTIFY_MIN_GAP_MS`=4s). מקומי (Notification
API), **לא** Web Push/VAPID — עובד רק כשהטאב/PWA פתוחים ברקע.

**שחזור-סשן: רשימה + נגן (`docs/session-replay-design.md`, שלב 3) — "תצוגה"
עצמאית, לא `appMode`.** נפתחת מעל 🏠 מהכרטיס `.p-replay` (`openReplayList()`);
כל מעבר-תצוגה אחר (`showView`, כולל בין קול/ACARS) סוגר אותה ועוצר נגינה/timers
פעילים (`stopReplayPlayback()`) — כדי שלא יישארו שני "מסכים" גלויים בו-זמנית.
כרטיס-הבית מציג רמז-זמינות מ-`GET /api/replay/buffer` (`loadReplayBuffer`) ומונה
סשנים (`refreshReplaySessionsCount`), ושולח `POST /api/sessions` (`saveReplaySession`).
מסך הרשימה (`loadSessionsList`/`buildSessionCard`) מציג כרטיס לכל סשן עם פתיחה/
ייצוא/מחיקה (`deleteReplaySession`, עם `confirm()`). מסך הנגן (`initReplayPlayer`) — **פריסה: מפה → סיכום-מטוסים מתקפל → רשימת
תשדורות → סרגל-נגן דביק בתחתית** (`.replay-player-bar`, מכיל ציר-צפיפות +
פקדים + תמלול). במסך רחב (≥1024px) המפה והרשימה זו-לצד-זו (`.replay-cols`).
- **מפה** (Leaflet, אותו דפוס טעינה-עצלנית/`layerGroup` כמו ב-`createDataView`)
  מציגה את שורת-המסלול הכי-עדכנית שאינה מאוחרת מ-`playT` (`findReplayRowAt` —
  **לא** אינטרפולציה בין נקודות). ⚠ **מסננת לרדיוס-תצוגה** (`REPLAY_VIEW_NM`=40,
  `nmFromArp`) עם מתג "הצג את כל הרדיוס" — **סינון-תצוגה, לא סינון-נתונים**:
  ‏`RADIUS_NM`=250 של `adsb.py` נבחר לזיהוי שיבוש GPS ומתועד ב-§5.2 בתכנון
  כ"לא לתצוגה", והצגת כולו נתנה 61 סיכות זהות בלתי-קריאות. הסמנים הם
  `circleMarker` עם **היררכיה חזותית** (קרוב+נמוך = גדול ואטום), ו-`fitBounds`
  ממקד על הנתונים בפתיחה. מטוס עם מיקום משובש (`lat=null`, §7.1) מוצג כצ'יפ
  `.spoofed` ולא כסמן — לא ממציאים מיקום, לא מוחקים את המטוס מהרשימה.
- **ציר-צפיפות** (`drawReplayDensity`, canvas כמו `rfSpark`/`drawSpectrum`) —
  ⚠ **החליף ציר שבו כל קליף היה בלוק נפרד**: ב-45 דק'/77 תשדורות כל הבלוקים
  נדחסו לרוחב המינימלי (2px) ונראו כברקוד אחיד — אי אפשר היה להבחין בין
  תשדורת של 2ש' ל-6ש', בין קליפ לפער-קליטה, ולא ללחוץ עליהם בטלפון. הציר
  מציג **צפיפות** (שניות-אודיו לדלי) ולכן קריא גם ב-7700 תשדורות; פערי-ADS-B
  מסומנים **נקודתית** (§7.2: לא נמדד משך-פער בפועל). לחיצה = קפיצה בזמן.
- **רשימת התשדורות** (`renderReplayTxList`) — **הניווט הראשי**: שעה, תדר, משך
  וסימון-מצב-תמלול, כל שורה יעד-נגיעה של 40px. השורה המתנגנת מסומנת ונגללת
  לתצוגה (`renderReplayNowTx`).
- **תמלול** (`#replayNowTx`) — הטקסט של התשדורת המתנגנת, מ-`tx` שמגיע חי
  מ-`GET /api/sessions/<id>`. מבחין בין חמשת מצבי ה-`tx.state` כמו יומן
  השידורים (§12: "לא ניסינו" ≠ "ניסינו ונכשלנו"), ו-`dir` לפי `tx.lang`.
- **נגן**: `<audio>` יחיד משותף לשני מצבי השמעה — **"דלג על שקט"**
  (`#replaySkipSilence`, ברירת מחדל; `onReplayClipEnded` קופץ לקליפ הבא
  כש-`ended` **או** `error` מגיע — קובץ פגום בודד לא תוקע את הנגן) מול
  **זמן-אמת** (`replayTick` מתקדם עם שעון-קיר אמיתי, `wallStartMs`/
  `wallStartPlayT`, ומשאיר שקט אמיתי בין קליפים). **שעון-קיר** (`fmtClockHM`,
  "20:46") לצד המונה היחסי — בתעופה זה המידע שמעניין.

**נבדק ידנית בדפדפן** (Playwright) **מול נתונים ריאליסטיים** (45 דק', 70
מטוסים, 77 תשדורות) בשלושה רוחבי-מסך — ⚠ הבדיקה מול סשן-דמה קטן (2 מטוסים,
2 קליפים) **החמיצה לגמרי** את שלוש הבעיות שלמעלה; עומס אמיתי הוא חלק מהבדיקה,
לא פינוק.

---

## 8. REST API (כל ה-routes)

| Method | Route | תיאור |
|--------|-------|------|
| GET | `/` | הדף הראשי |
| GET | `/<path>` | נכסים סטטיים |
| GET | `/live.m3u` | playlist לנגן חיצוני |
| GET | `/stream` | proxy same-origin ל-Icecast (נדרש כש-HTTPS, mixed-content) |
| GET | `/api/state` | המצב הנוכחי (תדר, mod, gain — `agc`/`if_gain`/`rf_gain`, `fm_notch`, squelch, app_mode, `mode_ok` — המצב השמור באמת רץ?, `prev_mode`, `scan_plan`, `acars_banks`, `vdl2_banks`, `vdl2_freqs`). ה-UI מאתחל ממנו את בקרי הרווח ומתג ה-FM בטעינה (`applyGainState`) |
| GET/PUT | `/api/presets` | קריאה/עדכון פריסטים |
| POST | `/api/tune` | **כיוונון תדר** (קול). דרך `_guard`. ‏`rf_gain` (מצב LNA 0–9) נקלט ונשמר **גם כש-`agc=true`**; ‏`fm_notch` (bool, עמיד ל-`"false"` טקסטואלי) — **לפי נוכחות המפתח**: חסר ⇒ נשאר כפי שנשמר (לקוח ישן/curl לא מכבה מסנן בשקט); `null` מפורש ⇒ כבוי. שאר השדות — החלפה מלאה (חסר ⇒ ברירת מחדל) |
| POST | `/api/mode` | **מעבר מצב** voice/acars/vdl2/satcom/off (standby)/**scan** (סבב). דרך `_guard`. `mode:"scan"` מקבל גם `plan` (רשימת רגלים; ברירת מחדל — הלוח השמור). `mode:"satcom"` מקבל `freqs` כרשימה בת-איבר-יחיד עם דגל הלוויין (למשל `["AF1"]`, ברירת מחדל — geostationary, לא בנק ערוצים), `bias_tee` (bool אופציונלי — `false` להזנת LNA חיצונית), `skip_c` (bool אופציונלי — `false` לפענוח כל 12 הערוצים; ברירת המחדל `true` חוסכת ~50% CPU, ר' §12), `spectrum` (bool אופציונלי — `false` לכיבוי האבחון, ר' §9/§12) ו-`gain` (‏`null`=AGC (ברירת מחדל) או מספר=gRdB ידני 20–59, נחתך; ר' §12). לכולם: ברירת מחדל = הבחירה השמורה. ⚠ ב-`gain` בלבד `null` הוא **ערך משמעותי** (AGC מפורש) ולא היעדר — הזיהוי הוא לפי נוכחות המפתח בגוף הבקשה. כישלון ⇒ נפילה ל-off: `{ok:false, error, detail, app_mode:"off", state}` + 500 |
| GET | `/api/scan` | סטטוס סבב הסריקה החי: `active`, `idx`, `leg`, `next_switch_at`, `plan` (ל-UI — רגל נוכחית + ספירה לאחור) |
| GET | `/api/acars` | הודעות ACARS אחרונות (**היום בלבד**; `?all=1` לכל מה שבזיכרון; `?day=YYYY-MM-DD` ארכיון מהדיסק, snapshot סטטי) + שדה `adsb` (העשרת ADS-B לזנבות שבפיד; `{}` בלי אינטרנט; לא ב-`?day=`). כל הודעה כוללת `level` (dBFS) ו-`snr` (None ב-ACARS — ראו §12) |
| GET | `/api/acars/export?format=csv\|json` | ייצוא (CSV עם BOM, עמודות `level`+`snr`) |
| GET | `/api/vdl2` | הודעות VDL2 אחרונות (**היום בלבד**; `?all=1`; `?day=YYYY-MM-DD` ארכיון) + שדה `adsb`. אותה סכמת כרטיס כמו ACARS + `icao`; `snr` תמיד אמיתי (dumpvdl2 מספק רצפת רעש) |
| GET | `/api/vdl2/export?format=csv\|json` | ייצוא VDL2 (CSV עם BOM, עמודות `icao`+`level`+`snr`) |
| GET | `/api/satcom` | הודעות SATCOM (Inmarsat, inmarsat-sniffer) אחרונות — אותם `?since=`/`?all=1`/`?day=`. אותה סכמת כרטיס כמו ACARS; **בלי** `level`/`snr`/`freq`/`adsb` (המפענח לא חושף אותם ב---feed/--udp — לעולם לא מומצאים, ראו §12) |
| GET | `/api/satcom/export?format=csv\|json` | ייצוא SATCOM (אותן עמודות כמו ACARS export) |
| GET | `/api/satcom/health` | אבחון SATCOM — proxy מקומי ל-dashboard האבחוני המובנה של inmarsat-sniffer (`--web`): נעילת דמודולטור/ebno/mse לכל ערוץ, גם באפס הודעות מפוענחות. `available:false` (לא שגיאה) כש-satcom כבוי/dashboard לא זמין. כולל `spectrum` (‎`spectrum_enabled` **של הכלי עצמו**, לא ה-state שלנו — האם `/api/satcom/spectrum` יעבוד *עכשיו*) |
| GET | `/api/satcom/spectrum?ch=N&bins=N` | ספקטרום baseband של ערוץ בודד (proxy ל-`/api/spectrum` של הכלי, דורש `--spectrum`). **האבחון היחיד שמפריד "אין RF בכלל" מ"יש RF, לא נעול"** — `lock`/`ebno` מציגים את שניהם זהים. `mags_db` מוגש **כמות שהוא**, בלי סף/ציון מומצא (§12). `bins` נחתך ל-32..1024 כמו ב-`web.c`. `available:false` (לא שגיאה) כשכבוי/ערוץ לא קיים |
| GET | `/api/satcom/log` | זנב `journalctl -u airam-satcom` (‎`SATCOM_LOG_TAIL_LINES`=40). שורות הפתיחה מכריעות אם ה-bias-T נדלק (`sdrplay: bias tee enabled` מול `bias tee not supported on this model`) — בשטח אין SSH. **על דרישה בלבד**, לא בפולינג (fork ל-journalctl) |
| GET | `/api/aircraft` | רוסטר מטוסים מאוחד — היתוך ACARS+VDL2+SATCOM+ADS-B לפי זהות (רישום/icao/טיסה). חי בכל מצב |
| GET | `/api/session` | דוח סשן ("מה קרה בזמן שלא הסתכלת"): כמות הודעות/מטוסים (וחדשים), עד `SESSION_HIGHLIGHTS_MAX` הודעות `notable` (ר' `_interest_score`), ומסלול פעיל/שיבוש GPS מתוך `adsb.session_series`. קורא מהדיסק (jsonl), לא מהזיכרון — עקבי עם `?day=`. `?since=<epoch>` דורס את הסמן השמור; `GET` idempotent — לא מקדם אותו |
| POST | `/api/session/ack` | מקדם את הסמן (`state["last_session_view_at"]`) ל"עכשיו" — הפעולה המפורשת היחידה שמקדמת אותו. דרך `_guard` |
| GET | `/api/signal` | מד שדה מאוחד למצב שרץ *בפועל* כרגע: רציף בקול (`_read_voice_metrics`), "הודעה אחרונה בלבד" ב-ACARS/VDL2 (`level`+`snr` — ACARS לעולם בלי `snr`, ר' §12), הפניה ל-`/api/satcom/health` ב-SATCOM. `verdict` (`ok`/`below_baseline`/`no_baseline`/`unknown`) תמיד מול `state["signal_baseline"]` בלבד — לעולם לא סף מומצא. `verdict_reason` מסביר `no_baseline`/`unknown` (`no_reading`/`no_baseline`/`baseline_untagged`/`baseline_config_mismatch`/`manual_gain`); בקול גם `frontend` (`{lna, fm_notch, agc}` שמולו הבסיס חייב להתאים) |
| GET/POST | `/api/rflog` | רשם ניסוי RF: GET מצב (`active`/`until`/`rows`/`marks`/`size`); POST `{active}` הפעלה/עצירה (idempotent). דרך `_guard` (POST). ר' `docs/antenna-calibration-experiment.md` |
| POST | `/api/rflog/mark` | `{label}` (עד 40 תווים) — סימון אירוע פיזי. **409 כשהרשם כבוי** (סימון שלא נרשם לא נראה כאילו נרשם). דרך `_guard` |
| GET | `/api/rflog/export` | הורדת `rf_log.jsonl` (404 כשאין) |
| GET/POST | `/api/experiment` | ניסוי כיול אוטומטי. GET: מצב (`running`/`step`/`step_index`/`steps_total`/`waiting`/`eta_sec`/`eta_prompt_sec`/`error`/`result`). POST `{action}`: `start` (409 כש-SATCOM חי/סריקה/`TUNE_LOCK` תפוס/כבר רץ), `confirm` (הפעולה הפיזית בוצעה; 409 כשאין בקשה ממתינה), `abort`. מחזיק את `TUNE_LOCK` לכל אורכו. דרך `_guard` (POST) |
| POST | `/api/antenna/check` | בדיקת אנטנה בת ~3 שניות: מעבר זמני לקול (AGC, סקוולץ' פתוח) בתדר המבוקש, מדידת רצפת רעש אמיתית (`_sample_probe_stats`), וחזרה למצב הקודם (`_restore_after_probe`, גם בכישלון). מודדת ב-LNA/מסנן ה-FM **השמורים של הקול** (`_probe_frontend`) ומחזירה גם `lna`/`fm_notch`/`verdict_reason`. `calibrate:true` שומר את התוצאה כ-`signal_baseline` **מתויגת** (`{noise, freq, ts, lna, fm_notch, agc}`). לא נוגע ב-`state["app_mode"]` — פעולת אבחון, לא מעבר-מצב. סריאלי תחת `TUNE_LOCK`; 409 כשתפוס |
| GET | `/api/activity` | יומן שידורים. כל אירוע כולל `exists`, `starred`, `tx` (‏`{state, text, err?, raw?, filtered?}` — ר' §12), ו-`rf` (תוכן ה-sidecar ‏`.rf.json`, נקרא חי; `null` = אין רשומה — הקלטה ישנה, לא "תקין"). גם קליפי `GET /api/sessions/<id>` מקבלים `rf`. `?starred=1` => רק ההקלטות המסומנות, **מתיקיית `saved/`** (‏`_rec_event` מהקובץ עצמו) ולא מהיומן (שורדות את קיצוץ `ACTIVITY_KEEP`) |
| POST | `/api/recordings/star` | `{file, starred}` — שמירה/ביטול (★, מעביר ל/מ-`saved/`, `os.replace` אטומי תחת `_STAR_LOCK`). שמורה פטורה מ-retention. 409 כשהמכסה מלאה (**לא מוחקים שמורה ותיקה**), 404 כשההקלטה כבר לא קיימת. דרך `_guard` |
| POST | `/api/recordings/transcribe` | `{file, lang?}` — תמלול שידור בודד לפי דרישה (כותב sidecar `state="pending"` — שורד restart, לא תור-בזיכרון). 501 עם פקודת ההתקנה כשwhisper/המודל לשפה חסר. דרך `_guard` |
| GET | `/api/recordings/starred.zip` | ZIP של כל ההקלטות השמורות + תמלוליהן (`ZIP_STORED`, ללא דחיסה — MP3 כבר דחוס). 404 כשאין שמורות. הדרך להוציא את השמורות לפני מוות של כרטיס SD |
| GET/POST | `/api/transcribe` | GET: מצב המנגנון (`available`/`model_name`/`lang`/`langs`/`auto`/`queue`/`install_hint`) — זה מה שמאפשר ל-UI לומר "לא מותקן, הנה הפקודה". POST `{auto?, lang?}`: מתג "תמלל הכול" ו/או שפת תמלול (שניהם ב-state). דרך `_guard` |
| GET | `/recordings/<name>` | קובץ הקלטה MP3 — מחפש בתיקייה החיה ואז ב-`saved/` |
| GET | `/api/metrics` | מדדי RF (SNR/signal/noise מ-stats_filepath) + `rf: {telemetry, overload, overload_events, last_overload_age, ifgr, lna_grdb, lna_state, fm_notch, agc, unknown_reason}` מטלמטריית החומרה. `overload` העליון = `rf.overload` (`true`/`false` מראיה חיובית מהדרייבר — `false` רק אחרי `stream=start` של הסשן — **`null` = לא ידוע**, ו-`unknown_reason` אומר למה). `overload_events` תמיד int — משמעותי רק כש-`overload !== null`. `overload_dbfs` **הוסר** |
| GET | `/api/airspace` | מסלול פעיל + שיבוש GPS (מ-adsb.py) |
| GET | `/api/replay/buffer` | מצב ה-buffer המתגלגל של ADS-B — `t_oldest`/`samples`/`gaps` (מ-`adsb.read_track_buffer`) + `clips_available` (יש הקלטה בתוך חלון הבאפר, נבדק כאן כי רק app.py מכיר את `REC_DIR`/`saved/`). שלב 1 ב-`docs/session-replay-design.md` |
| GET/POST | `/api/sessions` | GET: רשימת סשנים שמורים (חדש→ישן). POST `{minutes, note?}`: שומר את N הדקות האחרונות (נחתך ל-`adsb.TRACK_BUFFER_MIN`) — מסלול ADS-B + הקלטות בחלון (שמורה מ*עתיקה*, לא-שמורה מ*ועברת*) ל-`sessions/<id>/`. שלב 2. דרך `_guard` (POST בלבד) |
| GET/DELETE | `/api/sessions/<id>` | GET: `meta.json` + שדה `tx` לכל קליף (**נקרא חי מה-sidecar**, לא מוקפא בשמירה — קליף יכול להתמלל אחריה). DELETE: מחיקת הסשן כולו (בלתי הפיך). דרך `_guard` (DELETE בלבד) |
| GET | `/api/sessions/<id>/track` | מסלול ה-ADS-B של הסשן, מפוענח מ-`track.jsonl.gz` |
| GET | `/api/sessions/<id>/clips/<name>` | קליפ אודיו של הסשן — route ייעודי, לא הרחבת `/recordings/<name>` |
| GET | `/api/sessions/<id>/export.zip` | ייצוא הסשן כולו (מטא-דאטה+מסלול+קליפים), אותו דפוס כמו `starred.zip` |
| GET | `/api/sdr` | חיווי SDR: `state` (`missing`/`ours`/`switching`/`checking`/`api_down`/`api_error`/`free`/`busy`/`unavailable`/`unknown`), `usb`/`usb_desc`, `mode`+`service_state` כש-`ours`, `label` כש-`free`, `suspects` (לפי שם תהליך — רמז) כש-`busy`, `checked_age`. "פנוי" = ה-SDRplay API מציע אותו (`SoapySDRUtil --find`), נבדק רק כש-AIR-AM לא מחזיק בו |
| GET/POST | `/api/rfcheck` | 🩺 בדיקת RF. GET: `running`/`step`/`states`/`rows` (חלקי בזמן ריצה)/`error`/`est_sec`/`result` (= `state["rf_check_last"]`). POST `{action}`: `start` (409 כשרץ/ניסוי/סריקה/SATCOM חי/`TUNE_LOCK` תפוס), `abort`, `apply` (409 בלי המלצה/כשרץ/כשכבר בתוקף). דרך `_guard` |
| GET | `/api/power` | מתח/טמפ' ה-Pi (`vcgencmd`), מוגש מ-cache בן ~2 שניות (`_POWER_TTL`) |
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
- **PIN אופציונלי** דרך `AIRAM_PIN` ב-`/etc/airam/airam.env` (כבוי כברירת מחדל, קובץ
  `chmod 640` — לא world-readable). השוואה בזמן-קבוע (`hmac.compare_digest`, לא `==`)
  + rate-limit רך לפי IP (`PIN_RATE_MAX_ATTEMPTS`/`PIN_RATE_DELAY_SEC`) — לא חוסם
  IP לצמיתות (DHCP/NAT), רק מאט brute-force על מרחב 4-ספרות מ"שניות" ל"שעות".
- **הגנת CSRF (לא DNS-rebinding):** `_guard` דוחה בקשות משנות-מצב כש-`Origin != Host`.
  ⚠ **זו לא הגנת DNS-rebinding אמיתית** — בהתקפת rebinding הדפדפן שולח `Host`
  *ו*-`Origin` שנגזרים מאותו דומיין-תוקף ששוחזר ל-IP של ה-Pi, כך ששניהם תואמים
  ועוברים את הבדיקה. **החלטה מכוונת לא לצמצם**: תיקון אמיתי דורש allowlist של
  Host מותרים (IP/hostname ספציפיים), וזה סותר את המטרה "אפס-קונפיגורציה,
  שליטה מכל מכשיר ברשת" — IP יכול להשתנות (DHCP), וגישה יכולה לבוא דרך
  hostname/mDNS/Tailscale שונים. ה-`_guard` נשאר כהגנת-CSRF-רגילה (חוסם בקשה
  מדף בדומיין *אחר* לגמרי, המקרה הנפוץ), וה-DNS-rebinding הספציפי נשאר סיכון
  מקובל בהתאם למודל "רשת פרטית מהימנה בלבד" (סעיף זה, למטה) — לא "לא ידענו".
- **פורט אבחון SATCOM (`SATCOM_WEB_PORT`, ברירת מחדל 8888) נקשר ל-INADDR_ANY** —
  ‏`inmarsat-sniffer --web` לא ניתן להגבלה ל-loopback דרך הכלי עצמו (נבדק
  במקור). נגיש ברשת המקומית גם בלי `GET /api/satcom/health` (ה-proxy של
  airam-web) — אותה קטגוריית סיכון בדיוק כמו Icecast (גם הוא בלי אימות).
  ⚠ **`--spectrum` (דלוק כברירת מחדל) מוסיף לאותו פורט גם `GET /api/tune?ch=N&hz=X`
  — endpoint משנה-מצב** (retune של דמודולטור), בנוסף ל-`/api/spectrum`/
  `/api/constellation` הקוראים בלבד. זה לא נוגע ב-state של AIR-AM ולא ב-SDR
  עצמו (רק בדמודולטור של הכלי, עד ההפעלה הבאה), אבל זו הרחבה אמיתית של משטח
  התקיפה על פורט לא-מאומת. לכן זה **מתג** (`POST /api/mode {mode:"satcom",
  spectrum:false}`, נשמר ב-`state["satcom_spectrum"]`) ולא קבוע — מי שמעדיף
  לוותר על האבחון יכול לכבות. ההצדקה לברירת המחדל: בלי נעילה המצב חסר ערך
  ממילא, וזה הכלי היחיד שמאבחן *למה* אין נעילה (ר' §12).
- מיועד **לרשת פרטית בלבד**. אל תחשוף 8080/8000/8888 לאינטרנט; לגישה מרחוק — VPN/Tailscale.
- **`_decode_libacars_app` מריץ `subprocess.run` על `decode_acars_apps` עם `msg_text`
  כ-argv נפרד** (`app.py`, ליד `_libacars_decode`) — `msg_text` מגיע מרשת (SATCOM,
  לא מהימן) ולכן **תמיד `shell=False`** (ברירת המחדל של `subprocess.run`, לא
  מוגדר מפורש אחרת). זה גם ה-`subprocess.run` ה**יחיד** ב-`app.py` שמעביר `env=`
  (`os.environ.copy()` + `LA_JSON=1` רק בהרצת ה-JSON) — שאר הקריאות (`systemctl`,
  `journalctl`, `vcgencmd`, `ffmpeg`/`whisper`) לא צריכות env מיוחד. fail-safe
  מוחלט: `FileNotFoundError`/`TimeoutExpired`/`OSError`/`returncode!=0`/JSON פגום
  כולם נתפסים ומחזירים `None` — לעולם לא זורקים, לעולם לא מפילים הודעה.

---

## 10. `install.sh` — 8 שלבים (אידמפוטנטי)

1. תלויות מערכת (Python/Flask, `libglib2.0-dev` ל-dumpvdl2 וכו'). 2. **SDRplay API**
(הורדת `.run`, חילוץ והתקנה ללא אישור רישיון ידני — `SDRPLAY_VER` בראש הקובץ).
3. בניית `SoapySDRPlay3` — **נעוץ** ל-`SOAPYSDRPLAY_COMMIT` (‏48bd8b4 = tag ‏`soapy-sdrplay3-AI-0.6.0`,
נעוץ לפי SHA) + **`patches/soapysdrplay3-airam-rf.patch`** (`git apply --check && git apply`, ואז grep
של ארבע מחרוזות החוזה). חתימת בנייה = sha256(commit + sha256 של ה-patch + דגלי cmake); דילוג רק
כשהמודול נטען **וגם** ה-marker תואם. ה-marker (`/usr/local/share/airam/soapysdrplay3.build-sig`)
נמחק בתחילת כל בנייה ונכתב **רק** אחרי patch מאומת + בנייה מוצלחת — patch שנכשל ⇒ בנייה בלעדיו,
בלי marker (רדיו עובד, טלמטריה "לא זמינה"). clone לתיקייה זמנית ⇒ בלי רשת מקור/מודול קיים נשארים
(ובלי מודול כלל — die). ‏`checkout -f` מנקה patch מריצה קודמת. 4. בניית `rtl_airband` (4b: `libacars` ≥2.1.0 + `acarsdec`
ל-ACARS — הגייט בודק גם `command -v decode_acars_apps` (כלי ה-CLI הרשמי, מותקן
ללא-תנאי ע"י `examples/CMakeLists.txt` של libacars) לצד `pkg-config`, כי
`_decode_libacars_app` ב-`app.py` תלוי בו לפענוח SATCOM CPDLC/ADS-C בכיוון הנכון
(ר' §5/§12); אימות נפרד אחרי `make install` — `libacars` הוא היחיד מבין רכיבי
ה-build שאין לו build-signature משלו (probe-יכולת בלבד, לא שינוי דגלי cmake);
4c: `dumpvdl2` ל-VDL2, נעוץ ל-`DUMPVDL2_VER=v2.6.0`, חתימת בנייה; **4d:
`inmarsat-sniffer` ל-SATCOM, נעוץ ל-commit (`SATCOM_SNIFFER_COMMIT`) — אין
releases רשמיים לפרויקט — חתימת בנייה נכתבת *רק* כשתמיכת SDRplay אושרה בפועל
מלוג ה-cmake, אחרת הרצה חוזרת תמיד תבנה מחדש**). 5. `Icecast2` (מאזין בלי
סיסמה). 6. קונפיג התחלתי + state (6b: יצירת משתמש `airam` + sudoers ממוקד —
**9 פקודות systemctl**: restart/stop × rtl_airband/airam-acars/airam-vdl2/
airam-satcom, ועוד `reset-failed` ל-airam-satcom בלבד (מנקה תקרת-הפעלות אחרי
קריסה — ר' §12); seeding של `acars.env`+`vdl2.env`+`satcom.env`). 7. שרת הווב
(7b: תמלול whisper אופציונלי, `sudo INSTALL_WHISPER=1 ./install.sh` — ⚠ הסדר
קריטי: `INSTALL_WHISPER=1 sudo ...` נבלע בשקט, `sudo` מאפס env כברירת מחדל
ב-Debian; שני מודלים — `small.en`+`small`, ר' §5/§12). 8. שירותי systemd —
**enabled רק `sdrplay`+`airam-web`**; אף צרכן SDR (כולל rtl_airband) לא
enabled, ובשדרוג `disable rtl_airband` אידמפוטנטי. המצב משוחזר באתחול ע"י
`_boot_restore` של airam-web — **חוץ מ-satcom**, שלא משוחזר אוטומטית מסיבות
בטיחות (ר' §12).

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
- **CI** (`.github/workflows/ci.yml`): pytest על **Python 3.11 ו-3.13** (מטריצה —
  Pi OS Bookworm/Debian 12 מול Pi OS Trixie/Debian 13; שתיהן בשטח בפועל)
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
  מכוונת). **חריג יחיד: `satcom` לא משוחזר אוטומטית** — `write_satcom_env` מדליק
  bias-T (‎+4.7V על מחבר האנטנה) כברירת מחדל, ואחרי reboot אין בן-אדם בסביבה
  שיוודא איזו אנטנה מחוברת כרגע; `_boot_restore` נופל בכוונה ל-`off` עם
  `prev_mode="satcom"` (כפתור ⏻/כרטיס הבית מציעים כניסה ידנית — עם אישור אנטנה
  מפורש, ר' `confirmSatcomAntenna` ב-`index.html`). **כישלון כניסה למצב ⇒ נפילה ל-`off`** (`_fail_to_off`), לעולם לא fallback
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
  **בטיחות bias-T (סיכון חומרה ייחודי ל-satcom — אין מקביל בשלושת המצבים
  האחרים):** (1) `_boot_restore` לא נכנס אוטומטית ל-satcom (ר' למעלה); (2)
  כל נקודת כניסה ידנית מה-UI (כרטיס הבית, כפתור ⏻, כפתור ההפעלה בתצוגת SATCOM)
  עוברת דרך `confirmSatcomAntenna()` — דיאלוג חוסם *לפני* השליחה ל-`/api/mode`,
  לא רק הבאנר הקבוע בתצוגה; (3) `airam-satcom.service` הוא היחיד מבין ארבעת
  צרכני ה-SDR עם `StartLimitBurst` סופי (בניגוד ל"מתאושש לנצח" של השאר) — עוצר
  קריסה חוזרת שהייתה מדליקה bias-T ללא פיקוח; `_enter_satcom` קורא
  `systemctl reset-failed` (best-effort, sudoers ממוקד) לפני כל restart כדי
  שכניסה ידנית תמיד תעבוד גם אחרי שהתקרה הופעלה; (4) **`bias_tee=False`
  (הזנת LNA חיצונית)** — ה-RSP1B bias-T מוגבל ל-100mA, ותוספת הצריכה שלו +
  עליית ה-CPU של `inmarsat-sniffer` בו-זמנית עלולה לדחוף ספק כוח שולי (נצפה
  בפועל: power bank נייד מוגבל ל-5V/3A) מעבר לתקרה ולגרום לכיבוי מלא של ה-Pi.
  ‏`POST /api/mode {mode:"satcom", bias_tee:false}` מכבה את bias-T של ה-RSP1B
  (המשתמש מזין את ה-LNA ממקור נפרד) — נשמר ב-`state["satcom_bias_tee"]`
  ונזכר בכניסות הבאות (כמו `satcom_freqs`); checkbox `satcomExternalLna`
  ב-UI חייב להיות מאותחל מה-state **בטעינת הדף**, לא רק בביקור בתצוגת
  SATCOM — `enterSatcom()` נקרא גם מכרטיס הבית וגם מכפתור ⏻ בלי לעבור
  דרך התצוגה. `confirmSatcomAntenna()` מציג טקסט שונה בהתאם (bias-T ידלק /
  ודא הזנה חיצונית ו-bias-T כבוי) — לעולם לא טוען שbias-T ידלק כשהוא לא ידלק.
  ⚠ אחריות המשתמש: לא להזין משני מקורות (RSP1B + חיצוני) בו-זמנית.
  (5) **`skip_c` — הצד השני של אותו תקציב חשמל (CPU במקום זרם).** ‏Alphasat
  מגדיר 12 ערוצים, מהם **6 הם C-channels (OQPSK 8400)**. לפי המקור
  (`options.c`) הם *"rarely carry ACARS"* וחוסכים *"~50% CPU on low-power
  hosts"* — ו-AIR-AM צורך **ACARS בלבד** (`_normalize_satcom` קורא רק
  `isu.acars.*`), כך שהוויתור עליהם כמעט חינם. **ברירת המחדל: דולק**
  (`state["satcom_skip_c"]=True`), כי הפחתת ה-CPU היא קו ההגנה היחיד שעובד
  כשספק הכוח קוטע *לפני* שה-Pi מספיק לרשום undervoltage (ר' להלן).
  ‏`POST /api/mode {mode:"satcom", skip_c:false}` מחזיר את כל 12 הערוצים.
  ⚠ ה-auto-enable של הדגל במקור עטוף ב-`#ifdef HAVE_RTLSDR` ⇒ **ב-SDRplay
  הוא לא נדלק לבד** — חייבים להעביר אותו במפורש, וזה מה ש-`SATCOM_SKIP_C` עושה.
  (6) **`spectrum` — אבחון, לא חיסכון (המתג היחיד מהשלושה שלא נוגע בחשמל).**
  ‏`lock`/`ebno` לבדם מציגים **בדיוק אותו חיווי** ("אין נעילה") עבור אנטנה
  מנותקת, LNA בלי מתח, וכיוון שגוי — שלוש תקלות עם טיפול שונה לגמרי. רצפת
  הרעש ב-`GET /api/satcom/spectrum` היא ההפרדה היחידה: LNA מוזן מקפיץ אותה
  בעשרות dB. **דולק כברירת מחדל** (`state["satcom_spectrum"]=True`) — עלות
  CPU רציפה **אפס** (`web_get_spectrum_by_channel` קורא את מצב הדמודולטור
  הקיים, בלי ring buffer ובלי FFT מתמשך — אומת מ-`web.c`/`main.c`), ובלי
  נעילה המצב חסר ערך ממילא. המחיר האמיתי הוא **אבטחתי**: הדגל מוסיף גם
  `GET /api/tune` (משנה-מצב) לפורט הלא-מאומת — ר' §9, ולכן זה מתג ולא קבוע.
  ⚠ §12 חל כאן בדיוק כמו על `snr`/Eb·No: `mags_db` מוגש **גולמי**, ואין שום
  סף "רצפה תקינה" — אין ערך כזה שנכון לכל התקנה. ה-UI מנסח **הוראת-פעולה**
  ("נתק את ה-LNA וראה אם הרצפה זזה") שהופכת את המשתמש למדידה, במקום ניחוש שלנו.
  ⚠ **חומרה: `bias_tee` של ה-RSP1B נותן 100mA ו-SAWbird+ iO צורך ~180mA
  נומינלית** (מפרט היצרן) — הוא לא נכנס בתקציב. LNA תת-מוזן לא נכשל בצורה
  ברורה, והתסמין זהה ל"לא מכוון"; זה החשוד מספר 1 ל"מעולם לא הייתה נעילה".
  (7) **`gain` — AGC מול gRdB ידני, ו*למה* זה לא מקביל לרווח של הקול.**
  ‏`SATCOM_GAIN` ריק (ברירת מחדל) => `sdrplay_api_AGC_5HZ` עם setpoint
  ‎`-30dBfs`; `--sdrplay-gain=N` => `gRdB=N` (נחתך 20..59), **`LNAstate=0`
  מקובע**, ו-`AGC_DISABLE` (הכול מצוטט מ-`sdrplay.c` ליד `SATCOM_GAIN_DEFAULT`).
  שתי מסקנות: (א) הטווח **זהה ל-IFGR של הקול** (`IFGR_MIN`/`IFGR_MAX`) ובעל
  אותה סמנטיקה הפוכה — לכן משתמשים באותם קבועים ובאותו היפוך-סליידר;
  (ב) **אין `RFGR`/`LNAstate` לשלוט בו כמו בקול** — הכלי מקבע רווח RF מקסימלי,
  ולכן ב-UI יש **סליידר אחד** ולא שניים. זו לא השמטה אלא מה שהכלי חושף.
  **למה זה שימושי דווקא כשאין נעילה:** ה-AGC מכוון ל-setpoint על *כל* האנרגיה
  בחלון, כך שסלולר חזק ליד ה-L-band (בדיוק מה שה-SAW של ה-LNA נועד לחתוך)
  יכול לגרום לו להוריד רווח ולהחניק את נשא הלוויין; רווח ידני עוקף את זה.
  לכן זו **אופציה, לא ברירת מחדל** — AGC נשאר `None`.
  ⚠ `null` ב-`POST /api/mode` הוא **AGC מפורש**, לא "לא נשלח": הזיהוי חייב
  להיות `"gain" in data` ולא `data.get("gain")`, אחרת אין דרך לחזור מרווח
  ידני ל-AGC והמשתמש נתקע בו לנצח.
  **⚠ מגבלת הזיהוי — למה מניעה גוברת על ניטור:** כשספק הכוח (למשל power bank
  עם OCP) קוטע את המתח, הוא עושה זאת *מיידית* ולעיתים נועל עד ניתוק פיזי —
  ‏`vcgencmd` לעולם לא רואה את זה, ודגלי ה-`_ever` מתאפסים בהדלקה הבאה. לכן
  אזהרות המתח ב-UI הן **רשת ביטחון בלבד**, ולא ניתן להסתמך עליהן; מה שבאמת
  מגן זה להוריד את הצריכה מראש (`skip_c`, `bias_tee=False`).
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
  **אותו עיקרון חל על צביעה, לא רק על ערכים:** פאנל הכיוון של SATCOM (`#satcomAimPanel`)
  צובע Eb/No **לפי `lock` בלבד** — לא לפי סף מספרי בשם ("Eb/No טוב/סביר"), כי אין
  סף כזה מתועד במעלה הזרם (`inmarsat-sniffer` עצמו צובע לפי פעילות הודעות, לא סף —
  `web.c`). סף כזה יהיה בדיוק סוג ה-"המצאה" שהעיקרון הזה אוסר — אם תתפתה להוסיף
  אחד, ודא קודם שיש לו מקור מוסמך. **מד השדה המאוחד (§5/§7, `/api/signal`) מיישם
  את אותו עיקרון על פסק-דין, לא רק על ערך:** `_signal_verdict` משווה **רק** מול
  `state["signal_baseline"]` — מדידה שהמשתמש עצמו ביצע (`POST /api/antenna/check
  {calibrate:true}`) — ולעולם לא מול סף dBFS מוחלט שניחשנו. בלי כיול מפורש,
  התוצאה היא `"no_baseline"`, לא ניחוש. ⚠ **`DISCONNECT_DROP_DB`=10dB (הפער מהבסיס
  שנחשב חריג) תועד כאן בעבר כ"תצפית פיזיקלית" — בלי מדידה אחת מאחוריו.** ניתוח
  מהמקור (`docs/antenna-calibration-experiment.md`) מראה שהוא תלוי-תנאים: ב-VHF
  בשטח שקט הירידה הפיזיקלית קטנה מ-10dB גם ברווח קבוע, ותחת AGC — נשא חזק בחלון
  ה-1.536MHz (ATIS) גורם ל-AGC להעלות רווח אחרי הניתוק ולמחוק את הירידה. **זו אותה
  טעות שהסעיף הזה מזהיר מפניה, בכיוון ההפוך: הצגת הנחה כעובדה.** הקבוע נשאר עד
  שהניסוי שם יכריע — לא "מתוקן" לפי ניחוש אחר.
- **"לא ניסינו" ≠ "ניסינו ונכשלנו" — גם זו לא-המצאה, בכיוון ההפוך.** `_libacars_decode`
  (SATCOM+VDL2 מסלול B) מבחין ביניהם: כש-`inmarsat-sniffer`/`libacars` עצמו ניסה
  לפענח יישום מקונן (CPDLC/ADS-C) והחזיר `"err":true` בפנים (גם כש-CRC של המעטפת
  החיצונית תקין — מאומת מקליטת שדה אמיתית: CPDLC עם `crc_ok:true` ברמת המעטפת
  אבל `"cpdlc":{"err":true}` פנימה), `decoded` מקבל הודעה מפורשת ("לא פוענח —
  המפענח החזיר שגיאה") במקום `None` זהה-בדיוק ל"בכלל לא ניסינו". **לא ממציאים
  טקסט-פענוח** (עדיין `None` בפועל, לא ניחוש-תוכן) — אבל *כן* חושפים עובדה אמיתית
  שקיימת במבנה (הניסיון-שנכשל עצמו), כי הסתרתה גם היא סוג של הטעיה (המשתמש
  לא יכול להבדיל "המפענח לא תמך בזה" מ"האיתות היה חלש מדי הפעם").
- **⚠ אותו עיקרון, הפעם בפיצ'ר שלנו: תמלול ATC היה בלתי-נראה כי ארבעה מצבים
  הוצגו זהים.** משתמש דיווח שמעולם לא ראה תמלול — ובדיקה הראתה שהפיצ'ר "עבד"
  אבל **לא היה שום ערוץ שבו הוא יכול לדווח על עצמו**: `/api/activity` החזיר
  `text: null` בלבד, ולכן "whisper לא מותקן", "עדיין בתור", "פוענח ואין דיבור"
  ו-"הפענוח נכשל" נראו כולם כשורה בלי טקסט. בנוסף, ה-worker עשה `return` **ומת**
  כשהבינארי חסר בעלייה, כך שהתקנה מאוחרת לא הורגשה עד restart. **תוקן**: sidecar
  ‏`<file>.mp3.tx.json` עם `state` מפורש (`none` = לא ניסינו · `pending` · `ok` ·
  `empty` = נוסה ואין דיבור · `failed` + `err`), זמינות **נבדקת חיה בכל מחזור**
  ולא פעם אחת, ושורת סטטוס ב-UI שמציגה את פקודת ההתקנה. **הלקח:** "לא ניסינו ≠
  ניסינו ונכשלנו" אינו כלל על *פענוח רדיו* בלבד — הוא חל על כל דיווח-מצב שלנו
  למשתמש. פיצ'ר שאין לו דרך לומר "אני כבוי/חסר/נכשלתי" הוא, מבחינת המשתמש,
  פיצ'ר שלא קיים.
- **⚠ regression בגרסה הראשונה של הסינון עצמו — נמחק לגמרי, לא רק תוקן.**
  ניסיון ראשון לסנן הזיות-whisper-על-רעש (blocklist מילולי) נבדק ע"י ביקורת
  אדוורסרית ונמצא **הוא עצמו** מפר את §12: הנרמול מחק ספרות מהטקסט לפני
  ההשוואה, כך ש-`"Thank you, 385"` (מסירת תדר שגרתית במגדל) ו-`"Okay, 03"`
  (אישור קליטה) סוננו — **והוצגו למשתמש כ"סונן כהזיה"**, טענה שלא הייתה לה שום
  הוכחה. זו לא הייתה המצאת-ערך (§12 הרגיל) אלא **המצאת-פסילה**: לא ניחשנו
  תוכן, ניחשנו שתוכן-אמיתי הוא רעש. **התיקון: הוסר הסינון כליל.** אין
  `WHISPER_NOISE_PHRASES`, אין `_tx_is_noise` — `_transcribe_file` מחזיר את מה
  ש-whisper פלט, בלי שיפוט. הסינון היחיד שנשאר הוא `TX_MIN_SEC` (גודל קובץ —
  עובדה מדידה, לא ניחוש-תוכן). **הלקח:** blocklist מילולי על תוכן קצר וסטנדרטי
  (ATC כמעט כולו כזה) הוא סיכון-גבוה במיוחד — כל מילה חוקית עלולה להיות גם
  התחלה של תשדורת אמיתית.
- **★ הקלטות שמורות: תת-תיקייה (`saved/`), לא מאגר-מצב מקביל לקבצים.**
  גרסה ראשונה ניהלה `starred.json` — רשימת-פטורים ש-`_sweep_recordings` קרא
  כדי לדעת מה לא למחוק. ביקורת אדוורסרית הוכיחה (לא ניחשה) שלוש משפחות-כשל
  מאותה בחירה: (1) **fail-open** — קובץ פגום/לא-קריא הוחזר כ-`{}` (=אין
  שמורות), ו-`_sweep_recordings` **מחק בדיוק את ההקלטות שהמשתמש הגן עליהן**,
  בניגוד ישיר ללוג-האזהרה שהבטיח שהן לא ייפגעו; (2) **מרוץ**: סימון שהצליח
  (`ok:true`) יכול היה להימחק מיד ע"י `_sweep_recordings` שקרא snapshot ישן
  לפני שהכתיבה הספיקה; (3) **read-modify-write בלי נעילה** בשני כיוונים —
  ביטול-סימון שהתבטל מעצמו, ו-20 בקשות מקבילות על מכסה-5 שקיבלו 20 תשובות
  `ok:true` בזמן ש-2 בלבד נשמרו בפועל (מכסה עקיפה + תשובה שקרית). **התיקון:**
  שמורה יושבת פיזית ב-`REC_DIR/saved/`. `glob("*.mp3")` אינו רקורסיבי, כך
  שהפטור מ-`_sweep_recordings` מגיע ב**אפס שורות לוגיקה** — אין רשימה לקרוא,
  אין מה לפגום, אין עם מה להתחרות. `_STAR_LOCK` (יחיד, כמו `_METAR_LOCK`)
  מגן על בדיקת-המכסה+ה-`os.replace` כיחידה אטומית אחת, כך שמקבילות לא עוקפת
  את המכסה. **הלקח:** מאגר-מצב שמשקף "מה נמצא היכן על הדיסק" הוא סיכון
  מובנה — הקובץ *הוא* המצב שלו; ברגע שיש שני מקורות-אמת (מיקום הקובץ + רשומה
  עליו), הם *יתפרדו*, השאלה רק מתי.
- **⚠ retention עמיד ל-symlink/EACCES על קובץ בודד — לא רק על התיקייה כולה.**
  ‏`sorted(glob(...), key=p.stat().st_mtime)` עטוף ב-`try/except OSError: return`
  *סביב כל הקריאה* הוא רגרסיה שקטה: `stat()` שנכשל על קובץ **אחד** (symlink
  שבור, הרשאה) מפיל את כל ה-`sorted` וה-retention **מפסיק לגמרי** בלי שגיאה
  גלויה — הכרטיס מתמלא בשקט. התיקון: ה-`key` עצמו בולע כשל-פר-פריט (`try:
  ...except OSError: return 0.0`), כך שקובץ בעייתי אחד פשוט ממוין כ"הכי ישן"
  ולא מפיל את התהליך. אותו דפוס ב-`_scan_new_recordings`/`_tx_next_target`.
- **⚠ תור בזיכרון לא שורד restart — בדיוק הבאג ש-§12 בא לתקן, שוב.** גרסה
  ראשונה של "תמלל לפי דרישה" שמרה את הבקשה ברשימת-Python (`_TX_QUEUE`).
  ‏`systemctl restart airam-web` (עדכון קוד, קריסה, reboot) איפס אותה, וההקלטה
  חזרה להיראות **בדיוק כמו "לא ניסינו"** — למרות שהמשתמש ביקש וחיכה. התיקון:
  הבקשה נכתבת כ-sidecar `state="pending"` על הדיסק; `_tx_next_target` בוחר
  קודם כל `pending`. שורד restart מהגדרה, כי הוא הקובץ עצמו, לא מבנה-נתונים
  שגר בתהליך.
- **⚠ `decode_failed=True` חייב לחסום גם חילוץ נתונים, לא רק להוסיף הודעת-כישלון
  ל-`decoded` — regression אמיתי שקרה בפועל.** כשהוספנו לראשונה את `decode_failed`
  (הבולט הקודם), חילוץ המיקום מ-ADS-C (`_scan_latlon`) המשיך לרוץ *ללא תלות* בדגל
  הזה — טעות: מבנה שהמפענח עצמו סימן `err:true` יכול עדיין להכיל שדה numeric
  בשם `lat`/`lon` (שריד-מפענוח-חלקי, לא ערך אמיתי), ו-`_scan_latlon` הרקורסיבי
  לא יודע להבדיל. בפועל: הודעת A6 אמיתית (C-GHKX) עם `decoded="לא פוענח"`
  הניבה מיקום שגוי באלפי ק"מ (5.69°N/2.11°E במקום ~45.85°N/‑29.6°W האמיתי,
  מאומת מול ADS-B חיצוני). **הלקח הכללי:** דגל "ניסיון-שנכשל" צריך לחסום *כל*
  חילוץ-נתונים מהמבנה שסומן ככושל, לא רק להישאר כהערת-שוליים לצד ערך שעדיין
  יוצא. תוקן ב-`_normalize_acars`/`_normalize_vdl2`: מיקום מ-ADS-C מותנה כעת
  ב-`not decode_failed`, וכך גם `group="position"` (אחרת כרטיס-שנכשל מסונן
  תחת "📍 מיקום" ב-UI בלי מיקום אמיתי — `group` הוא גם מנגנון סינון, לא רק
  צביעה). **מסקנה מתודולוגית:** "מאומת בקליטת שדה אמיתית" על הודעה *אחת*
  שהצליחה איננה הוכחה שהמנגנון תקין באופן כללי — נדרשת השוואה מול מקור-אמת
  חיצוני (כאן: ADS-B), לא רק "יצא ערך והוא נראה סביר".
- **⚠ אותה מסקנה, פעם שנייה — הפעם כי `decode_failed=False` לבד גם לא הספיק.**
  מיד אחרי התיקון הקודם, משתמש שהצליב את "הוכחת ההצלחה" המקורית (A7-BBB, ר'
  למעלה) מול ADS-B חיצוני גילה שהיא **גם היא** הייתה שגויה — לא הזדמן לתפוס
  אותה קודם כי `decode_failed` היה `False` שם (אין `err` שמתגלה בכלל). הסיבה
  האמיתית: tag ADS-C מספרי (7) מתפרש **הפוך לגמרי** לפי כיוון ההודעה
  (`libacars/adsc.c`, אומת ישירות מהמקור: טבלת-תגיות נפרדת ל-uplink מול
  downlink) — "בקשה" בכיוון אחד, "דיווח מיקום אמיתי" בכיוון השני, בלי שום
  שגיאה מתגלה אם משתמשים בטבלה הלא-נכונה. שני "המיקומים" שנצפו עד כה
  (A7-BBB **וגם** C-GHKX) התבררו כ-uplink — כלומר **אף אחת** מהודעות ה-ADS-C
  שנקלטו לא הוכיחה עדיין מיקום אמיתי; שתיהן היו אותו באג, לא ראיה-בעד ולא
  נגד. תוקן ב-`_structural_dir` (SATCOM)/`direction` (VDL2, כבר זמין) —
  חוסמים מיקום ADS-C כש-uplink, בלי תלות ב-`decode_failed`. **הלקח שחוזר על
  עצמו:** "יצא ערך סביר, בלי שגיאה גלויה" הוא הרף הכי-נמוך שיש להוכחת-נכונות,
  לא הכי-גבוה — צריך גם לבדוק את *הפרוטוקול עצמו* (לא רק את קוד-השגיאה של
  היישום), וגם אז אין תחליף להשוואה מול מקור-אמת חיצוני אמיתי.
- **⚠ "כיוון שגוי" ≠ "איתות חלש" — אותה משפחת-באג בפעם שלישית, הפעם בתור סיבת-
  כישלון מדווחת ולא רק תוכן שגוי.** קליטת SATCOM נוספת (14.08.2026, 12 CPDLC +
  11 ADS-C, כולן uplink, מעטפות ARINC-622 תקינות — `crc_ok=true`) הראתה ש-רוב
  ההודעות הוצגו כ-"לא פוענח — המפענח החזיר שגיאה (כנראה איתות שולי)". התירוץ
  ("כנראה איתות שולי") היה **ניחוש שלנו, לא עובדה** — בדיוק סוג ה"המצאה" ש-§12
  אוסר, רק שהפעם זו הייתה סיבה מוצעת לכישלון ולא ערך מומצא. הסיבה האמיתית:
  ‏`inmarsat-sniffer` פענח את היישום המקונן (CPDLC/ADS-C) ב**כיוון ASN.1 הלא-
  נכון** — ASN.1 PER תלוי-כיוון (‏CPDLC: `FANSATCUplinkMessage` מול
  `FANSATCDownlinkMessage`, מבנים שונים לגמרי; ADS-C: כבר תועד למעלה). **תוקן**:
  ‏`_decode_libacars_app` מריץ re-decode מקומי עם `decode_acars_apps` (כלי ה-CLI
  הרשמי של `libacars`) בכיוון האמיתי מ-`structural_dir`. **ממצא מפתיע באימות
  מהמקור, לא הנחה שקיבלנו מוכנה:** `LA_JSON=1` **לא** מספיק — ה-JSON של libacars
  ל-ADS-C מכיל רק מפתחות מספריים (בלי משפט אחד), ול-CPDLC הטקסט קיים אבל תחת
  `choice_label` ש-`_libacars_decode` לא קוצר (מחפש `text`/`msg`/`message` בלבד).
  **הלקח:** מפרט-פענוח שמניח "דגל אחד מספיק כדי לקבל גם מבנה-מכונה וגם טקסט-
  אנושי" כדאי לבדוק מול קוד-המקור לפני מימוש — לא כל כלי CLI חושף את שניהם
  באותו מסלול פלט. הודעת השגיאה תוקנה בהתאם ("לא פוענח — libacars החזיר שגיאת
  פענוח; הטקסט הגולמי נשמר") — לא מסיקים סיבה שאין לה הוכחה. **ה-invariant
  שחוזר בכל התיקונים הקודמים נשאר בעינו ללא שינוי**: `adsc_dir_ok` ו-`decode_failed`
  עדיין חוסמים מיקום מ-ADS-C uplink, גם אחרי re-decode "מוצלח" — re-decode
  מחליף רק את תוצאת ה-application decode, לעולם לא את הבדיקה המבנית של הכיוון.
- **מיקום — לא רק "לא ממציאים ערך", גם "לא מייחסים ערך אמיתי לישות הלא-נכונה":**
  שני לקחים מקליטת שטח אמיתית (SATCOM, 33 דק'/465 הודעות/84 מטוסים), מעבר
  ל-`_parse_sq`'s "לקח 1.7.1" (כתובת תחנת-קרקע ≠ מיקום מטוס):
  (1) **`_text_latlon` דורש בדיוק התאמת-קואורדינטה *אחת*.** הודעת H1 עם תוכנית
  טיסה (`#M3FPN/.../F:IVAKI,N32558E015065..LUMED,N34200E014420..`) מכילה
  *שרשרת* waypoints בפורמט קומפקטי זהה-מבנית לדיווח-מיקום בודד — לקיחת
  ההתאמה הראשונה (כמו לפני התיקון) מדביקה את נ"צ ה-waypoint הראשון כאילו
  הוא מיקום המטוס. 2+ התאמות ⇒ מסלול, לא דיווח ⇒ `None` (לא ניחוש איזו
  התאמה "האמיתית"). (2) **tail דמוי-כתובת-תחנה (`_UPLINK_HEADER_RE`, אותו
  דפוס בדיוק כמו זיהוי הדר-ניתוב בטקסט) לא מקבל מיקום מנתיבי ה-heuristic**
  (`_text_latlon`/`_parse_label16`) **בכלל** — תחנת-קרקע לא "טסה", ללא קשר
  לכמה התאמות קואורדינטה נמצאו. שני ה-guards עצמאיים (כל אחד תופס את המקרה
  שהשני מפספס): tail תקין עם שרשרת-waypoints עדיין נחסם ע"י (1); tail
  דמוי-תחנה עם דיווח-מיקום-בודד-תקני עדיין נחסם ע"י (2). **תיקון נלווה, לא
  קשור למיקום:** `_H1_SUB_RE`/`_parse_fpn` לא תפסו אף הודעת H1 אחת בקליטת
  ה-SATCOM (12/12) — הפורמט האמיתי הוא `"- #XX..."` (מקף+רווח לפני ה-`#`,
  לא `#` בתחילת הטקסט ממש) ו-`"M3FPN/"` (בלי קו-נטוי פותח, בניגוד ל-VHF
  `"/FPN/"`) — שני הרחבות תוספתיות-בלבד (הפורמט המקורי נשאר תקין), שלא
  נוגעות במיקום כלל (`_parse_h1` תמיד היה string-only, לא lat/lon).
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
- **⚠ ה-AGC של SDRplay לא שולט ב-LNA — ועד v2.26.0 ה-LNA תחת AGC היה תקוע ברווח מרבי.**
  ה-AGC של ה-API מזיז רק `gRdB` (IF) — אין שדה LNA ב-`sdrplay_api_AgcT`
  (`sdrplay_api_control.h:36-45`). בלי שורת `gain` (מצב AGC) אף אחד לא כתב `LNAstate`, ולכן
  הוא נשאר 0 (ברירת המחדל, `sdrplay_api_tuner.h:63`) — רוויה ב-LNA/ADC *לפני* ה-AGC, שאינה
  הפיכה (ב-AM הדיבור *הוא* שינויי העוצמה). `RF_GAIN_DEFAULT=4` לא נאכף שם בכלל. עכשיו
  `rfgain_sel` ב-`device_string` (ר' `_device_string` — כל קישור בשרשרת אומת מהמקור). **אל
  תסיר אותו "כי AGC מטפל ברווח"** — AGC מטפל רק ב-IF. ⚠ 4 הוא הקבוע הקיים, **לא ערך מכויל** —
  🩺 בדיקת ה-RF (PR 2) תכריע לכל מקום. ⚠ **הגירה ב-`load_state`:** state מלפני v2.26.0 (בלי
  ‏`fm_notch`) תחת AGC ⇒ `rf_gain=RF_GAIN_DEFAULT` — הערך שנשמר שם היה ה-RFGR הידני האחרון,
  חסר-השפעה תחת AGC עד אז; בלי ההגירה השדרוג היה מחיל אותו בשקט (עד RFGR=9).
- **⚠ חיווי "עומס יתר" לפי ‎-3dBFS הוסר — ולמה לא להחזיר דומה לו.** `OVERLOAD_DBFS` השווה את
  רמת *הערוץ* מה-stats לסף: אחרי ה-AGC, בתוך bin FFT אחד. הוא לא רואה את ה-ADC/LNA, ולכן שתק
  בדיוק ברוויה שעיוותה את המגדל (ועלול היה להתריע בלי עומס). חיווי עומס בא **רק** מאירועי
  החומרה (`AIRAM_RF`, ה-patch ל-SoapySDRPlay3). **לא ידוע ≠ תקין, גם כאן:** בלי marker, כשהקול
  לא רץ, כשהעוקב לא קורא, כשהצטרף באמצע סשן, **או כשהסשן עוד לא שלח `stream=start`** —
  `overload=None`, וה-UI אומר "לא ידוע" (עם הסיבה), לא ירוק. ⚠ **עוקב מחובר אינו ראיה**: גרסה
  קודמת קבעה `False` ברגע שה-journalctl רץ, ועוקב "עיוור" (מודול לא-מתוקן שזכה ברישום, רמת log,
  הרשאות יומן) היה מציג "אין עומס" ירוק לנצח. `False` רק אחרי `stream=start` — הוכחה בזמן ריצה
  שהמודול המתוקן טעון ושה-log שלו מגיע (ה-marker מוכיח רק *בנייה*).
- **⚠ שורות `AIRAM_RF` עוברות דרך journald של rtl_airband — שמור על הזרם נקי.** rtl_airband
  רץ ב-`-F`, לא `-f`: ה-waterfall של `-f` כותב ל-stdout קודי ESC בלי `\n`, באותו זרם journald
  כמו ה-stderr (‏`rtl_airband.cpp:657-667`), כך שהשורה הייתה נדבקת לגוש זבל. ה-patch רושם ב-
  `SOAPY_SDR_INFO` (ב-WARNING ומעלה SoapySDR עוטף ב-ANSI, ו-`journalctl -o json` בלי `--all`
  מציג `[blob data]`); `SOAPY_SDR_LOG_LEVEL` מעל INFO בסביבת השירות מעלים את הטלמטריה — ה-UI
  יראה "לא ידוע — הדרייבר עוד לא אישר שהוא מדווח" (אין `stream=start`), לא "אין עומס" — אל
  תגדיר אותו. **גבול הסשן הוא `stream=start` ולא שורת האתחול של rtl_airband:** האחרונה עוברת
  ב-syslog (`do_syslog=1`, `rtl_airband.cpp:747,876-878`) ⇒ `/dev/log`, בעוד `AIRAM_RF` עוברות
  ב-stderr ⇒ ה-stream socket; journald לא מבטיח סדר בין שני מקורות. ‏`stream=start` נרשמת
  ב-`activateStream` לפני `sdrplay_api_Init` ⇒ באותו זרם ולפני כל אירוע של הסשן. המפענח משתמש
  ב-`search` לא-מעוגן, ו-`tests/test_rf_contract.py` שולף את מחרוזות הפורמט מה-patch עצמו.
- **בסיס מד השדה מתויג ב-LNA/מסנן — לא משווים בין קצוות-קדמיים.** רצפת הרעש ב-dBFS נמדדת אחרי
  ה-LNA; בסיס מקונפיגורציה אחרת (או ישן, בלי תיוג) ⇒ `no_baseline` + `verdict_reason`, לעולם לא
  השוואה. לכן הבדיקה/הניסוי מודדים ב-LNA/מסנן *השמורים של הקול* (`_probe_frontend`), ושינוי שלהם
  מבטל את הבסיס בגלוי. אחרי שדרוג ל-v2.26.0 כל בסיס קיים דורש כיול מחדש.
- **בידוד מקורות חיצוניים:** כל קריאת רשת (ADS-B, METAR) חייבת לרוץ ב-thread ולהיכשל
  חיננית — **הרדיו לעולם לא תלוי באינטרנט.**
- **כתיבה אטומית + עמידה בניתוק חשמל** לקבצי קונפיג/state (`_atomic_write`) — אסור
  להשאיר קובץ חצי-כתוב. לא רק `tmp`+`rename` (אטומי מול קוראים מקבילים) אלא גם
  `fsync` על קובץ ה-tmp *ועל תיקיית היעד* — בלעדיו הנתונים יכולים לשבת ב-page cache
  בלבד ולהיעלם בכיבוי פתאומי (תרחיש אמיתי בהפעלה מסוללה). `_cleanup_orphan_tmp`
  מנקה קבצי tmp יתומים מכתיבה שנקטעה — **רק בעלייה** (`__main__`), רק קבצים ישנים
  מ-`_TMP_ORPHAN_AGE_SEC`, ובכל הספריות שנכתבות אטומית (`state.json`/קונפיג הקול/
  `/etc/airam`/הקלטות — לא רק שתיים הראשונות). `load_state` מבחין קובץ-חסר (תקין —
  התקנה טרייה) מקובץ-פגום (לוג אזהרה + עותק `.corrupt` לאבחון, **פעם אחת לאירוע**
  דרך flag גלובלי שמתאפס בקריאה תקינה הבאה — לא לכל קריאה, כי הפונקציה נקראת גם
  מראוטים ב-polling תכוף כמו `/api/metrics`).
- **threaded=True חובה** ל-Flask (סטרים ארוך-טווח לא חוסם בקשות).
- **שמע ברקע = MediaSession** (אין API ל"שיחת טלפון" בדפדפן). אל תחפש חלופה.
- **עברית ב-RTL** ב-UI; CSV עם BOM ל-Excel. `_csv_safe` (`_export_response`)
  מנטרל הזרקת נוסחה: תא שמתחיל ב-`=`/`+`/`-`/`@`/Tab/CR (למשל תוכן `text`/`tail`
  שמגיע משידור רדיו — לא נתון סטטי-מהימן) מקבל `'` מוביל לפני שהוא נכתב.

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
