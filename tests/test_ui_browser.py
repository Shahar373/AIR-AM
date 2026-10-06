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
        "/api/sdr": {"ok": True, "state": "ours", "mode": "voice", "service_state": "active",
                     "usb": True, "usb_desc": "SDRplay RSP1B", "api": "active",
                     "label": None, "detail": None, "suspects": [], "checked_age": None},
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


def test_aim_audio_toggle_is_honest_about_wake_lock_and_stops_on_view_change(page):
    """כיוון-בשמיעה (SATCOM): המתג נדלק/נכבה, מעבר-תצוגה מכבה אותו — והרמז
    כן לגבי נעילת-המסך. ⚠ הדף מוגש כאן ב-http://airam.test (לא secure
    context), בדיוק כמו ברירת המחדל של AIR-AM (http://<IP>:8080): ב-HTTP
    ‏navigator.wakeLock לא קיים בכלל, ולכן הרמז *חייב* להודות שהמסך עלול
    לכבות — ולא להבטיח "המסך יישאר דולק" (§12: לא מבטיחים יכולת שלא נתפסה).
    זו גם בדיקת-ריצה אמיתית של Web Audio ב-Chromium: AudioContext נוצר
    מלחיצה (user gesture) ואסור שתיזרק חריגה (ה-fixture אוכף)."""
    satcom_state = {**_default_api()["/api/state"], "app_mode": "satcom"}
    health = {"ok": True, "app_mode": "satcom", "mode_ok": True, "stats_age": None,
              "services": {"airam-satcom": "active", "sdrplay": "active",
                           "rtl_airband": "inactive", "icecast2": "active",
                           "airam-acars": "inactive", "airam-vdl2": "inactive"}}
    _mount(page, overrides={
        "/api/state": satcom_state, "/api/health": health,
        "/api/satcom": {"ok": True, "active": True, "freqs": ["AF1"], "cursor": 0,
                        "messages": []},
        "/api/satcom/health": {"ok": True, "available": True, "spectrum": False,
                               "channels": [{"ch": 0, "baud": 600, "msgs": 0, "age": 0,
                                             "mse": 0.4, "ebno": 6.5, "lock": False}],
                               "channels_locked": 0, "channels_total": 1},
        "/api/satcom/spectrum": {"ok": True, "available": False},
    })
    page.click("#modeSeg button[data-v=satcom]")
    btn = page.locator("#satcomAimAudioBtn")
    expect(btn).to_be_visible()
    expect(btn).to_have_attribute("aria-pressed", "false")
    # http => אין wakeLock בכלל (secure-context בלבד) — מוודאים את הנחת הבדיקה
    assert page.evaluate("'wakeLock' in navigator") is False

    btn.click()
    expect(btn).to_have_attribute("aria-pressed", "true")
    expect(btn).to_have_class(re.compile(r"\bon\b"))
    hint = page.locator("#satcomAimAudioHint")
    expect(hint).to_contain_text("השאר את המסך דולק בעצמך")
    expect(hint).not_to_contain_text("המסך יישאר דולק")

    # מעבר-תצוגה (בית) חייב לכבות את האודיו — לא צליל ברקע אחרי שעזבנו את SATCOM
    page.click("#modeSeg button[data-v=home]")
    expect(btn).to_have_attribute("aria-pressed", "false")
    expect(btn).not_to_have_class(re.compile(r"\bon\b"))



def test_rflog_recorder_toggle_marks_and_download(page):
    """רשם ניסוי הכיול (תצוגת קול): הפעלה חושפת את כפתורי הסימון, סימון שולח
    את התווית המדויקת ומציג אישור גלוי, וקישור ההורדה מופיע כשיש הקלטה.
    בשטח אין דרך אחרת לדעת שלחיצה נקלטה — האישור הוא חלק מהפיצ'ר."""
    st = {"active": False, "rows": 0, "marks": 0, "size": 0, "until": None, "started_at": None}
    sent = {"toggle": [], "mark": []}

    def rflog(route, url):
        req = route.request
        if req.method == "POST":
            body = json.loads(req.post_data or "{}")
            sent["toggle"].append(body.get("active"))
            st["active"] = bool(body.get("active"))
            if st["active"]:
                import time as _t
                st.update(until=_t.time() + 7200, started_at=_t.time(), rows=3, size=512,
                          remaining=7200)
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"ok": True, **st}))

    def mark(route, url):
        body = json.loads(route.request.post_data or "{}")
        sent["mark"].append(body.get("label"))
        st["marks"] += 1
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"ok": True, "label": body.get("label"), **st}))

    _mount(page, overrides={"/api/rflog": rflog, "/api/rflog/mark": mark})
    page.click("#modeSeg button[data-v=voice]")
    btn = page.locator("#rflogBtn")
    expect(btn).to_have_attribute("aria-pressed", "false")
    expect(page.locator("#rflogMarks")).to_be_hidden()

    btn.click()
    expect(btn).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#rflogStatus")).to_contain_text("מקליט")
    expect(page.locator("#rflogStatus")).to_contain_text("נותרו 2:00:00")
    expect(page.locator("#rflogMarks")).to_be_visible()
    expect(page.locator("#rflogDl")).to_be_visible()

    page.click("#rflogMarks button[data-mark='מנותק']")
    expect(page.locator("#rflogHint")).to_contain_text("✓ סומן: מנותק")
    assert sent["mark"] == ["מנותק"]

    btn.click()
    expect(btn).to_have_attribute("aria-pressed", "false")
    expect(page.locator("#rflogMarks")).to_be_hidden()
    assert sent["toggle"] == [True, False]


def test_experiment_flow_prompt_confirm_and_results(page):
    """ניסוי הכיול האוטומטי מקצה לקצה בדפדפן: התחלה מהבית, סרגל גלובלי עם בקשת
    ניתוק (גלויה גם בתצוגה אחרת), אישור שנשלח לשרת, וטבלת סיכום בסוף. התוויות
    מכילות מירכאות (נתב"ג) — הטבלה נבנית ב-innerHTML, אז גם ה-escaping נבדק כאן."""
    phase = {"n": "idle"}
    sent = []
    result = {
        "threshold_db": 10.0,
        "conditions": [
            {"key": "clean_f20", "label": 'רווח קבוע · אין נתב"ג בחלון', "p1": {"steady": -60.0},
             "p2": {"steady": -72.0}, "drop": 12.0, "detects": True},
            {"key": "atiswin_agc", "label": "132.000 · ATIS בחלון · AGC", "p1": {"steady": -58.0},
             "p2": {"steady": -60.0}, "drop": 2.0, "detects": False}],
        "probes": [{"key": "probe_product", "label": "בדיקת האנטנה של המוצר · 130.450",
                    "freq": 130.45, "p1": {"noise": -61.0}, "p2": {"noise": -63.5},
                    "drop": 2.5, "detects": False}],
        "atis": {"drop": 53.0, "gone": True},
        "after_disconnect": {"instant": -70.0, "after": -61.0, "rise": 9.0},
        "stale_rows": 0, "stale_probes": 1, "lna": 4, "fm_notch": True,
    }

    def status():
        base = {"ok": True, "running": False, "steps_total": 22, "step_index": -1, "waiting": None,
                "step": None, "eta_sec": None, "eta_prompt_sec": None, "error": None, "result": None}
        if phase["n"] == "running":
            base.update(running=True, step_index=3, eta_sec=900, eta_prompt_sec=300,
                        step={"label": "122.600 · AGC"})
        elif phase["n"] == "waiting":
            base.update(running=True, step_index=8, eta_sec=700,
                        waiting={"action": "disconnect", "since": 1000.0})
        elif phase["n"] == "done":
            base.update(result=result)
        return base

    def exp(route, url):
        req = route.request
        if req.method == "POST":
            body = json.loads(req.post_data or "{}")
            sent.append(body.get("action"))
            phase["n"] = {"start": "running", "confirm": "done"}.get(body.get("action"), phase["n"])
        route.fulfill(status=200, content_type="application/json", body=json.dumps(status()))

    _mount(page, overrides={"/api/experiment": exp})
    page.evaluate("window.confirm = () => true")
    page.click("#modeSeg button[data-v=home]")
    page.click("#expStartBtn")
    expect(page.locator("#expBar")).to_be_visible()
    expect(page.locator("#expBarSub")).to_contain_text("הבקשה הבאה בעוד ~5 דק'")

    # הבקשה הפיזית חייבת להופיע גם כשהמשתמש בתצוגה אחרת (הסרגל גלובלי)
    page.click("#modeSeg button[data-v=voice]")
    phase["n"] = "waiting"
    expect(page.locator("#expPrompt")).to_be_visible(timeout=6000)
    expect(page.locator("#expPromptTxt")).to_contain_text("נתק עכשיו")
    expect(page.locator("#expConfirmBtn")).to_have_text("✓ ניתקתי")

    page.click("#expConfirmBtn")
    expect(page.locator("#expBar")).to_be_hidden()
    assert sent == ["start", "confirm"]

    page.click("#modeSeg button[data-v=home]")
    res = page.locator("#expResult")
    expect(res).to_be_visible()
    expect(res.locator(".exp-head")).to_contain_text("לא היה מזהה")
    expect(res.locator("table")).to_contain_text('אין נתב"ג בחלון')     # escaping תקין, לא שבור
    expect(res.locator(".exp-notes")).to_contain_text("קראו נתונים של התהליך הקודם")
    # הקצה הקדמי של הריצה (LNA state 4 => 5/9) — ריצות בקצה-קדמי שונה אינן ברות-השוואה
    notes = res.locator(".exp-notes")
    expect(notes).to_contain_text("קצה קדמי בריצה")
    expect(notes).to_contain_text("LNA 5/9")
    expect(notes).to_contain_text("· מסנן FM")
    expect(page.locator("#expPillTxt")).to_have_text("הושלם")


def test_sdr_chip_reports_detected_busy_and_free(page):
    """חיווי ה-SDR: "תפוס ע״י תוכנה אחרת" עם החשודים לפי שם, ומעבר ל"פנוי".
    שמות התהליכים מגיעים מ-/proc — ודא שהם מוצגים כטקסט, לא כ-HTML."""
    sdr = {"ok": True, "state": "busy", "mode": None, "service_state": None, "usb": True,
           "usb_desc": "SDRplay RSP1B", "api": "active", "label": None, "detail": None,
           "suspects": [{"pid": 4242, "name": "<b>sdrpp</b>"}], "checked_age": 3.0}

    def handler(route, url):
        route.fulfill(status=200, content_type="application/json", body=json.dumps(sdr))

    _mount(page, overrides={"/api/sdr": handler})
    chip = page.locator("#sdrChip")
    expect(chip).to_have_text("SDR תפוס ע״י תוכנה אחרת")
    expect(chip).to_have_class(re.compile(r"\berr\b"))
    chip.click()
    expect(page.locator("#toastMsg")).to_contain_text("<b>sdrpp</b> (PID 4242)")
    page.click("#modeSeg button[data-v=home]")
    expect(page.locator("#homeSdrTxt")).to_contain_text("תוכנה אחרת מחזיקה בו")

    sdr.update(state="free", suspects=[], label="SDRplay Dev0 RSP1B 2305012345")
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    expect(chip).to_have_text("SDR מזוהה · פנוי")
    expect(chip).to_have_class(re.compile(r"\bok\b"))
    expect(page.locator("#homeSdrTxt")).to_contain_text("2305012345")

    sdr.update(state="missing", usb=False, usb_desc=None, label=None)
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    expect(chip).to_have_text("SDR לא מזוהה")


# --- PR 1 (v2.26.0): שליטה ב-RF + חיווי עומס מהחומרה ---------------------------
# ‏360px — רוחב הטלפון הצר שבו היו רגרסיות גלישה בעבר (ר' הערת .act-row ב-CSS).

def _phone(page):
    page.set_viewport_size({"width": 360, "height": 800})


def _no_hscroll(page):
    w = page.evaluate("document.documentElement.scrollWidth")
    assert w <= 360, f"גלילה אופקית ב-360px: scrollWidth={w}"


def test_lna_slider_enabled_under_agc_and_fm_notch_from_state(page):
    """⚠ הממצא שהוביל ל-PR 1: הסליידר הושבת תחת AGC (`rfGain.disabled = auto`),
    כך שהצירוף הנכון ליד שדה תעופה — AGC על ה-IF + LNA מופחת — היה חסום מהממשק.
    כאן: AGC דלוק => IF מושבת אבל LNA פעיל; fm_notch מאותחל מ-/api/state בטעינה;
    ושחרור הסליידר שולח rf_gain + fm_notch גם כש-agc דלוק."""
    _phone(page)
    sent = []

    def tune(route, url):
        sent.append(json.loads(route.request.post_data or "{}"))
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"ok": True}))

    st = {**_default_api()["/api/state"], "agc": True, "rf_gain": 6, "fm_notch": True}
    _mount(page, overrides={"/api/state": st, "/api/tune": tune})
    page.click("#modeSeg button[data-v=voice]")
    expect(page.locator("#fmNotch")).to_be_checked()
    expect(page.locator("#rfGain")).to_be_enabled()
    expect(page.locator("#ifGain")).to_be_disabled()
    expect(page.locator("#rfGainVal")).to_have_text("3/9")       # 9 − LNA state 6
    _no_hscroll(page)

    # שחרור הסליידר (change) => retune. state 6 => סליידר 3; מזיזים ל-2 => state 7.
    page.evaluate("""() => { const r = document.getElementById('rfGain');
                             r.value = 2; r.dispatchEvent(new Event('input'));
                             r.dispatchEvent(new Event('change')); }""")
    expect(page.locator("#rfGainVal")).to_have_text("2/9")
    expect(page.locator("#status")).not_to_contain_text("מכוונן…", timeout=10000)
    assert sent, "שחרור סליידר ה-LNA לא שלח /api/tune"
    body = sent[-1]
    assert body["agc"] is True
    assert body["rf_gain"] == 7, body
    assert body["fm_notch"] is True, body


def test_fm_notch_unchecked_when_state_lacks_it(page):
    """state ישן (לפני v2.26.0) בלי fm_notch => המתג כבוי (ולא "מאותחל" — כיוונון לא
    ישלח fm_notch, והשרת ישמור את הערך השמור; ר' fmNotchField)."""
    _phone(page)
    _mount(page)
    page.click("#modeSeg button[data-v=voice]")
    expect(page.locator("#rfGain")).to_be_enabled()
    expect(page.locator("#fmNotch")).not_to_be_checked()


def test_rf_overload_from_hardware_and_unknown_states(page):
    """חיווי העומס מגיע מ-rf (אירועי החומרה), לא מכלל ה-‎-3dBFS הישן.
    §12: telemetry=false => "לא זמין" + פקודת התקנה; overload=null => "לא ידוע";
    בשני המקרים הצ'יפ האדום מוסתר אבל אין שום טענת "תקין"."""
    _phone(page)
    rf = {"telemetry": True, "overload": True, "overload_events": 3,
          "last_overload_age": 4.2, "ifgr": 43, "lna_grdb": 24, "lna_state": 4,
          "fm_notch": False, "agc": True}
    metrics = {"ok": True, "snr": 20.0, "signal": -30.0, "noise": -50.0, "fresh": True,
               "overload": True, "rf": rf}

    def handler(route, url):
        route.fulfill(status=200, content_type="application/json", body=json.dumps(metrics))

    _mount(page, overrides={"/api/metrics": handler})
    page.click("#modeSeg button[data-v=voice]")
    chip, line = page.locator("#overload"), page.locator("#rfHw")
    expect(chip).to_be_visible()
    expect(chip).to_contain_text("עומס RF")
    expect(line).to_contain_text("עומס RF עכשיו")
    expect(line).to_contain_text("IFGR 43 dB")
    expect(line).to_contain_text("LNA 5/9")
    expect(line).to_contain_text("עומסים: 3")
    expect(line).to_have_class(re.compile(r"\bbad\b"))
    _no_hscroll(page)

    rf.update(overload=False, overload_events=0, last_overload_age=None)
    expect(chip).to_be_hidden()
    expect(line).to_contain_text("אין (לפי החומרה)")
    expect(line).to_have_class(re.compile(r"\bok\b"))

    # טלמטריה לא זמינה (הדרייבר בלי ה-patch): ניטרלי, עם הוראת התקנה
    rf.update(telemetry=False, overload=None, ifgr=None, lna_grdb=None)
    expect(line).to_contain_text("לא זמין")
    expect(line.locator("code")).to_have_text("sudo ./install.sh")
    expect(chip).to_be_hidden()
    expect(line).not_to_have_class(re.compile(r"\bok\b"))

    # טלמטריה קיימת אבל אין קריאה (הקול לא רץ / העוקב לא קורא) — לא ידוע, לא תקין
    rf.update(telemetry=True, overload=None)
    expect(line).to_contain_text("לא ידוע")
    expect(line).not_to_contain_text("עומסים")
    expect(line).not_to_have_class(re.compile(r"\bok\b"))

    # ⚠ השדה העליון הישן overload=True בלי rf (שרת מלפני v2.26.0, כלל ה-‎-3dBFS)
    # לא מדליק חיווי — אין לו בסיס בחומרה.
    metrics.pop("rf")
    expect(line).to_have_text("עומס RF: לא ידוע")
    expect(chip).to_be_hidden()


def test_activity_rows_show_rf_meta_at_360px(page):
    """מטא RF לכל שורה מה-sidecar: ⚠ עומס ×N כשהחומרה דיווחה, "עומס: ?" כשלא
    ידוע, ושום דבר כשאין sidecar בכלל. וה-LNA *רק* כשהקונפיג בזמן ההקלטה ידוע."""
    _phone(page)
    base = {"freq": 132.5, "dur": 4.0, "exists": True, "starred": False,
            "tx": {"state": "none"}}
    events = [
        {**base, "ts": 1_700_000_300, "file": "a.mp3",
         "rf": {"config_known": True, "lna_state": 4, "agc": True, "fm_notch": True,
                "telemetry": True, "overload": True, "overload_events": 3,
                "ifgr_min": 40, "ifgr_max": 48}},
        {**base, "ts": 1_700_000_200, "file": "b.mp3",
         "rf": {"config_known": False, "lna_state": None, "agc": None,
                "telemetry": False, "overload": None, "overload_events": None}},
        {**base, "ts": 1_700_000_100, "file": "c.mp3", "rf": None},
    ]
    _mount(page, overrides={"/api/activity": {"ok": True, "events": events}})
    page.click("#modeSeg button[data-v=voice]")
    items = page.locator("#activity .act-item")
    expect(items).to_have_count(3)
    first = items.nth(0).locator(".act-rf")
    expect(first).to_contain_text("LNA 5/9")
    expect(first).to_contain_text("מסנן FM")
    expect(first).to_contain_text("⚠ עומס RF ×3")
    expect(first).to_contain_text("IFGR 40–48")
    second = items.nth(1).locator(".act-rf")
    expect(second).to_contain_text("עומס: ?")
    expect(second).not_to_contain_text("LNA")         # config_known=false => לא מנחשים
    expect(items.nth(2).locator(".act-rf")).to_have_count(0)
    _no_hscroll(page)


def test_voice_field_meter_explains_baseline_config_mismatch(page):
    """בסיס שנמדד ב-LNA/מסנן אחרים => no_baseline עם סיבה. הממשק חייב להסביר
    *למה* (כייל מחדש), לא להציג "אין בסיס" כאילו מעולם לא כוילו."""
    _phone(page)
    sig = {"ok": True, "mode": "voice", "kind": "continuous", "fresh": True,
           "signal": -30.0, "noise": -50.0, "snr": 20.0,
           "baseline": {"noise": -52.0, "lna": 0, "fm_notch": False},
           "verdict": "no_baseline", "verdict_reason": "baseline_config_mismatch"}
    _mount(page, overrides={"/api/signal": sig})
    page.click("#modeSeg button[data-v=voice]")
    expect(page.locator("#voiceFmVerdict")).to_contain_text("הגדרת LNA/מסנן FM אחרת")
    expect(page.locator("#voiceFmBaseline")).to_contain_text("LNA 9/9")


# --- תיקוני ביקורת PR 1 -------------------------------------------------------

def test_gain_sliders_right_means_more_gain(page):
    """⚠ blocker מהביקורת: בדף dir=rtl טווח יורש rtl, כך ש*שמאלה* היה ערך 9 = רווח
    מרבי — והוראת העומס "הזז LNA שמאלה" *העלתה* רווח. נגיעה בקצה הימני חייבת לתת
    את הערך המרבי (יותר רווח), בקצה השמאלי — 0, בכל סליידרי הרווח."""
    _phone(page)
    st = {**_default_api()["/api/state"], "agc": False}          # IF פעיל רק ברווח ידני
    _mount(page, overrides={"/api/state": st,
                            "/api/tune": {"ok": True}})
    page.click("#modeSeg button[data-v=voice]")
    for sid, top in (("#rfGain", "9"), ("#ifGain", "39")):
        el = page.locator(sid)
        expect(el).to_be_enabled()
        box = el.bounding_box()
        el.click(position={"x": box["width"] - 2, "y": box["height"] / 2})
        assert el.input_value() == top, (sid, "ימין")
        el.click(position={"x": 2, "y": box["height"] / 2})
        assert el.input_value() == "0", (sid, "שמאל")
    assert page.evaluate("getComputedStyle(document.getElementById('satcomGain')).direction") == "ltr"


def test_overload_advice_is_direction_free_and_unknown_reason_is_specific(page):
    """הוראת העומס לא תלויה בכיוון ("הורד", לא "שמאלה"); "לא ידוע" אומר *למה*
    (unknown_reason) ולא "ממתין לדרייבר" גנרי; gRdB מחוץ ל-20..59 לא מוצג כ"בחירת ה-AGC"."""
    _phone(page)
    rf = {"telemetry": True, "overload": True, "overload_events": 1, "last_overload_age": 2.0,
          "ifgr": 43, "lna_grdb": 24, "lna_state": 4, "fm_notch": False, "agc": True,
          "unknown_reason": None}
    metrics = {"ok": True, "snr": 20.0, "signal": -30.0, "noise": -50.0, "fresh": True,
               "overload": True, "rf": rf}
    _mount(page, overrides={"/api/metrics": lambda route, url: route.fulfill(
        status=200, content_type="application/json", body=json.dumps(metrics))})
    page.click("#modeSeg button[data-v=voice]")
    line = page.locator("#rfHw")
    expect(line).to_contain_text("הורד את ה-LNA")
    assert "שמאלה" not in line.inner_text()
    assert "שמאלה" not in (page.locator("#overload").get_attribute("title") or "")
    rf.update(overload=None, unknown_reason="joined_mid_session")
    expect(line).to_contain_text("השרת הופעל מחדש באמצע הסשן")
    expect(line).not_to_have_class(re.compile(r"\bok\b"))
    rf.update(unknown_reason="no_driver_evidence", ifgr=250)
    expect(line).to_contain_text("הדרייבר עוד לא אישר")
    expect(line).to_contain_text("לא מאומת")
    expect(line).not_to_contain_text("ה-AGC בחר")


def test_failed_tune_resyncs_fm_toggle_from_server_state(page):
    """כיוונון שנכשל (השרת חזר לקונפיג הקודם) => המתג חוזר למה שהשרת אומר, לא נשאר
    "דלוק" בזמן ש-#rfHw (מה-state) אומר "כבוי"."""
    _phone(page)
    back = {**_default_api()["/api/state"], "fm_notch": False, "rf_gain": 4}

    def tune(route, url):
        route.fulfill(status=500, content_type="application/json",
                      body=json.dumps({"ok": False, "error": "x", "state": back}))
    _mount(page, overrides={"/api/tune": tune})
    page.click("#modeSeg button[data-v=voice]")
    expect(page.locator("#fmNotch")).not_to_be_checked()
    page.click("label[for=fmNotch]")
    expect(page.locator("#status")).to_contain_text("שגיאה")
    expect(page.locator("#fmNotch")).not_to_be_checked()
    expect(page.locator("#rfGainVal")).to_have_text("5/9")


def test_tune_omits_fm_notch_when_state_never_loaded(page):
    """/api/state נכשל בטעינה => המתג לא אותחל => לא שולחים fm_notch (השרת שומר את
    הערך השמור) במקום לכבות בשקט מסנן שהמשתמש הדליק."""
    _phone(page)
    sent = []

    def tune(route, url):
        sent.append(json.loads(route.request.post_data or "{}"))
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True}))

    def state_fail(route, url):
        route.abort()                                   # כשל רשת — הדף נשאר על ברירות המחדל
    _mount(page, overrides={"/api/state": state_fail, "/api/tune": tune})
    page.click("#modeSeg button[data-v=voice]")
    page.evaluate("""() => { const r = document.getElementById('rfGain');
                             r.value = 3; r.dispatchEvent(new Event('change')); }""")
    expect(page.locator("#status")).not_to_contain_text("מכוונן…", timeout=10000)
    assert sent and "fm_notch" not in sent[-1], sent


# --- PR 2 (v2.27.0): 🩺 בדיקת RF (GET/POST /api/rfcheck) --------------------------
# ה-API ממוקף לפי המפרט (§6.5–6.8) וסכמת התוצאה של rfcheck_analysis.analyze. כל הבדיקות
# ב-360px (הטלפון הצר): בלי גלילה אופקית, כפתורים ≥44px ("החל" 52px).

_RFC_STATES = [0, 2, 4, 6, 7, 8]
_RFC_GR = {0: 0, 2: 12, 4: 20, 6: 32, 7: 38, 8: 57}


def _rfc_status(**kw):
    st = {"ok": True, "available": True, "reasons": [], "install_hint": "sudo ./install.sh",
          "telemetry_expected": True, "experimental": True, "running": False, "run_id": None,
          "phase": None, "ref": None, "freq": None, "elapsed": 0, "soft_max": 180,
          "hard_max": 180, "can_extend": False, "states": [], "current_state": None,
          "last_slot": None, "tx_captured": 0, "tx_target": 3, "full_blocks": 0,
          "blocks_target": 12, "no_traffic_sec": None, "offer_atis": False, "result": None,
          "diagnose_available": False, "error": None, "restore": None}
    st.update(kw)
    return st


def _rfc_running(slot="silence", tx=0, blocks=0, elapsed=12, offer=False, ncar=0, **kw):
    states = [{"lna": s, "label": f"{9 - s}/9", "gr_db": _RFC_GR[s], "ifgr": 40,
               "n_carrier": ncar, "n_silence": 5, "overload": None, "clip": "not_observed",
               "current": s == 4, "production": s == 0} for s in _RFC_STATES]
    base = dict(running=True, run_id="r1", phase="listening", ref="tower", freq=132.5,
                elapsed=elapsed, states=states, current_state=4, last_slot=slot,
                tx_captured=tx, full_blocks=blocks, offer_atis=offer,
                n_carrier_rows=ncar, progress=min(tx / 3, blocks / 12))
    base.update(kw)
    return _rfc_status(**base)


def _rfc_result(**kw):
    per_state = [{"lna": s, "label": f"{9 - s}/9", "gr_db": _RFC_GR[s], "ifgr_start": 40,
                  "ifgr_final": 40, "clamped": None, "ratchets": 0, "valid": 20, "invalid": 0,
                  "n_carrier": 12, "n_silence": 8, "n_edge": 0, "noise_ref": "silence",
                  "noise_dbfs": -60.0, "cnr_median": 30.0, "cnr_p25": 29.0, "cnr_p75": 31.0,
                  "op_clip": 0, "op_ovl": None, "lost": 0, "probe_clip": 0, "probe_ovl": None,
                  "env_ovl_silence": None, "overload": None, "clip": "not_observed",
                  "flags": ["current"] if s == 0 else []} for s in _RFC_STATES]
    r = {"v": 1, "run_id": "r1", "phase": "lna", "ref": "tower", "freq": 132.5,
         "atis_freq": None, "started_at": 1_790_000_000, "ended_at": 1_790_000_090,
         "ended": "stopped", "config_at_start": {"freq": 132.5, "mod": "am", "agc": True,
                                                  "if_gain": 40, "rf_gain": 0, "fm_notch": False},
         "states": _RFC_STATES, "current_state": 0, "level": "stat",
         "headline": "reduce_gain_tie", "headline_params": {"x": 0, "y": 6},
         "recommendation": {"rf_gain": 6, "if_gain": None, "fm_notch": None, "basis": "stat",
                            "reasons": [], "changes": True},
         "indication": None, "refine_suggestion": None, "apply_allowed": True,
         "evidence": [{"code": "tie", "states": [0, 2, 4, 6]}],
         "per_state": per_state, "comparisons": [], "n": 12, "best": 4, "tie_set": [0, 2, 4, 6],
         "transmissions": 3, "tx_captured": 3, "full_blocks": 12, "cycles": 30,
         "selfcheck": {"telemetry": "absent", "overload_semantics_verified": False,
                       "settle": {"suspect": False, "p": None, "n": 0, "median_db": None},
                       "guard_buffers": 1, "guard_verified": False, "overflows": 0,
                       "gr_timeouts": 0, "invalid_frac": 0.0, "proc_ms_p50": 20.0,
                       "proc_ms_p95": 30.0, "light_mode": False, "rate_measured": 2560000,
                       "rail_code": 32767, "rail_source": "driver_source", "peak_code_max": 9000,
                       "bias_t_forced_off": False, "notch_readback_ok": True,
                       "proxy_check": None, "hw": "RSP1B", "verified": False},
         "untestable": [{"code": "soft_compression_tower"}, {"code": "settle_unverified"},
                        {"code": "telemetry_absent", "status": "absent"}],
         "restore": {"ok": True, "error": None}}
    r.update(kw)
    return r


def _rfc_mount(page, holder, sent, extra=None):
    """‏holder["st"] = תשובת GET הנוכחית (הבדיקה מחליפה אותה בין שלבים); POST נרשם ל-sent
    ומוחזר דרך holder["post"](body) אם הוגדר, אחרת ok + הסטטוס."""
    def rfcheck(route, url):
        req = route.request
        if req.method == "POST":
            body = json.loads(req.post_data or "{}")
            sent.append(body)
            resp = holder.get("post", lambda b: {"ok": True, **holder["st"]})(body)
            route.fulfill(status=200 if resp.get("ok") else 409,
                          content_type="application/json", body=json.dumps(resp))
            return
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(holder["st"]))
    ov = {"/api/rfcheck": rfcheck}
    ov.update(extra or {})
    _mount(page, overrides=ov)
    page.click("#modeSeg button[data-v=voice]")


def _rfc_refresh(page):
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")


def test_rfcheck_unavailable_keeps_old_calibrate_button(page):
    """לא מותקן (python3-soapysdr חסר) ⇒ אין 🩺, הסיבה ופקודת ההתקנה גלויות, והכיול הישן
    נשאר בצורתו הרגילה (קריטריון קבלה 8)."""
    _phone(page)
    holder = {"st": _rfc_status(available=False, reasons=["soapysdr_missing"], experimental=True)}
    _rfc_mount(page, holder, [])
    expect(page.locator("#rfcUnavail")).to_be_visible()
    expect(page.locator("#rfcUnavail")).to_contain_text("python3-soapysdr")
    expect(page.locator("#rfcUnavail")).to_contain_text("sudo ./install.sh")
    expect(page.locator("#rfcStart")).to_be_hidden()
    expect(page.locator("#rfcBadge")).to_be_hidden()
    cal = page.locator("#voiceFmCalBtn")
    expect(cal).to_be_visible()
    expect(cal).to_have_class(re.compile(r"\bghost\b"))
    expect(cal).to_contain_text("כייל בסיס עכשיו")
    _no_hscroll(page)


def test_rfcheck_primary_button_keeps_baseline_link_and_experimental_list(page):
    """החלטת משתמש 2: 🩺 הוא הכפתור הראשי, אבל "📏 כייל בסיס" נשאר כקישור משני קטן
    ושורת הבסיס/פסק-הדין לא מוסתרת — והקישור עדיין מכייל. "🧪 ניסיוני" + רשימת מה שטרם
    אומת מוצגים כל עוד experimental. ואין כפתור "הארך" (החלטת משתמש 1)."""
    _phone(page)
    cal_sent = []

    def check(route, url):
        cal_sent.append(json.loads(route.request.post_data or "{}"))
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"ok": True, "noise": -55.0, "verdict": "ok",
                                       "baseline": {"noise": -55.0, "lna": 0, "fm_notch": False}}))
    holder = {"st": _rfc_status()}
    _rfc_mount(page, holder, [], extra={"/api/antenna/check": check})
    start = page.locator("#rfcStart")
    expect(start).to_be_visible()
    assert start.bounding_box()["height"] >= 52
    cal = page.locator("#voiceFmCalBtn")
    expect(cal).to_be_visible()
    expect(cal).to_have_class(re.compile(r"\bfm-link\b"))
    expect(cal).to_have_text("📏 כייל בסיס")
    expect(page.locator("#voiceFmBaseline")).to_be_visible()
    expect(page.locator("#rfcBadge")).to_be_visible()
    page.click("#rfcExperimental > summary")
    expect(page.locator("#rfcExpList li")).to_have_count(6)
    expect(page.locator("#rfcExpList")).to_contain_text("AGC כבוי")
    assert "עוד דקה" not in page.inner_text("body")       # טקסט גלוי, לא הערות בקוד
    cal.click()
    expect(page.locator("#voiceFmBaseline")).to_contain_text("-55.0")
    assert cal_sent and cal_sent[-1]["calibrate"] is True
    _no_hscroll(page)


def test_rfcheck_start_progress_silence_carrier_and_audio_resume(page):
    """מקצה לקצה: דף אישור ("עד 3 דקות", מספר המצבים) ⇒ start ⇒ השמע נעצר ⇒ התקדמות
    בשקט ("ממשיכים עד שיש מספיק נתונים", מונים, צ'יפ לכל מצב עם תווית הסליידר) ⇒ נשא
    (שורת מצב + רטט) ⇒ סיום עם שחזור ⇒ השמע חוזר כי ניגן לפני (מפרט §7.4)."""
    _phone(page)
    sent = []
    holder = {"st": _rfc_status()}
    holder["post"] = lambda b: ({"ok": True, **_rfc_running()} if b.get("action") == "start"
                                else {"ok": True, **holder["st"]})
    _rfc_mount(page, holder, sent)
    expect(page.locator("#rfcStart")).to_be_visible()
    page.evaluate("""() => {
        const p = document.getElementById('player');
        window.__paused = false; window.__pauses = 0; window.__plays = 0; window.__vib = 0;
        Object.defineProperty(p, 'paused', {get: () => window.__paused, configurable: true});
        p.pause = () => { window.__paused = true; window.__pauses++; };
        p.play = () => { window.__plays++; window.__paused = false; return Promise.resolve(); };
        p.load = () => {};
        navigator.vibrate = () => { window.__vib++; return true; };
    }""")
    page.click("#rfcStart")
    sheet = page.locator("#rfcConfirm")
    expect(sheet).to_be_visible()
    expect(sheet).to_contain_text("עד 3 דקות")
    expect(sheet).to_contain_text("6")                     # {0,2,4,6,7,8} ∪ {0} = 6 מצבים
    expect(sheet).to_contain_text("🧪 ניסיוני")
    assert page.locator("#rfcConfirmOk").bounding_box()["height"] >= 44
    _no_hscroll(page)
    holder["st"] = _rfc_running()
    page.click("#rfcConfirmOk")
    expect(page.locator("#rfcProgress")).to_be_visible()
    assert sent[0] == {"action": "start", "phase": "lna"}, sent
    assert page.evaluate("window.__pauses") >= 1
    expect(page.locator("#rfcUntil")).to_have_text("ממשיכים עד שיש מספיק נתונים (עד 3 דקות).")
    expect(page.locator("#rfcCounters")).to_contain_text("שידורים:")
    expect(page.locator("#rfcCounters")).to_contain_text("0/3")
    expect(page.locator("#rfcCounters")).to_contain_text("0/12")
    expect(page.locator("#rfcChips .rfc-chip")).to_have_count(6)
    expect(page.locator("#rfcChips .rfc-chip").first).to_contain_text("9/9")   # LNA state 0
    expect(page.locator("#rfcChips .rfc-chip.cur")).to_contain_text("5/9")     # state 4 נמדד עכשיו
    expect(page.locator("#rfcStatus")).to_contain_text("שקט")
    expect(page.locator("#rfcPlayerNote")).to_contain_text("מושהה לבדיקת RF")
    expect(page.locator("#rfcTime")).to_contain_text("3:00")
    expect(page.locator("#rfcBar")).to_be_visible()
    expect(page.locator("#rfcStart")).to_be_hidden()
    _no_hscroll(page)

    holder["st"] = _rfc_running(slot="carrier", tx=1, blocks=4, ncar=4)
    expect(page.locator("#rfcStatus")).to_contain_text("📡 שידור!", timeout=5000)
    expect(page.locator("#rfcCounters")).to_contain_text("1/3")
    assert page.evaluate("window.__vib") >= 1

    holder["st"] = _rfc_status(run_id="r1", result=_rfc_result(), restore={"ok": True, "error": None})
    expect(page.locator("#rfcResult")).to_be_visible(timeout=5000)
    expect(page.locator("#rfcProgress")).to_be_hidden()
    expect(page.locator("#rfcBar")).to_be_hidden()
    expect(page.locator("#rfcMsg")).to_contain_text("✓ השמע חזר")
    page.wait_for_function("window.__plays >= 1", timeout=5000)


def test_rfcheck_no_traffic_offers_atis(page):
    """30 שניות בלי שידור ⇒ הצעה (לעולם לא אוטומטית) לעבור ל-ATIS ‏132.5; "עבור" שולח atis,
    "המשך לחכות" מסתיר את ההצעה לריצה הזאת."""
    _phone(page)
    sent = []
    holder = {"st": _rfc_running(elapsed=31, offer=True)}
    _rfc_mount(page, holder, sent)
    offer = page.locator("#rfcAtisOffer")
    expect(offer).to_be_visible()
    expect(offer).to_contain_text("132.500")
    expect(offer).to_contain_text("30 שניות")
    _no_hscroll(page)
    page.click("#rfcAtisNo")
    expect(offer).to_be_hidden()
    # רינדור נוסף של אותה ריצה (הזמן מוכיח שהגיע) — ההצעה שנדחתה לא חוזרת. בלי sleep.
    holder["st"] = _rfc_running(elapsed=33, offer=True)
    expect(page.locator("#rfcTime")).to_contain_text("0:33", timeout=5000)
    expect(offer).to_be_hidden()
    # ריצה אחרת ⇒ הצעה חדשה; "עבור" שולח atis, והשרת עובר לשלב ATIS (בלי הצעה)
    holder["st"] = _rfc_running(elapsed=40, offer=True, run_id="r2")
    expect(offer).to_be_visible(timeout=5000)

    def post(b):
        holder["st"] = _rfc_running(elapsed=41, phase="atis", ref="atis", run_id="r3")
        return {"ok": True, **holder["st"]}
    holder["post"] = post
    page.click("#rfcAtisYes")
    expect(page.locator("#rfcPhase")).to_contain_text("ATIS", timeout=5000)
    expect(page.locator("#rfcUntil")).to_contain_text("20")
    expect(offer).to_be_hidden()
    assert {"action": "atis"} in sent, sent


def test_rfcheck_global_bar_visible_in_home_and_finishes(page):
    """בזמן ריצה הסרגל הגלובלי מופיע בכל תצוגה (גם בבית), ו-"⏹ סיים" שולח finish."""
    _phone(page)
    sent = []
    holder = {"st": _rfc_running(elapsed=65)}
    _rfc_mount(page, holder, sent)
    page.click("#modeSeg button[data-v=home]")
    bar = page.locator("#rfcBar")
    expect(bar).to_be_visible()
    expect(page.locator("#rfcBarTitle")).to_contain_text("1:05")
    def post(b):
        holder["st"] = _rfc_running(elapsed=66, phase="finishing")
        return {"ok": True, **holder["st"]}
    holder["post"] = post
    page.click("#rfcBarFinish")
    expect(page.locator("#rfcBarTitle")).to_contain_text("1:06", timeout=5000)
    assert {"action": "finish"} in sent, sent
    _no_hscroll(page)


def test_rfcheck_indication_shows_direction_without_apply(page):
    """אינדיקציה ⇒ "כיוון אפשרי" בלי כפתור "החל" (החלטת משתמש 5); הסליידר נשאר להחלה ידנית.
    "🔁 השווה מסנן FM" מוצע לפי דרישה בלבד."""
    _phone(page)
    res = _rfc_result(level="indication", headline="indication",
                      headline_params={"n": 3, "y": 6}, recommendation=None,
                      indication={"rf_gain": 6, "best": 4, "states": [4, 6], "n": 3, "reasons": []},
                      apply_allowed=False, n=3,
                      untestable=[{"code": "soft_compression_tower"}, {"code": "few_blocks"}])
    holder = {"st": _rfc_status(run_id="r1", result=res)}
    _rfc_mount(page, holder, [])
    box = page.locator("#rfcResult")
    expect(box).to_be_visible()
    expect(box).to_contain_text("אינדיקציה בלבד")
    expect(box.locator(".rfc-dir")).to_contain_text("כיוון אפשרי")
    expect(box.locator(".rfc-dir")).to_contain_text("LNA 3/9")
    expect(page.locator("#rfcApply")).to_have_count(0)
    expect(box).to_contain_text("דחיסה רכה")
    expect(page.locator("#rfcNotchBtn")).to_be_visible()
    _no_hscroll(page)


def test_rfcheck_stat_result_apply_and_unknown_overload(page):
    """רמת stat ⇒ כפתור "החל LNA y/9" (52px) שולח apply עם run_id ומיישר את הסליידר מהתשובה.
    עמודת העומס בלי טלמטריה ⇒ "לא נבדק" (לעולם לא "לא נצפה"/ירוק); "מה לא נבדק" כולל
    תמיד דחיסה רכה על המגדל."""
    _phone(page)
    sent = []
    holder = {"st": _rfc_status(run_id="r1", result=_rfc_result())}
    state_after = {**_default_api()["/api/state"], "rf_gain": 6, "fm_notch": False}
    holder["post"] = lambda b: {"ok": True, **state_after}
    _rfc_mount(page, holder, sent)
    box = page.locator("#rfcResult")
    expect(box).to_be_visible()
    expect(box.locator(".rfc-lvl")).to_contain_text("השוואה מובהקת")
    expect(box.locator(".rfc-head")).to_contain_text("LNA 3/9")
    expect(box).to_contain_text("דחיסה רכה (עיוות הדיבור) לא נבדקת על שידורי המגדל")
    expect(box).to_contain_text("חיבור האנטנה לא נבדק")
    box.locator("summary", has_text="טבלת מצבי ה-LNA").click()
    expect(box.locator(".rfc-tbl")).to_contain_text("לא נבדק")
    apply = page.locator("#rfcApply")
    expect(apply).to_be_visible()
    expect(apply).to_contain_text("החל")
    expect(apply).to_contain_text("LNA 3/9")
    assert apply.bounding_box()["height"] >= 52
    _no_hscroll(page)
    apply.click()
    expect(page.locator("#rfcMsg")).to_contain_text("✓ הוחל")
    assert sent[-1] == {"action": "apply", "run_id": "r1"}, sent
    expect(page.locator("#rfGainVal")).to_have_text("3/9")


def test_rfcheck_recommendation_already_in_effect_hides_apply(page):
    """אחרי reload הדף לא זוכר שההמלצה כבר הוחלה — ההגדרה החיה (rf_gain=6) היא שמכריעה: בלי
    "החל" (השרת היה מסרב ב-409 "כבר בתוקף"), עם "✓ ההמלצה בתוקף". וגם: IFGR הוא *הנחתה* —
    ‏ifgr_clamped_low = רווח כולל *נמוך* מהנוכחי (הטקסט היה הפוך בגרסת הפיתוח)."""
    _phone(page)
    res = _rfc_result(untestable=[{"code": "soft_compression_tower"},
                                  {"code": "ifgr_clamped_low", "states": [8]},
                                  {"code": "ifgr_clamped_high", "states": [0]}])
    holder = {"st": _rfc_status(run_id="r1", result=res)}
    st = {**_default_api()["/api/state"], "rf_gain": 6, "agc": True, "fm_notch": False}
    _rfc_mount(page, holder, [], extra={"/api/state": st})
    box = page.locator("#rfcResult")
    expect(box).to_be_visible()
    expect(box).to_contain_text("✓ ההמלצה בתוקף")
    expect(page.locator("#rfcApply")).to_have_count(0)
    expect(box).to_contain_text("ונחתך — הרווח הכולל שם נמוך מהנוכחי")
    expect(box).to_contain_text("ונחתך — הרווח הכולל שם גבוה מהנוכחי")
    _no_hscroll(page)


def test_rfcheck_manual_gain_fact_apply_label_includes_if(page):
    """רווח ידני: "החל" קובע גם את נקודת ה-IF שנמדדה (החלטת משתמש 5) — והתווית אומרת את זה."""
    _phone(page)
    res = _rfc_result(level="fact", headline="reduce_gain_overload",
                      headline_params={"list": [0, 2], "x": 0, "y": 4},
                      recommendation={"rf_gain": 4, "if_gain": 47, "fm_notch": None,
                                      "basis": "fact", "reasons": [], "changes": True},
                      config_at_start={"freq": 132.5, "mod": "am", "agc": False, "if_gain": 40,
                                       "rf_gain": 0, "fm_notch": False},
                      evidence=[{"code": "op_clip", "states": [0, 2], "cycles": 3,
                                 "counts": {"0": 3, "2": 2}}])
    holder = {"st": _rfc_status(run_id="r1", result=res)}
    st = {**_default_api()["/api/state"], "agc": False, "fm_notch": False}
    _rfc_mount(page, holder, [], extra={"/api/state": st})
    box = page.locator("#rfcResult")
    expect(box.locator(".rfc-lvl")).to_have_text("עובדה")
    expect(box.locator(".rfc-head")).to_contain_text("עומס חומרה")
    expect(box).to_contain_text("חיתוך ADC")
    apply = page.locator("#rfcApply")
    expect(apply).to_contain_text("LNA 5/9")
    expect(apply).to_contain_text("IF")
    expect(apply).to_contain_text("47")
    _no_hscroll(page)


def test_rfcheck_error_with_failed_restore_is_explicit(page):
    """כשל + שחזור שנכשל ⇒ אומרים את שניהם במפורש ("השמע לא חזר"), לא "השמע הוחזר"."""
    _phone(page)
    res = _rfc_result(level="none", headline="error",
                      headline_params={"error": "ה-SDR לא נפתח לבדיקה"}, recommendation=None,
                      apply_allowed=False, error="ה-SDR לא נפתח לבדיקה",
                      restore={"ok": False, "error": "rtl_airband לא עלה"})
    holder = {"st": _rfc_status(run_id="r1", result=res,
                                restore={"ok": False, "error": "rtl_airband לא עלה"})}
    _rfc_mount(page, holder, [])
    head = page.locator("#rfcResult .rfc-head")
    expect(head).to_contain_text("הבדיקה נכשלה: ה-SDR לא נפתח לבדיקה")
    expect(head).to_contain_text("השמע לא חזר אוטומטית")
    expect(head).not_to_contain_text("השמע הוחזר")
    expect(page.locator("#rfcApply")).to_have_count(0)
    expect(page.locator("#rfcNotchBtn")).to_have_count(0)
    _no_hscroll(page)


def test_rfcheck_notch_compare_is_on_demand_from_result(page):
    """"🔁 השווה מסנן FM" — אחרי תוצאת LNA, בלחיצה בלבד, עם from_run של התוצאה."""
    _phone(page)
    sent = []
    holder = {"st": _rfc_status(run_id="r1", result=_rfc_result())}
    def post(b):
        holder["st"] = _rfc_running(phase="notch")
        return {"ok": True, **holder["st"]}
    holder["post"] = post
    _rfc_mount(page, holder, sent)
    page.click("#rfcNotchBtn")
    expect(page.locator("#rfcConfirm")).to_be_visible()
    expect(page.locator("#rfcConfirm")).to_contain_text("LNA 3/9")
    page.click("#rfcConfirmOk")
    expect(page.locator("#rfcProgress")).to_be_visible()
    assert sent[0] == {"action": "start", "phase": "notch", "from_run": "r1"}, sent
