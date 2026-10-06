#!/usr/bin/env python3
# ============================================================================
#  AIR-AM - 🩺 בודק ה-RF (PR 2, v2.27.0): צרכן ה-SDR החמישי
# ----------------------------------------------------------------------------
#  רץ כ-root דרך systemd/airam-rfcheck.service:
#      ExecStart=/usr/bin/python3 -I /opt/airam/webtune/rfcheck_probe.py
#  ומחליף את מצב ה-LNA של ה-RSP1B *תוך כדי קליטה* (סבב ABBA), מודד כל סלוט
#  ב-DSP זהה ל-rtl_airband, וכותב שורה לכל סלוט ל-/run/airam-rfcheck/. הוא
#  "טיפש" בכוונה (docs/rf-check-design.md §4.4): בלי סיווג, בלי סטטיסטיקה ובלי
#  המלצה — את כל אלה עושה webtune/rfcheck_analysis.py (פייתון טהור) בתוך
#  airam-web. כך מסלול הקוד שרץ כ-root קטן, ופסק הדין ניתן לשחזור מהשורות.
#
#  ⚠ קובץ *יחיד* בכוונה: ב-Python ≥3.11 הדגל ‎-I גורר ‎-P, כך שתיקיית הסקריפט לא
#  נכנסת ל-sys.path ו-import של קובץ שכן היה נכשל. airam-web **לעולם לא מייבא**
#  את הקובץ הזה (ולא טוען את libsdrplay_api לתהליך שלו).
#
#  תלויות: stdlib בלבד ברמת המודול. numpy ו-SoapySDR (python3-soapysdr) נטענים
#  בעצלתיים — ‎--selftest ואימות הפרמטרים עובדים גם בלעדיהם.
#
#  מקורות שאומתו ישירות (ההפניות בהערות למטה):
#    SoapySDRPlay3 ‏48bd8b4 (הנעוץ ב-install.sh): Settings.cpp, Streaming.cpp,
#      SoapySDRPlay.hpp.
#    SoapySDR 0.8.1: lib/LoggerC.cpp, include/SoapySDR/{Errors,Logger,Constants}.h,
#      python/SoapySDR.in.i (חתימות הקישור לפייתון).
#    RTLSDR-Airband v5.2.0: src/rtl_airband.cpp, src/input-soapysdr.cpp.
#    SDRplay API 3.15: spec.txt, inc/sdrplay_api*.h.
# ============================================================================
import bisect
import collections
import errno
import fnmatch
import json
import math
import os
import platform
import pwd
import re
import signal
import stat
import sys
import time

try:  # אופציונלי ברמת המודול: --selftest מדווח null במקום לקרוס
    import numpy as np
except ImportError:  # pragma: no cover - נבדק דרך subprocess
    np = None

# --- נתיבים -----------------------------------------------------------------
PARAMS_PATH = "/var/lib/airam/rfcheck-params.json"
OUT_DIR = "/run/airam-rfcheck"
SOAPY_RF_MARK = "/usr/local/share/airam/soapysdrplay3.build-sig"
SDRPLAY_API_VERSION_PATH = "/usr/local/share/airam/sdrplay-api.version"
OUT_FILES = ("meta.json", "status.json", "rows.jsonl", "end.json", "diagnose.json")

# --- קודי יציאה (spec §4.3) --------------------------------------------------
EXIT_OK = 0            # stopped / max_sec (וגם אבחון שהסתיים)
EXIT_EXCEPTION = 1     # חריגה לא צפויה (end.json — best effort)
EXIT_PARAMS = 2        # קובץ פרמטרים לא תקין — המכשיר לא נפתח
EXIT_OPEN = 3          # import / פתיחת המכשיר נכשלו
EXIT_DEVICE_LOST = 4   # המכשיר אבד באמצע

# --- SoapySDR (include/SoapySDR/Errors.h:33-56, Constants.h:22, Logger.h:31-39) ---
SOAPY_SDR_TIMEOUT = -1
SOAPY_SDR_OVERFLOW = -4
SOAPY_SDR_NOT_SUPPORTED = -5
SOAPY_SDR_RX = 1
LOG_WARNING = 4        # SOAPY_SDR_WARNING; קטן יותר = חמור יותר
LOG_INFO = 6
LOG_SSI = 9            # "O" על overflow — עובר ל-handler תמיד (LoggerC.cpp:77)
_LOG_NAMES = {1: "FATAL", 2: "CRITICAL", 3: "ERROR", 4: "WARNING", 5: "NOTICE",
              6: "INFO", 7: "DEBUG", 8: "TRACE", 9: "SSI"}

# --- SoapySDRPlay3: מבנה הזרם (SoapySDRPlay.hpp:44-45) ------------------------
# ‏8 מאגרים של עד 65536 דגימות. rx_callback סוגר מאגר כשהחבילה הבאה לא נכנסת
# (Streaming.cpp:108), ו-overflow מסומן כשהמאגר ה-8 נסגר בלי שנקרא
# (Streaming.cpp:101-118) — ה-acquire הבא מרוקן הכול ומחזיר OVERFLOW
# (Streaming.cpp:505-521). readStream מחזיר לכל היותר מאגר אחד לקריאה:
# הוא רוכש מאגר רק כשהקודם נגמר (‎:424-438) ומחזיר min(נותר, numElems).
BUFFER_ELEMS = 65536
NUM_BUFFERS = 8
RATE = 2_560_000                       # app.py SAMPLE_RATE (2.56 Msps)
BUF_S = BUFFER_ELEMS / RATE            # 25.6ms — חסם עליון למשך מאגר
# תקציב עיבוד לסלוט לפני overflow: 7 מאגרים מלאים + אחד בהתמלאות (ר' למעלה).
SLOT_BUDGET_MS = (NUM_BUFFERS - 1) * BUF_S * 1000.0
READ_TIMEOUT_US = 200_000              # מאגר מגיע כל 25.6ms; 200ms = "לא הגיע"
DRAIN_MAX_ITER = 64
# ⚠ הסרת המכשיר *לא* מחזירה NOT_SUPPORTED כשהטבעת ריקה: acquireReadBuffer בודק
# count==0 ומחזיר TIMEOUT *לפני* device_unavailable (Streaming.cpp:524-537), ו-
# ev_callback רק רושם "Device has been removed" (‎:186-193). לכן שני אותות נוספים
# ל-device_lost: שורת היומן הזאת, ו-STALL_SEC בלי אף מאגר. זה כלב-שמירה תפעולי
# (~78 תקופות מאגר; ה-API מזרים ברציפות), לא סף RF.
STALL_SEC = 2.0
STATUS_MIN_INTERVAL = 0.25             # status.json לכל היותר ב-4Hz (spec §4.3)
LOG_CAP = 4096                         # spec §4.4; גלישה => מצב העומס "לא ידוע"
ECHO_MAX = 400                         # הגבלת הדהוד שורות דרייבר ל-journal

# --- DSP: זהה לערוץ של rtl_airband ------------------------------------------
FS = 32767.0                           # fullScale של CS16 (Streaming.cpp:40-43)
# ⚠ ב-python3-soapysdr ‏0.8.1 הפרמטר double &fullScale של getNativeStreamFormat
# נעטף בלי typemap של OUTPUT (python/SoapySDR.in.i) — לא קריא מפייתון. לכן קבוע
# ומקור "driver_source", ולא קוראים ל-getNativeStreamFormat בכלל.
FFT_N = 512
HOP = 160                              # round(fs/WAVE_RATE)=round(2.56e6/16000) (rtl_airband.cpp:419)
# Blackman-Harris 7 איברים, מועתק מ-rtl_airband.cpp:361-367 *כולל* הסיומת f:
# המקור מצהיר double אבל מאתחל מליטרל float — הערך האפקטיבי הוא העיגול ל-float32.
# המכנה הוא N-1 (rtl_airband.cpp:370-371).
_BH7_LITERALS = (0.27105140069342, 0.43329793923448, 0.21812299954311,
                 0.06592544638803, 0.01081174209837, 0.00077658482522,
                 0.00001388721735)
BIN_STEP_HZ = 5000                     # rate/N = 2.56e6/512
DB_FLOOR = 1e-20                       # => ‎-200dB, לא ‎-inf (JSON)

# טבלת LNA GR (dB) של RSP1B/RSP1A ל-60–420MHz, מצבים 0..9 (spec.txt:2279-2293).
# משמשת רק לנקודת הפתיחה של ה-IF ולתצוגה — לא לפסק הדין (design §2.3).
GR_RSP1B_60_420 = (0, 6, 12, 18, 20, 26, 32, 38, 57, 62)
SDRPLAY_RSP1A_ID, SDRPLAY_RSP1B_ID = 255, 6   # inc/sdrplay_api.h:34,38
IFGR_MIN, IFGR_MAX = 20, 59            # Settings.cpp:647; MAX_BB_GR spec.txt:567

# --- טלמטריית PR 1 (patches/soapysdrplay3-airam-rf.patch) ---------------------
# אותם regex לא-מעוגנים כמו ב-app.py (שם _RF_*_RE) — קובץ יחיד, לכן מועתקים;
# tests/test_rfcheck_probe.py שולף את מחרוזות הפורמט מה-patch ומוודא התאמה.
_RF_START_RE = re.compile(r"AIRAM_RF stream=start(?!\w)")
_RF_OVERLOAD_RE = re.compile(r"AIRAM_RF overload=([01])(?!\d)")
_RF_GAIN_RE = re.compile(r"AIRAM_RF gain grdb=(\d+) lna_grdb=(\d+)")
_GR_TIMEOUT_MSG = "Gain reduction update timeout."            # Settings.cpp:622
_AGC_IFGR_MSG = "Not updating IFGR gain because AGC is enabled"  # Settings.cpp:594
_REMOVED_MSGS = ("Device has been removed", "Master stream has been removed")  # Streaming.cpp:191,200

# --- אבחון (spec §4.7): משכים. ניתנים לדריסה רק מבדיקות (kwarg diag_timing) ---
# התדר (ATIS ‏132.5, spec §4.7) מגיע מ-airam-web בפרמטרים כמו בכל שלב — לא קבוע כאן.
DIAG_MAX_SEC = 150
DIAG_TIMING = {
    "d3_reps": 5, "d3_buffers": 10,
    "d4_ifgr": (59, 53, 47, 41, 35, 29), "d4_sec": 1.0,
    "d5_ifgr": tuple(range(59, 19, -3)), "d5_sec": 1.0,
    "d6_toggles": 10, "d6_buffers": 20,
    "d7_agc_sec": 5.0, "d7_step_sec": 1.5,
    "d8_sec": 20.0, "d8_states": (0, 4, 7),
    "settle_buffers": 4,               # גודל ה"מצב היציב" בסוף כל מסלול
}
DIAG_REF_LNA, DIAG_REF_IFGR = 4, 40


# ============================================================================
#  קובץ הפרמטרים: פתיחה בטוחה + whitelist (spec §4.2)
# ============================================================================
class ParamError(Exception):
    """שגיאת פרמטרים. נושאת *קוד בלבד* — לעולם לא תוכן מהקובץ (גם לא שם מפתח זר)."""

    def __init__(self, code, run_id=None):
        super().__init__(code)
        self.code = code
        self.run_id = run_id


PARAMS_MAX_BYTES = 4096
_RUN_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_PARAM_KEYS = ("v", "run_id", "phase", "ref", "freq_hz", "center_hz", "rate", "states",
               "ifgr_start", "notch_base", "notch_alternate", "guard_buffers",
               "notch_guard_buffers", "measure_buffers", "ratchet_db", "nb_offsets_hz",
               "rail_code", "max_sec", "ovl_margin_buffers")
MAX_STATES = 7    # החלטת המשתמש (2026-10-06): (0,2,4,6,7,8) ∪ {נוכחי}, עד 7


def _default_allowed_uids():
    uids = {0}
    try:
        uids.add(pwd.getpwnam("airam").pw_uid)
    except KeyError:
        pass
    return uids


def load_params(path=PARAMS_PATH, allowed_uids=None):
    """פותח את קובץ הפרמטרים בזהירות ומחזיר dict מאומת; זורק ParamError(code).

    ‏O_NOFOLLOW: symlink => ELOOP (לא עוקבים אחרי קישור שהשתיל משתמש אחר).
    ‏O_NONBLOCK: FIFO לא חוסם את ה-open; fstat דוחה כל דבר שאינו קובץ רגיל.
    הבעלים: root או airam בלבד (airam-web כותב אותו ב-_atomic_write)."""
    if allowed_uids is None:
        allowed_uids = _default_allowed_uids()
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    try:
        fd = os.open(path, flags)
    except OSError as e:
        raise ParamError("open:" + errno.errorcode.get(e.errno, "E?"))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ParamError("not_regular")
        if st.st_size > PARAMS_MAX_BYTES:
            raise ParamError("too_large")
        if st.st_uid not in allowed_uids:
            raise ParamError("owner")
        chunks, total = [], 0
        while True:
            try:
                b = os.read(fd, PARAMS_MAX_BYTES + 1 - total)
            except OSError:
                raise ParamError("read")
            if not b:
                break
            chunks.append(b)
            total += len(b)
            if total > PARAMS_MAX_BYTES:
                raise ParamError("too_large")
    finally:
        os.close(fd)
    try:
        text = b"".join(chunks).decode("utf-8")
        obj = json.loads(text, object_pairs_hook=_no_dup_pairs,
                         parse_constant=_reject_constant)
    except ParamError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ParamError("json")
    return validate_params(obj)


def _no_dup_pairs(pairs):
    d = {}
    for k, v in pairs:
        if k in d:
            raise ParamError("json")      # מפתח כפול: איזה מהשניים "נכון"? לא מנחשים
        d[k] = v
    return d


def _reject_constant(_name):
    raise ParamError("json")              # NaN/Infinity אינם JSON תקני


def _is_int(v):
    return type(v) is int                 # bool הוא תת-מחלקה של int — לא מקבלים אותו


def _int_in(p, key, lo, hi):
    v = p[key]
    if not _is_int(v):
        raise ParamError("type:" + key, p.get("_rid"))
    if not lo <= v <= hi:
        raise ParamError("range:" + key, p.get("_rid"))
    return v


def validate_params(obj):
    """whitelist מלא (spec §4.2 + החלטות המשתמש). מחזיר עותק מנורמל.
    מפתח לא מוכר => "unknown_key" (בלי שמו — תוכן הקובץ לא מהודהד)."""
    if not isinstance(obj, dict):
        raise ParamError("not_object")
    rid = obj.get("run_id")
    rid = rid if isinstance(rid, str) and _RUN_ID_RE.match(rid) else None
    for k in obj:
        if k not in _PARAM_KEYS:
            raise ParamError("unknown_key", rid)
    for k in _PARAM_KEYS:
        if k not in obj:
            raise ParamError("missing:" + k, rid)
    p = dict(obj)
    p["_rid"] = rid
    if not (_is_int(p["v"]) and p["v"] == 1):
        raise ParamError("version", rid)
    if rid is None:
        raise ParamError("type:run_id")
    if p["phase"] not in ("lna", "notch", "diagnose"):
        raise ParamError("range:phase", rid)
    if p["ref"] not in ("tower", "atis"):
        raise ParamError("range:ref", rid)
    freq = _int_in(p, "freq_hz", 108_000_000, 137_000_000)
    center = p["center_hz"]
    if not _is_int(center):
        raise ParamError("type:center_hz", rid)
    if abs(center - freq) > 1_000_000:
        raise ParamError("range:center_hz", rid)
    if not (_is_int(p["rate"]) and p["rate"] == RATE):
        raise ParamError("range:rate", rid)
    # ה-bin המדויק (spec §4.6 "assert integer"): ההיסט חייב להיות כפולה של rate/N
    if (center - freq) % BIN_STEP_HZ:
        raise ParamError("center_bin", rid)
    states = p["states"]
    if not isinstance(states, list) or not states:
        raise ParamError("type:states", rid)
    if not all(_is_int(s) and 0 <= s <= 9 for s in states):
        raise ParamError("range:states", rid)
    if len(states) > MAX_STATES or len(set(states)) != len(states) or states != sorted(states):
        raise ParamError("range:states", rid)
    if p["phase"] == "notch" and len(states) != 1:
        raise ParamError("range:states", rid)
    ig = p["ifgr_start"]
    if not isinstance(ig, dict) or set(ig) != {str(s) for s in states}:
        raise ParamError("type:ifgr_start", rid)
    if not all(_is_int(v) and IFGR_MIN <= v <= IFGR_MAX for v in ig.values()):
        raise ParamError("range:ifgr_start", rid)
    for k in ("notch_base", "notch_alternate"):
        if type(p[k]) is not bool:
            raise ParamError("type:" + k, rid)
    if p["notch_alternate"] != (p["phase"] == "notch"):
        raise ParamError("range:notch_alternate", rid)
    _int_in(p, "guard_buffers", 0, 8)
    _int_in(p, "notch_guard_buffers", 0, 20)
    _int_in(p, "measure_buffers", 2, 16)
    _int_in(p, "ratchet_db", 1, 20)
    _int_in(p, "rail_code", 16384, 32768)
    _int_in(p, "max_sec", 5, DIAG_MAX_SEC if p["phase"] == "diagnose" else 180)
    _int_in(p, "ovl_margin_buffers", 0, 4)
    nb = p["nb_offsets_hz"]
    if not isinstance(nb, list) or len(nb) > 8:
        raise ParamError("type:nb_offsets_hz", rid)
    for off in nb:
        if not _is_int(off):
            raise ParamError("type:nb_offsets_hz", rid)
        if off % BIN_STEP_HZ or not 40_000 <= abs(off) <= 600_000:
            raise ParamError("range:nb_offsets_hz", rid)
    # כל ה-bins בתוך Nyquist: bin מעבר ל-±N/2 היה מתקפל לצד השני ומודד תדר אחר
    k_ch = (freq - center) // BIN_STEP_HZ
    for k in [k_ch] + [k_ch + off // BIN_STEP_HZ for off in nb]:
        if not -FFT_N // 2 < k < FFT_N // 2:
            raise ParamError("bins", rid)
    p.pop("_rid")
    p["states"] = list(states)
    p["ifgr_start"] = {str(k): int(v) for k, v in ig.items()}
    p["nb_offsets_hz"] = list(nb)
    return p


# ============================================================================
#  פלט: root כותב *רק* ל-OUT_DIR (design §4.3)
# ============================================================================
class OutDir:
    """JSON אטומי (tmp+rename, 0644) ו-rows.jsonl ב-append. tmpfs => בלי fsync."""

    def __init__(self, path=OUT_DIR):
        self.path = str(path)
        self._rows_fd = None

    def ensure(self):
        os.makedirs(self.path, mode=0o755, exist_ok=True)

    def reset(self):
        self.ensure()
        for name in OUT_FILES:
            try:
                os.unlink(os.path.join(self.path, name))
            except FileNotFoundError:
                pass
        for name in os.listdir(self.path):
            if name.startswith(".") and ".tmp." in name:
                try:
                    os.unlink(os.path.join(self.path, name))
                except OSError:
                    pass

    def write_json(self, name, obj):
        data = (json.dumps(obj, ensure_ascii=False, allow_nan=False,
                           separators=(",", ":")) + "\n").encode("utf-8")
        tmp = os.path.join(self.path, ".%s.tmp.%d" % (name, os.getpid()))
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644)
        try:
            os.fchmod(fd, 0o644)
            view = memoryview(data)
            while view:
                n = os.write(fd, view)
                view = view[n:]
        finally:
            os.close(fd)
        os.replace(tmp, os.path.join(self.path, name))

    def append_row(self, obj):
        if self._rows_fd is None:
            self._rows_fd = os.open(os.path.join(self.path, "rows.jsonl"),
                                    os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC,
                                    0o644)
            os.fchmod(self._rows_fd, 0o644)
        line = (json.dumps(obj, ensure_ascii=False, allow_nan=False,
                           separators=(",", ":")) + "\n").encode("utf-8")
        os.write(self._rows_fd, line)    # O_APPEND + כתיבה אחת = שורה שלמה לקורא

    def close(self):
        if self._rows_fd is not None:
            try:
                os.close(self._rows_fd)
            except OSError:
                pass
            self._rows_fd = None


# ============================================================================
#  לוג SoapySDR: handler רזה + פענוח בת'רד הראשי (spec §4.4 צעד 2)
# ============================================================================
class LogSink:
    """ה-handler רץ על ת'רדים של ה-SDRplay API (ה-SWIG נבנה עם ‎-threads ותופס את
    ה-GIL). הוא רק מוסיף ל-deque — כל פענוח קורה בת'רד הראשי. הוא **לעולם לא
    זורק**: חריגה ב-director של SWIG הופכת ל-DirectorMethodException בקוד
    נייטיבי על ת'רד של ה-API (python/SoapySDR.in.i:192-196)."""

    def __init__(self, clock, cap=LOG_CAP):
        self._q = collections.deque()
        self._cap = cap
        self._clock = clock
        self.dropped = 0        # מעבר ל-cap: נספר, ומצב העומס הופך ל"לא ידוע"

    def handler(self, level, message):
        try:
            if len(self._q) < self._cap:
                self._q.append((self._clock(), int(level), str(message)))
            else:
                self.dropped += 1
        except BaseException:  # noqa: BLE001 - חובה: אסור לזרוק לקוד נייטיבי
            pass

    def drain(self):
        out = []
        q = self._q
        while True:
            try:
                out.append(q.popleft())
            except IndexError:
                return out


class Telemetry:
    """מצב העומס לאורך זמן, משורות AIRAM_RF בלבד (PR 1).

    ציר זמן של (t, state) עם state ∈ {None, False, True}: מתחיל ב-None ("לא ידוע").
    רק "AIRAM_RF stream=start" (נרשמת ב-activateStream *לפני* sdrplay_api_Init,
    ולכן לפני כל אירוע של הסשן — ה-patch, Streaming.cpp) מעבירה ל-False: זו ההוכחה
    היחידה שהמודול המתוקן נטען *ושה-log שלו מגיע אלינו*. סימן-הבנייה מוכיח רק בנייה.
    קצוות overload=1/0 הם ראיה חיובית בכל מקרה. גלישת ה-deque => None עד הקצה הבא.
    (זה מחליף את "המצב ההתחלתי False" של spec §4.5 צעד 5 — החלטת משתמש 6.)"""

    GAIN_KEEP = 512

    def __init__(self, t_open):
        self.timeline = [(t_open, None)]
        self.stream_start = False
        self.lines = 0
        self.gain_lines = 0
        self.ovl_lines = 0
        self.lna_grdb_seen = set()
        self.gr_timeouts = []
        self.removed = False
        self.agc_ifgr_ignored = 0
        self.gain_events = collections.deque(maxlen=self.GAIN_KEEP)
        self.ovl_events = collections.deque(maxlen=self.GAIN_KEEP)

    def feed(self, t, level, msg):
        if "AIRAM_RF" in msg:
            self.lines += 1
            if _RF_START_RE.search(msg):
                self.stream_start = True
                self._set(t, False)
                return
            m = _RF_OVERLOAD_RE.search(msg)
            if m:
                on = m.group(1) == "1"
                self.ovl_lines += 1
                self.ovl_events.append((t, on))
                self._set(t, on)
                return
            m = _RF_GAIN_RE.search(msg)
            if m:
                self.gain_lines += 1
                grdb, lna = int(m.group(1)), int(m.group(2))
                if len(self.lna_grdb_seen) < 64:
                    self.lna_grdb_seen.add(lna)
                self.gain_events.append((t, grdb, lna))
            return
        if _GR_TIMEOUT_MSG in msg:
            self.gr_timeouts.append(t)
        elif _AGC_IFGR_MSG in msg:
            self.agc_ifgr_ignored += 1
        elif any(s in msg for s in _REMOVED_MSGS):
            self.removed = True

    def mark_unknown(self, t):
        self._set(t, None)

    def _set(self, t, state):
        # ת'רדים שונים של ה-API עלולים להגיע בסדר הפוך מעט — הכנסה ממוינת
        idx = bisect.bisect_right([x[0] for x in self.timeline], t)
        self.timeline.insert(idx, (t, state))

    def state_over(self, a, b):
        """True אם עומס היה "דלוק" בחלק כלשהו של [a, b]; None אם חלק לא ידוע (ואין
        True); אחרת False. ⚠ "לא ידוע" לעולם לא מתורגם ל-False (§12)."""
        if b < a:
            return None
        seen = set()
        tl = self.timeline
        for i, (ti, si) in enumerate(tl):
            t_next = tl[i + 1][0] if i + 1 < len(tl) else math.inf
            if ti <= b and t_next > a:
                seen.add(si)
        if not seen:
            return None
        if True in seen:
            return True
        if None in seen:
            return None
        return False

    def prune(self, before):
        """משמיט קטעים שהסתיימו לפני `before`, ושומר את הקטע שחוצה אותו."""
        tl = self.timeline
        k = 0
        while k + 1 < len(tl) and tl[k + 1][0] <= before:
            k += 1
        if k:
            del tl[:k]
        self.gr_timeouts = [t for t in self.gr_timeouts if t >= before]

    def gr_timeout_between(self, a, b):
        return any(a <= t <= b for t in self.gr_timeouts)


def ovl_flags(telem, enabled, sw_start, t_first, t_last, margin_buffers):
    """(ovl, ovl_edge) לסלוט (spec §4.6).

    ovl: האם העומס היה דלוק בחלון [t_first − (1+margin)·25.6ms, t_last] — כלומר
    משך המאגר הנמדד הראשון (הדגימות שלו נאספו ב-25.6ms שלפני שהקריאה חזרה) ועוד
    margin מאגרים. ovl_edge: היה דלוק *רק* בחלון ההחלפה/ההשלכה שלפני כן — מידע
    בלבד. enabled=False (אין handler או סימן-בנייה) => (None, None)."""
    if not enabled or t_first is None:
        return None, None
    win_a = t_first - (1 + margin_buffers) * BUF_S
    ovl = telem.state_over(win_a, t_last)
    if ovl is None:
        return None, None
    if ovl is True:
        return True, False
    edge = telem.state_over(sw_start, win_a)
    if edge is None:
        return False, None
    return False, bool(edge)


# ============================================================================
#  DSP פר-סלוט (spec §4.6)
# ============================================================================
def bh7_window(n=FFT_N, float32_coeffs=True):
    """חלון ה-BH7 של rtl_airband (rtl_airband.cpp:361-371), float64."""
    if np is None:
        raise RuntimeError("numpy")
    a = [float(np.float32(v)) for v in _BH7_LITERALS] if float32_coeffs else list(_BH7_LITERALS)
    i = np.arange(n, dtype=np.float64)
    w = np.zeros(n, dtype=np.float64)
    for k, ak in enumerate(a):
        w += ((-1) ** k) * ak * np.cos(2.0 * np.pi * k * i / (n - 1))
    return w


def _db10(v):
    return 10.0 * math.log10(max(float(v), DB_FLOOR))


def _r2(v):
    if v is None:
        return None
    v = float(v)
    return round(v, 2) if math.isfinite(v) else None


def _r3(v):
    if v is None:
        return None
    v = float(v)
    return round(v, 3) if math.isfinite(v) else None


class SlotDSP:
    """DFT בחלון גולש על ה-bin של הערוץ + bins שכנים, בדיוק כמו ערוץ של rtl_airband
    (FFT 512, BH7, hop 160) — אבל על ה-bin *המדויק* (rtl_airband בוחר ceil(x−1),
    config.cpp:670). מכפלת מטריצה-וקטור על sliding_window_view, לא FFT מלא.

    נרמול: חלוקה ב-Σw·FS => נשא טהור באמפליטודה A (ביחידות full-scale) נותן |z|=A.
    complex64: הדליפה האריתמטית לבין שכנים נמדדה ‎≈−160dBFS — זניח."""

    def __init__(self, freq_hz, center_hz, rate=RATE, nb_offsets_hz=(), rail_code=32767):
        if np is None:
            raise RuntimeError("numpy")
        bin_hz = rate / FFT_N
        d = freq_hz - center_hz
        k = d / bin_hz
        if abs(k - round(k)) > 1e-9:
            raise ValueError("bin")      # spec: "assert that the result is an integer"
        self.k_ch = int(round(k))
        self.bins = [self.k_ch] + [self.k_ch + int(round(off / bin_hz)) for off in nb_offsets_hz]
        self.rail = int(rail_code)
        self.w = bh7_window(FFT_N)
        i = np.arange(FFT_N, dtype=np.float64)
        ks = np.asarray(self.bins, dtype=np.float64)
        taps = (self.w[:, None] * np.exp(-2j * np.pi * np.outer(i, ks) / FFT_N)
                / (self.w.sum() * FS))
        self.Tm = taps.astype(np.complex64)
        self.has_nb = len(self.bins) > 1

    def compute(self, raw, n_list, want_nb=True):
        """raw: int16 משולב (I,Q,...) באורך ≥2·Σn. מחזיר dict של שדות הסלוט, או None
        כשאין מספיק דגימות לשתי מסגרות."""
        n = int(sum(n_list))
        if n < FFT_N + HOP:
            return None
        r = raw[:2 * n]
        a32 = np.abs(r.astype(np.int32))           # int32: כולל ‎−32768 (|x|=32768)
        clip = int(np.count_nonzero(a32 >= self.rail))
        peak = int(a32.max())
        rf = r.astype(np.float32)
        r64 = rf.astype(np.float64)
        p_wb = _db10(float(np.dot(r64, r64)) / n / (FS * FS))
        x = rf.view(np.complex64)
        fr = np.lib.stride_tricks.sliding_window_view(x, FFT_N)[::HOP]
        F = fr.shape[0]                             # = (n−N)//HOP + 1
        Z = fr @ self.Tm                            # (F, nbins)
        z = Z[:, 0]
        p = (z.real.astype(np.float64) ** 2 + z.imag.astype(np.float64) ** 2)
        mag = np.sqrt(p)
        half = F // 2
        out = {
            "c_tot": _db10(p.mean()),
            "c_tot_h1": _db10(p[:half].mean()),
            "c_tot_h2": _db10(p[half:].mean()),
            "c_car": 20.0 * math.log10(max(float(mag.mean()), 1e-10)),
            "p_wb": p_wb,
            "clip": clip,
            "peak": peak,
            "frames": F,
        }
        # b_tot: c_tot לכל מאגר נמדד, לפי המאגר שבו *מתחילה* המסגרת
        cum = np.cumsum([0] + [int(v) for v in n_list])
        starts = np.arange(F) * HOP
        which = np.searchsorted(cum, starts, side="right") - 1
        b_tot = []
        for j in range(len(n_list)):
            sel = p[which == j]
            b_tot.append(_db10(sel.mean()) if sel.size else None)
        out["b_tot"] = b_tot
        if want_nb and self.has_nb:
            nbp = (np.abs(Z[:, 1:]).astype(np.float64)) ** 2
            out["n_nb"] = min(_db10(v) for v in np.median(nbp, axis=0))
        else:
            out["n_nb"] = None
        # מעטפת — מידע בלבד (לא לפסק הדין), ב-dB כדי שיחסים יהיו הפרשים:
        # e_std−e_mean ≈ עומק האפנון, e_p99−e_mean = מקדם שיא.
        out["e_mean"] = out["c_car"]
        out["e_std"] = 20.0 * math.log10(max(float(mag.std()), 1e-10))
        out["e_p99"] = 20.0 * math.log10(max(float(np.percentile(mag, 99)), 1e-10))
        return out


# ============================================================================
#  עטיפת SoapySDR (backend + radio). FakeRadio ב-tests/rfsim.py זהה בממשק.
# ============================================================================
class SoapyBackend:
    """טוען את SoapySDR בעצלתיים. כל חתימה כאן אומתה מול python/SoapySDR.in.i של
    SoapySDR 0.8.1 (python3-soapysdr)."""

    def __init__(self):
        self.mod = None

    def load(self):
        import SoapySDR  # noqa: PLC0415 - בכוונה עצלני
        self.mod = SoapySDR

    def versions(self):
        m = self.mod
        out = {"soapy_api": None, "soapy_lib": None}
        for k, fn in (("soapy_api", "getAPIVersion"), ("soapy_lib", "getLibVersion")):
            try:
                out[k] = str(getattr(m, fn)())
            except Exception:  # noqa: BLE001
                pass
        return out

    def set_log_level_info(self):
        # רמת INFO: שם נרשמות שורות AIRAM_RF (ה-patch) — SoapySDR_log מסנן לפי
        # registeredLogLevel (LoggerC.cpp:77,83,104-107).
        self.mod.setLogLevel(self.mod.SOAPY_SDR_INFO)

    def register_log_handler(self, cb):
        # SoapySDR.registerLogHandler הוא עטיפת פייתון (SoapySDR.in.i:245-263) מעל
        # director שקורא handler(level, message) מכל ת'רד שרושם.
        if not hasattr(self.mod, "registerLogHandler"):
            return False
        self.mod.registerLogHandler(cb)
        return True

    def unregister_log_handler(self):
        try:
            self.mod.registerLogHandler(None)    # מחזיר את ה-handler הנייטיבי
        except Exception:  # noqa: BLE001
            pass

    def open(self, notch_base):
        # Device(kwargs) => Device.make (SoapySDR.in.i:298-300). רק driver ו-
        # rfnotch_ctrl, מבוליאן מאומת. לעולם לא rfgain_sel: writeSetting שלו
        # מחכה ל-grChanged גם בלי שינוי (Settings.cpp:1628-1652); אנחנו משתמשים
        # ב-setGain("RFGR") שמדלג על no-op (Settings.cpp:598-604).
        dev = self.mod.Device({"driver": "sdrplay",
                               "rfnotch_ctrl": "true" if notch_base else "false"})
        return SoapyRadio(self.mod, dev)

    def env_info(self):
        m = self.mod
        out = {}
        try:
            out["search_paths"] = [str(s) for s in m.listSearchPaths()][:32]
        except Exception as e:  # noqa: BLE001
            out["search_paths"] = {"error": type(e).__name__}
        try:
            out["modules"] = [str(s) for s in m.listModules()][:64]
        except Exception as e:  # noqa: BLE001
            out["modules"] = {"error": type(e).__name__}
        return out


class SoapyRadio:
    """המכשיר. read() מחזיר את ret של readStream (שלילי = קוד שגיאה של SoapySDR)."""

    def __init__(self, mod, dev):
        self.m = mod
        self.dev = dev
        self.st = None
        self._active = False

    def read_setting(self, key):
        return str(self.dev.readSetting(key))

    def write_setting(self, key, value):
        self.dev.writeSetting(key, str(value))

    def set_sample_rate(self, rate):
        self.dev.setSampleRate(SOAPY_SDR_RX, 0, float(rate))

    def set_frequency(self, hz):
        self.dev.setFrequency(SOAPY_SDR_RX, 0, float(hz))

    def set_freq_correction(self, ppm):
        self.dev.setFrequencyCorrection(SOAPY_SDR_RX, 0, float(ppm))

    def set_agc(self, on):
        # setGainMode(true) => AGC_CTRL_EN; false => AGC_DISABLE (Settings.cpp:553-566)
        self.dev.setGainMode(SOAPY_SDR_RX, 0, bool(on))

    def set_gain(self, name, value):
        # תוך הזרמה: Update(Tuner_Gr) ואז המתנה עד 500ms ל-grChanged של חבילה
        # (Settings.cpp:605-623; updateTimeout SoapySDRPlay.hpp:323). IFGR מתעלם
        # תחת AGC עם אזהרה (‎:584-595). ערך זהה => בלי Update בכלל (‎:586,599).
        self.dev.setGain(SOAPY_SDR_RX, 0, str(name), float(value))

    def get_gain(self, name):
        return float(self.dev.getGain(SOAPY_SDR_RX, 0, str(name)))

    def start_stream(self):
        # CS16 = התצורה של rtl_airband (Streaming.cpp:247-252); activateStream קורא
        # ל-sdrplay_api_Init (‎:327-381), ולפניו ה-patch רושם "AIRAM_RF stream=start".
        self.st = self.dev.setupStream(SOAPY_SDR_RX, "CS16", [0])
        ret = self.dev.activateStream(self.st)
        if ret != 0:
            raise RuntimeError("activateStream")
        self._active = True

    def read(self, buf, n, timeout_us):
        # readStream(stream, [buff], numElems, flags, timeoutUs) => StreamResult
        # (SoapySDR.in.i:323-331,363-365); הכתובת נשלפת מ-__array_interface__.
        sr = self.dev.readStream(self.st, [buf], int(n), 0, int(timeout_us))
        return int(sr.ret)

    def get_sample_rate(self):
        return float(self.dev.getSampleRate(SOAPY_SDR_RX, 0))

    def get_bandwidth(self):
        return float(self.dev.getBandwidth(SOAPY_SDR_RX, 0))

    def stream_formats(self):
        return [str(f) for f in self.dev.getStreamFormats(SOAPY_SDR_RX, 0)]

    def gain_range(self, name):
        r = self.dev.getGainRange(SOAPY_SDR_RX, 0, str(name))
        return [float(r.minimum()), float(r.maximum()), float(r.step())]

    def setting_keys(self):
        return [str(a.key) for a in self.dev.getSettingInfo()]

    def info(self):
        hw = {}
        try:
            kw = self.dev.getHardwareInfo()
            hw = {str(k): str(kw[k]) for k in kw.keys()}
        except Exception:  # noqa: BLE001
            pass
        return {"driver": str(self.dev.getDriverKey()),
                "hardware": str(self.dev.getHardwareKey()), "info": hw}

    def close(self):
        if self.st is not None:
            for fn in ("deactivateStream", "closeStream"):
                try:
                    getattr(self.dev, fn)(self.st)
                except Exception:  # noqa: BLE001
                    pass
            self.st = None
        try:
            self.dev.close()     # Device.unmake (SoapySDR.in.i:352-356) — שחרור ה-API
        except Exception:  # noqa: BLE001
            pass


# ============================================================================
#  הבודק
# ============================================================================
class StopFlag:
    """דגל עצירה. bool פשוט — בטוח ל-signal handler (בניגוד ל-Event עם מנעול)."""

    def __init__(self):
        self.flag = False

    def set(self):
        self.flag = True

    def is_set(self):
        return self.flag


class StopRequested(Exception):
    pass


class MaxSec(Exception):
    pass


class SlotLimit(Exception):
    """בדיקות בלבד (max_slots)."""


class DeviceLost(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class SetupError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _read_small(path, limit=256):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        return os.read(fd, limit).decode("utf-8", "replace")
    except OSError:
        return None
    finally:
        os.close(fd)


def _gr_table_for(info, center_hz):
    hw = (info or {}).get("hardware", "")
    hwver = ((info or {}).get("info") or {}).get("sdrplay_api_hw_version", "")
    known = hw in ("RSP1B", "RSP1A") or hwver in (str(SDRPLAY_RSP1B_ID), str(SDRPLAY_RSP1A_ID))
    if known and 60_000_000 <= center_hz <= 420_000_000:
        return list(GR_RSP1B_60_420)
    return None


def _pctl(vals, q):
    v = sorted(x for x in vals if x is not None)
    if not v:
        return None
    k = (len(v) - 1) * q
    lo, hi = int(math.floor(k)), int(math.ceil(k))
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def _median(vals):
    return _pctl(vals, 0.5)


def settle_index(traj, steady_n=4, expand=True):
    """האינדקס הראשון j שממנו *כל* הערכים בתוך פס היציבות של `steady_n` האחרונים.

    expand=False: הפס הוא [min, max] של המצב היציב, כלשון spec §4.7 (D3).
    ⚠ עם רעש בלבד, דגימה יציבה נופלת מחוץ ל-[min,max] של 4 דגימות iid בהסתברות
    ~40% — ההגדרה המילולית מנפחת את זמן ההתייצבות. expand=True מרחיב את הפס
    במרווח *הנמדד* שלו (max−min) מכל צד; לא סף מומצא — קנה מידה שנמדד באותו מסלול.
    None => לא ניתן לחשב."""
    v = [x for x in traj]
    if len(v) < steady_n + 1 or any(x is None for x in v):
        return None
    st = v[-steady_n:]
    lo, hi = min(st), max(st)
    if expand:
        r = hi - lo
        lo, hi = lo - r, hi + r
    for j in range(len(v)):
        if all(lo <= x <= hi for x in v[j:]):
            return j
    return len(v) - steady_n


def rail_pileup(hist, peak_max):
    """קוד המסילה מהיסטוגרמת |קוד| (D5), או None כשאין "ערימה".

    ערימה = קוד אחד בטווח ‎≥0.98·max שמחזיק *רוב* הספירות בטווח, או שני קודים סמוכים
    שמחזיקים יחד את הרוב (מסילה דו-צדדית: ‎+32767 ו-‎|−32768|=32768 ב-int16). זה
    קריטריון מבני — ADC חתוך נערם על המסילה, זנב רגיל יורד בהדרגה — ולא סף מספרי.
    במקרה הדו-צדדי מוחזר הקטן משניהם, כי הבודק סופר ‎|x| ≥ rail_code. זו *הצעה* לבן-
    אדם, וההיסטוגרמה הגולמית נשמרת לצידה."""
    if not peak_max:
        return None
    rng = sorted(((c, v) for v, c in hist.items() if v >= 0.98 * peak_max), reverse=True)
    if not rng:
        return None
    total = sum(c for c, _ in rng)
    c1, v1 = rng[0]
    if c1 > total - c1:
        return v1
    if len(rng) >= 2:
        c2, v2 = rng[1]
        if abs(v1 - v2) == 1 and c1 + c2 > total - c1 - c2:
            return min(v1, v2)
    return None


class Probe:
    """סלוט = (החלפת רווח, ניקוז, השלכה, מדידה, לוג, מדדים) — spec §4.5."""

    def __init__(self, params, backend, out, *, clock=time.monotonic, wall=time.time,
                 stop=None, process_hook=None, measure_hook=None,
                 marker_path=SOAPY_RF_MARK, api_version_path=SDRPLAY_API_VERSION_PATH,
                 max_slots=None, diag_timing=None, stderr=None):
        self.p = params
        self.backend = backend
        self.out = out
        self.clock = clock
        self.wall = wall
        self.stop = stop or StopFlag()
        self.process_hook = process_hook
        self.measure_hook = measure_hook
        self.marker_path = marker_path
        self.api_version_path = api_version_path
        self.max_slots = max_slots
        self.dt = dict(DIAG_TIMING, **(diag_timing or {}))
        self.err = stderr if stderr is not None else sys.stderr
        self.run_id = params["run_id"]
        self.radio = None
        self.sink = None
        self.telem = None
        self.handler = False
        self.marker = False
        self.build_sig = None
        self.meta = None
        self.t0 = None
        # מונים
        self.slots = 0
        self.cycles = 0
        self.overflows = 0
        self.drain_overflows = 0
        self.read_errors = 0
        self.rates = []
        self.proc_hist = collections.deque(maxlen=8)
        self.proc_all = []
        self.light_mode = False
        self.bias_t_forced_off = False
        self.log_dropped = 0
        self._seen_drops = 0
        self._last_pump = None
        self._echoed = 0
        self._last_status = None
        self._last_ok_read = None
        self._emit_rows = True
        self._phase_status = "opening"
        self._gr_timeout_slots = []
        self.dsp = None
        self.diag = None
        self._diag_step = None
        self.cur = None            # (lna, ifgr) המיושם בפועל
        self.notch_cur = bool(params["notch_base"])
        self.state_now = None
        # מצב פר-מפתח (lna, או "lna:notch" בשלב המסנן)
        self.ifgr_cur = {}
        self.epoch = {}
        self.ratchets = {}
        if np is not None:
            self.scratch = np.empty(2 * BUFFER_ELEMS, dtype=np.int16)
            self.meas = np.empty(2 * BUFFER_ELEMS * params["measure_buffers"], dtype=np.int16)

    # ---------------------------------------------------------------- כללי
    def _say(self, text):
        try:
            self.err.write("[airam-rfcheck] %s\n" % text)
            self.err.flush()
        except Exception:  # noqa: BLE001
            pass

    def _elapsed(self):
        return self.clock() - self.t0

    def _check_stop(self):
        if self.stop.is_set():
            raise StopRequested()

    def _check_time(self, limit=None):
        self._check_stop()
        if self._elapsed() >= (self.p["max_sec"] if limit is None else limit):
            raise MaxSec()

    def _pump_logs(self):
        if self.sink is None:
            return
        now = self.clock()
        if self.sink.dropped > self._seen_drops:
            self.telem.mark_unknown(self._last_pump if self._last_pump is not None else now)
            self.log_dropped = self.sink.dropped
            self._seen_drops = self.sink.dropped
        for t, level, msg in self.sink.drain():
            self.telem.feed(t, level, msg)
            # הדהוד ל-journal: ה-handler שלנו החליף את ה-handler הנייטיבי שכתב ל-stderr
            if (level <= LOG_WARNING or "AIRAM_RF" in msg) and level != LOG_SSI:
                if self._echoed < ECHO_MAX:
                    self._say("%s %s" % (_LOG_NAMES.get(level, str(level)), msg[:200]))
                self._echoed += 1
        self._last_pump = now
        if self.telem.removed:
            raise DeviceLost("removed")

    def _status(self, phase=None, force=False, **extra):
        if phase:
            self._phase_status = phase
        now = self.clock()
        if not force and self._last_status is not None and now - self._last_status < STATUS_MIN_INTERVAL:
            return
        self._last_status = now
        st = {"run_id": self.run_id, "t_wall": _r3(self.wall()), "phase": self._phase_status,
              "cycle": self.cycles, "slot": self.slots, "state": self.state_now,
              "ifgr": dict(self.ifgr_cur), "rows": self.slots, "overflows": self.overflows,
              "gr_timeouts": len(self._gr_timeout_slots),
              "telemetry_lines": self.telem.lines if self.telem else 0}
        if self._diag_step is not None:
            st["diag_step"] = self._diag_step
        st.update(extra)
        try:
            self.out.write_json("status.json", st)
        except OSError as e:
            self._say("status.json: %s" % e.__class__.__name__)

    # ---------------------------------------------------------------- קריאה
    def _read(self, dst, timeout_us):
        self._check_stop()
        ret = self.radio.read(dst, BUFFER_ELEMS, timeout_us)
        now = self.clock()
        self._pump_logs()
        if ret > 0:
            self._last_ok_read = now
        elif ret == SOAPY_SDR_NOT_SUPPORTED:
            raise DeviceLost("not_supported")
        elif ret != SOAPY_SDR_OVERFLOW and timeout_us > 0:
            if ret != SOAPY_SDR_TIMEOUT:
                self.read_errors += 1
            if self._last_ok_read is not None and now - self._last_ok_read > STALL_SEC:
                raise DeviceLost("stream_stall")
        return ret, now

    def _drain(self):
        """ניקוז עד TIMEOUT: כל המאגרים *המלאים* כרגע הם מלפני ההחלפה (Streaming.cpp:
        524-531 — timeoutUs=0 => "ריק עכשיו"). OVERFLOW (טבעת מלאה) => נספר וממשיכים:
        ה-acquire כבר רוקן הכול (‎:505-521), וכל מה שיגיע אחריו הוא אחרי ההחלפה."""
        drained, ovf = 0, 0
        for _ in range(DRAIN_MAX_ITER):
            ret, _ = self._read(self.scratch, 0)
            if ret == SOAPY_SDR_TIMEOUT:
                break
            if ret == SOAPY_SDR_OVERFLOW:
                ovf += 1
                continue
            if ret > 0:
                drained += ret
                continue
            self.read_errors += 1
            break
        self.overflows += ovf
        self.drain_overflows += ovf
        return drained, ovf

    def _discard(self, k):
        """k קריאות *מוצלחות* בהמתנה. הראשונה היא המאגר שהיה בהתמלאות כשההחלפה
        נחתה (מעורב — gr_changed ננעל לפני העתקת החבילה, Streaming.cpp:87-90)."""
        got, ovf = 0, 0
        while got < k:
            ret, _ = self._read(self.scratch, READ_TIMEOUT_US)
            if ret > 0:
                got += 1
            elif ret == SOAPY_SDR_OVERFLOW:
                ovf += 1
        self.overflows += ovf
        return ovf

    def _measure(self, m):
        off, t_ret, n_list = 0, [], []
        for _ in range(m):
            dst = self.meas[2 * off: 2 * off + 2 * BUFFER_ELEMS]
            ret, t = self._read(dst, READ_TIMEOUT_US)
            if ret > 0:
                off += ret
                t_ret.append(t)
                n_list.append(ret)
            elif ret == SOAPY_SDR_OVERFLOW:
                self.overflows += 1
                return "overflow", t_ret, n_list
            else:
                return "short", t_ret, n_list
        return "ok", t_ret, n_list

    # ---------------------------------------------------------------- רווח
    def _apply_gain(self, lna, ifgr):
        """החלפה לפי כיוון (design §5.3): הגדלת רווח כולל (LNA נמוך יותר, או אותו LNA
        ו-IFGR קטן יותר) => IFGR קודם ואז RFGR; אחרת RFGR ואז IFGR — כך הרווח לא
        "קופץ מעל" באמצע ההחלפה. רכיב שלא השתנה לא נקרא (גם setGain עצמו מדלג על
        no-op, Settings.cpp:586,599 — אבל קריאה מיותרת אינה בחינם)."""
        s_prev, g_prev = self.cur
        if (lna, ifgr) == (s_prev, g_prev):
            return False
        up = lna < s_prev or (lna == s_prev and ifgr < g_prev)
        seq = (("IFGR", ifgr, g_prev), ("RFGR", lna, s_prev)) if up else \
              (("RFGR", lna, s_prev), ("IFGR", ifgr, g_prev))
        for name, v, old in seq:
            if v != old:
                self.radio.set_gain(name, v)
        self.cur = (lna, ifgr)
        return True

    def _force_gain(self, lna, ifgr):
        """אחרי AGC: הערך השמור ב-chParams עלול להיות שווה ליעד בעוד שהחומרה במקום
        אחר — ו-setGain היה מדלג (Settings.cpp:586). עוברים דרך ערך שכן ואז ליעד."""
        alt = ifgr + 1 if ifgr < IFGR_MAX else ifgr - 1
        self.radio.set_gain("IFGR", alt)
        self.radio.set_gain("IFGR", ifgr)
        self.radio.set_gain("RFGR", lna)
        self.cur = (lna, ifgr)

    # ---------------------------------------------------------------- פתיחה
    def _setup_device(self):
        p, r = self.p, self.radio
        try:
            bt = r.read_setting("biasT_ctrl")      # Settings.cpp:1900-1909
        except Exception:  # noqa: BLE001
            bt = None
        if bt == "true":
            # ברירת המחדל ב-RSP1B היא 0 (inc/sdrplay_api_rsp1a.h:19); הבודק לעולם
            # לא *מדליק* bias-T — רק מוודא שהוא כבוי (design §11).
            try:
                r.write_setting("biasT_ctrl", "false")   # Settings.cpp:1699-1726
            except Exception:  # noqa: BLE001
                raise SetupError("setup:bias_t")
            self.bias_t_forced_off = True
        try:
            # הסדר של rtl_airband (input-soapysdr.cpp:227-241)
            r.set_sample_rate(p["rate"])
            r.set_frequency(p["center_hz"])
            r.set_freq_correction(0)
        except Exception:  # noqa: BLE001
            raise SetupError("setup:tune")
        first = self._first_state()
        g = self.ifgr_cur[self._kfor(first, self.notch_cur)]
        try:
            # AGC כבוי *לפני* IFGR: תחת AGC (ברירת המחדל של הבנאי, Settings.cpp:98)
            # setGain("IFGR") מתעלם (Settings.cpp:584-595). לפני Init אין המתנה.
            r.set_agc(False)
            r.set_gain("IFGR", g)
            r.set_gain("RFGR", first)
        except Exception:  # noqa: BLE001
            raise SetupError("setup:gain")
        self.cur = (first, g)
        self.state_now = first
        try:
            r.start_stream()
        except Exception:  # noqa: BLE001
            raise SetupError("setup:stream")
        self._last_ok_read = self.clock()
        info = {}
        try:
            info = r.info()
        except Exception:  # noqa: BLE001
            pass
        meta_dev = {}
        for k, fn in (("rate_set", r.get_sample_rate), ("bw_set", r.get_bandwidth),
                      ("stream_formats", r.stream_formats)):
            try:
                meta_dev[k] = fn()
            except Exception:  # noqa: BLE001
                meta_dev[k] = None
        try:
            meta_dev["notch_readback"] = r.read_setting("rfnotch_ctrl")
        except Exception:  # noqa: BLE001
            meta_dev["notch_readback"] = None
        return info, meta_dev

    def _first_state(self):
        return self.p["states"][0]

    @staticmethod
    def _key(lna, notch=None, notch_phase=False):
        return "%d:%d" % (lna, 1 if notch else 0) if notch_phase else str(lna)

    def _kfor(self, lna, notch):
        """מפתח ה-IFGR/התקופה: לפי LNA, ובשלב המסנן לפי (LNA, מסנן) — spec §4.5."""
        return self._key(lna, notch, self.p["phase"] == "notch")

    def _build_meta(self, info, dev):
        p = self.p
        ver = self.backend.versions() if self.backend else {}
        api_v = _read_small(self.api_version_path, 64)
        api_v = api_v.strip() if api_v and re.match(r"^[0-9.]{1,16}\s*$", api_v) else None
        return {
            "run_id": self.run_id, "phase": p["phase"], "ref": p["ref"],
            "freq_hz": p["freq_hz"], "center_hz": p["center_hz"],
            "rate_set": _r3(dev.get("rate_set")), "bw_set": _r3(dev.get("bw_set")),
            "states": p["states"], "ifgr_start": p["ifgr_start"],
            "hw": info, "stream_formats": dev.get("stream_formats"),
            "fullscale": int(FS), "fullscale_source": "driver_source",
            "bias_t_forced_off": self.bias_t_forced_off,
            "notch_readback": dev.get("notch_readback") if dev.get("notch_readback") in ("true", "false") else None,
            "telemetry_marker": self.marker, "soapy_build_sig": self.build_sig,
            "handler": self.handler, "stream_start": self.telem.stream_start,
            "soapy_api": ver.get("soapy_api"), "soapy_lib": ver.get("soapy_lib"),
            "sdrplay_api_version_file": api_v,
            "numpy": np.__version__ if np is not None else None,
            "python": platform.python_version(),
            "t0_mono": _r3(self.t0), "t0_wall": _r3(self.wall()),
            "gr_table": _gr_table_for(info, p["center_hz"]),
            "buffer_s": BUF_S, "slot_budget_ms": round(SLOT_BUDGET_MS, 1),
            "nb_offsets_hz": p["nb_offsets_hz"], "rail_code": p["rail_code"],
            "guard_buffers": p["guard_buffers"], "measure_buffers": p["measure_buffers"],
            "notch_guard_buffers": p["notch_guard_buffers"], "ratchet_db": p["ratchet_db"],
        }

    def _read_marker(self):
        raw = _read_small(self.marker_path, 256)
        if raw is None:
            return False, None
        line = raw.strip().splitlines()[0].strip() if raw.strip() else ""
        sig = line if re.match(r"^[0-9a-fA-F]{8,128}$", line) else "unparsed"
        return True, sig

    # ---------------------------------------------------------------- ריצה
    def run(self):
        """מריץ ומחזיר קוד יציאה. end.json נכתב תמיד, *אחרי* סגירת המכשיר
        (spec §4.5 צעד 7: deactivate, closeStream, שחרור, end.json, יציאה)."""
        try:
            ended, error, code = self._run_body()
        except Exception as e:  # noqa: BLE001 - רשת ביטחון אחרונה
            ended, error, code = "error", "exception:" + type(e).__name__, EXIT_EXCEPTION
        return self._finish(ended, error, code)

    def _init_keys(self):
        p = self.p
        notch_phase = p["phase"] == "notch"
        for s in p["states"]:
            keys = ([self._key(s, nv, True) for nv in (False, True)] if notch_phase
                    else [self._key(s)])
            for k in keys:
                self.ifgr_cur[k] = p["ifgr_start"][str(s)]
                self.epoch[k] = 0
                self.ratchets[k] = 0

    def _run_body(self):
        p = self.p
        self.t0 = self.clock()
        self._init_keys()
        self._status("opening", force=True)
        if np is None:
            return ("error", "import:numpy", EXIT_OPEN)
        try:
            self.backend.load()
        except Exception:  # noqa: BLE001
            return ("error", "import:soapysdr", EXIT_OPEN)
        self.marker, self.build_sig = self._read_marker()
        self.sink = LogSink(self.clock)
        self.telem = Telemetry(self.t0)
        try:
            self.backend.set_log_level_info()
        except Exception:  # noqa: BLE001
            pass
        try:
            # ה-handler נרשם *לפני* פתיחת המכשיר: כך שורת "AIRAM_RF stream=start"
            # (מ-activateStream) והאזהרות של הפתיחה מגיעות אלינו (spec §4.4 צעד 2)
            self.handler = bool(self.backend.register_log_handler(self.sink.handler))
        except Exception:  # noqa: BLE001
            self.handler = False
        try:
            try:
                self.radio = self.backend.open(p["notch_base"])
            except Exception as e:  # noqa: BLE001
                self._say("פתיחת המכשיר נכשלה: %s" % type(e).__name__)
                return ("error", "open", EXIT_OPEN)
            try:
                info, dev = self._setup_device()
                self._pump_logs()
                self.meta = self._build_meta(info, dev)
                self.out.write_json("meta.json", self.meta)
                self._status("streaming", force=True)
                if p["phase"] == "diagnose":
                    return self._diagnose()
                return self._loop(notch_phase=p["phase"] == "notch")
            except SetupError as e:
                return ("error", e.code, EXIT_OPEN)
            except DeviceLost as e:
                return ("device_lost", e.code, EXIT_DEVICE_LOST)
            except StopRequested:
                return ("stopped", None, EXIT_OK)
            except Exception as e:  # noqa: BLE001
                self._say("חריגה: %s" % type(e).__name__)
                return ("error", "exception:" + type(e).__name__, EXIT_EXCEPTION)
        finally:
            self._status("stopping", force=True)
            self._close()

    def _close(self):
        if self.radio is not None:
            try:
                self.radio.close()
            except Exception:  # noqa: BLE001
                pass
        if self.sink is not None:
            try:
                self.backend.unregister_log_handler()   # אחרי close: אין ת'רדי API
            except Exception:  # noqa: BLE001
                pass
            try:
                self._pump_logs()
            except DeviceLost:
                pass
        self.out.close()

    def _finish(self, ended, error, code):
        rate = _median(self.rates)
        end = {
            "run_id": self.run_id, "ended": ended, "error": error,
            "slots": self.slots, "cycles": self.cycles, "overflows": self.overflows,
            "drain_overflows": self.drain_overflows,
            "gr_timeouts": len(self._gr_timeout_slots),
            "telemetry_lines": self.telem.lines if self.telem else 0,
            "telemetry_gain_lines": self.telem.gain_lines if self.telem else 0,
            "telemetry_overload_lines": self.telem.ovl_lines if self.telem else 0,
            "stream_start_seen": bool(self.telem and self.telem.stream_start),
            "lna_grdb_seen": sorted(self.telem.lna_grdb_seen) if self.telem else [],
            "log_dropped": self.log_dropped,
            "rate_measured": _r3(rate),
            "duration_sec": _r3(self._elapsed()) if self.t0 is not None else None,
            "light_mode": self.light_mode,
            "ifgr_final": dict(self.ifgr_cur), "ratchets": dict(self.ratchets),
            "read_errors": self.read_errors,
            "proc_ms_p50": _r2(_median(self.proc_all)), "proc_ms_p95": _r2(_pctl(self.proc_all, 0.95)),
        }
        if self.p["phase"] == "diagnose":
            # ended="stopped" גם כשהאבחון הסתיים מעצמו (קוד 0, spec §4.3); complete מבחין
            end["diagnose_complete"] = bool(self.diag and self.diag.get("complete"))
        try:
            self.out.write_json("end.json", end)
        except OSError as e:
            self._say("end.json: %s" % e.__class__.__name__)
        return code

    # ---------------------------------------------------------------- סלוט
    def _slot(self, lna, notch, key, cyc, dirn, pos=None, want_row=True):
        p = self.p
        if self.max_slots is not None and self.slots >= self.max_slots:
            raise SlotLimit()
        self._check_time()
        ifgr = self.ifgr_cur[key]
        self.state_now = lna
        sw_start = self.clock()
        self.telem.prune(sw_start - 1.0)
        gain_changed = self._apply_gain(lna, ifgr)
        notch_changed, notch_rb = False, None
        if p["notch_alternate"]:
            if notch != self.notch_cur:
                # Update(Rsp1a_RfNotchControl) *בלי* המתנה להשלמה (Settings.cpp:1777-1783)
                self.radio.write_setting("rfnotch_ctrl", "true" if notch else "false")
                self.notch_cur = notch
                notch_changed = True
            try:
                # readSetting קורא את הפרמטר השמור, לא את החומרה (Settings.cpp:1911-1929)
                notch_rb = self.radio.read_setting("rfnotch_ctrl")
            except Exception:  # noqa: BLE001
                notch_rb = None
        sw_end = self.clock()
        drained, dovf = self._drain()
        n_discard = 1 + p["guard_buffers"]
        if notch_changed:
            n_discard = max(n_discard, p["notch_guard_buffers"])
        self._discard(n_discard)
        disc_end = self.clock()
        status, t_ret, n_list = self._measure(p["measure_buffers"])
        t_meas_end = self.clock()
        self._pump_logs()
        gr_to = self.telem.gr_timeout_between(sw_start, disc_end)
        if gr_to:
            self._gr_timeout_slots.append(self.slots)
        inv = "gr_timeout" if gr_to else ("overflow" if status == "overflow" else
                                           ("short" if status == "short" else None))
        idx = self.slots
        want_nb = bool(p["nb_offsets_hz"]) and not (self.light_mode and idx % 4 != 0)
        if self.measure_hook is not None and status == "ok":
            self.measure_hook(self.meas[:2 * sum(n_list)],
                              {"i": idx, "lna": lna, "ifgr": ifgr, "notch": notch})
        m = self.dsp.compute(self.meas, n_list, want_nb=want_nb) if status == "ok" else None
        enabled = self.handler and self.marker
        if t_ret:
            ovl, ovl_edge = ovl_flags(self.telem, enabled, sw_start, t_ret[0], t_ret[-1],
                                      p["ovl_margin_buffers"])
        else:
            ovl, ovl_edge = None, None
        if status == "ok" and len(t_ret) >= 2 and t_ret[-1] > t_ret[0]:
            self.rates.append(sum(n_list[1:]) / (t_ret[-1] - t_ret[0]))
        if self.process_hook is not None:
            self.process_hook()
        proc_ms = (self.clock() - t_meas_end) * 1000.0
        self.proc_hist.append(proc_ms)
        self.proc_all.append(proc_ms)
        # מצב קל (spec §4.5 צעד 11): תקציב תזמון, לא סף RF. לא ב-ATIS — שם n_nb
        # הוא הרצפה היחידה. דביק: פעם שנכנסים, נשארים (בלי תנודה פנימה-החוצה).
        if (not self.light_mode and p["ref"] == "tower" and len(self.proc_hist) >= 8
                and _median(self.proc_hist) > 0.5 * self._slot_nominal_ms()):
            self.light_mode = True
        valid = inv is None
        row = {
            "run_id": self.run_id, "i": idx, "cyc": cyc, "dir": dirn, "lna": lna,
            "ifgr": ifgr, "epoch": self.epoch[key], "notch": bool(notch),
            "t0": _r3(t_ret[0]) if t_ret else None, "t1": _r3(t_ret[-1]) if t_ret else None,
            "wall": _r3(self.wall()), "valid": valid, "inv": inv,
            "sw_ms": _r2((sw_end - sw_start) * 1000.0), "sw": gain_changed or notch_changed,
            "drained": drained,
            "drain_ovf": dovf, "n": int(sum(n_list)),
            "c_tot": None, "c_tot_h1": None, "c_tot_h2": None, "c_car": None, "b_tot": None,
            "n_nb": None, "p_wb": None, "clip": None, "peak": None,
            "ovl": ovl, "ovl_edge": ovl_edge, "e_mean": None, "e_std": None, "e_p99": None,
            "proc_ms": _r2(proc_ms),
        }
        if m is not None:
            for k in ("c_tot", "c_tot_h1", "c_tot_h2", "c_car", "n_nb", "p_wb",
                      "e_mean", "e_std", "e_p99"):
                row[k] = _r2(m[k])
            row["b_tot"] = [_r2(v) for v in m["b_tot"]]
            row["clip"], row["peak"] = m["clip"], m["peak"]
        if p["notch_alternate"]:
            row["notch_rb"] = notch_rb
            row["pos"] = pos
        self.slots += 1
        if want_row and self._emit_rows:
            self.out.append_row(row)
        self._status()
        return row

    def _slot_nominal_ms(self):
        return (1 + self.p["guard_buffers"] + self.p["measure_buffers"]) * BUF_S * 1000.0

    def _ratchet(self, hit):
        """ratchet פר-מצב בסוף סבב (spec §4.5 צעד 9): חיתוך/עומס בסלוט תקין ו-IFGR<59
        => ‎+ratchet_db, נחתך ב-59, תקופה חדשה. לעולם לא יורד; לא משפיע על מצבים אחרים."""
        for k in hit:
            if self.ifgr_cur[k] < IFGR_MAX:
                self.ifgr_cur[k] = min(IFGR_MAX, self.ifgr_cur[k] + self.p["ratchet_db"])
                self.ratchets[k] += 1
                self.epoch[k] += 1

    def _loop(self, notch_phase):
        p = self.p
        self.dsp = SlotDSP(p["freq_hz"], p["center_hz"], p["rate"], p["nb_offsets_hz"], p["rail_code"])
        try:
            c = 0
            while True:
                hit = set()
                if notch_phase:
                    # ABBA בתוך קבוצה של 4: (A,B,B,A), A=notch_base (spec §4.5)
                    lna = p["states"][0]
                    a = bool(p["notch_base"])
                    for pos, nv in enumerate((a, not a, not a, a)):
                        key = self._key(lna, nv, True)
                        row = self._slot(lna, nv, key, c, "f" if pos < 2 else "r", pos)
                        if row["valid"] and ((row["clip"] or 0) > 0 or row["ovl"] is True):
                            hit.add(key)
                else:
                    # ABBA בין סבבים: זוגי קדימה, אי-זוגי אחורה (spec §4.5 צעד 8)
                    order = p["states"] if c % 2 == 0 else p["states"][::-1]
                    for s in order:
                        key = self._key(s)
                        row = self._slot(s, p["notch_base"], key, c, "f" if c % 2 == 0 else "r")
                        if row["valid"] and ((row["clip"] or 0) > 0 or row["ovl"] is True):
                            hit.add(key)
                self._ratchet(hit)
                c += 1
                self.cycles = c
        except StopRequested:
            return ("stopped", None, EXIT_OK)
        except SlotLimit:
            return ("stopped", None, EXIT_OK)
        except MaxSec:
            return ("max_sec", None, EXIT_OK)

    # ================================================================ אבחון
    def _settle_cfg(self, lna, ifgr, n=None):
        self._apply_gain(lna, ifgr)
        self._drain()
        self._discard(1 + self.p["guard_buffers"] if n is None else n)

    def _buf_metrics(self, ret, want_nb=True):
        m = self.dsp.compute(self.scratch, [ret], want_nb=want_nb)
        if m is None:
            return None
        return {"c_tot": _r2(m["c_tot"]), "c_car": _r2(m["c_car"]), "n_nb": _r2(m["n_nb"]),
                "p_wb": _r2(m["p_wb"]), "peak": m["peak"], "clip": m["clip"]}

    def _read_bufs(self, count=None, seconds=None, want_nb=True, raw_hook=None):
        """קריאה רציפה בלי השלכה; מדדים לכל מאגר. count או seconds."""
        out = []
        t_start = self.clock()
        while True:
            if count is not None and len(out) >= count:
                break
            if seconds is not None and self.clock() - t_start >= seconds:
                break
            self._check_time()
            ret, t = self._read(self.scratch, READ_TIMEOUT_US)
            if ret == SOAPY_SDR_OVERFLOW:
                self.overflows += 1
                out.append({"ovf": True, "t": _r3(t - t_start)})
                continue
            if ret <= 0:
                continue
            if raw_hook is not None:
                raw_hook(self.scratch[:2 * ret])
            mm = self._buf_metrics(ret, want_nb) or {}
            mm["t"] = _r3(t - t_start)
            mm["n"] = ret
            out.append(mm)
            self._status()
        return out

    def _d1(self, sug):
        r = self.radio
        d = {"meta": {k: self.meta.get(k) for k in ("hw", "rate_set", "bw_set", "stream_formats",
                                                      "notch_readback", "bias_t_forced_off",
                                                      "telemetry_marker", "soapy_build_sig",
                                                      "handler", "stream_start", "soapy_api",
                                                      "soapy_lib", "numpy", "python",
                                                      "sdrplay_api_version_file", "gr_table")}}
        try:
            d.update(self.backend.env_info())
        except Exception as e:  # noqa: BLE001
            d["env_error"] = type(e).__name__
        try:
            d["setting_keys"] = r.setting_keys()[:64]
        except Exception as e:  # noqa: BLE001
            d["setting_keys"] = {"error": type(e).__name__}
        gr = {}
        for g in ("IFGR", "RFGR"):
            try:
                gr[g] = r.gain_range(g)
            except Exception as e:  # noqa: BLE001
                gr[g] = {"error": type(e).__name__}
        d["gain_ranges"] = gr
        shm = []
        try:
            for name in sorted(os.listdir("/dev/shm"))[:512]:
                if fnmatch.fnmatch(name.lower(), "*sdrplay*"):
                    try:
                        st = os.lstat(os.path.join("/dev/shm", name))
                        shm.append({"name": name[:96], "mode": oct(st.st_mode & 0o7777),
                                    "uid": st.st_uid, "gid": st.st_gid, "size": st.st_size})
                    except OSError:
                        shm.append({"name": name[:96]})
                if len(shm) >= 32:
                    break
        except OSError as e:
            shm = {"error": type(e).__name__}
        d["dev_shm"] = shm
        maps = []
        try:
            with open("/proc/self/maps", encoding="utf-8", errors="replace") as f:
                for ln in f:
                    if "sdrplay" in ln.lower():
                        path = ln.split()[-1] if ln.split() else ""
                        if path and path not in maps:
                            maps.append(path[:200])
                    if len(maps) >= 16:
                        break
        except OSError as e:
            maps = {"error": type(e).__name__}
        d["proc_maps"] = maps
        return d

    def _d3(self, sug):
        """תגובת מדרגה: AGC כבוי, LNA 4/IFGR 40, ‏4→0, 0→4, 4→7, 7→4 ×reps."""
        reps, nbuf = self.dt["d3_reps"], self.dt["d3_buffers"]
        self.radio.set_agc(False)
        self._force_gain(DIAG_REF_LNA, DIAG_REF_IFGR)
        self._settle_cfg(DIAG_REF_LNA, DIAG_REF_IFGR, n=4)
        g0, o0 = self.telem.gain_lines, self.telem.ovl_lines
        toggles = []
        seq = ((4, 0), (0, 4), (4, 7), (7, 4))
        for _ in range(reps):
            for a, b in seq:
                self._check_time()
                t_sw = self.clock()
                self._apply_gain(b, DIAG_REF_IFGR)
                sw_ms = (self.clock() - t_sw) * 1000.0
                drained, ovf = self._drain()
                bufs = self._read_bufs(count=nbuf)
                toggles.append({"from": a, "to": b, "sw_ms": _r2(sw_ms), "drained": drained,
                                "drain_ovf": ovf, "bufs": bufs})
        self._d3_counts = (self.telem.gain_lines - g0, self.telem.ovl_lines - o0)
        per_type, strict = {}, []
        steady_n = self.dt["settle_buffers"]
        for a, b in seq:
            ts = [t for t in toggles if t["from"] == a and t["to"] == b]
            rec = {}
            for metric in ("p_wb", "c_tot"):
                trajs = [[bb.get(metric) for bb in t["bufs"] if not bb.get("ovf")] for t in ts]
                trajs = [tr for tr in trajs if len(tr) == nbuf and None not in tr]
                for tr in trajs:
                    strict.append(settle_index(tr, steady_n, expand=False))
                med = [_median([tr[j] for tr in trajs]) for j in range(nbuf)] if trajs else []
                rec[metric] = {"median_traj": [_r2(v) for v in med],
                               "settle": settle_index(med, steady_n) if med else None}
            per_type["%d->%d" % (a, b)] = rec
        idx = [v[m]["settle"] for v in per_type.values() for m in v if v[m]["settle"] is not None]
        st_idx = [s for s in strict if s is not None]
        sug["guard_buffers"] = max(0, min(8, max(idx) - 1)) if idx else None
        sug["guard_buffers_strict"] = max(0, min(8, max(st_idx) - 1)) if st_idx else None
        return {"toggles": toggles, "per_type": per_type,
                "settle_rule": "median-of-reps, band=[min-range,max+range] of last %d" % steady_n}

    def _d2(self, sug):
        enabled = self.handler and self.marker and self.telem.stream_start
        g, o = getattr(self, "_d3_counts", (None, None))
        val = (g > 0) if (enabled and g is not None) else None
        sug["gainchange_on_manual"] = val
        return {"gain_lines_during_d3": g, "overload_lines_during_d3": o,
                "telemetry_enabled": enabled, "gainchange_on_manual": val}

    def _d4(self, sug):
        self.radio.set_agc(False)
        steps = []
        for g in self.dt["d4_ifgr"]:
            self._check_time()
            self._settle_cfg(DIAG_REF_LNA, g)
            bufs = self._read_bufs(seconds=self.dt["d4_sec"])
            ok = [b for b in bufs if not b.get("ovf")]
            steps.append({"ifgr": g, "n_bufs": len(ok),
                          "c_car": _r2(_median([b.get("c_car") for b in ok])),
                          "c_tot": _r2(_median([b.get("c_tot") for b in ok])),
                          "n_nb": _r2(_median([b.get("n_nb") for b in ok])),
                          "p_wb": _r2(_median([b.get("p_wb") for b in ok]))})
        return {"lna": DIAG_REF_LNA, "steps": steps}

    def _d5(self, sug):
        """full-scale ועומס: LNA 0, IFGR 59→20. היסטוגרמת הקודים העליונים + אירועי עומס
        *כש-AGC כבוי* — האם החומרה בכלל מדווחת עומס במצב הזה (design §3)."""
        self.radio.set_agc(False)
        rail = self.p["rail_code"]
        agg = collections.Counter()
        steps = []
        peak_max = 0
        total_ovl = 0
        for g in self.dt["d5_ifgr"]:
            self._check_time()
            self._settle_cfg(0, g)
            t_a = self.clock()
            hist = collections.Counter()
            stats = {"n": 0, "rail": 0, "peak": 0}

            def hook(raw, hist=hist, stats=stats):
                a32 = np.abs(raw.astype(np.int32))
                pk = int(a32.max())
                stats["peak"] = max(stats["peak"], pk)
                stats["n"] += a32.size
                stats["rail"] += int(np.count_nonzero(a32 >= rail))
                lo = max(0, pk - 2047)
                vals, cnts = np.unique(a32[a32 >= lo], return_counts=True)
                for v, c in zip(vals[-64:], cnts[-64:]):
                    hist[int(v)] += int(c)

            self._read_bufs(seconds=self.dt["d5_sec"], want_nb=False, raw_hook=hook)
            t_b = self.clock()
            ev = sum(1 for (t, on) in self.telem.ovl_events if t_a <= t <= t_b and on)
            enabled = self.handler and self.marker
            ovl_state = self.telem.state_over(t_a, t_b) if enabled else None
            total_ovl += ev
            top = sorted(hist.items())[-16:]
            agg.update(dict(sorted(hist.items())[-64:]))
            peak_max = max(peak_max, stats["peak"])
            steps.append({"ifgr": g, "peak": stats["peak"],
                          "top16": [[v, c] for v, c in top],
                          "frac_rail": _r3(stats["rail"] / stats["n"] * 1e6) if stats["n"] else None,
                          "overload_events": ev, "overload_state": ovl_state})
        rail_sug = rail_pileup(agg, peak_max)
        clipped = rail_sug is not None
        sug["rail_code"] = rail_sug
        sug["peak_code_max"] = peak_max
        enabled = self.handler and self.marker and self.telem.stream_start
        any_on = total_ovl > 0 or any(st["overload_state"] is True for st in steps)
        if not enabled:
            orep = None
        elif any_on:
            orep = True
        elif clipped:
            orep = False      # ה-ADC נחתך בבירור ולא דווח אף אירוע
        else:
            orep = None       # לא היה עומס להעיד עליו — לא ידוע
        sug["overload_reported_with_agc_off"] = orep
        return {"lna": 0, "steps": steps, "peak_code_max": peak_max,
                "frac_rail_unit": "ppm", "pileup": clipped, "overload_events": total_ovl}

    def _d6(self, sug):
        """מסנן FM: החלפה חיה + מסלול לכל מאגר + readback (design §2.4 שאלה 7)."""
        self.radio.set_agc(False)
        self._settle_cfg(DIAG_REF_LNA, DIAG_REF_IFGR)
        nbuf = self.dt["d6_buffers"]
        toggles = []
        base = bool(self.p["notch_base"])
        cur = self.notch_cur
        for _ in range(self.dt["d6_toggles"]):
            self._check_time()
            nv = not cur
            t_w = self.clock()
            self.radio.write_setting("rfnotch_ctrl", "true" if nv else "false")
            w_ms = (self.clock() - t_w) * 1000.0
            try:
                rb = self.radio.read_setting("rfnotch_ctrl")
            except Exception:  # noqa: BLE001
                rb = None
            cur = nv
            self.notch_cur = nv
            drained, ovf = self._drain()
            bufs = self._read_bufs(count=nbuf)
            toggles.append({"to": nv, "readback": rb, "write_ms": _r2(w_ms),
                            "drained": drained, "drain_ovf": ovf, "bufs": bufs})
        if cur != base:
            self.radio.write_setting("rfnotch_ctrl", "true" if base else "false")
            self.notch_cur = base
        steady_n = self.dt["settle_buffers"]
        per_dir, idx = {}, []
        for to in (True, False):
            ts = [t for t in toggles if t["to"] == to]
            rec = {}
            for metric in ("p_wb", "c_tot"):
                trajs = [[bb.get(metric) for bb in t["bufs"] if not bb.get("ovf")] for t in ts]
                trajs = [tr for tr in trajs if len(tr) == nbuf and None not in tr]
                med = [_median([tr[j] for tr in trajs]) for j in range(nbuf)] if trajs else []
                s = settle_index(med, steady_n) if med else None
                if s is not None:
                    idx.append(s)
                rec[metric] = {"median_traj": [_r2(v) for v in med], "settle": s}
            per_dir["on" if to else "off"] = rec
        # בשלב המסנן משליכים notch_guard_buffers אחרי הניקוז (בלי "מעורב" נפרד)
        sug["notch_guard_buffers"] = max(0, min(20, max(idx))) if idx else None
        rb_ok = all(t["readback"] == ("true" if t["to"] else "false") for t in toggles)
        return {"toggles": toggles, "per_dir": per_dir, "readback_ok": rb_ok}

    def _d7(self, sug):
        """AGC האמיתי — לתיעוד לעתיד (design §2.4 שאלה 4). לא משמש לפסק הדין."""
        r = self.radio
        r.set_agc(False)
        self._force_gain(DIAG_REF_LNA, DIAG_REF_IFGR)
        self._settle_cfg(DIAG_REF_LNA, DIAG_REF_IFGR)
        t_a = self.clock()
        out = {}
        try:
            r.set_agc(True)
            on = self._read_bufs(seconds=self.dt["d7_agc_sec"], want_nb=False)
            try:
                out["ifgr_param_under_agc"] = r.get_gain("IFGR")
            except Exception:  # noqa: BLE001
                out["ifgr_param_under_agc"] = None
            t_sw = self.clock()
            r.set_gain("RFGR", 0)        # תחת AGC ההמתנה עלולה לחזור על grChanged של ה-AGC
            out["step_sw_ms"] = _r2((self.clock() - t_sw) * 1000.0)
            self.cur = (0, self.cur[1])
            step = self._read_bufs(seconds=self.dt["d7_step_sec"], want_nb=False)
        finally:
            r.set_agc(False)
            self._force_gain(DIAG_REF_LNA, DIAG_REF_IFGR)
        t_b = self.clock()
        out["agc_bufs"] = [{"t": b.get("t"), "p_wb": b.get("p_wb"), "c_car": b.get("c_car")} for b in on]
        out["step_bufs"] = [{"t": b.get("t"), "p_wb": b.get("p_wb"), "c_car": b.get("c_car")} for b in step]
        out["gain_lines"] = [[_r3(t - t_a), g, l] for (t, g, l) in self.telem.gain_events
                             if t_a <= t <= t_b][:200]
        out["overload_events"] = [[_r3(t - t_a), on_] for (t, on_) in self.telem.ovl_events
                                  if t_a <= t <= t_b][:200]
        return out

    def _d8(self, sug):
        """CPU: הלולאה האמיתית (מצבים 0/4/7, IFGR מפוצה) למשך d8_sec."""
        p = self.p
        self.radio.set_agc(False)
        states = list(self.dt["d8_states"])
        saved = (dict(self.ifgr_cur), dict(self.epoch), dict(self.ratchets))
        ovf0, slots0, rates0 = self.overflows, self.slots, len(self.rates)
        proc0 = len(self.proc_all)
        for s in states:
            k = self._key(s)
            g = DIAG_REF_IFGR + GR_RSP1B_60_420[DIAG_REF_LNA] - GR_RSP1B_60_420[s]
            self.ifgr_cur[k] = max(IFGR_MIN, min(IFGR_MAX, g))
            self.epoch.setdefault(k, 0)
            self.ratchets.setdefault(k, 0)
        self._emit_rows = False
        rows = []
        t_end = self.clock() + self.dt["d8_sec"]
        c = 0
        try:
            while self.clock() < t_end:
                order = states if c % 2 == 0 else states[::-1]
                for s in order:
                    if self.clock() >= t_end:
                        break
                    rows.append(self._slot(s, p["notch_base"], self._key(s), c,
                                           "f" if c % 2 == 0 else "r"))
                c += 1
        finally:
            self._emit_rows = True
            self.ifgr_cur, self.epoch, self.ratchets = saved
        proc = self.proc_all[proc0:]
        ovf = self.overflows - ovf0
        p95 = _pctl(proc, 0.95)
        rate = _median(self.rates[rates0:])
        sug["cpu_ok"] = (ovf == 0 and p95 is not None and p95 < SLOT_BUDGET_MS) if proc else None
        sug["proc_ms_p50"] = _r2(_median(proc))
        sug["proc_ms_p95"] = _r2(p95)
        sug["rate_measured"] = _r3(rate)
        return {"states": states, "slots": self.slots - slots0, "overflows": ovf,
                "proc_ms_p50": _r2(_median(proc)), "proc_ms_p95": _r2(p95),
                "slot_budget_ms": round(SLOT_BUDGET_MS, 1), "rate_measured": _r3(rate),
                "invalid": collections.Counter(r["inv"] for r in rows if r["inv"]),
                "light_mode": self.light_mode,
                "slots_detail": [[r["lna"], r["ifgr"], r["valid"], r["inv"], r["proc_ms"]]
                                 for r in rows[:120]]}

    def _diagnose(self):
        p = self.p
        self.dsp = SlotDSP(p["freq_hz"], p["center_hz"], p["rate"], p["nb_offsets_hz"], p["rail_code"])
        steps, sug = {}, {}
        self.diag = {"run_id": self.run_id, "meta": self.meta, "steps": steps,
                     "suggested": sug, "summary_text": None}
        plan = (("D1", self._d1), ("D3", self._d3), ("D2", self._d2), ("D4", self._d4),
                ("D5", self._d5), ("D6", self._d6), ("D7", self._d7), ("D8", self._d8))
        ended = ("stopped", None, EXIT_OK)
        halt = None
        for name, fn in plan:
            if halt is not None:
                steps[name] = {"error": halt}
                continue
            try:
                self._check_time()
                self._diag_step = name
                self._status(force=True)
                steps[name] = fn(sug)
            except StopRequested:
                halt = "stopped"
                steps[name] = {"error": halt}
                ended = ("stopped", None, EXIT_OK)
            except MaxSec:
                halt = "max_sec"
                steps[name] = {"error": halt}
                ended = ("max_sec", None, EXIT_OK)
            except DeviceLost as e:
                halt = "device_lost"
                steps[name] = {"error": halt}
                ended = ("device_lost", e.code, EXIT_DEVICE_LOST)
            except Exception as e:  # noqa: BLE001 - שלב אחד לא עוצר את האחרים
                steps[name] = {"error": "exception:" + type(e).__name__}
                self._say("%s: %s" % (name, type(e).__name__))
            self._write_diag()
        self.diag["steps"] = {k: steps[k] for k in ("D1", "D2", "D3", "D4", "D5", "D6", "D7", "D8")}
        self.diag["complete"] = halt is None
        self.diag["summary_text"] = self._summary_text()
        self._write_diag()
        return ended

    def _write_diag(self):
        try:
            self.out.write_json("diagnose.json", _jsonable(self.diag))
        except (OSError, ValueError) as e:
            self._say("diagnose.json: %s" % e.__class__.__name__)

    def _summary_text(self):
        m, s = self.meta or {}, self.diag["suggested"]

        def fmt(v):
            if v is None:
                return "null"
            if isinstance(v, bool):
                return "true" if v else "false"
            return str(v)
        hw = m.get("hw") or {}
        lines = ["AIRAM-RFDIAG v1",
                 "run_id=%s" % self.run_id,
                 "python=%s" % fmt(m.get("python")), "numpy=%s" % fmt(m.get("numpy")),
                 "soapy_api=%s" % fmt(m.get("soapy_api")),
                 "hw=%s" % fmt(hw.get("hardware")),
                 "sdrplay_api=%s" % fmt((hw.get("info") or {}).get("sdrplay_api_api_version")),
                 "sdrplay_api_file=%s" % fmt(m.get("sdrplay_api_version_file")),
                 "build_sig=%s" % fmt(m.get("soapy_build_sig")),
                 "telemetry_marker=%s" % fmt(m.get("telemetry_marker")),
                 "handler=%s" % fmt(m.get("handler")),
                 "stream_start=%s" % fmt(bool(self.telem and self.telem.stream_start)),
                 "rate_set=%s" % fmt(m.get("rate_set")), "bw_set=%s" % fmt(m.get("bw_set"))]
        for k in ("gainchange_on_manual", "guard_buffers", "guard_buffers_strict",
                  "notch_guard_buffers", "rail_code", "peak_code_max",
                  "overload_reported_with_agc_off", "cpu_ok", "proc_ms_p50", "proc_ms_p95",
                  "rate_measured"):
            lines.append("%s=%s" % (k, fmt(s.get(k))))
        lines.append("overflows=%d" % self.overflows)
        lines.append("bias_t_forced_off=%s" % fmt(self.bias_t_forced_off))
        errs = ["%s:%s" % (k, v["error"]) for k, v in sorted(self.diag["steps"].items())
                if isinstance(v, dict) and "error" in v]
        lines.append("errors=%s" % (",".join(errs) if errs else "none"))
        return "\n".join(lines) + "\n"


def _jsonable(obj):
    """Counter/set/tuple => JSON; לא-סופי => None."""
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if np is not None and isinstance(obj, np.generic):
        return _jsonable(obj.item())
    return obj


def run_probe(params, backend, out, **kw):
    """נקודת כניסה לבדיקות ול-main: מריץ ומחזיר קוד יציאה (end.json נכתב תמיד)."""
    return Probe(params, backend, out, **kw).run()


def main_with(params_path, backend, out, *, allowed_uids=None, stderr=None, **kw):
    """main בלי הנתיבים הקבועים (לבדיקות): ניקוי OUT_DIR, טעינת פרמטרים, הרצה.
    פרמטרים לא תקינים => end.json עם קוד בלבד, יציאה 2, **המכשיר לא נפתח**."""
    err = stderr if stderr is not None else sys.stderr
    try:
        out.reset()
    except OSError as e:
        try:
            err.write("[airam-rfcheck] out_dir:%s\n" % errno.errorcode.get(e.errno, "E?"))
        except Exception:  # noqa: BLE001
            pass
        return EXIT_EXCEPTION
    try:
        params = load_params(params_path, allowed_uids=allowed_uids)
    except ParamError as e:
        try:
            out.write_json("end.json", {"run_id": e.run_id, "ended": "error",
                                        "error": "params:" + e.code})
        except OSError:
            pass
        try:
            err.write("[airam-rfcheck] params:%s\n" % e.code)
        except Exception:  # noqa: BLE001
            pass
        return EXIT_PARAMS
    return run_probe(params, backend, out, stderr=stderr, **kw)


def _import_soapy():
    import SoapySDR  # noqa: PLC0415
    return SoapySDR


def selftest(importer=None):
    """בדיקת זמינות כמשתמש airam: **לעולם לא פותח מכשיר**. listModules רק סורק
    נתיבים (SoapySDR lib/Modules.in.cpp:157-166) — לא טוען מודולים."""
    res = {"python": platform.python_version(),
           "numpy": np.__version__ if np is not None else None,
           "soapysdr": None, "sdrplay_module": False, "log_handler_api": False}
    try:
        mod = (importer or _import_soapy)()
    except Exception:  # noqa: BLE001 - ImportError, או .so שבור
        mod = None
    if mod is not None:
        try:
            res["soapysdr"] = str(mod.getAPIVersion())
        except Exception:  # noqa: BLE001
            res["soapysdr"] = None
        try:
            # שם המודול של SoapySDRPlay3: TARGET sdrPlaySupport (CMakeLists.txt:54-55)
            res["sdrplay_module"] = any("sdrPlaySupport" in str(m) for m in mod.listModules())
        except Exception:  # noqa: BLE001
            res["sdrplay_module"] = False
        res["log_handler_api"] = hasattr(mod, "registerLogHandler")
    ok = bool(res["numpy"] and res["soapysdr"] and res["sdrplay_module"])
    return res, ok


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--selftest"]:
        res, ok = selftest()
        print(json.dumps(res, separators=(",", ":")))
        return EXIT_OK if ok else 1
    if args:
        # אין ‎--params במצב רגיל (spec §4.1): הקלט היחיד הוא PARAMS_PATH
        sys.stderr.write("usage: rfcheck_probe.py [--selftest]\n")
        return EXIT_PARAMS
    stop = StopFlag()

    def on_term(_sig, _frame):
        stop.set()      # נבדק בין קריאות; ה-cleanup קורה בת'רד הראשי
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    os.umask(0o022)
    return main_with(PARAMS_PATH, SoapyBackend(), OutDir(OUT_DIR), stop=stop)


if __name__ == "__main__":
    sys.exit(main())
