#!/usr/bin/env python3
# ============================================================================
#  AIR-AM  -  שרת בורר התדרים (web tuner)
# ----------------------------------------------------------------------------
#  ממשק וובי לבחירת תדר (פריסטים + תדר חופשי). בכל בחירה:
#   1. כותב קובץ הגדרות חדש ל-rtl_airband עם התדר הנבחר.
#   2. מפעיל מחדש את שירות rtl_airband.
#   3. הדפדפן מנגן את הסטרים מ-Icecast (mountpoint קבוע: live.mp3).
#
#  מיועד לרשת פרטית מהימנה בלבד (רץ כ-root, ללא אימות).
# ============================================================================
import json
import os
import subprocess
import threading
import time
from pathlib import Path

from flask import Flask, request, jsonify, send_from_directory

# --- קבועים ---------------------------------------------------------------
CONFIG_PATH = Path("/etc/rtl_airband/airband.conf")
STATE_PATH = Path("/var/lib/airam/state.json")
MOUNT = "live.mp3"          # שם ה-stream הקבוע ב-Icecast
ICECAST_PORT = 8000
SOURCE_PW = "airam"         # חייבת להיות זהה ל-SOURCE_PW ב-install.sh (נכתבת ל-Icecast שם)
SAMPLE_RATE = 2.56          # Msps - ערוץ יחיד, חלון צר מספיק
DC_OFFSET = 0.3             # MHz - מזיזים את centerfreq מהתדר כדי להתרחק מ-spike ה-DC
GAIN_DEFAULT = 40
SQUELCH_MODES = {"auto", "open", "manual"}
SNR_MIN, SNR_MAX = 0.0, 60.0   # dB - תחום clamp ל-SNR ידני
SNR_DEFAULT = 9.0              # ≈ סף ה-auto הפנימי של rtl_airband (~9.54 dB)

APP_DIR = Path(__file__).resolve().parent
app = Flask(__name__, static_folder=str(APP_DIR / "static"))

# כיוונון אחד בכל רגע: שני POST-ים מקבילים => שני restart שלובים זה בזה
TUNE_LOCK = threading.Lock()

# פריסטים של נתב"ג / TMA (אפשר לערוך כרצונך)
PRESETS = [
    {"name": "מגדל (Tower)",     "freq": 134.600},
    {"name": "ATIS",             "freq": 132.500, "sq": "open"},  # רציף => תמיד פתוח
    {"name": "קרקע מזרח",        "freq": 129.200},
    {"name": "גישה/המראה",       "freq": 120.500},
    {"name": "Tel Aviv Control", "freq": 121.400},
    {"name": "קרקע מערב",        "freq": 118.050},
    {"name": "מסירה (Delivery)", "freq": 121.950},
    {"name": "Guard (חירום)",    "freq": 121.500},
]

DEFAULT_STATE = {"freq": 132.500, "mod": "am", "agc": True, "gain": GAIN_DEFAULT,
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
def render_config(freq, mod, agc, gain, squelch_mode="auto", squelch_snr=SNR_DEFAULT):
    f = float(freq)
    lines = [
        "# נוצר אוטומטית ע\"י AIR-AM web tuner. שינויים ידניים נדרסים בכל כיוונון.",
        "devices:",
        "(",
        "  {",
        '    type = "soapysdr";',
        '    device_string = "driver=sdrplay";',
    ]
    if not agc:
        lines.append(f"    gain = {int(gain)};")  # אחרת AGC אוטומטי
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
        "          }",
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


def write_config(freq, mod, agc, gain, squelch_mode="auto", squelch_snr=SNR_DEFAULT):
    _atomic_write(CONFIG_PATH, render_config(freq, mod, agc, gain, squelch_mode, squelch_snr))


def load_state():
    try:
        st = json.loads(STATE_PATH.read_text())
        return {**DEFAULT_STATE, **st}
    except Exception:
        return dict(DEFAULT_STATE)


def save_state(st):
    _atomic_write(STATE_PATH, json.dumps(st))


# --- הפעלה מחדש מאומתת + רולבק --------------------------------------------
def _journal_tail(lines=8):
    return subprocess.run(["journalctl", "-u", "rtl_airband", "-n", str(lines), "--no-pager"],
                          capture_output=True, text=True).stdout


def _restart_and_verify():
    """מפעיל מחדש את rtl_airband ומוודא שנשאר חי.
    מחזיר (error, detail, sdr_down): ‏sdr_down=True כשה-restart נתקע על המתנה
    ל-SDR — במצב הזה גם רולבק נדון לאותו כישלון ואין טעם לנסות אותו.
    ה-restart עצמו יכול לחסום עד ~30 שניות (airam-wait-sdrplay) כשה-SDR מנותק."""
    try:
        r = subprocess.run(["systemctl", "restart", "rtl_airband"],
                           capture_output=True, text=True, timeout=45)
    except subprocess.TimeoutExpired:
        return "ה-restart נתקע — בדוק שה-SDR מחובר", None, True
    if r.returncode != 0:
        return (r.stderr or "restart failed").strip(), _journal_tail(), False
    # restart מחזיר 0 כשהשירות עלה, אבל rtl_airband יכול לקרוס על config רע
    # גם ~2 שניות אחרי העלייה => פולינג (לא בדיקה בודדת שמפספסת קריסה מאוחרת).
    for _ in range(7):
        time.sleep(0.5)
        chk = subprocess.run(["systemctl", "is-active", "rtl_airband"],
                             capture_output=True, text=True)
        if chk.stdout.strip() != "active":
            return "rtl_airband נכשל לעלות — בדוק תדר/חיבור SDR", _journal_tail(), False
    return None, None, False


def _rollback(prev):
    """כיוונון נכשל => משחזרים את ההגדרות האחרונות שעבדו ומרימים מחדש (best-effort)."""
    try:
        write_config(prev["freq"], prev["mod"], prev["agc"], prev["gain"],
                     prev["squelch_mode"], prev["squelch_snr"])
        subprocess.run(["systemctl", "restart", "rtl_airband"],
                       capture_output=True, text=True, timeout=45)
    except Exception:
        pass


# --- נתיבים ----------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/state")
def api_state():
    st = load_state()
    st.update(presets=PRESETS, mount=MOUNT, port=ICECAST_PORT)
    return jsonify(st)


@app.route("/api/tune", methods=["POST"])
def api_tune():
    data = request.get_json(force=True, silent=True) or {}

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
        gain = max(0, min(60, int(data.get("gain", GAIN_DEFAULT))))
    except (TypeError, ValueError):
        gain = GAIN_DEFAULT

    squelch_mode = str(data.get("squelch_mode", "auto")).lower()
    if squelch_mode not in SQUELCH_MODES:
        squelch_mode = "auto"
    try:
        squelch_snr = float(data.get("squelch_snr", SNR_DEFAULT))
    except (TypeError, ValueError):
        squelch_snr = SNR_DEFAULT
    squelch_snr = max(SNR_MIN, min(SNR_MAX, squelch_snr))

    if not TUNE_LOCK.acquire(blocking=False):
        return jsonify(ok=False, error="כיוונון אחר מתבצע כרגע — המתן שנייה ונסה שוב"), 409
    try:
        prev = load_state()   # ההגדרות האחרונות שעבדו, לרולבק במקרה כישלון
        write_config(freq, mod, agc, gain, squelch_mode, squelch_snr)

        err, detail, sdr_down = _restart_and_verify()
        if err:
            if not sdr_down:   # SDR מנותק => גם רולבק ייתקע באותה המתנה; מדלגים
                _rollback(prev)   # לא משאירים את השירות בלולאת קריסה על config רע
                err += " (חזרתי לתדר הקודם)"
            # state בתשובה => ה-UI מיישר תצוגה בלי בקשת /api/state נוספת
            return jsonify(ok=False, error=err, detail=detail, state=prev), 500

        # נשמר רק אחרי שאומת שהשירות חי => state תמיד משקף הגדרות שעובדות
        save_state({"freq": freq, "mod": mod, "agc": agc, "gain": gain,
                    "squelch_mode": squelch_mode, "squelch_snr": squelch_snr})
        return jsonify(ok=True, freq=freq, mod=mod, agc=agc, gain=gain,
                       squelch_mode=squelch_mode, squelch_snr=squelch_snr)
    finally:
        TUNE_LOCK.release()


if __name__ == "__main__":
    # ודא שקיים קובץ הגדרות התחלתי כדי ש-rtl_airband יעלה
    if not CONFIG_PATH.exists():
        st = load_state()
        write_config(st["freq"], st["mod"], st["agc"], st["gain"],
                     st["squelch_mode"], st["squelch_snr"])
    app.run(host="0.0.0.0", port=8080)
