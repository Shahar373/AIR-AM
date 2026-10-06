# ============================================================================
#  AIR-AM - בדיקות ה-DSP של בודק ה-RF (PR 2; spec §10.2)
# ----------------------------------------------------------------------------
#  ה-DSP פר-סלוט ב-webtune/rfcheck_probe.py (SlotDSP) חייב להיות *אותו ערוץ* כמו
#  של rtl_airband: חלון BH7 על 512 נקודות עם מכנה N−1 (rtl_airband.cpp:361-371),
#  hop ‏160 (‏:419), על ה-bin המדויק ‎−60. כאן מוכיחים זאת מול FFT מפורש ומול
#  פיזיקה ידועה (אמפליטודת נשא), לא מול המימוש עצמו.
#  numpy אופציונלי ב-CI (‏importorskip) — אותו דפוס כמו ב-spec §3.5.
# ============================================================================
import math

import pytest

np = pytest.importorskip("numpy")

import rfcheck_probe as rp  # noqa: E402
import rfsim  # noqa: E402

FREQ, CENTER = 132_500_000, 132_800_000
NB = [-85000, -70000, -55000, -40000, 40000, 55000, 70000, 85000]
M = 7                                    # מאגרים נמדדים בסלוט (ברירת המחדל של app.py)
N_SLOT = M * rfsim.BUF
T_SQ = 9.54                              # squelch.cpp:38 — ברירת המחדל של rtl_airband


def _dsp(nb=NB, rail=32767):
    return rp.SlotDSP(FREQ, CENTER, rp.RATE, nb, rail)


def _compute(dsp, raw, nbuf=M, want_nb=True):
    return dsp.compute(raw, [rfsim.BUF] * nbuf, want_nb=want_nb)


def _response_db(w, f_hz, fs=rp.RATE):
    i = np.arange(w.size)
    r = abs(np.sum(w * np.exp(2j * np.pi * f_hz * i / fs))) / np.sum(w)
    return 20 * math.log10(max(r, 1e-300))


# --- החלון וה-taps -----------------------------------------------------------
def test_channel_bin_is_exact_minus_60():
    d = _dsp()
    assert d.k_ch == -60                       # ‎−300kHz / (2.56MHz/512)
    assert d.bins[1:] == [-60 + off // 5000 for off in NB]


def test_taps_equal_explicit_fft_bin():
    """מכפלת ה-taps שווה ל-np.fft.fft(w·frame)[452]/(Σw·FS) — ‎−60 ≡ 452 מודולו 512."""
    d = _dsp()
    rng = np.random.default_rng(5)
    w = rp.bh7_window()
    for _ in range(5):
        frame = (rng.integers(-20000, 20000, 512) + 1j * rng.integers(-20000, 20000, 512))
        ref = np.fft.fft(w * frame) / (w.sum() * rp.FS)
        got = frame.astype(np.complex64) @ d.Tm
        for col, k in enumerate(d.bins):
            assert abs(got[col] - ref[k % 512]) < 1e-5, (k, got[col], ref[k % 512])


def test_window_matches_rtl_airband_literals():
    """המקדמים מועתקים כולל הסיומת f (ליטרל float ב-rtl_airband.cpp:361-367)."""
    w = rp.bh7_window()
    a = [float(np.float32(v)) for v in rp._BH7_LITERALS]
    i = 100
    x = sum(((-1) ** k) * a[k] * math.cos(2 * math.pi * k * i / 511) for k in range(7))
    assert w[i] == pytest.approx(x, abs=1e-15)
    assert w.size == 512 and w[0] == pytest.approx(w[-1])     # סימטרי — מכנה N−1


def test_enbw_13_19_khz():
    w = rp.bh7_window()
    enbw = rp.RATE * np.sum(w ** 2) / np.sum(w) ** 2
    assert enbw == pytest.approx(13190, abs=50)


def test_bin_response_sidelobes():
    """אפס ראשון ב-35kHz (מתחת ל-‎−150dB); כל ה-bins השכנים (‎≥40kHz) מתחת ל-‎−170dB.

    ⚠ סטייה מתועדת מ-spec §10.2 ("‎≤−180dB ב-‎≥40kHz"): סריקה בצעד 250Hz עד 700kHz
    מראה ‎−178.7dB במקדמי float64 המדויקים, ו-‎−171.9dB במקדמים כפי ש-rtl_airband
    באמת משתמש בהם (ליטרלים float32). ‎−170dB עדיין רחוק מתחת לטווח הדינמי של ADC
    ‏16-ביט (~96dB + רווח עיבוד) — הדליפה מנשא חזק לרצפת השכנים זניחה."""
    w = rp.bh7_window()
    assert _response_db(w, 35e3) < -150
    worst = max(_response_db(w, f) for f in np.arange(40e3, 700e3, 250.0))
    assert worst <= -170
    for off in NB:
        assert _response_db(w, abs(off)) <= -170
    # ‎−3dB ב-±6.3kHz (התאמה למספרי התכנון, design §3)
    assert _response_db(w, 6.3e3) == pytest.approx(-3.09, abs=0.05)


# --- מדדי סלוט ---------------------------------------------------------------
def test_pure_carrier_levels_are_exact():
    """נשא טהור באמפליטודה A (full-scale) בדיוק על ה-bin => c_car=c_tot=20log10(A),
    p_wb=20log10(A) (הספק מרוכב A²)."""
    A = 0.25
    t = np.arange(N_SLOT) / rp.RATE
    y = A * np.exp(2j * np.pi * -300e3 * t)
    raw = rfsim.quantize(y)
    m = _compute(_dsp(), raw)
    want = 20 * math.log10(A)
    assert m["c_car"] == pytest.approx(want, abs=0.01)
    assert m["c_tot"] == pytest.approx(want, abs=0.01)
    assert m["p_wb"] == pytest.approx(want, abs=0.01)
    assert m["clip"] == 0
    assert all(b == pytest.approx(want, abs=0.01) for b in m["b_tot"])
    # מעטפת שטוחה: סטיית תקן זניחה ו-p99 ≈ ממוצע
    assert m["e_std"] < m["e_mean"] - 60
    assert m["e_p99"] == pytest.approx(m["e_mean"], abs=0.01)
    assert m["frames"] == (N_SLOT - 512) // 160 + 1


def test_c_car_speech_invariant_across_syllables():
    """c_car של AM עם אפנון דמוי-דיבור (m_peak 0.85) בתוך ±0.1dB מהנשא ב-20 חלונות
    הברה שונים, ב-CNR ‏≥30dB (spec §10.2). זה מה שמאפשר להשוות סלוטים שמכילים
    הברות שונות (design §5.5): ב-DSB-AM בלי אפנון-יתר mean|z| = משרעת הנשא."""
    level = -20.0
    noise = -28.0                     # בתוך ה-bin: ‎−28−22.9 => CNR ≈ 30.9dB
    sc = rfsim.Scene(noise_dbfs=noise, carriers=[rfsim.Carrier(level, m_peak=0.85)])
    fe = rfsim.FrontEnd(adc_noise_dbfs=None)
    d = _dsp()
    cnr_floor = level - (noise + 10 * math.log10(13185.3 / rp.RATE))
    assert cnr_floor >= 30
    errs, tot = [], []
    for k in range(20):
        s0 = int(k * 0.37 * rp.RATE)
        raw = rfsim.iq(sc, fe, s0, N_SLOT)
        m = _compute(d, raw, want_nb=False)
        errs.append(m["c_car"] - level)
        tot.append(m["c_tot"])
    assert max(abs(e) for e in errs) <= 0.1, errs
    # לעומת זאת c_tot *כן* תלוי בהברה (מכיל את הספק האפנון) — לכן ה-CNR נשען על c_car
    assert max(tot) - min(tot) > 3 * (max(errs) - min(errs))


def test_clip_counter_counts_rail_including_minus_32768():
    raw = np.zeros(2 * N_SLOT, dtype=np.int16)
    raw[:6] = [32767, -32768, 32766, -32767, 100, -100]
    m = _compute(_dsp(), raw)
    assert m["clip"] == 3                          # 32767, ‎−32768, ‎−32767
    assert m["peak"] == 32768                      # int32 — |‎−32768| לא גולש
    m2 = _compute(_dsp(rail=30000), raw)
    assert m2["clip"] == 4                         # גם 32766


def test_n_nb_ignores_own_carrier_and_picks_quietest_neighbour():
    """n_nb = מינימום על השכנים של חציון |Z|². שכן תפוס (‎+55kHz) לא נבחר, והנשא
    עצמו (גם ‎−10dBFS) לא דולף לשכנים (אפס ראשון ב-35kHz).

    ⚠ החציון של |z|² ברעש גאוסי מרוכב (התפלגות מעריכית) הוא ln2·ממוצע — כלומר
    n_nb נמוך ב-1.59dB מהספק הרעש הממוצע ב-bin. זה בהגדרה (spec §4.6, עמידות
    לשידור לסירוגין בשכן) והוא מתבטל בכל הפרש CNR זוגי."""
    noise = -60.0
    floor_mean = noise + 10 * math.log10(13185.3 / rp.RATE)
    sc = rfsim.Scene(noise_dbfs=noise,
                     carriers=[rfsim.Carrier(-10.0, m_peak=0.85)],
                     tones=[rfsim.Tone(-300e3 + 55e3, -30.0)])
    fe = rfsim.FrontEnd(adc_noise_dbfs=None)
    m = _compute(_dsp(), rfsim.iq(sc, fe, 0, N_SLOT))
    assert m["n_nb"] == pytest.approx(floor_mean + 10 * math.log10(math.log(2)), abs=0.3)
    # בלי ה-nb: null
    assert _compute(_dsp(nb=[]), rfsim.iq(sc, fe, 0, N_SLOT))["n_nb"] is None
    assert _compute(_dsp(), rfsim.iq(sc, fe, 0, N_SLOT), want_nb=False)["n_nb"] is None


def test_if_scaling_invariance_of_cnr():
    """הזזת ה-IF ב-±20dB (בתוך המרווח) משנה את CNR = c_car − n_nb ב-‎≤0.05dB: CNR
    נמדד באותו מצב ובאותו IFGR לנשא ולרעש (design §2.3)."""
    sc = rfsim.Scene(noise_dbfs=-45.0, carriers=[rfsim.Carrier(-35.0, m_peak=0.85)])
    fe = rfsim.FrontEnd(adc_noise_dbfs=None)
    d = _dsp()
    cnr = {}
    for ifgr in (20, 40, 59):          # ‎+20dB, ייחוס, ‎−19dB
        m = _compute(d, rfsim.iq(sc, fe, 12345, N_SLOT, lna=4, ifgr=ifgr))
        assert m["clip"] == 0
        cnr[ifgr] = m["c_car"] - m["n_nb"]
    assert abs(cnr[20] - cnr[40]) <= 0.05, cnr
    assert abs(cnr[59] - cnr[40]) <= 0.05, cnr


def _cls(m, floor, T=T_SQ):
    """כלל החצאים של spec §5.3 צעד 3 (המימוש עצמו ב-rfcheck_analysis — כאן רק מוכיחים
    שהשדות שהבודק מפיק מספיקים לו)."""
    h1, h2 = m["c_tot_h1"] - floor, m["c_tot_h2"] - floor
    if h1 >= T and h2 >= T:
        return "carrier"
    if h1 < T and h2 < T:
        return "silence"
    return "edge"


def test_halves_expose_ptt_edge():
    dur = N_SLOT / rp.RATE
    d = _dsp()
    fe = rfsim.FrontEnd(adc_noise_dbfs=None)
    base = dict(noise_dbfs=-60.0)
    edge = rfsim.Scene(carriers=[rfsim.Carrier(-30.0, ptt=[(0.0, 0.4 * dur)])], **base)
    full = rfsim.Scene(carriers=[rfsim.Carrier(-30.0, ptt=[(0.0, 10.0)])], **base)
    quiet = rfsim.Scene(**base)
    out = {}
    for name, sc in (("edge", edge), ("full", full), ("quiet", quiet)):
        m = _compute(d, rfsim.iq(sc, fe, 0, N_SLOT))
        out[name] = _cls(m, m["n_nb"])
    assert out == {"edge": "edge", "full": "carrier", "quiet": "silence"}


def test_b_tot_follows_buffer_boundaries():
    """b_tot[j] = c_tot של המסגרות שמתחילות במאגר j — בסיס בדיקת ההתייצבות."""
    t_step = 3 * rfsim.BUF / rp.RATE           # מדרגה בדיוק בתחילת מאגר 3
    sc = rfsim.Scene(noise_dbfs=-70.0, carriers=[rfsim.Carrier(-30.0, m_peak=0.0, ptt=[(t_step, 10.0)])])
    m = _compute(_dsp(), rfsim.iq(sc, rfsim.FrontEnd(adc_noise_dbfs=None), 0, N_SLOT))
    b = m["b_tot"]
    assert len(b) == M
    assert all(v < -80 for v in b[:2])          # רעש בלבד (‎−70−22.9)
    assert all(v == pytest.approx(-30.0, abs=0.05) for v in b[3:])
    assert -80 < b[2] < -30                      # מסגרות אחרונות של מאגר 2 חופפות את המדרגה


def test_rfsim_im3_lands_on_channel_only_when_nonlinear():
    """הסימולטור עצמו: חוסמים ב-‎+100/‎+500kHz => IM3 ‏(2f1−f2) בדיוק על ‎−300kHz — רק
    כשהקצה הקדמי לא ליניארי (Rapp). כך בדיקות הניתוח מקבלות "דחיסה" אמיתית."""
    sc = rfsim.Scene(noise_dbfs=-80.0, tones=[rfsim.Tone(100e3, -12.0), rfsim.Tone(500e3, -12.0)])
    d = _dsp()
    lin = _compute(d, rfsim.iq(sc, rfsim.FrontEnd(adc_noise_dbfs=None), 0, N_SLOT))
    sat = _compute(d, rfsim.iq(sc, rfsim.FrontEnd(adc_noise_dbfs=None, sat_dbfs=-8.0), 0, N_SLOT))
    floor = -80.0 + 10 * math.log10(13185.3 / rp.RATE)
    assert lin["c_tot"] == pytest.approx(floor, abs=1.0)
    assert sat["c_tot"] > floor + 30


def test_compute_rejects_too_few_samples():
    assert _dsp().compute(np.zeros(2 * 600, dtype=np.int16), [600]) is None


def test_bin_must_be_exact():
    with pytest.raises(ValueError):
        rp.SlotDSP(FREQ, CENTER + 2500, rp.RATE, NB)
