# היתכנות ואפיון — מצב `satcom` (Inmarsat L-band ACARS)

> מסמך היתכנות/אפיון. סטטוס: **הצעה, עם אימות Phase 0 חלקי שבוצע בפועל** (ראה §2).
> טרם מומש בקוד המערכת (`app.py`/UI/systemd/install.sh). מטרתו לשמש כמקור-אמת לתכנון
> מצב ה-satcom לצד ה-README וה-CLAUDE.md. ראה §11/§13 ב-`CLAUDE.md` למוסכמות הפרויקט.

## 1. הקשר — למה, מה הבעיה, ומה התוצאה הרצויה

AIR-AM היום קולט ACARS **רק ב-VHF** (acarsdec סביב 131MHz + dumpvdl2/VDL2 ב-136.7–137MHz).
חלק מתעבורת ה-ACARS העולמית — בעיקר טיסות מעל אוקיינוסים/אזורים בלי כיסוי VHF — עוברת
**דרך לוויינים** (SATCOM). המצב החדש קולט את התעבורה הזו, וכן דורש חידוד הנחיות אנטנה כי
מדובר בבנד תדרים אחר לגמרי (L-band, ~1.5GHz).

**החלטות עיצוב:**
1. **טכנולוגיה: Inmarsat Classic Aero.** צר-פס, כיוון לוויין קבוע, והפלט הוא **ACARS** —
   כך שהוא זורם דרך `_normalize_acars` הקיים בדיוק כמו "מסלול A" של VDL2, וכל הפרסרים
   (ATIS/OOOI/PDC/label-15/16/H1/ARINC-622), ה-roster, המפה והארכיון מתקבלים **בחינם**.
2. **המפענח: `inmarsat-sniffer` (עודכן אחרי Phase 0 — ראה §2), לא JAERO המקורי.**
3. **החלפת אנטנה: ידנית** (ולא ממסר RF נשלט-GPIO ולא SDR שני). חוסך חומרה; המחיר הוא
   באנר-הוראה ב-UI במעבר מצב.

**עיקרון מנחה:** `satcom` הוא **צרכן SDR רביעי שווה-מעמד** לצד voice/acars/vdl2 — לא "מצב-על"
ולא שירות מיוחד. VDL2 (הפיצ'ר האחרון שנוסף) הוא תבנית-העתקה מדויקת בכל שכבה.

---

## 2. Phase 0 — תוצאות אימות (בוצע בפועל, בסביבת ענן ללא חומרת SDR)

הסיכון המקורי שזוהה: **JAERO (jontio/JAERO) היא אפליקציית Qt GUI** — הרצה headless על
Pi דורשת `xvfb` ולא מתועדת, ופורמט הפלט ברשת לא מתועד בפירוט. זה נבדק ונמצא נכון.

**במחקר המשך אותר פרויקט חלופי: [`alphafox02/inmarsat-sniffer`](https://github.com/alphafox02/inmarsat-sniffer)**
— מפענח L-band עצמאי בשפת C, **ללא Qt/GUI בכלל**, שמבוסס על ליבת ה-DSP של JAERO המקורית
(רישיון MIT) אך עטוף כבינארי CLI. זה מבטל את סיכון ה-headless **מהיסוד**.

### מה אומת בפועל בסשן הזה (בלי RSP1B/אנטנה — רק בנייה + הרצה מקומית):
- **Clone + build נקי** של `libacars` (v2.2.1, תג רשמי) ו-`inmarsat-sniffer` (מ-HEAD, ראה
  קיבוע-גרסה למטה) עם toolchain רגיל — `build-essential cmake pkg-config`, **בלי Qt כלל**.
  קונפיגורציית ה-CMake נופלת בחן ללא אף SDR backend מותקן (כמתועד: `find_package` אופציונלי
  לכל אחד מ-SoapySDR/SDRplay/RTL-SDR/HackRF/BladeRF/UHD/Airspy).
- `./inmarsat-sniffer --help` מציג בדיוק את מה שהתועד: `-i sdrplay[-SERIAL]`, `-B/--bias-tee`
  (**דגל ישיר ל-bias-T — לא צריך "האק" של Soapy source string כפי שהונח מקורית!**),
  `--satellite=AF1` (Alphasat), `--udp=HOST:PORT` (JSON, עד 4 יעדים), `--jaero-format` (טקסט
  JAERO פורמט 3, לתאימות לאחור), `--feed` (JSON ל-stdout).
- **הרצה מול קובץ IQ אקראי** (`-f dummy.ci8 --format=ci8 --satellite=AF1 --feed -v`) עלתה
  **נקי מקצה לקצה**: זיהוי טבלת הלוויין (`Alphasat (I-4A F4) EMEA +25.0E`), חישוב תדר-מרכז
  וקצב-דגימה אוטומטיים, בניית channelizer עם 12 ערוצים (בדיוק לפי טבלת הלוויין: P-channel
  MSK 600/1200 baud, Fwd OQPSK 10500 baud, C-channel OQPSK 8400 baud), ואתחול
  `Aero decoder: JAERO/AeroL embedded`. רעש אקראי מטבעו לא הפיק הודעת ACARS (צפוי — אין
  מבנה אות אמיתי), אבל **כל הצנרת עד לפענוח עלתה בלי קריסה**.
- `--list-satellites`: מאושר שקיים `AF1` = `Alphasat (I-4A F4)`, `+25.0E`, `EMEA`, 12 ערוצי
  aero — תואם בדיוק לחישוב הכיוון (§3) שנעשה עבור ישראל.

### ⚠️ פערים שנתפסו בין תיעוד (README, שסוכם ע"י כלי חיצוני) לבין בדיקה ישירה:
- ה-README (כפי שסוכם) טען ש-`F1` הוא "Inmarsat 4-F1, 143.5°E, Pacific"; ההרצה בפועל של
  `--list-satellites` מציגה `I-6 F1`, `+83.5E`, `IOE` — לוויין ומיקום שונים. **מסקנה: אל
  תסמכו על תמצות-README חיצוני; ה-`--help`/`--list-satellites` בפועל הם מקור-האמת.**
  (לא משפיע על AF1/Alphasat, שאומת ישירות ותקין.)

### מה **לא** אומת (דורש Pi + RSP1B + אנטנה אמיתית — לא ניתן בסביבת ענן זו):
- זיהוי דרייבר SDRplay בפועל וקליטת RF חי (ה-build כאן לא כלל את SDRplay API הקנייני —
  code path קיים ב-`find_package(SDRplay)` אך לא הודגם קומפילציה מולו).
- קליטת אות לוויין אמיתי ופענוח הודעת ACARS אמיתית מהאוויר.
- bias-T מזין בפועל את ה-LNA.
- תוכן ה-JSON/UDP מהודעה אמיתית (רק אתחול הצנרת אומת, לא תוצר פענוח אמיתי).

**המשמעות:** רוב סיכון ה-headless/build/CLI כבר סולק. מה שנשאר ל-Phase 0 "האמיתי" על ה-Pi
הוא קצר בהרבה: להתקין SDRplay API (כבר קיים ב-install.sh), לבנות מול `-DSDRplay`, ולהריץ
`inmarsat-sniffer -i sdrplay --satellite=AF1 --udp=127.0.0.1:5558 -v` מול אנטנה אמיתית.

### קיבוע גרסה (ל-install.sh, כשיתבצע Phase 1)
ל-`inmarsat-sniffer` **אין releases/tags רשמיים** (בניגוד ל-dumpvdl2/`DUMPVDL2_VER`) — יש
לקבע ל-**commit SHA** ולא לתג. ה-commit שנבדק ואומת בסשן זה:
```
SATCOM_SNIFFER_COMMIT = 2827b3a0c7cd349783aeee4621096db14f43264a  (2026-06-03)
```
`libacars` **כבר בנוי** ב-install.sh שלב 4b (`libacars ≥2.1.0`) — נבדק ישירות ש-v2.2.1 תואם
ומספיק (`pkg_check_modules(LIBACARS QUIET libacars-2)` מזהה אותו נכון). **אין צורך בבנייה
נוספת של libacars.**

---

## 3. חומרה (BOM) + הנחיות אנטנה

מדובר ב-**שרשרת RF נפרדת לחלוטין** מה-airband — אין אנטנה אחת שעושה את שתי המשימות טוב.

| רכיב | פירוט | הערה |
|------|--------|------|
| אנטנת L-band | patch או helix, **RHCP (קיטוב מעגלי ימני — חובה)** | helix מודפס תלת-מימד עם backplane, או patch מסחרי. הלוויין משדר RHCP. |
| **LNA (חובה)** | Nooelec **SAWbird+ IO** (ממורכז 1.542GHz, ~30dB, SAW filter מובנה) | מותקן **צמוד לאנטנה**, לא ליד ה-SDR. ה-SAW דוחה שידורי סלולר סמוכים שאחרת ידכאו את המקלט. |
| הזנת LNA | **bias-T של ה-RSP1B** (‎+4.7V @ 100mA), מופעל ב-`inmarsat-sniffer` דרך דגל `-B/--bias-tee` | ודא צריכת LNA ≤100mA; אחרת מזין bias-T חיצוני ב-USB. |
| כבל | RG-400 / LMR-240 (לא RG-58 — מפסידני מדי ב-1.5GHz) | קצר ככל האפשר בין אנטנה ל-LNA. |
| SDR | ה-RSP1B הקיים | מכסה עד 2GHz; 1545MHz בטווח. |

**כיוון (Alphasat = `--satellite=AF1`, Inmarsat-4A F4 @ +25.0°E — מאושר ב-`--list-satellites`):**
מ-~32°N/35°E (ישראל) — **elevation ~50°, azimuth ~200° (דרום-דרום-מערב)**. גאו-סטציונרי ⇒
כיוון **חד-פעמי** לשמיים פתוחים לכיוון זה (גג/חלון דרומי-מערבי). כוונון עדין: סיבוב איטי
סביב הנקודה עד שהמפענח נועל (12 ערוצי aero פעילים ב-`-v`).

**תדרים:** ערוצי P-channel סביב **1545.0–1545.13MHz** (600/1200 baud, ACARS).

**⚠️ bias-T חייב להיות דולק רק במצב satcom.** בניגוד להנחה המקורית (מחרוזת Soapy מיוחדת),
`inmarsat-sniffer` חושף דגל CLI ישיר `-B`/`--bias-tee` — כלומר `write_satcom_env` פשוט מוסיף
את הדגל הזה ל-`ExecStart`/env רק כשנכנסים ל-satcom, ולא כותב אותו כלל במצבי VHF.

**החלפה ידנית — UX:** ב-`applyMode("satcom")` ה-UI מציג באנר: "נתק את אנטנת ה-airband, חבר את
אנטנת ה-L-band וכוון ללוויין". בעצירה: "החזר את אנטנת ה-airband". הרצף הבטוח: לכבות מצב → להחליף
אנטנה פיזית → הקוד מדליק bias-T במעבר (אסור bias-T דולק כשאנטנת airband מחוברת).

---

## 4. ארכיטקטורה — איך `satcom` נכנס (מיפוי מדויק, file:line)

`satcom` מחקה את VDL2 בכל שכבה. `inmarsat-sniffer` מוזן `--udp=127.0.0.1:5558` (JSON, שדות
בסגנון JAERO) ומסונתז ל-dict בסגנון acarsdec דרך `_normalize_acars` — **בדיוק כמו מסלול A
של VDL2** (`app.py:1349-1371`).

### 4.1 backend — `webtune/app.py`
**פונקציות/קבועים חדשים (מודל על מקבילי VDL2):**
- קבועים (אזור `app.py:158-178`): `SATCOM_SERVICE="airam-satcom"`, `SATCOM_ENV_PATH`,
  `SATCOM_UDP_PORT=5558` (יציאה חדשה), `SATCOM_LOG_PATH`, `SATCOM_FREQS_DEFAULT`, gain default.
- `write_satcom_env(freqs, ...)` — מודל על `write_vdl2_env` (`app.py:1578-1597`), כתיבה אטומית
  ל-`/etc/airam/satcom.env` דרך `_atomic_write`. מפתחות: `SATCOM_UDP=127.0.0.1:5558`,
  `SATCOM_BIAS_TEE=1` (מתורגם ל-`-B` ב-`ExecStart`), `SATCOM_GAIN` (`--soapy-gain`).
- `_enter_satcom(freqs)` — מודל **מדויק** על `_enter_vdl2` (`app.py:1600-1621`): עצירת
  `("rtl_airband", ACARS_SERVICE, VDL2_SERVICE)` → `write_satcom_env` → `_sysctl("restart",
  SATCOM_SERVICE)` → בדיקת returncode → 7×poll ל-`_is_active`. מחזיר `(error, detail)`.
- `_normalize_satcom(m)` — בונה `raw` dict בסגנון acarsdec מתוך שדות ה-JSON של
  `inmarsat-sniffer` (`icao_hex`→`icao`, `registration`→`tail`, `acars_label`→`label`,
  `text_body`→`text`, `signal_quality`→`level`, `latitude`/`longitude` — כבר מפוענחים ע"י
  הכלי עצמו, לא רק חילוץ-טקסט) ומזין ל-`_normalize_acars` (`app.py:1031`), כמו
  `_normalize_vdl2` ב-`1349-1371`. **הערה:** יש לאמת את סכמת ה-JSON המדויקת מול פלט אמיתי
  (ה-README מתאר שדות לדוגמה, אך ראו §2 — לא לסמוך על README בלי אימות ישיר; להריץ עם
  `--feed -v` ולבדוק שורת JSON אמיתית לפני מימוש `_normalize_satcom`).
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
`ExecStart=/usr/local/bin/inmarsat-sniffer -i sdrplay --satellite=AF1 --udp=127.0.0.1:5558 ...`
(דגלים נוספים דרך `EnvironmentFile`). כולל `Requires=`+`PartOf=sdrplay.service`,
`EnvironmentFile=-/etc/airam/satcom.env`, `ExecStartPre=/usr/local/bin/airam-wait-sdrplay`,
`User=root`, `Restart=always`, ו**בלי `[Install]`** (כמו כל צרכני ה-SDR — לא enabled;
`airam-web` משחזר באתחול).

### 4.3 config — `config/satcom.env` (חדש)
פורמט EnvironmentFile (לא shell). מפתחות: `SATCOM_UDP=127.0.0.1:5558`, `SATCOM_BIAS_TEE`,
`SATCOM_GAIN` (`--soapy-gain`).

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
- שלב **4d** חדש (מודל על 4c/dumpvdl2 `:227-259`), **פשוט משמעותית מהתכנון המקורי** — אין
  Qt, אין xvfb: `apt-get install build-essential cmake pkg-config` (כבר קיימים ברובם),
  clone ל-commit מקובע (`SATCOM_SNIFFER_COMMIT`, ראה §2), `cmake .. && make -j && make install`.
  `libacars` **כבר בנוי** ב-4b — אין דבר נוסף לבנות בשבילו.
- sudoers (`install.sh:322-329`): הוסף 2 שורות — `restart airam-satcom` + `stop airam-satcom`.
- העתקת env (`:342-343`): הוסף העתקת `satcom.env` ל-`/etc/airam/` (`chown` ב-`:344` כבר מכסה).
- העתקת unit (`:389-393`): הוסף `cp systemd/airam-satcom.service`.
- **שורת ה-enable (`:398`) לא משתנה** — צרכני SDR אף פעם לא enabled.

### 4.6 tests — `tests/test_satcom.py` (חדש) + עריכות
- מודל על `test_vdl2.py`: fixture `paths` (monkeypatch `SATCOM_ENV_PATH`/`SATCOM_LOG_PATH`/state),
  `no_sleep`, בדיקות `_normalize_satcom` (מבוסס על שורת JSON אמיתית מ-`inmarsat-sniffer --feed`,
  לא על README), `write_satcom_env`, listener+API (monkeypatch `_is_active`→True).
- `test_boot.py`: הוסף `test_boot_restore_satcom_enters_satcom` (מודל `:61-67`) — assert
  `("restart", app.SATCOM_SERVICE) in sysctl_calls`.
- `test_app.py`/`test_scan.py`: עדכן את בדיקות ה-whitelist של המצבים לכלול `satcom`.

---

## 5. גרסאות ותיעוד (מוסכמת הפרויקט §11/§13)
- `VERSION` — MINOR bump (פיצ'ר).
- `CHANGELOG.md` — שורות תחת `[Unreleased]`.
- `README.md` — סעיף מצב satcom + הוראות אנטנה/כיוון.
- `CLAUDE.md` — מצב נוסף ב-`app_mode`, route חדש, נתיב runtime (`satcom.jsonl`, `satcom.env`),
  יחידת systemd, ומוסכמת ה-bias-T (`-B` דרך `inmarsat-sniffer`, לא Soapy source string).

---

## 6. אימות (end-to-end)
1. **יחידת בדיקות:** `python -m pytest tests/ -v` — הכול ירוק, כולל `test_satcom.py` החדש
   (SDR/systemd ממוקפים, רץ בלי חומרה).
2. **CI:** `bash -n install.sh` נשאר ירוק אחרי שלב 4d/sudoers.
3. **Phase 0 שנותר על ה-Pi (הקצר בהרבה, ראה §2):** בנייה מול `-DSDRplay` אמיתי, הרצה
   `inmarsat-sniffer -i sdrplay --satellite=AF1 --udp=127.0.0.1:5558 -v -B` מול אנטנה
   אמיתית מכוונת ל-Alphasat, אימות הודעת ACARS אמיתית ב-UDP.
4. **אינטגרציה ידנית (על ה-Pi):** מעבר `/api/mode {mode:"satcom"}` מהטלפון → הצגת באנר האנטנה →
   החלפה פיזית → מפענח נועל → הודעה מופיעה בכרטיס satcom, ב-roster וב-`/api/satcom` → עצירה
   חוזרת ל-standby.
5. **boot-restore:** קבע satcom, `reboot`, ודא ש-`_boot_restore` משחזר את המצב.

---

## 7. שלבי ביצוע מוצעים (סדר עבודה)
- ~~**Phase 0 — הוכחת headless**~~ **בוצע חלקית (§2).** נותר רק אימות מול SDRplay API +
  אנטנה אמיתית על ה-Pi עצמו — לא ניתן לביצוע בסביבת ענן זו.
- **Phase 1 — backend** (`app.py` + `satcom.env` + `airam-satcom.service`) + `test_satcom.py`/`test_boot.py`.
  לפני מימוש `_normalize_satcom`: להריץ `inmarsat-sniffer --feed -v` על ה-Pi ולתעד שורת JSON
  אמיתית אחת (ראו האזהרה ב-§4.1 — אל תסמכו על סכמת ה-README בלבד).
- ~~**Phase 2 — UI**~~ **בוצע** (מופע `createDataView` רביעי, כרטיס בית, view,
  בורר-לוויין, באנר החלפת אנטנה ידנית) — אומת בדפדפן (Chromium), בלי שגיאות JS.
- **Phase 3 — install.sh** (שלב 4d — קצר, ללא Qt/xvfb — sudoers, העתקות).
- **Phase 4 — תיעוד/גרסה** (VERSION/CHANGELOG/README/CLAUDE).
