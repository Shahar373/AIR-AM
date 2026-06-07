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
import subprocess
from pathlib import Path

from flask import Flask, request, jsonify, send_from_directory

# --- קבועים ---------------------------------------------------------------
CONFIG_PATH = Path("/etc/rtl_airband/airband.conf")
STATE_PATH = Path("/var/lib/airam/state.json")
MOUNT = "live.mp3"          # שם ה-stream הקבוע ב-Icecast
ICECAST_PORT = 8000
SOURCE_PW = "airam"         # סיסמת source פנימית קבועה (המשתמש לא נחשף אליה)
SAMPLE_RATE = 2.56          # Msps - ערוץ יחיד ממורכז, חלון צר מספיק
GAIN_DEFAULT = 40

APP_DIR = Path(__file__).resolve().parent
app = Flask(__name__, static_folder=str(APP_DIR / "static"))

# פריסטים של נתב"ג / TMA (אפשר לערוך כרצונך)
PRESETS = [
    {"name": "מגדל (Tower)",     "freq": 134.600},
    {"name": "ATIS",             "freq": 132.500},
    {"name": "קרקע מזרח",        "freq": 129.200},
    {"name": "גישה/המראה",       "freq": 120.500},
    {"name": "Tel Aviv Control", "freq": 121.400},
    {"name": "קרקע מערב",        "freq": 118.050},
    {"name": "מסירה (Delivery)", "freq": 121.950},
    {"name": "Guard (חירום)",    "freq": 121.500},
]

DEFAULT_STATE = {"freq": 132.500, "mod": "am", "agc": True, "gain": GAIN_DEFAULT}


# --- בניית קובץ ההגדרות ל-rtl_airband ------------------------------------
def render_config(freq, mod, agc, gain):
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
        f"    centerfreq = {f:.4f};",
        "    channels:",
        "    (",
        "      {",
        f"        freq = {f:.4f};",
        f'        modulation = "{mod}";',
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


def write_config(freq, mod, agc, gain):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(render_config(freq, mod, agc, gain))


def load_state():
    try:
        st = json.loads(STATE_PATH.read_text())
        return {**DEFAULT_STATE, **st}
    except Exception:
        return dict(DEFAULT_STATE)


def save_state(st):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(st))


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
    if not (0.1 <= freq <= 2000.0):
        return jsonify(ok=False, error="תדר מחוץ לטווח (0.1–2000 MHz)"), 400

    mod = "nfm" if str(data.get("mod", "am")).lower() == "nfm" else "am"
    agc = bool(data.get("agc", True))
    try:
        gain = max(0, min(60, int(data.get("gain", GAIN_DEFAULT))))
    except (TypeError, ValueError):
        gain = GAIN_DEFAULT

    write_config(freq, mod, agc, gain)
    save_state({"freq": freq, "mod": mod, "agc": agc, "gain": gain})

    r = subprocess.run(
        ["systemctl", "restart", "rtl_airband"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return jsonify(ok=False, error=(r.stderr or "restart failed").strip()), 500

    return jsonify(ok=True, freq=freq, mod=mod, agc=agc, gain=gain)


if __name__ == "__main__":
    # ודא שקיים קובץ הגדרות התחלתי כדי ש-rtl_airband יעלה
    if not CONFIG_PATH.exists():
        st = load_state()
        write_config(st["freq"], st["mod"], st["agc"], st["gain"])
    app.run(host="0.0.0.0", port=8080)
