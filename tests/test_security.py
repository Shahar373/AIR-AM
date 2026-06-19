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


def test_sudo_prefix_shape():
    # כ-root => ללא prefix; כלא-root => sudo -n לפני systemctl restart
    assert app.SUDO == ([] if __import__("os").geteuid() == 0 else ["sudo", "-n"])
