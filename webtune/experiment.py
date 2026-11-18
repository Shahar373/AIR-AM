# ============================================================================
#  AIR-AM — רשם ניסוי RF וניסוי הכיול האוטומטי (docs/antenna-calibration-experiment.md)
# ----------------------------------------------------------------------------
#  הוצא מ-app.py (שלב ג2). הרשם והניסוי באותו מודול כי הם תלויים הדדית (ה-worker של הרשם לא כבה באמצע
#  ניסוי; הניסוי כותב לרשם) ופיצול היה מעגלי. נתיבי ה-HTTP נשארו ב-app.py.
#  ⚠ הניסוי קורא לפונקציות שהבדיקות מחליפות על `app` (`_enter_voice`, `_probe_params`, `_live_mode`, …) ול-`TUNE_LOCK`/
#  נתיבי stats/config — ולכן הם **מוזרקים** דרך `bind(...)` כ-lambda שפותר את השם ב-app בזמן *הקריאה* (החלפה על
#  `app` ממשיכה לעבוד). ייבוא `app` מכאן אסור: כשהשרת רץ כסקריפט הוא `__main__` וייטען עותק שני.
#  ⚠ הקוראים ניגשים כ-`experiment.<שם>` (לא `from experiment import`).
# ============================================================================
import json
import logging
import os
import re
import statistics
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import appstate
import config
import storage

log = logging.getLogger("airam")

# התלויות המוזרקות (ר' bind). קריאה לפני bind ⇒ AttributeError מפורש — לא ברירת מחדל שקטה.
_D = SimpleNamespace()


def bind(**deps):
    """מזריק את התלויות מ-app.py: enter_voice, probe_params, restore_after_probe, sample_probe_stats,
    live_mode, rtl_airband_start_wall, parse_stats, tune_lock(), stats_path(), config_path(), sample_sec(),
    version, composite_secondary(), composite_label(), scanning()."""
    vars(_D).update(deps)


# --- רשם ניסוי RF (docs/antenna-calibration-experiment.md) -----------------
# קובץ ה-stats של rtl_airband נדרס כל שנייה בלי היסטוריה, ובשטח אין SSH —
# כלומר בדיוק מה שהניסוי צריך (השניות שאחרי restart, הדקה שאחרי ניתוק אנטנה)
# לא ניתן לקריאה בעין מהטלפון. הרשם כותב שורה לכל כתיבה *חדשה* של ה-stats,
# עם הקונפיג שבאמת רץ (airband.conf, לא state.json — בדיקת האנטנה לא כותבת
# state), ו-mtime של שני הקבצים. ⚠ השוואת שני ה-mtime **לא** מזהה flush-יציאה
# של התהליך הקודם: ה-flush נכתב בזמן העצירה — *אחרי* כתיבת הקונפיג — ולכן
# stats_mtime שלו גדול מ-conf_mtime. ההבחנה האמינה היא מול זמן ההפעלה של
# התהליך (`_rtl_airband_start_wall`), שהניסוי האוטומטי רושם אחרי כל הפעלה.
# כבוי כברירת מחדל; thread רק כשמפעילים; כיבוי אוטומטי.
RFLOG_PATH = Path("/var/lib/airam/rf_log.jsonl")


RFLOG_MAX_SEC = 2 * 3600          # לא כותבים ל-SD לנצח אם שכחו לכבות


RFLOG_POLL_SEC = 0.4              # rtl_airband כותב ~1Hz — דוגמים מהר יותר כדי לא לפספס כתיבה


RFLOG_ROTATE_BYTES = 5_000_000    # בהפעלה: קובץ גדול מזה עובר ל-.prev (שעתיים ≈ 1.5MB)


RFLOG_LABEL_MAX = 40


# ה-fsync של הרשם דרך כינוי ברמת המודול — כך בדיקה שדוחסת זמן (test_experiment)
# יכולה לנטרל *רק אותו*, בלי למקף את os.fsync הגלובלי (ר' no_sleep ב-CHANGELOG:
# מיקוף גלובלי כבר הפך בדיקה בפרויקט לתלוית-מזל).
_rflog_fsync = os.fsync


# --- רשם ניסוי RF ------------------------------------------------------------
_rflog_lock = threading.Lock()


_rflog = {"active": False, "started_at": None, "until": None, "rows": 0,
          "marks": 0, "write_errors": 0, "thread": None, "stop": None}


_CONF_FREQ_RE = re.compile(r"^\s*freq\s*=\s*([0-9.]+)\s*;", re.M)          # לא centerfreq


_CONF_GAIN_RE = re.compile(r'^\s*gain\s*=\s*"IFGR=(\d+),RFGR=(\d+)"', re.M)


_CONF_SQ_RE = re.compile(r"^\s*squelch_snr_threshold\s*=\s*(-?[0-9.]+)\s*;", re.M)


_CONF_MOD_RE = re.compile(r'^\s*modulation\s*=\s*"(\w+)"', re.M)


def _parse_airband_conf(text):
    """הקונפיג *שבאמת רץ* מתוך airband.conf שכתב render_config. בלי שורת gain
    = AGC (כך render_config מבקש אותו — ר' שם); squelch=None = אוטומטי."""
    out = {}
    m = _CONF_FREQ_RE.search(text)
    if m:
        try:
            out["freq"] = float(m.group(1))
        except ValueError:
            pass
    g = _CONF_GAIN_RE.search(text)
    out["agc"] = g is None
    out["ifgr"] = int(g.group(1)) if g else None
    out["rfgr"] = int(g.group(2)) if g else None
    q = _CONF_SQ_RE.search(text)
    out["squelch_snr"] = float(q.group(1)) if q else None
    mm = _CONF_MOD_RE.search(text)
    out["mod"] = mm.group(1) if mm else None
    return out


def _rflog_sample(prev_sig):
    """דגימה אחת: מחזיר (row, sig). row=None כשאין כתיבה חדשה של ה-stats או של
    הקונפיג (אותה חתימת mtime) — כך כל שורה בקובץ היא מדידה חדשה, ובמצבים
    שאינם קול (stats קפוא) לא נכתב כלום."""
    try:
        st = _D.stats_path().stat()
        text = _D.stats_path().read_text()
    except OSError:
        return None, prev_sig
    try:
        cst = _D.config_path().stat()
        conf = _parse_airband_conf(_D.config_path().read_text())
    except OSError:
        cst, conf = None, {}
    sig = (st.st_mtime, cst.st_mtime if cst else None)
    if sig == prev_sig:
        return None, prev_sig
    # ⚠ המדדים מתויגים לפי תדר: stats של תהליך קודם על תדר אחר => None, לא
    # ערך "קרוב" (§12). על *אותו* תדר הערך נכתב — וההבחנה אם הוא של התהליך
    # הקודם נעשית בניתוח, מול proc_start (ר' _experiment_summary), לא כאן.
    vals = _D.parse_stats(text, f"{conf['freq']:.3f}") if conf.get("freq") is not None else {}
    row = {"t": round(time.time(), 2), "stats_mtime": round(st.st_mtime, 2),
           "conf_mtime": round(cst.st_mtime, 2) if cst else None, **conf,
           "signal": vals.get("channel_dbfs_signal_level"),
           "noise": vals.get("channel_dbfs_noise_level")}
    return row, sig


def _rflog_write(obj):
    """append + fsync. בניגוד ל-track.jsonl (buffer אפמרי, בלי fsync) — כאן זו
    תוצאת ניסוי שדה, והתרחיש הסביר לאבדן הוא בדיוק power bank שקוטע (§12).
    ⚠ לעולם לא זורק: כשל כתיבה (כרטיס SD מלא, הרשאה) נספר ב-write_errors
    ומוחזר False. בגרסה הראשונה הוא זרק — ובתוך ה-finally של _experiment_run
    זה דילג על סימון הסיום, כך שהניסוי נשאר "רץ" לנצח וסירב להתחיל מחדש."""
    line = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
    with _rflog_lock:
        try:
            RFLOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(RFLOG_PATH, "a+b") as f:
                storage.append_line_on_boundary(f, line)   # ר' שם: שורה קטועה מכיבוי קודם
                f.flush()
                _rflog_fsync(f.fileno())
        except OSError as e:
            _rflog["write_errors"] += 1
            if _rflog["write_errors"] in (1, 100):      # לא מציפים את היומן בכל שנייה
                log.warning("רשם RF: כתיבה ל-%s נכשלה: %s", RFLOG_PATH, e)
            return False
        _rflog["rows"] += 1
    return True


def _rflog_stop(reason):
    """עוצר את הרשם (idempotent). לא עושה join כשנקרא מתוך ה-worker עצמו."""
    with _rflog_lock:
        if not _rflog["active"]:
            return False
        _rflog["active"] = False
        evt, th = _rflog["stop"], _rflog["thread"]
    if evt:
        evt.set()
    _rflog_write({"ev": "stop", "t": round(time.time(), 2), "reason": reason})
    if th and th is not threading.current_thread():
        th.join(timeout=2)
    return True


def _rflog_worker(stop_evt):
    prev = None
    while not stop_evt.is_set():
        with _rflog_lock:
            until = _rflog["until"]
        with _exp_lock:
            exp_running = _exp["running"]
        # לא נכבים באמצע ניסוי כיול: רשם שהופעל ידנית לפני ~שעתיים היה נעצר
        # באמצע, והניסוי (על שתי הפעולות הפיזיות שלו) היה מסתיים בלי נתונים.
        if until is not None and time.time() >= until and not exp_running:
            _rflog_stop("timeout")
            return
        try:
            row, prev = _rflog_sample(prev)
            if row:
                _rflog_write(row)
        except Exception:
            log.warning("רשם RF: דגימה נכשלה", exc_info=True)   # לא מפילים את ה-thread על שורה אחת
        stop_evt.wait(RFLOG_POLL_SEC)


def _rflog_start():
    with _rflog_lock:
        if _rflog["active"]:
            return False
        try:
            if RFLOG_PATH.exists() and RFLOG_PATH.stat().st_size > RFLOG_ROTATE_BYTES:
                os.replace(RFLOG_PATH, RFLOG_PATH.with_suffix(".jsonl.prev"))
        except OSError:
            pass
        now = time.time()
        evt = threading.Event()
        _rflog.update(active=True, started_at=now, until=now + RFLOG_MAX_SEC,
                      rows=0, marks=0, write_errors=0, stop=evt, thread=None)
    _rflog_write({"ev": "start", "t": round(now, 2), "version": _D.version,
                  "max_sec": RFLOG_MAX_SEC})
    th = threading.Thread(target=_rflog_worker, args=(evt,), daemon=True)
    with _rflog_lock:
        _rflog["thread"] = th
    th.start()
    return True


def _rflog_event(obj):
    """אירוע מתויג (סימון משתמש / תוצאת בדיקת אנטנה) — רק כשהרשם פעיל."""
    with _rflog_lock:
        active = _rflog["active"]
    if active:
        _rflog_write({"t": round(time.time(), 2), **obj})
    return active


def _rflog_status():
    with _rflog_lock:
        st = {k: _rflog[k] for k in ("active", "started_at", "until", "rows", "marks", "write_errors")}
    # "נותרו" מחושב כאן ולא בטלפון: שעון הטלפון ושעון ה-Pi לא בהכרח מסונכרנים בשטח
    st["remaining"] = max(0, round(st["until"] - time.time())) if st["active"] and st["until"] else None
    try:
        st["size"] = RFLOG_PATH.stat().st_size
    except OSError:
        st["size"] = 0
    return st


# --- ניסוי כיול אוטומטי (docs/antenna-calibration-experiment.md) -----------
# ה-Pi מריץ לבד את כל התנאים (תדר × רווח קבוע/AGC × בדיקת-האנטנה של המוצר)
# פעם עם אנטנה מחוברת ופעם מנותקת. המשתמש מבצע **שתי פעולות פיזיות בלבד**
# (לנתק, לחבר) — במקום ניתוק/חיבור לכל תנאי. הנתונים נרשמים ברשם ה-RF,
# והסיכום מחושב על ה-Pi (_experiment_summary). הניסוי מחזיק את TUNE_LOCK לכל
# אורכו: כוונון/מעבר-מצב מהטלפון מקבלים 409, ו-_mode_reconcile_once מדלג.
EXP_ATIS_FREQ = 132.5        # ה-ATIS עצמו: אימות שהניתוק/החיבור באמת קרו (הנשא נעלם/חוזר)


EXP_CLEAN_FREQ = 122.6       # חלון 122.13–123.67: אף תדר של נתב"ג בחלון ה-AGC


EXP_ATISWIN_FREQ = 132.0     # חלון 131.53–133.07: ה-ATIS 0.2MHz ממרכזו


EXP_FIXED_GAINS = ((20, 0), (35, 0))   # (IFGR, RFGR): מקסימום ("AGC על המסילה"), ועוד אחד לזיהוי רוויה


EXP_REF_SEC = 30


EXP_FIXED_SEC = 45           # רווח קבוע: גשש-הרעש מתכנס תוך ~0.2ש' (אין הליכת AGC)


EXP_AGC_SEC = 90             # AGC: כל restart מתחיל מ-gRdB=50 — צריך זמן לראות אם/איך מתכנס


EXP_AFTER_PROMPT_SEC = 90    # אחרי ניתוק/חיבור: הזחילה האיטית של הגשש (משטר C)


EXP_FINAL_AGC_SEC = 60


EXP_PROMPT_TIMEOUT_SEC = 600


EXP_RESTART_EST_SEC = 6      # הערכה ל-ETA בלבד, לא ללוגיקה


# חלונות הניתוח (ר' _experiment_summary)
EXP_FIXED_SETTLE_SEC = 10


EXP_AGC_TAIL_SEC = 30


EXP_EARLY_SEC = 3            # "מה שבדיקת האנטנה רואה": 3 השניות שאחרי שה-restart אומת


EXP_CREEP_HEAD_SEC = 5


EXP_CREEP_TAIL_SEC = 15


EXP_REF_SETTLE_SEC = 5       # ATIS: מדלגים על השניות הראשונות אחרי ה-restart


EXP_STALE_WINDOW_SEC = 5     # כמה זמן אחרי restart מחפשים כתיבה של התהליך הקודם


# mtime של הקרנל נלקח משעון גס (מפגר מילישניות אחרי time.time()) ו-stats_mtime
# מעוגל ל-10ms. בין flush-היציאה של התהליך הקודם להפעלת החדש עוברות לפחות מאות
# מילישניות (עצירה + airam-wait-sdrplay), כך ש-50ms הם מרווח בטוח לשני הכיוונים.
EXP_MTIME_TOL_SEC = 0.05


# ⚠ יוריסטיקת-שלמות *של הניסוי*, לא פסק-דין של המוצר: נשא ATIS חזק שלא ירד
# לפחות בזה בניתוק => כנראה שהאנטנה לא נותקה בפועל (או שדולף הרבה). מוצג
# עם המספר הגולמי, כדי שאפשר יהיה לשפוט אחרת.
EXP_ATIS_GONE_DB = 20.0


_exp_lock = threading.Lock()


_exp = {"running": False, "id": None, "started_at": None, "finished_at": None,
        "plan": [], "i": -1, "step_started_at": None, "waiting": None, "error": None,
        "result": None, "stop": None, "confirm": None, "thread": None}


def _experiment_plan(probe_freq):
    """תוכנית הניסוי: מחזור מלא עם אנטנה מחוברת, בקשת ניתוק, אותו מחזור מנותק,
    בקשת חיבור, ומחזור קצר לאימות. כל מחזור מסתיים על 132.000 AGC מתכנס — כך
    הניתוק/החיבור קורים בדיוק במצב שבו ה-AGC אמור להסתיר (משטר B), והדקה שאחריהם
    מתעדת את הזחילה (משטר C)."""
    def dwell(phase, key, label, freq, agc, gains=(None, None), sec=None):
        return {"kind": "dwell", "phase": phase, "key": key, "label": label, "freq": freq,
                "agc": agc, "ifgr": gains[0], "rfgr": gains[1],
                "sec": sec if sec is not None else (EXP_AGC_SEC if agc else EXP_FIXED_SEC)}

    def probe(phase, key, label, freq):
        return {"kind": "probe", "phase": phase, "key": key, "label": label, "freq": freq}

    def cycle(phase):
        steps = [dwell(phase, "atis", "ATIS 132.500 — אימות", EXP_ATIS_FREQ, True, sec=EXP_REF_SEC)]
        for freq, fk, fl in ((EXP_CLEAN_FREQ, "clean", "122.600 · אין נתב\"ג בחלון"),
                             (EXP_ATISWIN_FREQ, "atiswin", "132.000 · ATIS בחלון")):
            for ifgr, rfgr in EXP_FIXED_GAINS:
                steps.append(dwell(phase, f"{fk}_f{ifgr}", f"{fl} · רווח קבוע IFGR {ifgr}",
                                   freq, False, (ifgr, rfgr)))
            if fk == "atiswin":
                steps.append(probe(phase, "probe_product", f"בדיקת האנטנה של המוצר · {probe_freq:.3f}",
                                   probe_freq))
                steps.append(probe(phase, "probe_atiswin", "בדיקת האנטנה · 132.000", EXP_ATISWIN_FREQ))
            steps.append(dwell(phase, f"{fk}_agc", f"{fl} · AGC", freq, True))
        return steps

    plan = cycle(1)
    plan.append({"kind": "prompt", "phase": 2, "key": "after_disconnect", "action": "disconnect",
                 "label": "נתק את האנטנה בכניסת ה-SDR", "sec": EXP_AFTER_PROMPT_SEC})
    plan += cycle(2)
    plan.append({"kind": "prompt", "phase": 3, "key": "after_reconnect", "action": "reconnect",
                 "label": "חבר חזרה את האנטנה", "sec": EXP_AFTER_PROMPT_SEC})
    plan.append(dwell(3, "atis", "ATIS 132.500 — אימות", EXP_ATIS_FREQ, True, sec=EXP_REF_SEC))
    plan.append(dwell(3, "clean_agc", "122.600 · אין נתב\"ג בחלון · AGC", EXP_CLEAN_FREQ, True,
                      sec=EXP_FINAL_AGC_SEC))
    for i, st in enumerate(plan):
        st["i"] = i
    return plan


def _exp_step_est(st):
    if st["kind"] == "dwell":
        return st["sec"] + EXP_RESTART_EST_SEC
    if st["kind"] == "probe":
        return EXP_RESTART_EST_SEC + _D.sample_sec()
    return st["sec"]          # prompt: רק ההקלטה שאחרי האישור (זמן התגובה שלך לא ידוע)


def _experiment_status():
    with _exp_lock:
        e = dict(_exp)
    plan, i = e["plan"], e["i"]
    st = {k: e[k] for k in ("running", "id", "started_at", "finished_at", "error", "result")}
    st["steps_total"] = len(plan)
    st["step_index"] = i
    st["waiting"] = e["waiting"]
    cur = plan[i] if 0 <= i < len(plan) else None
    st["step"] = ({k: cur.get(k) for k in ("kind", "phase", "key", "label", "freq", "agc", "ifgr", "action")}
                  if cur else None)
    eta = eta_prompt = None
    if e["running"] and plan:
        elapsed = time.time() - e["step_started_at"] if e["step_started_at"] else 0
        cur_left = max(0.0, _exp_step_est(cur) - elapsed) if cur and not e["waiting"] else 0.0
        rest = plan[i + 1:] if i >= 0 else plan
        eta = round(cur_left + sum(_exp_step_est(s) for s in rest))
        nxt = next((s for s in rest if s["kind"] == "prompt"), None)
        if cur and cur["kind"] == "prompt" and e["waiting"]:
            eta_prompt = 0
        elif nxt is not None:
            eta_prompt = round(cur_left + sum(_exp_step_est(s) for s in rest[:rest.index(nxt)]))
    st["eta_sec"] = eta
    st["eta_prompt_sec"] = eta_prompt
    return st


def _exp_voice_params(st):
    return {"freq": st["freq"], "mod": "am", "agc": st["agc"],
            "if_gain": st["ifgr"] if st["ifgr"] is not None else config.IF_GAIN_DEFAULT,
            "rf_gain": st["rfgr"] if st["rfgr"] is not None else config.RF_GAIN_DEFAULT,
            "squelch_mode": "open", "squelch_snr": config.SNR_DEFAULT}


def _exp_meta(st):
    return {k: st.get(k) for k in ("i", "kind", "phase", "key", "label", "freq", "agc",
                                    "ifgr", "rfgr", "sec", "action")}


def _experiment_run(run_id, prev, prev_live, plan, stop_evt, confirm_evt, own_rflog):
    """ה-thread של הניסוי. מחזיק את TUNE_LOCK (נתפס ב-_experiment_start) ומשחרר
    אותו תמיד ב-finally, אחרי שחזור המצב הקודם — בדיוק כמו /api/antenna/check."""
    err = None
    proc_start = None
    try:
        for st in plan:
            if stop_evt.is_set():
                err = "הניסוי בוטל"
                break
            with _exp_lock:
                _exp["i"] = st["i"]
                _exp["step_started_at"] = time.time()
            if st["kind"] in ("dwell", "probe"):
                t_enter = time.time()
                params = _exp_voice_params(st) if st["kind"] == "dwell" else _D.probe_params(st["freq"])
                e, _detail, _down = _D.enter_voice(params)
                t_ready = time.time()
                proc_start = _D.rtl_airband_start_wall()
                if e:
                    _rflog_event({"ev": "step", "exp": run_id, **_exp_meta(st), "t_enter": t_enter,
                                  "t_ready": t_ready, "proc_start": proc_start, "error": e})
                    err = f"המעבר לקול נכשל ({st['label']}): {e}"
                    break
                if st["kind"] == "dwell":
                    _rflog_event({"ev": "step", "exp": run_id, **_exp_meta(st), "t_enter": t_enter,
                                  "t_ready": t_ready, "proc_start": proc_start})
                    if stop_evt.wait(st["sec"]):
                        err = "הניסוי בוטל"
                        break
                    _rflog_event({"ev": "step_end", "exp": run_id, "i": st["i"]})
                else:
                    res = _D.sample_probe_stats(st["freq"], _D.sample_sec())
                    _rflog_event({"ev": "probe", "exp": run_id, **_exp_meta(st), "t_enter": t_enter,
                                  "t_ready": t_ready, "proc_start": proc_start,
                                  "noise": (res or {}).get("noise"), "signal": (res or {}).get("signal"),
                                  "stats_mtime": (res or {}).get("stats_mtime"),
                                  "error": None if res else "no_fresh_stats"})
            else:   # prompt: פעולה פיזית — ממתינים לאישור מהטלפון, בלי לשנות קונפיג
                confirm_evt.clear()
                with _exp_lock:
                    _exp["waiting"] = {"action": st["action"], "label": st["label"], "since": time.time()}
                _rflog_event({"ev": "prompt", "exp": run_id, **_exp_meta(st)})
                deadline = time.time() + EXP_PROMPT_TIMEOUT_SEC
                confirmed = False
                while not stop_evt.is_set() and time.time() < deadline:
                    if confirm_evt.wait(0.5):
                        confirmed = True
                        break
                with _exp_lock:
                    _exp["waiting"] = None
                    _exp["step_started_at"] = time.time()
                if not confirmed:
                    err = ("הניסוי בוטל" if stop_evt.is_set()
                           else f"לא התקבל אישור לפעולה הפיזית תוך {EXP_PROMPT_TIMEOUT_SEC // 60} דקות — הניסוי נעצר")
                    break
                t_c = time.time()
                _rflog_event({"ev": "mark", "exp": run_id, "label":
                              "מנותק" if st["action"] == "disconnect" else "מחובר", "prompted": True})
                _rflog_event({"ev": "step", "exp": run_id, **_exp_meta(st), "kind": "after_prompt",
                              "freq": EXP_ATISWIN_FREQ, "agc": True, "ifgr": None,
                              "t_enter": t_c, "t_ready": t_c, "proc_start": proc_start})
                if stop_evt.wait(st["sec"]):
                    err = "הניסוי בוטל"
                    break
                _rflog_event({"ev": "step_end", "exp": run_id, "i": st["i"]})
    except Exception as ex:                       # לא משאירים את ה-SDR תקוע בגלל באג בניסוי
        log.warning("ניסוי כיול: שגיאה פנימית", exc_info=True)
        err = f"שגיאה פנימית: {ex}"
    finally:
        try:
            _D.restore_after_probe(prev, prev_live)
        except Exception:
            log.warning("ניסוי כיול: שחזור המצב הקודם נכשל", exc_info=True)
        _D.tune_lock().release()
        _rflog_event({"ev": "exp_end", "exp": run_id, "error": err})
        result = None
        try:
            result = _experiment_summary(_read_rflog_run(run_id))
            _rflog_event({"ev": "summary", "exp": run_id, "result": result})
        except Exception:
            log.warning("ניסוי כיול: חישוב הסיכום נכשל", exc_info=True)
        if own_rflog:
            _rflog_stop("experiment")
        with _exp_lock:
            _exp.update(running=False, finished_at=time.time(), error=err, result=result,
                        waiting=None, stop=None, confirm=None)


def _experiment_start():
    """מחזיר (payload, status)."""
    with _exp_lock:
        if _exp["running"]:
            return {"ok": False, "error": "הניסוי כבר רץ"}, 409
    if _D.scanning():
        return {"ok": False, "error": "עצור את הסריקה לפני הניסוי"}, 409
    prev_live = _D.live_mode()
    if prev_live in _D.composite_secondary():
        return {"ok": False, "error": "לא זמין במצב " + _D.composite_label()[prev_live] + " — עבור לקול בלבד"}, 409
    if prev_live == "satcom":
        return {"ok": False, "error": "SATCOM פעיל — עצור אותו וחבר את אנטנת ה-VHF לפני הניסוי"}, 409
    if not _D.tune_lock().acquire(blocking=False):
        return {"ok": False, "error": "פעולה אחרת מתבצעת כרגע — נסה שוב בעוד רגע"}, 409
    try:
        prev = appstate.load_state()
        acars = prev.get("acars_freqs") or config.ACARS_FREQS_DEFAULT
        try:
            probe_freq = float(acars[0])
        except (TypeError, ValueError, IndexError):
            probe_freq = float(config.ACARS_FREQS_DEFAULT[0])
        plan = _experiment_plan(probe_freq)
        own_rflog = _rflog_start()          # False => המשתמש כבר הקליט; לא נכבה אותו בסוף
        run_id = time.strftime("%Y%m%d-%H%M%S")
        stop_evt, confirm_evt = threading.Event(), threading.Event()
        with _exp_lock:
            _exp.update(running=True, id=run_id, started_at=time.time(), finished_at=None,
                        plan=plan, i=-1, step_started_at=None, waiting=None, error=None,
                        result=None, stop=stop_evt, confirm=confirm_evt)
        _rflog_event({"ev": "exp_start", "exp": run_id, "version": _D.version, "prev_live": prev_live,
                      "probe_freq": probe_freq, "steps": len(plan)})
        th = threading.Thread(target=_experiment_run, daemon=True,
                              args=(run_id, prev, prev_live, plan, stop_evt, confirm_evt, own_rflog))
        with _exp_lock:
            _exp["thread"] = th
        th.start()
    except Exception:
        _D.tune_lock().release()
        with _exp_lock:
            _exp["running"] = False
        raise
    return {"ok": True, **_experiment_status()}, 200


def _read_rflog_run(run_id):
    """כל שורות הרשם בין exp_start ל-exp_end של ריצה מסוימת (שורה פגומה מדולגת)."""
    rows, inside = [], False
    try:
        lines = storage.read_log_lines(RFLOG_PATH)
    except OSError:
        return rows
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if not isinstance(r, dict):
            continue
        if r.get("ev") == "exp_start" and r.get("exp") == run_id:
            inside = True
        if inside:
            rows.append(r)
        if r.get("ev") == "exp_end" and r.get("exp") == run_id:
            break
    return rows


def _med(vals):
    vals = [v for v in vals if isinstance(v, (int, float))]
    return round(statistics.median(vals), 1) if vals else None


def _experiment_summary(rows):
    """מספרים גולמיים בלבד (§12): חציוני רצפת-רעש לכל תנאי, מחובר מול מנותק.
    'detects' = האם *הסף של המוצר עצמו* (DISCONNECT_DROP_DB) היה מזהה את הניתוק
    בתנאי הזה — לא סף חדש. None בכל מקום שאין מספיק נתונים, לא ניחוש."""
    data = [r for r in rows if "ev" not in r and "noise" in r]
    steps, ends = {}, {}
    for r in rows:
        if r.get("ev") == "step":
            steps[r["i"]] = r
        elif r.get("ev") == "step_end":
            ends[r["i"]] = r["t"]

    def match(st, r):
        if r.get("freq") is None or abs(r["freq"] - st["freq"]) > 5e-4:
            return False
        if bool(r.get("agc")) != bool(st.get("agc")):
            return False
        if not st.get("agc") and r.get("ifgr") != st.get("ifgr"):
            return False
        ps = st.get("proc_start")
        # רק כתיבות של התהליך הנוכחי — flush-יציאה של הקודם נספר בנפרד (stale)
        return ps is None or (r.get("stats_mtime") or 0) >= ps - EXP_MTIME_TOL_SEC

    def vals(st, a, b, key="noise"):
        return [r[key] for r in data if a <= r["t"] <= b and match(st, r) and r.get(key) is not None]

    cond, atis, prompts = {}, {}, {}
    stale_rows = 0
    for i, st in steps.items():
        t_end = ends.get(i)
        if t_end is None:
            continue                      # צעד שלא הושלם (ביטול) — לא מנתחים חלקי
        ps = st.get("proc_start")
        if ps is not None:
            stale_rows += sum(1 for r in data
                              if st["t_enter"] <= r["t"] <= st["t_ready"] + EXP_STALE_WINDOW_SEC
                              and r.get("freq") is not None and abs(r["freq"] - st["freq"]) <= 5e-4
                              and r.get("noise") is not None
                              and (r.get("stats_mtime") or 0) < ps - EXP_MTIME_TOL_SEC)
        if st["kind"] == "after_prompt":
            head = vals(st, st["t_ready"], st["t_ready"] + EXP_CREEP_HEAD_SEC)
            tail = vals(st, t_end - EXP_CREEP_TAIL_SEC, t_end)
            inst = round(min(head), 1) if head else None
            after = _med(tail)
            prompts[st["key"]] = {"instant": inst, "after": after,
                                  "rise": round(after - inst, 1) if after is not None and inst is not None else None}
            continue
        if st["kind"] != "dwell":
            continue
        if st["key"] == "atis":
            atis[st["phase"]] = _med(vals(st, st["t_ready"] + EXP_REF_SETTLE_SEC, t_end, "signal"))
            continue
        if st["agc"]:
            steady = vals(st, t_end - EXP_AGC_TAIL_SEC, t_end)
        else:
            steady = vals(st, st["t_ready"] + EXP_FIXED_SETTLE_SEC, t_end)
        early = vals(st, st["t_ready"], st["t_ready"] + EXP_EARLY_SEC)
        c = cond.setdefault(st["key"], {"key": st["key"], "label": st["label"], "freq": st["freq"],
                                        "agc": st["agc"], "ifgr": st.get("ifgr")})
        c[f"p{st['phase']}"] = {"steady": _med(steady), "early": _med(early), "n": len(steady)}

    def drop(a, b):
        return round(a - b, 1) if a is not None and b is not None else None

    conditions = []
    for c in cond.values():
        p1, p2 = c.get("p1", {}), c.get("p2", {})
        d = drop(p1.get("steady"), p2.get("steady"))
        c["drop"] = d
        c["drop_early"] = drop(p1.get("early"), p2.get("early"))
        c["detects"] = None if d is None else d >= config.DISCONNECT_DROP_DB
        conditions.append(c)

    probes = {}
    stale_probes = 0
    for r in rows:
        if r.get("ev") != "probe":
            continue
        p = probes.setdefault(r["key"], {"key": r["key"], "label": r["label"], "freq": r["freq"]})
        stale = (r.get("stats_mtime") is not None and r.get("proc_start") is not None
                 and r["stats_mtime"] < r["proc_start"] - EXP_MTIME_TOL_SEC)
        stale_probes += int(stale)
        p[f"p{r['phase']}"] = {"noise": r.get("noise"), "stale": stale, "error": r.get("error")}
    for p in probes.values():
        d = drop((p.get("p1") or {}).get("noise"), (p.get("p2") or {}).get("noise"))
        p["drop"] = d
        p["detects"] = None if d is None else d >= config.DISCONNECT_DROP_DB

    atis_drop = drop(atis.get(1), atis.get(2))
    clean_agc = cond.get("clean_agc", {})
    p3 = clean_agc.get("p3") or {}
    return {
        "threshold_db": config.DISCONNECT_DROP_DB,
        "conditions": conditions,
        "probes": list(probes.values()),
        "atis": {"p1": atis.get(1), "p2": atis.get(2), "p3": atis.get(3), "drop": atis_drop,
                 "gone": None if atis_drop is None else atis_drop >= EXP_ATIS_GONE_DB,
                 "back": drop(atis.get(3), atis.get(1))},
        "after_disconnect": prompts.get("after_disconnect"),
        "after_reconnect": prompts.get("after_reconnect"),
        "drift_clean_agc": drop(p3.get("steady"), (clean_agc.get("p1") or {}).get("steady")),
        "stale_rows": stale_rows,
        "stale_probes": stale_probes,
    }
