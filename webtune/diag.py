# ============================================================================
#  AIR-AM — יומן בדיקה וייצוא לוגים: החלק הטהור
# ----------------------------------------------------------------------------
#  פענוח פלט journalctl/systemctl, זיהוי אירועים (נפילת מתח, ניתוק USB, קריסת
#  שירות), הסקת "בדיקות" (תקין/אזהרה/תקלה) ובניית summary.md.
#  בלי subprocess, בלי Flask, בלי קבצים — app.py אוסף, כאן רק מנתחים. כך כל
#  הלוגיקה נבדקת על טקסט קבוע (tests/test_diag.py).
#
#  עיקרון (CLAUDE.md §12): "תקלה" ו"אזהרה" נגזרות רק מעובדה שנרשמה — שורת קרנל,
#  שירות במצב failed, דגל של ה-Pi, health שאומר ok=false. אין כאן אף סף מספרי
#  שהמצאנו (מתח, טמפרטורה, CPU מוצגים כערכים, לא כפסק דין). ומה שלא נקרא
#  (הרשאה ליומן, אין vcgencmd) מדווח "לא נבדק" — לא "תקין".
# ============================================================================
import json
import re
import time

LEVELS = ("ok", "info", "warn", "fail")
_RANK = {"info": 0, "ok": 1, "warn": 2, "fail": 3}
LEVEL_ICON = {"ok": "✅", "info": "ℹ️", "warn": "⚠️", "fail": "❌"}
LEVEL_TEXT = {"ok": "תקין", "info": "מידע", "warn": "אזהרה", "fail": "תקלה"}

SDRPLAY_VENDOR = "1df7"

# שורות קרנל שמעניינות תחנה על power bank עם מקלטי USB. ⚠ רק טקסט שהקרנל
# כותב בעצמו — לא ניחוש. ‏Pi 5: ‏"hwmon hwmon1: Undervoltage detected!".
KERNEL_PATTERNS = (
    ("undervoltage", re.compile(r"under-?voltage detected", re.I)),
    ("voltage_normal", re.compile(r"voltage normali[sz]ed", re.I)),
    ("overcurrent", re.compile(r"over-?current", re.I)),
    ("usb_disconnect", re.compile(r"USB disconnect", re.I)),
    ("usb_new", re.compile(r"New USB device found", re.I)),
    ("oom", re.compile(r"Out of memory|oom-kill|invoked oom-killer", re.I)),
)

# הודעות של systemd עצמו על יחידה (מגיעות עם UNIT=) ושל התוכנה (מגיעות עם _SYSTEMD_UNIT=)
UNIT_PATTERNS = (
    ("started", re.compile(r"^Started ")),
    ("stopped", re.compile(r"^Stopped |Deactivated successfully")),
    ("failed", re.compile(r"Failed with result")),
    ("exited", re.compile(r"Main process exited, code=(?!exited, status=0/SUCCESS)")),
    ("restart", re.compile(r"Scheduled restart job")),
    ("start_limit", re.compile(r"Start request repeated too quickly")),
    ("traceback", re.compile(r"^Traceback \(most recent call last\)")),
    ("error", re.compile(r"^(ERROR|CRITICAL) ")),
)
# אירועים שנכנסים לציר הזמן ב-summary (השאר רק נספרים)
_TIMELINE_UNIT_KINDS = {"started", "stopped", "failed", "exited", "restart", "start_limit", "traceback"}
_TIMELINE_KERNEL_KINDS = {"undervoltage", "voltage_normal", "overcurrent", "usb_disconnect", "oom"}


def worst(levels):
    """הרמה החמורה ביותר (info לא משפיע — מתחיל מ-ok)."""
    out = "ok"
    for lv in levels:
        if _RANK.get(lv, 0) > _RANK[out]:
            out = lv
    return out


def fmt_t(t, with_date=True):
    """epoch ⇒ שעה מקומית של ה-Pi (כך גם journalctl מציג)."""
    if t is None:
        return "—"
    lt = time.localtime(t)
    ms = int((t % 1) * 1000)
    base = time.strftime("%Y-%m-%d %H:%M:%S" if with_date else "%H:%M:%S", lt)
    return f"{base}.{ms:03d}"


# --- journalctl -o json --------------------------------------------------------

def _msg_text(v):
    """MESSAGE של journald: מחרוזת, או מערך בייטים כשהטקסט אינו UTF-8 תקין."""
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        try:
            return bytes(int(b) & 0xFF for b in v).decode("utf-8", "replace")
        except (TypeError, ValueError):
            return ""
    return "" if v is None else str(v)


def parse_journal_json(text):
    """פלט ‎`journalctl -o json` (אובייקט לשורה) ⇒ רשימת רשומות
    {t, unit, ident, pid, msg}. שורה פגומה מדולגת (לא זורק)."""
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if not isinstance(o, dict):
            continue
        try:
            t = int(o.get("__REALTIME_TIMESTAMP")) / 1e6
        except (TypeError, ValueError):
            continue
        # הודעות של systemd *על* יחידה מגיעות עם UNIT=; הודעות של התוכנה עצמה — _SYSTEMD_UNIT=
        unit = o.get("UNIT") or o.get("_SYSTEMD_UNIT") or ""
        if unit.endswith(".service"):
            unit = unit[:-len(".service")]
        out.append({"t": t, "unit": unit,
                    "ident": o.get("SYSLOG_IDENTIFIER") or o.get("_COMM") or "",
                    "pid": o.get("_PID") or "",
                    "msg": _msg_text(o.get("MESSAGE"))})
    out.sort(key=lambda r: r["t"])
    return out


def format_journal(recs):
    """רשומות ⇒ טקסט קריא, שורה לכל רשומה (כמו journalctl, עם מילישניות)."""
    lines = []
    for r in recs:
        who = r["ident"] + (f"[{r['pid']}]" if r["pid"] else "")
        for i, part in enumerate((r["msg"] or "").splitlines() or [""]):
            lines.append(f"{fmt_t(r['t'])} {who}: {part}" if i == 0 else f"{' ' * 24}{part}")
    return "\n".join(lines) + ("\n" if lines else "")


def classify(msg, patterns):
    for kind, rx in patterns:
        if rx.search(msg or ""):
            return kind
    return None


def unit_events(recs):
    """{unit: {kind: count}} + ציר-זמן [(t, unit, kind, msg)] של האירועים החשובים."""
    counts, timeline = {}, []
    for r in recs:
        kind = classify(r["msg"], UNIT_PATTERNS)
        if not kind:
            continue
        c = counts.setdefault(r["unit"] or r["ident"], {})
        c[kind] = c.get(kind, 0) + 1
        if kind in _TIMELINE_UNIT_KINDS:
            timeline.append((r["t"], r["unit"] or r["ident"], kind, r["msg"].splitlines()[0][:200]))
    return counts, timeline


def kernel_events(recs):
    """{kind: [(t, msg)]} משורות הקרנל. ניתוק/חיבור USB נשמרים כולם — ההכרעה
    אם זה ה-SDR נעשית מול vendor 1df7 בשורת ה-New USB device (ר' analyze)."""
    out = {}
    for r in recs:
        kind = classify(r["msg"], KERNEL_PATTERNS)
        if kind:
            out.setdefault(kind, []).append((r["t"], r["msg"].splitlines()[0][:200]))
    return out


# --- systemctl show -------------------------------------------------------------

def parse_systemctl_show(text):
    """‏`systemctl show -p A,B u1 u2` ⇒ בלוקים מופרדים בשורה ריקה ⇒ {unit: {prop: value}}."""
    units, cur = {}, {}
    for line in (text or "").splitlines() + [""]:
        if not line.strip():
            if cur.get("Id"):
                uid = cur["Id"]
                units[uid[:-len(".service")] if uid.endswith(".service") else uid] = cur
            cur = {}
            continue
        k, sep, v = line.partition("=")
        if sep:
            cur[k.strip()] = v.strip()
    return units


# --- הבדיקות ------------------------------------------------------------------

def _chk(level, title, detail=""):
    return {"level": level, "title": title, "detail": detail}


def _times(items, n=4):
    ts = [fmt_t(t, with_date=False)[:8] for t, *_ in items[:n]]
    return ", ".join(ts) + (f" (+{len(items) - n})" if len(items) > n else "")


def analyze(d):
    """d: מה ש-app.py אסף (ר' _diag_collect). מחזיר רשימת בדיקות, החמורות קודם."""
    checks = []
    health = d.get("health")
    if not health:
        checks.append(_chk("warn", "מצב המערכת לא נקרא", d.get("health_error") or ""))
    elif health.get("ok"):
        checks.append(_chk("ok", f"המצב השמור רץ ({health.get('app_mode')})"))
    else:
        bad = [u for u, s in (health.get("services") or {}).items() if s == "failed"]
        checks.append(_chk("fail", f"המצב השמור ({health.get('app_mode')}) לא רץ כרגע",
                           ("יחידות failed: " + ", ".join(bad)) if bad else "ר' services ב-snapshot.json"))

    units = d.get("units") or {}
    failed = sorted(u for u, p in units.items() if p.get("ActiveState") == "failed")
    if failed:
        checks.append(_chk("fail", "יחידות במצב failed עכשיו: " + ", ".join(failed),
                           "; ".join(f"{u}: Result={units[u].get('Result')}" for u in failed)))
    restarted = {u: p.get("NRestarts") for u, p in units.items()
                 if p.get("NRestarts") not in (None, "", "0")}
    if restarted:
        checks.append(_chk("info", "הפעלות-מחדש אוטומטיות מאז שהיחידה נטענה",
                           ", ".join(f"{u}={n}" for u, n in sorted(restarted.items()))))

    # --- חשמל: יומן הקרנל הוא העדות היחידה לנפילה רגעית (vcgencmd לא רואה אותה) ---
    kev = d.get("kernel_events")
    power = d.get("power")
    if kev is None:
        checks.append(_chk("warn", "יומן הקרנל לא נקרא — נפילות מתח בחלון לא נבדקו",
                           d.get("kernel_error") or "המשתמש airam צריך להיות בקבוצה systemd-journal"))
    else:
        uv = kev.get("undervoltage", [])
        if uv:
            checks.append(_chk("fail", f"נפילת מתח (undervoltage) בחלון: {len(uv)} פעמים", _times(uv)))
        elif power and power.get("undervolt_ever"):
            checks.append(_chk("warn", "דגל undervoltage של ה-Pi דלוק מאז האתחול — אבל לא בחלון",
                                "כנראה קרה לפני תחילת החלון (או בעליית המערכת)"))
        else:
            checks.append(_chk("ok", "אין נפילת מתח ביומן הקרנל בחלון"))
        oc = kev.get("overcurrent", [])
        if oc:
            checks.append(_chk("fail", f"over-current ב-USB: {len(oc)}", _times(oc)))
        if kev.get("oom"):
            checks.append(_chk("fail", f"נגמר הזיכרון (OOM): {len(kev['oom'])}", _times(kev["oom"])))
        disc = kev.get("usb_disconnect", [])
        sdr_new = [e for e in kev.get("usb_new", []) if f"idVendor={SDRPLAY_VENDOR}" in e[1]]
        if disc:
            checks.append(_chk("warn", f"ניתוק USB בחלון: {len(disc)} (חיבור SDRplay מחדש: {len(sdr_new)})",
                               _times(disc)))
    if power is None:
        checks.append(_chk("info", "אין vcgencmd — מצב האספקה לא נבדק"))
    elif power.get("undervolt_now") or power.get("throttle_now"):
        checks.append(_chk("fail", "ה-Pi מדווח עכשיו undervoltage/throttling",
                           f"throttled={power.get('throttled')}"))

    samples = d.get("samples") or []
    if samples:
        uv_s = [s for s in samples if (s.get("power") or {}).get("undervolt_now")]
        if uv_s:
            checks.append(_chk("fail", f"דגימות עם undervoltage פעיל: {len(uv_s)} מתוך {len(samples)}",
                               _times([(s["t"],) for s in uv_s])))
        bad = [s for s in samples if s.get("ok") is False]
        if bad:
            checks.append(_chk("warn", f"דגימות שבהן המצב השמור לא רץ: {len(bad)} מתוך {len(samples)}",
                               "לרוב מעבר מצב/עליית מקלט; רצף ארוך = תקלה. ר' test-log.jsonl"))
        volts = [s["power"]["volts_in"] for s in samples
                 if isinstance((s.get("power") or {}).get("volts_in"), (int, float))]
        temps = [s["power"]["temp"] for s in samples
                 if isinstance((s.get("power") or {}).get("temp"), (int, float))]
        if volts or temps:
            parts = []
            if volts:
                parts.append(f"מתח כניסה {min(volts):.2f}–{max(volts):.2f}V")
            if temps:
                parts.append(f"טמפ' {min(temps):.1f}–{max(temps):.1f}°C")
            checks.append(_chk("info", "טווחי מדידה בדגימות", " · ".join(parts)))

    # --- יחידות ושרת ---
    if d.get("units_error"):
        checks.append(_chk("warn", "יומני השירותים לא נקראו", d["units_error"]))
    for unit, c in sorted((d.get("unit_events") or {}).items()):
        if c.get("start_limit"):
            checks.append(_chk("fail", f"{unit}: הגיע לתקרת ההפעלות (Start request repeated too quickly)"))
        n_bad = c.get("failed", 0) + c.get("exited", 0)
        if n_bad or c.get("restart"):
            checks.append(_chk("warn", f"{unit}: יציאה לא תקינה {n_bad} · הפעלה-מחדש אוטומטית {c.get('restart', 0)}",
                               f"ר' logs/{unit}.log"))
        if c.get("traceback") or c.get("error"):
            checks.append(_chk("warn", f"{unit}: שגיאות ביומן — Traceback {c.get('traceback', 0)}, ERROR {c.get('error', 0)}",
                               f"ר' logs/{unit}.log"))

    sdr = d.get("sdr")
    if sdr:
        state = sdr.get("state")
        if state == "missing":
            checks.append(_chk("fail", "ה-SDR לא מזוהה ב-USB"))
        elif state in ("api_down", "api_error"):
            checks.append(_chk("fail", f"שירות ה-SDRplay API לא תקין ({state})", sdr.get("detail") or ""))
        elif state == "busy":
            checks.append(_chk("warn", "ה-SDR מוחזק ע\"י תוכנה אחרת",
                               ", ".join(sdr.get("suspects") or []) or ""))
        else:
            checks.append(_chk("ok" if state in ("ours", "free") else "info", f"SDR: {state}",
                               f"מקלטים ב-USB: {sdr.get('usb_count', '?')}"))

    if d.get("boot_in_window"):
        checks.append(_chk("info", "המערכת אותחלה בתוך החלון", fmt_t(d.get("boot_time"))))

    msgs = d.get("messages") or {}
    if msgs:
        checks.append(_chk("info", "הודעות/שידורים בחלון",
                           " · ".join(f"{k}: {v}" for k, v in msgs.items())))
    for err in d.get("collect_errors") or []:
        checks.append(_chk("warn", "חלק מהנתונים לא נאסף", err))

    order = {"fail": 0, "warn": 1, "ok": 2, "info": 3}
    checks.sort(key=lambda c: order[c["level"]])
    return checks


# --- ציר הזמן -------------------------------------------------------------------

def sample_transitions(samples):
    """רק השינויים: מצב/ok/שירות שהשתנה בין דגימה לדגימה."""
    out, prev = [], None
    for s in samples:
        cur = (s.get("mode"), s.get("ok"), tuple(sorted((s.get("services") or {}).items())))
        if prev is None or cur != prev:
            if prev is None:
                txt = f"מצב {s.get('mode')} · ok={s.get('ok')}"
            else:
                chg = [f"{u}: {dict(prev[2]).get(u)}→{v}" for u, v in cur[2] if dict(prev[2]).get(u) != v]
                bits = []
                if cur[0] != prev[0] or cur[1] != prev[1]:
                    bits.append(f"מצב {s.get('mode')} · ok={s.get('ok')}")
                bits += chg
                txt = " · ".join(bits)
            out.append((s["t"], "דגימה", txt))
        prev = cur
    return out


def build_timeline(test_rows, unit_tl, kernel_ev, max_items=400):
    """ציר זמן משולב: סימונים ופעולות מהטלפון, שינויי מצב בדגימות, אירועי
    יחידות, אירועי קרנל. ממוין, חתוך ל-max_items (האחרונים נשמרים)."""
    items = []
    samples = [r for r in test_rows if r.get("ev") == "sample"]
    for r in test_rows:
        ev = r.get("ev")
        if ev in ("start", "stop", "resume"):
            items.append((r["t"], "בדיקה", f"{ev} {r.get('label') or r.get('reason') or ''}".strip()))
        elif ev == "mark":
            items.append((r["t"], "📍 סימון", r.get("label", "")))
        elif ev == "request":
            st = r.get("status")
            txt = f"{r.get('method')} {r.get('path')} ⇒ {st}"
            if r.get("body"):
                txt += f" {r['body']}"
            if r.get("error"):
                txt += f" · שגיאה: {r['error']}"
            items.append((r["t"], "פעולה" + (" ❌" if isinstance(st, int) and st >= 400 else ""), txt))
    items += sample_transitions(samples)
    items += [(t, f"יחידה {u}", f"{k}: {m}") for t, u, k, m in unit_tl]
    for kind, evs in (kernel_ev or {}).items():
        if kind in _TIMELINE_KERNEL_KINDS:
            items += [(t, "קרנל", m) for t, m in evs]
    items.sort(key=lambda x: x[0])
    dropped = max(0, len(items) - max_items)
    return items[-max_items:], dropped


# --- summary.md -------------------------------------------------------------------

def _md_cell(s):
    return str(s).replace("|", "\\|").replace("\n", " ")


def render_summary(meta, checks, timeline, dropped, files):
    """summary.md — מה שקוראים ראשון. עברית, עם טבלאות פשוטות (נקרא גם בטלפון)."""
    overall = worst(c["level"] for c in checks)
    L = [f"# AIR-AM — דוח בדיקה ({LEVEL_ICON[overall]} {LEVEL_TEXT[overall]})", ""]
    L.append(f"- גרסה: **{meta.get('version')}**")
    L.append(f"- חלון: {fmt_t(meta.get('start'))} ⇐ {fmt_t(meta.get('end'))} "
             f"({round((meta['end'] - meta['start']) / 60)} דק')")
    if meta.get("test_label") is not None:
        L.append(f"- בדיקה: **{meta.get('test_label') or '(בלי שם)'}**"
                 + (" · עדיין רצה" if meta.get("test_active") else ""))
    L.append(f"- נוצר: {fmt_t(meta.get('created'))} · מצב שמור: {meta.get('app_mode')}")
    if meta.get("boot_time"):
        L.append(f"- אתחול אחרון של ה-Pi: {fmt_t(meta['boot_time'])}")
    L += ["", "## בדיקות", "", "| | בדיקה | פירוט |", "|---|---|---|"]
    for c in checks:
        L.append(f"| {LEVEL_ICON[c['level']]} | {_md_cell(c['title'])} | {_md_cell(c['detail'])} |")
    L += ["", "> תקלה/אזהרה נגזרות רק מעובדה שנרשמה (שורת קרנל, יחידה failed, דגל של ה-Pi, "
          "health). מתח/טמפ' מוצגים כערכים, בלי ספים.", ""]
    L += ["## ציר זמן", ""]
    if dropped:
        L.append(f"_({dropped} אירועים מוקדמים הושמטו — ר' הקבצים המלאים)_")
        L.append("")
    if timeline:
        L += ["| שעה | מקור | אירוע |", "|---|---|---|"]
        for t, src, txt in timeline:
            L.append(f"| {fmt_t(t, with_date=False)} | {_md_cell(src)} | {_md_cell(txt)} |")
    else:
        L.append("אין אירועים בחלון.")
    L += ["", "## קבצים בחבילה", ""]
    for name, desc in files:
        L.append(f"- `{name}` — {desc}")
    return "\n".join(L) + "\n"
