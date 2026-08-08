# ============================================================================
#  AIR-AM - בדיקות חיזוק האבטחה (#5)
# ----------------------------------------------------------------------------
#  ה-guard של בקשות משנות-מצב: התאמת Origin ל-Host, ו-PIN אופציונלי.
#  רץ בלי חומרה: בקשות לא-תקינות נחסמות לפני נתיב הכיוונון.
# ============================================================================
import time

import pytest

import app


@pytest.fixture
def client():
    return app.app.test_client()


def test_origin_mismatch_blocked(client):
    # Origin זר => 403 (CSRF / DNS-rebinding)
    r = client.post("/api/tune", json={"freq": 134.6},
                    headers={"Origin": "http://evil.example.com"})
    assert r.status_code == 403


def test_same_origin_passes_guard(client):
    # אותו origin => עובר את ה-guard; freq לא תקין => 400 (לא 401/403)
    r = client.post("/api/tune", json={"freq": "bad"},
                    headers={"Origin": "http://localhost"})
    assert r.status_code == 400


def test_no_pin_no_origin_passes(client):
    # בלי PIN ובלי Origin => לא נחסם (400 על freq לא תקין מוכיח passthrough)
    assert client.post("/api/tune", json={"freq": "bad"}).status_code == 400


def test_pin_required_when_set(client, tmp_path, monkeypatch):
    monkeypatch.setattr(app, "AIRAM_PIN", "1234")
    monkeypatch.setattr(app, "PRESETS_PATH", tmp_path / "presets.json")
    body = [{"name": "Tower", "freq": 134.6}]
    assert client.put("/api/presets", json=body).status_code == 401          # בלי header
    r = client.put("/api/presets", json=body, headers={"X-AIRAM-PIN": "1234"})
    assert r.status_code == 200 and r.get_json()["ok"]                        # עם header נכון


def test_pin_wrong_rejected(client, tmp_path, monkeypatch):
    monkeypatch.setattr(app, "AIRAM_PIN", "1234")
    monkeypatch.setattr(app, "PRESETS_PATH", tmp_path / "presets.json")
    r = client.put("/api/presets", json=[{"name": "x", "freq": 134.6}],
                   headers={"X-AIRAM-PIN": "9999"})
    assert r.status_code == 401


def test_pin_repeated_failures_are_rate_limited(client, tmp_path, monkeypatch):
    """⚠ רגרסיה אמיתית: בלי rate-limit, PIN בן 4 ספרות (10,000 ערכים) ניתן
    למיצוי תוך שניות מכל לקוח ברשת המקומית.

    ⚠⚠ הגרסה הראשונה של ההגנה *לא באמת הגבילה קצב*: היא השהתה שנייה ואז בדקה
    את ה-PIN בכל זאת. Flask רץ threaded=True, ולכן ההשהיה מתרחשת במקביל בכל
    thread — תוקף עם 100 חיבורים בו-זמנית היה ממצה 4 ספרות בכ-100 שניות במקום
    ב-"2.7 שעות" שההערה בקוד הבטיחה. עכשיו הבקשה **נדחית ב-429 בלי לבדוק PIN
    בכלל** עד סוף החלון, וזה חסין למקביליות: 5 ניסיונות לחלון, נקודה."""
    monkeypatch.setattr(app, "AIRAM_PIN", "1234")
    monkeypatch.setattr(app, "PRESETS_PATH", tmp_path / "presets.json")
    monkeypatch.setattr(app, "_pin_fails", {})
    slept = []
    monkeypatch.setattr(app.time, "sleep", lambda s: slept.append(s))
    body = [{"name": "x", "freq": 134.6}]
    for _ in range(app.PIN_RATE_MAX_ATTEMPTS):
        r = client.put("/api/presets", json=body, headers={"X-AIRAM-PIN": "0000"})
        assert r.status_code == 401
    assert slept == []                          # עדיין מתחת לסף — אין השהיה
    r = client.put("/api/presets", json=body, headers={"X-AIRAM-PIN": "0000"})
    assert r.status_code == 429                 # מעבר לסף — נדחה, לא רק מושהה
    assert slept == [app.PIN_RATE_DELAY_SEC]
    # ⚠ המחיר המכוון: גם PIN *נכון* נדחה עד סוף החלון. זו המשמעות של הגבלת
    # קצב אמיתית — אילו הנכון היה עובר, תוקף מקבילי היה ממשיך לנחש בחינם.
    blocked = client.put("/api/presets", json=body, headers={"X-AIRAM-PIN": "1234"})
    assert blocked.status_code == 429
    assert blocked.get_json()["auth"] is True
    # אחרי שהחלון פג — הספירה מתאפסת וה-PIN הנכון עובד שוב (לא חסימה קבועה).
    # ⚠ תופסים את השעון האמיתי *לפני* המיקוף: app.time הוא מודול time הגלובלי,
    # ולכן lambda שקוראת ל-time.time() אחרי המיקוף קוראת לעצמה (RecursionError).
    real_time = time.time()
    monkeypatch.setattr(app.time, "time", lambda: real_time + app.PIN_RATE_WINDOW_SEC + 1)
    ok = client.put("/api/presets", json=body, headers={"X-AIRAM-PIN": "1234"})
    assert ok.status_code == 200


def test_pin_rate_limit_prunes_expired_entries(monkeypatch):
    """‏_pin_fails נמחק רק בהצלחה => IP שנכשל ולא הצליח אף פעם נשאר לנצח.
    ברשת עם DHCP/NAT זו צמיחה איטית אך בלתי-חסומה לאורך סשן headless ארוך."""
    monkeypatch.setattr(app, "_pin_fails", {})
    monkeypatch.setattr(app, "_PIN_FAILS_MAX", 4)
    base = time.time()
    monkeypatch.setattr(app.time, "time", lambda: base)
    for i in range(6):
        app._pin_record_fail(f"10.0.0.{i}")
    assert len(app._pin_fails) == 6                      # אף אחד לא פג עדיין
    monkeypatch.setattr(app.time, "time", lambda: base + app.PIN_RATE_WINDOW_SEC + 1)
    app._pin_record_fail("10.0.0.99")                    # מפעיל גיזום
    assert app._pin_fails == {"10.0.0.99": (1, base + app.PIN_RATE_WINDOW_SEC + 1)}


def test_pin_uses_constant_time_comparison(monkeypatch):
    """מוודא ש-_guard באמת קורא ל-hmac.compare_digest ולא להשוואת == רגילה."""
    import hmac as hmac_module
    calls = []
    real_compare = hmac_module.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real_compare(a, b)
    monkeypatch.setattr(app.hmac, "compare_digest", spy)
    monkeypatch.setattr(app, "AIRAM_PIN", "1234")
    c = app.app.test_client()
    c.post("/api/tune", json={"freq": 134.6}, headers={"X-AIRAM-PIN": "9999"})
    assert calls and calls[0] == ("9999", "1234")


def test_sudo_prefix_shape():
    # כ-root => ללא prefix; כלא-root => sudo -n לפני systemctl restart
    assert app.SUDO == ([] if __import__("os").geteuid() == 0 else ["sudo", "-n"])
