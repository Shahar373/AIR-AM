# ============================================================================
#  AIR-AM - פרופילי רווח לפי מקום (PR 4, docs/voice-rf-quality-plan.md).
# ============================================================================
import pytest

import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "CONFIG_PATH", tmp_path / "airband.conf")
    monkeypatch.setattr(app, "STATE_PATH", tmp_path / "state.json")
    return app.app.test_client()


def _save(client, name):
    return client.post("/api/profiles", json={"action": "save", "name": name})


def test_save_captures_current_frontend_and_matches(client):
    app.save_state({**app.DEFAULT_STATE, "rf_gain": 6, "fm_notch": True, "voice_lowpass": 3000})
    r = _save(client, "פארק אריאל שרון")
    assert r.status_code == 200
    p = r.get_json()["profiles"][0]
    assert (p["rf_gain"], p["fm_notch"], p["voice_lowpass"], p["agc"]) == (6, True, 3000, True)
    g = client.get("/api/profiles").get_json()
    assert g["active"] == p["id"]                         # ההגדרות הנוכחיות = הפרופיל
    app.save_state({**app.load_state(), "rf_gain": 2})
    assert client.get("/api/profiles").get_json()["active"] is None   # "שונה"


def test_save_requires_name_and_has_limit(client, monkeypatch):
    assert _save(client, "  ").status_code == 400
    monkeypatch.setattr(app, "PROFILES_MAX", 2)
    assert _save(client, "א").status_code == 200 and _save(client, "ב").status_code == 200
    assert _save(client, "ג").status_code == 409


def test_delete(client):
    pid = _save(client, "x").get_json()["profiles"][0]["id"]
    assert client.post("/api/profiles", json={"action": "delete", "id": pid}).status_code == 200
    assert client.get("/api/profiles").get_json()["profiles"] == []
    assert client.post("/api/profiles", json={"action": "delete", "id": pid}).status_code == 404


def test_apply_not_voice_only_saves_state(client, monkeypatch):
    app.save_state({**app.DEFAULT_STATE, "rf_gain": 8, "voice_narrow": True})
    pid = _save(client, "x").get_json()["profiles"][0]["id"]
    app.save_state({**app.load_state(), "rf_gain": 2, "voice_narrow": False, "app_mode": "off"})
    monkeypatch.setattr(app, "_live_mode", lambda: None)
    r = client.post("/api/profiles", json={"action": "apply", "id": pid})
    st = app.load_state()
    assert r.status_code == 200 and st["rf_gain"] == 8 and st["voice_narrow"] is True
    assert st["app_mode"] == "off"                        # לא מדליק קול


def test_apply_in_voice_goes_through_voice_tune(client, monkeypatch):
    app.save_state({**app.DEFAULT_STATE, "rf_gain": 6, "freq": 134.6, "app_mode": "voice"})
    pid = _save(client, "x").get_json()["profiles"][0]["id"]
    app.save_state({**app.load_state(), "rf_gain": 1})
    monkeypatch.setattr(app, "_live_mode", lambda: "voice")
    calls = []
    monkeypatch.setattr(app, "_voice_tune", lambda p: (calls.append(p) or {"ok": True}, 200))
    assert client.post("/api/profiles", json={"action": "apply", "id": pid}).status_code == 200
    assert calls[0]["rf_gain"] == 6 and calls[0]["freq"] == 134.6   # התדר הנוכחי נשמר


def test_apply_unknown_and_bad_action(client):
    assert client.post("/api/profiles", json={"action": "apply", "id": "nope"}).status_code == 404
    assert client.post("/api/profiles", json={"action": "x"}).status_code == 400
