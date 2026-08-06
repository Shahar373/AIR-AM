# ============================================================================
#  AIR-AM - בדיקות חיזוק האבטחה (#5)
# ----------------------------------------------------------------------------
#  ה-guard של בקשות משנות-מצב: התאמת Origin ל-Host, ו-PIN אופציונלי.
#  רץ בלי חומרה: בקשות לא-תקינות נחסמות לפני נתיב הכיוונון.
# ============================================================================
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
    למיצוי תוך שניות מכל לקוח ברשת המקומית. אחרי כמה ניסיונות כושלים מאותו
    IP, בקשות נוספות מושהות (לא נחסמות לצמיתות — DHCP/NAT)."""
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
    assert r.status_code == 401
    assert slept == [app.PIN_RATE_DELAY_SEC]     # מעבר לסף — הושהה
    # PIN נכון שמגיע תוך כדי חלון-ההשהיה גם הוא מושהה (הבדיקה קודמת לתוצאה —
    # אחרת "תשובה מיידית" הייתה מסגירה בעצמה שהניסיון נכון), אבל הצלחה מאפסת
    # את המונה עבור אותו IP.
    ok = client.put("/api/presets", json=body, headers={"X-AIRAM-PIN": "1234"})
    assert ok.status_code == 200
    assert slept == [app.PIN_RATE_DELAY_SEC] * 2
    r2 = client.put("/api/presets", json=body, headers={"X-AIRAM-PIN": "0000"})
    assert r2.status_code == 401
    assert slept == [app.PIN_RATE_DELAY_SEC] * 2   # לא הושהה שוב מיד אחרי איפוס


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
