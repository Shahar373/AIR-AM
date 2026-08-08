# ============================================================================
#  AIR-AM - בדיקות התנהגות ל-UI (דפדפן אמיתי, Playwright)
# ----------------------------------------------------------------------------
#  משלימות את tests/test_frontend.py (תחביר + בדיקות סטטיות): כאן טוענים את
#  index.html *האמיתי* בדפדפן, מזריקים תשובות API מזויפות, ובודקים DOM.
#
#  למה דווקא ככה, ולא ע"י חילוץ הלוגיקה למודול נבדק:
#    §7 ב-CLAUDE.md קובע "אין build step" ו"ה-UI כולו inline" כבחירה מכוונת.
#    ‏route interception נותן כיסוי התנהגותי בלי לגעת בארכיטקטורה הזאת בכלל —
#    הקובץ שנבדק הוא בדיוק הקובץ שמשודר ל-Pi.
#
#  ⚠ הבדיקות מדלגות אוטומטית כש-playwright/דפדפן לא מותקנים, כדי שחבילת
#  הבדיקות המהירה (שאינה דורשת דפדפן) תמשיך לרוץ בכל מקום.
#
#  ⚠ בלי המתנות מבוססות-sleep. הפרויקט הזה כבר נשרף מבדיקה תלוית-תזמון (ר'
#  no_sleep ב-CHANGELOG) — כאן משתמשים ב-expect() עם ה-auto-waiting של
#  Playwright בלבד.
# ============================================================================
import json
import re
from pathlib import Path

import pytest

playwright_api = pytest.importorskip("playwright.sync_api",
                                     reason="playwright לא מותקן")
from playwright.sync_api import expect, sync_playwright   # noqa: E402

STATIC = Path(__file__).resolve().parent.parent / "webtune" / "static"
BASE = "http://airam.test/"

# נתיבי דפדפן אפשריים בסביבות שונות; None => ברירת המחדל של playwright.
_CHROME_CANDIDATES = [
    "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
]


def _chrome_path():
    for p in _CHROME_CANDIDATES:
        if Path(p).exists():
            return p
    return None


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        try:
            b = pw.chromium.launch(executable_path=_chrome_path(),
                                   args=["--no-sandbox"])
        except Exception as e:                      # דפדפן לא מותקן בסביבה הזו
            pytest.skip(f"דפדפן Chromium לא זמין: {e}")
        yield b
        b.close()


# --- שרת API מזויף ----------------------------------------------------------

def _default_api():
    """תשובות ברירת מחדל לכל endpoint שהדף מושך בטעינה. מצב: קול פעיל."""
    return {
        # ⚠ חייב לשקף את מה ש-api_state באמת מחזיר, כולל presets/mount/port/
        # version/satcom_banks — הדף קורא אותם באתחול (presetFor וכו').
        "/api/state": {"ok": True, "freq": 132.5, "mod": "am", "agc": True,
                       "if_gain": 40, "rf_gain": 0, "squelch_mode": "open",
                       "squelch_snr": 12.0, "app_mode": "voice", "mode_ok": True,
                       "prev_mode": "off", "acars_freqs": ["131.550"],
                       "vdl2_freqs": ["136.975"], "satcom_freqs": ["AF1"],
                       "acars_banks": [], "vdl2_banks": [], "satcom_banks": [],
                       "scan_plan": [], "satcom_bias_tee": True,
                       "satcom_skip_c": True, "satcom_spectrum": True,
                       "satcom_gain": None, "signal_baseline": None,
                       "presets": [{"name": "ATIS", "freq": 132.5, "sq": "open"}],
                       "mount": "airam.mp3", "port": 8000, "version": "test"},
        "/api/presets": {"ok": True, "presets": [{"name": "ATIS", "freq": 132.5}]},
        # ⚠ pollGlobalState גוזר את המצב החי מ-`services` (השירות שרץ *בפועל*),
        # לא מ-app_mode — מוק עם services ריק נראה לו כמו "הכול כבוי".
        "/api/health": {"ok": True, "app_mode": "voice", "mode_ok": True,
                        "services": {"rtl_airband": "active", "icecast2": "active",
                                     "sdrplay": "active", "airam-acars": "inactive",
                                     "airam-vdl2": "inactive", "airam-satcom": "inactive"},
                        "stats_age": 1.0},
        "/api/metrics": {"ok": True, "snr": 20.0, "signal": -30.0, "noise": -50.0,
                         "fresh": True},
        "/api/activity": {"ok": True, "events": []},
        "/api/airspace": {"ok": True, "landing": None, "takeoff": None, "gps": {}},
        "/api/power": {"ok": True, "volts": 5.1, "temp": 45.0, "throttled": "0x0"},
        "/api/metar": {"ok": True, "text": "LLBG 081000Z 27010KT CAVOK 30/18 Q1010"},
        "/api/aircraft": {"ok": True, "aircraft": []},
        "/api/session": {"ok": True, "show": False},
        "/api/scan": {"ok": True, "active": False, "idx": -1, "leg": None,
                      "next_switch_at": None, "plan": [], "now": 0},
        "/api/signal": {"ok": True, "mode": "voice", "fresh": True, "snr": 20.0,
                        "level": -30.0, "verdict": "no_baseline"},
        "/api/satcom/health": {"ok": True, "available": False},
        "/api/acars": {"ok": True, "active": False, "freqs": [], "cursor": 0,
                       "messages": [], "adsb": {}},
        "/api/vdl2": {"ok": True, "active": False, "freqs": [], "cursor": 0,
                      "messages": [], "adsb": {}},
        "/api/satcom": {"ok": True, "active": False, "freqs": [], "cursor": 0,
                        "messages": []},
    }


def _mount(page, overrides=None, on_request=None):
    """מרכיב את הדף עם API מזויף. overrides: path -> dict|callable(route,path)."""
    api = _default_api()
    api.update(overrides or {})

    def handle(route, request):
        url = request.url[len(BASE) - 1:] if request.url.startswith(BASE) else request.url
        path = url.split("?")[0]
        if on_request:
            on_request(request)
        if path == "/" or path == "":
            route.fulfill(status=200, content_type="text/html; charset=utf-8",
                          body=(STATIC / "index.html").read_text(encoding="utf-8"))
            return
        asset = STATIC / path.lstrip("/").replace("static/", "", 1)
        if path.startswith("/static/") and asset.exists():
            route.fulfill(status=200, body=asset.read_bytes())
            return
        handler = api.get(path)
        if callable(handler):
            handler(route, url)
            return
        if handler is not None:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(handler))
            return
        route.fulfill(status=404, content_type="application/json",
                      body=json.dumps({"ok": False, "error": "not mocked"}))

    page.route("**/*", handle)
    page.goto(BASE)
    return page


@pytest.fixture
def page(browser):
    ctx = browser.new_context()
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    yield pg
    ctx.close()
    # ⚠ חריגת JS לא-מטופלת בטעינה/בפולינג היא כשל אמיתי — הדף נראה "תקין"
    # אבל חלקים ממנו מתים בשקט. זו בדיוק צורת הכישלון שאין לה חיווי בשטח.
    assert not errors, "חריגות JS בדף:\n" + "\n".join(errors)


# --- הבדיקות ----------------------------------------------------------------

def test_page_loads_without_js_errors(page):
    """שער בסיסי: הדף עולה, הכותרת קיימת, ואין חריגת JS (נאכף ב-fixture)."""
    _mount(page)
    expect(page.locator("#status")).to_be_visible()


def test_message_stats_do_not_freeze_above_window_cap(page):
    """⚠ רגרסיה אמיתית (v2.15.1): מונה ההודעות "נתקע" על 500.

    ‏msgs הוא חלון-נגלל (MAX=500) לפיד/מפה/חיפוש. renderStats קרא ממנו גם את
    המספרים שאמורים לגדול כל הסשן, כך שמעל 500 הודעות המונה נראה כאילו הקליטה
    הפסיקה — בזמן שהפיד המשיך לזרום כרגיל (נצפה בשטח, לא תיאורטית).
    כאן מזרימים 600 הודעות ומוודאים שהמונה מציג 600."""
    total = 600
    state = {"sent": 0}

    def acars(route, url):
        m = re.search(r"since=(\d+)", url)
        since = int(m.group(1)) if m else 0
        msgs = []
        if since == 0 and state["sent"] == 0:
            msgs = [{"id": i + 1, "t": 1_700_000_000 + i, "tail": f"4X-EH{i % 7}",
                     "label": "10", "text": f"msg {i}", "category": "כללי",
                     "dir": "downlink"} for i in range(total)]
            state["sent"] = total
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"ok": True, "active": True, "freqs": ["131.550"],
                                       "cursor": state["sent"], "messages": msgs,
                                       "adsb": {}}))

    _mount(page, overrides={"/api/acars": acars,
                            "/api/state": {**_default_api()["/api/state"],
                                           "app_mode": "acars"},
                            "/api/health": {"ok": True, "app_mode": "acars",
                                            "mode_ok": True, "stats_age": None,
                                            "services": {"airam-acars": "active",
                                                         "sdrplay": "active",
                                                         "rtl_airband": "inactive",
                                                         "icecast2": "active",
                                                         "airam-vdl2": "inactive",
                                                         "airam-satcom": "inactive"}}})
    page.click("#modeSeg button[data-v=acars]")
    # ⚠ auto-waiting של expect, בלי sleep: הפיד מגיע בפולינג אסינכרוני
    expect(page.locator("#acarsStTotal")).to_have_text(str(total), timeout=15000)


def test_auth_failure_does_not_claim_receiver_is_off(page):
    """⚠ רגרסיה: תשובת כישלון-אימות נעצרת ב-_guard ולכן אין בה `state`,
    ו-applyMode גזר back="off" מהיעדר השדה — כלומר הכריז "המקלט בכיבוי"
    בזמן שה-SDR ממשיך לשדר. כאן /api/mode מחזיר 401 עם auth:true, ומוודאים
    שהממשק *לא* מציג standby ושהוא מסתנכרן חזרה מול /api/health (שממשיך
    לדווח שהקול חי)."""
    def mode(route, url):
        route.fulfill(status=401, content_type="application/json",
                      body=json.dumps({"ok": False, "auth": True,
                                       "error": "נדרש PIN"}))

    _mount(page, overrides={"/api/mode": mode})
    page.evaluate("window.prompt = () => null")      # ביטול תיבת ה-PIN
    page.click("#modeSeg button[data-v=home]")
    # מוודאים שהמצב ההתחלתי אכן "קול" לפני שמנסים את המעבר הכושל
    expect(page.locator("#homeStateTxt")).to_contain_text("קול", timeout=10000)
    page.click("#homeGoAcars")
    # ממתינים שתשובת השגיאה *עובדה* (setStatus יושב באותו בלוק שבו הבאג היה
    # מכריז standby) — ורק אז בודקים.
    expect(page.locator("#status")).to_contain_text("נדרש PIN", timeout=10000)
    # ⚠ קריאה מיידית דרך text_content, בלי expect ובלי retry: הבאג הוא הצהרה
    # *רגעית* שגויה, ו-pollGlobalState מתקן אותה לבד תוך 10 שניות. assertion
    # עם חלון-המתנה פשוט היה ממתין לתיקון העצמי ועובר — כלומר לא בודק כלום.
    # (אומת: גרסה קודמת של הבדיקה עברה גם כשההגנה הוסרה לגמרי.)
    txt = page.locator("#homeStateTxt").text_content()
    assert "כיבוי" not in txt, f"הממשק הכריז standby אחרי כישלון אימות: {txt!r}"
    assert "קול" in txt, f"המצב החי (קול) לא נשמר אחרי כישלון אימות: {txt!r}"
    # ⚠ והעדכון האופטימי בוטל: המשתמש חוזר לתצוגה שממנה לחץ, ולא נשאר תקוע
    # בתצוגת ACARS של מצב שכלל לא הופעל. סינכרוני — לא תלוי בפולינג התקופתי.
    assert not page.locator("#homeView").is_hidden(), "לא חזרנו לתצוגת הבית"
    assert page.locator("#acarsView").is_hidden(), "נשארנו בתצוגת ACARS שלא הופעלה"


def test_connection_chip_appears_when_server_goes_silent(page):
    """⚠ החוב שהרודמאפ (§2.1) מגדיר כתנאי מקדים למד השדה: בלי חיווי ניתוק,
    ‏Pi שנפל / Wi-Fi שנשמט מציגים נתונים ישנים ודף שנראה תקין לחלוטין.
    כאן מפילים את כל בקשות ה-API אחרי הטעינה ומוודאים שהצ'יפ מופיע."""
    _mount(page)
    expect(page.locator("#status")).to_be_visible()
    page.route("**/api/**", lambda route, request: route.abort())
    # NET_DISCONNECT_AFTER=12s + מחזור בדיקה של 3s => נותנים מרווח נדיב
    expect(page.locator("#connChip")).to_be_visible(timeout=30000)


def test_real_network_failure_reports_no_connection(page):
    """כשל רשת אמיתי => "אין חיבור לשרת" (מסלול הדחייה של ה-fetch)."""
    def dead(route, url):
        route.abort()

    _mount(page, overrides={"/api/state": dead})
    expect(page.locator("#status")).to_contain_text("אין חיבור לשרת", timeout=15000)


def test_boot_failure_is_not_reported_as_network_failure(page):
    """⚠ באג אבחון: *כל* שגיאה באתחול הוצגה כ"אין חיבור לשרת" — כולל המקרה
    שבו השרת ענה מצוין ורק משהו בגוף האתחול נכשל (שדה חסר בתשובה אחרי שדרוג,
    באג בדף). זה שולח את המשתמש לבדוק Wi-Fi ו-USB בשטח בזמן שהרשת תקינה
    לגמרי — בדיוק סוג ההטעיה ש-§12 אוסר, רק על אבחון במקום על ערך.
    כאן השרת מחזיר 200 עם גוף לא-שמיש, ומוודאים שההודעה *לא* מאשימה את הרשת."""
    _mount(page, overrides={"/api/state": None})     # 200 עם null => האתחול ייפול
    status = page.locator("#status")
    expect(status).to_contain_text("אתחול", timeout=15000)
    assert "אין חיבור" not in status.text_content()


# --- ארכיון רב-יומי + createDataView -----------------------------------------

def _acars_stream(total, day_count=3):
    """מוק ACARS: זרם חי (since=) + snapshot ארכיוני (day=). מחזיר (handler, state)."""
    state = {"sent": 0, "live_polls": 0, "day_calls": 0}

    def handler(route, url):
        if "day=" in url:
            state["day_calls"] += 1
            msgs = [{"t": 1_600_000_000 + i, "tail": f"ARC-{i}", "label": "10",
                     "text": f"archive {i}", "category": "כללי", "dir": "uplink"}
                    for i in range(day_count)]
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"ok": True, "day": "2026-01-02",
                                           "messages": msgs}))
            return
        state["live_polls"] += 1
        msgs = []
        if state["sent"] == 0:
            msgs = [{"id": i + 1, "t": 1_700_000_000 + i, "tail": f"4X-EH{i % 5}",
                     "label": "10", "text": f"live {i}", "category": "כללי",
                     "dir": "downlink"} for i in range(total)]
            state["sent"] = total
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"ok": True, "active": True,
                                       "freqs": ["131.550"], "cursor": state["sent"],
                                       "messages": msgs, "adsb": {}}))
    return handler, state


def _acars_mode_overrides(handler):
    return {"/api/acars": handler,
            "/api/state": {**_default_api()["/api/state"], "app_mode": "acars"},
            "/api/health": {"ok": True, "app_mode": "acars", "mode_ok": True,
                            "stats_age": None,
                            "services": {"airam-acars": "active", "sdrplay": "active",
                                         "icecast2": "active", "rtl_airband": "inactive",
                                         "airam-vdl2": "inactive",
                                         "airam-satcom": "inactive"}}}


def test_archive_round_trip_restores_live_session(page):
    """⚠ הקוד המורכב ביותר ב-UI: הכניסה לארכיון מחליפה את `msgs` בתוכן יום
    שלם מהדיסק, ו-exitArchive משחזר את הסשן החי מ-liveSnapshot.
    הסכנה הספציפית: `_rebuildFromMsgs` בונה craft מחדש מ-`msgs`, שהוא חלון
    נגלל (MAX=500) — סשן חי ארוך יותר היה מאבד את הצבירה של מטוסים שכבר נגזמו
    מהחלון. כאן: 600 הודעות חיות => ארכיון (3 הודעות) => חזרה, ומוודאים
    שהמונה המצטבר חזר ל-600 ולא ל-500 (או ל-3)."""
    live_total = 600
    handler, state = _acars_stream(live_total)
    _mount(page, overrides=_acars_mode_overrides(handler))
    page.click("#modeSeg button[data-v=acars]")
    expect(page.locator("#acarsStTotal")).to_have_text(str(live_total), timeout=15000)

    page.fill("#acarsArchiveDate", "2026-01-02")
    page.click("#acarsArchiveGo")
    expect(page.locator("#acarsArchiveLabel")).to_be_visible(timeout=15000)
    expect(page.locator("#acarsStTotal")).to_have_text("3", timeout=15000)

    page.click("#acarsArchiveLive")
    # חזרה לשידור חי: המונה המצטבר משוחזר במלואו, לא נגזר מחדש מחלון ה-500
    expect(page.locator("#acarsStTotal")).to_have_text(str(live_total), timeout=15000)
    expect(page.locator("#acarsArchiveLabel")).to_be_hidden()


def test_archive_stops_live_polling_while_open(page):
    """בזמן עיון בארכיון הפולינג החי נעצר — אחרת הודעות היום הנוכחי היו
    נדחפות לתוך תצוגת הארכיון בזמן שהתווית עדיין אומרת "מציג ארכיון"."""
    handler, state = _acars_stream(5)
    _mount(page, overrides=_acars_mode_overrides(handler))
    page.click("#modeSeg button[data-v=acars]")
    expect(page.locator("#acarsStTotal")).to_have_text("5", timeout=15000)

    page.fill("#acarsArchiveDate", "2026-01-02")
    page.click("#acarsArchiveGo")
    expect(page.locator("#acarsArchiveLabel")).to_be_visible(timeout=15000)
    polls_at_entry = state["live_polls"]

    # מעבר לתצוגה אחרת וחזרה — show() לא אמור לחדש polling כשארכיון פתוח
    page.click("#modeSeg button[data-v=home]")
    page.click("#modeSeg button[data-v=acars]")
    expect(page.locator("#acarsArchiveLabel")).to_be_visible()
    page.wait_for_timeout(4000)          # יותר ממחזור פולינג אחד (3ש')
    assert state["live_polls"] == polls_at_entry, (
        f"הפולינג החי המשיך בזמן ארכיון: {polls_at_entry} => {state['live_polls']}")
    # והמונה עדיין מציג את הארכיון, לא את הזרם החי
    expect(page.locator("#acarsStTotal")).to_have_text("3")


def test_data_view_instances_are_isolated(page):
    """שלושת מופעי createDataView (ACARS/VDL2/SATCOM) הם closures נפרדים —
    שום state לא משותף. הודעות שנכנסות ל-ACARS לא אמורות להופיע במוני VDL2."""
    handler, _ = _acars_stream(7)
    _mount(page, overrides=_acars_mode_overrides(handler))
    page.click("#modeSeg button[data-v=acars]")
    expect(page.locator("#acarsStTotal")).to_have_text("7", timeout=15000)
    page.click("#modeSeg button[data-v=vdl2]")
    expect(page.locator("#vdl2StTotal")).to_have_text("0")
    expect(page.locator("#satcomStTotal")).to_have_text("0")
