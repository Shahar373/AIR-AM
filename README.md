# AIR-AM ✈️ — האזנה לתדרי תעופה דרך הטלפון

האזנה לתדרי תעופה (Air band, AM, 118–137 MHz) באמצעות **Raspberry Pi 5 + SDRplay RSP1B**,
ושידור האודיו כסטרים אינטרנטי שאפשר לשמוע מ**כל טלפון** דרך VLC או דפדפן רגיל — בלי אפליקציה ייעודית.

```
  אנטנה ──► SDRplay RSP1B ──► Raspberry Pi 5
                                  │
                                  │  SDRplay API + SoapySDR + SoapySDRPlay3
                                  ▼
                              rtl_airband        (פענוח AM של הערוצים)
                                  │  סטרים MP3
                                  ▼
                              Icecast2  :8000
                                  │
        ┌─────────────────────────┼─────────────────────────┐
        ▼                         ▼                         ▼
     הטלפון (VLC)            דפדפן בטלפון              web/index.html
```

---

## למה לא פשוט SDRConnect?

ל-**SDRConnect אין אפליקציית לקוח לטלפון** (אין אנדרואיד/iOS). הוא עובד בארכיטקטורת שרת–לקוח,
וגם מצב ה-"Audio" היעיל שלו דורש את אפליקציית הלקוח לדסקטופ (Windows/Mac/Linux).
כלומר עם SDRConnect לבד **אי אפשר לפתוח בטלפון ולהאזין** — צריך לפחות לפטופ באמצע.

**RTLSDR-Airband + Icecast** הוא הכלי הסטנדרטי בקהילה למטרה הזו: הוא בנוי לניטור תדרי תעופה,
רץ ללא מסך (headless) על ה-Pi, ומשדר סטרים שכל טלפון יכול לשמוע ישירות.

> **"RTLSDR"-Airband תומך ב-RSP1B?** כן. השם היסטורי — מאז גרסה 3 הוא תומך כמעט בכל SDR דרך
> **SoapySDR**. ה-RSP1B עובד דרך התוסף **SoapySDRPlay3** עם `driver=sdrplay` (לא דרך דרייבר ה-rtl-sdr).

---

## דרישות

- **חומרה:** Raspberry Pi 5 (או 4), SDRplay RSP1B, אנטנה מתאימה ל-VHF (לתעופה — אנטנה אנכית/דיפול ל-~120 MHz).
- **מערכת:** Raspberry Pi OS 64-bit (Bookworm) מומלץ.
- **רשת:** הטלפון וה-Pi באותה רשת מקומית (Wi-Fi). לגישה מחוץ לבית — ראה "גישה מרחוק".

---

## התקנה

הרץ **על ה-Pi**:

```bash
git clone https://github.com/Shahar373/AIR-AM.git
cd AIR-AM
chmod +x install.sh
sudo ./install.sh
```

הסקריפט יתקין תלויות, יבנה את `SoapySDRPlay3` ואת `rtl_airband`, יפעיל `Icecast2`, ויתקין שירות systemd.

> **שלב ידני חד-פעמי — SDRplay API:** ה-API של SDRplay הוא קובץ קנייני שמורידים מהאתר הרשמי
> (מאחורי טופס), ולכן הסקריפט לא יכול להוריד אותו לבד. אם הוא חסר, הסקריפט יעצור ויראה לך בדיוק מה לעשות:
> מורידים את *API/HW Driver V3.xx (Linux)* מ-[sdrplay.com/downloads](https://www.sdrplay.com/downloads/),
> מריצים את קובץ ה-`.run`, ואז מריצים שוב את `install.sh`.

---

## הגדרת התדרים

ערוך את `/etc/rtl_airband/airband.conf` (או את `config/airband.conf` בריפו לפני ההתקנה).

### איך מוצאים את התדרים של שדה התעופה שלי?

- אתרים כמו **LiveATC.net**, **RadioReference**, או פרסומי ה-**AIP** של רשות התעופה המקומית.
- תדרים אופייניים לכל שדה: **Tower / Ground / Approach / ATIS / Departure**.
- תדר אוניברסלי לבדיקה: **121.500** (חירום בינלאומי — Guard).

### חוק הזהב — חלון התדרים 🔑

במצב `multichannel`, **כל הערוצים חייבים** להיות בטווח:

```
centerfreq ± (sample_rate / 2)
```

ברירת המחדל: `centerfreq = 120.5`, `sample_rate = 2.56` → חלון **119.22–121.78 MHz**.

- כדי לכסות חלון **רחב יותר** (ה-Pi 5 שלך מסוגל): העלה את `sample_rate` (למשל `6.0` או `8.0`),
  אבל **קודם** בדוק אילו קצבים נתמכים:
  ```bash
  SoapySDRUtil --probe="driver=sdrplay"
  ```
- ה-RSP1B מכסה עד ~10 MHz בו-זמנית. כל הבאנד התעופתי הוא ~19 MHz —
  אז את הקצוות הרחוקים (למשל 118.x מול 130.x) לא ניתן לתפוס יחד במכשיר אחד. בוחרים את הקבוצה הרלוונטית.

### דוגמה לערוץ

```conf
{
  freq = 118.300;            # MHz, חייב להיות בתוך החלון
  modulation = "am";
  # squelch_threshold = -45; # dBFS — הסר הערה כדי לחתוך רעש בין שידורים
  outputs:
  (
    {
      type = "icecast";
      server = "127.0.0.1";
      port = 8000;
      mountpoint = "tower.mp3";   # שם ייחודי לכל ערוץ
      name = "Tower 118.3";
      username = "source";
      password = "CHANGE_ME_SOURCE_PASSWORD";
    }
  );
}
```

לכל ערוץ צריך `mountpoint` ייחודי. הוסף את אותם שמות גם ל-`web/index.html` (מערך `STREAMS`).

---

## Icecast — סיסמאות

ערוך את `/etc/icecast2/icecast.xml`:

- `<source-password>` — **חייב** להתאים ל-`password` שב-`airband.conf`.
- `<admin-password>` — סיסמת ניהול.
- `<hostname>` — כתובת ה-IP של ה-Pi.

ואז: `sudo systemctl restart icecast2`

---

## הפעלה

```bash
sudo systemctl start rtl_airband      # הפעלה
sudo systemctl status rtl_airband     # סטטוס
sudo journalctl -u rtl_airband -f     # לוג חי
```

השירות מוגדר לעלות אוטומטית בכל אתחול.

---

## האזנה מהטלפון 📱

מצא את ה-IP של ה-Pi: `hostname -I`. נניח `192.168.1.50`.

- **הכי פשוט:** פתח בדפדפן/VLC בטלפון:
  ```
  http://192.168.1.50:8000/guard.mp3
  ```
- **דף נוח עם כל הערוצים:** פתח את `web/index.html`. אפשר להגיש אותו מה-Pi:
  ```bash
  cd AIR-AM/web && python3 -m http.server 8080
  ```
  ואז בטלפון: `http://192.168.1.50:8080/`
- **רשימת כל הערוצים הפעילים:** דף הסטטוס של Icecast — `http://192.168.1.50:8000/`

---

## כיוונון ופתרון תקלות

| בעיה | בדיקה / פתרון |
|------|----------------|
| המכשיר לא מזוהה | `SoapySDRUtil --probe="driver=sdrplay"` — אמור להראות RSP1B. ודא ש-`sudo systemctl status sdrplay` רץ. |
| אין סאונד בכלל | בדוק `journalctl -u rtl_airband -f` לשגיאות; ודא שהתדר בתוך החלון; ודא שיש בכלל שידור בתדר. |
| רעש/עיוות | הורד `gain` (נסה 20–30), או הסר את שורת ה-`gain` ל-AGC. |
| שקט בין שידורים מציק | הוסף `squelch_threshold` (למשל `-45`) לערוץ. |
| Icecast לא עולה | סיסמת ה-`source` ב-`icecast.xml` חייבת להתאים ל-`airband.conf`. |
| רוצה חלון רחב יותר | העלה `sample_rate` (בדוק נתמכים עם `SoapySDRUtil --probe`) ועדכן `centerfreq`. |

---

## גישה מרחוק (מחוץ לבית)

האפשרות הבטוחה: **VPN** לרשת הביתית (למשל WireGuard / Tailscale) ואז להתחבר ל-IP המקומי כרגיל.
פתיחת פורט 8000 ישירות לאינטרנט **לא מומלצת** (Icecast ללא הצפנה/הגנה).

---

## SDRConnect (אופציונלי, במקביל)

אם בכל זאת תרצה את חוויית ה-waterfall הוויזואלית, אפשר להתקין גם את **SDRConnect Server** על אותו Pi
ולהתחבר אליו מלפטופ עם אפליקציית הלקוח (לא בו-זמנית עם rtl_airband על אותו מכשיר — RSP אחד = משתמש אחד).
ראה: [SDRconnect — SDRplay](https://www.sdrplay.com/sdrconnect/).

---

## מקורות

- [RTLSDR-Airband — Wiki](https://github.com/rtl-airband/RTLSDR-Airband/wiki)
- [Configuring SoapySDR devices](https://github.com/rtl-airband/RTLSDR-Airband/wiki/Configuring-SoapySDR-devices)
- [SoapySDRPlay3](https://github.com/pothosware/SoapySDRPlay3)
- [SDRplay Downloads (API)](https://www.sdrplay.com/downloads/)
- [SDRconnect](https://www.sdrplay.com/sdrconnect/)
