#!/usr/bin/env python3
"""ניסוי A/B למנועי סינון הרעשים — הבסיס להחלטות ב-docs/denoise-design.md §4.

קלט: דיבור אנושי אמיתי (LibriSpeech, 3 דוברים, 16kHz — בדיוק WAVE_RATE של
rtl_airband בבנייה עם NFM), עובר סימולציה של מה ש-rtl_airband מוציא ב-udp_stream:
  - פס מכשיר קשר (300–2700Hz), שלוש תשדורות בעוצמות שונות (מגדל/טייס/טייס חלש)
  - רצפת רעש קבועה של מקלט => תשדורת חלשה = SNR נמוך
  - צפצוף הטרודיין חלש + קליקים אימפולסיביים
  - שני תרחישים: "squelch" (שקט מוחלט בין תשדורות — rtl_airband מאפס את waveout)
    ו-"atis_open" (סקוולץ' פתוח, רעש רציף)
מדדים מול הדיבור הנקי (אחרי יישור זמן): ESTOI לכל תשדורת, הנחתת החלש מול החזק,
רעש בקטעים בלי דיבור, פיזור עוצמות בין התשדורות, RTF חד-ליבתי.

⚠ מגבלות (מתועדות גם במסמך): ערוץ סינתטי, ו-ESTOI הוא מדד — לא אוזן. ההכרעה
הסופית היא האזנה להקלטות אמיתיות על ה-Pi (ר' §8 במסמך).

הרצה:  ./fetch.sh && pip install numpy scipy soundfile pystoi && python3 run.py
"""
import json
import os
import subprocess
import time

import numpy as np
import soundfile as sf
from pystoi import stoi
from scipy.signal import butter, correlate, sosfiltfilt

SR = 16000
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
AUDIO = os.path.join(DATA, "librosa-data", "audio")
MODELS = os.path.join(DATA, "rnnoise-models")
RNNOISE = os.path.join(DATA, "airam-rnnoise")
OUT = os.path.join(DATA, "out")

BAND = "highpass=f=250:poles=2,lowpass=f=3000:poles=2"
DYN = "dynaudnorm=f=100:g=7:p=0.7:m=30:t=0.003"         # = scripts/airam-denoise
SPEECHNORM = "speechnorm=e=25:l=1"


def arnndn(model):
    return f"aresample=48000,arnndn=m={os.path.join(MODELS, model)},aresample=16000"


# None = בלי עיבוד; "RNN:<post>" = airam-rnnoise בצינור (בדיוק כמו בייצור) + post
PIPELINES = {
    "raw": None,
    "band": BAND,
    "band+speechnorm": f"{BAND},{SPEECHNORM}",
    "band+dynaudnorm": f"{BAND},{DYN}",
    "afftdn+dynaudnorm": f"{BAND},afftdn=nr=12:nf=-40:tn=1,{DYN}",
    "arnndn_bd": arnndn("beguiling-drafter-2018-08-30/bd.rnnn"),
    "arnndn_cb": arnndn("conjoined-burgers-2018-08-28/cb.rnnn"),
    "arnndn_mp": arnndn("marathon-prescription-2018-08-29/mp.rnnn"),
    "arnndn_lq": arnndn("leavened-quisling-2018-08-31/lq.rnnn"),
    "arnndn_sh": arnndn("somnolent-hogwash-2018-09-01/sh.rnnn"),
    "rnnoise02": "RNN:",
    "rnnoise02+speechnorm": f"RNN:{BAND},{SPEECHNORM}",
    "rnnoise02+dynaudnorm (=production)": f"RNN:{BAND},{DYN}",
}


def rms_db(x):
    return float(20 * np.log10(np.sqrt(np.mean(x.astype(np.float64) ** 2)) + 1e-12))


def bp(x, lo, hi, order=4):
    sos = butter(order, [lo, hi], btype="band", fs=SR, output="sos")
    return sosfiltfilt(sos, x).astype(np.float32)


def build_scene(open_squelch, rng, seconds_each=8.0):
    names = ["198-209-0000", "3436-172162-0000", "5703-47212-0000"]
    levels = [-20.0, -30.0, -40.0]                 # dBFS של הדיבור עצמו
    labels = ["tower_strong", "pilot_mid", "pilot_weak"]
    gap = int(2.0 * SR)
    clean, segs, pos = [np.zeros(gap, np.float32)], [], gap
    for nm, lv, lb in zip(names, levels, labels):
        s, sr = sf.read(os.path.join(AUDIO, nm + ".hq.ogg"), dtype="float32")
        assert sr == SR
        s = bp(s[: int(seconds_each * SR)], 300, 2700)
        s = s * (10 ** ((lv - rms_db(s)) / 20))
        clean += [s, np.zeros(gap, np.float32)]
        segs.append((pos, pos + len(s), lb))
        pos += len(s) + gap
    clean = np.concatenate(clean)
    n = len(clean)
    noise = bp(rng.standard_normal(n).astype(np.float32), 150, 4000)
    noise *= 10 ** ((-44.0 - rms_db(noise)) / 20)
    t = np.arange(n) / SR
    whistle = (10 ** (-56 / 20) * np.sqrt(2)) * np.sin(2 * np.pi * 1300 * t)
    clicks = np.zeros(n, np.float32)
    idx = rng.choice(n, size=int(3 * n / SR), replace=False)
    clicks[idx] = rng.choice([-1, 1], size=len(idx)) * 0.1
    clicks = bp(clicks, 200, 5000, order=2)
    mask = np.ones(n, np.float32)
    if not open_squelch:                           # פתיחה 150ms לפני, זנב 250ms אחרי
        mask[:] = 0
        for a, b, _ in segs:
            mask[max(0, a - int(0.15 * SR)):min(n, b + int(0.25 * SR))] = 1
    noisy = clean + (noise + whistle + clicks).astype(np.float32) * mask
    return noisy.astype(np.float32), clean, segs, mask


def ff(args, **kw):
    return subprocess.Popen(["ffmpeg", "-nostdin", "-loglevel", "error", "-threads", "1", *args], **kw)


def run_pipeline(af, inp, out):
    if af.startswith("RNN:"):
        post = "aresample=16000" + ("," + af[4:] if af[4:] else "")
        a = ff(["-i", inp, "-ar", "48000", "-ac", "1", "-f", "f32le", "-"], stdout=subprocess.PIPE)
        b = subprocess.Popen([RNNOISE], stdin=a.stdout, stdout=subprocess.PIPE)
        a.stdout.close()
        c = ff(["-y", "-f", "f32le", "-ar", "48000", "-ac", "1", "-i", "-", "-af", post,
                "-ar", str(SR), "-c:a", "pcm_f32le", out], stdin=b.stdout)
        b.stdout.close()
        for p in (c, b, a):
            assert p.wait() == 0
    else:
        assert ff(["-y", "-i", inp, "-af", af, "-ar", str(SR), "-ac", "1",
                   "-c:a", "pcm_f32le", out]).wait() == 0


def align(ref, y, max_ms=50):
    m, n = int(max_ms * SR / 1000), min(len(ref), len(y))
    c = correlate(y[:n], ref[:n], mode="full", method="fft")
    lag = int(np.argmax(c[n - 1 - m:n + m]) - m)
    y = y[lag:] if lag > 0 else np.concatenate([np.zeros(-lag, np.float32), y])
    return np.pad(y[:len(ref)], (0, max(0, len(ref) - len(y))))


def evaluate(y, clean, segs, mask, noisy):
    y = align(clean, y)
    res, levels = {}, []
    for a, b, lb in segs:
        res[f"estoi_{lb}"] = round(float(stoi(clean[a:b], y[a:b], SR, extended=True)), 3)
        levels.append(rms_db(y[a:b]))
    res["level_spread_db"] = round(max(levels) - min(levels), 1)

    def gain(a, b):   # גבר לינארי אופטימלי מוצא/ייחוס — בלתי תלוי בנורמליזציה
        return float(np.dot(y[a:b], clean[a:b]) / (np.dot(clean[a:b], clean[a:b]) + 1e-12))
    res["weak_vs_strong_db"] = round(20 * np.log10(max(gain(*segs[2][:2]), 1e-9)
                                                   / max(gain(*segs[0][:2]), 1e-9)), 1)
    nospeech = mask > 0
    for a, b, _ in segs:
        nospeech[a:b] = False
    res["noise_in_gaps_db"] = round(rms_db(y[nospeech]) - rms_db(noisy[nospeech]), 1)
    return res


def main():
    os.makedirs(OUT, exist_ok=True)
    report = {}
    for scen, open_sq in (("squelch", False), ("atis_open", True)):
        noisy, clean, segs, mask = build_scene(open_sq, np.random.default_rng(1234))
        inp = os.path.join(OUT, f"{scen}_input.wav")
        sf.write(inp, noisy, SR, subtype="FLOAT")
        sf.write(os.path.join(OUT, f"{scen}_clean.wav"), clean, SR, subtype="FLOAT")
        report[scen] = {}
        for name, af in PIPELINES.items():
            out = os.path.join(OUT, f"{scen}_{name.split(' ')[0]}.wav")
            t0 = time.perf_counter()
            if af is None:
                sf.write(out, noisy, SR, subtype="FLOAT")
            else:
                run_pipeline(af, inp, out)
            res = evaluate(sf.read(out, dtype="float32")[0], clean, segs, mask, noisy)
            res["rtf"] = round((time.perf_counter() - t0) / (len(noisy) / SR), 4)
            report[scen][name] = res
    cols = ["estoi_tower_strong", "estoi_pilot_mid", "estoi_pilot_weak",
            "weak_vs_strong_db", "noise_in_gaps_db", "level_spread_db", "rtf"]
    for scen, rows in report.items():
        print(f"\n== {scen}\n{'pipeline':36}" + "".join(f"{c.replace('estoi_', '')[:13]:>14}" for c in cols))
        for k, v in rows.items():
            print(f"{k:36}" + "".join(f"{v[c]:>14}" for c in cols))
    with open(os.path.join(OUT, "report.json"), "w") as f:
        json.dump(report, f, indent=1)


if __name__ == "__main__":
    main()
