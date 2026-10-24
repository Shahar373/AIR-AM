"""מדידת מסנן הערוץ של rtl_airband במצב AM — הבסיס ל-docs/voice-channel-design.md.

מריץ את rtl_airband האמיתי (v5.2.0, נבנה עם -DNFM=ON) על קלט IQ סינתטי (type="file"),
וקולט את פלט ה-udp_stream (f32 16kHz — *לפני* MP3). מקטעים (כל אחד 0.6ש'):
2×חימום | טונים 300..5000Hz (m=0.5) | נשא בלבד (רעש) | נשא + שכן חזק פי 10 (‎+20dB)
ב-8.33k / 12.5k / 25k עם טון 777Hz. מדדים: תגובת תדר מול 1kHz, רעש בשמע מול אות
1kHz, דליפת השכן (כל ההספק בשמע — צפצוף ודיבור), ושבר של שקט מוחלט (סקוולץ' נסגר).

⚠ מגבלות: קלט u8 (רעש קוונטיזציה) ⇒ השוואת *רעש* בין קצבי דגימה שונים לא תקפה
(הוא מתרכז בפס צר יותר); בתוך אותו קצב — תקפה. אות סינתטי, לא RF אמיתי.

הרצה:  python3 measure.py /path/to/rtl_airband"""
import numpy as np, socket, subprocess, os, sys
S = os.path.dirname(os.path.abspath(__file__))
RTL = sys.argv[1] if len(sys.argv) > 1 else "rtl_airband"
OUT = os.path.join(S, "data")
os.makedirs(OUT, exist_ok=True)
TONES = [300, 1000, 2500, 3000, 3500, 4000, 5000]
INTERF = [8333, 12500, 25000]
SEG = 0.6
OFF_MHZ = 0.3
A_DES, A_INT = 0.05, 0.5          # המפריע חזק פי 10 (‎+20dB)

def segments():
    return ([("warm", None)] * 2 + [("tone", t) for t in TONES] + [("carrier", None)]
            + [("interf", f) for f in INTERF])

def make_iq(path, fs):
    rng = np.random.default_rng(5); t0 = 0
    with open(path, "wb") as f:
        for kind, v in segments():
            n = int(fs * SEG); t = (t0 + np.arange(n)) / fs; t0 += n
            mod = 0.5 * np.sin(2*np.pi*v*t) if kind == "tone" else 0.0
            sig = A_DES * (1 + mod) * np.exp(2j*np.pi*-OFF_MHZ*1e6*t)
            if kind == "interf":
                sig = sig + A_INT * (1 + 0.5*np.sin(2*np.pi*777*t)) * np.exp(2j*np.pi*(-OFF_MHZ*1e6 + v)*t)
            sig = sig + (rng.standard_normal(n) + 1j*rng.standard_normal(n)) * 0.004
            iq = np.empty(2*n, np.uint8)
            iq[0::2] = np.clip(127.5 + 127*sig.real, 0, 255); iq[1::2] = np.clip(127.5 + 127*sig.imag, 0, 255)
            f.write(iq.tobytes())

def run(fs, fft, bw):
    global OFF_MHZ
    OFF_MHZ = 0.3 if fs >= 2e6 else 0.1   # בחלון צר: הסטה קטנה יותר מה-DC
    iq = os.path.join(OUT, f"iq_{int(fs)}_{OFF_MHZ}.u8")
    if not os.path.exists(iq): make_iq(iq, fs)
    bwl = f"bandwidth = {bw};" if bw else ""
    cf = os.path.join(OUT, "c.conf"); open(cf, "w").write(f"""fft_size = {fft};
devices: ( {{ type = "file"; filepath = "{iq}"; speedup_factor = 1.0; sample_rate = {fs/1e6};
  mode = "multichannel"; centerfreq = {132.5 + OFF_MHZ}; channels: ( {{ freq = 132.5; modulation = "am";
  squelch_snr_threshold = 0; {bwl} outputs: ( {{ type = "udp_stream"; dest_address = "127.0.0.1";
  dest_port = 15602; continuous = true; }} ); }} ); }} );""")
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind(("127.0.0.1", 15602)); s.settimeout(3)
    buf = []; p = subprocess.Popen([RTL, "-f", "-e", "-c", cf], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        while True:
            try: buf.append(np.frombuffer(s.recv(65536), np.float32))
            except socket.timeout: break
    finally: p.kill(); s.close()
    y = np.concatenate(buf); w = int(16000*SEG); segs = segments()
    def seg(k): return y[k*w + w//4:(k+1)*w - w//4]
    def tone_db(x, f):
        sp = np.abs(np.fft.rfft(x*np.hanning(len(x)))); fr = np.fft.rfftfreq(len(x), 1/16000)
        return 20*np.log10(sp[np.argmin(abs(fr-f))] + 1e-12)
    def pwr(x): x = x - x.mean(); return 10*np.log10(np.mean(x**2) + 1e-15)
    idx = {(k, v): i for i, (k, v) in enumerate(segs)}
    ref_db = tone_db(seg(idx[("tone", 1000)]), 1000); ref_p = pwr(seg(idx[("tone", 1000)]))
    resp = {t: round(tone_db(seg(idx[("tone", t)]), t) - ref_db, 1) for t in TONES}
    noise = round(pwr(seg(idx[("carrier", None)])) - ref_p, 1)            # רעש ביחס לאות 1kHz m=0.5
    leak = {f: round(pwr(seg(idx[("interf", f)])) - ref_p, 1) for f in INTERF}  # כל ההפרעה (צפצוף+דיבור)
    zeros = {f: round(float(np.mean(seg(idx[("interf", f)]) == 0)), 2) for f in INTERF}
    return resp, noise, leak, zeros

if __name__ == "__main__":
    for fs, fft, bw, name in [(2.56e6, 512, 0, "standard"), (2.56e6, 512, 8000, "bw8k-only"),
                              (2.56e6, 1024, 0, "narrow"), (2.56e6, 1024, 8000, "narrow8k"),
                              (2.56e6, 2048, 0, "fft2048"), (2.56e6, 256, 0, "fft256"),
                              (2.0e6, 256, 0, "2M/fft256"), (768e3, 256, 0, "768k/fft256"),
                              (1.0e6, 512, 0, "1M/fft512")]:
        resp, noise, leak, zeros = run(fs, fft, bw)
        print(f"{name:10} | resp(dB vs 1k): " + " ".join(f"{t}:{v}" for t, v in resp.items())
              + f" | noise:{noise} | leak(+20dB neighbor): " + " ".join(f"{f/1000:g}k:{v}" for f, v in leak.items()) + f" | zero-frac:{zeros}", flush=True)
