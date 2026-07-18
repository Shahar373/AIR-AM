# היתכנות ואפיון — מצב `satcom` (Inmarsat L-band ACARS דרך JAERO)

> מסמך היתכנות/אפיון. סטטוס: **הצעה** (טרם מומש). מטרתו לשמש כמקור-אמת לתכנון
> מצב ה-satcom לצד ה-README וה-CLAUDE.md. ראה §11/§13 ב-`CLAUDE.md` למוסכמות הפרויקט.

## 1. הקשר — למה, מה הבעיה, ומה התוצאה הרצויה

AIR-AM היום קולט ACARS **רק ב-VHF** (acarsdec סביב 131MHz + dumpvdl2/VDL2 ב-136.7–137MHz).
חלק מתעבורת ה-ACARS העולמית — בעיקר טיסות מעל אוקיינוסים/אזורים בלי כיסוי VHF — עוברת
**דרך לוויינים** (SATCOM). המצב החדש קולט את התעבורה הזו, וכן דורש חידוד הנחיות אנטנה כי
מדובר בבנד תדרים אחר לגמרי (L-band, ~1.5GHz).

**החלטות עיצוב (מעצבות את כל האפיון):**
1. **טכנולוגיה: Inmarsat Classic Aero דרך JAERO** (ולא Iridium). צר-פס, כיוון לוויין קבוע,
   מתועד היטב, וקריטית — **הפלט הוא ACARS**, כך שהוא זורם דרך `_normalize_acars` הקיים
   בדיוק כמו "מסלול A" של VDL2, וכל הפרסרים (ATIS/OOOI/PDC/label-15/16/H1/ARINC-622),
   ה-roster, המפה והארכיון מתקבלים **בחינם**.
2. **החלפת אנטנה: ידנית** (ולא ממסר RF נשלט-GPIO ולא SDR שני). חוסך חומרה; המחיר הוא
   באנר-הוראה ב-UI במעבר מצב.

**עיקרון מנחה:** `satcom` הוא **צרכן SDR רביעי שווה-מעמד** לצד voice/acars/vdl2 — לא "מצב-על"
ולא שירות מיוחד. VDL2 (הפיצ'ר האחרון שנוסף) הוא תבנית-העתקה מדויקת בכל שכבה.

---

## 2. ⚠️ סיכון #1 — לאמת קודם כול (Phase 0, לפני נגיעה בקוד המערכת)

**JAERO היא אפליקציית Qt GUI.** נקודת אי-הוודאות היחידה בכל הפרויקט היא הרצתה **headless**
על ה-Pi 5 וחיבור הפלט שלה כ-UDP feed בסגנון acarsdec. לפני שנוגעים ב-systemd/UI/`app.py`
צריך להוכיח שרשרת עובדת ידנית (בדומה ל-`adsb.py --selftest` — סקריפט נפרד להרצה ידנית):

```
SoapySDRPlay3 (biasT=on, 1545MHz) → דמודולציה → JAERO (headless תחת xvfb) → ACARS forward (UDP)
```

- JAERO תומכת ב-forwarding של הודעות ACARS מפוענחות ליציאת רשת — זו נקודת החיבור אל `_satcom_listener`.
- אם ל-JAERO אין מסלול CLI/headless נקי, מריצים אותה תחת `xvfb-run`.
- **קריטריון הצלחה ל-Phase 0:** קליטת הודעת ACARS אמיתית אחת לפחות, מאומתת ב-UDP.
  אם זה עובד — כל השאר הוא עבודה שגרתית לפי התבנית הקיימת ובסיכון נמוך.

---

## 3. חומרה (BOM) + הנחיות אנטנה

מדובר ב-**שרשרת RF נפרדת לחלוטין** מה-airband — אין אנטנה אחת שעושה את שתי המשימות טוב.

| רכיב | פירוט | הערה |
|------|--------|------|
| אנטנת L-band | patch או helix, **RHCP (קיטוב מעגלי ימני — חובה)** | helix מודפס תלת-מימד עם backplane, או patch מסחרי. הלוויין משדר RHCP. |
| **LNA (חובה)** | Nooelec **SAWbird+ IO** (ממורכז 1.542GHz, ~30dB, SAW filter מובנה) | מותקן **צמוד לאנטנה**, לא ליד ה-SDR. ה-SAW דוחה שידורי סלולר סמוכים שאחרת ידכאו את המקלט. |
| הזנת LNA | **bias-T של ה-RSP1B** (‎+4.7V @ 100mA) | ודא צריכת LNA ≤100mA; אחרת מזין bias-T חיצוני ב-USB. |
| כבל | RG-400 / LMR-240 (לא RG-58 — מפסידני מדי ב-1.5GHz) | קצר ככל האפשר בין אנטנה ל-LNA. |
| SDR | ה-RSP1B הקיים | מכסה עד 2GHz; 1545MHz בטווח. |

**כיוון (Alphasat, Inmarsat-4A F4 @ ~25°E):** מ-~32°N/35°E (ישראל) — **elevation ~50°,
azimuth ~200° (דרום-דרום-מערב)**. גאו-סטציונרי ⇒ כיוון **חד-פעמי** לשמיים פתוחים לכיוון זה
(גג/חלון דרומי-מערבי). כוונון עדין: סיבוב איטי סביב הנקודה עד ש-JAERO נועל.

**תדרים:** ערוצי C סביב **1545.0–1545.05MHz**, ב-JAERO מתחילים ב-600/1200 bps.

**⚠️ bias-T חייב להיות דולק רק במצב satcom.** בקוד אין גישה ישירה לחומרת SDR — כל הגדרת
gain/hardware נכתבת לקבצי env שהמפענחים קוראים. לכן הדלקת ה-bias-T תיכתב כארגומנט ל-**SoapySDR
source string** של JAERO בקובץ `satcom.env` (למשל `biasT=true` בדרייבר `sdrplay`), ולא באופן
פרוגרמטי. משמעות: במצבי VHF, שבהם `satcom.env` לא בשימוש, ה-bias-T כבוי מעצם העובדה שהמפענחים
האחרים לא מפעילים אותו.

**החלפה ידנית — UX:** ב-`applyMode("satcom")` ה-UI מציג באנר: "נתק את אנטנת ה-airband, חבר את
אנטנת ה-L-band וכוון ללוויין". בעצירה: "החזר את אנטנת ה-airband". הרצף הבטוח: לכבות מצב → להחליף
אנטנה פיזית → הקוד מדליק bias-T במעבר (אסור bias-T דולק כשאנטנת airband מחוברת).

---

## 4. ארכיטקטורה — איך `satcom` נכנס (מיפוי מדויק, file:line)

`satcom` מחקה את VDL2 בכל שכבה. הפלט של JAERO מסונתז ל-dict בסגנון acarsdec ומוזרם דרך
`_normalize_acars` — **בדיוק כמו מסלול A של VDL2** (`app.py:1349-1371`).

### 4.1 backend — `webtune/app.py`
**פונקציות/קבועים חדשים (מודל על מקבילי VDL2):**
- קבועים (אזור `app.py:158-178`): `SATCOM_SERVICE="airam-satcom"`, `SATCOM_ENV_PATH`,
  `SATCOM_UDP_PORT=5558` (יציאה חדשה), `SATCOM_LOG_PATH`, `SATCOM_FREQS_DEFAULT`, gain default.
- `write_satcom_env(freqs, ...)` — מודל על `write_vdl2_env` (`app.py:1578-1597`), כתיבה אטומית
  ל-`/etc/airam/satcom.env` דרך `_atomic_write`. כולל את מחרוזת ה-SoapySDR source (עם `biasT=true`).
- `_enter_satcom(freqs)` — מודל **מדויק** על `_enter_vdl2` (`app.py:1600-1621`): עצירת
  `("rtl_airband", ACARS_SERVICE, VDL2_SERVICE)` → `write_satcom_env` → `_sysctl("restart",
  SATCOM_SERVICE)` → בדיקת returncode → 7×poll ל-`_is_active`. מחזיר `(error, detail)`.
- `_normalize_satcom(m)` — בונה `raw` dict בסגנון acarsdec (label/text/tail/flight/level/noise/
  freq/error) ומזין ל-`_normalize_acars` (`app.py:1031`), כמו `_normalize_vdl2` ב-`1349-1371`.
  **חשוב:** `noise` בקלט ⇒ SNR אמיתי מחושב (JAERO מספק sig/noise, בניגוד ל-acarsdec).
- `_satcom_listener()` — thread על UDP 5558, מודל על `_vdl2_listener` (`app.py:1447-1506`):
  recvfrom→json→`_normalize_satcom`→dedup→`_append_satcom_log`→ring buffer תחת lock.
- log helpers דקים: `_append_satcom_log`/`_trim_satcom_log`/`_load_satcom_history`/`_read_satcom_log`
  מעל הגנריים `_append_jsonl_log`(`:1157`)/`_trim_jsonl_log`(`:1168`)/`_read_jsonl_log`(`:2556`),
  מודל על `app.py:1413-1444`.

**עריכות לרישומים קיימים (הוספת `satcom` לרשימות):**
- `MODE_SERVICE` — `app.py:1671`: הוסף `"satcom": SATCOM_SERVICE`.
- `_live_mode()` tuple — `app.py:1678`: הוסף `"satcom"`.
- `_enter_standby()` — `app.py:1654`: הוסף `SATCOM_SERVICE` ל-tuple הצרכנים שנעצרים.
- `_enter_voice`/`_enter_acars`/`_enter_vdl2` — הוסף `SATCOM_SERVICE` ללולאת עצירת-הפירים בכל אחד.
- `/api/mode` — `app.py:2747` הוסף `"satcom"` ל-whitelist; `app.py:2777-2782` הוסף לענף
  `elif mode in ("acars","vdl2","satcom")` (משתמש בזנב הגנרי `(key, default, wcheck, enter)`).
- `_boot_restore` — `app.py:2905`: הוסף `elif mode == "satcom": err,_ = _enter_satcom(st.get("satcom_freqs"))`.
- `DEFAULT_STATE` — `app.py:309`: הוסף `"satcom_freqs": SATCOM_FREQS_DEFAULT` (מתמזג אוטומטית לקבצי state ישנים).
- `_build_roster()` — `app.py:2692`: הוסף `("satcom", satcom_snapshot)` ללולאת המיזוג (roster חי בכל מצב).
- routes חדשים: `/api/satcom` + `/api/satcom/export` — מודל על `/api/vdl2` (`app.py:2632`) ו-export (`:2661`),
  כולל `?since=`/`?all=1`/`?day=` בחינם.
- הפעלת ה-thread ב-startup — `app.py:2933-2935`: הוסף `_load_satcom_history()` + הרצת `_satcom_listener`.

### 4.2 systemd — `systemd/airam-satcom.service` (חדש)
מודל על `airam-vdl2.service`. שורת המפתח:
`Conflicts=rtl_airband.service airam-acars.service airam-vdl2.service` — Conflicts דו-כיווני,
כך ששורה **אחת** מכסה את כל שלושת הזוגות ו**אין צורך לערוך את שלוש היחידות הקיימות**.
כולל `Requires=`+`PartOf=sdrplay.service`, `EnvironmentFile=-/etc/airam/satcom.env`,
`ExecStartPre=/usr/local/bin/airam-wait-sdrplay`, `User=root`, `Restart=always`,
ו**בלי `[Install]`** (כמו כל צרכני ה-SDR — לא enabled; `airam-web` משחזר באתחול).

### 4.3 config — `config/satcom.env` (חדש)
פורמט EnvironmentFile (לא shell). מפתחות: `SATCOM_FREQS` (~1545MHz), `SATCOM_GAIN`,
`SATCOM_UDP=127.0.0.1:5558`, ומחרוזת ה-Soapy source (`driver=sdrplay,biasT=true`).

### 4.4 UI — `webtune/static/index.html`
מופע רביעי של אותו פקטורי `createDataView` — **אפס CSS כפול**:
- מופע חדש `var satcom = createDataView({prefix:"satcom", mode:"satcom", label:"SATCOM", emptyHint})`
  אחרי `index.html:3432`.
- section חדש `#satcomView` — שכפול מבנה `#vdl2View` (`:1406`), אותן מחלקות `.dl-*`/`.acars-*`.
- כרטיס בית `homeCardSatcom`/`homeGoSatcom`/`homeSatcomSub` — שכפול `homeCardVdl2` (`:1035-1041`).
- כפתור toggle ב-`#modeSeg` — הוסף `data-v="satcom"` (`:981`).
- עריכות: `showView` (`:2327`), `applyMode` (`:2341` — satcom לכל היפוכי-הפירים, ענף הצלחה,
  rollbacks), `renderHome` (`:2517`, מפת cards ב-`:2549`), wiring `$("homeGoSatcom").onclick`
  (`:3525`), `ROSTER_SRC_LABEL` (`:3438`).
- **⚠️ אמוג'י:** 🛰️ כבר תפוס ע"י VDL2. ל-satcom בחר אייקון נבדל (למשל **📶** או **🌐**).
- **MVP: לא scannable.** לא מוסיפים ל-`SCAN_MODE_LABEL` (`:1518`) ולאופציות עורך הסריקה
  (`:2571`) בשלב ראשון — אפשר בהמשך.

### 4.5 install — `install.sh`
- שלב **4d** חדש (מודל על 4c/dumpvdl2 `:227-259`): pin `JAERO_VER`, build signature + marker,
  clone/build. **JAERO תלוי ב-Qt** (qmake) — הוסף deps ל-`apt-get install` (`:30-37`):
  `qtbase5-dev`, `qtmultimedia5-dev`, `libqt5svg5-dev`, וכן `xvfb` להרצה headless.
  libacars (נבנה כבר ב-4b) שמיש ל-JAERO.
- sudoers (`install.sh:322-329`): הוסף 2 שורות — `restart airam-satcom` + `stop airam-satcom`.
- העתקת env (`:342-343`): הוסף העתקת `satcom.env` ל-`/etc/airam/` (`chown` ב-`:344` כבר מכסה).
- העתקת unit (`:389-393`): הוסף `cp systemd/airam-satcom.service`.
- **שורת ה-enable (`:398`) לא משתנה** — צרכני SDR אף פעם לא enabled.

### 4.6 tests — `tests/test_satcom.py` (חדש) + עריכות
- מודל על `test_vdl2.py`: fixture `paths` (monkeypatch `SATCOM_ENV_PATH`/`SATCOM_LOG_PATH`/state),
  `no_sleep`, בדיקות `_normalize_satcom`, `write_satcom_env`, listener+API (monkeypatch `_is_active`→True).
- `test_boot.py`: הוסף `test_boot_restore_satcom_enters_satcom` (מודל `:61-67`) — assert
  `("restart", app.SATCOM_SERVICE) in sysctl_calls`.
- `test_app.py`/`test_scan.py`: עדכן את בדיקות ה-whitelist של המצבים לכלול `satcom`.

---

## 5. גרסאות ותיעוד (מוסכמת הפרויקט §11/§13)
- `VERSION` — MINOR bump (פיצ'ר).
- `CHANGELOG.md` — שורות תחת `[Unreleased]`.
- `README.md` — סעיף מצב satcom + הוראות אנטנה/כיוון.
- `CLAUDE.md` — מצב נוסף ב-`app_mode`, route חדש, נתיב runtime (`satcom.jsonl`, `satcom.env`),
  יחידת systemd, ומוסכמת ה-bias-T.

---

## 6. אימות (end-to-end)
1. **יחידת בדיקות:** `python -m pytest tests/ -v` — הכול ירוק, כולל `test_satcom.py` החדש
   (SDR/systemd ממוקפים, רץ בלי חומרה).
2. **CI:** `bash -n install.sh` נשאר ירוק אחרי שלב 4d/sudoers.
3. **Phase 0 ידני (על ה-Pi):** הרצת שרשרת JAERO-headless, אימות קליטת הודעת ACARS אמיתית ב-UDP 5558.
4. **אינטגרציה ידנית (על ה-Pi):** מעבר `/api/mode {mode:"satcom"}` מהטלפון → הצגת באנר האנטנה →
   החלפה פיזית → JAERO נועל → הודעה מופיעה בכרטיס satcom, ב-roster וב-`/api/satcom` → עצירה חוזרת ל-standby.
5. **boot-restore:** קבע satcom, `reboot`, ודא ש-`_boot_restore` משחזר את המצב.

---

## 7. שלבי ביצוע מוצעים (סדר עבודה)
- **Phase 0 — הוכחת JAERO headless** (סיכון #1). חוסם את כל השאר. סקריפט ידני נפרד.
- **Phase 1 — backend** (`app.py` + `satcom.env` + `airam-satcom.service`) + `test_satcom.py`/`test_boot.py`.
- **Phase 2 — UI** (מופע `createDataView`, כרטיס בית, view, באנר החלפת אנטנה ידנית).
- **Phase 3 — install.sh** (שלב 4d, Qt deps, sudoers, העתקות).
- **Phase 4 — תיעוד/גרסה** (VERSION/CHANGELOG/README/CLAUDE).
