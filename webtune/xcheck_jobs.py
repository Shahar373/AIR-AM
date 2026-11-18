# ============================================================================
#  AIR-AM — תור ההצלבה מול ADS-B והעובד שלו (docs/adsb-crosscheck-design.md)
# ----------------------------------------------------------------------------
#  הוצא מ-app.py (שלב ג2): תור עם השהיה (heap), `_xc_worker`, הערכה (`_xc_evaluate`/`_xc_collect`), תקצירי הפיד, וראיות
#  (`xcheck_events.jsonl`). נתיבי ה-HTTP (`/api/xcheck/*`) ו-`_xc_feed_payload` נשארו ב-app.py.
#  ⚠ בידוד הרדיו (§12): ה-listeners רק מכניסים לתור (`_xc_submit` — heap, נעילה לרגע, בלי רשת/I/O, לעולם לא זורק).
#  ⚠ המעגליות (listener ⇒ תור ⇒ קריאה מהבאפרים של הפידים) נפתרת בהזרקה: `bind(buffers=…, day_bounds=…, version=…)` מ-app.py,
#  כש-`buffers` = lambda שפותר את `_xc_buffers` ב-app בזמן הקריאה. ייבוא `app` מכאן אסור (`__main__` ⇒ עותק שני).
#  ⚠ הקוראים ניגשים כ-`xcheck_jobs.<שם>` (לא `from … import`) — כך החלפה בבדיקה משפיעה.
# ============================================================================
import collections
import hashlib
import heapq
import itertools
import json
import logging
import math
import os
import re
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import acars_parse
import adsb
import appstate
import storage
import xcheck

log = logging.getLogger("airam")

# התלויות המוזרקות (ר' bind). קריאה לפני bind ⇒ AttributeError מפורש.
_D = SimpleNamespace()


def bind(**deps):
    """מזריק מ-app.py: buffers(src) ⇒ (lock, deque, jsonl_path), day_bounds(day) ⇒ (start, end), version."""
    vars(_D).update(deps)


# --- הצלבת ADS-B + אופק רדיו / BLOS (docs/adsb-crosscheck-design.md) ---------
# שלוש שכבות: adsb.py (נתונים: snapshot/buffer/lookup/trace), xcheck.py (ניתוח
# טהור), וכאן — תזמור, התמדת ראיות ו-API.
# ⚠ בידוד הרדיו (§12): ה-listeners רק *מכניסים לתור* (_xc_submit — heap בזיכרון,
# נעילה לרגע, בלי רשת ובלי I/O); כל ה-HTTP רץ ב-thread ה-worker או בבקשת "אמת"
# של המשתמש. ADS-B לא זמין => ההצלבה מדווחת "אין נתונים", הקליטה ממשיכה כרגיל.
XC_EVENTS_PATH = Path("/var/lib/airam/xcheck_events.jsonl")


XC_EVENTS_KEEP = 2000            # ראיות — שומרים הרבה (~5–10KB לאירוע)


XC_QUEUE_MAX = 400               # מעבר לזה הודעות לא מוצלבות (נספר ב-dropped) — הקליטה לא נפגעת


XC_RESULTS_MAX = 1500            # תקצירים לפיד (בזיכרון בלבד)


# שלב 2: ~40ש' אחרי הקליטה ה-poll (כל 15ש') כבר הניב נקודה *אחרי* הקליטה —
# ובזכותה אינטרפולציה במקום "הנקודה האחרונה לפני". לא שאלת איכות — תזמון ה-poll.
XC_RECHECK_SEC = 40.0


XC_LIVE_MAX_AGE_SEC = 120.0      # הודעה "חיה": snapshot/lookup חי עוד רלוונטיים לזמן הקליטה


XC_TRACK_WINDOW_SEC = 900.0      # מסלול למפה: ±15 דק' סביב הקליטה


XC_TRACK_MAX_POINTS = 400


XC_EVIDENCE_WINDOW_SEC = 300.0   # נקודות ADS-B "מקוריות" שנשמרות עם הראיה: ±5 דק'


XC_EVIDENCE_MAX_POINTS = 60


XC_VERIFY_BUDGET_SEC = 30.0      # "אמת": אחרי זה לא פותחים בקשת רשת נוספת (ה-UI מחכה ≤60ש')


XC_AUTO_LOOKUP = True            # ה-worker רשאי ל-lookup ממוקד כשהמטוס לא ב-snapshot


XC_EVENT_ID_RE = re.compile(r"^[0-9a-f]{16}$")


XC_SOURCES = ("acars", "vdl2", "satcom")


def _xc_has_identity(rec):
    return bool((rec.get("tail") and not rec.get("tail_is_station")) or rec.get("flight")
                or rec.get("icao") or rec.get("aes"))


def _xc_event_id(src, rec):
    ident = rec.get("tail") or rec.get("icao") or rec.get("aes") or rec.get("flight") or ""
    key = f"{src}|{float(rec.get('t') or 0):.3f}|{ident}|{rec.get('label') or ''}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def _xc_direction(src, rec):
    """(direction, basis). VDL2/SATCOM — הכיוון המבני שנשמר (AVLC/ISU). ACARS —
    מחושב *מחדש*: block ID שנשמר (מ-v2.31.0), אחרת MSN (acarsdec מוסיף msgno *רק*
    לבלוק downlink — ר' acars_parse._acars_block_dir), ורק אז הכללים לפי label/header. הודעה
    שנשמרה לפני תיקון (B6 שסווג uplink עד v2.26.0; ‏C1/B3/B4 עד v2.30.0) מקבלת את
    הכיוון הנכון, והשינוי מתועד ב-basis. ⚠ קריטי להצלבה: uplink שסווג downlink
    (‏loadsheet של C1) היה נבחן כשידור *מהמטוס* מול אופק הרדיו."""
    if src == "vdl2" and rec.get("icao") and rec.get("dir"):
        return rec["dir"], "מבני — כתובת AVLC"
    if src == "satcom" and rec.get("aes") and rec.get("dir"):
        return rec["dir"], "מבני — שכבת ISU"
    label, text = rec.get("label"), rec.get("text")
    d = acars_parse._acars_block_dir(rec.get("block_id")) if src == "acars" else None
    if d:
        basis = "מבני — block ID של המסגרת"
    elif src == "acars" and rec.get("msgno"):
        d, basis = "downlink", "מבני — יש מספר הודעה (MSN), שנשלח רק מהמטוס"
    elif label in acars_parse._ACARS_DIR_BY_LABEL:
        d, basis = acars_parse._ACARS_DIR_BY_LABEL[label], f"לפי label {label}"
    else:
        d = acars_parse._acars_direction(label, text)
        basis = "לפי כותרת ניתוב (heuristic)" if d else None
    if d and rec.get("dir") and rec["dir"] != d:
        basis += f"; נשמר בעבר כ-{rec['dir']} — חושב מחדש"
    return d, basis


def _xc_adsc(rec, direction):
    """פרטי דוח ה-ADS-C (ר' acars_parse._adsc_details) + זמן מוחלט של הדוח. הודעה שנשמרה לפני
    שהשדה נוסף — re-decode מקומי ב-decode_acars_apps (כלי libacars הרשמי, מקור-האמת
    לתוכן; אותו מסלול כמו acars_parse._decode_libacars_app) בכיוון שחושב. אין => None."""
    d, how = rec.get("adsc"), "message"
    if (not d and rec.get("pos_src") == "adsc" and direction in ("uplink", "downlink")
            and rec.get("label") and isinstance(rec.get("text"), str)):
        out = acars_parse._run_libacars("d" if direction == "downlink" else "u", rec["label"], rec["text"],
                            as_json=True)
        body = acars_parse._strip_echoed_msg(out, rec["text"]) if out else ""
        i = body.find("{")
        if i >= 0:
            try:
                d, how = acars_parse._adsc_details(json.loads(body[i:])), "redecode"
            except ValueError:
                d = None
    if not d:
        return None
    return {**d, "t": xcheck.adsc_report_time(d.get("ts_sec"), rec.get("t")), "source": how}


def _xc_point(rec, provider, origin, t_fallback):
    """נקודת ADS-B מרשומת snapshot/lookup. רשומה משובשת (מיקום מדוכא) נשארת
    כנקודה בלי מיקום — כדי ש-position_at ידווח "מזויף" ולא "אין נתונים"."""
    t = rec.get("t_pos") if rec.get("t_pos") is not None else t_fallback
    return adsb.make_point(t, rec.get("lat"), rec.get("lon"), alt_baro=rec.get("alt_baro"),
                           alt_geom=rec.get("alt_geom"), gs=rec.get("gs"), track=rec.get("track"),
                           src_type=rec.get("src_type"), nic=rec.get("nic"), nac_p=rec.get("nac_p"),
                           provider=provider, origin=origin)


_XC_BY_RANK = {"hex": 3, "reg": 2, "flight": 1, None: 0}


def _xc_collect(rec, ident, *, network, full, prior=None, deadline=None, imported=None):
    """אוסף נקודות ADS-B סביב זמן הקליטה: snapshot חי → buffer מקומי → lookup
    ממוקד → (full בלבד) trace היסטורי → OpenSky. כל מקור מתועד ב-sources —
    כולל מה *לא* נמצא ולמה ("לא ניסינו" ≠ "ניסינו ולא נמצא", §12)."""
    t = float(rec.get("t") or time.time())
    now = time.time()
    pts, sources = list(prior or []), []
    best = {"by": None, "conflict": None, "meta": None, "conflict_src": None}

    def budget_ok():
        return deadline is None or time.monotonic() < deadline

    def consider(name, cand, weak=False):
        """מתאים רשומת ADS-B לזהות. מחזיר by או None; מעדכן את ההתאמה הטובה ביותר.
        ⚠ סתירה נרשמת רק כשהמועמד נמצא לפי מזהה *חזק* (weak=False): מטוס אחר עם
        אותו מספר טיסה הוא "לא זה", לא "סתירת זהות"."""
        by, conflict = xcheck.match_record(ident, cand)
        if weak and conflict:
            return None
        if by and _XC_BY_RANK[by] > _XC_BY_RANK[best["by"]]:
            best.update(by=by, meta={k: cand.get(k) for k in ("hex", "reg", "flight", "type")
                                     if cand.get(k)})
        if conflict in ("hex_mismatch", "reg_mismatch") and not best["conflict"]:
            best.update(conflict=conflict, conflict_src=name)
        if by and not ident["hex"] and adsb.norm_hex(cand.get("hex")):
            ident.update(hex=adsb.norm_hex(cand["hex"]),
                         hex_src="adsb_db" if by in ("hex", "reg") else "flight_lookup")
        return by

    recent = abs(now - t) <= XC_LIVE_MAX_AGE_SEC
    if recent:
        cand, weak = adsb.local_aircraft(ident["hex"], ident["reg"]), False
        if cand is None and ident["flight_key"]:
            cand, weak = next((a for a in adsb.all_aircraft()
                               if xcheck.flight_key(a.get("flight")) == ident["flight_key"]), None), True
        by = consider("snapshot", cand, weak) if cand else None
        snapshot_matched = bool(by)
        if by:
            pts.append(_xc_point(cand, "poll", "live", now - (cand.get("age") or 0)))
        sources.append({"name": "snapshot", "status": "ok" if by else "miss"})
    else:
        snapshot_matched = False
        sources.append({"name": "snapshot", "status": "skipped", "detail": "ההודעה ישנה מ-2 דק'"})

    if not full:
        # ⚠ ה-worker לא קורא את track.jsonl (עד ~5MB ב-300 מטוסים × 90 דק') על כל
        # הודעה — זה CPU (ו-GIL ליד ה-listeners) בלי צורך: נקודות ה-snapshot/lookup
        # של שלב 1 עוברות לשלב 2 (prior) ונותנות את שני צידי הקליטה.
        sources.append({"name": "buffer", "status": "skipped",
                        "detail": "מצב חי — נקודות snapshot/lookup משני שלבי הבדיקה"})
    elif now - t <= adsb.TRACK_BUFFER_MIN * 60.0 + XC_TRACK_WINDOW_SEC:
        bp, gaps = adsb.local_track_points(t - XC_TRACK_WINDOW_SEC, t + XC_TRACK_WINDOW_SEC,
                                           reg=ident["reg"], hex_=ident["hex"])
        near_gaps = [g for g in gaps if isinstance(g.get("t"), (int, float)) and abs(g["t"] - t) <= 120]
        if bp:
            pts += bp
            by = "reg" if ident["reg"] else "hex"
            if _XC_BY_RANK[by] > _XC_BY_RANK[best["by"]]:
                best.update(by=by, meta=best["meta"] or {"reg": ident["reg"]})
        sources.append({"name": "buffer", "status": "ok" if bp else "miss", "points": len(bp),
                        "detail": f"{len(near_gaps)} פערי קליטת ADS-B סביב זמן הקליטה" if near_gaps else None})
    else:
        sources.append({"name": "buffer", "status": "skipped",
                        "detail": f"מחוץ לחלון ה-buffer ({adsb.TRACK_BUFFER_MIN:g} דק')"})

    if not ident["hex"] and ident["reg"]:
        hx = adsb.reg_to_hex(ident["reg"])
        if hx:
            ident.update(hex=hx, hex_src="adsb_db")

    # lookup ממוקד רק כשצריך: הודעה חיה שהמטוס שלה לא ב-snapshot (מחוץ לרדיוס
    # ה-poll), או "אמת" מלא בלי hex (לזיהוי ה-hex עבור ה-trace). בשלב 2 של ה-worker
    # זה lookup *נוסף* — נקודה אחרי הקליטה (ה-cache של שלב 1 כבר פג, LOOKUP_TTL_HIT).
    want_lookup = network and ((recent and not snapshot_matched) or (full and not ident["hex"]))
    if want_lookup and budget_ok():
        if ident["hex"]:
            kind, val = "hex", ident["hex"]
        elif ident["reg"]:
            kind, val = "reg", rec.get("tail")
        else:
            kind, val = "callsign", ident["flight_key"]
        if val:
            res = adsb.lookup(kind, val)
            got = 0
            for a in res.get("aircraft") or []:
                if consider("lookup", a, weak=kind == "callsign") and recent:
                    pts.append(_xc_point(a, a.get("provider"), "lookup", a.get("fetched_at") or now))
                    got += 1
            sources.append({"name": "lookup", "status": res.get("status"), "kind": kind,
                            "provider": res.get("provider"), "points": got,
                            "detail": "; ".join(res.get("errors") or []) or None})

    if imported is not None:
        # trace שהמשתמש העלה (adsb.import_trace). ⚠ הקובץ חייב לזהות את *המטוס הזה*
        # לפי ה-icao/r שבו — קובץ של מטוס אחר שנבחר בטעות לא הופך לראיה. hex שנלמד
        # מכאן מסומן "import": זיהוי לפי רישום תואם, לא hex מבני מההודעה.
        meta = imported.get("meta") or {}
        f_hex, f_reg = adsb.norm_hex(meta.get("icao")), adsb.norm_reg(meta.get("r"))
        status, by = imported.get("status"), None
        if status == "ok":
            if ident["hex"] and f_hex and f_hex != ident["hex"]:
                status = "conflict"
                if not best["conflict"] and ident.get("hex_src") in ("message", "user"):
                    best.update(conflict="hex_mismatch", conflict_src="import")
            elif ident["reg"] and f_reg and f_reg != ident["reg"]:
                status = "conflict"
                if not best["conflict"]:
                    best.update(conflict="reg_mismatch", conflict_src="import")
            elif ident["hex"] and f_hex == ident["hex"]:
                by = "hex" if ident.get("hex_src") in ("message", "user") else "reg"
            elif ident["reg"] and f_reg == ident["reg"]:
                by = "reg"
            else:
                status = "unmatched"            # אין בקובץ מזהה שאפשר להשוות להודעה
        if by:
            pts += imported["points"]
            if not ident["hex"] and f_hex:
                ident.update(hex=f_hex, hex_src="import")
            if _XC_BY_RANK[by] > _XC_BY_RANK[best["by"]]:
                best.update(by=by, meta={"hex": f_hex, "reg": meta.get("r"), "type": meta.get("t")})
        covers = (imported.get("t_first") is not None
                  and imported["t_first"] <= t <= imported["t_last"])
        sources.append({"name": "import", "status": "ok" if by else status,
                        "file": imported.get("name"), "points": len(imported["points"]) if by else 0,
                        "covers": covers if by else None,
                        "detail": imported.get("error") or (
                            "הקובץ לא מזהה את המטוס שבהודעה" if status == "unmatched" else None)})
    if full and network and imported is None:
        if ident["hex"] and budget_ok():
            tr = adsb.fetch_trace(ident["hex"], t)
            use = tr.get("status") == "ok"
            meta_reg = adsb.norm_reg((tr.get("meta") or {}).get("r"))
            if use and ident["reg"] and meta_reg and meta_reg != ident["reg"]:
                use = False                     # ה-trace של hex אחר מהרישום שבהודעה
                if not best["conflict"] and ident.get("hex_src") in ("message", "user"):
                    best.update(conflict="reg_mismatch", conflict_src="trace")
            if use:
                pts += tr["points"]
                by = "hex" if ident.get("hex_src") in ("message", "user") else (
                    "reg" if ident.get("hex_src") == "adsb_db" else "flight")
                if _XC_BY_RANK[by] > _XC_BY_RANK[best["by"]]:
                    best.update(by=by, meta={"hex": ident["hex"], "reg": (tr.get("meta") or {}).get("r"),
                                             "type": (tr.get("meta") or {}).get("t")})
            sources.append({"name": "trace", "status": tr.get("status") if use or tr.get("status") != "ok"
                            else "conflict", "provider": tr.get("provider"), "url": tr.get("url"),
                            "points": len(tr.get("points") or []) if use else 0,
                            "covers": tr.get("covers"),
                            "detail": "; ".join(tr.get("errors") or []) or None})
            if not (use and tr.get("covers")) and budget_ok():
                os_ = adsb.fetch_opensky_track(ident["hex"], t)
                if os_.get("status") == "ok":
                    pts += os_["points"]
                sources.append({"name": "opensky", "status": os_.get("status"),
                                "points": len(os_.get("points") or []), "covers": os_.get("covers"),
                                "detail": "; ".join(os_.get("errors") or []) or None})
        elif not ident["hex"]:
            sources.append({"name": "trace", "status": "no_hex",
                            "detail": "כתובת ICAO לא ידועה (אפשר להזין ידנית)"})
        else:
            sources.append({"name": "trace", "status": "skipped", "detail": "חרגנו מתקציב הזמן"})

    # dedup: אותה נקודה משני מקורות (buffer+snapshot) לא נספרת פעמיים
    seen, uniq = set(), []
    for p in sorted((p for p in pts if p.get("t") is not None), key=lambda p: p["t"]):
        k = (round(p["t"], 1), p.get("lat"), p.get("lon"))
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    by = best["by"]
    if by == "hex" and ident.get("hex_src") == "adsb_db":
        by = "reg"                               # ה-hex נגזר מהרישום — ההתאמה היא לפי רישום
    return {"points": uniq, "sources": sources,
            "match": {"by": by, "conflict": best["conflict"], "conflict_src": best["conflict_src"],
                      "hex": ident["hex"], "hex_src": ident.get("hex_src"), "adsb": best["meta"]}}


def _xc_track(points, t):
    """מסלול למפה: נקודות עם מיקום בטווח ±XC_TRACK_WINDOW_SEC, מדולל ל-XC_TRACK_MAX_POINTS."""
    pts = [p for p in points if p.get("lat") is not None and p.get("lon") is not None
           and abs(p["t"] - t) <= XC_TRACK_WINDOW_SEC]
    step = max(1, math.ceil(len(pts) / XC_TRACK_MAX_POINTS))
    return [{"t": p["t"], "lat": p["lat"], "lon": p["lon"],
             "alt": p.get("alt_geom") if p.get("alt_geom") is not None else p.get("alt_baro"),
             "src_type": p.get("src_type"), "origin": p.get("origin")} for p in pts[::step]]


def _xc_evidence_points(points, t):
    near = [p for p in points if abs(p["t"] - t) <= XC_EVIDENCE_WINDOW_SEC]
    near.sort(key=lambda p: abs(p["t"] - t))
    return sorted(near[:XC_EVIDENCE_MAX_POINTS], key=lambda p: p["t"])


def _xc_evaluate(src, rec, *, network, full, hex_override=None, station=None, prior=None,
                 deadline=None, imported=None):
    """הצלבה מלאה להודעה אחת => (תוצאה, נקודות). התוצאה = xcheck.assess + כל מה
    שנדרש כדי לחקור אותה אחר כך (ההודעה המקורית, הזהות, מקורות שנוסו, נקודות
    ADS-B מקוריות, מסלול, התחנה שבשימוש, גרסת התוכנה)."""
    t = float(rec.get("t") or time.time())
    ident = xcheck.message_identity(rec, hex_override)
    direction, basis = _xc_direction(src, rec)
    adsc = _xc_adsc(rec, direction)
    col = _xc_collect(rec, ident, network=network, full=full, prior=prior, deadline=deadline,
                      imported=imported)
    pts = col["points"]
    rx_at = xcheck.position_at(pts, t)
    pos_at = (xcheck.position_at(pts, adsc["t"])
              if adsc and adsc.get("t") is not None and xcheck.valid_latlon(rec.get("lat"), rec.get("lon"))
              else None)
    st = station if station is not None else appstate.load_state().get("station")
    res = xcheck.assess(src, rec, direction, st, col["match"], rx_at, pos_at, adsc,
                        direction_basis=basis)
    res.update({
        "event_id": _xc_event_id(src, rec), "src": src, "t_rx": t,
        "msg": {k: v for k, v in rec.items() if k != "id"},
        "identity": ident, "match": col["match"], "sources": col["sources"],
        "rx_at": rx_at, "pos_at": pos_at,
        "adsb_points": _xc_evidence_points(pts, t), "track": _xc_track(pts, t),
        "mode": "full" if full else "live", "computed_at": round(time.time(), 1),
        "version": _D.version,
    })
    return res, pts


# --- worker: תור עם השהיה (שלב 1 מיד, שלב 2 אחרי XC_RECHECK_SEC) -----------------
_xc_cv = threading.Condition()


_xc_heap = []                          # (due_mono, tie, job)


_xc_tie = itertools.count()


_xc_stats = {"submitted": 0, "dropped": 0, "done": 0, "errors": 0, "events_saved": 0}


_xc_res_lock = threading.Lock()


_xc_results = collections.OrderedDict()    # (src, id) -> (seq, summary)


_xc_seq = 0


_XC_EVENTS_LOCK = threading.Lock()


def _xc_push(due, job):
    with _xc_cv:
        if len(_xc_heap) >= XC_QUEUE_MAX:
            _xc_stats["dropped"] += 1
            return False
        heapq.heappush(_xc_heap, (due, next(_xc_tie), job))
        _xc_cv.notify()
        return True


def _xc_submit(src, rec):
    """נקרא מ-thread ה-listener אחרי ההתמדה: הכנסה לתור בלבד — בלי רשת, בלי דיסק,
    נעילה לרגע. ⚠ לעולם לא זורק: שום דבר בהצלבה לא רשאי לעצור את קליטת הרדיו."""
    try:
        if not _xc_has_identity(rec):
            return
        if _xc_push(time.monotonic(), {"src": src, "rec": dict(rec), "stage": 1, "prior": []}):
            with _xc_cv:
                _xc_stats["submitted"] += 1
    except Exception:
        log.exception("xcheck: הכנסה לתור נכשלה — ההודעה לא תוצלב")


def _xc_store(src, msg_id, res):
    global _xc_seq
    if msg_id is None:
        return
    summ = xcheck.summary(res)
    with _xc_res_lock:
        _xc_seq += 1
        key = (src, msg_id)
        _xc_results[key] = (_xc_seq, summ)
        _xc_results.move_to_end(key)
        while len(_xc_results) > XC_RESULTS_MAX:
            _xc_results.popitem(last=False)


def _xc_run_job(job):
    src, rec = job["src"], job["rec"]
    res, pts = _xc_evaluate(src, rec, network=XC_AUTO_LOOKUP, full=False, prior=job["prior"])
    _xc_store(src, rec.get("id"), res)
    rx = res.get("rx_at") or {}
    improvable = (rx.get("status") not in ("measured", "interpolated")
                  or rx.get("status") == "measured" and abs(rx.get("dt_s") or 0) > 5)
    if job["stage"] == 1 and res["match"]["by"] and improvable:
        # שלב 2: נקודה *אחרי* הקליטה תאפשר אינטרפולציה. נקודות snapshot/lookup של
        # שלב 1 עוברות הלאה — הן הצד ה"לפני" (ה-snapshot מתחלף ב-poll הבא).
        delay = max(0.0, float(rec.get("t") or time.time()) + XC_RECHECK_SEC - time.time())
        if delay > 0 and _xc_push(time.monotonic() + delay,
                                  {"src": src, "rec": rec, "stage": 2,
                                   "prior": [p for p in pts if p.get("origin") in ("live", "lookup")]}):
            return
    if xcheck.is_anomalous(res):
        _xc_save_event(res, "auto")


def _xc_worker():
    """thread יחיד: מוציא משימות שזמנן הגיע. חריגה במשימה לא מפילה את ה-thread."""
    while True:
        with _xc_cv:
            while True:
                now = time.monotonic()
                if _xc_heap and _xc_heap[0][0] <= now:
                    _due, _tie, job = heapq.heappop(_xc_heap)
                    break
                _xc_cv.wait(None if not _xc_heap else max(0.05, _xc_heap[0][0] - now))
        try:
            _xc_run_job(job)
            with _xc_cv:
                _xc_stats["done"] += 1
        except Exception:
            with _xc_cv:
                _xc_stats["errors"] += 1
            log.exception("xcheck: הצלבה נכשלה (%s)", job.get("src"))


# --- ראיות: אירועים חריגים (xcheck_events.jsonl) --------------------------------
def _xc_save_event(res, reason):
    """שורה לאירוע (append + fsync — ראיה מניסוי שטח, כמו rf_log). אותו אירוע
    שנשמר שוב (אימות חוזר) — "האחרון מנצח" בקריאה. מחזיר את ה-id או None."""
    rec = {**res, "id": res["event_id"], "saved_at": round(time.time(), 1), "reason": reason}
    data = (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8")
    with _XC_EVENTS_LOCK:
        try:
            XC_EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
            with XC_EVENTS_PATH.open("a+b") as f:
                storage.append_line_on_boundary(f, data)
                f.flush()
                os.fsync(f.fileno())
        except OSError:
            log.exception("xcheck: שמירת ראיה נכשלה")
            return None
        with _xc_cv:
            _xc_stats["events_saved"] += 1
            n = _xc_stats["events_saved"]
        if n % 50 == 0:
            storage.trim_jsonl_log(XC_EVENTS_PATH, XC_EVENTS_KEEP)
    return rec["id"]


def _xc_read_events():
    """כל האירועים, הגרסה האחרונה לכל id, מהחדש לישן (לפי זמן הקליטה)."""
    try:
        lines = storage.read_log_lines(XC_EVENTS_PATH)
    except OSError:
        return []
    latest = {}
    for r in storage.jsonl_records(lines):
        if isinstance(r.get("id"), str):
            latest[r["id"]] = r
    return sorted(latest.values(), key=lambda r: r.get("t_rx") or 0, reverse=True)


def _xc_event_row(e):
    """שורת סיכום שטוחה לאירוע — לרשימה ב-UI ולייצוא CSV."""
    m, hz = e.get("msg") or {}, e.get("horizon") or {}
    rx, pc = e.get("rx_at") or {}, e.get("position_check") or {}
    st, mt = e.get("station") or {}, e.get("match") or {}
    return {
        "id": e.get("id"), "t": e.get("t_rx"), "src": e.get("src"), "tail": m.get("tail"),
        "flight": m.get("flight"), "hex": mt.get("hex"), "match_by": mt.get("by"),
        "freq": m.get("freq"), "signal_dbfs": m.get("level"), "error": m.get("error"),
        "label": m.get("label"), "dir": e.get("direction"),
        "msg_lat": m.get("lat"), "msg_lon": m.get("lon"), "pos_src": m.get("pos_src"),
        "adsb_status": rx.get("status"), "adsb_lat": rx.get("lat"), "adsb_lon": rx.get("lon"),
        "adsb_dt_s": json.dumps(rx.get("dt_s")) if isinstance(rx.get("dt_s"), list) else rx.get("dt_s"),
        "adsb_provider": rx.get("provider"), "adsb_src_type": rx.get("src_type"),
        "adsb_uncert_km": rx.get("uncert_km"),
        "pos_diff_km": pc.get("distance_km"), "pos_agree": pc.get("agree"),
        "station_lat": st.get("lat"), "station_lon": st.get("lon"), "station_alt_m": st.get("alt_m"),
        "distance_km": hz.get("distance_km"), "radio_horizon_km": hz.get("radio_km"),
        "geometric_horizon_km": hz.get("geometric_km"), "excess_km": hz.get("excess_km"),
        "aircraft_alt_ft": hz.get("aircraft_alt_ft"),
        "verdict": e.get("verdict"), "verdict_text": e.get("verdict_text"),
        "evidence_level": e.get("level"), "reason": e.get("reason"), "saved_at": e.get("saved_at"),
        "text": m.get("text"),
    }


XC_EXPORT_COLS = ["time_iso", "timestamp", "id", "src", "tail", "flight", "hex", "match_by",
                  "freq", "signal_dbfs", "error", "label", "dir", "msg_lat", "msg_lon", "pos_src",
                  "adsb_status", "adsb_lat", "adsb_lon", "adsb_dt_s", "adsb_provider",
                  "adsb_src_type", "adsb_uncert_km", "pos_diff_km", "pos_agree", "station_lat",
                  "station_lon", "station_alt_m", "distance_km", "radio_horizon_km",
                  "geometric_horizon_km", "excess_km", "aircraft_alt_ft", "verdict",
                  "verdict_text", "evidence_level", "reason", "saved_at", "text"]


def _xc_filter(events, args):
    """סינון לפי ?min_km=&max_km=&min_dbfs=&max_dbfs=&day=YYYY-MM-DD&evidence=&verdict=."""
    def fnum(k):
        try:
            return float(args.get(k)) if args.get(k) not in (None, "") else None
        except (TypeError, ValueError):
            return None
    min_km, max_km, min_db, max_db = fnum("min_km"), fnum("max_km"), fnum("min_dbfs"), fnum("max_dbfs")
    bounds = _D.day_bounds(args["day"]) if args.get("day") else None
    levels = set(filter(None, (args.get("evidence") or "").split(",")))
    verdicts = set(filter(None, (args.get("verdict") or "").split(",")))
    out = []
    for e in events:
        d = (e.get("horizon") or {}).get("distance_km")
        lv = (e.get("msg") or {}).get("level")
        if min_km is not None and (d is None or d < min_km):
            continue
        if max_km is not None and (d is None or d > max_km):
            continue
        if min_db is not None and (lv is None or lv < min_db):
            continue
        if max_db is not None and (lv is None or lv > max_db):
            continue
        if bounds and not bounds[0] <= (e.get("t_rx") or 0) < bounds[1]:
            continue
        if levels and e.get("level") not in levels:
            continue
        if verdicts and e.get("verdict") not in verdicts:
            continue
        out.append(e)
    return out


def _xc_find_message(src, t, tail=None, label=None):
    """ההודעה המקורית — מהזיכרון, ואם אינה שם (ארכיון/אחרי restart) — מה-jsonl.
    ⚠ הראיה נבנית תמיד מהעותק *שבשרת*, לא ממה שהדפדפן שלח."""
    lock, msgs, path = _D.buffers(src)

    def ok(r):
        return (abs(float(r.get("t") or 0) - t) < 1e-3
                and (tail is None or r.get("tail") == tail)
                and (label is None or r.get("label") == label))
    with lock:
        for m in reversed(msgs):
            if ok(m):
                return dict(m)
    for r in reversed(storage.read_jsonl_log(path)):
        if ok(r):
            return r
    return None
