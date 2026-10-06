# ============================================================================
#  AIR-AM - PR 3 (docs/voice-rf-quality-plan.md): היסט ה-bin ב-rtl_airband והגדרות
#  השמע (מסנן ערוץ צר / רוחב שמע). בלי חומרה — שחזור הנוסחה של rtl_airband בפייתון.
# ============================================================================
import math

import pytest

import app

RATE = 2_560_000      # app.SAMPLE_RATE * 1e6
FFT = 512             # DEFAULT_FFT_SIZE_LOG=9 (rtl_airband.h:81)


def _anynum2int(mhz_text):
    """parse_anynum2int (config.cpp:298-310) לערך float: (int)(MHz·1e6) — קיטום, לא עיגול."""
    return int(float(mhz_text) * 1e6)


def _rtl_bin(freq_txt, center_txt):
    """config.cpp:670 בדיוק — כולל ה-ceil(x−1) שמוריד bin כש-x שלם."""
    f, c = _anynum2int(freq_txt), _anynum2int(center_txt)
    return math.ceil((f + RATE - c) / (RATE // FFT) - 1.0) % FFT, f, c


def _true_bin(f, c):
    """ה-bin שבו FFTW שם גוון ב-(f−c): עיגול לקרוב, מיפוי שלילי ל-N+k."""
    return round((f - c) / (RATE / FFT)) % FFT


def _channels():
    """כל ערוצי 25kHz וגם 8.33kHz בתחום האווירי 118–137."""
    out = set()
    for k in range(int((136.975 - 118.0) / 0.025) + 1):
        out.add(round(118.0 + k * 0.025, 4))
    for k in range(int((136.99 - 118.0) / (0.025 / 3)) + 1):
        out.add(round(118.0 + k * 0.025 / 3, 4))
    return sorted(out)


def test_every_airband_channel_lands_on_the_true_bin():
    """⚠ הבאג שתוקן: ב-DC_OFFSET=0.3 כל ערוץ נבחר bin אחד מתחת (5kHz). עכשיו — לכל
    ערוץ, בפורמט ש-render_config באמת כותב (.4f), ה-bin של rtl_airband = ה-bin האמיתי,
    והערוץ במרחק ≤ ~100Hz ממרכזו."""
    bad = []
    for ch in _channels():
        freq_txt, center_txt = f"{ch:.4f}", f"{ch + app.DC_OFFSET:.4f}"
        b, f, c = _rtl_bin(freq_txt, center_txt)
        if b != _true_bin(f, c):
            bad.append(ch)
        k = b if b < FFT // 2 else b - FFT
        assert abs((f - c) - k * (RATE / FFT)) <= 120, ch
    assert not bad, f"{len(bad)} ערוצים על bin שגוי, למשל {bad[:5]}"


def test_old_round_offset_was_off_by_one_bin():
    """המצב הקודם, כתיעוד: 0.3 עגול ⇒ 451 במקום 452 על ATIS."""
    b, f, c = _rtl_bin("132.5000", "132.8000")
    assert b == 451 and _true_bin(f, c) == 452


def test_render_narrow_and_lowpass_only_when_set():
    base = app.render_config(132.5, "am", True, 40, 4)
    assert "bandwidth" not in base and "lowpass" not in base      # ברירת מחדל = upstream
    cfg = app.render_config(132.5, "am", True, 40, 4, narrow=True, lowpass=3000)
    assert f"bandwidth = {app.CHANNEL_BW_NARROW};" in cfg and "lowpass = 3000;" in cfg


def test_sanitize_lowpass():
    assert app._sanitize_lowpass(3000) == 3000
    assert app._sanitize_lowpass("3000") == 3000
    assert app._sanitize_lowpass(9999) == app.AUDIO_LOWPASS_DEFAULT
    assert app._sanitize_lowpass(None) == app.AUDIO_LOWPASS_DEFAULT


def test_parse_tune_audio_options_are_presence_based():
    p, _ = app._parse_tune({"freq": 132.5})
    assert p["voice_narrow"] is None and p["voice_lowpass"] is None   # לקוח ישן לא מכבה
    p, _ = app._parse_tune({"freq": 132.5, "voice_narrow": "true", "voice_lowpass": 3000})
    assert p["voice_narrow"] is True and p["voice_lowpass"] == 3000


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "CONFIG_PATH", tmp_path / "airband.conf")
    monkeypatch.setattr(app, "STATE_PATH", tmp_path / "state.json")
    return tmp_path


def test_write_config_uses_saved_audio_options(paths):
    """כל מסלול שכותב קונפיג קול (בדיקת אנטנה/🩺/סריקה/שחזור) שומר על הגדרות השמע."""
    app.save_state({**app.DEFAULT_STATE, "voice_narrow": True, "voice_lowpass": 3000})
    app.write_config(132.5, "am", True, 40, 4)
    cfg = app.CONFIG_PATH.read_text()
    assert "bandwidth = 7000;" in cfg and "lowpass = 3000;" in cfg


def test_config_with_old_round_offset_is_stale(paths):
    app.write_config(132.5, "am", True, 40, 4)
    assert app._config_stale() is False
    old = app.CONFIG_PATH.read_text().replace("centerfreq = 132.7999;", "centerfreq = 132.8000;")
    app.CONFIG_PATH.write_text(old)
    assert app._config_stale() is True


def test_free_entry_5_decimals_still_correct_bin_and_not_stale(tmp_path, monkeypatch):
    """עיגול אחד לשני הערכים — 132.28125 נתן קודם הפרש 0.3000 (bin שגוי + stale בכל אתחול)."""
    monkeypatch.setattr(app, "CONFIG_PATH", tmp_path / "airband.conf")
    monkeypatch.setattr(app, "STATE_PATH", tmp_path / "state.json")
    for fr in (132.28125, 136.49185, 118.00835):
        cfg = app.render_config(fr, "am", True, 40, 4)
        ft = app._CONF_FREQ_RE.search(cfg).group(1)
        ct = app._CONF_CENTER_RE.search(cfg).group(1)
        b, f, c = _rtl_bin(ft, ct)
        assert b == _true_bin(f, c), fr
        app.write_config(fr, "am", True, 40, 4)
        assert app._config_stale() is False, fr


def test_narrow_is_am_only():
    assert "bandwidth" not in app.render_config(145.5, "nfm", True, 40, 4, narrow=True)
