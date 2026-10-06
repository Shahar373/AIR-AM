#!/usr/bin/env python3
# ============================================================================
#  AIR-AM  -  airam_launch.py: מפעיל צרכני ה-SDR (רץ כ-root, PR 6 · v2.30.0)
# ----------------------------------------------------------------------------
#  ארבע יחידות ה-systemd של צרכני ה-SDR (rtl_airband / airam-acars / airam-vdl2 /
#  airam-satcom) מריצות *אותו* קובץ:
#      /usr/bin/python3 -I /opt/airam/webtune/airam_launch.py {voice|acars|vdl2|satcom}
#
#  למה: airam-web (משתמש airam, לא root) כותב את בחירת המשתמש ל-/etc/rtl_airband/
#  airband.conf ול-/etc/airam/{acars,vdl2,satcom}.env — קבצים ש-airam שולט בהם. עד
#  v2.30.0 הם נטענו ישירות לתהליך root: ה-env כ-EnvironmentFile (systemd מייצא *כל*
#  מפתח — LD_PRELOAD / SOAPY_SDR_PLUGIN_PATH ⇒ קוד כ-root), וה-airband.conf כקונפיג של
#  rtl_airband (stats_filepath / directory של פלט קובץ ⇒ כתיבה כ-root לכל נתיב).
#  עכשיו root קורא אותם **כנתונים בלבד**: כל ערך נבדק מול תבנית/טווח/רשימה סגורה,
#  ה-argv נבנה כאן מקבועים (נתיבים, פורטים, דגלים), קונפיג הקול מרונדר מחדש מהמספרים
#  לתיקייה של root (/run/airam-voice), והמפענח מקבל סביבה נקייה (execve — אותו PID).
#  קלט לא תקין ⇒ יציאה 78 (EX_CONFIG; RestartPreventExitStatus ביחידות) עם *קוד* בלבד —
#  תוכן הקובץ לעולם לא מודפס (airam קורא את היומן — קובץ שהוחלף ב-hardlink לא ידלוף).
#
#  ⚠ ספריית תקן בלבד, בלי תופעות-לוואי ב-import: app.py מייבא מכאן את קבועי הקול ואת
#  render_config (מקור-אמת יחיד לקונפיג). ⚠ אל תוסיף כאן תלות בקובץ שכן — `python3 -I`
#  לא מוסיף את תיקיית הסקריפט ל-sys.path. הקובץ root:root 0644 (install.sh).
# ============================================================================
import os
import re
import stat
import sys
from pathlib import Path

# --- קבועי הקול (מיובאים ע"י app.py) ---------------------------------------
MOUNT = "live.mp3"          # שם ה-stream הקבוע ב-Icecast
ICECAST_PORT = 8000
SOURCE_PW = "airam"         # חייבת להיות זהה ל-SOURCE_PW ב-install.sh (נכתבת ל-Icecast שם)
SAMPLE_RATE = 2.56          # Msps - ערוץ יחיד, חלון צר מספיק
# MHz — מזיזים את centerfreq מהתדר כדי להתרחק מ-spike ה-DC. ⚠ **לא 0.3 עגול** (v2.28.0):
# rtl_airband v5.2.0 בוחר את ה-bin ב-ceil(x−1) (config.cpp:670), כש-x = (freq+rate−center)/
# (rate/fft_size). ‏0.3MHz = בדיוק 60 bins של 5kHz ⇒ x שלם ⇒ נבחר bin אחד *מתחת* — כל ערוץ
# נדגם 5kHz מתחת לתדר (סימולציה מלאה: עיוות גבוה יותר, ודחיית הערוץ השכן מלמטה ‎-34dB
# במקום ‎-57dB). ב-0.2999 ‏x≈452.02 ⇒ ה-bin הנכון (452), 100Hz ממרכזו — מרווח שבולע את
# קיטום (int)(MHz·1e6) של שני הערכים (parse_anynum2int, config.cpp:298-310). אל "תעגל" ל-0.3.
DC_OFFSET = 0.2999
CHANNEL_BW_NARROW = 7000    # Hz — `bandwidth` לערוץ (מסנן Bessel מסדר 2 ב-bw/2, config.cpp:595-618)
AUDIO_LOWPASS_DEFAULT = 2500   # Hz — ברירת המחדל של rtl_airband (config.cpp:328)
AUDIO_LOWPASS_OPTIONS = (2500, 3000)
# רווח SDRplay (מודל legacy של SoapySDRPlay3): שני אלמנטים נפרדים, וקטן יותר = רווח גדול יותר.
#   IFGR - הפחתת רווח בתדר הביניים, 20–59 dB.
#   RFGR - מצב ה-LNA (הפחתת רווח RF), 0–9 (לא-לינארי, ~7dB לצעד).
# כש-AGC כבוי כותבים gain = "IFGR=..,RFGR=.."; כש-AGC דלוק משמיטים את gain => AGC חומרתי.
IFGR_MIN, IFGR_MAX = 20, 59
RFGR_MIN, RFGR_MAX = 0, 9
IF_GAIN_DEFAULT = 40            # IFGR - אמצע הטווח, בטוח מפני עומס יתר
# RFGR - מצב LNA בינוני. ⚠ עד v2.26.0 הקבוע הזה **לא נאכף במצב AGC** (בלי שורת
# gain הדרייבר השאיר LNAstate=0 — רווח RF מקסימלי, ר' render_config). מאז
# הוא נכתב גם תחת AGC (‎rfgain_sel ב-device_string) — docs/voice-rf-quality-plan.md §2.2/1.
RF_GAIN_DEFAULT = 4
# ⚠ הוסר: OVERLOAD_DBFS (‎-3dBFS על רמת *הערוץ* מה-stats). הערוץ נמדד אחרי
# ה-AGC ובתוך bin אחד — הוא לא רואה את ה-ADC/LNA, ולכן שתק בעומס אמיתי
# (docs/voice-rf-quality-plan.md §2.2/3). חיווי העומס מגיע עכשיו מאירועי
# החומרה עצמם (‎AIRAM_RF, ר' "טלמטריית RF מהחומרה" למטה), או "לא ידוע".
SQUELCH_MODES = {"auto", "open", "manual"}
SNR_MIN, SNR_MAX = 0.0, 60.0   # dB - תחום clamp ל-SNR ידני
SNR_DEFAULT = 9.0              # ≈ סף ה-auto הפנימי של rtl_airband (~9.54 dB)
STATS_PATH = Path("/run/rtl_airband_stats.txt")   # tmpfs - בלי שחיקת SD

# הקלטות: rtl_airband כותב קובץ MP3 לכל שידור (split_on_transmission) בשם
# <REC_BASENAME>_YYYYMMDD_HHMMSS_<Hz>.mp3 (.tmp בזמן כתיבה, rename בסגירה
# ~0.5ש' אחרי שהסקוולץ' נסגר). קובץ שהסתיים = אירוע ביומן השידורים.
REC_DIR = Path("/var/lib/airam/recordings")
REC_BASENAME = "airam"         # filename_template ב-config וגם עוגן הפרסור של השמות


# --- שורת ה-squelch: מקור אמת יחיד -----------------------------------------
def _squelch_line(squelch_mode, squelch_snr):
    """מחזיר את שורת ה-squelch (או None) לכל מצב. שנה כאן בלבד.
      auto   -> None  (ללא שורה => squelch אוטומטי, ~9.54 dB מעל הרעש)
      open   -> תמיד פתוח (ל-ATIS / שידור רציף)
      manual -> סף SNR ידני ב-dB
    תמיד squelch_snr_threshold (לא dBFS) => בלתי תלוי ב-gain/AGC, ואף פעם לא שני
    הפרמטרים יחד.
    """
    if squelch_mode == "manual":
        return f"        squelch_snr_threshold = {float(squelch_snr):.1f};"
    if squelch_mode == "open":
        return "        squelch_snr_threshold = 0;"   # 0 = תמיד פתוח
    return None  # auto


# --- בניית קובץ ההגדרות ל-rtl_airband ------------------------------------
def _device_string(agc, rf_gain, fm_notch):
    """ה-device_string של SoapySDR — מקור-אמת יחיד (גם _parse_airband_conf/_config_stale
    נשענים על הצורה שלו).
    ⚠ למה מפתחות נוספים ב-device_string *מגיעים* לדרייבר (מאומת מהמקור, לא הנחה):
      1. rtl_airband v5.2.0 מעביר את המחרוזת כמות שהיא ל-SoapySDRDevice_makeStrArgs —
         ‏input-soapysdr.cpp:196 (בדיקת יכולות, ואז unmake) ו-:220 (הפתיחה האמיתית).
      2. SoapySDR (Factory.cpp:154-157) ממזג את כל ה-kwargs של הקלט מעל תוצאת
         ה-enumerate ‏(hybridArgs) ומעביר אותם ל-make של הדרייבר (:177). ה-find של
         sdrplay מסנן רק לפי serial/mode (Registration.cpp:55,100) — מפתחות אחרים
         לא מפילים את ההתאמה.
      3. הבנאי של SoapySDRPlay3 (Settings.cpp:105-114) קורא writeSetting(key, value)
         לכל kwarg שאינו driver/label/mode/serial/soapy — בכל אחת משתי הפתיחות של
         rtl_airband, כך שגם המכשיר שבאמת מזרים מקבל אותם.
      4. writeSetting: ‏"rfgain_sel" => tunerParams.gain.LNAstate (Settings.cpp:1628-1630,
         תחת RF_GAIN_IN_MENU — ON כברירת מחדל ב-CMakeLists.txt:36); "rfnotch_ctrl" ל-RSP1B
         => rsp1aParams.rfNotchEnable (Settings.cpp:1745-1748,1777-1783; "false"=>0, כל
         ערך אחר=>1).
    ⚠ ולמה ה-LNA לא נדרס אחר כך תחת AGC: LNAstate נכתב **רק** ב-writeSetting
    (rfgain_sel) וב-setGain("RFGR") (Settings.cpp:597-601). rtl_airband קורא
    setGainMode(agc) תמיד, אבל setGain/setGainElement **רק כש-AGC כבוי**
    (input-soapysdr.cpp:247-270); setGainMode עצמו נוגע רק ב-agc.enable
    (Settings.cpp:553-566). ה-AGC של ה-API פועל על gRdB בלבד — אין שדה LNA ב-
    sdrplay_api_AgcT (sdrplay_api_control.h:36-45). ‏selectDevice() החוזר (מ-getSettingInfo)
    בוחר מחדש רק כשמכשיר *אחר* נבחר באותו תהליך (Settings.cpp:2031-2038) — לא אצלנו
    (RSP1B יחיד). לכן עד v2.26.0 (בלי rfgain_sel)
    ה-AGC רץ עם LNAstate=0 של ברירת-המחדל (sdrplay_api_tuner.h:63) — רווח RF מרבי
    ורוויה *לפני* ה-AGC (docs/voice-rf-quality-plan.md §2.2/1).
    ⚠ ברווח ידני לא מוסיפים rfgain_sel: ה-LNA נקבע שם ע"י RFGR בשורת gain
    (setGainElement אחרי הבנאי) — שני מקורות לאותו ערך היו מזמינים סתירה."""
    parts = ["driver=sdrplay", f"rfnotch_ctrl={'true' if fm_notch else 'false'}"]
    if agc:
        parts.append(f"rfgain_sel={int(rf_gain)}")
    return ",".join(parts)


def render_config(freq, mod, agc, if_gain, rf_gain, squelch_mode="auto", squelch_snr=SNR_DEFAULT,
                  fm_notch=False, narrow=False, lowpass=AUDIO_LOWPASS_DEFAULT):
    # מעגלים *פעם אחת* ובונים את שני הערכים מאותו מספר: עיגול נפרד של freq ושל freq+DC_OFFSET
    # בתדר חופשי עם 5 ספרות (למשל 132.28125) נתן הפרש 0.3000 — שוב ה-bin הלא-נכון
    f = round(float(freq), 4)
    lines = [
        "# נוצר אוטומטית ע\"י AIR-AM web tuner. שינויים ידניים נדרסים בכל כיוונון.",
        "localtime = true;   # חותמות הזמן בשמות קובצי ההקלטה בזמן מקומי",
        f'stats_filepath = "{STATS_PATH}";   # מדדי RF (signal/noise) ל-/api/metrics',
        "devices:",
        "(",
        "  {",
        '    type = "soapysdr";',
        f'    device_string = "{_device_string(agc, rf_gain, fm_notch)}";',
    ]
    if not agc:
        # רווח ידני => שני אלמנטים. הגדרת gain מבטלת אוטומטית את ה-AGC בדרייבר.
        lines.append(f'    gain = "IFGR={int(if_gain)},RFGR={int(rf_gain)}";')  # אחרת AGC אוטומטי
    lines += [
        f"    sample_rate = {SAMPLE_RATE};",
        '    mode = "multichannel";',
        f"    centerfreq = {f + DC_OFFSET:.4f};",   # מוסט מהערוץ כדי להימנע מ-spike ה-DC
        "    channels:",
        "    (",
        "      {",
        f"        freq = {f:.4f};",
        f'        modulation = "{mod}";',
    ]
    if narrow and mod == "am":
        # רק ב-AM: ב-NFM מסנן ב-3.5kHz לפני הדיסקרימינטור היה חותך את הסטייה ומעוות
        # מסנן ערוץ לפני גלאי המעטפה — דוחה ערוצים צמודים (25/8.33kHz) שה-bin הרחב
        # (‎-3dB ב-±6.2kHz) מעביר; לא משפר SNR בתוך הערוץ. כבוי כברירת מחדל עד A/B בשטח.
        lines.append(f"        bandwidth = {CHANNEL_BW_NARROW};")
    if int(lowpass) != AUDIO_LOWPASS_DEFAULT:
        lines.append(f"        lowpass = {int(lowpass)};")   # רוחב השמע (LAME) — ברירת המחדל לא נכתבת
    sq = _squelch_line(squelch_mode, squelch_snr)
    if sq is not None:
        lines.append(sq)
    record = squelch_mode != "open"   # "פתוח" (ATIS) => הסקוולץ' לא נסגר לעולם
    lines += [
        "        outputs:",
        "        (",
        "          {",
        '            type = "icecast";',
        '            server = "127.0.0.1";',
        f"            port = {ICECAST_PORT};",
        f'            mountpoint = "{MOUNT}";',
        f'            name = "AIR-AM {f:.3f}";',
        '            username = "source";',
        f'            password = "{SOURCE_PW}";',
        "          }" + ("," if record else ""),
    ]
    if record:
        lines += [
            "          {",
            '            type = "file";',
            f'            directory = "{REC_DIR}";',
            f'            filename_template = "{REC_BASENAME}";',
            "            split_on_transmission = true;   # קובץ MP3 נפרד לכל שידור",
            "            include_freq = true;            # התדר (Hz) בשם הקובץ",
            "          }",
        ]
    lines += [
        "        );",
        "      }",
        "    );",
        "  }",
        ");",
        "",
    ]
    return "\n".join(lines)

# --- המפעיל -----------------------------------------------------------------
EX_USAGE = 64
EX_CONFIG = 78            # RestartPreventExitStatus=78 ביחידות: קלט פסול ≠ קריסה שכדאי לחזור עליה
MAX_INPUT_BYTES = 16384   # הקבצים שלנו ~0.3–3KB (כולל הערות ה-seed)

VOICE_CONF_IN = Path("/etc/rtl_airband/airband.conf")    # נכתב ע"י airam-web (airam)
VOICE_CONF_DIR = Path("/run/airam-voice")                # RuntimeDirectory של rtl_airband (root, 0755)
VOICE_CONF_OUT = VOICE_CONF_DIR / "airband.conf"         # מה ש-rtl_airband באמת קורא
ENV_PATHS = {"acars": Path("/etc/airam/acars.env"),
             "vdl2": Path("/etc/airam/vdl2.env"),
             "satcom": Path("/etc/airam/satcom.env")}
BIN = {"voice": "/usr/local/bin/rtl_airband",
       "acars": "/usr/local/bin/acarsdec",
       "vdl2": "/usr/local/bin/dumpvdl2",
       "satcom": "/usr/local/bin/inmarsat-sniffer"}

# ערכים שהיו "משתנים" ב-env אבל app.py תמיד כותב אותם קבועים — נעוצים כאן (לא נלקחים מהקובץ,
# רק נבדקים שהם בדיוק אלה): יעד ה-UDP, מסנן ההודעות, פורט האבחון.
ACARS_UDP = "127.0.0.1:5556"       # = ACARS_UDP_HOST:ACARS_UDP_PORT ב-app.py (נבדק)
VDL2_UDP_PORT = 5557               # = VDL2_UDP_PORT ב-app.py
VDL2_MSG_FILTER = "all,-avlc_s,-acars_nodata,-gsif,-x25_control,-idrp_keepalive,-esis"
SATCOM_UDP = "127.0.0.1:5558"      # = SATCOM_UDP_PORT ב-app.py
SATCOM_WEB_PORT = "8888"           # = SATCOM_WEB_PORT ב-app.py
SATCOM_SATELLITES = ("AF1", "4F3", "3F5", "F1")   # = SATCOM_BANKS ב-app.py (נבדק)
MAX_CHANNELS = 8

# ⚠ re.ASCII בכל תבנית: בלעדיו ‎\d תופס גם ספרות לא-לטיניות (١٣١.٥٥٠), שהיו עוברות כמות שהן
FREQ_MHZ_RE = re.compile(r"\d{2,3}\.\d{1,3}", re.ASCII)   # = _FREQ_RE ב-app.py (נבדק)
FREQ_HZ_RE = re.compile(r"\d{7,9}", re.ASCII)             # VDL2 ב-Hz (136975000)
INT_RE = re.compile(r"-?\d{1,4}", re.ASCII)
VDL2_GAIN_RE = re.compile(r"--soapy-gain IFGR=(\d{2}),RFGR=(\d)", re.ASCII)
SATCOM_GAIN_RE = re.compile(r"--sdrplay-gain=(\d{2})", re.ASCII)
ENV_KEY_RE = re.compile(r"[A-Z][A-Z0-9_]{0,31}", re.ASCII)

# מפתחות לכל מצב: (חובה, רשות). מפתח אחר כלשהו (LD_PRELOAD...) ⇒ סירוב — לא "מתעלמים".
ENV_KEYS = {
    "acars": ({"ACARS_FREQS", "ACARS_GAIN", "ACARS_RATEMULT", "ACARS_UDP"}, set()),
    "vdl2": ({"VDL2_FREQS", "VDL2_MSG_FILTER"}, {"VDL2_GAIN"}),
    "satcom": ({"SATCOM_SATELLITE", "SATCOM_UDP", "SATCOM_WEB_PORT"},
               {"SATCOM_GAIN", "SATCOM_BIAS_TEE", "SATCOM_SKIP_C", "SATCOM_SPECTRUM"}),
}

SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


class LaunchError(Exception):
    """קוד שגיאה קצר (ASCII) — זה כל מה שמודפס ליומן. לעולם לא תוכן הקלט."""


def read_input(path):
    """קורא קובץ קלט שבשליטת airam: בלי לעקוב אחרי symlink, בלי להיתקע על FIFO, רק קובץ
    רגיל עם קישור יחיד (hardlink לקובץ של root ⇒ סירוב), עד MAX_INPUT_BYTES, UTF-8."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        raise LaunchError("missing")
    except OSError:
        raise LaunchError("open")          # ELOOP (symlink), ENXIO, EACCES...
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise LaunchError("not_regular")
        if st.st_nlink != 1:
            raise LaunchError("hardlink")
        if st.st_size > MAX_INPUT_BYTES:
            raise LaunchError("too_big")
        data = os.read(fd, MAX_INPUT_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > MAX_INPUT_BYTES:
        raise LaunchError("too_big")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise LaunchError("encoding")


# --- קול: airband.conf ⇒ מספרים ⇒ render_config --------------------------------
def _one(text, key, value, required=True):
    """ערך יחיד של `key` בשורה מעוגנת. כל שורה שמתחילה ב-`key =` חייבת להתאים לתבנית
    המדויקת (אחרת שורה בפורמט אחר הייתה "נעלמת" בשקט ברינדור מחדש); שתי הופעות ⇒ דו-משמעי."""
    loose = re.findall(r"^\s*%s\s*=" % key, text, re.M | re.ASCII)
    found = re.findall(r"^\s*%s = %s;[^\n]*$" % (key, value), text, re.M | re.ASCII)
    if len(loose) != len(found):
        raise LaunchError("bad:" + key)
    if len(found) > 1:
        raise LaunchError("dup:" + key)
    if not found:
        if required:
            raise LaunchError("missing:" + key)
        return None
    return found[0]


def parse_voice_conf(text):
    """מחלץ מ-airband.conf (פלט render_config של airam-web) *רק* את הפרמטרים המספריים/
    הסגורים, ומחזיר kwargs ל-render_config. כל השאר (נתיבים, פלטים, שורות לא מוכרות)
    לא נקרא בכלל — הקונפיג שרץ מרונדר מחדש מקבועים. סובל קונפיג מגרסה קודמת (בלי
    rfnotch_ctrl/rfgain_sel, centerfreq ישן) — את אלה airam-web ממילא משכתב (_config_stale)."""
    dev = _one(text, "device_string", r'"([^"\n]*)"')
    m = re.fullmatch(r"driver=sdrplay(?:,rfnotch_ctrl=(true|false))?(?:,rfgain_sel=(\d))?", dev, re.ASCII)
    if not m:
        raise LaunchError("bad:device_string")
    fm_notch = m.group(1) == "true"
    gain = _one(text, "gain", r'"IFGR=(\d{2}),RFGR=(\d)"', required=False)
    agc = gain is None
    if agc:
        # בלי rfgain_sel (קונפיג מלפני v2.26.0) ⇒ 0, ההתנהגות שרצה אז (ברירת המחדל של ה-API)
        rf_gain, if_gain = int(m.group(2) or 0), IF_GAIN_DEFAULT
    else:
        if m.group(2) is not None:
            raise LaunchError("bad:device_string")   # ברווח ידני ה-LNA הוא RFGR בלבד
        if_gain, rf_gain = int(gain[0]), int(gain[1])
    if not (IFGR_MIN <= if_gain <= IFGR_MAX and RFGR_MIN <= rf_gain <= RFGR_MAX):
        raise LaunchError("range:gain")
    freq = float(_one(text, "freq", r"(\d{1,4}\.\d{1,6})"))
    if not 0.1 <= freq <= 1999.5:
        raise LaunchError("range:freq")
    mod = _one(text, "modulation", r'"(am|nfm)"')
    bw = _one(text, "bandwidth", r"(\d{1,6})", required=False)
    if bw is not None and (int(bw) != CHANNEL_BW_NARROW or mod != "am"):
        raise LaunchError("bad:bandwidth")
    lp = _one(text, "lowpass", r"(\d{1,5})", required=False)
    lowpass = AUDIO_LOWPASS_DEFAULT if lp is None else int(lp)
    if lowpass not in AUDIO_LOWPASS_OPTIONS:
        raise LaunchError("bad:lowpass")
    sq = _one(text, "squelch_snr_threshold", r"(\d{1,2}(?:\.\d)?)", required=False)
    if sq is None:
        squelch_mode, squelch_snr = "auto", SNR_DEFAULT
    elif sq == "0":                       # render_config: "פתוח" נכתב כ-0 שלם, ידני תמיד עם .1f
        squelch_mode, squelch_snr = "open", SNR_DEFAULT
    else:
        squelch_mode, squelch_snr = "manual", float(sq)
        if not SNR_MIN <= squelch_snr <= SNR_MAX:
            raise LaunchError("range:squelch")
    return dict(freq=freq, mod=mod, agc=agc, if_gain=if_gain, rf_gain=rf_gain,
                squelch_mode=squelch_mode, squelch_snr=squelch_snr, fm_notch=fm_notch,
                narrow=bw is not None, lowpass=lowpass)


def write_voice_conf(text, conf_dir=None):
    """כותב את הקונפיג המרונדר לתיקייה של root. התיקייה חייבת להיות שלנו ולא ניתנת לכתיבה
    לאחרים (אחרת airam היה מחליף את הקובץ אחרי הרינדור). תוכן זהה ⇒ לא נוגעים (mtime נשמר)."""
    conf_dir = Path(VOICE_CONF_DIR if conf_dir is None else conf_dir)
    try:
        st = os.lstat(conf_dir)
    except OSError:
        raise LaunchError("voice_dir")
    if (not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid()
            or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
        raise LaunchError("voice_dir")
    out = conf_dir / "airband.conf"
    data = text.encode("utf-8")
    try:
        with open(out, "rb") as f:
            if f.read() == data:
                return out
    except OSError:
        pass
    tmp = conf_dir / (".airband.conf.%d.tmp" % os.getpid())
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644)
    try:
        os.fchmod(fd, 0o644)              # לא תלוי ב-umask של היחידה — airam-web קורא אותו
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, out)
    return out


# --- ACARS / VDL2 / SATCOM: KEY=VALUE ⇒ ערכים מאומתים -----------------------------
def parse_env(text, mode):
    """KEY=VALUE בלבד (מה ש-write_*_env כותב): בלי מירכאות, בלי המשך-שורה, בלי מפתח כפול.
    מפתח שאינו ברשימה של המצב ⇒ סירוב (קובץ שמישהו הוסיף לו LD_PRELOAD הוא לא "רעש")."""
    required, optional = ENV_KEYS[mode]
    vals = {}
    for line in text.split("\n"):
        line = line.strip()
        if not line or line[0] in "#;":
            continue
        key, sep, value = line.partition("=")
        if not sep or not ENV_KEY_RE.fullmatch(key):
            raise LaunchError("syntax")
        if key not in required and key not in optional:
            raise LaunchError("unknown_key")
        if key in vals:
            raise LaunchError("dup_key")
        vals[key] = value
    if not required <= set(vals):
        raise LaunchError("missing_key")
    return vals


def _freq_list(value, rx):
    """התבנית לבדה (ספרות ASCII + נקודה) מספיקה: כל טוקן הוא ארגומנט יחיד שלא מתחיל ב-'-'.
    בלי בדיקת טווח/כפילויות — app.py לא אוכף אותן, ותדר "מוזר" הוא בעיה של המפענח, לא של root."""
    toks = value.split(" ")
    if not 1 <= len(toks) <= MAX_CHANNELS or not all(rx.fullmatch(t) for t in toks):
        raise LaunchError("bad:freqs")
    return toks


def _int_in(value, lo, hi, code):
    if not INT_RE.fullmatch(value) or not lo <= int(value) <= hi:
        raise LaunchError("bad:" + code)
    return str(int(value))


def _exact(value, allowed, code):
    if value not in allowed:
        raise LaunchError("bad:" + code)
    return value


def build_argv(mode, vals):
    """ה-argv של המפענח — זהה בדיוק להרחבת ה-ExecStart של systemd לפני v2.30.0
    (‏$VAR בלי סוגריים = פיצול לפי רווחים, ריק ⇒ נעלם; ${VAR} = ארגומנט יחיד). נבדק מול
    golden ב-tests/test_launch.py. לקול: vals = הנתיב של הקונפיג המרונדר."""
    if mode == "voice":
        return [BIN["voice"], "-F", "-c", str(vals)]
    if mode == "acars":
        return [BIN["acars"],
                "-g", _int_in(vals["ACARS_GAIN"], -10, 60, "gain"),
                "-m", _int_in(vals["ACARS_RATEMULT"], 1, 1000, "ratemult"),
                "-o", "1",
                "-j", _exact(vals["ACARS_UDP"], {ACARS_UDP}, "udp"),
                "-d", "driver=sdrplay",
                *_freq_list(vals["ACARS_FREQS"], FREQ_MHZ_RE)]
    if mode == "vdl2":
        gain = []
        g = vals.get("VDL2_GAIN", "")
        if g:
            m = VDL2_GAIN_RE.fullmatch(g)
            if not m or not (IFGR_MIN <= int(m.group(1)) <= IFGR_MAX
                             and RFGR_MIN <= int(m.group(2)) <= RFGR_MAX):
                raise LaunchError("bad:gain")
            gain = ["--soapy-gain", "IFGR=%d,RFGR=%d" % (int(m.group(1)), int(m.group(2)))]
        return [BIN["vdl2"], "--soapysdr", "driver=sdrplay", "--oversample", "20", *gain,
                "--msg-filter", _exact(vals["VDL2_MSG_FILTER"], {VDL2_MSG_FILTER}, "msg_filter"),
                "--output", "decoded:json:udp:address=127.0.0.1,port=%d" % VDL2_UDP_PORT,
                *_freq_list(vals["VDL2_FREQS"], FREQ_HZ_RE)]
    if mode == "satcom":
        gain = []
        g = vals.get("SATCOM_GAIN", "")
        if g:
            m = SATCOM_GAIN_RE.fullmatch(g)
            if not m or not IFGR_MIN <= int(m.group(1)) <= IFGR_MAX:
                raise LaunchError("bad:gain")
            gain = ["--sdrplay-gain=%d" % int(m.group(1))]
        flags = []
        for key, flag in (("SATCOM_BIAS_TEE", "-B"), ("SATCOM_SKIP_C", "--skip-c-channel"),
                          ("SATCOM_SPECTRUM", "--spectrum")):
            v = _exact(vals.get(key, ""), {"", flag}, key.lower())
            if v:
                flags.append(v)
        return [BIN["satcom"], "-i", "sdrplay",
                "--satellite=" + _exact(vals["SATCOM_SATELLITE"], SATCOM_SATELLITES, "satellite"),
                "--mode=aero", *gain, *flags,
                "--udp=" + _exact(vals["SATCOM_UDP"], {SATCOM_UDP}, "udp"),
                "--web=" + _exact(vals["SATCOM_WEB_PORT"], {SATCOM_WEB_PORT}, "web_port"),
                "--station-id=airam"]
    raise LaunchError("mode")


def clean_env(environ):
    """סביבה מינימלית ונקייה למפענח. אין EnvironmentFile ביחידה, אבל לא מעבירים הלאה כלום
    שלא צריך (LD_*, SOAPY_*, LANG...). INVOCATION_ID/JOURNAL_STREAM של systemd — רק בצורתם."""
    env = {"PATH": SAFE_PATH, "HOME": "/root"}
    inv = environ.get("INVOCATION_ID", "")
    if re.fullmatch(r"[0-9a-f]{32}", inv):
        env["INVOCATION_ID"] = inv
    js = environ.get("JOURNAL_STREAM", "")
    if re.fullmatch(r"\d{1,20}:\d{1,20}", js, re.ASCII):
        env["JOURNAL_STREAM"] = js
    return env


def prepare(mode):
    """קורא ומאמת את הקלט של המצב ומחזיר את ה-argv. זורק LaunchError (קוד בלבד)."""
    if mode == "voice":
        kw = parse_voice_conf(read_input(VOICE_CONF_IN))
        return build_argv("voice", write_voice_conf(render_config(**kw)))
    return build_argv(mode, parse_env(read_input(ENV_PATHS[mode]), mode))


def _log(msg):
    # ⚠ שורות היומן כאן עוברות באותו זרם journald כמו שורות AIRAM_RF של rtl_airband — אל
    # תכלול כאן את טוקני הטלמטריה (נבדק ב-tests/test_launch.py).
    sys.stderr.write("airam-launch: " + msg + "\n")
    sys.stderr.flush()


_SELFTEST_ENV = {
    "acars": "ACARS_FREQS=131.550 131.725\nACARS_GAIN=-10\nACARS_RATEMULT=160\nACARS_UDP=" + ACARS_UDP,
    "vdl2": "VDL2_FREQS=136975000\nVDL2_GAIN=\nVDL2_MSG_FILTER=" + VDL2_MSG_FILTER,
    "satcom": ("SATCOM_SATELLITE=AF1\nSATCOM_BIAS_TEE=-B\nSATCOM_UDP=" + SATCOM_UDP
               + "\nSATCOM_WEB_PORT=" + SATCOM_WEB_PORT),
}


def selftest():
    """בדיקת זמינות ל-install.sh (כל משתמש, בלי מכשיר ובלי קבצים): רינדור⇄פענוח ובניית argv."""
    text = render_config(132.5, "am", True, IF_GAIN_DEFAULT, RF_GAIN_DEFAULT, "open")
    if render_config(**parse_voice_conf(text)) != text:
        raise LaunchError("selftest:voice")
    for mode, env in _SELFTEST_ENV.items():
        build_argv(mode, parse_env(env, mode))
    return 0


def main(argv=None, execve=os.execve):
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["--selftest"]:
        try:
            selftest()
        except LaunchError as e:
            _log("selftest failed (%s)" % e)
            return 1
        _log("selftest ok")
        return 0
    if len(argv) != 1 or argv[0] not in BIN:
        _log("usage: airam_launch.py {voice|acars|vdl2|satcom}")
        return EX_USAGE
    mode = argv[0]
    try:
        cmd = prepare(mode)
    except LaunchError as e:
        _log("%s: refusing to start (%s)" % (mode, e))
        return EX_CONFIG
    except OSError as e:
        _log("%s: refusing to start (io:%s)" % (mode, e.errno))
        return EX_CONFIG
    _log("%s: exec %s" % (mode, " ".join(cmd)))
    try:
        execve(cmd[0], cmd, clean_env(os.environ))
    except OSError as e:
        _log("%s: exec failed (%s)" % (mode, e.errno))
        return 1
    return 0      # מגיעים לכאן רק עם execve מדומה (בדיקות)


if __name__ == "__main__":
    sys.exit(main())
