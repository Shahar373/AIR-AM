"""מסנן הערוץ + FM notch במצב קול (docs/voice-channel-design.md).

הנבדק: (1) הקונפיג שנכתב ל-rtl_airband לכל בחירה (fft_size / bandwidth /
rfnotch_ctrl), וברירת המחדל = ההתנהגות ההיסטורית; (2) רשימה סגורה — שום ערך
מהלקוח לא נכתב לקונפיג כמות שהוא; (3) הגדרות "חומרה" לא מתאפסות בשקט כשקורא
לא שולח אותן; (4) בדיקת האנטנה והניסוי נשארים בתנאים קבועים (בני-השוואה
לבסיס), אבל הבחירה של המשתמש חוזרת אחריהם.
"""
import pytest

import app


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "CONFIG_PATH", tmp_path / "airband.conf")
    monkeypatch.setattr(app, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(app, "STATS_PATH", tmp_path / "stats.txt")
    monkeypatch.setattr(app, "PRESETS_PATH", tmp_path / "presets.json")
    rec = tmp_path / "recordings"
    rec.mkdir()
    monkeypatch.setattr(app, "REC_DIR", rec)
    return tmp_path


@pytest.fixture
def client(paths):
    return app.app.test_client()


def _cfg(**kw):
    args = dict(freq=132.5, mod="am", agc=True, if_gain=40, rf_gain=4, squelch_mode="auto")
    args.update(kw)
    return app.render_config(**args)


# --- render_config ------------------------------------------------------------

def test_default_is_historical_behavior():
    cfg = _cfg()
    assert "fft_size = 512;" in cfg                 # ברירת המחדל של rtl_airband — כמו קודם
    assert 'device_string = "driver=sdrplay";' in cfg
    assert "bandwidth" not in cfg
    assert app.DEFAULT_STATE["chan_filter"] == "standard"
    assert app.DEFAULT_STATE["fm_notch"] is False


@pytest.mark.parametrize("name,fft,bw", [("standard", 512, 0), ("narrow", 1024, 0), ("narrow8k", 1024, 8000)])
def test_channel_filters(name, fft, bw):
    cfg = _cfg(chan_filter=name)
    assert f"fft_size = {fft};" in cfg
    if bw:
        line = f"        bandwidth = {bw};"
        assert line in cfg
        # בתוך הערוץ, לפני ה-outputs (מפתח של הערוץ, לא של ההתקן)
        assert cfg.index('modulation = "am";') < cfg.index(line) < cfg.index("outputs:")
    else:
        assert "bandwidth" not in cfg


def test_fft_size_is_root_setting():
    """fft_size הוא מפתח שורש ב-rtl_airband (rtl_airband.cpp: root["fft_size"]) — לפני devices."""
    cfg = _cfg(chan_filter="narrow")
    assert cfg.index("fft_size = 1024;") < cfg.index("devices:")


def test_fm_notch_in_device_string():
    assert 'device_string = "driver=sdrplay,rfnotch_ctrl=true";' in _cfg(fm_notch=True)
    assert "rfnotch" not in _cfg(fm_notch=False)


def test_unknown_filter_falls_back_to_standard():
    assert "fft_size = 512;" in _cfg(chan_filter="../../etc")


def test_fft_sizes_are_valid_for_rtl_airband():
    """rtl_airband מקבל רק חזקות של 2 בטווח 256..8192 (MIN/MAX_FFT_SIZE_LOG=8..13)."""
    for spec in app.CHAN_FILTERS.values():
        n = spec["fft"]
        assert 256 <= n <= 8192 and n & (n - 1) == 0
        # bandwidth 0 = כבוי; אחרת חייב להיות בתוך Nyquist של ה-I/Q ב-16kHz
        assert spec["bandwidth"] == 0 or 0 < spec["bandwidth"] <= 16000


# --- _parse_tune ----------------------------------------------------------------

def test_parse_whitelist_and_bool(paths):
    p, err = app._parse_tune({"freq": 132.5, "chan_filter": "narrow8k", "fm_notch": True})
    assert err is None and p["chan_filter"] == "narrow8k" and p["fm_notch"] is True
    p, _ = app._parse_tune({"freq": 132.5, "chan_filter": "evil\";", "fm_notch": "false"})
    assert p["chan_filter"] == "standard" and p["fm_notch"] is False
    p, _ = app._parse_tune({"freq": 132.5, "chan_filter": "narrow", "fm_notch": "on"})
    assert p["fm_notch"] is True


def test_parse_missing_keys_keep_saved_choice(paths):
    """"האזן למגדל"/curl שלא שולחים את המפתחות לא מאפסים את בחירת המשתמש."""
    app.save_state({**app.DEFAULT_STATE, "chan_filter": "narrow", "fm_notch": True})
    p, _ = app._parse_tune({"freq": 134.6})
    assert p["chan_filter"] == "narrow" and p["fm_notch"] is True
    p, _ = app._parse_tune({"freq": 134.6, "chan_filter": "standard", "fm_notch": False})
    assert p["chan_filter"] == "standard" and p["fm_notch"] is False


# --- זרימות ---------------------------------------------------------------------

def test_tune_persists_and_writes_config(client, monkeypatch):
    monkeypatch.setattr(app, "_is_active", lambda svc: False)
    monkeypatch.setattr(app, "_restart_and_verify", lambda: (None, None, False))
    monkeypatch.setattr(app, "_denoise_after_voice", lambda: None)
    r = client.post("/api/tune", json={"freq": 132.5, "mod": "am", "agc": True, "if_gain": 40,
                                       "rf_gain": 4, "squelch_mode": "auto", "squelch_snr": 9,
                                       "chan_filter": "narrow8k", "fm_notch": True})
    assert r.status_code == 200
    st = app.load_state()
    assert st["chan_filter"] == "narrow8k" and st["fm_notch"] is True
    cfg = app.CONFIG_PATH.read_text()
    assert "fft_size = 1024;" in cfg and "bandwidth = 8000;" in cfg and "rfnotch_ctrl=true" in cfg


def test_rollback_keeps_user_filter(paths, monkeypatch):
    monkeypatch.setattr(app.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(app, "_is_active", lambda svc: True)
    monkeypatch.setattr(app.time, "sleep", lambda *a: None)
    prev = {**app.DEFAULT_STATE, "chan_filter": "narrow", "fm_notch": True}
    assert app._rollback(prev) is True
    cfg = app.CONFIG_PATH.read_text()
    assert "fft_size = 1024;" in cfg and "rfnotch_ctrl=true" in cfg


def test_probe_runs_under_fixed_conditions(paths, monkeypatch):
    """בדיקת האנטנה/הניסוי נמדדים תמיד באותם תנאים (בני-השוואה ל-signal_baseline):
    _probe_params לא נושא את בחירת המשתמש ⇒ standard, בלי notch."""
    monkeypatch.setattr(app, "_is_active", lambda svc: False)
    monkeypatch.setattr(app, "_restart_and_verify", lambda: (None, None, False))
    monkeypatch.setattr(app, "_denoise_after_voice", lambda: None)
    app.save_state({**app.DEFAULT_STATE, "chan_filter": "narrow8k", "fm_notch": True})
    app._enter_voice(app._probe_params(131.55))
    cfg = app.CONFIG_PATH.read_text()
    assert "fft_size = 512;" in cfg and "bandwidth" not in cfg and "rfnotch" not in cfg


def test_restore_after_probe_brings_back_user_choice(paths, monkeypatch):
    seen = []
    monkeypatch.setattr(app, "_enter_voice", lambda params: seen.append(params) or (None, None, False))
    prev = {**app.DEFAULT_STATE, "chan_filter": "narrow", "fm_notch": True}
    app._restore_after_probe(prev, "voice")
    assert seen and seen[-1]["chan_filter"] == "narrow" and seen[-1]["fm_notch"] is True
