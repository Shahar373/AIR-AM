# -*- coding: utf-8 -*-
"""
rfcheck_analysis — שכבת הניתוח של 🩺 בדיקת RF (PR 2, v2.27.0).

הבודק (`rfcheck_probe.py`, root, numpy) *מודד*; המודול הזה *שופט* (docs/rf-check-design.md
§4.4). Python טהור: בלי numpy, בלי SoapySDR ובלי I/O — מקבל את שורות `rows.jsonl`, את
`meta.json`/`end.json` ואת ההקשר של המתזמר (ctx), ומחזיר dict שנכתב ל-`rfcheck_last.json`.
airam-web מייבא אותו; הוא חייב להיטען גם בלי numpy (נבדק ב-CI).

ממשק ציבורי (מה ש-app.py קורא):
    analyze(rows, meta, end, ctx)      -> תוצאה (§5.9)
    live_summary(rows, ctx)            -> סיכום חי לסטטוס (GET /api/rfcheck)
    stop_reason(summary, ctx, elapsed) -> None | "target" | "atis_target" | "notch_target" | "time"
    offer_atis(summary, ctx, elapsed)  -> bool
עזרים (גם לבדיקות): sign_test_p, holm, n_min, threshold_db, telemetry_status, classify,
cycles, transmissions, facts, cnr, pairs, U, compare_states, analyze_notch.

כלל ההחלטה (החלטת משתמש, 2026-10-06 — ר' docs/rf-check-design.md §6.4):
  1. עובדות (חיתוך/עומס ב-IFGR=59, "אבד") רק *מרחיקות* ממצב רע — לעולם לא מקרבות לרווח גבוה.
  2. העלאת רווח (המלצה על מצב LNA נמוך מהנוכחי) דורשת ראיה סטטיסטית ל-sensitivity_cost
     במצב הנוכחי — או את העובדה "אבד".
  3. שוויון (בתוך פיזור המדידה) ⇒ ההנחתה הגדולה יותר (max של TieSet).
  ⇒ "החל" מוצע רק ברמת fact/stat; ב-indication רק "כיוון אפשרי" (שדה apply_allowed).

סטיות מתועדות מהמפרט (§5), וכל אחת *למה*:
  (א) השוואות: Holm על *כל הזוגות* במקום best-מול-השאר. זה ה-fallback שהמפרט עצמו קובע
      ("If this fails, switch to all-pairs Holm") — והוא נכשל: בחירת best מאותם נתונים
      ("קללת המנצח") מנפחת טענות-שווא. סימולציה תחת השערת אפס גלובלית (CNR שווה, רעש
      גאוסי, 3000 זרעים) — שיעור הריצות עם טענת compression/sensitivity כלשהי:
          K=5,n=12: best-מול-השאר 0.042 · כל-הזוגות 0.004
          K=7,n=12: 0.078 · 0.011      K=7,n=20: 0.136 · 0.008      K=6,n=30: 0.131 · 0.020
      ברירת המחדל של המשתמש היא עד 7 מצבים, וכלל העצירה רץ עד שיש "מספיק" (n גדל) — בדיוק
      התחום שבו best-מול-השאר נכשל. Holm על כל הזוגות שומר FWER≤α בלי תלות בבחירה.
      tests/test_rfcheck_analysis.py מכיל גם את בדיקת הכיול (500 זרעים) וגם הדגמה
      שהגרסה המקורית נכשלת.
  (ב) מצב "אבד" (lost) שנמצא *בתוך* הקבוצה הפסולה (≤ lo) לא קובע את hi: הוא כבר מחוץ ל-C,
      ואין סיבה שיפסול גם את המצבים המוחלשים יותר שמעליו (שאחרת היה הופך "עומס ב-0" + "0 אבד"
      ל-all_overloaded שגוי).
  (ג) indication: מצב בלי אף זוג נתונים, או שנראה גרוע מ-best ביותר מ-U, לא נכלל ב"כיוון
      האפשרי" (המפרט כלל כל מצב עם n<n_min ⇒ הכיוון היה תמיד המצב המוחלש ביותר, גם בלי
      שום נתון עליו — "המצאת כיוון", §12). "שוויון" = |median|≤U, כלשון החלטת המשתמש.
  (ד) בלוק מלא נספר על מצבי C (המועמדים) — זהה להגדרת המפרט כשאין עובדות; מצב שנפסל
      בעובדה (למשל "אבד" במצב 8) לא חוסם לעד את יעד העצירה.
  (ה) overload/clip פר-מצב: ערך נוסף "probe_only" — נצפה רק ב-IFGR<59 של הבדיקה (לא עובדה).
      אחרת "not_observed" היה משקר כשהעומס כן נצפה ברווח הבדיקה.
  (ו) סלוט עם clip>0 או ovl=True לא נכנס ל-CNR ולא לרצפת הייחוס (design §6.1: "פוסל את
      הסלוט לחישוב CNR").
  (ז) שגיאה (ctx.error או end.ended ∈ {error, device_lost}) ⇒ level "none", headline
      "error", בלי המלצה — העובדות עדיין מוצגות ב-per_state.
  (ח) ממצא סותר (stat ממליץ להעלות רווח בלי sensitivity_cost במצב הנוכחי או במצב
      מוחלש-פחות ממנו — אפשרי רק במבחנים לא-טרנזיטיביים) ⇒ הרמה יורדת ל-indication (כלל 2).
  (ט) "שומר מונוטוני" גם לצד הרגישות: max(TieSet) לא יקפוץ *מעל* מצב עם sensitivity_cost
      מובהק. בשרשרת ליניארית הנחתה נוספת רק מורידה CNR (design §6.2), ולכן מצב מוחלש
      יותר שלא נמצא "גרוע" הוא כמעט תמיד חוסר-עוצמה של המבחן, לא שוויון אמיתי — בלי
      השומר, "שוויון ⇒ הנחתה" היה ממליץ על 8 כש-6 ו-7 כבר הוכחו כמאבדי רגישות. זו
      המראה הסטטיסטית של השומר המונוטוני של העובדות (פסילה ב-s פוסלת כל מה שמתחתיו).
      אותו עיקרון ב-indication: הכיוון לא עובר מצב שנראה גרוע מ-best ביותר מ-U.

מקורות (מאומתים):
  טבלת GR של RSP1B, ‏60–420MHz: SDRplay API Specification v3.15 (spec.txt:2287-2293).
  IFGR 20..59: SoapySDRPlay3 48bd8b4 Settings.cpp:647; ‏MAX_BB_GR=59 sdrplay_api_tuner.h:4.
  סף הסקוולץ' האוטומטי 9.54dB: RTLSDR-Airband v5.2.0 src/squelch.cpp:38.
"""

from __future__ import annotations

import math
import statistics
from functools import lru_cache

# --- קבועים (§5.1) ---------------------------------------------------------------

# LNA GR (dB) לפי LNAstate, ‏RSP1B, ‏60–420MHz (spec.txt:2287-2293). משמש לתצוגה ולזיהוי
# חיתוך ה-IF בלבד — ההכרעה לא תלויה בו (CNR נמדד מול רצפה מאותו מצב; בדיקת אי-תלות בטבלה).
GR_RSP1B_60_420 = (0, 6, 12, 18, 20, 26, 32, 38, 57, 62)
IFGR_MIN, IFGR_MAX = 20, 59          # Settings.cpp:647; MAX_BB_GR sdrplay_api_tuner.h:4
STATE_MIN, STATE_MAX = 0, 9
SQUELCH_AUTO_DB = 9.54               # squelch.cpp:38 (אומדן אחר — ר' design §5.6)
ALPHA = 0.05                         # מוסכמה סטטיסטית, לא סף RF
FACT_MIN_CYCLES = 2                  # עובדה = ≥2 סבבים שונים; 1 = "נצפה פעם אחת"
NOISE_REF_MAX = 6
NOISE_REF_MIN = 3
U_MIN_CYCLES = 4
SETTLE_ALPHA = 0.01
SETTLE_MIN_N = 10
INVALID_MAX_FRAC = 0.30              # מספיקות נתונים, לא סף RF
TARGET_TX = 3
TARGET_BLOCKS = 12
ATIS_TARGET_BLOCKS = 20
ATIS_MAX_SEC = 30
ATIS_OFFER_SEC = 30
NOTCH_TARGET_PAIRS = 12
HARD_MAX_SEC = 180                   # החלטת משתמש: עד 3 דקות, בלי "הארך"

CARRIER, SILENCE, EDGE, INVALID, UNCLASSIFIED = (
    "carrier", "silence", "edge", "invalid", "unclassified")

# החלטות מושוות על ערכים מעוגלים ל-1e-6 dB: הזזה של מצב שלם בקבוע (שגיאת טבלה, IF אחר)
# משאירה אותן זהות *בדיוק* ולא "כמעט" (‎(x+3)-(y+3) ≠ x-y בייצוג בינארי).
_ROUND = 6

_NUM_FIELDS = ("t0", "t1", "c_tot", "c_tot_h1", "c_tot_h2", "c_car", "n_nb", "p_wb",
               "proc_ms", "sw_ms")


# --- עזרים מספריים ----------------------------------------------------------------

def _num(x):
    """float סופי או None (bool אינו מספר כאן)."""
    if x is None or isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        x = float(x)
        return x if math.isfinite(x) else None
    return None


def _int(x, default=None):
    if x is None or isinstance(x, bool):
        return default
    if isinstance(x, int):
        return x
    if isinstance(x, float) and math.isfinite(x) and x == int(x):
        return int(x)
    return default


def _rd(x, nd=_ROUND):
    return None if x is None else round(float(x), nd)


def _median(vals):
    v = [x for x in vals if x is not None]
    return statistics.median(v) if v else None


def _quantile(vals, q):
    """quantile ליניארי (כמו numpy "linear")."""
    s = sorted(v for v in vals if v is not None)
    if not s:
        return None
    pos = q * (len(s) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def _iqr(vals):
    v = [x for x in vals if x is not None]
    if not v:
        return None
    return _quantile(v, 0.75) - _quantile(v, 0.25)


def label(state):
    """תווית ה-LNA כמו הסליידר (#rfGainVal): ‎(9 − state)/9."""
    return f"{STATE_MAX - int(state)}/9"


# --- סטטיסטיקה (§5.2) -------------------------------------------------------------

@lru_cache(maxsize=8192)
def _binom_upper(k, n):
    total = 0
    for j in range(k, n + 1):
        total += math.comb(n, j)
    return total / (2 ** n)          # חלוקת int/int — מדויקת-בעיגול גם ל-n גדול


def sign_test_p(k, n):
    """P(X ≥ k), ‏X~Bin(n, ½) — מבחן סימן מדויק חד-צדדי."""
    k, n = int(k), int(n)
    if n <= 0 or k <= 0:
        return 1.0
    if k > n:
        return 0.0
    return _binom_upper(k, n)


def holm(pvals, alpha=ALPHA):
    """Holm step-down. מחזיר list[bool] (דחייה) בסדר הקלט. None ⇒ לא נדחה (נספר ב-m)."""
    m = len(pvals)
    order = sorted(range(m), key=lambda i: (1.0 if pvals[i] is None else pvals[i], i))
    rej = [False] * m
    for rank, idx in enumerate(order):
        p = pvals[idx]
        if p is not None and p <= alpha / (m - rank):
            rej[idx] = True
        else:
            break
    return rej


def n_min(m, alpha=ALPHA):
    """ה-n הקטן ביותר שבו מבחן סימן *דו-צדדי* יכול בכלל לדחות בצעד הראשון של Holm על m
    השוואות: ‎2^(1−n) ≤ α/m. מתחתיו מובהקות בלתי-אפשרית מתמטית (לא "חוסר מזל").
    m=1 ⇒ 6 (זהה ל-⌈log2(2/α)⌉ של שני מבחנים חד-צדדיים במסנן ה-FM); m=10 ⇒ 9; m=21 ⇒ 10."""
    if not m or m < 1:
        return None
    n = 1
    while 2.0 ** (1 - n) > alpha / m:
        n += 1
    return n


def threshold_db(config):
    """T: ‏squelch_snr של המשתמש כשהסקוולץ' ידני (וחיובי), אחרת 9.54 (squelch.cpp:38).
    ‏"open" (ATIS) ⇒ 9.54: סף 0 היה הופך כל סלוט ל"נשא"."""
    cfg = config if isinstance(config, dict) else {}
    if cfg.get("squelch_mode") == "manual":
        v = _num(cfg.get("squelch_snr"))
        if v is not None and v > 0:
            return v
    return SQUELCH_AUTO_DB


def telemetry_status(meta, end, lines=None):
    """ok / silent / absent / no_handler / unknown (§5.5).
    ‏ok רק כשה-handler נרשם, סימן הבנייה קיים ונקלטה לפחות שורת AIRAM_RF אחת (כולל
    ‏"stream=start" של PR 1 — הוכחת-חיים). לא ידוע ≠ תקין: בלי end.json/סטטוס ⇒ unknown."""
    if not isinstance(meta, dict):
        return "unknown"
    if not meta.get("telemetry_marker"):
        return "absent"
    if not meta.get("handler"):
        return "no_handler"
    n = _int((end or {}).get("telemetry_lines")) if isinstance(end, dict) else None
    if n is None:
        n = _int(lines)
    if n is None:
        return "unknown"
    return "ok" if n > 0 else "silent"


# --- נרמול שורות ----------------------------------------------------------------

def _prep(rows, telemetry_ok=True):
    """עותקים מנורמלים (טיפוסים, ברירות מחדל), ממוינים לפי i. לא משנה את הקלט.
    טלמטריה לא-תקינה ⇒ ovl=None בכל שורה ("לא נבדק", לעולם לא False)."""
    out = []
    for pos, r in enumerate(rows or ()):
        if not isinstance(r, dict):
            continue
        q = dict(r)
        if not r.get("_p"):
            i = _int(r.get("i"))
            q["i"] = i if i is not None else pos
            q["cyc"] = _int(r.get("cyc"))
            q["lna"] = _int(r.get("lna"))
            q["epoch"] = _int(r.get("epoch"), 0)
            q["ifgr"] = _int(r.get("ifgr"))
            n = r.get("notch")
            q["notch"] = n if isinstance(n, bool) else None
            q["valid"] = r.get("valid") is True
            for f in _NUM_FIELDS:
                q[f] = _num(r.get(f))
            q["clip"] = max(0, _int(r.get("clip"), 0) or 0)
            q["peak"] = _int(r.get("peak"))
            o = r.get("ovl")
            q["ovl"] = o if isinstance(o, bool) else None
            b = r.get("b_tot")
            q["b_tot"] = [_num(x) for x in b] if isinstance(b, list) else None
            q["_p"] = True
        if not telemetry_ok:
            q["ovl"] = None
        out.append(q)
    out.sort(key=lambda q: q["i"])
    return out


def _gkey(r):
    return (r["lna"], r["epoch"], r["notch"])


def _clean(r):
    """סלוט שמותר להשתמש בו ל-CNR/רצפה: בלי חיתוך ובלי עומס מדווח (design §6.1)."""
    return r["clip"] == 0 and r["ovl"] is not True


# --- סיווג (§5.3) ----------------------------------------------------------------

def _classify(P, T, ref):
    cls, floor, groups = {}, {}, {}
    by = {}
    for r in P:
        if not r["valid"]:
            cls[r["i"]] = INVALID
            continue
        by.setdefault(_gkey(r), []).append(r)
    for key, G in by.items():
        tot = [r for r in G if r["c_tot"] is not None]
        info = {"silence_ref": False, "N0": None, "n_cand": 0, "n": len(G)}
        if tot:
            m = min(r["c_tot"] for r in tot)
            # הפרשים מעוגלים (_rd): הזזת קבוצה שלמה בקבוע לא משנה אף השוואה מול T.
            cand = [r for r in tot if _rd(r["c_tot"] - m) < T]
            n0 = statistics.median(r["c_tot"] for r in cand)
            nnb = [r["n_nb"] for r in cand if r["n_nb"] is not None]
            info["N0"] = n0
            info["n_cand"] = len(cand)
            # "רצפה" שהיא בעצם נשא (ATIS, תקופה שכולה שידור) נדחית מול רעש-השכנים.
            if (ref == "tower" and len(cand) >= NOISE_REF_MIN
                    and (not nnb or _rd(n0 - statistics.median(nnb)) < T)):
                info["silence_ref"] = True
        groups[key] = info
        for r in G:
            f = info["N0"] if info["silence_ref"] else r["n_nb"]
            h1, h2 = r["c_tot_h1"], r["c_tot_h2"]
            if f is None or h1 is None or h2 is None:
                cls[r["i"]] = UNCLASSIFIED
                continue
            floor[r["i"]] = f
            a, b = _rd(h1 - f) >= T, _rd(h2 - f) >= T
            cls[r["i"]] = CARRIER if (a and b) else SILENCE if (not a and not b) else EDGE
    return cls, floor, groups


def classify(rows, T, ref):
    """→ (cls {i: carrier|silence|edge|invalid|unclassified}, floor {i: dBFS},
    groups {(lna, epoch, notch): {silence_ref, N0, n_cand, n}})."""
    return _classify(_prep(rows), T, ref)


# --- סבבים, שידורים, בלוקים (§5.4) ------------------------------------------------

def _units_lna(P):
    by = {}
    for r in P:
        if r["cyc"] is None or r["lna"] is None:
            continue
        by.setdefault(r["cyc"], {}).setdefault(r["lna"], r)
    return [{"seq": c, "cyc": c, "slots": by[c]} for c in sorted(by)]


def _units_notch(P):
    """ABBA בקבוצות של 4 לפי i: ‏(A1,B1) ו-(B2,A2) הם שתי "יחידות" זוגיות. cyc = הקבוצה."""
    by = {}
    for r in P:
        if r["notch"] is None:
            continue
        g, pos = divmod(r["i"], 4)
        seq = 2 * g + (pos // 2)
        u = by.setdefault(seq, {"seq": seq, "cyc": g, "slots": {}})
        u["slots"].setdefault(r["notch"], r)
    return [by[s] for s in sorted(by)]


def _is_carrier_unit(u, cls):
    return any(cls.get(r["i"]) == CARRIER for r in u["slots"].values())


def _is_quiet_unit(u, cls):
    return (not _is_carrier_unit(u, cls)
            and any(cls.get(r["i"]) == SILENCE for r in u["slots"].values()))


def _carrier_seqs(units, cls):
    return {u["seq"] for u in units if _is_carrier_unit(u, cls)}


def _is_full(u, cls, req):
    return bool(req) and all(k in u["slots"] and cls.get(u["slots"][k]["i"]) == CARRIER
                             for k in req)


def _transmissions(units, cls, req):
    """שידור = רצף מקסימלי של סבבי-נשא; סבב שקט (בלי נשא, עם ≥1 סלוט שקט) מסיים אותו.
    סבב בלי מידע (רק edge/invalid) לא מסיים ולא מאריך — הספירה לא מתנפחת מקפיצת overflow."""
    txs, cur = [], None
    for u in units:
        if _is_carrier_unit(u, cls):
            if cur is None:
                cur = {"start_cyc": u["seq"], "end_cyc": u["seq"], "carrier_cycles": 0,
                       "full_blocks": 0}
            cur["end_cyc"] = u["seq"]
            cur["carrier_cycles"] += 1
            if _is_full(u, cls, req):
                cur["full_blocks"] += 1
        elif _is_quiet_unit(u, cls) and cur is not None:
            txs.append(cur)
            cur = None
    if cur is not None:
        txs.append(cur)
    for t in txs:
        t["captured"] = t["full_blocks"] >= 1
    return txs


def cycles(rows, cls=None):
    """→ {cyc: {lna: row}} (שורות מנורמלות)."""
    return {u["cyc"]: u["slots"] for u in _units_lna(_prep(rows))}


def _units_from_map(cyc_map):
    return [{"seq": c, "cyc": c, "slots": cyc_map[c]} for c in sorted(cyc_map)]


def transmissions(cyc_map, cls, states):
    """→ [{start_cyc, end_cyc, carrier_cycles, full_blocks, captured}]. בלוק מלא = כל
    המצבים ב-states נשא (קרא עם C כדי לקבל את ספירת המועמדים)."""
    return _transmissions(_units_from_map(cyc_map), cls, list(states))


def interior_cycles(cyc_map, cls):
    """סבבים שגם לפניהם וגם אחריהם יש סבב-נשא."""
    units = _units_from_map(cyc_map)
    cs = _carrier_seqs(units, cls)
    return sorted(u["seq"] for u in units if u["seq"] - 1 in cs and u["seq"] + 1 in cs)


# --- עובדות (§5.5) ----------------------------------------------------------------

def _facts(units, cls, keys, telemetry_ok):
    cs = _carrier_seqs(units, cls)
    acc = {k: {n: set() for n in ("op_clip", "op_ovl", "probe_clip", "probe_ovl", "lost",
                                  "env_ovl_silence", "op_clip_in", "op_ovl_in",
                                  "probe_clip_in", "probe_ovl_in", "heard_by")}
           for k in keys}
    for u in units:
        interior = (u["seq"] - 1) in cs and (u["seq"] + 1) in cs
        carriers = [k for k, r in u["slots"].items() if cls.get(r["i"]) == CARRIER]
        for k, r in u["slots"].items():
            if k not in acc or not r["valid"]:
                continue
            a = acc[k]
            c = cls.get(r["i"])
            # עובדה פוסלת רק ב-IFGR=59: גם ה-AGC של הייצור לא יכול להפחית יותר (MAX_BB_GR).
            op = r["ifgr"] is not None and r["ifgr"] >= IFGR_MAX
            if r["clip"] > 0:
                a["op_clip" if op else "probe_clip"].add(u["cyc"])
                a["op_clip_in" if op else "probe_clip_in"].add(c)
            if telemetry_ok and r["ovl"] is True:
                a["op_ovl" if op else "probe_ovl"].add(u["cyc"])
                a["op_ovl_in" if op else "probe_ovl_in"].add(c)
                if op and c == SILENCE:
                    a["env_ovl_silence"].add(u["cyc"])
            if interior and c == SILENCE:
                others = [x for x in carriers if x != k]
                if others:
                    a["lost"].add(u["cyc"])
                    a["heard_by"].update(others)
    out = {}
    for k in keys:
        a = acc[k]
        out[k] = {
            "op_clip": len(a["op_clip"]), "probe_clip": len(a["probe_clip"]),
            "op_ovl": len(a["op_ovl"]) if telemetry_ok else None,
            "probe_ovl": len(a["probe_ovl"]) if telemetry_ok else None,
            "env_ovl_silence": len(a["env_ovl_silence"]) if telemetry_ok else None,
            "lost": len(a["lost"]), "heard_by": sorted(a["heard_by"]),
            "op_clip_in": sorted(a["op_clip_in"]), "op_ovl_in": sorted(a["op_ovl_in"]),
            "probe_clip_in": sorted(a["probe_clip_in"]),
            "probe_ovl_in": sorted(a["probe_ovl_in"]),
        }
    return out


def facts(cyc_map, cls, states, telemetry_ok=True):
    """→ {state: {op_clip, op_ovl, probe_clip, probe_ovl, env_ovl_silence, lost, heard_by,
    *_in}}. ספירות = מספר סבבים שונים. op_ovl/probe_ovl/env = None כשהטלמטריה לא ok."""
    return _facts(_units_from_map(cyc_map), cls, list(states), telemetry_ok)


def _merge_facts(fa, fb):
    """איחוד עובדות מגדל+ATIS: סבבים שונים בשתי ריצות ⇒ הספירות מתחברות."""
    out = {}
    for k in set(fa) | set(fb):
        a, b = fa.get(k), fb.get(k)
        if a is None or b is None:
            out[k] = dict(a or b)
            continue
        m = {}
        for n in ("op_clip", "probe_clip", "lost"):
            m[n] = a[n] + b[n]
        for n in ("op_ovl", "probe_ovl", "env_ovl_silence"):
            vals = [x for x in (a[n], b[n]) if x is not None]
            m[n] = sum(vals) if vals else None
        for n in ("heard_by", "op_clip_in", "op_ovl_in", "probe_clip_in", "probe_ovl_in"):
            m[n] = sorted(set(a[n]) | set(b[n]))
        out[k] = m
    return out


def _fact_status(op, probe):
    """observed (≥2) / observed_once / probe_only / not_observed / None (לא נבדק)."""
    if op is None:
        return None
    if op >= FACT_MIN_CYCLES:
        return "observed"
    if op == 1:
        return "observed_once"
    if probe:
        return "probe_only"
    return "not_observed"


# --- CNR ו-U (§5.6) ----------------------------------------------------------------

def _cnr(P, cls, groups):
    """→ {i: (CNR, "silence"|"proxy")} לסלוטי נשא נקיים."""
    use_t = all(r["t0"] is not None for r in P)

    def tt(r):
        return r["t0"] if use_t else float(r["i"])

    sil = {}
    for r in P:
        if cls.get(r["i"]) == SILENCE and _clean(r) and r["c_tot"] is not None:
            sil.setdefault(_gkey(r), []).append(r)
    out = {}
    for r in P:
        if cls.get(r["i"]) != CARRIER or r["c_car"] is None or not _clean(r):
            continue
        g = groups.get(_gkey(r))
        if g and g["silence_ref"]:
            t = tt(r)
            near = sorted(sil.get(_gkey(r), ()), key=lambda s: (abs(tt(s) - t), s["i"]))
            near = near[:NOISE_REF_MAX]
            if len(near) < NOISE_REF_MIN:
                continue
            ref, kind = statistics.median(s["c_tot"] for s in near), "silence"
        else:
            if r["n_nb"] is None:
                continue
            ref, kind = r["n_nb"], "proxy"
        out[r["i"]] = (_rd(r["c_car"] - ref), kind)
    return out


def cnr(rows, cls, groups):
    """→ {i: CNR dB}. ‏N_ref: חציון עד 6 סלוטי שקט קרובים מאותה קבוצה (≥3), אחרת n_nb."""
    return {i: v for i, (v, _k) in _cnr(_prep(rows), cls, groups).items()}


def _pairs(units, cnr_map, keys):
    ks = sorted(keys)
    keyset = set(ks)
    plist = [(a, b) for x, a in enumerate(ks) for b in ks[x + 1:]]
    out = {p: [] for p in plist}
    kinds = {p: set() for p in plist}
    for u in units:
        vals = {k: cnr_map[r["i"]] for k, r in u["slots"].items()
                if k in keyset and r["i"] in cnr_map}
        for (a, b) in plist:
            if a in vals and b in vals:
                out[(a, b)].append(_rd(vals[b][0] - vals[a][0]))
                kinds[(a, b)].update((vals[a][1], vals[b][1]))
    return out, kinds


def pairs(cyc_map, cnr_map, states):
    """→ {(a, b): [D…]} עם a<b ו-D = CNR_b − CNR_a, לכל סבב שבו שניהם נשא עם CNR."""
    m = {i: (v, "x") for i, v in cnr_map.items()}
    return _pairs(_units_from_map(cyc_map), m, list(states))[0]


def _u_maps(units, cls, groups, keys):
    ks = sorted(keys)
    plist = [(a, b) for x, a in enumerate(ks) for b in ks[x + 1:]]
    sil = {p: [] for p in plist}
    prx = {p: [] for p in plist}
    for u in units:
        S = u["slots"]
        for (a, b) in plist:
            ra, rb = S.get(a), S.get(b)
            if ra is None or rb is None:
                continue
            ca, cb = cls.get(ra["i"]), cls.get(rb["i"])
            if ca == SILENCE and cb == SILENCE:
                ga, gb = groups.get(_gkey(ra)), groups.get(_gkey(rb))
                if (ga and gb and ga["silence_ref"] and gb["silence_ref"] and _clean(ra)
                        and _clean(rb) and ra["c_tot"] is not None and rb["c_tot"] is not None):
                    sil[(a, b)].append(_rd((rb["c_tot"] - gb["N0"]) - (ra["c_tot"] - ga["N0"])))
            elif (ca == CARRIER and cb == CARRIER and ra["n_nb"] is not None
                  and rb["n_nb"] is not None):
                prx[(a, b)].append(_rd(rb["n_nb"] - ra["n_nb"]))
    us = {p: (_rd(_iqr(v)) if len(v) >= U_MIN_CYCLES else None) for p, v in sil.items()}
    up = {p: (_rd(_iqr(v)) if len(v) >= U_MIN_CYCLES else None) for p, v in prx.items()}
    return us, up


def _u_for(p, kinds, us, up):
    """ה-U שמתאים לרצפות שבהן השתמשו ה-D של הזוג; שילוב ⇒ השמרני (max), וחייב את שניהם."""
    k = kinds.get(p) or set()
    a, b = us.get(p), up.get(p)
    if k == {"silence"}:
        return a
    if k == {"proxy"}:
        return b
    if k == {"silence", "proxy"}:
        return max(a, b) if (a is not None and b is not None) else None
    return a if a is not None else b


def U(cyc_map, cls, groups, a, b):
    """U(a,b): ‏IQR של שאריות רעש זוגיות (שקט), או של ‎n_nb_b − n_nb_a בסבבי נשא (proxy).
    ‏None כשיש פחות מ-4 סבבים — אז ההשוואה "לא נבדקה" (לא הונח)."""
    units = _units_from_map(cyc_map)
    us, up = _u_maps(units, cls, groups, [a, b])
    p = tuple(sorted((a, b)))
    return us.get(p) if us.get(p) is not None else up.get(p)


# --- השוואות: Holm על כל הזוגות (סטייה (א)) ----------------------------------------

def compare_states(pair_d, u_map, cand, alpha=ALPHA):
    """השוואת כל זוגות המועמדים: מבחן סימן מדויק דו-צדדי + Holm על m=k(k−1)/2 + שער U.
    a<b (b מוחלש יותר), D = CNR_b − CNR_a:
      D>0 מובהק ⇒ b טוב מ-a ⇒ compression_evidence(a) (אי-ליניאריות ברווח הגבוה);
      D<0 מובהק ⇒ a טוב מ-b ⇒ sensitivity_cost(b).
    "מובהק" = Holm דוחה ∧ n ≥ n_min ∧ U ידוע ∧ |median D| > U.
    "נבדק" (testable) = n ≥ n_min ∧ U ידוע."""
    cand = sorted(set(cand))
    plist = [(a, b) for x, a in enumerate(cand) for b in cand[x + 1:]]
    m = len(plist)
    nm = n_min(m, alpha)
    comps, pv = [], []
    for a, b in plist:
        d = [x for x in pair_d.get((a, b), ()) if x is not None]
        nz = [x for x in d if x != 0]
        kp = sum(1 for x in nz if x > 0)
        kn = len(nz) - kp
        n = len(nz)
        p = min(1.0, 2.0 * min(sign_test_p(kp, n), sign_test_p(kn, n))) if n else 1.0
        u = u_map.get((a, b))
        comps.append({"a": a, "b": b, "n": n, "n_pairs": len(d), "k_pos": kp, "k_neg": kn,
                      "median_d": _rd(statistics.median(d)) if d else None, "u": u,
                      "p": p, "testable": bool(nm is not None and n >= nm and u is not None)})
        pv.append(p)
    rej = holm(pv, alpha)
    worse, compression, sensitivity = set(), {}, {}
    for c, rj in zip(comps, rej):
        c["p_holm_reject"] = bool(rj)
        c["better"] = c["worse"] = None
        md = c["median_d"]
        if rj and c["testable"] and md is not None and abs(md) > c["u"]:
            if md > 0 and c["k_pos"] > c["k_neg"]:
                c["better"], c["worse"] = c["b"], c["a"]
                compression.setdefault(c["a"], []).append(c)
            elif md < 0 and c["k_neg"] > c["k_pos"]:
                c["better"], c["worse"] = c["a"], c["b"]
                sensitivity.setdefault(c["b"], []).append(c)
        c["significant"] = c["worse"] is not None
        if c["worse"] is not None:
            worse.add(c["worse"])
    return {"comparisons": comps, "m": m, "n_min": nm, "worse": worse,
            "compression": compression, "sensitivity": sensitivity,
            "all_testable": bool(m) and all(c["testable"] for c in comps)}


# --- בדיקות עצמיות ----------------------------------------------------------------

def _settle_check(P, cls):
    """מבחן סימן (p≤0.01, n≥10) על ‎b_tot[0] − median(b_tot[1:])‎ בסלוטי שקט שגם הקודם להם
    שקט, מכוון לכיוון רמת הסלוט הקודם: חיובי = המאגר הראשון "זוכר" את המצב הקודם ⇒ מגן
    ההתייצבות קצר מדי. מדווח בלבד (design §9)."""
    by_i = {r["i"]: r for r in P}
    d = []
    for r in P:
        p = by_i.get(r["i"] - 1)
        if (p is None or cls.get(r["i"]) != SILENCE or cls.get(p["i"]) != SILENCE
                or (r["lna"], r["ifgr"], r["notch"]) == (p["lna"], p["ifgr"], p["notch"])):
            continue
        b, pb = r["b_tot"], p["b_tot"]
        if not b or len(b) < 2 or b[0] is None or not pb:
            continue
        cur = _median(b[1:])
        prev = _median(pb)
        if cur is None or prev is None or prev == cur:
            continue
        sgn = 1.0 if prev > cur else -1.0
        d.append(_rd((b[0] - cur) * sgn))
    nz = [x for x in d if x != 0]
    n = len(nz)
    k = sum(1 for x in nz if x > 0)
    p = sign_test_p(k, n) if n else None
    return {"suspect": bool(n >= SETTLE_MIN_N and p is not None and p <= SETTLE_ALPHA),
            "p": _rd(p), "n": n, "median_db": _rd(_median(d), 2)}


def _proxy_check(P, cls, groups):
    """בריצת מגדל: ‎c_tot − n_nb בסלוטי שקט של קבוצות silence_ref — כמה ה-proxy רחוק
    מהרעש האמיתי ב-bin (‏n_nb הוא חציון ומינימום על 8 שכנים ⇒ מוטה כלפי מטה; מוצג גולמי)."""
    d = []
    for r in P:
        g = groups.get(_gkey(r))
        if (cls.get(r["i"]) == SILENCE and g and g["silence_ref"] and _clean(r)
                and r["c_tot"] is not None and r["n_nb"] is not None):
            d.append(r["c_tot"] - r["n_nb"])
    if len(d) < U_MIN_CYCLES:
        return None
    return {"median_diff": _rd(_median(d), 2), "u": _rd(_iqr(d), 2), "n": len(d)}


# --- ליבת שלב ה-LNA ----------------------------------------------------------------

def _ok_state(s):
    return s is not None and STATE_MIN <= s <= STATE_MAX


def _states_of(ctx, meta, P):
    """מצבי ה-LNA של הריצה (0..9 בלבד — מחוץ לטווח אין שורת GR ואין משמעות)."""
    for src in (ctx.get("states"), (meta or {}).get("states")):
        if isinstance(src, (list, tuple)) and src:
            vals = sorted({_int(s) for s in src if _ok_state(_int(s))})
            if vals:
                return vals
    return sorted({r["lna"] for r in P if _ok_state(r["lna"])})


def _gr_table(meta, ctx):
    for src in ((meta or {}).get("gr_table"), ctx.get("gr_table")):
        if isinstance(src, (list, tuple)) and len(src) == 10:
            vals = [_num(x) for x in src]
            if all(v is not None for v in vals):
                return vals
    return None


def _cfg(ctx):
    c = ctx.get("config_at_start")
    return c if isinstance(c, dict) else {}


def _cur_state(ctx):
    s = _int(_cfg(ctx).get("rf_gain"))
    return s if s is not None and STATE_MIN <= s <= STATE_MAX else None


def _lna_core(rows, meta, end, ctx, ref, extra_facts=None, telemetry_lines=None):
    tel = telemetry_status(meta, end, telemetry_lines)
    tel_ok = tel == "ok"
    P = _prep(rows, tel_ok)
    T = threshold_db(_cfg(ctx))
    states = _states_of(ctx, meta, P)
    cls, floor, groups = _classify(P, T, ref)
    units = _units_lna(P)
    fx_own = _facts(units, cls, states, tel_ok)
    fx = _merge_facts(fx_own, extra_facts) if extra_facts else fx_own

    D = sorted(s for s in states if fx[s]["op_clip"] >= FACT_MIN_CYCLES
               or (fx[s]["op_ovl"] or 0) >= FACT_MIN_CYCLES)
    lo = max(D) if D else -1
    L = sorted(s for s in states if fx[s]["lost"] >= FACT_MIN_CYCLES)
    L_eff = [s for s in L if s > lo]                       # סטייה (ב)
    hi = min(L_eff) if L_eff else STATE_MAX + 1
    C = [s for s in states if lo < s < hi]

    txs = _transmissions(units, cls, C)                     # בלוק מלא על C — סטייה (ד)
    full_all = sum(1 for u in units if _is_full(u, cls, states))
    cmap = _cnr(P, cls, groups)
    pd, kinds = _pairs(units, cmap, states)
    us, up = _u_maps(units, cls, groups, states)
    u_map = {p: _u_for(p, kinds, us, up) for p in pd}
    cmp = compare_states(pd, u_map, C)
    cnr_vals = {s: [] for s in states}
    for r in P:
        if r["i"] in cmap and r["lna"] in cnr_vals:
            cnr_vals[r["lna"]].append(cmap[r["i"]][0])
    return {"tel": tel, "tel_ok": tel_ok, "P": P, "T": T, "ref": ref, "states": states,
            "cls": cls, "floor": floor, "groups": groups, "units": units, "facts_own": fx_own,
            "facts": fx, "D": D, "L": L, "L_eff": L_eff, "lo": lo, "hi": hi, "C": C,
            "txs": txs, "full_all": full_all, "cnr": cmap, "pairs": pd, "kinds": kinds,
            "u": u_map, "cmp": cmp, "cnr_vals": cnr_vals,
            "n_carrier": sum(1 for v in cls.values() if v == CARRIER)}


def _best(core, cands, min_n):
    cv = core["cnr_vals"]
    ok = [s for s in cands if len(cv.get(s, ())) >= min_n]
    if not ok:
        return None
    # שוויון בחציון ⇒ המצב המוחלש יותר (כלל 3).
    return max(ok, key=lambda s: (_rd(statistics.median(cv[s])), s))


def _oriented(core, best, c):
    """D של c מול best, מכוון ‎CNR_c − CNR_best."""
    a, b = sorted((best, c))
    d = core["pairs"].get((a, b), [])
    return (list(d) if (a, b) == (best, c) else [-x for x in d]), core["u"].get((a, b))


def _verdict(core, ctx, error):
    cur = _cur_state(ctx)
    C, D, L_eff, lo, hi = core["C"], core["D"], core["L_eff"], core["lo"], core["hi"]
    cmp = core["cmp"]
    best = _best(core, C, 3)
    fact_rec = fact_head = None
    if C and cur is not None:
        if cur <= lo:
            fact_rec, fact_head = min(C), "reduce_gain_overload"
        elif cur >= hi:
            fact_rec, fact_head = max(C), "increase_gain_lost"

    stat_ok, tie, rec_stat, contradiction = False, [], None, False
    sens = sorted(cmp["sensitivity"])
    if len(C) >= 2 and best is not None and cmp["all_testable"] and cur is not None:
        # TieSet, ואז שומר מונוטוני לצד הרגישות (סטייה (ט)): לא מעל sensitivity_cost מובהק.
        tie = [s for s in C if s not in cmp["worse"] and (not sens or s < sens[0])]
        if tie:
            rec_stat = max(tie)
            stat_ok = True
            # כלל 2: העלאת רווח מחייבת sensitivity_cost מובהק במצב הנוכחי או במצב מוחלש-פחות
            # ממנו (שבשרשרת ליניארית גורר אותו גם בנוכחי) — סטייה (ח).
            if cur in C and rec_stat < cur and not any(x <= cur for x in sens):
                stat_ok, contradiction = False, True

    rec, flagged = None, None
    if error:
        level, head = "none", "error"
    elif not C:
        if D:
            level, head = "fact", "all_overloaded"
        else:
            level, head = "none", ("indication" if core["n_carrier"] else "no_traffic")
    elif stat_ok:
        level, rec = "stat", rec_stat
        if fact_head:
            head = fact_head
        elif rec == cur:
            head = "keep_current"
        elif rec > cur:
            head = ("reduce_gain_compression" if cur in cmp["compression"]
                    else "reduce_gain_tie")
        else:
            head = "increase_gain_sensitivity"
            # x = המצב שבו הרגישות *נמדדה* נמוכה (הנוכחי, או הגבוה מבין המסומנים שמתחתיו).
            flagged = max((x for x in sens if x <= cur), default=None)
    elif fact_rec is not None:
        level, rec, head = "fact", fact_rec, fact_head
    elif len(C) == 1 and (D or L_eff) and cur == C[0]:
        # העובדות פסלו את כל החלופות שנבדקו; נשאר רק הנוכחי.
        level, rec, head = "fact", cur, "keep_current"
    elif core["n_carrier"]:
        level, head = "indication", "indication"
    else:
        level, head = "none", "no_traffic"
    return {"cur": cur, "best": best, "tie": sorted(tie) if stat_ok else [],
            "rec": rec, "level": level, "headline": head, "fact_rec": fact_rec,
            "stat_ok": stat_ok, "contradiction": contradiction, "flagged": flagged}


def _indication(core, v):
    """כיוון אפשרי (סטייה (ג)): best לפי חציון CNR (≥1 ערך), ועוד כל מצב שיש לו ≥1 זוג
    וש-median(D) שלו ≥ −U (שוויון בתוך הפיזור או טוב יותר). שוויון ⇒ המוחלש יותר."""
    C = core["C"]
    cands = [s for s in C if core["cnr_vals"].get(s)]
    if not cands:
        return {"rf_gain": None, "best": None, "states": [], "n": core_full_blocks(core),
                "reasons": []}
    bi = _best(core, cands, 1)
    inc, worse_above = [bi], []
    for c in cands:
        if c == bi:
            continue
        d, u = _oriented(core, bi, c)
        if not d:
            continue
        if statistics.median(d) >= -(u or 0.0):
            inc.append(c)
        elif c > bi:
            worse_above.append(c)
    if worse_above:                                   # שומר מונוטוני — סטייה (ט)
        inc = [s for s in inc if s < min(worse_above)]
    return {"rf_gain": max(inc), "best": bi, "states": sorted(inc),
            "n": core_full_blocks(core), "reasons": []}


def core_full_blocks(core):
    return sum(t["full_blocks"] for t in core["txs"])


def _fact_reasons(core):
    fx, out = core["facts"], []
    for code, key in (("op_clip", "op_clip"), ("op_ovl", "op_ovl")):
        st = [s for s in core["states"] if (fx[s][key] or 0) >= FACT_MIN_CYCLES]
        if st:
            counts = {str(s): fx[s][key] for s in st}
            out.append({"code": code, "states": st, "cycles": max(counts.values()),
                        "counts": counts})
    env = [s for s in core["states"] if (fx[s]["env_ovl_silence"] or 0) >= FACT_MIN_CYCLES]
    if env:
        out.append({"code": "env_ovl_silence", "states": env})
    for s in core["L"]:
        out.append({"code": "lost", "state": s, "heard_by": fx[s]["heard_by"],
                    "cycles": fx[s]["lost"]})
    return out


def _stat_reasons(core, v):
    out = []
    cmp = core["cmp"]
    for a in sorted(cmp["compression"]):
        c = max(cmp["compression"][a], key=lambda x: abs(x["median_d"]))
        out.append({"code": "compression", "a": a, "b": c["b"], "median_d": _rd(c["median_d"], 2),
                    "n": c["n"], "p": _rd(c["p"])})
    for b in sorted(cmp["sensitivity"]):
        c = max(cmp["sensitivity"][b], key=lambda x: abs(x["median_d"]))
        out.append({"code": "sensitivity", "c": b, "best": c["a"],
                    "median_d": _rd(-c["median_d"], 2), "n": c["n"], "p": _rd(c["p"])})
    if v["stat_ok"] and len(v["tie"]) >= 2:
        out.append({"code": "tie", "states": v["tie"]})
    return out


def _uses_proxy(core):
    return any(k == "proxy" for _v, k in core["cnr"].values())


def _per_state(core, meta, ctx, v):
    P, cls, groups, fx = core["P"], core["cls"], core["groups"], core["facts"]
    gr = _gr_table(meta, ctx)
    cur = v["cur"] if v else _cur_state(ctx)
    ist = (meta or {}).get("ifgr_start") if isinstance((meta or {}).get("ifgr_start"), dict) else {}
    ref = ctx.get("ifgr_ref") if isinstance(ctx.get("ifgr_ref"), dict) else {}
    ifgr_ref = _int(ref.get("value"))
    cmp = core["cmp"]
    out = []
    for s in core["states"]:
        rs = [r for r in P if r["lna"] == s]
        valid = [r for r in rs if r["valid"]]
        cl = [cls.get(r["i"]) for r in rs]
        g0 = _int(ist.get(str(s)))
        if g0 is None:
            g0 = next((r["ifgr"] for r in rs if r["ifgr"] is not None), None)
        gs = [r["ifgr"] for r in rs if r["ifgr"] is not None]
        gfin = max(gs) if gs else g0                     # ה-ratchet לעולם לא יורד
        clamped = None
        if gr is not None and ifgr_ref is not None and cur is not None:
            want = ifgr_ref + gr[cur] - gr[s]
            clamped = "low" if want < IFGR_MIN else "high" if want > IFGR_MAX else None
        eps = [r["epoch"] for r in rs]
        ratchets = max(eps) if eps else 0
        # רצפת הרעש של התקופה האחרונה (תצוגה בלבד; ב-dBFS — תלוי ב-IF של הבדיקה).
        nref = noise = None
        if rs:
            last = max(rs, key=lambda r: (r["epoch"], r["i"]))
            g = groups.get(_gkey(last))
            if g is not None:
                if g["silence_ref"]:
                    nref, noise = "silence", g["N0"]
                else:
                    nref = "proxy"
                    noise = _median(r["n_nb"] for r in rs if r["epoch"] == last["epoch"])
        cv = core["cnr_vals"].get(s, [])
        f = fx[s]
        flags = []
        if s in core["D"]:
            flags.append("disqualified")
        if s in core["L"]:
            flags.append("lost")
        if s not in core["C"]:
            flags.append("excluded")
        if s in cmp["compression"]:
            flags.append("compression_evidence")
        if s in cmp["sensitivity"]:
            flags.append("sensitivity_cost")
        if v and v["best"] == s:
            flags.append("best")
        if v and s in v["tie"]:
            flags.append("tie")
        if v and v["rec"] == s:
            flags.append("recommended")
        if cur == s:
            flags.append("current")
        if ratchets:
            flags.append("ratchet")
        if clamped:
            flags.append("clamped_" + clamped)
        if f["probe_clip"]:
            flags.append("probe_clip")
        if f["probe_ovl"]:
            flags.append("probe_ovl")
        pk = [r["peak"] for r in valid if r["peak"] is not None]
        out.append({
            "lna": s, "label": label(s), "gr_db": gr[s] if gr is not None else None,
            "ifgr_start": g0, "ifgr_final": gfin, "clamped": clamped, "ratchets": ratchets,
            "valid": len(valid), "invalid": len(rs) - len(valid),
            "n_carrier": cl.count(CARRIER), "n_silence": cl.count(SILENCE),
            "n_edge": cl.count(EDGE), "n_unclassified": cl.count(UNCLASSIFIED),
            "noise_ref": nref, "noise_dbfs": _rd(noise, 2),
            "cnr_median": _rd(_median(cv), 2), "cnr_p25": _rd(_quantile(cv, .25), 2),
            "cnr_p75": _rd(_quantile(cv, .75), 2), "n_cnr": len(cv),
            "op_clip": f["op_clip"], "op_ovl": f["op_ovl"], "lost": f["lost"],
            "lost_heard_by": f["heard_by"], "probe_clip": f["probe_clip"],
            "probe_ovl": f["probe_ovl"], "env_ovl_silence": f["env_ovl_silence"],
            "op_clip_in": f["op_clip_in"], "op_ovl_in": f["op_ovl_in"],
            "overload": _fact_status(f["op_ovl"], f["probe_ovl"]),
            "clip": _fact_status(f["op_clip"], f["probe_clip"]),
            "flags": flags, "peak_max": max(pk) if pk else None,
            "p_wb_median": _rd(_median(r["p_wb"] for r in valid), 2),
        })
    return out


def _comparisons_out(core):
    out = []
    for c in core["cmp"]["comparisons"]:
        out.append({"a": c["a"], "b": c["b"], "n": c["n"], "n_pairs": c["n_pairs"],
                    "median_d": _rd(c["median_d"], 2), "u": _rd(c["u"], 2), "p": _rd(c["p"]),
                    "p_holm_reject": c["p_holm_reject"], "testable": c["testable"],
                    "significant": c["significant"], "worse": c["worse"], "better": c["better"]})
    return out


def _phase_summary(core, meta, end, ctx):
    fx = core["facts_own"]
    return {"run_id": (meta or {}).get("run_id") or ctx.get("run_id"),
            "freq": ctx.get("freq"), "ref": core["ref"], "rows": len(core["P"]),
            "cycles": len(core["units"]), "transmissions": len(core["txs"]),
            "tx_captured": sum(1 for t in core["txs"] if t["captured"]),
            "full_blocks": core_full_blocks(core), "n_carrier": core["n_carrier"],
            "telemetry": core["tel"], "ended": (end or {}).get("ended"),
            "facts": {str(s): {"op_clip": f["op_clip"], "op_ovl": f["op_ovl"],
                               "lost": f["lost"], "probe_clip": f["probe_clip"],
                               "probe_ovl": f["probe_ovl"],
                               "env_ovl_silence": f["env_ovl_silence"]}
                      for s, f in fx.items()}}


def _selfcheck(core, meta, end, ctx, proxy_core):
    P = core["P"]
    meta = meta if isinstance(meta, dict) else {}
    end = end if isinstance(end, dict) else {}
    ver = ctx.get("verified") if isinstance(ctx.get("verified"), dict) else None
    inv = sum(1 for r in P if not r["valid"])
    proc = [r["proc_ms"] for r in P if r["proc_ms"] is not None]
    peaks = [r["peak"] for r in P if r["peak"] is not None]
    ov = _int(end.get("overflows"))
    grt = _int(end.get("gr_timeouts"))
    if ov is None:
        ov = sum(1 for r in P if r.get("inv") == "overflow")
    if grt is None:
        grt = sum(1 for r in P if r.get("inv") == "gr_timeout")
    nrb = meta.get("notch_readback")
    exp = _cfg(ctx).get("fm_notch")
    nrb_ok = None
    if nrb in ("true", "false") and isinstance(exp, bool) and core.get("phase") != "notch":
        nrb_ok = (nrb == "true") == exp
    rows_rb = [(r.get("notch_rb"), r["notch"]) for r in P if r.get("notch_rb") is not None]
    if rows_rb:
        ok = all(str(rb).lower() == ("true" if n else "false") for rb, n in rows_rb)
        nrb_ok = ok if nrb_ok is None else (nrb_ok and ok)
    return {
        "telemetry": core["tel"],
        "overload_semantics_verified": bool(ver and ver.get("overload_reported_with_agc_off") is True),
        "settle": _settle_check(P, core["cls"]),
        "guard_buffers": ctx.get("guard_buffers"),
        "guard_verified": bool(ver and "guard_buffers" in ver),
        "overflows": ov, "gr_timeouts": grt,
        "invalid_frac": _rd(inv / len(P), 3) if P else None,
        "proc_ms_p50": _rd(_quantile(proc, .5), 1), "proc_ms_p95": _rd(_quantile(proc, .95), 1),
        "light_mode": end.get("light_mode"), "rate_measured": end.get("rate_measured"),
        "rail_code": ctx.get("rail_code"),
        "rail_source": "verified" if (ver and "rail_code" in ver) else "driver_source",
        "peak_code_max": max(peaks) if peaks else None,
        "bias_t_forced_off": meta.get("bias_t_forced_off"),
        "notch_readback_ok": nrb_ok,
        "proxy_check": (_proxy_check(proxy_core["P"], proxy_core["cls"], proxy_core["groups"])
                        if proxy_core else None),
        "hw": meta.get("hw"),
        "verified": bool(ver),
    }


def _untestable(core, ctx, meta, sc, level, ref_label, per_state, blocks_target):
    out = []
    if "tower" in ref_label:
        out.append({"code": "soft_compression_tower"})
    if not sc["guard_verified"]:
        out.append({"code": "settle_unverified"})
    tel = core["tel"]
    if tel == "ok" and not sc["overload_semantics_verified"]:
        out.append({"code": "overload_semantics_unverified"})
    if tel == "silent":
        out.append({"code": "telemetry_silent"})
    elif tel != "ok":
        out.append({"code": "telemetry_absent", "status": tel})
    if sc["rail_source"] != "verified":
        out.append({"code": "rail_unverified"})
    if _cfg(ctx).get("agc", True) is not False:
        out.append({"code": "agc_dynamics"})
    if _uses_proxy(core):
        out.append({"code": "proxy_noise"})
    if level != "stat" and core_full_blocks(core) < blocks_target:
        out.append({"code": "few_blocks"})
    if sc["invalid_frac"] is not None and sc["invalid_frac"] > INVALID_MAX_FRAC:
        out.append({"code": "cpu_invalid"})
    lowc = [p["lna"] for p in per_state if p["clamped"] == "low"]
    highc = [p["lna"] for p in per_state if p["clamped"] == "high"]
    rat = [p["lna"] for p in per_state if p["ratchets"]]
    if lowc:
        out.append({"code": "ifgr_clamped_low", "states": lowc})
    if highc:
        out.append({"code": "ifgr_clamped_high", "states": highc})
    if rat:
        out.append({"code": "ratchets", "states": rat})
    if _gr_table(meta, ctx) is None:
        out.append({"code": "gr_table_unknown"})
    return out


def _error_of(ctx, end):
    e = ctx.get("error")
    if e:
        return str(e)
    ended = (end or {}).get("ended") if isinstance(end, dict) else None
    if ended in ("error", "device_lost"):
        return str((end or {}).get("error") or ended)
    return None


def _headline_params(v, core, ind):
    h, cur, rec = v["headline"], v["cur"], v["rec"]
    if h in ("keep_current",):
        return {"x": cur}
    if h == "increase_gain_sensitivity":
        return {"x": v["flagged"] if v["flagged"] is not None else cur, "cur": cur, "y": rec}
    if h in ("reduce_gain_tie", "reduce_gain_compression", "increase_gain_lost"):
        return {"x": cur, "y": rec}
    if h == "reduce_gain_overload":
        return {"list": core["D"], "x": cur, "y": rec}
    if h == "all_overloaded":
        return {"list": core["D"]}
    if h == "indication":
        return {"n": core_full_blocks(core), "y": (ind or {}).get("rf_gain")}
    return {}


def _base_result(ctx, meta, end, phase, ref_label):
    meta = meta if isinstance(meta, dict) else {}
    end = end if isinstance(end, dict) else {}
    ir = ctx.get("ifgr_ref") if isinstance(ctx.get("ifgr_ref"), dict) else None
    return {
        "v": 1, "run_id": ctx.get("run_id") or meta.get("run_id"),
        "parent_id": ctx.get("parent_id"), "phase": phase, "ref": ref_label,
        "freq": ctx.get("freq"), "atis_freq": ctx.get("atis_freq"),
        "started_at": ctx.get("started_at"), "ended_at": ctx.get("ended_at"),
        "ended": ctx.get("ended") or end.get("ended"),
        "config_at_start": dict(_cfg(ctx)) or None,
        "ifgr_ref": {"value": _int(ir.get("value")), "source": ir.get("source")} if ir else None,
        "restore": ctx.get("restore"),
    }


def analyze(rows, meta, end, ctx):
    """התוצאה המלאה (§5.9) לשלב lna (כולל מיזוג מגדל+ATIS דרך ctx["tower"]); notch מועבר
    ל-analyze_notch. לעולם לא זורק על נתונים חסרים — שדות לא-ידועים הם None."""
    ctx = ctx if isinstance(ctx, dict) else {}
    meta = meta if isinstance(meta, dict) else {}
    phase = ctx.get("phase") or meta.get("phase") or "lna"
    if phase == "notch":
        return analyze_notch(rows, meta, end, ctx)
    if phase == "diagnose":
        # האבחון לא נשפט כאן: diagnose.json נשמר כמות שהוא (app.py).
        r = _base_result(ctx, meta, end, "diagnose", "atis")
        r.update(level="none", headline=None, recommendation=None, apply_allowed=False)
        return r
    ref = ctx.get("ref") or meta.get("ref") or "tower"
    tower = ctx.get("tower") if isinstance(ctx.get("tower"), dict) else None
    tcore = None
    if tower and ref == "atis":
        tcore = _lna_core(tower.get("rows") or [], tower.get("meta"), tower.get("end"),
                          ctx, "tower")
    core = _lna_core(rows, meta, end, ctx, ref,
                     extra_facts=tcore["facts_own"] if tcore else None)
    core["phase"] = "lna"
    ref_label = "tower+atis" if tcore else ref
    error = _error_of(ctx, end)
    v = _verdict(core, ctx, error)
    ind = None
    if not error and v["level"] in ("indication", "fact") and core["n_carrier"] and core["C"]:
        ind = _indication(core, v)
    reasons = _fact_reasons(core) + _stat_reasons(core, v)
    if "atis" in ref_label:
        reasons.append({"code": "atis_based"})
    if _uses_proxy(core):
        reasons.append({"code": "proxy_noise"})
    if ind is not None:
        ind["reasons"] = list(reasons)

    cfg = _cfg(ctx)
    per_state = _per_state(core, meta, ctx, v)
    rec = None
    if v["rec"] is not None:
        agc = cfg.get("agc", True) is not False
        ps_by = {p["lna"]: p for p in per_state}
        # תחת רווח ידני "החל" קובע גם את נקודת ה-IF שנמדדה בפועל (החלטת משתמש 5).
        if_gain = None if agc else ps_by.get(v["rec"], {}).get("ifgr_final")
        changes = (v["rec"] != _cur_state(ctx)
                   or (if_gain is not None and if_gain != _int(cfg.get("if_gain"))))
        rec = {"rf_gain": v["rec"], "if_gain": if_gain, "fm_notch": None,
               "basis": v["level"], "reasons": list(reasons), "changes": bool(changes)}

    refine = None
    if rec is not None and v["level"] in ("fact", "stat"):
        y = rec["rf_gain"]
        nb = [s for s in (y - 1, y + 1) if STATE_MIN <= s <= STATE_MAX]
        if any(s not in core["states"] for s in nb):
            refine = sorted({y, *nb})

    proxy_core = tcore if tcore else (core if ref == "tower" else None)
    sc = _selfcheck(core, meta, end, ctx, proxy_core)
    blocks_target = ATIS_TARGET_BLOCKS if ref == "atis" else TARGET_BLOCKS
    comps = [c for c in core["cmp"]["comparisons"]]
    out = _base_result(ctx, meta, end, "lna", ref_label)
    out.update({
        "states": core["states"], "current_state": v["cur"],
        "level": v["level"], "headline": v["headline"],
        "headline_params": _headline_params(v, core, ind),
        "recommendation": rec, "indication": ind, "refine_suggestion": refine,
        "apply_allowed": bool(rec is not None and v["level"] in ("fact", "stat")
                              and rec["changes"]),
        "evidence": reasons,
        "per_state": per_state,
        "comparisons": _comparisons_out(core), "method": "all_pairs_holm",
        "alpha": ALPHA, "n_min": core["cmp"]["n_min"],
        "n": min((c["n"] for c in comps), default=0),
        "best": v["best"], "tie_set": v["tie"], "candidates": core["C"],
        "disqualified": core["D"], "lost_states": core["L"],
        "transmissions": len(core["txs"]),
        "tx_captured": sum(1 for t in core["txs"] if t["captured"]),
        "full_blocks": core_full_blocks(core), "full_blocks_all": core["full_all"],
        "cycles": len(core["units"]), "threshold_db": core["T"],
        "selfcheck": sc,
        "tower": _phase_summary(tcore, tower.get("meta"), tower.get("end"), ctx) if tcore else None,
    })
    out["untestable"] = _untestable(core, ctx, meta, sc, v["level"], ref_label, per_state,
                                    blocks_target)
    if error:
        out["headline_params"] = {"error": error}
        out["error"] = error
    return out


# --- מסנן FM (§5.8) ----------------------------------------------------------------

def _notch_core(rows, meta, end, ctx, telemetry_lines=None):
    tel = telemetry_status(meta, end, telemetry_lines)
    tel_ok = tel == "ok"
    P = _prep(rows, tel_ok)
    ref = ctx.get("ref") or (meta or {}).get("ref") or "tower"
    T = threshold_db(_cfg(ctx))
    base = _cfg(ctx).get("fm_notch")
    if not isinstance(base, bool):
        base = next((r["notch"] for r in P if r["notch"] is not None), False)
    cls, floor, groups = _classify(P, T, ref)
    units = _units_notch(P)
    keys = [False, True]
    fx = _facts(units, cls, keys, tel_ok)
    cmap = _cnr(P, cls, groups)
    pd, kinds = _pairs(units, cmap, keys)
    us, up = _u_maps(units, cls, groups, keys)
    u = _u_for((False, True), kinds, us, up)
    d = pd[(False, True)]                            # ‎CNR_on − CNR_off
    d = list(d) if base is False else [-x for x in d]   # ⇒ ‎CNR_B − CNR_A, ‏A = base
    A, B = base, (not base)
    bad = {k: (fx[k]["op_clip"] >= FACT_MIN_CYCLES or (fx[k]["op_ovl"] or 0) >= FACT_MIN_CYCLES
               or fx[k]["lost"] >= FACT_MIN_CYCLES) for k in keys}
    nz = [x for x in d if x != 0]
    n = len(nz)
    kp = sum(1 for x in nz if x > 0)
    kn = n - kp
    pB, pA = sign_test_p(kp, n), sign_test_p(kn, n)
    rej = holm([pB, pA])
    nm = n_min(1)
    med = _rd(statistics.median(d)) if d else None
    testable = n >= nm and u is not None
    winner_stat = None
    if testable:
        if rej[0] and med > u and kp > kn:
            winner_stat = B
        elif rej[1] and -med > u and kn > kp:
            winner_stat = A
    lna = None
    for s in (ctx.get("states"), (meta or {}).get("states")):
        if isinstance(s, (list, tuple)) and s:
            lna = _int(s[0])
            break
    if lna is None:
        lna = next((r["lna"] for r in P if r["lna"] is not None), None)
    return {"tel": tel, "tel_ok": tel_ok, "P": P, "T": T, "ref": ref, "base": base, "A": A,
            "B": B, "cls": cls, "groups": groups, "units": units, "facts": fx, "cnr": cmap,
            "d": d, "u": u, "n": n, "k_pos": kp, "k_neg": kn, "p_B": pB, "p_A": pA,
            "holm": rej, "n_min": nm, "median_d": med, "testable": testable,
            "winner_stat": winner_stat, "bad": bad, "lna": lna, "phase": "notch",
            "n_carrier": sum(1 for x in cls.values() if x == CARRIER),
            "pairs_n": len(d)}


def _notch_verdict(nc, error):
    A, B, bad = nc["A"], nc["B"], nc["bad"]
    winner = None
    if error:
        return "none", "error", None, None
    if bad[A] != bad[B]:
        winner = B if bad[A] else A
        level = "fact"
    elif nc["testable"]:
        level = "stat"
        winner = nc["winner_stat"]
    elif nc["pairs_n"]:
        u = nc["u"] or 0.0
        md = nc["median_d"]
        ind = B if md > u else (A if -md > u else None)
        return "indication", "indication", None, ind
    else:
        return "none", "no_traffic", None, None
    if winner is None:
        return level, "notch_no_difference", A, None
    return level, ("notch_on" if winner else "notch_off"), winner, None


def analyze_notch(rows, meta, end, ctx):
    """השוואת מסנן FM כבוי/דלוק ב-LNA קבוע (§5.8): עובדה בתצורה אחת בלבד מכריעה; אחרת
    שני מבחני סימן חד-צדדיים עם Holm (m=2), ‏n≥6 ושער U; אחרת "אין הבדל" — רק כשנבדק."""
    ctx = ctx if isinstance(ctx, dict) else {}
    meta = meta if isinstance(meta, dict) else {}
    nc = _notch_core(rows, meta, end, ctx)
    error = _error_of(ctx, end)
    level, head, choice, ind_choice = _notch_verdict(nc, error)
    cfg = _cfg(ctx)
    P, cls = nc["P"], nc["cls"]

    def cfg_stats(k):
        rs = [r for r in P if r["notch"] == k]
        cl = [cls.get(r["i"]) for r in rs]
        cv = [nc["cnr"][r["i"]][0] for r in rs if r["i"] in nc["cnr"]]
        gs = [r["ifgr"] for r in rs if r["ifgr"] is not None]
        f = nc["facts"][k]
        return {"fm_notch": k, "valid": sum(1 for r in rs if r["valid"]),
                "invalid": sum(1 for r in rs if not r["valid"]),
                "n_carrier": cl.count(CARRIER), "n_silence": cl.count(SILENCE),
                "n_edge": cl.count(EDGE), "cnr_median": _rd(_median(cv), 2),
                "ifgr_final": max(gs) if gs else None,
                "op_clip": f["op_clip"], "op_ovl": f["op_ovl"], "lost": f["lost"],
                "probe_clip": f["probe_clip"], "probe_ovl": f["probe_ovl"],
                "env_ovl_silence": f["env_ovl_silence"],
                "overload": _fact_status(f["op_ovl"], f["probe_ovl"]),
                "clip": _fact_status(f["op_clip"], f["probe_clip"])}

    per_config = [cfg_stats(False), cfg_stats(True)]
    reasons = []
    for k in (False, True):
        f = nc["facts"][k]
        if f["op_clip"] >= FACT_MIN_CYCLES:
            reasons.append({"code": "op_clip", "fm_notch": k, "cycles": f["op_clip"]})
        if (f["op_ovl"] or 0) >= FACT_MIN_CYCLES:
            reasons.append({"code": "op_ovl", "fm_notch": k, "cycles": f["op_ovl"]})
        if f["lost"] >= FACT_MIN_CYCLES:
            reasons.append({"code": "lost", "fm_notch": k, "heard_by": f["heard_by"],
                            "cycles": f["lost"]})
    if nc["winner_stat"] is not None and level == "stat":
        reasons.append({"code": "notch_compare", "better": nc["winner_stat"],
                        "median_d": _rd(abs(nc["median_d"]), 2), "n": nc["n"],
                        "p": _rd(min(nc["p_A"], nc["p_B"]))})
    if "atis" in nc["ref"]:
        reasons.append({"code": "atis_based"})
    if _uses_proxy(nc):
        reasons.append({"code": "proxy_noise"})
    rec = None
    if choice is not None and level in ("fact", "stat"):
        agc = cfg.get("agc", True) is not False
        ifg = None if agc else per_config[1 if choice else 0]["ifgr_final"]
        changes = (choice != nc["base"]
                   or (ifg is not None and ifg != _int(cfg.get("if_gain")))
                   or (nc["lna"] is not None and nc["lna"] != _cur_state(ctx)))
        rec = {"rf_gain": nc["lna"], "if_gain": ifg, "fm_notch": choice, "basis": level,
               "reasons": list(reasons), "changes": bool(changes)}
    ind = None
    if level == "indication":
        ind = {"rf_gain": nc["lna"], "fm_notch": ind_choice, "n": nc["n"],
               "reasons": list(reasons)}
    sc = _selfcheck(nc, meta, end, ctx, None)
    out = _base_result(ctx, meta, end, "notch", nc["ref"])
    hp = {"fm_notch": nc["base"]} if head == "notch_no_difference" else (
        {"n": nc["n"], "fm_notch": ind_choice} if head == "indication" else {})
    out.update({
        "states": [nc["lna"]] if nc["lna"] is not None else [],
        "current_state": _cur_state(ctx), "fm_notch_base": nc["base"],
        "level": level, "headline": head, "headline_params": hp,
        "recommendation": rec, "indication": ind, "refine_suggestion": None,
        "apply_allowed": bool(rec is not None and rec["changes"]),
        "evidence": reasons, "per_state": [], "per_config": per_config,
        "comparisons": [{"a": nc["A"], "b": nc["B"], "n": nc["n"], "n_pairs": nc["pairs_n"],
                         "median_d": _rd(nc["median_d"], 2), "u": _rd(nc["u"], 2),
                         "p": _rd(min(nc["p_A"], nc["p_B"])),
                         "p_holm_reject": bool(any(nc["holm"])), "testable": nc["testable"],
                         "significant": nc["winner_stat"] is not None,
                         "better": nc["winner_stat"],
                         "worse": (None if nc["winner_stat"] is None
                                   else (not nc["winner_stat"]))}],
        "method": "sign_test_holm_m2", "alpha": ALPHA, "n_min": nc["n_min"], "n": nc["n"],
        "best": None, "tie_set": [], "transmissions": 0, "tx_captured": 0,
        "full_blocks": 0, "pairs": nc["pairs_n"], "cycles": len({u["cyc"] for u in nc["units"]}),
        "threshold_db": nc["T"], "selfcheck": sc, "tower": None,
    })
    unt = []
    if "tower" in nc["ref"]:
        unt.append({"code": "soft_compression_tower"})
    if not sc["guard_verified"]:
        unt.append({"code": "settle_unverified"})
    if nc["tel"] == "ok" and not sc["overload_semantics_verified"]:
        unt.append({"code": "overload_semantics_unverified"})
    if nc["tel"] == "silent":
        unt.append({"code": "telemetry_silent"})
    elif nc["tel"] != "ok":
        unt.append({"code": "telemetry_absent", "status": nc["tel"]})
    if sc["rail_source"] != "verified":
        unt.append({"code": "rail_unverified"})
    if _uses_proxy(nc):
        unt.append({"code": "proxy_noise"})
    if level != "stat" and nc["pairs_n"] < NOTCH_TARGET_PAIRS:
        unt.append({"code": "few_blocks"})
    if sc["invalid_frac"] is not None and sc["invalid_frac"] > INVALID_MAX_FRAC:
        unt.append({"code": "cpu_invalid"})
    out["untestable"] = unt
    if error:
        out["headline_params"] = {"error": error}
        out["error"] = error
    return out


# --- סטטוס חי וכללי עצירה (§5.11 + החלטת משתמש 1) ---------------------------------

def _live_status(op_probe_counts):
    n = op_probe_counts
    if n is None:
        return None
    return "observed" if n >= FACT_MIN_CYCLES else ("observed_once" if n == 1 else "not_observed")


def live_summary(rows, ctx):
    """סיכום לסטטוס (כל poll של המתזמר). ctx: phase, ref, states, config_at_start, meta
    (meta.json של הריצה הנוכחית), telemetry_lines (מ-status.json), tower (מיזוג), gr_table.
    בצ'יפים: overload/clip בכל רמת IF ("נצפה בבדיקה", לא עובדה); None כשהטלמטריה לא ok."""
    ctx = ctx if isinstance(ctx, dict) else {}
    meta = ctx.get("meta") if isinstance(ctx.get("meta"), dict) else None
    phase = ctx.get("phase") or (meta or {}).get("phase") or "lna"
    ref = ctx.get("ref") or (meta or {}).get("ref") or "tower"
    tl = ctx.get("telemetry_lines")
    if phase == "notch":
        nc = _notch_core(rows, meta, None, ctx, telemetry_lines=tl)
        lvl, _h, _c, _i = _notch_verdict(nc, None)
        P, cls = nc["P"], nc["cls"]
        last = P[-1] if P else None
        configs = []
        for k in (False, True):
            rs = [r for r in P if r["notch"] == k]
            cl = [cls.get(r["i"]) for r in rs]
            ov = (len({r["i"] // 4 for r in rs if r["ovl"] is True}) if nc["tel_ok"] else None)
            configs.append({"fm_notch": k, "n_carrier": cl.count(CARRIER),
                            "n_silence": cl.count(SILENCE), "overload": _live_status(ov),
                            "clip": _live_status(len({r["i"] // 4 for r in rs if r["clip"] > 0})),
                            "current": bool(last and last["notch"] == k)})
        return {"phase": "notch", "ref": ref, "rows": len(P),
                "valid": sum(1 for r in P if r["valid"]),
                "invalid": sum(1 for r in P if not r["valid"]),
                "cycles": len({u["cyc"] for u in nc["units"]}),
                "last_slot": cls.get(last["i"]) if last else None,
                "current_state": last["lna"] if last else None,
                "current_notch": last["notch"] if last else None,
                "configs": configs, "states": [], "n_carrier_rows": nc["n_carrier"],
                "pairs": nc["pairs_n"], "pairs_target": NOTCH_TARGET_PAIRS,
                "level": lvl, "stat_ready": bool(nc["testable"]), "terminal_fact": False,
                "telemetry": nc["tel"], "tx_captured": 0, "tx_target": 0, "full_blocks": 0,
                "blocks_target": 0, "transmissions": 0,
                "no_traffic_sec": _no_traffic_sec(P, cls),
                "progress": round(min(1.0, nc["pairs_n"] / NOTCH_TARGET_PAIRS), 3)}

    tower = ctx.get("tower") if isinstance(ctx.get("tower"), dict) else None
    tf = None
    if tower and ref == "atis":
        tf = _lna_core(tower.get("rows") or [], tower.get("meta"), tower.get("end"),
                       ctx, "tower")["facts_own"]
    core = _lna_core(rows, meta, None, ctx, ref, extra_facts=tf, telemetry_lines=tl)
    v = _verdict(core, ctx, None)
    P, cls = core["P"], core["cls"]
    last = P[-1] if P else None
    gr = _gr_table(meta, ctx)
    cur = _cur_state(ctx)
    states_out = []
    for s in core["states"]:
        rs = [r for r in P if r["lna"] == s]
        cl = [cls.get(r["i"]) for r in rs]
        gs = [r["ifgr"] for r in rs if r["ifgr"] is not None]
        ov = len({r["cyc"] for r in rs if r["ovl"] is True}) if core["tel_ok"] else None
        states_out.append({
            "lna": s, "label": label(s), "gr_db": gr[s] if gr is not None else None,
            "ifgr": gs[-1] if gs else None, "n_carrier": cl.count(CARRIER),
            "n_silence": cl.count(SILENCE), "n_edge": cl.count(EDGE),
            "overload": _live_status(ov),
            "clip": _live_status(len({r["cyc"] for r in rs if r["clip"] > 0})),
            "current": bool(last and last["lna"] == s), "production": cur == s,
            "candidate": s in core["C"]})
    tx_cap = sum(1 for t in core["txs"] if t["captured"])
    fb = core_full_blocks(core)
    tx_t, b_t = (TARGET_TX, TARGET_BLOCKS) if ref == "tower" else (0, ATIS_TARGET_BLOCKS)
    prog = min(1.0, fb / b_t) if b_t else 0.0
    if tx_t:
        prog = min(prog, tx_cap / tx_t)
    if prog >= 1.0 and v["level"] != "stat":
        prog = 0.99                                  # היעד המספרי הושג, עוד לא מובהק
    return {"phase": "lna", "ref": ref, "rows": len(P),
            "valid": sum(1 for r in P if r["valid"]),
            "invalid": sum(1 for r in P if not r["valid"]), "cycles": len(core["units"]),
            "last_slot": cls.get(last["i"]) if last else None,
            "current_state": last["lna"] if last else None,
            "states": states_out, "n_carrier_rows": core["n_carrier"],
            "transmissions": len(core["txs"]), "tx_captured": tx_cap, "tx_target": tx_t,
            "full_blocks": fb, "blocks_target": b_t, "candidates": core["C"],
            "level": v["level"], "stat_ready": v["level"] == "stat",
            "terminal_fact": bool(not core["C"] and core["D"]),
            "telemetry": core["tel"], "no_traffic_sec": _no_traffic_sec(P, cls),
            "progress": round(prog, 3)}


def _no_traffic_sec(P, cls):
    """שניות (שעון הבודק) מאז סלוט הנשא האחרון, או מאז תחילת הריצה כשלא היה נשא."""
    ts = [r for r in P if r["t1"] is not None or r["t0"] is not None]
    if not ts:
        return None

    def tend(r):
        return r["t1"] if r["t1"] is not None else r["t0"]

    last_t = max(tend(r) for r in ts)
    car = [tend(r) for r in ts if cls.get(r["i"]) == CARRIER]
    if car:
        return round(max(0.0, last_t - max(car)), 1)
    first = min((r["t0"] if r["t0"] is not None else r["t1"]) for r in ts)
    return round(max(0.0, last_t - first), 1)


def stop_reason(summary, ctx, elapsed):
    """החלטת משתמש 1: עוצרים ברגע שהיעד הסטטיסטי הושג, או ב-180 שניות — בלי "הארך".
      מגדל: עובדה סופית (C ריק ו-D לא ריק — D רק גדל) ⇒ "target";
            ‏≥3 שידורים שנמדדו ∧ ≥12 בלוקים מלאים ∧ התוצאה כבר ברמת stat ⇒ "target";
            ‏elapsed ≥ hard_max ⇒ "time".
      ATIS (יעד ומקסימום משלו): ‏≥20 בלוקים או עובדה סופית ⇒ "atis_target"; ‏≥30ש' ⇒ "time".
      מסנן FM: ‏≥12 זוגות ∧ נבדק ⇒ "notch_target"; ‏elapsed ≥ hard_max ⇒ "time".
    elapsed = שניות מתחילת השלב הנוכחי (ב-ATIS: מאז המעבר)."""
    ctx = ctx if isinstance(ctx, dict) else {}
    s = summary if isinstance(summary, dict) else {}
    phase = ctx.get("phase") or s.get("phase") or "lna"
    ref = ctx.get("ref") or s.get("ref") or "tower"
    hard = _num(ctx.get("hard_max"))
    hard = HARD_MAX_SEC if hard is None else min(hard, HARD_MAX_SEC)
    el = _num(elapsed) or 0.0
    if phase == "notch":
        if s.get("stat_ready") and (s.get("pairs") or 0) >= NOTCH_TARGET_PAIRS:
            return "notch_target"
        return "time" if el >= hard else None
    if ref == "atis":
        if s.get("terminal_fact") or (s.get("full_blocks") or 0) >= ATIS_TARGET_BLOCKS:
            return "atis_target"
        return "time" if el >= min(ATIS_MAX_SEC, hard) else None
    if s.get("terminal_fact"):
        return "target"
    if ((s.get("tx_captured") or 0) >= TARGET_TX and (s.get("full_blocks") or 0) >= TARGET_BLOCKS
            and s.get("stat_ready")):
        return "target"
    return "time" if el >= hard else None


def offer_atis(summary, ctx, elapsed):
    """ההצעה לעבור ל-ATIS: שלב lna מול המגדל, ‏≥30 שניות, ואף סלוט נשא. לעולם לא אוטומטי.
    ‏"לא נמדד" ≠ "שקט": בלי אף סלוט תקין (‏valid=0 בסיכום החי) — לא מציעים."""
    ctx = ctx if isinstance(ctx, dict) else {}
    s = summary if isinstance(summary, dict) else {}
    return bool((ctx.get("phase") or s.get("phase") or "lna") == "lna"
                and (ctx.get("ref") or s.get("ref") or "tower") == "tower"
                and (_num(elapsed) or 0.0) >= ATIS_OFFER_SEC
                and (s.get("n_carrier_rows") or 0) == 0
                and s.get("valid", 1) != 0)
