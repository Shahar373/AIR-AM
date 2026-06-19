#!/usr/bin/env python3
# ============================================================================
#  AIR-AM  -  שרת בורר התדרים (web tuner)
# ----------------------------------------------------------------------------
#  ממשק וובי לבחירת תדר (פריסטים + תדר חופשי). בכל בחירה:
#   1. כותב קובץ הגדרות חדש ל-rtl_airband עם התדר הנבחר.
#   2. מפעיל מחדש את שירות rtl_airband.
#   3. הדפדפן מנגן את הסטרים מ-Icecast (mountpoint קבוע: live.mp3).
#
#  מיועד לרשת פרטית מהימנה בלבד. רץ כמשתמש לא-root (airam) עם sudoers ממוקד
#  ל-restart בלבד; אימות PIN אופציונלי (AIRAM_PIN), כבוי כברירת מחדל.
# ============================================================================
import json
import logging
import os
import re
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, request, jsonify, send_from_directory, abort

import adsb   # מסלול פעיל + אינדיקציית GPS מנתוני ADS-B (thread נפרד)

# stdout => journald (השירות רץ תחת systemd); journalctl -u airam-web מציג הכל
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("airam")

# --- קבועים ---------------------------------------------------------------
CONFIG_PATH = Path("/etc/rtl_airband/airband.conf")
STATE_PATH = Path("/var/lib/airam/state.json")
MOUNT = "live.mp3"          # שם ה-stream הקבוע ב-Icecast
ICECAST_PORT = 8000
SOURCE_PW = "airam"         # חייבת להיות זהה ל-SOURCE_PW ב-install.sh (נכתבת ל-Icecast שם)
SAMPLE_RATE = 2.56          # Msps - ערוץ יחיד, חלון צר מספיק
DC_OFFSET = 0.3             # MHz - מזיזים את centerfreq מהתדר כדי להתרחק מ-spike ה-DC
# רווח SDRplay (מודל legacy של SoapySDRPlay3): שני אלמנטים נפרדים, וקטן יותר = רווח גדול יותר.
#   IFGR - הפחתת רווח בתדר הביניים, 20–59 dB.
#   RFGR - מצב ה-LNA (הפחתת רווח RF), 0–9 (לא-לינארי, ~7dB לצעד).
# כש-AGC כבוי כותבים gain = "IFGR=..,RFGR=.."; כש-AGC דלוק משמיטים את gain => AGC חומרתי.
IFGR_MIN, IFGR_MAX = 20, 59
RFGR_MIN, RFGR_MAX = 0, 9
IF_GAIN_DEFAULT = 40            # IFGR - אמצע הטווח, בטוח מפני עומס יתר
RF_GAIN_DEFAULT = 4            # RFGR - מצב LNA בינוני
OVERLOAD_DBFS = -3.0          # סף "עומס יתר": אות ערוץ קרוב ל-full scale של ה-ADC
SQUELCH_MODES = {"auto", "open", "manual"}
SNR_MIN, SNR_MAX = 0.0, 60.0   # dB - תחום clamp ל-SNR ידני
SNR_DEFAULT = 9.0              # ≈ סף ה-auto הפנימי של rtl_airband (~9.54 dB)
STATS_PATH = Path("/run/rtl_airband_stats.txt")   # tmpfs - בלי שחיקת SD
STATS_MAX_AGE = 5.0            # rtl_airband כותב כל ~1 שנייה; ~5 כתיבות => סובל ג'יטר אך עדיין מזהה restart

# הקלטות: rtl_airband כותב קובץ MP3 לכל שידור (split_on_transmission) בשם
# <REC_BASENAME>_YYYYMMDD_HHMMSS_<Hz>.mp3 (.tmp בזמן כתיבה, rename בסגירה
# ~0.5ש' אחרי שהסקוולץ' נסגר). קובץ שהסתיים = אירוע ביומן השידורים.
REC_DIR = Path("/var/lib/airam/recordings")
REC_BASENAME = "airam"         # filename_template ב-config וגם עוגן הפרסור של השמות
REC_BYTES_PER_SEC = 6000       # CBR 48kbps (ה-patch ב-install.sh) => הערכת משך מגודל
REC_MAX_FILES = 200            # retention
REC_MAX_BYTES = 100 * 1024 * 1024
ACTIVITY_PATH = Path("/var/lib/airam/activity.jsonl")
ACTIVITY_KEEP = 500            # היומן שורד את מחיקת הקבצים (retention) - רק בלי נגינה
ACTIVITY_RETURN = 50
WATCH_INTERVAL = 10.0          # שניות בין סריקות של תיקיית ההקלטות

# תמלול ATC (אופציונלי): whisper.cpp מקומי. לכל הקלטה שמסתיימת נכתב קובץ-צד
# <file>.mp3.txt עם הטקסט. פעיל רק אם AIRAM_TRANSCRIBE=1 וגם הבינארי+המודל קיימים
# (install.sh בונה אותם רק עם INSTALL_WHISPER=1) => התקנות קיימות לא מושפעות.
TRANSCRIBE = os.environ.get("AIRAM_TRANSCRIBE", "").strip().lower() in ("1", "true", "yes", "on")
WHISPER_BIN = os.environ.get("AIRAM_WHISPER_BIN", "/usr/local/bin/whisper-cli")
WHISPER_MODEL = os.environ.get("AIRAM_WHISPER_MODEL", "/opt/airam/models/ggml-base.en.bin")
WHISPER_LANG = os.environ.get("AIRAM_WHISPER_LANG", "en")   # ATC בישראל = אנגלית
TRANSCRIBE_TIMEOUT = 120.0     # שניות לקובץ בודד (המרה + תמלול)
# רמז הקשר => מטה את המודל לפרזיולוגיית ATC ושמות מקומיים (משפר דיוק משמעותית)
WHISPER_PROMPT = ("Air traffic control radio between pilots and Ben Gurion / Tel Aviv "
                  "tower, ground, approach. Phrases: cleared for takeoff, line up and wait, "
                  "taxi to runway, hold short, contact tower, squawk, climb, descend, "
                  "heading, knots, QNH, wind, runway 03 12 21 26 30.")

APP_DIR = Path(__file__).resolve().parent


def _read_version():
    # VERSION יושב בשורש המאגר (פיתוח) או לצד app.py (ב-Pi: install.sh מעתיק אותו)
    for p in (APP_DIR / "VERSION", APP_DIR.parent / "VERSION"):
        try:
            return p.read_text().strip()
        except OSError:
            continue
    return "dev"


VERSION = _read_version()
app = Flask(__name__, static_folder=str(APP_DIR / "static"))

# כיוונון אחד בכל רגע: שני POST-ים מקבילים => שני restart שלובים זה בזה
TUNE_LOCK = threading.Lock()

# הרצה כמשתמש לא-root (חיזוק אבטחה): ה-restart עובר דרך sudoers ממוקד.
# כ-root אין צורך ב-sudo => פריסות ישנות (טרם re-install) ממשיכות לעבוד.
SUDO = [] if os.geteuid() == 0 else ["sudo", "-n"]

# אימות אופציונלי: פעיל אך ורק אם AIRAM_PIN הוגדר ב-environment של השירות.
# לא הוגדר => אפס שינוי בחוויה ("בלי סיסמאות" כברירת מחדל).
AIRAM_PIN = os.environ.get("AIRAM_PIN", "").strip()


@app.before_request
def _guard():
    """הגנות קלות על בקשות משנות-מצב (POST/PUT/DELETE):
      1. CSRF / DNS-rebinding: אם נשלח Origin/Referer הוא חייב להתאים ל-Host.
      2. אימות אופציונלי: אם AIRAM_PIN הוגדר, נדרש header X-AIRAM-PIN תואם.
    בקשות GET (סטרים/מדדים/health/activity/airspace/metar/power) לא מושפעות."""
    if request.method not in ("POST", "PUT", "DELETE"):
        return None
    origin = request.headers.get("Origin") or request.headers.get("Referer")
    if origin and urlparse(origin).netloc != request.host:
        return jsonify(ok=False, error="מקור הבקשה לא תואם (Origin)"), 403
    if AIRAM_PIN and request.headers.get("X-AIRAM-PIN", "") != AIRAM_PIN:
        return jsonify(ok=False, error="נדרש PIN", auth=True), 401
    return None

# פריסטים של נתב"ג / TMA - רק זריעה ראשונית; מרגע עריכה בממשק האמת היא
# /var/lib/airam/presets.json (נטען בכל בקשה - הקובץ זעיר והעריכה נדירה)
DEFAULT_PRESETS = [
    {"name": "מגדל (Tower)",     "freq": 134.600},
    {"name": "ATIS",             "freq": 132.500, "sq": "open"},  # רציף => תמיד פתוח
    {"name": "קרקע מזרח",        "freq": 129.200},
    {"name": "גישה/המראה",       "freq": 120.500},
    {"name": "Tel Aviv Control", "freq": 121.400},
    {"name": "קרקע מערב",        "freq": 118.050},
    {"name": "מסירה (Delivery)", "freq": 121.950},
    {"name": "Guard (חירום)",    "freq": 121.500},
]
PRESETS_PATH = Path("/var/lib/airam/presets.json")
PRESETS_MAX = 30


def _validate_presets(lst):
    """(ok, cleaned) - מנרמל ומאמת רשימת פריסטים מהלקוח/מהדיסק."""
    if not isinstance(lst, list) or len(lst) > PRESETS_MAX:
        return False, None
    out = []
    for p in lst:
        if not isinstance(p, dict):
            return False, None
        name = str(p.get("name", "")).strip()
        try:
            freq = float(p.get("freq"))
        except (TypeError, ValueError):
            return False, None
        if not name or len(name) > 40 or not (0.1 <= freq <= 1999.5):
            return False, None
        item = {"name": name, "freq": round(freq, 4)}
        sq = p.get("sq")
        if sq is not None:
            sq = str(sq).lower()
            if sq not in SQUELCH_MODES:
                return False, None
            item["sq"] = sq
        out.append(item)
    return True, out


def load_presets():
    try:
        ok, cleaned = _validate_presets(json.loads(PRESETS_PATH.read_text()))
        if ok:
            return cleaned
    except Exception:
        pass   # אין קובץ / פגום => ברירת המחדל (הקובץ נכתב רק בעריכה הראשונה)
    return [dict(p) for p in DEFAULT_PRESETS]

DEFAULT_STATE = {"freq": 132.500, "mod": "am", "agc": True,
                 "if_gain": IF_GAIN_DEFAULT, "rf_gain": RF_GAIN_DEFAULT,
                 "squelch_mode": "open", "squelch_snr": SNR_DEFAULT}  # ברירת מחדל ATIS => תמיד פתוח


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
def render_config(freq, mod, agc, if_gain, rf_gain, squelch_mode="auto", squelch_snr=SNR_DEFAULT):
    f = float(freq)
    lines = [
        "# נוצר אוטומטית ע\"י AIR-AM web tuner. שינויים ידניים נדרסים בכל כיוונון.",
        "localtime = true;   # חותמות הזמן בשמות קובצי ההקלטה בזמן מקומי",
        f'stats_filepath = "{STATS_PATH}";   # מדדי RF (signal/noise) ל-/api/metrics',
        "devices:",
        "(",
        "  {",
        '    type = "soapysdr";',
        '    device_string = "driver=sdrplay";',
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


def _atomic_write(path, text):
    """כתיבה אטומית (tmp + rename): rtl_airband יכול לעלות בכל רגע
    (Restart=always / udev) ואסור שיקרא קובץ חצי-כתוב."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def write_config(freq, mod, agc, if_gain, rf_gain, squelch_mode="auto", squelch_snr=SNR_DEFAULT):
    _atomic_write(CONFIG_PATH, render_config(freq, mod, agc, if_gain, rf_gain, squelch_mode, squelch_snr))


def load_state():
    try:
        st = json.loads(STATE_PATH.read_text())
        return {**DEFAULT_STATE, **st}
    except Exception:
        return dict(DEFAULT_STATE)


def save_state(st):
    _atomic_write(STATE_PATH, json.dumps(st))


# --- הפעלה מחדש מאומתת + רולבק --------------------------------------------
def _sdr_present():
    """בדיקת USB מהירה (vendor 1df7 = SDRplay) בלי לפתוח את המכשיר."""
    try:
        return subprocess.run(["lsusb", "-d", "1df7:"],
                              capture_output=True, timeout=5).returncode == 0
    except Exception:
        return True   # אין lsusb / ספק => מניחים שמחובר (עדיף רולבק מיותר מאף-פעם)


def _journal_tail(lines=8):
    return subprocess.run(["journalctl", "-u", "rtl_airband", "-n", str(lines), "--no-pager"],
                          capture_output=True, text=True).stdout


def _restart_and_verify():
    """מפעיל מחדש את rtl_airband ומוודא שנשאר חי.
    מחזיר (error, detail, sdr_down): ‏sdr_down=True כשה-restart נתקע על המתנה
    ל-SDR — במצב הזה גם רולבק נדון לאותו כישלון ואין טעם לנסות אותו.
    ה-restart עצמו יכול לחסום עד ~30 שניות (airam-wait-sdrplay) כשה-SDR מנותק."""
    try:
        r = subprocess.run([*SUDO, "systemctl", "restart", "rtl_airband"],
                           capture_output=True, text=True, timeout=45)
    except subprocess.TimeoutExpired:
        return "ה-restart נתקע — בדוק שה-SDR מחובר", None, True
    if r.returncode != 0:
        # המסלול הנפוץ כשה-SDR מנותק: airam-wait-sdrplay ממצה 30 ניסיונות
        # וה-restart נכשל עם rc!=0 (לא timeout) => מזהים לפי נוכחות ה-USB.
        return (r.stderr or "restart failed").strip(), _journal_tail(), not _sdr_present()
    # restart מחזיר 0 כשהשירות עלה, אבל rtl_airband יכול לקרוס על config רע
    # גם ~2 שניות אחרי העלייה => פולינג (לא בדיקה בודדת שמפספסת קריסה מאוחרת).
    for _ in range(7):
        time.sleep(0.5)
        try:
            chk = subprocess.run(["systemctl", "is-active", "rtl_airband"],
                                 capture_output=True, text=True, timeout=5)
        except subprocess.TimeoutExpired:
            continue   # systemctl תקוע => מדלגים על הבדיקה הזו, לא תוקעים את הבקשה
        if chk.stdout.strip() != "active":
            return "rtl_airband נכשל לעלות — בדוק תדר/חיבור SDR", _journal_tail(), False
    return None, None, False


def _rollback(prev):
    """כיוונון נכשל => משחזרים את ההגדרות האחרונות שעבדו ומרימים מחדש (best-effort)."""
    log.warning("rollback to %.3f MHz", prev["freq"])
    try:
        write_config(prev["freq"], prev["mod"], prev["agc"], prev["if_gain"],
                     prev["rf_gain"], prev["squelch_mode"], prev["squelch_snr"])
        subprocess.run([*SUDO, "systemctl", "restart", "rtl_airband"],
                       capture_output=True, text=True, timeout=45)
    except Exception:
        pass


# --- נתיבים ----------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/live.m3u")
def live_playlist():
    """Playlist המצביע על סטרים ה-Icecast. פתיחה בנגן שמע חיצוני (VLC וכו')
    מנגנת ברקע בצורה חסינה, ללא תלות בדפדפן."""
    host = request.host.split(":", 1)[0]          # רק ה-hostname, בלי פורט ה-web
    url = f"http://{host}:{ICECAST_PORT}/{MOUNT}"
    body = "#EXTM3U\n#EXTINF:-1,AIR-AM live\n" + url + "\n"
    return app.response_class(body, mimetype="audio/x-mpegurl")


# נכסי PWA המוגשים מהשורש (לא מ-/static): ה-service worker *חייב* להיות מהשורש
# כדי שה-scope שלו יכסה את כל האתר, וה-manifest/אייקונים נוחים בשורש לצדו.
_ROOT_ASSETS = {
    "manifest.webmanifest": "application/manifest+json",
    "sw.js": "text/javascript",
    "icon-192.png": "image/png",
    "icon-512.png": "image/png",
    "apple-touch-icon.png": "image/png",
}


@app.route("/<path:fname>")
def root_asset(fname):
    mimetype = _ROOT_ASSETS.get(fname)
    if mimetype is None:
        abort(404)
    resp = send_from_directory(app.static_folder, fname, mimetype=mimetype)
    if fname == "sw.js":
        resp.headers["Service-Worker-Allowed"] = "/"   # scope לכל האתר
        resp.headers["Cache-Control"] = "no-cache"      # עדכון UI נקלט מיד
    return resp


@app.route("/api/state")
def api_state():
    st = load_state()
    st.update(presets=load_presets(), mount=MOUNT, port=ICECAST_PORT, version=VERSION)
    return jsonify(st)


@app.route("/api/presets", methods=["GET", "PUT"])
def api_presets():
    """PUT מחליף את הרשימה כולה - העריכה בממשק היא על הסט המלא, אין צורך ב-CRUD."""
    if request.method == "GET":
        return jsonify(ok=True, presets=load_presets())
    data = request.get_json(silent=True)
    ok, cleaned = _validate_presets(data)
    if not ok:
        return jsonify(ok=False, error="רשימת פריסטים לא תקינה", presets=load_presets()), 400
    _atomic_write(PRESETS_PATH, json.dumps(cleaned, ensure_ascii=False))
    log.info("presets updated (%d items, from %s)", len(cleaned), request.remote_addr)
    return jsonify(ok=True, presets=cleaned)


@app.route("/api/health")
def api_health():
    """סטטוס המערכת — מאפשר ל-UI להבדיל בין "אין שידור" ל"משהו נפל"."""
    services = {}
    for svc in ("rtl_airband", "icecast2", "sdrplay"):
        try:
            r = subprocess.run(["systemctl", "is-active", svc],
                               capture_output=True, text=True, timeout=5)
            services[svc] = (r.stdout.strip() or "unknown")
        except Exception:
            services[svc] = "unknown"
    try:
        stats_age = round(time.time() - STATS_PATH.stat().st_mtime, 1)
    except OSError:
        stats_age = None     # עוד לא נכתב (rtl_airband לא עלה / זה עתה הופעל)
    return jsonify(ok=(services["rtl_airband"] == "active" and services["icecast2"] == "active"),
                   services=services, sdr_present=_sdr_present(), stats_age=stats_age)


# שורת מדד בקובץ ה-stats של rtl_airband (פורמט Prometheus):
#   channel_dbfs_signal_level{freq="132.500"}	-42.3
# ה-label freq מאותר בתוך הסוגריים בנפרד => עמיד לשינוי סדר/הוספת labels ב-upstream.
_METRIC_RE = re.compile(r'^(\w+)\{([^}]*)\}\s+(-?[0-9.]+)')
_FREQ_LABEL_RE = re.compile(r'(?:^|[,{\s])freq="([0-9.]+)"')


def parse_stats(text, want_freq):
    """מחלץ {metric: value} לשורות שה-label freq שלהן תואם (MHz בפורמט 3 ספרות)."""
    vals = {}
    for line in text.splitlines():
        m = _METRIC_RE.match(line)
        if not m:
            continue
        fl = _FREQ_LABEL_RE.search(m.group(2))
        if fl and fl.group(1) == want_freq:
            vals[m.group(1)] = float(m.group(3))
    return vals


# --- יומן שידורים והקלטות ---------------------------------------------------
_REC_NAME_RE = re.compile(rf"^{re.escape(REC_BASENAME)}_\d{{8}}_\d{{6}}_(\d+)\.mp3$")


def _rec_freq_mhz(name):
    """airam_20260611_203455_134600000.mp3 => 134.600 (MHz). אחר => None."""
    m = _REC_NAME_RE.match(name)
    return round(int(m.group(1)) / 1e6, 3) if m else None


def _append_activity(rows):
    """append + קיצוץ. הקובץ מוגבל (מאות שורות) => קריאה מלאה זולה, וכתיבה
    אטומית כדי ש-/api/activity לא יקרא קובץ חצי-כתוב."""
    try:
        lines = ACTIVITY_PATH.read_text().splitlines()
    except OSError:
        lines = []
    lines += [json.dumps(r, ensure_ascii=False) for r in rows]
    if len(lines) > ACTIVITY_KEEP * 2:   # קיצוץ בהיסטרזיס - לא משכתבים בכל append
        lines = lines[-ACTIVITY_KEEP:]
    _atomic_write(ACTIVITY_PATH, "\n".join(lines) + "\n")


def _last_logged_ts():
    """ה-ts האחרון ביומן - ממנו ממשיכים אחרי restart (בלי לרשום כפולים)."""
    try:
        for ln in reversed(ACTIVITY_PATH.read_text().splitlines()):
            try:
                return float(json.loads(ln)["ts"])
            except (ValueError, KeyError, TypeError):
                continue
    except OSError:
        pass
    return 0.0


def _transcript_path(mp3):
    """קובץ-צד התמלול לצד ההקלטה: airam_....mp3 => airam_....mp3.txt."""
    return mp3.parent / (mp3.name + ".txt")


def _transcribe_file(mp3):
    """ממיר MP3 ל-WAV 16kHz מונו (ffmpeg) ומריץ whisper.cpp. מחזיר טקסט או None.
    כל כשל (ffmpeg/whisper/timeout) מטופל בשקט => לולאת הרקע ממשיכה."""
    wav = mp3.parent / (mp3.name + ".wav.tmp")
    try:
        subprocess.run(["ffmpeg", "-nostdin", "-y", "-i", str(mp3),
                        "-ar", "16000", "-ac", "1", str(wav)],
                       capture_output=True, timeout=TRANSCRIBE_TIMEOUT, check=True)
        out = subprocess.run([WHISPER_BIN, "-m", WHISPER_MODEL, "-f", str(wav),
                              "-l", WHISPER_LANG, "-nt", "--prompt", WHISPER_PROMPT],
                             capture_output=True, text=True,
                             timeout=TRANSCRIBE_TIMEOUT, check=True)
        return " ".join(out.stdout.split()).strip() or None
    except Exception:
        log.exception("transcribe %s", mp3.name)
        return None
    finally:
        try:
            wav.unlink()
        except OSError:
            pass


def _transcribe_worker():
    """לולאת רקע: מתמלל הקלטות שעוד אין להן קובץ-צד .txt (חדש=>ישן).
    כותב גם תמלול ריק => לא מנסים שוב את אותו קובץ בלולאה הבאה."""
    if not (Path(WHISPER_BIN).exists() and Path(WHISPER_MODEL).exists()):
        log.warning("transcription on, but whisper missing (%s / %s) - מדלג",
                    WHISPER_BIN, WHISPER_MODEL)
        return
    log.info("transcription worker started (model=%s)", WHISPER_MODEL)
    while True:
        try:
            recs = sorted(REC_DIR.glob("*.mp3"),
                          key=lambda p: p.stat().st_mtime, reverse=True)
            for mp3 in recs:
                txt = _transcript_path(mp3)
                if txt.exists():
                    continue
                _atomic_write(txt, (_transcribe_file(mp3) or "") + "\n")
        except Exception:
            log.exception("transcribe worker")
        time.sleep(WATCH_INTERVAL)


def _sweep_recordings():
    """retention: עד REC_MAX_FILES / REC_MAX_BYTES (חדש=>ישן), ו-.tmp נטושים
    (שידור שנקטע בקריסה משאיר .tmp שלעולם לא ייסגר ל-mp3). קובץ-צד התמלול
    (.txt) נמחק יחד עם ההקלטה שלו."""
    try:
        recs = sorted(REC_DIR.glob("*.mp3"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return
    total = 0
    for i, p in enumerate(recs):
        try:
            total += p.stat().st_size
            if i >= REC_MAX_FILES or total > REC_MAX_BYTES:
                p.unlink()
                _transcript_path(p).unlink(missing_ok=True)
        except OSError:
            pass
    now = time.time()
    for p in REC_DIR.glob("*.tmp"):
        try:
            if now - p.stat().st_mtime > 3600:
                p.unlink()
        except OSError:
            pass


def _scan_new_recordings(last_seen):
    """(rows, newest) - הקלטות שה-mtime שלהן מאוחר מ-last_seen, חדש=>ישן לפי mtime.
    ה-ts מעוגל *לפני* ההשוואה - אותו עיגול שנכתב ליומן (ושחוזר מ-_last_logged_ts)
    => סריקה חוזרת אחרי restart לא תייצר שורות כפולות."""
    rows, newest = [], last_seen
    try:
        recs = sorted(REC_DIR.glob("*.mp3"), key=lambda p: p.stat().st_mtime)
    except OSError:
        recs = []
    for p in recs:
        try:
            stat = p.stat()
        except OSError:
            continue   # נמחק בינתיים (retention) => מדלגים
        ts = round(stat.st_mtime, 1)
        if ts > last_seen:
            rows.append({"ts": ts, "freq": _rec_freq_mhz(p.name), "file": p.name,
                         "dur": round(stat.st_size / REC_BYTES_PER_SEC, 1)})
            newest = max(newest, ts)
    return rows, newest


def _activity_watcher():
    """לולאת רקע: הקלטה חדשה שהסתיימה => שורה ביומן; ואז retention.
    בעלייה ממשיכים מה-ts האחרון שנרשם => הקלטות מהזמן שהשרת היה כבוי נקלטות."""
    last_seen = _last_logged_ts()
    while True:
        try:
            rows, newest = _scan_new_recordings(last_seen)
            if rows:
                _append_activity(rows)
                last_seen = newest   # מקדמים רק אחרי כתיבה מוצלחת => כישלון append לא מאבד אירועים
            _sweep_recordings()
        except Exception:
            log.exception("activity watcher")
        time.sleep(WATCH_INTERVAL)


@app.route("/api/activity")
def api_activity():
    """אירועי השידור האחרונים, חדש=>ישן. exists=False כשההקלטה כבר נמחקה ב-retention."""
    try:
        lines = ACTIVITY_PATH.read_text().splitlines()
    except OSError:
        lines = []
    events = []
    for ln in reversed(lines):
        if len(events) >= ACTIVITY_RETURN:
            break
        try:
            ev = json.loads(ln)
        except ValueError:
            continue
        ev["exists"] = bool(ev.get("file")) and (REC_DIR / ev["file"]).is_file()
        ev["text"] = None
        if ev.get("file"):
            try:
                ev["text"] = (REC_DIR / (ev["file"] + ".txt")).read_text().strip() or None
            except OSError:
                pass   # אין תמלול (כבוי, עדיין מעובד, או נמחק) => None
        events.append(ev)
    return jsonify(ok=True, events=events)


@app.route("/recordings/<name>")
def recordings(name):
    # send_from_directory חוסם path traversal; ‏<name> (לא <path:>) חוסם תתי-תיקיות
    return send_from_directory(str(REC_DIR), name)


# --- METAR נתב"ג --------------------------------------------------------------
METAR_URL = "https://aviationweather.gov/api/data/metar?ids=LLBG"
METAR_TTL = 300.0              # ה-METAR מתעדכן ~כל חצי שעה; 5 דקות cache מנומס
_metar = {"checked": 0.0, "fetched": 0.0, "text": None}
_METAR_LOCK = threading.Lock()


@app.route("/api/metar")
def api_metar():
    """METAR גולמי של LLBG. כשל (אין אינטרנט) => מחזירים את האחרון שיש + גילו,
    וה-UI מחליט; אין retry לפני שעבר ה-TTL כדי לא להציק ל-API הציבורי."""
    now = time.time()
    # תופסים את ה-slot מתחת לנעילה (רק thread אחד מביא), אבל מבצעים את ה-fetch
    # *מחוץ* לנעילה => בקשות /api/metar מקבילות לא נחסמות 5 שניות על ה-HTTP.
    with _METAR_LOCK:
        do_fetch = now - _metar["checked"] > METAR_TTL
        if do_fetch:
            _metar["checked"] = now
    if do_fetch:
        try:
            req = urllib.request.Request(METAR_URL, headers={"User-Agent": "AIR-AM tuner"})
            with urllib.request.urlopen(req, timeout=5) as r:
                text = r.read().decode("utf-8", "replace").strip()
            if text:
                with _METAR_LOCK:
                    _metar.update(fetched=now, text=text)
        except Exception:
            pass   # שומרים את הישן; age בתשובה חושף שהוא לא טרי
    with _METAR_LOCK:
        text = _metar["text"]
        age = round(now - _metar["fetched"], 1) if text else None
    return jsonify(ok=True, metar=text, age=age)


@app.route("/api/metrics")
def api_metrics():
    """מדדי RF חיים לתדר הנוכחי. rtl_airband מרענן את הקובץ כל ~1 שנייה."""
    try:
        age = time.time() - STATS_PATH.stat().st_mtime
        text = STATS_PATH.read_text()
    except OSError:
        return jsonify(ok=True, fresh=False)   # עוד לא נכתב (אחרי restart/אתחול)

    want = f"{load_state()['freq']:.3f}"       # מדדים מתויגים freq=MHz ב-3 ספרות
    vals = parse_stats(text, want)

    sig = vals.get("channel_dbfs_signal_level")
    noise = vals.get("channel_dbfs_noise_level")
    snr = round(sig - noise, 1) if (sig is not None and noise is not None) else None
    # עומס יתר: רמת האות בערוץ מתקרבת ל-full scale (0 dBFS) => ה-ADC/רווח רווי.
    overload = sig is not None and sig >= OVERLOAD_DBFS
    return jsonify(ok=True, fresh=(age <= STATS_MAX_AGE and snr is not None),
                   age=round(age, 1), signal=sig, noise=noise, snr=snr,
                   overload=overload, overload_dbfs=OVERLOAD_DBFS,
                   squelch_opens=vals.get("channel_squelch_counter"))


@app.route("/api/airspace")
def api_airspace():
    """מסלול נחיתות/המראות פעיל ומצב GPS, מנותחים מ-ADS-B (ראה adsb.py).
    קורא snapshot בזיכרון בלבד - אף פעם לא חוסם ואף פעם לא 500."""
    return jsonify(adsb.snapshot())


def _vcgencmd(*args):
    """מריץ vcgencmd ומחזיר stdout (או None אם לא Pi / לא מותקן / נכשל)."""
    try:
        r = subprocess.run(["vcgencmd", *args], capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


@app.route("/api/power")
def api_power():
    """מצב אספקת המתח ל-Pi (שימושי במיוחד עם סוללה ניידת):
      get_throttled  -> דגלי undervoltage/throttling (כל דגמי Pi)
      pmic_read_adc  -> מתח כניסה 5V בפועל (Pi 5 בלבד)
      measure_temp   -> טמפ' ליבה
    ביטים של get_throttled: 0=under-volt עכשיו · 2=throttled עכשיו ·
    16=under-volt קרה מאז אתחול · 18=throttling קרה."""
    out = _vcgencmd("get_throttled")
    if out is None:
        return jsonify(ok=False)   # אין vcgencmd (לא Pi / חסר) => הממשק מסתיר את החיווי

    flags = 0
    m = re.search(r"0x([0-9a-fA-F]+)", out)
    if m:
        flags = int(m.group(1), 16)

    volts_in = None
    adc = _vcgencmd("pmic_read_adc")          # Pi 5 בלבד
    if adc:
        mv = re.search(r"EXT5V_V\s+volt\([^)]*\)=([0-9.]+)", adc)
        if mv:
            volts_in = round(float(mv.group(1)), 2)

    temp = None
    mt = re.search(r"=([0-9.]+)", _vcgencmd("measure_temp") or "")
    if mt:
        temp = round(float(mt.group(1)), 1)

    return jsonify(ok=True, throttled=hex(flags),
                   undervolt_now=bool(flags & 0x1),
                   throttle_now=bool(flags & 0x4),
                   undervolt_ever=bool(flags & 0x10000),
                   throttle_ever=bool(flags & 0x40000),
                   volts_in=volts_in, temp=temp)


@app.route("/api/tune", methods=["POST"])
def api_tune():
    # בלי force=True: מחייב Content-Type: application/json => דפדפן זר (CSRF) לא
    # יכול לשלוח טופס text/plain שמכוונן את הרדיו (כמו ב-/api/presets).
    data = request.get_json(silent=True) or {}

    # ולידציה של התדר (נכתב כ-float מפורמט => ללא סיכון הזרקה)
    try:
        freq = float(data.get("freq"))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="תדר לא תקין"), 400
    if not (0.1 <= freq <= 1999.5):   # מרווח עבור DC_OFFSET (centerfreq <= 2000)
        return jsonify(ok=False, error="תדר מחוץ לטווח (0.1–1999.5 MHz)"), 400

    mod = "nfm" if str(data.get("mod", "am")).lower() == "nfm" else "am"
    agc_raw = data.get("agc", True)   # עמיד גם ל-"false" טקסטואלי (curl), לא רק bool
    agc = agc_raw if isinstance(agc_raw, bool) else str(agc_raw).lower() not in ("false", "0", "off", "no")
    try:
        if_gain = max(IFGR_MIN, min(IFGR_MAX, int(data.get("if_gain", IF_GAIN_DEFAULT))))
    except (TypeError, ValueError):
        if_gain = IF_GAIN_DEFAULT
    try:
        rf_gain = max(RFGR_MIN, min(RFGR_MAX, int(data.get("rf_gain", RF_GAIN_DEFAULT))))
    except (TypeError, ValueError):
        rf_gain = RF_GAIN_DEFAULT

    squelch_mode = str(data.get("squelch_mode", "auto")).lower()
    if squelch_mode not in SQUELCH_MODES:
        squelch_mode = "auto"
    try:
        squelch_snr = float(data.get("squelch_snr", SNR_DEFAULT))
    except (TypeError, ValueError):
        squelch_snr = SNR_DEFAULT
    squelch_snr = max(SNR_MIN, min(SNR_MAX, squelch_snr))

    if not TUNE_LOCK.acquire(blocking=False):
        # state בתשובה => ה-UI מיישר את התצוגה האופטימית חזרה למציאות
        return jsonify(ok=False, error="כיוונון אחר מתבצע כרגע — המתן שנייה ונסה שוב",
                       state=load_state()), 409
    try:
        prev = load_state()   # ההגדרות האחרונות שעבדו, לרולבק במקרה כישלון
        new_state = {"freq": freq, "mod": mod, "agc": agc, "if_gain": if_gain,
                     "rf_gain": rf_gain, "squelch_mode": squelch_mode, "squelch_snr": squelch_snr}
        log.info("tune %.3f MHz mod=%s agc=%s if_gain=%d rf_gain=%d squelch=%s snr=%.1f (from %s)",
                 freq, mod, agc, if_gain, rf_gain, squelch_mode, squelch_snr, request.remote_addr)
        write_config(freq, mod, agc, if_gain, rf_gain, squelch_mode, squelch_snr)

        err, detail, sdr_down = _restart_and_verify()
        if err:
            log.warning("tune %.3f MHz failed: %s (sdr_down=%s)", freq, err, sdr_down)
            if sdr_down:
                # ה-SDR מנותק: רולבק ייתקע באותה המתנה בדיוק, אז מדלגים עליו.
                # הקונפיג החדש נשאר על הדיסק וייקלט כשהמכשיר יחובר (udev מרים
                # את השירותים) => שומרים state תואם לדיסק, לא את הקודם.
                save_state(new_state)
                return jsonify(ok=False, detail=detail, state=new_state,
                               error=err + " — התדר יוחל אוטומטית כשה-SDR יחובר"), 500
            _rollback(prev)   # config רע => לא משאירים את השירות בלולאת קריסה
            return jsonify(ok=False, error=err + " (חזרתי לתדר הקודם)",
                           detail=detail, state=prev), 500

        # נשמר רק אחרי שאומת שהשירות חי => state תמיד משקף הגדרות שעובדות
        save_state(new_state)
        return jsonify(ok=True, **new_state)
    finally:
        TUNE_LOCK.release()


if __name__ == "__main__":
    # ודא קובץ הגדרות עדכני: חסר => כותבים; קיים בלי תכונה שהממשק מסתמך עליה
    # (שדרוג מגרסה ישנה: stats_filepath למדדי RF, localtime להקלטות) =>
    # משכתבים ומרימים את rtl_airband פעם אחת כדי שהתכונות יפעלו.
    try:
        _cur = CONFIG_PATH.read_text()
    except OSError:
        _cur = None
    if _cur is None or "stats_filepath" not in _cur or "localtime" not in _cur:
        st = load_state()
        write_config(st["freq"], st["mod"], st["agc"], st["if_gain"],
                     st["rf_gain"], st["squelch_mode"], st["squelch_snr"])
        if _cur is not None:   # שדרוג: השירות כבר רץ עם ההגדרות הישנות
            try:
                subprocess.run([*SUDO, "systemctl", "restart", "rtl_airband"],
                               capture_output=True, timeout=60)
            except Exception:
                pass
    REC_DIR.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=_activity_watcher, daemon=True).start()
    if TRANSCRIBE:   # תמלול ATC אופציונלי - דמון נפרד (לא חוסם את היומן/retention)
        threading.Thread(target=_transcribe_worker, daemon=True).start()
    adsb.start()   # רק כשרצים כשרת (לא בזמן import) - דמון, לא מעכב עלייה
    app.run(host="0.0.0.0", port=8080)
