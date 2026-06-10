# AIR-AM ✈️ — האזנה לתדרי תעופה דרך הטלפון

האזנה לתדרי תעופה (Air band, AM, 118–137 MHz) באמצעות **Raspberry Pi 5 + SDRplay RSP1B**,
עם **ממשק וובי לבחירת תדר** (פריסטים + תדר חופשי) שנפתח מכל טלפון — בלי אפליקציה ייעודית
ובלי סיסמאות.

```
  אנטנה ──► SDRplay RSP1B ──► Raspberry Pi 5
                                  │  SDRplay API + SoapySDR + SoapySDRPlay3
                                  ▼
        ┌────────────────┐   כותב conf + restart   ┌──────────────┐
        │  airam-web     │ ───────────────────────►│ rtl_airband  │
        │ (בורר תדרים)   │◄─── בחירת תדר ───────────│ (פענוח AM)   │
        │   :8080        │      מהטלפון              └──────┬───────┘
        └────────────────┘                                 │ סטרים MP3
                                                            ▼
                                                     Icecast2 :8000
                                                            │
                                                   הטלפון (דפדפן / נגן)
```

הטלפון פותח דף אחד (`http://<IP>:8080`), בוחר פריסט או מקליד תדר — וזהו.

---

## למה לא פשוט SDRConnect?

ל-**SDRConnect אין אפליקציית לקוח לטלפון**. מצב ה-"Audio" שלו דורש לקוח דסקטופ (Windows/Mac/Linux),
אז אי אפשר פשוט לפתוח בטלפון ולהאזין. **RTLSDR-Airband + Icecast** בנוי בדיוק למטרה הזו, רץ headless,
ותומך ב-RSP1B דרך **SoapySDR** (`driver=sdrplay`) — השם "RTLSDR" היסטורי בלבד.

---

## דרישות

- **חומרה:** Raspberry Pi 5 (או 4), SDRplay RSP1B, אנטנה ל-VHF (~120 MHz).
- **מערכת:** Raspberry Pi OS 64-bit (Bookworm).
- **רשת:** הטלפון וה-Pi באותה רשת מקומית.

---

## התקנה (פקודה אחת)

הרץ **על ה-Pi**:

```bash
git clone https://github.com/Shahar373/AIR-AM.git
cd AIR-AM
chmod +x install.sh
sudo ./install.sh
```

הסקריפט עושה **הכל אוטומטית**:
1. תלויות מערכת (כולל Python/Flask).
2. **SDRplay API** — מוריד את ה-`.run`, מחלץ אותו ומתקין את הספרייה והשירות (ללא אישור רישיון ידני).
3. בונה `SoapySDRPlay3` ו-`rtl_airband`.
4. מגדיר `Icecast2` — **מאזינים ללא סיסמה** (סיסמת ה-source פנימית וקבועה, לא נחשפת).
5. מתקין את שרת בורר התדרים ושלושה שירותי systemd שעולים אוטומטית בכל אתחול.

בסיום תקבל כתובת: `http://<IP-של-ה-Pi>:8080`

> אם SDRplay יוציאו גרסת API חדשה והקישור ישתנה — עדכן את `SDRPLAY_VER` בראש `install.sh`.

### עדכון גרסה (Pi שכבר מותקן)

```bash
cd ~/AIR-AM
git pull
sudo ./install.sh
```

ההתקנה אידמפוטנטית: מדלגת על מה שכבר מותקן, בונה מחדש את `rtl_airband` רק כשצריך
(ייקח כמה דקות), ובסוף **מפעילה מחדש את כל השירותים** כך שהבינארי, הקוד וההגדרות
החדשים נקלטים מיד — בלי reboot.

---

## שימוש — בורר התדרים 🎛️

פתח בטלפון: **`http://<IP-של-ה-Pi>:8080`**

- **פריסטים:** כפתורים מהירים לתדרי נתב"ג (מגדל, ATIS, גישה, קרקע וכו').
- **תדר חופשי:** הקלד כל תדר (למשל `134.600`) ולחץ "כוונן".
- **אפנון:** AM (תעופה) או NFM.
- **Gain:** ברירת מחדל AGC אוטומטי; אפשר לכבות ולכוונן ידנית.
- **סקוולץ׳ (Squelch):** שלושה מצבים —
  **אוטומטי** (ברירת מחדל, פותח ~9.54dB מעל הרעש, מתאים לרוב התדרים) ·
  **תמיד פתוח** (חובה ל-ATIS ושידור רציף — שם ה-auto נכשל כי אין שקט לכיול) ·
  **ידני** עם סף SNR ב-dB. בחירת הפריסט **ATIS** עוברת אוטומטית ל"תמיד פתוח".

בכל בחירה השרת כותב הגדרות חדשות ומפעיל מחדש את `rtl_airband` (~3 שניות), והנגן מתחבר מחדש לבד.
זה כמו חוגת רדיו: **תדר אחד פעיל בכל רגע** (יש לך RSP1B אחד).

> בדיקה ראשונה: בחר **ATIS 132.5** — הוא משדר ברצף, אז אם נשמע משהו, כל השרשרת עובדת.

---

## תדרי נתב"ג (LLBG) ו-TMA תל-אביב

הפריסטים מבוססים על מאגרים ציבוריים (OurAirports / SkyVector / RadioReference) —
**מומלץ לאמת מול ה-AIP הרשמי** של רת"א
([AD 2.5 LLBG](https://en.caa.gov.il/index.php?option=com_content&view=article&id=414&Itemid=278)).

| שירות | תדר (MHz) | ודאות |
|-------|-----------|-------|
| ATIS | 132.500 | גבוהה |
| Tower (מגדל, ראשי) | 134.600 | גבוהה |
| Tower (משני) | 119.350 | בינונית |
| Ground West (קרקע מערב) | 118.050 | גבוהה |
| Ground East (קרקע מזרח) | 129.200 | גבוהה |
| Clearance Delivery (מסירה) | 121.950 | בינונית-גבוהה |
| Approach / Departure (גישה/המראה) | 120.500 | גבוהה |
| Tel Aviv Control (TMA) | 121.400 | בינונית |

לעריכת רשימת הפריסטים: ערוך את `PRESETS` בקובץ `webtune/app.py` (או `/opt/airam/webtune/app.py` ב-Pi)
ואז `sudo systemctl restart airam-web`.

---

## איך זה עובד מתחת למכסה המנוע

- **`webtune/app.py`** — שרת Flask קטן. בכל `POST /api/tune` הוא כותב `/etc/rtl_airband/airband.conf`
  עם ערוץ יחיד ממורכז על התדר הנבחר (ולכן תמיד בתוך החלון), ומריץ `systemctl restart rtl_airband`.
- **`config/airband.conf`** — קובץ ברירת מחדל לאתחול ראשון (ATIS 132.5). נדרס ע"י הבורר בכל כיוונון.
- **שלושה שירותים:** `sdrplay` (שירות ה-API), `rtl_airband` (הפענוח), `airam-web` (הממשק).

---

## פתרון תקלות

| בעיה | בדיקה / פתרון |
|------|----------------|
| הדף לא נטען | `sudo systemctl status airam-web` · `sudo journalctl -u airam-web -f` |
| המכשיר לא מזוהה | `SoapySDRUtil --probe="driver=sdrplay"` — אמור להראות RSP1B. ודא ש-`systemctl status sdrplay` רץ ושה-USB מחובר. |
| אין סאונד | בחר ATIS 132.5 (משדר ברצף). בדוק `journalctl -u rtl_airband -f`. ודא שיש שידור בתדר. |
| `unknown modulation` / קורס ב-NFM | תמיכת NFM כבויה כברירת מחדל בבנייה של RTLSDR-Airband. תעופה היא AM בלבד — בחר **AM**. כדי שאופציית NFM תעבוד, הרץ שוב `sudo ./install.sh` (יבנה מחדש עם NFM אוטומטית). |
| רעש/עיוות חזק | כבה AGC בממשק והורד Gain (נסה 20–30). |
| הסטרים לא חוזר אחרי כיוונון | זה לוקח ~3 שניות; הדף מנסה שוב לבד. אם לא — לחץ ▶ בנגן. |
| latency גבוה (עיכוב בשמיעה) | שני גורמים: (1) Icecast `burst-size=0` — `install.sh` מבטיח זאת גם אם התג חסר או נמצא בהערה בקובץ ברירת המחדל. (2) rtl_airband מקודד MP3 ב-VBR שצונח ל-~1KB/s בשקט => הדפדפן ממלא את ה-buffer ההתחלתי ~30 שניות; ההתקנה בונה מחדש עם **CBR 48kbps** (זרם קבוע 6KB/s) => הנגן מתחיל תוך שניות. **הרץ `sudo ./install.sh` (עדכון)** כדי ששני התיקונים יחולו. כפתור "סנכרן ל-live" בממשק מחזיר ל-live אם הנגן נסחף מאחור. רצפה מעשית בנגן דפדפן: ~2–5 שניות. |
| ניתקתי וחיברתי את ה-SDR | השירותים מתאוששים לבד (`Restart=always`, ללא StartLimit) + כלל udev מפעיל מחדש בחיבור. התאוששות תוך שניות. |
| התקנת API נכשלה | ודא רשת; אם יצא API חדש, עדכן `SDRPLAY_VER` ב-`install.sh` והרץ שוב. |

---

## אבטחה ו"בלי סיסמאות"

- **מאזינים:** ללא סיסמה (Icecast פתוח להאזנה ברשת המקומית).
- סיסמת ה-source בין `rtl_airband` ל-Icecast היא ערך פנימי קבוע (`airam`) שנקבע אוטומטית — אתה לא נחשף אליו.
- שרת הבורר רץ כ-root וללא אימות, **מיועד לרשת פרטית מהימנה בלבד**. אל תחשוף את הפורטים 8080/8000
  ישירות לאינטרנט. לגישה מבחוץ השתמש ב-VPN (WireGuard / Tailscale).

---

## SDRConnect (אופציונלי)

לחוויית waterfall ויזואלית אפשר להתקין גם **SDRConnect Server** על אותו Pi ולהתחבר מלפטופ —
אך לא בו-זמנית עם `rtl_airband` (RSP אחד = משתמש אחד). ראה [SDRconnect](https://www.sdrplay.com/sdrconnect/).

---

## מקורות

- [RTLSDR-Airband — Wiki](https://github.com/rtl-airband/RTLSDR-Airband/wiki) ·
  [Configuring SoapySDR devices](https://github.com/rtl-airband/RTLSDR-Airband/wiki/Configuring-SoapySDR-devices)
- [SoapySDRPlay3](https://github.com/pothosware/SoapySDRPlay3) ·
  [SDRplay API](https://www.sdrplay.com/api/)
- [install-libsdrplay (שיטת ההתקנה האוטומטית)](https://github.com/sdr-enthusiasts/install-libsdrplay)
