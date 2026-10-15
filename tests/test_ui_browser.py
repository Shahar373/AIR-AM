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
        "stale_rows": 0, "stale_probes": 1,
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


# --- SDR שלא ב-USB: הממשק אומר *מה* קרה ו*מה יקרה* --------------------------
#
#  מאז v2.25.1 מצב שמור לא נופל ל-off כשה-SDR חסר (אתחול בלי SDR / ניתוק USB
#  בסריקה) — הוא ממתין לו, ו-health מדווח ok=False. בלי הבדיקות האלה הממשק
#  הציג "ממתין לחלון הזמן הבא" (שקר — אין קשר לחלון שעות) או "בדוק את המערכת"
#  (בלי לומר שהמצב יחזור לבד כשיחברו את הכבל).

_ALL_DOWN = {"rtl_airband": "inactive", "icecast2": "active", "sdrplay": "active",
             "airam-acars": "inactive", "airam-vdl2": "inactive",
             "airam-satcom": "inactive"}


def _sdr_missing_api(mode, sdr_present=False, scan_wait=None):
    st = _default_api()["/api/state"]
    over = {
        "/api/state": {**st, "app_mode": mode, "mode_ok": False,
                       "scan_plan": [{"mode": "acars", "dwell_sec": 60}]},
        "/api/health": {"ok": False, "app_mode": mode, "mode_ok": False,
                        "services": _ALL_DOWN, "sdr_present": sdr_present,
                        "stats_age": None},
    }
    if scan_wait:
        over["/api/scan"] = {"ok": True, "active": True, "idx": -1, "leg": None,
                             "next_switch_at": None, "wait": scan_wait,
                             "plan": [{"mode": "acars", "dwell_sec": 60}], "now": 0}
    return over


def test_scan_waiting_for_sdr_is_not_reported_as_waiting_for_window(page):
    _mount(page, overrides=_sdr_missing_api("scan", scan_wait="sdr"))
    state = page.locator("#homeStateTxt")
    expect(state).to_contain_text("ממתין ל-SDR", timeout=10000)
    expect(state).not_to_contain_text("חלון")
    expect(page.locator("#status")).to_contain_text("יחזור לבד כשיחובר")


def test_scan_waiting_for_window_still_says_window(page):
    """הצד השני: המתנה לחלון שעות אינה תקלה ונשארת בניסוח הקיים."""
    st = _default_api()["/api/state"]
    plan = [{"mode": "acars", "dwell_sec": 60, "active_from": "03:00", "active_to": "04:00"}]
    _mount(page, overrides={
        "/api/state": {**st, "app_mode": "scan", "mode_ok": True, "scan_plan": plan},
        "/api/health": {"ok": True, "app_mode": "scan", "services": _ALL_DOWN,
                        "sdr_present": True, "stats_age": None},
        "/api/scan": {"ok": True, "active": True, "idx": -1, "leg": None,
                      "next_switch_at": None, "wait": "window", "plan": plan, "now": 0},
    })
    expect(page.locator("#homeStateTxt")).to_contain_text("ממתין לחלון", timeout=10000)
    expect(page.locator("#status")).not_to_contain_text("תקלה")


@pytest.mark.parametrize("mode,sdr_present,expected", [
    ("acars", False, "המצב (acars) יחזור לבד כשיחובר"),
    ("vdl2", False, "המצב (vdl2) יחזור לבד כשיחובר"),
    # SATCOM לא חוזר לבד (StartLimitBurst — בטיחות bias-T): הוראה אחרת
    ("satcom", False, "היכנס שוב ל-SATCOM"),
    # SDR נוכח => זו תקלה אחרת; לא טוענים שום דבר על USB
    ("acars", True, "בדוק את המערכת"),
])
def test_fault_message_names_missing_sdr_and_what_happens_next(page, mode, sdr_present, expected):
    _mount(page, overrides=_sdr_missing_api(mode, sdr_present=sdr_present))
    # מיד — לא אחרי 10ש' של poll תקופתי (האתחול קורא ל-pollGlobalState כש-mode_ok=false)
    expect(page.locator("#status")).to_contain_text(expected, timeout=5000)
    if sdr_present:
        expect(page.locator("#status")).not_to_contain_text("USB")


# --- כניסה ל-SATCOM: אישור bias-T (סיכון חומרה) ------------------------------
#
#  זה המסלול היחיד בממשק שמדליק מתח (‎+4.7V) על מחבר האנטנה. החוזה (§12):
#  דיאלוג חוסם *לפני* הבקשה, בכל נקודות הכניסה; ביטול לא שולח כלום; והטקסט
#  והבקשה תואמים את "הזנת LNA חיצונית" — לעולם לא טוענים שה-bias-T ידלק כשלא.

def _standby_api(**state_extra):
    st = _default_api()["/api/state"]
    return {
        "/api/state": {**st, "app_mode": "off", "mode_ok": True, **state_extra},
        "/api/health": {"ok": True, "app_mode": "off", "services": _ALL_DOWN,
                        "sdr_present": True, "stats_age": None},
    }


def _record_mode_posts(posts, reply=None):
    def handler(route, url):
        posts.append(json.loads(route.request.post_data or "{}"))
        body = reply or {"ok": True, "app_mode": "satcom"}
        route.fulfill(status=200 if body.get("ok") else 500,
                      content_type="application/json", body=json.dumps(body))
    return handler


def test_satcom_entry_cancelled_sends_nothing(page):
    posts, dialogs = [], []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    _mount(page, overrides={**_standby_api(), "/api/mode": _record_mode_posts(posts)})
    expect(page.locator("#homeGoSatcom")).to_be_visible()
    page.locator("#homeGoSatcom").click()
    page.evaluate("1")                    # סיבוב event-loop אחרי הדיאלוג
    assert len(dialogs) == 1 and "bias-T" in dialogs[0]
    assert posts == []                    # ביטול => שום בקשה, שום מתח


@pytest.mark.parametrize("external,bias_tee,must,must_not", [
    (False, True, "ידליק את ה-bias-T", "יישאר כבוי"),
    (True, False, "יישאר כבוי", "ידליק את ה-bias-T"),
])
def test_satcom_confirm_text_and_request_match_external_lna(page, external, bias_tee, must, must_not):
    """⚠ ה-checkbox מאותחל מה-state *בטעינת הדף* — לא רק בביקור בתצוגת SATCOM —
    כי הכניסה כאן היא מכרטיס הבית. אחרת משתמש שבחר הזנה חיצונית היה שולח
    בשקט bias_tee=true (ר' §12, בטיחות bias-T, סעיף 4)."""
    posts, dialogs = [], []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.accept()))
    _mount(page, overrides={**_standby_api(satcom_bias_tee=not external),
                            "/api/mode": _record_mode_posts(posts)})
    expect(page.locator("#homeGoSatcom")).to_be_visible()
    page.locator("#homeGoSatcom").click()
    expect(page.locator("#status")).to_contain_text("מצב SATCOM פעיל", timeout=5000)
    assert len(dialogs) == 1
    assert must in dialogs[0] and must_not not in dialogs[0]
    assert len(posts) == 1 and posts[0]["mode"] == "satcom"
    assert posts[0]["bias_tee"] is bias_tee


def test_power_button_back_to_satcom_also_asks_for_confirmation(page):
    """⏻ מ-standby מחזיר את prev_mode. כשזה SATCOM — אותו אישור, לא קיצור דרך."""
    posts, dialogs = [], []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    _mount(page, overrides={**_standby_api(prev_mode="satcom"),
                            "/api/mode": _record_mode_posts(posts)})
    expect(page.locator("#powerBtnLbl")).to_contain_text("הפעלה")
    page.locator("#powerBtn").click()
    page.evaluate("1")
    assert len(dialogs) == 1 and posts == []


# --- כישלון כניסה למצב: נוחתים בבית עם השגיאה, לעולם לא בקול -----------------

def test_mode_entry_failure_lands_home_with_error_not_voice(page):
    st = _default_api()["/api/state"]
    fail = {"ok": False, "error": "acarsdec נכשל לעלות", "detail": "journal tail…",
            "app_mode": "off", "state": {**st, "app_mode": "off", "prev_mode": "acars"}}
    posts = []
    _mount(page, overrides={**_standby_api(), "/api/mode": _record_mode_posts(posts, fail)})
    expect(page.locator("#homeGoAcars")).to_be_visible()
    page.locator("#homeGoAcars").click()
    expect(page.locator("#homeErrBox")).to_be_visible(timeout=5000)
    expect(page.locator("#homeErrBox summary")).to_contain_text("acarsdec נכשל לעלות")
    expect(page.locator("#homeView")).to_be_visible()
    expect(page.locator("#voiceView")).to_be_hidden()        # אין fallback לקול
    expect(page.locator("#powerBtnLbl")).to_contain_text("הפעלה")   # standby, לא "פעיל"
    assert [p["mode"] for p in posts] == ["acars"]


@pytest.mark.parametrize("mode,card,label", [
    ("acars", "#homeGoAcars", "ACARS"),
    ("vdl2", "#homeGoVdl2", "VDL2"),
])
def test_successful_data_mode_entry_does_not_leave_status_stuck(page, mode, card, label):
    """⚠ רגרסיה: אחרי כניסה *מוצלחת* ל-ACARS/VDL2/SATCOM מה-UI, שורת הסטטוס
    נשארה "מפעיל ACARS…" עד טעינה מחדש — אף poller לא דורס אותה בהצלחה. נראה
    כמו מעבר תקוע, בדיוק כשהכול עובד. (קול לא הושפע — יש לו מסלול סטטוס משלו.)"""
    posts = []
    _mount(page, overrides={**_standby_api(),
                            "/api/mode": _record_mode_posts(posts, {"ok": True, "app_mode": mode})})
    expect(page.locator(card)).to_be_visible()
    page.locator(card).click()
    expect(page.locator("#status")).to_contain_text(f"מצב {label} פעיל", timeout=5000)
    assert [p["mode"] for p in posts] == [mode]
