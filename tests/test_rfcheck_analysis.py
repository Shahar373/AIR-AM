# ============================================================================
#  AIR-AM - בדיקות לשכבת הניתוח של 🩺 בדיקת RF (webtune/rfcheck_analysis.py, PR 2)
# ----------------------------------------------------------------------------
#  Python טהור, מהיר, בלי numpy ובלי חומרה. השורות נוצרות כאן במחולל זעיר משלנו
#  (gen/gen_notch) — *לא* tests/rfsim.py: זה מחולל IQ (numpy) שבודק את ה-DSP של
#  הבודק; כאן בודקים רק את השיפוט, על שורות בסכמת rows.jsonl (spec §4.6).
#
#  שתי בדיקות-מפתח (spec §10.4 + §11.6):
#    1. אי-תלות בשגיאת טבלת ה-GR: הזזה של ±3dB פר-מצב (דרך ifgr_start) ⇒ פסק דין זהה.
#    2. כיול תחת השערת אפס גלובלית: 500 זרעים, CNR שווה, K=5, n=12 ⇒ שיעור טענות
#       compression/sensitivity ≤ 5%. ⚠ הגרסה המקורית (best-מול-השאר) נכשלת בתחום
#       שהמשתמש יפגוש (K=7, n גדל עד שיש "מספיק") — ר' test_best_vs_rest_*; לכן
#       המודול משתמש ב-fallback שהמפרט קבע: Holm על כל הזוגות.
# ============================================================================
import ast
import copy
import json
import math
import random
import subprocess
import sys
from pathlib import Path

import pytest

import rfcheck_analysis as A

ROOT = Path(__file__).resolve().parent.parent
MOD = ROOT / "webtune" / "rfcheck_analysis.py"
GR = A.GR_RSP1B_60_420
RID = "0123456789abcdef"
BASE = -70.0
STATES6 = (0, 2, 4, 6, 7, 8)


# --- מחולל שורות מינימלי -----------------------------------------------------------

def gen(states=STATES6, cur=4, n_cycles=32, tx=((8, 21, 25.0),), atis=False,
        offset=None, noise_off=None, shift=None, sigma_car=0.3, sigma_sil=0.1, seed=1,
        clip_below=None, clip_when="any", clip_at=(), ovl_below=None, ovl_at=(),
        handler=True, marker=True, lines=5, nnb=True, invalid=(), burst=None,
        outlier=None, cnr_fn=None, ifgr_ref=40, ifgr_fixed=None, agc=True, if_gain=40,
        squelch=("auto", 9.54), leak=0.0, gr_table=GR, slot_s=0.25, i0=0):
    """שורות בסכמת הבודק. ABBA לפי סבב; ratchet של +10dB/סבב כמו הבודק (spec §4.5.9).
    offset[s] = שינוי CNR אמיתי במצב s (האפקט שבודקים); shift[s] = שגיאת רווח פר-מצב
    (מזיז את *כל* רמות ה-dBFS של המצב ואת ה-IFGR בהתאם — כמו טבלת GR שגויה)."""
    rng = random.Random(seed)
    states = sorted(states)
    offset, noise_off, shift = offset or {}, noise_off or {}, shift or {}
    clip_below, ovl_below = clip_below or {}, ovl_below or {}
    ref = "atis" if atis else "tower"
    tel = handler and marker

    def g(s):
        return rng.gauss(0.0, s) if s else 0.0

    if ifgr_fixed is not None:
        ifgr0 = {s: ifgr_fixed - shift.get(s, 0) for s in states}
    else:
        ifgr0 = {s: max(20, min(59, ifgr_ref + GR[cur] - GR[s])) - shift.get(s, 0)
                 for s in states}
    ifgr = dict(ifgr0)
    epoch = {s: 0 for s in states}
    sil_n = {s: 0 for s in states}
    rows, i, prev_level = [], i0, None
    for c in range(n_cycles):
        order = states if c % 2 == 0 else list(reversed(states))
        carrier_c, cn = False, None
        if atis:
            carrier_c, cn = True, 25.0
        else:
            for (a, b, v) in tx:
                if a <= c <= b:
                    carrier_c, cn = True, v
        if carrier_c and cnr_fn:
            cn = cnr_fn(c)
        hit = {s: False for s in states}
        for s in order:
            N = BASE + noise_off.get(s, 0.0) + shift.get(s, 0)
            valid = (c, s) not in invalid
            if carrier_c:
                car = N + cn + offset.get(s, 0.0) + g(sigma_car)
                if outlier and outlier[0] == c and outlier[1] == s:
                    car += outlier[2]
                c_tot = 10 * math.log10(10 ** (car / 10) * 1.3 + 10 ** (N / 10)) + g(0.03)
                h1, h2, c_car = c_tot + g(0.03), c_tot + g(0.03), car
            else:
                extra = 0.0
                if burst and burst[0] == s:
                    extra = burst[1] if sil_n[s] % 2 else 0.0
                    sil_n[s] += 1
                c_tot = N + extra + g(sigma_sil)
                h1, h2 = c_tot + g(sigma_sil / 2), c_tot + g(sigma_sil / 2)
                c_car = N + extra - 1.05 + g(sigma_sil)
            n_nb = (N - 2.0 + g(sigma_sil)) if nnb else None
            clipping = (((ifgr[s] < clip_below.get(s, -1))
                         and (clip_when == "any" or (clip_when == "carrier") == carrier_c))
                        or (c, s) in clip_at)
            ov = ((ifgr[s] < ovl_below.get(s, -1)) or (c, s) in ovl_at) if tel else None
            b0 = c_tot + (leak * (prev_level - c_tot) if (leak and prev_level is not None) else 0)
            rows.append({
                "run_id": RID, "i": i, "cyc": c, "dir": "f" if c % 2 == 0 else "r",
                "lna": s, "ifgr": ifgr[s], "epoch": epoch[s], "notch": False,
                "t0": round(i * slot_s, 3), "t1": round(i * slot_s + 0.18, 3),
                "wall": 1.7e9 + i * slot_s, "valid": valid,
                "inv": None if valid else "overflow", "sw_ms": 3.0, "drained": 0,
                "n": 458752, "c_tot": round(c_tot, 2), "c_tot_h1": round(h1, 2),
                "c_tot_h2": round(h2, 2), "c_car": round(c_car, 2),
                "b_tot": [round(b0 + g(0.02), 2)] + [round(c_tot + g(0.02), 2) for _ in range(6)],
                "n_nb": None if n_nb is None else round(n_nb, 2),
                "p_wb": round(N + 10.0, 2), "clip": 5 if clipping else 0,
                "peak": 32767 if clipping else 12000, "ovl": ov, "ovl_edge": False,
                "e_mean": 0.0, "e_std": 0.0, "e_p99": 0.0, "proc_ms": 40.0,
            })
            if valid and (clipping or ov):
                hit[s] = True
            prev_level = c_tot
            i += 1
        for s in states:
            if hit[s] and ifgr[s] < 59:
                ifgr[s] = min(59, ifgr[s] + 10)
                epoch[s] += 1
    meta = {"run_id": RID, "phase": "lna", "ref": ref,
            "freq_hz": 132500000 if atis else 118100000, "states": list(states),
            "ifgr_start": {str(s): ifgr0[s] for s in states}, "handler": handler,
            "telemetry_marker": marker, "gr_table": list(gr_table) if gr_table else None,
            "notch_readback": "false", "bias_t_forced_off": False,
            "hw": {"driver": "sdrplay", "hardware": "RSP1B"}}
    end = {"run_id": RID, "ended": "stopped", "telemetry_lines": lines,
           "overflows": len(invalid), "gr_timeouts": 0, "light_mode": False,
           "rate_measured": 2560000.0}
    ctx = {"phase": "lna", "ref": ref, "run_id": RID, "freq": 132.5 if atis else 118.1,
           "states": list(states),
           "config_at_start": {"freq": 118.1, "mod": "am", "agc": agc, "if_gain": if_gain,
                               "rf_gain": cur, "fm_notch": False,
                               "squelch_mode": squelch[0], "squelch_snr": squelch[1]},
           "ifgr_ref": {"value": ifgr_ref, "source": "assumed"}, "guard_buffers": 1,
           "rail_code": 32767, "verified": None}
    return rows, meta, end, ctx


def run(**kw):
    rows, meta, end, ctx = gen(**kw)
    return A.analyze(rows, meta, end, ctx)


def flags(res, s):
    return next(p for p in res["per_state"] if p["lna"] == s)["flags"]


def pstate(res, s):
    return next(p for p in res["per_state"] if p["lna"] == s)


def codes(lst):
    return [x["code"] for x in lst]


# --- סטטיסטיקה: ערכים שחושבו ביד ---------------------------------------------------

def test_sign_test_p_hand_values():
    assert A.sign_test_p(12, 12) == 1 / 4096
    assert A.sign_test_p(11, 12) == 13 / 4096
    assert A.sign_test_p(10, 12) == 79 / 4096          # 1+12+66
    assert A.sign_test_p(3, 5) == 0.5                  # (10+5+1)/32
    assert A.sign_test_p(0, 7) == 1.0
    assert A.sign_test_p(1, 0) == 1.0                  # אין נתונים ⇒ לא מובהק
    assert A.sign_test_p(13, 12) == 0.0
    assert A.sign_test_p(7, 7) == 1 / 128


def test_holm_hand_values():
    assert A.holm([0.01, 0.04, 0.03]) == [True, False, False]   # 0.03 > 0.05/2
    assert A.holm([0.01, 0.02, 0.04]) == [True, True, True]
    assert A.holm([0.03, 0.03]) == [False, False]               # 0.03 > 0.025
    assert A.holm([0.02, 0.01]) == [True, True]
    assert A.holm([None, 0.001]) == [False, True]               # None נספר ב-m, לא נדחה
    assert A.holm([]) == []


def test_n_min_is_the_first_possible_rejection():
    # ‎2^(1−n) ≤ α/m: מתחת לזה מובהקות בלתי-אפשרית מתמטית
    assert A.n_min(1) == 6
    assert A.n_min(10) == 9          # K=5
    assert A.n_min(15) == 10         # K=6
    assert A.n_min(21) == 10         # K=7
    assert A.n_min(0) is None
    for m in (1, 3, 10, 15, 21):
        n = A.n_min(m)
        assert 2.0 ** (1 - n) <= A.ALPHA / m < 2.0 ** (2 - n)


def test_threshold_db():
    assert A.threshold_db({"squelch_mode": "manual", "squelch_snr": 12.0}) == 12.0
    assert A.threshold_db({"squelch_mode": "auto", "squelch_snr": 12.0}) == 9.54
    assert A.threshold_db({"squelch_mode": "open"}) == 9.54      # סף 0 = הכול "נשא"
    assert A.threshold_db({"squelch_mode": "manual", "squelch_snr": 0}) == 9.54
    assert A.threshold_db(None) == 9.54


# --- סיווג ----------------------------------------------------------------------

def _row(i, cyc, lna, c_tot, n_nb=-72.0, epoch=0, valid=True, car=None, ifgr=40, **kw):
    r = {"i": i, "cyc": cyc, "lna": lna, "epoch": epoch, "notch": False, "valid": valid,
         "t0": i * 0.25, "t1": i * 0.25 + 0.18, "c_tot": c_tot, "c_tot_h1": c_tot,
         "c_tot_h2": c_tot, "c_car": c_tot - 1.0 if car is None else car, "n_nb": n_nb,
         "clip": 0, "ovl": None, "ifgr": ifgr, "b_tot": [c_tot] * 7, "peak": 1000,
         "p_wb": -40.0}
    r.update(kw)
    return r


def test_classify_silence_ref_group():
    rows = [_row(k, k, 4, -70.0 + 0.05 * (k % 3)) for k in range(6)]
    rows.append(_row(6, 6, 4, -45.0))                                 # נשא
    rows.append(_row(7, 7, 4, -70.0, c_tot_h1=-70.0, c_tot_h2=-55.0))  # קצה PTT
    cls, floor, groups = A.classify(rows, 9.54, "tower")
    assert groups[(4, 0, False)]["silence_ref"] is True
    assert [cls[k] for k in range(6)] == ["silence"] * 6
    assert cls[6] == "carrier" and cls[7] == "edge"
    assert floor[6] == groups[(4, 0, False)]["N0"]


def test_classify_carrier_only_epoch_falls_back_to_proxy():
    # תקופה שכולה נשא (ATIS / אחרי ratchet): ה"רצפה" היא בעצם נשא ⇒ proxy (n_nb).
    rows = [_row(k, k, 4, -45.0 + 0.1 * k, n_nb=-72.0) for k in range(5)]
    cls, floor, groups = A.classify(rows, 9.54, "tower")
    g = groups[(4, 0, False)]
    assert g["silence_ref"] is False
    assert all(cls[k] == "carrier" for k in range(5))
    assert floor[0] == -72.0
    # ב-ATIS לעולם לא silence_ref, גם כשכולו שקט
    rows = [_row(k, k, 4, -70.0, n_nb=-72.0) for k in range(5)]
    _cls, _f, groups = A.classify(rows, 9.54, "atis")
    assert groups[(4, 0, False)]["silence_ref"] is False


def test_classify_unclassified_without_n_nb_and_invalid():
    rows = [_row(k, k, 4, -45.0, n_nb=-72.0) for k in range(4)]
    rows.append(_row(4, 4, 4, -45.0, n_nb=None))                    # proxy בלי n_nb
    rows.append(_row(5, 5, 4, -45.0, valid=False))
    cls, _f, _g = A.classify(rows, 9.54, "tower")
    assert cls[4] == "unclassified"
    assert cls[5] == "invalid"


# --- סבבים, שידורים, בלוקים מלאים ------------------------------------------------

def test_transmissions_full_blocks_and_interior():
    rows, i = [], 0
    pattern = {  # cyc: (class at state 0, class at state 4)
        0: ("s", "s"), 1: ("c", "c"), 2: ("c", "c"), 3: ("c", "s"), 4: ("c", "c"),
        5: ("s", "s"), 6: ("c", "c"), 7: ("x", "x"), 8: ("c", "c"), 9: ("s", "s")}
    for cyc in sorted(pattern):
        for lna, k in zip((0, 4), pattern[cyc]):
            rows.append(_row(i, cyc, lna, -45.0 if k == "c" else -70.0, valid=(k != "x")))
            i += 1
    # מספיק שקט לקבוצת silence_ref בכל מצב
    cls, _f, _g = A.classify(rows, 9.54, "tower")
    cm = A.cycles(rows)
    txs = A.transmissions(cm, cls, [0, 4])
    # סבב 7 כולו invalid: לא מסיים את השידור (לא מתנפחים מ-overflow), סבב 5/9 שקט כן
    assert [(t["start_cyc"], t["end_cyc"]) for t in txs] == [(1, 4), (6, 8)]
    assert [t["full_blocks"] for t in txs] == [3, 2]
    assert all(t["captured"] for t in txs)
    # "פנימי" לפי ההגדרה = שני השכנים סבבי-נשא; גם 5 ו-7 (בין שני שידורים) — אבל שם אין
    # אף מצב נשא, ולכן הם לא יכולים לייצר "אבד".
    assert A.interior_cycles(cm, cls) == [2, 3, 5, 7]
    f = A.facts(cm, cls, [0, 4])
    assert f[4]["lost"] == 1 and f[4]["heard_by"] == [0]     # סבב 3: פנימי, 4 שקט, 0 נשא
    assert f[0]["lost"] == 0


# --- עובדות --------------------------------------------------------------------

def test_op_fact_needs_two_cycles_single_is_observed_once():
    res = run(tx=(), clip_at={(3, 2)}, ifgr_fixed=59)          # IFGR 59, סבב אחד בלבד
    p = pstate(res, 2)
    assert p["op_clip"] == 1 and p["clip"] == "observed_once"
    assert 2 not in res["disqualified"]
    res = run(tx=(), clip_at={(3, 2), (9, 2)}, ifgr_fixed=59)
    assert pstate(res, 2)["clip"] == "observed"
    assert res["disqualified"] == [2]


def test_clip_below_59_is_never_a_fact():
    # מצב 0 נחתך רק ב-IFGR<55: ה-ratchet מעלה ל-58 והחיתוך נעלם ⇒ probe בלבד, לא עובדה
    res = run(tx=(), clip_below={0: 55}, ifgr_fixed=40)
    p = pstate(res, 0)
    assert p["op_clip"] == 0 and p["probe_clip"] >= 1
    assert p["clip"] == "probe_only"                  # לא "not_observed" — כן נצפה בבדיקה
    assert p["ratchets"] >= 1 and p["ifgr_final"] > p["ifgr_start"]
    assert "probe_clip" in p["flags"] and "ratchet" in p["flags"]
    assert res["disqualified"] == []
    assert {"code": "ratchets", "states": [0]} in res["untestable"]


def test_telemetry_absent_or_silent_nulls_every_overload():
    for kw, code in ((dict(marker=False), "telemetry_absent"),
                     (dict(handler=False), "telemetry_absent"),
                     (dict(lines=0), "telemetry_silent")):
        # גם כשהשורות *נושאות* ovl=True ב-IFGR 59 — הטלמטריה לא אומתה ⇒ "לא נבדק"
        rows, meta, end, ctx = gen(tx=(), ovl_at={(2, 0), (5, 0), (8, 0)}, ifgr_fixed=59,
                                   **kw)
        for r in rows:
            if r["lna"] == 0 and r["cyc"] in (2, 5, 8):
                r["ovl"] = True
        res = A.analyze(rows, meta, end, ctx)
        assert res["selfcheck"]["telemetry"] != "ok"
        for p in res["per_state"]:
            assert p["overload"] is None and p["op_ovl"] is None
            assert p["overload"] != "not_observed"
        assert res["disqualified"] == []
        assert code in codes(res["untestable"])


def test_telemetry_ok_reports_overload_and_unverified_semantics():
    res = run(tx=(), ovl_at={(2, 0), (5, 0)}, ifgr_fixed=59)
    assert res["selfcheck"]["telemetry"] == "ok"
    assert pstate(res, 0)["overload"] == "observed"
    assert pstate(res, 0)["env_ovl_silence"] == 2          # בשקט ⇒ עומס סביבתי
    assert pstate(res, 4)["overload"] == "not_observed"
    assert res["disqualified"] == [0]
    assert "overload_semantics_unverified" in codes(res["untestable"])
    assert "env_ovl_silence" in codes(res["evidence"])
    # כשהסמנטיקה אומתה (RFCHECK_VERIFIED) — אין רשומת אי-ודאות
    rows, meta, end, ctx = gen(tx=(), ovl_at={(2, 0), (5, 0)}, ifgr_fixed=59)
    ctx["verified"] = {"overload_reported_with_agc_off": True, "guard_buffers": 1,
                       "rail_code": 32767}
    res = A.analyze(rows, meta, end, ctx)
    u = codes(res["untestable"])
    assert "overload_semantics_unverified" not in u
    assert "settle_unverified" not in u and "rail_unverified" not in u


def test_unknown_telemetry_without_end_is_not_ok():
    rows, meta, _end, ctx = gen(tx=())
    res = A.analyze(rows, meta, None, ctx)
    assert res["selfcheck"]["telemetry"] == "unknown"
    assert all(p["overload"] is None for p in res["per_state"])


# --- טבלת התרחישים (spec §10.4) --------------------------------------------------

def test_scenario_linear_chain_tie_goes_to_most_attenuated():
    res = run(cur=4)
    assert res["level"] == "stat"
    assert res["n"] >= 12
    assert res["tie_set"] == list(STATES6)
    assert res["recommendation"]["rf_gain"] == 8
    assert res["headline"] == "reduce_gain_tie"
    assert res["headline_params"] == {"x": 4, "y": 8}
    assert res["apply_allowed"] is True
    assert "tie" in codes(res["evidence"])
    for s in STATES6:
        assert "compression_evidence" not in flags(res, s)
        assert "sensitivity_cost" not in flags(res, s)


def test_scenario_internal_noise_rise_less_attenuated_wins():
    res = run(cur=8, offset={7: -3.0, 8: -8.0})
    assert res["level"] == "stat"
    assert "sensitivity_cost" in flags(res, 8) and "sensitivity_cost" in flags(res, 7)
    assert res["recommendation"]["rf_gain"] == 6
    assert res["headline"] == "increase_gain_sensitivity"
    assert "sensitivity" in codes(res["evidence"])


def test_scenario_compression_at_low_states_recommends_4_or_6():
    res = run(cur=0, offset={0: -6.0, 2: -3.0, 7: -3.0, 8: -8.0})
    assert res["level"] == "stat"
    assert "compression_evidence" in flags(res, 0)
    assert "compression_evidence" in flags(res, 2)
    assert res["recommendation"]["rf_gain"] in (4, 6)
    assert res["headline"] == "reduce_gain_compression"
    comp = [r for r in res["evidence"] if r["code"] == "compression"]
    assert {r["a"] for r in comp} == {0, 2} and all(r["median_d"] > 0 for r in comp)


def test_scenario_op_clip_at_0_fact_level():
    # שקט בלבד, חיתוך סביבתי במצב 0: ה-ratchet מעלה 40→50→59, ושם החיתוך נמשך ⇒ עובדה
    res = run(cur=0, tx=(), clip_below={0: 99})
    p0 = pstate(res, 0)
    assert (p0["ifgr_start"], p0["ifgr_final"], p0["ratchets"]) == (40, 59, 2)
    assert p0["op_clip"] >= 2 and p0["op_clip_in"] == ["silence"]
    assert res["level"] == "fact"
    assert res["headline"] == "reduce_gain_overload"
    assert res["recommendation"]["rf_gain"] == 2
    assert res["headline_params"]["list"] == [0]
    assert "op_clip" in codes(res["evidence"])
    assert res["apply_allowed"] is True


def test_scenario_op_clip_monotone_guard():
    # עובדה במצב 2 פוסלת גם את 0 (יותר רווח), גם אם ב-0 לא נצפה חיתוך
    res = run(cur=0, tx=(), clip_below={2: 99})
    assert res["disqualified"] == [2]
    assert res["candidates"] == [4, 6, 7, 8]
    assert res["recommendation"]["rf_gain"] == 4
    assert res["headline"] == "reduce_gain_overload"


def test_scenario_op_clip_with_traffic_is_stat_with_overload_headline():
    # cur=4 ⇒ הפיצוי דוחף את מצב 0 ל-IFGR 63 ⇒ נחתך ל-59 ("clamped high") — עובדה מהסבב הראשון
    res = run(cur=4, tx=(), clip_below={0: 99})
    assert pstate(res, 0)["ifgr_start"] == 59 and pstate(res, 0)["clamped"] == "high"
    assert pstate(res, 0)["ratchets"] == 0
    assert {"code": "ifgr_clamped_high", "states": [0]} in res["untestable"]
    res = run(cur=0, clip_below={0: 99})
    assert res["level"] == "stat"
    assert res["headline"] == "reduce_gain_overload"
    assert res["recommendation"]["rf_gain"] > 0
    assert 0 not in res["candidates"]


def test_scenario_lost_at_7_recommends_below_7():
    res = run(cur=7, tx=((8, 21, 12.0),), offset={7: -6.0, 8: -10.0})
    assert pstate(res, 7)["lost"] >= 2 and 7 in res["lost_states"]
    assert res["recommendation"]["rf_gain"] < 7
    assert res["headline"] == "increase_gain_lost"
    assert res["level"] in ("fact", "stat")
    lost = [r for r in res["evidence"] if r["code"] == "lost"]
    assert {r["state"] for r in lost} == {7, 8}
    assert 4 in next(r for r in lost if r["state"] == 7)["heard_by"]


def test_scenario_silence_only():
    res = run(tx=())
    assert res["level"] == "none" and res["headline"] == "no_traffic"
    assert res["recommendation"] is None and res["apply_allowed"] is False
    assert "soft_compression_tower" in codes(res["untestable"])
    assert "few_blocks" in codes(res["untestable"])


def test_scenario_n_below_n_min_is_indication_without_apply():
    res = run(cur=4, tx=((8, 10, 25.0),))
    assert res["full_blocks"] == 3 < res["n_min"]
    assert res["level"] == "indication" and res["headline"] == "indication"
    assert res["recommendation"] is None and res["apply_allowed"] is False
    assert res["indication"]["rf_gain"] in STATES6
    assert res["headline_params"]["n"] == 3


def test_scenario_all_states_disqualified():
    res = run(cur=4, tx=(), clip_below={s: 99 for s in STATES6})
    assert res["disqualified"] == list(STATES6)
    assert res["level"] == "fact" and res["headline"] == "all_overloaded"
    assert res["recommendation"] is None and res["apply_allowed"] is False
    assert res["headline_params"] == {"list": list(STATES6)}


# --- כלל ההחלטה (החלטת משתמש 3) ----------------------------------------------------

def test_raising_gain_needs_statistical_evidence():
    # שוויון כשהנוכחי הוא המוחלש ביותר ⇒ לא מעלים רווח
    res = run(cur=8)
    assert res["level"] == "stat"
    assert res["recommendation"]["rf_gain"] == 8 and res["headline"] == "keep_current"
    assert res["recommendation"]["changes"] is False and res["apply_allowed"] is False
    # הבדל אמיתי אבל מעט מדי נתונים ⇒ indication בלבד, בלי "החל"
    res = run(cur=8, tx=((8, 12, 25.0),), offset={8: -8.0})
    assert res["level"] == "indication" and res["apply_allowed"] is False
    assert res["indication"]["rf_gain"] < 8           # הכיוון נתמך בנתונים, בלי כפתור


def test_tie_never_jumps_over_a_proven_sensitivity_cost():
    # 6 ו-7 מאבדים רגישות באופן מובהק; 8 "לא גרוע" (סותר את הפיזיקה — חוסר-עוצמה/תקלה).
    # בלי השומר המונוטוני max(TieSet)=8 — המלצה לדלג מעל מצבים שהוכחו כגרועים.
    res = run(cur=4, offset={6: -3.0, 7: -3.0})
    assert "sensitivity_cost" in flags(res, 6) and "sensitivity_cost" in flags(res, 7)
    assert "sensitivity_cost" not in flags(res, 8)
    assert res["level"] == "stat"
    assert res["recommendation"]["rf_gain"] == 4 and res["headline"] == "keep_current"
    assert res["tie_set"] == [0, 2, 4]
    # הנוכחי 8 "בסדר" אבל 7 שמתחתיו הוכח כגרוע ⇒ העלאת רווח מוצדקת, ה-x הוא המצב שנמדד
    res = run(cur=8, offset={7: -3.0})
    assert res["headline"] == "increase_gain_sensitivity"
    assert res["recommendation"]["rf_gain"] == 6
    assert res["headline_params"] == {"x": 7, "cur": 8, "y": 6}


def test_indication_does_not_jump_over_an_apparently_worse_state():
    res = run(cur=4, tx=((8, 11, 25.0),), offset={6: -4.0, 7: -4.0})
    assert res["level"] == "indication"
    assert res["indication"]["rf_gain"] <= 4


def test_indication_never_points_to_a_state_without_data():
    # מצב 8 "אבד" פעם אחת בלבד (לא עובדה) ובשאר הסבבים בלי נשא תקין ⇒ אין לו זוגות.
    rows, meta, end, ctx = gen(cur=4, tx=((8, 11, 25.0),),
                               invalid={(c, 8) for c in range(8, 12)})
    res = A.analyze(rows, meta, end, ctx)
    assert res["level"] == "indication"
    assert res["indication"]["rf_gain"] != 8


def test_lost_inside_disqualified_set_does_not_empty_candidates():
    # מצב 0 נחתך ב-59 *וגם* "אבד" — זה לא הופך את 2..8 לפסולים (סטייה (ב))
    res = run(cur=0, clip_below={0: 99}, tx=((8, 21, 14.0),), offset={0: -10.0})
    assert 0 in res["disqualified"] and 0 in res["lost_states"]
    assert res["candidates"] == [2, 4, 6, 7, 8]
    assert res["headline"] == "reduce_gain_overload"


# --- KEY: אי-תלות בשגיאת טבלת ה-GR ------------------------------------------------

def _verdict_view(res):
    return {
        "level": res["level"], "headline": res["headline"],
        "rec": (res["recommendation"] or {}).get("rf_gain"),
        "ind": (res["indication"] or {}).get("rf_gain"),
        "tie": res["tie_set"], "best": res["best"], "C": res["candidates"],
        "D": res["disqualified"], "L": res["lost_states"],
        "flags": {p["lna"]: sorted(f for f in p["flags"] if f in (
            "compression_evidence", "sensitivity_cost", "disqualified", "lost", "best",
            "tie", "recommended")) for p in res["per_state"]},
        "cls": {p["lna"]: (p["n_carrier"], p["n_silence"], p["n_edge"])
                for p in res["per_state"]},
        "cnr": {p["lna"]: p["cnr_median"] for p in res["per_state"]},
        "cmp": [(c["a"], c["b"], c["n"], c["median_d"], c["u"], c["significant"])
                for c in res["comparisons"]],
        "full_blocks": res["full_blocks"], "tx": res["transmissions"],
    }


@pytest.mark.parametrize("scenario", [
    dict(cur=4),                                                     # stat, tie
    dict(cur=0, offset={0: -6.0, 2: -3.0, 7: -3.0, 8: -8.0}),       # compression+sens.
    dict(cur=8, offset={7: -3.0, 8: -8.0}),                         # sensitivity
    dict(cur=7, tx=((8, 21, 12.0),), offset={7: -6.0, 8: -10.0}),   # lost
    dict(cur=4, tx=((8, 10, 25.0),)),                                # indication
    dict(cur=4, sigma_car=1.0, sigma_sil=0.15),                      # רועש
])
def test_KEY_table_error_invariance(scenario):
    for seed in range(6):
        rng = random.Random(1000 + seed)
        err = {s: rng.choice((-3, -2, -1, 1, 2, 3)) for s in STATES6}
        base = run(seed=seed, ifgr_fixed=40, **scenario)
        shifted = run(seed=seed, ifgr_fixed=40, shift=err, **scenario)
        # ה-IFGR באמת זז (אחרת הבדיקה ריקה)
        assert ([pstate(shifted, s)["ifgr_start"] for s in STATES6]
                != [pstate(base, s)["ifgr_start"] for s in STATES6])
        assert _verdict_view(shifted) == _verdict_view(base), (seed, err)


# --- שער U --------------------------------------------------------------------

def test_U_gate_blocks_false_worse_from_noise_burst():
    # פרץ רעש בכל סלוט שקט שני של מצב 6 מנפח את רצפת הייחוס ⇒ ה-CNR שלו "נמוך" באופן
    # עקבי (מבחן הסימן דוחה) — אבל U(·,6) הנמדד מאותו שקט גדול יותר ⇒ אין טענה.
    res = run(cur=4, burst=(6, 3.0))
    c6 = [c for c in res["comparisons"] if 6 in (c["a"], c["b"])]
    assert any(c["p_holm_reject"] for c in c6)
    assert not any(c["significant"] for c in c6)
    assert "sensitivity_cost" not in flags(res, 6)
    assert "compression_evidence" not in flags(res, 6)
    # בקרה: אותו פער אמיתי בלי פרץ (U קטן) ⇒ כן מובהק
    res = run(cur=4, offset={6: -1.5, 7: -1.5, 8: -1.5})
    assert "sensitivity_cost" in flags(res, 6)


# --- החלפת דובר ובלוק חריג -----------------------------------------------------------

def test_speaker_change_and_outlier_block_are_handled():
    res = run(cur=4, tx=((8, 23, 25.0),),
              cnr_fn=lambda c: 25.0 if c < 15 else 14.0,       # דובר/מטוס אחר באמצע
              outlier=(12, 8, 9.0))                             # בלוק חריג אחד
    assert res["level"] == "stat"
    assert res["recommendation"]["rf_gain"] == 8
    for s in STATES6:
        assert "compression_evidence" not in flags(res, s)
        assert "sensitivity_cost" not in flags(res, s)


# --- KEY: כיול תחת השערת אפס גלובלית -------------------------------------------------

def _false_claims(n_seeds, states, cur, n_blocks, seed0):
    bad = 0
    for k in range(n_seeds):
        res = run(states=states, cur=cur, n_cycles=n_blocks + 16,
                  tx=((8, 8 + n_blocks - 1, 25.0),), sigma_car=1.0, sigma_sil=0.15,
                  seed=seed0 + k)
        assert res["full_blocks"] == n_blocks
        if any(f in ("compression_evidence", "sensitivity_cost")
               for p in res["per_state"] for f in p["flags"]):
            bad += 1
    return bad / n_seeds


def test_KEY_global_null_calibration_500_seeds():
    """CNR אמיתי שווה בכל המצבים, K=5, ‏n=12 ⇒ ≤5% ריצות עם טענת compression/sensitivity.
    הרעש פר-סלוט (σ=1dB, דעיכה) גדול בהרבה מ-U (‏σ שקט 0.15dB), כך ששער ה-U כמעט לא
    מגן — זו בדיקה של Holm עצמו, לא של השער."""
    rate = _false_claims(500, (0, 2, 4, 6, 7), 4, 12, seed0=50_000)
    assert rate <= 0.05, rate


def test_global_null_calibration_large_K_and_n():
    """התחום שבו best-מול-השאר נכשל (K=7, n=20) — כל-הזוגות נשאר מכויל."""
    rate = _false_claims(150, (0, 2, 3, 4, 6, 7, 8), 3, 20, seed0=70_000)
    assert rate <= 0.05, rate


def _best_vs_rest(cnr):
    """הגרסה המקורית של המפרט (§5.7.4): best לפי חציון, ואז k−1 מבחנים חד-צדדיים."""
    import statistics as st
    K, n = len(cnr), len(cnr[0])
    best = max(range(K), key=lambda s: (st.median(cnr[s]), s))
    ps = []
    for c in range(K):
        if c != best:
            d = [cnr[c][j] - cnr[best][j] for j in range(n)]
            nz = [x for x in d if x != 0]
            ps.append(A.sign_test_p(sum(1 for x in nz if x < 0), len(nz)))
    return any(A.holm(ps))


def _all_pairs(cnr):
    K, n = len(cnr), len(cnr[0])
    pd = {(a, b): [cnr[b][j] - cnr[a][j] for j in range(n)]
          for a in range(K) for b in range(a + 1, K)}
    res = A.compare_states(pd, {p: 0.0 for p in pd}, list(range(K)))
    return bool(res["worse"])


def test_best_vs_rest_is_not_calibrated_so_all_pairs_holm_is_used():
    """תיעוד-בהרצה לסטייה (א): תחת אפס, K=7 ו-n=20, בחירת best מאותם נתונים מנפחת את
    שיעור טענות-השווא מעל 5% (≈13%); Holm על כל הזוגות — באותם נתונים בדיוק — לא."""
    rng = random.Random(4242)
    N, bvr, ap = 1500, 0, 0
    for _ in range(N):
        cnr = [[rng.gauss(0, 1) for _ in range(20)] for _ in range(7)]
        bvr += _best_vs_rest(cnr)
        ap += _all_pairs(cnr)
    assert bvr / N > 0.08
    assert ap / N <= 0.05


# --- מסנן FM -------------------------------------------------------------------

def gen_notch(lna=4, base=False, n_groups=16, carrier_groups=range(4, 12), delta_on=0.0,
              sigma_car=0.3, sigma_sil=0.1, seed=1, clip_cfg=None, ovl_cfg=None,
              agc=True, if_gain=40, cnr=25.0, cur=None):
    rng = random.Random(seed)
    rows = []
    ifgr = 59 if (clip_cfg or ovl_cfg) else 40
    for g in range(n_groups):
        for p, nv in enumerate((base, not base, not base, base)):
            i = 4 * g + p
            N = BASE
            if g in carrier_groups:
                car = N + cnr + (delta_on if nv else 0.0) + rng.gauss(0, sigma_car)
                c_tot = 10 * math.log10(10 ** (car / 10) * 1.3 + 10 ** (N / 10))
                h1 = h2 = c_tot
                c_car = car
            else:
                c_tot = N + rng.gauss(0, sigma_sil)
                h1, h2, c_car = c_tot, c_tot, N - 1.05
            clip = 5 if (clip_cfg or {}).get(nv) else 0
            ovl = bool((ovl_cfg or {}).get(nv))
            rows.append({"run_id": RID, "i": i, "cyc": g, "dir": "f", "lna": lna,
                         "ifgr": ifgr, "epoch": 0, "notch": nv, "notch_rb": str(nv).lower(),
                         "t0": i * 0.5, "t1": i * 0.5 + 0.18, "valid": True, "inv": None,
                         "c_tot": round(c_tot, 2), "c_tot_h1": round(h1, 2),
                         "c_tot_h2": round(h2, 2), "c_car": round(c_car, 2),
                         "b_tot": [round(c_tot, 2)] * 7,
                         "n_nb": round(N - 2.0 + rng.gauss(0, sigma_sil), 2),
                         "p_wb": N + 10, "clip": clip, "peak": 32767 if clip else 9000,
                         "ovl": ovl, "proc_ms": 40.0})
    meta = {"run_id": RID, "phase": "notch", "ref": "tower", "states": [lna],
            "handler": True, "telemetry_marker": True, "gr_table": list(GR),
            "notch_readback": str(base).lower(), "ifgr_start": {str(lna): ifgr}}
    end = {"run_id": RID, "ended": "stopped", "telemetry_lines": 3, "overflows": 0,
           "gr_timeouts": 0}
    ctx = {"phase": "notch", "ref": "tower", "run_id": RID, "freq": 118.1, "states": [lna],
           "config_at_start": {"freq": 118.1, "mod": "am", "agc": agc, "if_gain": if_gain,
                               "rf_gain": lna if cur is None else cur, "fm_notch": base,
                               "squelch_mode": "auto", "squelch_snr": 9.54}}
    return rows, meta, end, ctx


def notch(**kw):
    return A.analyze(*gen_notch(**kw))


def test_notch_fact_in_one_configuration_decides():
    res = notch(base=False, clip_cfg={False: True}, carrier_groups=())
    assert res["phase"] == "notch" and res["level"] == "fact"
    assert res["headline"] == "notch_on"
    assert res["recommendation"]["fm_notch"] is True
    assert res["recommendation"]["rf_gain"] == 4
    assert res["apply_allowed"] is True
    # עובדה בשתי התצורות ⇒ לא מכריעה (ואין נשא ⇒ none)
    res = notch(base=False, clip_cfg={False: True, True: True}, carrier_groups=())
    assert res["level"] == "none" and res["headline"] == "no_traffic"


def test_notch_stat_winner_and_no_difference():
    res = notch(base=False, delta_on=2.0)
    assert res["level"] == "stat" and res["headline"] == "notch_on"
    assert res["recommendation"]["fm_notch"] is True and res["apply_allowed"] is True
    assert res["n"] >= 6
    res = notch(base=True, delta_on=-2.0)                 # המסנן מזיק, הוא דלוק ⇒ לכבות
    assert res["headline"] == "notch_off" and res["recommendation"]["fm_notch"] is False
    res = notch(base=False, delta_on=0.0)
    assert res["level"] == "stat" and res["headline"] == "notch_no_difference"
    assert res["headline_params"] == {"fm_notch": False}
    assert res["recommendation"]["fm_notch"] is False
    assert res["apply_allowed"] is False                  # "אין הבדל" — אין מה להחיל


def test_notch_few_pairs_is_indication_not_no_difference():
    # לא נבדק ≠ "אין הבדל": 2 קבוצות נשא = 4 זוגות < 6
    res = notch(base=False, delta_on=3.0, carrier_groups=range(4, 6))
    assert res["level"] == "indication" and res["headline"] == "indication"
    assert res["recommendation"] is None and res["apply_allowed"] is False
    assert res["indication"]["fm_notch"] is True


def test_notch_manual_gain_applies_measured_if():
    res = notch(base=False, clip_cfg={False: True}, carrier_groups=(), agc=False, if_gain=40)
    assert res["recommendation"]["if_gain"] == 59
    res = notch(base=False, delta_on=2.0, agc=True)
    assert res["recommendation"]["if_gain"] is None
    assert res["selfcheck"]["notch_readback_ok"] is True


# --- כללי עצירה והצעת ATIS (החלטת משתמש 1) ----------------------------------------

def test_stop_rules_tower():
    ctx = {"phase": "lna", "ref": "tower"}
    ok = {"tx_captured": 3, "full_blocks": 12, "stat_ready": True}
    assert A.stop_reason(ok, ctx, 40) == "target"
    assert A.stop_reason({**ok, "stat_ready": False}, ctx, 100) is None   # עד שיש מספיק
    assert A.stop_reason({**ok, "tx_captured": 2}, ctx, 100) is None
    assert A.stop_reason({**ok, "full_blocks": 11}, ctx, 100) is None
    assert A.stop_reason({**ok, "stat_ready": False}, ctx, 180) == "time"
    assert A.stop_reason({"terminal_fact": True}, ctx, 5) == "target"
    assert A.stop_reason({}, {**ctx, "hard_max": 60}, 60) == "time"
    assert A.stop_reason({}, {**ctx, "hard_max": 999}, 179) is None      # תקרה 180
    assert A.stop_reason({}, {**ctx, "hard_max": 999}, 180) == "time"


def test_stop_rules_atis_and_notch():
    atis = {"phase": "lna", "ref": "atis"}
    assert A.stop_reason({"full_blocks": 20}, atis, 10) == "atis_target"
    assert A.stop_reason({"full_blocks": 19}, atis, 29) is None
    assert A.stop_reason({"full_blocks": 19}, atis, 30) == "time"
    nt = {"phase": "notch", "ref": "tower"}
    assert A.stop_reason({"pairs": 12, "stat_ready": True}, nt, 20) == "notch_target"
    assert A.stop_reason({"pairs": 12, "stat_ready": False}, nt, 20) is None
    assert A.stop_reason({"pairs": 3}, nt, 180) == "time"


def test_offer_atis_timing():
    ctx = {"phase": "lna", "ref": "tower"}
    assert A.offer_atis({"n_carrier_rows": 0}, ctx, 29.9) is False
    assert A.offer_atis({"n_carrier_rows": 0}, ctx, 30) is True
    assert A.offer_atis({"n_carrier_rows": 1}, ctx, 60) is False
    assert A.offer_atis({"n_carrier_rows": 0}, {"phase": "lna", "ref": "atis"}, 60) is False
    assert A.offer_atis({"n_carrier_rows": 0}, {"phase": "notch", "ref": "tower"}, 60) is False
    # לא נמדד ≠ שקט: בלי אף סלוט תקין לא מציעים
    assert A.offer_atis({"n_carrier_rows": 0, "valid": 0}, ctx, 60) is False
    assert A.offer_atis(A.live_summary([], ctx), ctx, 60) is False


def test_live_summary_and_stop_on_real_rows():
    # שלושה שידורים × 5 סבבים = 15 בלוקים ⇒ stat ⇒ "target"
    rows, meta, _end, ctx = gen(cur=4, n_cycles=40,
                                tx=((4, 8, 25.0), (14, 18, 22.0), (26, 30, 24.0)))
    lctx = {**ctx, "meta": meta, "telemetry_lines": 4}
    s = A.live_summary(rows, lctx)
    assert s["tx_captured"] == 3 and s["full_blocks"] == 15
    assert s["stat_ready"] is True and s["level"] == "stat"
    assert A.stop_reason(s, lctx, 50) == "target"
    assert s["progress"] == 1.0
    chips = {c["lna"]: c for c in s["states"]}
    assert chips[0]["label"] == "9/9" and chips[8]["label"] == "1/9"
    assert chips[8]["gr_db"] == 57 and chips[4]["production"] is True
    assert sum(1 for c in s["states"] if c["current"]) == 1
    assert chips[4]["overload"] == "not_observed"
    # רק שני שידורים ⇒ ממשיכים (עד 180)
    s2 = A.live_summary([r for r in rows if r["cyc"] < 22], lctx)
    assert s2["tx_captured"] == 2 and A.stop_reason(s2, lctx, 100) is None
    assert s2["progress"] < 1.0
    # שקט בלבד: הצעת ATIS אחרי 30 שניות, והחיווי יודע כמה זמן אין תנועה
    rows, meta, _end, ctx = gen(tx=(), n_cycles=24)
    s3 = A.live_summary(rows, {**ctx, "meta": meta})
    assert s3["n_carrier_rows"] == 0 and s3["last_slot"] == "silence"
    assert s3["no_traffic_sec"] >= 30
    assert A.offer_atis(s3, ctx, 31) is True
    # בלי telemetry_lines ובלי end ⇒ לא ידוע ⇒ צ'יפ עומס None (לא ירוק)
    assert all(c["overload"] is None for c in s3["states"])


def test_live_summary_terminal_fact_stops_early():
    rows, meta, _end, ctx = gen(cur=4, tx=(), n_cycles=10, clip_below={s: 99 for s in STATES6})
    s = A.live_summary(rows, {**ctx, "meta": meta, "telemetry_lines": 2})
    assert s["terminal_fact"] is True
    assert A.stop_reason(s, ctx, 12) == "target"


def test_live_summary_notch():
    rows, meta, _end, ctx = gen_notch(delta_on=2.0)
    s = A.live_summary(rows, {**ctx, "meta": meta, "telemetry_lines": 3})
    assert s["phase"] == "notch" and s["pairs"] == 16 and s["stat_ready"] is True
    assert A.stop_reason(s, ctx, 30) == "notch_target"
    assert {c["fm_notch"] for c in s["configs"]} == {False, True}


# --- מיזוג מגדל + ATIS ------------------------------------------------------------

def test_merge_tower_and_atis_unions_facts():
    t_rows, t_meta, t_end, _ = gen(cur=4, tx=(), n_cycles=20, clip_at={(3, 0)})
    a_rows, a_meta, a_end, a_ctx = gen(cur=4, atis=True, n_cycles=26, clip_at={(5, 0)})
    a_ctx.update(tower={"rows": t_rows, "meta": t_meta, "end": t_end},
                 freq=118.1, atis_freq=132.5)
    # כל שלב לבד: "נצפה פעם אחת" — לא עובדה
    assert pstate(A.analyze(t_rows, t_meta, t_end, {**a_ctx, "ref": "tower",
                                                    "tower": None}), 0)["op_clip"] == 1
    res = A.analyze(a_rows, a_meta, a_end, a_ctx)
    assert res["ref"] == "tower+atis"
    assert res["atis_freq"] == 132.5 and res["freq"] == 118.1
    assert pstate(res, 0)["op_clip"] == 2 and 0 in res["disqualified"]   # איחוד ⇒ עובדה
    assert res["tower"]["facts"]["0"]["op_clip"] == 1
    assert res["tower"]["n_carrier"] == 0
    assert "atis_based" in codes(res["evidence"])
    assert "proxy_noise" in codes(res["untestable"])
    assert "soft_compression_tower" in codes(res["untestable"])
    assert res["level"] == "stat"                              # ATIS נותן בלוקים
    assert res["selfcheck"]["proxy_check"] is not None          # מאומת מול שקט המגדל


# --- תכונות כלליות ---------------------------------------------------------------

def test_result_schema_and_json():
    rows, meta, end, ctx = gen(cur=4, agc=False, if_gain=40)
    before = copy.deepcopy(rows)
    res = A.analyze(rows, meta, end, ctx)
    assert rows == before                                       # לא משנה את הקלט
    json.dumps(res, allow_nan=False)
    for k in ("v", "run_id", "phase", "ref", "freq", "atis_freq", "started_at", "ended_at",
              "ended", "config_at_start", "states", "ifgr_ref", "level", "headline",
              "recommendation", "indication", "refine_suggestion", "per_state",
              "comparisons", "best", "tie_set", "transmissions", "full_blocks", "cycles",
              "selfcheck", "untestable", "tower", "restore", "apply_allowed",
              "headline_params", "evidence", "n", "n_min"):
        assert k in res, k
    for k in ("lna", "label", "gr_db", "ifgr_start", "ifgr_final", "clamped", "ratchets",
              "valid", "invalid", "n_carrier", "n_silence", "n_edge", "noise_ref",
              "noise_dbfs", "cnr_median", "cnr_p25", "cnr_p75", "op_clip", "op_ovl", "lost",
              "probe_clip", "probe_ovl", "env_ovl_silence", "overload", "clip", "flags",
              "peak_max", "p_wb_median"):
        assert k in res["per_state"][0], k
    for k in ("telemetry", "overload_semantics_verified", "settle", "guard_buffers",
              "guard_verified", "overflows", "gr_timeouts", "invalid_frac", "proc_ms_p50",
              "proc_ms_p95", "light_mode", "rate_measured", "rail_code", "rail_source",
              "peak_code_max", "bias_t_forced_off", "notch_readback_ok", "proxy_check",
              "hw", "verified"):
        assert k in res["selfcheck"], k
    assert res["v"] == 1 and res["level"] in ("fact", "stat", "indication", "none")
    # רווח ידני: "החל" כולל את נקודת ה-IF שנמדדה בפועל במצב המומלץ (החלטת משתמש 5)
    rec = res["recommendation"]
    assert rec["rf_gain"] == 8
    assert rec["if_gain"] == pstate(res, 8)["ifgr_final"] == 20
    assert pstate(res, 8)["clamped"] == "low"
    assert "ifgr_clamped_low" in codes(res["untestable"])
    assert "agc_dynamics" not in codes(res["untestable"])       # AGC כבוי בייצור
    assert res["refine_suggestion"] == [7, 8, 9]                 # 9 לא נבדק
    assert res["per_state"][0]["label"] == "9/9"
    assert res["selfcheck"]["notch_readback_ok"] is True
    res = run(cur=4)
    assert res["recommendation"]["if_gain"] is None              # AGC: רק rf_gain
    assert "agc_dynamics" in codes(res["untestable"])


def test_refine_suggestion_only_with_untested_neighbour():
    res = run(cur=0, offset={0: -6.0, 2: -3.0, 7: -3.0, 8: -8.0})
    assert res["recommendation"]["rf_gain"] == 6
    assert res["refine_suggestion"] == [5, 6, 7]
    res = run(cur=7, states=(0, 2, 4, 6, 7, 8), tx=((8, 21, 12.0),),
              offset={7: -6.0, 8: -10.0})
    assert res["recommendation"]["rf_gain"] == 6 and res["refine_suggestion"] == [5, 6, 7]
    res = run(cur=7, states=(5, 6, 7, 8), offset={7: -3.0, 8: -8.0})
    assert res["recommendation"]["rf_gain"] == 6 and res["refine_suggestion"] is None


def test_error_never_recommends_but_keeps_facts():
    rows, meta, end, ctx = gen(cur=0, clip_below={0: 99})
    ctx["error"] = "watchdog"
    res = A.analyze(rows, meta, end, ctx)
    assert res["level"] == "none" and res["headline"] == "error"
    assert res["recommendation"] is None and res["apply_allowed"] is False
    assert res["headline_params"] == {"error": "watchdog"}
    assert pstate(res, 0)["op_clip"] >= 2                        # העובדות עדיין מוצגות
    end2 = dict(end, ended="device_lost", error=None)
    res = A.analyze(rows, meta, end2, {k: v for k, v in ctx.items() if k != "error"})
    assert res["headline"] == "error" and res["error"] == "device_lost"


def test_settle_check_detects_leak_from_previous_state():
    noise = {0: 6.0, 2: 4.0, 4: 2.0, 6: 0.0, 7: -2.0, 8: -4.0}
    res = run(tx=(), noise_off=noise, leak=0.4)
    st = res["selfcheck"]["settle"]
    assert st["suspect"] is True and st["n"] >= 10 and st["median_db"] > 0
    res = run(tx=(), noise_off=noise, leak=0.0)
    assert res["selfcheck"]["settle"]["suspect"] is False


def test_invalid_fraction_flags_cpu():
    inv = {(c, s) for c in range(0, 32, 2) for s in STATES6}
    res = run(tx=(), invalid=inv)
    assert res["selfcheck"]["invalid_frac"] == 0.5
    assert "cpu_invalid" in codes(res["untestable"])
    assert all(p["invalid"] == 16 for p in res["per_state"])


def test_garbage_input_never_raises():
    rows, meta, end, ctx = gen(cur=4, tx=((8, 21, 25.0),))
    rows += [None, 7, {"i": "x"}, {"i": 9999, "lna": 12, "cyc": 3, "valid": True,
                                   "c_tot": float("nan"), "clip": "lots"}]
    ctx["states"] = [0, 2, 4, 6, 7, 8, 12, "z", None]
    res = A.analyze(rows, meta, end, ctx)
    json.dumps(res, allow_nan=False)
    assert res["states"] == list(STATES6) and res["level"] == "stat"
    for args in (([], None, None, None), ([{"garbage": 1}], {}, {}, {}),
                 ([], {"phase": "notch"}, None, {"phase": "notch"})):
        r = A.analyze(*args)
        json.dumps(r, allow_nan=False)
        assert r["level"] == "none" and r["recommendation"] is None
    assert A.live_summary([], {})["level"] == "none"
    assert A.stop_reason(None, None, None) is None


def test_diagnose_is_not_judged():
    res = A.analyze([], {"phase": "diagnose"}, None, {"phase": "diagnose"})
    assert res["phase"] == "diagnose" and res["recommendation"] is None
    assert res["apply_allowed"] is False


def test_module_is_pure_python_no_numpy_no_io():
    tree = ast.parse(MOD.read_text(encoding="utf-8"))
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            mods.add((node.module or "").split(".")[0])
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in ("open", "exec", "eval", "__import__"), node.func.id
    assert mods <= {"__future__", "math", "statistics", "functools"}, mods
    code = ("import sys; sys.modules['numpy'] = None; sys.modules['SoapySDR'] = None; "
            f"sys.path.insert(0, {str(MOD.parent)!r}); import rfcheck_analysis")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
