# ============================================================================
#  AIR-AM - סימולטור RF ל-🩺 בדיקת ה-RF (PR 2; spec §10.1)
# ----------------------------------------------------------------------------
#  שני חלקים:
#   (1) IQ סינתטי ב-2.56Msps סביב מרכז fc, הערוץ ב-‎−300kHz: רעש אנטנה, נשא AM
#       עם אפנון דמוי-דיבור (רעש בפס 300–2500Hz × שער הברות 3–5Hz, m_peak 0.85),
#       לוח PTT, דעיכה איטית, תחנה שנייה, החלפת דובר, וחוסמים CW (‎+100/‎+500kHz =>
#       IM3 ‏2f1−f2 נוחת בדיוק על ‎−300kHz). קצה קדמי פר-מצב LNA: רווח לפי טבלת
#       ה-GR + שגיאה מוזרקת, אי-ליניאריות Rapp, רעש מקלט אחרי ה-LNA, IFGR, רעש ADC,
#       קוונטיזציה ל-int16 עם מסילה ניתנת להגדרה, והדמיית אירועי עומס.
#   (2) FakeRadio + FakeBackend — אותו ממשק כמו SoapyRadio/SoapyBackend בבודק, על
#       שעון וירטואלי, עם *אותה* מכניקת טבעת כמו SoapySDRPlay3 ‏48bd8b4:
#         * חבילות של PKT דגימות; מאגר נסגר כשהחבילה הבאה לא נכנסת
#           (Streaming.cpp:108) => 65 חבילות = 65520 דגימות למאגר;
#         * overflow כשהמאגר ה-8 נסגר בלי שנקרא (Streaming.cpp:101-118); ה-read
#           הבא מרוקן הכול ומחזיר OVERFLOW (‎:505-521), והמאגר הבא מתחיל מהחבילה
#           הראשונה שאחרי הריקון;
#         * read(timeout=0) => TIMEOUT כשאין מאגר מלא (‎:524-531); timeout>0 מקדם
#           זמן וירטואלי עד שמאגר נסגר (cond.wait_for);
#         * setGain: no-op כשהערך לא השתנה (Settings.cpp:586,599); אחרת השינוי חל
#           מהחבילה הראשונה אחרי latency, ו-set_gain "חוסם" בזמן וירטואלי עד
#           החבילה הזו (grChanged, Settings.cpp:605-621); IFGR מתעלם תחת AGC
#           (‎:584-595); כשל => "Gain reduction update timeout." אחרי 500ms;
#         * rfnotch_ctrl: Update בלי המתנה (Settings.cpp:1777-1783).
#  ⚠ PKT=1008 הוא ערך המודל של ה-spec (§10.1), לא נמדד על החומרה — הבדיקות לא
#  תלויות בו (המאגר נגזר ממנו לפי אותו כלל של המקור).
#  מצב marker: האמפליטודה מקודדת (מזהה-שינוי, LNA, IFGR) — כך בדיקה מוכיחה שאף
#  דגימה מלפני ההחלפה לא נמדדה.
# ============================================================================
import bisect
import heapq
import math

import numpy as np

RATE = 2_560_000
PKT = 1008
BUFFER_SHORTS = 65536 * 2                       # bufferLength ל-CS16 (Streaming.cpp:247-252)
NUM_BUFFERS = 8                                 # SoapySDRPlay.hpp:45
GR = (0, 6, 12, 18, 20, 26, 32, 38, 57, 62)     # spec.txt:2287-2293 (RSP1B, 60–420MHz)
TIMEOUT, OVERFLOW, NOT_SUPPORTED = -1, -4, -5    # SoapySDR Errors.h:33,50,56
LOG_ERROR, LOG_WARNING, LOG_INFO, LOG_SSI = 3, 4, 6, 9


def _pkts_per_buffer(pkt=PKT):
    """כלל הסגירה של rx_callback (Streaming.cpp:108): חבילה נכנסת למאגר הנוכחי רק
    אם size+spaceReqd < bufferLength; אחרת המאגר נסגר והיא פותחת את הבא."""
    size, k = 0, 0
    while size + 2 * pkt < BUFFER_SHORTS:
        size += 2 * pkt
        k += 1
    return k


PKTS_PER_BUF = _pkts_per_buffer()               # 65
BUF = PKTS_PER_BUF * PKT                        # 65520


def db2a(db):
    return 10.0 ** (db / 20.0)


def db2p(db):
    return 10.0 ** (db / 10.0)


# ============================================================================
#  (1) IQ סינתטי
# ============================================================================
class NoiseBank:
    """בנק רעש מרוכב (הספק 1) שנחתך לפי מיקום מוחלט — דטרמיניסטי ומהיר."""

    def __init__(self, seed=1, size=1 << 20):
        rng = np.random.default_rng(seed)
        self.z = ((rng.standard_normal(size) + 1j * rng.standard_normal(size))
                  / math.sqrt(2.0)).astype(np.complex64)
        self.size = size

    def get(self, s0, n, salt=0):
        off = (int(s0) * 2654435761 + salt * 40503 + 12345) % (self.size - n)
        return self.z[off:off + n]


_SPEECH = {}


def speech_table(seed=3, fs_a=16000, seconds=8.0):
    """אפנון דמוי-דיבור מחזורי, שיא 1: רעש בפס 300–2500Hz × שער הברות 3–5Hz."""
    key = (seed, fs_a, seconds)
    if key in _SPEECH:
        return _SPEECH[key]
    n = int(fs_a * seconds)
    rng = np.random.default_rng(seed)
    x = rng.standard_normal(n)
    X = np.fft.rfft(x)
    f = np.fft.rfftfreq(n, 1.0 / fs_a)
    X[(f < 300.0) | (f > 2500.0)] = 0.0
    band = np.fft.irfft(X, n)
    t = np.arange(n) / fs_a
    syl = round(rng.uniform(3.0, 5.0) * seconds) / seconds          # מחזורי בטבלה
    wob = 1.0 / seconds
    ph = 2 * np.pi * syl * t + 0.8 * np.sin(2 * np.pi * wob * t) + rng.uniform(0, 2 * np.pi)
    gate = 0.5 * (1.0 - np.cos(ph))
    m = band * gate
    m /= np.max(np.abs(m))
    _SPEECH[key] = (m, fs_a)
    return _SPEECH[key]


class Carrier:
    """נשא AM. ptt=None => רציף (ATIS); אחרת רשימת (t_on, t_off) בשניות-זרם.
    changes: [(t, d_level_db, m_peak_new)] — "החלפת דובר" באמצע שידור."""

    def __init__(self, level_dbfs, offset_hz=-300e3, m_peak=0.85, ptt=None,
                 fade_db=0.0, fade_hz=0.2, speech_seed=3, speech_offset_s=0.0,
                 changes=(), phase=0.3):
        self.level_dbfs = level_dbfs
        self.offset_hz = offset_hz
        self.m_peak = m_peak
        self.ptt = list(ptt) if ptt is not None else None
        self.fade_db = fade_db
        self.fade_hz = fade_hz
        self.speech_seed = speech_seed
        self.speech_offset_s = speech_offset_s
        self.changes = sorted(changes)
        self.phase = phase

    def on_mask(self, t):
        if self.ptt is None:
            return None
        m = np.zeros(t.shape, dtype=bool)
        for a, b in self.ptt:
            m |= (t >= a) & (t < b)
        return m

    def __call__(self, s0, n, fs=RATE):
        t = (s0 + np.arange(n)) / fs
        gate = self.on_mask(t)
        if gate is not None and not gate.any():
            return None
        tab, fs_a = speech_table(self.speech_seed)
        L = tab.size
        xq = np.mod((t + self.speech_offset_s) * fs_a, L)
        m = np.interp(xq, np.arange(L + 1), np.append(tab, tab[0]))
        level = np.full(n, self.level_dbfs)
        mp = np.full(n, self.m_peak)
        for tc, dl, mnew in self.changes:
            sel = t >= tc
            level[sel] += dl
            mp[sel] = mnew
        amp = db2a(level) * (1.0 + mp * m)
        if self.fade_db:
            amp *= db2a(self.fade_db * np.sin(2 * np.pi * self.fade_hz * t))
        sig = amp * np.exp(1j * (2 * np.pi * self.offset_hz * t + self.phase))
        if gate is not None:
            sig *= gate
        return sig


class Tone:
    """חוסם CW."""

    def __init__(self, offset_hz, level_dbfs, phase=1.1):
        self.offset_hz = offset_hz
        self.level_dbfs = level_dbfs
        self.phase = phase

    def __call__(self, s0, n, fs=RATE):
        t = (s0 + np.arange(n)) / fs
        return db2a(self.level_dbfs) * np.exp(1j * (2 * np.pi * self.offset_hz * t + self.phase))


def ptt_schedule(seed, total_s, on=(2.0, 5.0), off=(3.0, 10.0), first_off=None):
    rng = np.random.default_rng(seed)
    t = rng.uniform(*off) if first_off is None else first_off
    out = []
    while t < total_s:
        d = rng.uniform(*on)
        out.append((t, t + d))
        t += d + rng.uniform(*off)
    return out


class Scene:
    """הסביבה באנטנה, ביחידות full-scale *בנקודת הייחוס* (LNA ref, IFGR ref).
    noise_dbfs: הספק הרעש המרוכב לדגימה. oob_dbfs: הספק חוסם מחוץ לחלון (FM וכד')
    שלא מגיע ל-ADC אבל מעמיס את ה-LNA (נכנס ל-drive של Rapp)."""

    def __init__(self, noise_dbfs=-60.0, carriers=(), tones=(), oob_dbfs=None, seed=1):
        self.noise_dbfs = noise_dbfs
        self.carriers = list(carriers)
        self.tones = list(tones)
        self.oob_dbfs = oob_dbfs
        self.bank = NoiseBank(seed)

    def antenna(self, s0, n):
        x = self.bank.get(s0, n).astype(np.complex128) * db2a(self.noise_dbfs)
        for c in self.carriers + self.tones:
            v = c(s0, n)
            if v is not None:
                x = x + v
        return x


class FrontEnd:
    """קצה קדמי פר-מצב LNA (spec §10.1): רווח GR[ref]−GR[s]+err[s] יחסית לייחוס, Rapp
    (sat_dbfs=None => ליניארי), רעש מקלט אחרי ה-LNA, IF ‏−(IFGR−ref), רעש ADC,
    קוונטיזציה ל-int16 עם מסילה. ovl_dbfs: שיא לפני ה-ADC מעליו "עומס" (הדמיה)."""

    def __init__(self, err=None, ref_lna=4, ref_ifgr=40, sat_dbfs=None, rapp_p=2.0,
                 rx_noise_dbfs=None, adc_noise_dbfs=-80.0, rail=32767, ovl_dbfs=None,
                 notch_il_db=0.5, notch_oob_atten_db=30.0, seed=7):
        self.err = list(err) if err is not None else [0.0] * 10
        self.ref_lna = ref_lna
        self.ref_ifgr = ref_ifgr
        self.sat_dbfs = sat_dbfs
        self.rapp_p = rapp_p
        self.rx_noise_dbfs = rx_noise_dbfs
        self.adc_noise_dbfs = adc_noise_dbfs
        self.rail = rail
        self.ovl_dbfs = ovl_dbfs
        self.notch_il_db = notch_il_db
        self.notch_oob_atten_db = notch_oob_atten_db
        self.bank = NoiseBank(seed)

    def lna_db(self, lna):
        return GR[self.ref_lna] - GR[lna] + self.err[lna]

    def process(self, x, lna_db, ifgr, notch, s0, oob_dbfs=None, pkt=None):
        """x: מרוכב באנטנה. lna_db: סקלר או מערך (התייצבות τ). => (int16 משולב, שיא לפני
        ה-ADC — סקלר, או מערך שיא-לחבילה כש-pkt נתון)."""
        y = x * db2a(lna_db)
        if notch:
            y = y * db2a(-self.notch_il_db)
        if self.sat_dbfs is not None:
            drive = np.abs(y) ** 2
            if oob_dbfs is not None:
                drive = drive + db2p(oob_dbfs + lna_db
                                     - (self.notch_oob_atten_db if notch else 0.0))
            a2 = db2p(self.sat_dbfs)
            p = self.rapp_p
            y = y / (1.0 + (drive / a2) ** p) ** (1.0 / (2 * p))
        n = x.shape[0]
        if self.rx_noise_dbfs is not None:
            y = y + self.bank.get(s0, n, salt=1) * db2a(self.rx_noise_dbfs)
        y = y * db2a(-(ifgr - self.ref_ifgr))
        if self.adc_noise_dbfs is not None:
            y = y + self.bank.get(s0, n, salt=2) * db2a(self.adc_noise_dbfs)
        if pkt:
            peak = np.abs(y).reshape(-1, pkt).max(axis=1)
        else:
            peak = float(np.max(np.abs(y))) if n else 0.0
        return quantize(y, self.rail), peak


def quantize(y, rail=32767):
    """מרוכב (full-scale=1) => int16 משולב I,Q. מסילה 32767 => טווח int16 הטבעי
    (כולל ‎−32768); מסילה אחרת => ±rail."""
    lo, hi = (-32768, 32767) if rail >= 32767 else (-rail, rail)
    out = np.empty(2 * y.shape[0], dtype=np.int16)
    out[0::2] = np.clip(np.rint(y.real * 32767.0), lo, hi)
    out[1::2] = np.clip(np.rint(y.imag * 32767.0), lo, hi)
    return out


def iq(scene, fe, s0, n, lna=4, ifgr=40, notch=False):
    """קיצור לבדיקות DSP: IQ מקוונטז במצב קבוע."""
    codes, _ = fe.process(scene.antenna(s0, n), fe.lna_db(lna), ifgr, notch, s0, scene.oob_dbfs)
    return codes


# ============================================================================
#  (2) FakeRadio / FakeBackend
# ============================================================================
class FakeRadio:
    """מכשיר מדומה על שעון וירטואלי. ממשק זהה ל-SoapyRadio (webtune/rfcheck_probe.py).

    הזרקות: gr_timeout_on (אינדקסים של עדכוני רווח אפקטיביים), overflow_on_reads
    (מספרי קריאות, 1-based), not_supported_at_read, remove_at (זמן — שורת "Device has
    been removed" + המאגרים נעצרים, כמו במקור), notch_readback (ערך קבוע), bias_t
    (readback התחלתי), patched=False (מודול בלי ה-patch: אין שורות AIRAM_RF)."""

    def __init__(self, scene=None, fe=None, *, marker=False, hardware="RSP1B", patched=True,
                 bias_t="false", notch_readback=None, gain_latency_s=0.002,
                 notch_latency_s=0.001, settle_tau_s=0.0, gr_timeout_on=(),
                 overflow_on_reads=(), not_supported_at_read=None, remove_at=None,
                 start_time=1000.0, cb_latency_s=0.0003, agc_setpoint_dbfs=-30.0,
                 agc_step_db=3.0):
        self.scene = scene if scene is not None else Scene()
        self.fe = fe if fe is not None else FrontEnd()
        self.marker = marker
        self.hardware = hardware
        self.patched = patched
        self.bias_t = bias_t
        self.notch_readback = notch_readback
        self.gain_latency_s = gain_latency_s
        self.notch_latency_s = notch_latency_s
        self.settle_tau_s = settle_tau_s
        self.gr_timeout_on = set(gr_timeout_on)
        self.overflow_on_reads = set(overflow_on_reads)
        self.not_supported_at_read = not_supported_at_read
        self.remove_at = remove_at
        self.cb = cb_latency_s
        self.agc_setpoint_dbfs = agc_setpoint_dbfs
        self.agc_step_db = agc_step_db
        self.now = float(start_time)
        self.calls = []            # (kind, ...args, t)
        self.logs = []             # (t, level, msg)
        self.settings = {}
        self.cfg = {"lna": 0, "ifgr": 50, "notch": False, "agc": True, "cid": 0}
        self.opened = False
        self.closed = False
        self.streaming = False
        self.removed = False
        self.reads = 0
        self.gain_updates = 0
        self.buffers_generated = 0
        self.overflows = 0
        self._log_cb = None
        self._sched_pkts = []
        self._sched_cfgs = []
        self._ring = []
        self._fill = 0
        self._dropping = False
        self._ovf_pending = False
        self._t0s = None
        self._pending_logs = []
        self._ovl_state = False
        self._agc_ifgr = None
        self._last_gain_log = -1e9
        self._last_gain_logged = None

    # ------------------------------------------------------------ שעון
    def clock(self):
        return self.now

    def wall(self):
        return 1.7e9 + self.now

    def advance(self, dt):
        self._advance_to(self.now + dt)

    def backend(self, **kw):
        return FakeBackend(self, **kw)

    def schedule_log(self, t, level, msg):
        heapq.heappush(self._pending_logs, (t, len(self.logs) + len(self._pending_logs), level, msg))

    def _log(self, level, msg):
        self.logs.append((self.now, level, msg))
        if self._log_cb is not None:
            self._log_cb(level, msg)

    def _log_at(self, t, level, msg):
        """אירוע שקרה בזמן t שכבר עבר (חבילה בתוך מאגר שנוצר עכשיו). ה-handler האמיתי
        נקרא מת'רד ה-API *בזמן* האירוע; כאן מחזירים את השעון לרגע הקריאה בלבד, כדי
        שחותמת הזמן שהבודק רושם (clock() בתוך ה-handler) תהיה זמן האירוע."""
        saved = self.now
        self.now = min(saved, t)
        try:
            self._log(level, msg)
        finally:
            self.now = saved

    # ------------------------------------------------------------ תזמון חבילות
    def _T(self, p):
        return self._t0s + (p + 1) * PKT / RATE + self.cb

    def _first_pkt_at_or_after(self, t):
        x = (t - self._t0s - self.cb) * RATE / PKT - 1.0
        p = max(0, int(math.ceil(x - 1e-9)))
        while self._T(p) < t:
            p += 1
        while p > 0 and self._T(p - 1) >= t:
            p -= 1
        return p

    def _next_completion(self):
        if not self.streaming or self._dropping or self.removed:
            return math.inf
        return self._T(self._fill + PKTS_PER_BUF)

    def _advance_to(self, t):
        while True:
            t_c = self._next_completion()
            t_l = self._pending_logs[0][0] if self._pending_logs else math.inf
            t_r = self.remove_at if (self.remove_at is not None and not self.removed) else math.inf
            t_n = min(t_c, t_l, t_r)
            if t_n > t:
                break
            self.now = max(self.now, t_n)
            if t_n == t_r:
                self.removed = True
                self._log(LOG_ERROR, "Device has been removed. Stopping.")   # Streaming.cpp:191
                continue
            if t_n == t_l:
                _, _, level, msg = heapq.heappop(self._pending_logs)
                self._log(level, msg)
                continue
            if len(self._ring) >= NUM_BUFFERS - 1:
                # המאגר ה-8 נסגר בלי שנקרא => overflowEvent; חבילות נזרקות עד ה-acquire
                self._ovf_pending = True
                self._dropping = True
                continue
            self._ring.append(self._gen_buffer(self._fill))
            self._fill += PKTS_PER_BUF
        self.now = max(self.now, t)

    # ------------------------------------------------------------ תצורה
    def _schedule(self, p):
        cfg = dict(self.cfg)
        i = bisect.bisect_right(self._sched_pkts, p)
        # שינוי חדש דורס שינויים מתוזמנים מאוחרים יותר (לא קורה בפועל — הזמן מונוטוני)
        del self._sched_pkts[i:]
        del self._sched_cfgs[i:]
        self._sched_pkts.append(p)
        self._sched_cfgs.append(cfg)

    def _cfg_at(self, p):
        i = bisect.bisect_right(self._sched_pkts, p) - 1
        return self._sched_cfgs[max(i, 0)], (self._sched_cfgs[i - 1] if i >= 1 else None), \
            (self._sched_pkts[i] if i >= 0 else 0)

    def _total_db(self, cfg):
        return self.fe.lna_db(cfg["lna"]) - (cfg["ifgr"] - self.fe.ref_ifgr)

    # ------------------------------------------------------------ יצירת מאגר
    def _gen_buffer(self, p0):
        self.buffers_generated += 1
        s0 = p0 * PKT
        n = BUF
        # גבולות סגמנטים לפי שינויים מתוזמנים בתוך המאגר
        bounds = [p0]
        for p in self._sched_pkts:
            if p0 < p < p0 + PKTS_PER_BUF:
                bounds.append(p)
        bounds.append(p0 + PKTS_PER_BUF)
        out = np.empty(2 * n, dtype=np.int16)
        if self.marker:
            for a, b in zip(bounds[:-1], bounds[1:]):
                cfg, _, _ = self._cfg_at(a)
                ia, ib = (a - p0) * PKT, (b - p0) * PKT
                out[2 * ia:2 * ib:2] = 1000 + cfg["cid"]
                out[2 * ia + 1:2 * ib:2] = 100 * cfg["lna"] + cfg["ifgr"]
            return out
        x = self.scene.antenna(s0, n)
        peaks = np.zeros(PKTS_PER_BUF)
        agc_any = False
        for a, b in zip(bounds[:-1], bounds[1:]):
            cfg, prev, p_apply = self._cfg_at(a)
            ia, ib = (a - p0) * PKT, (b - p0) * PKT
            if cfg["agc"]:
                agc_any = True
                if self._agc_ifgr is None:
                    self._agc_ifgr = cfg["ifgr"]
                ifgr = self._agc_ifgr
            else:
                ifgr = cfg["ifgr"]
            lna_db = self.fe.lna_db(cfg["lna"])
            if self.settle_tau_s > 0 and prev is not None and not cfg["agc"]:
                # התייצבות אקספוננציאלית של הרווח הכולל מהמצב הקודם
                k = np.arange(s0 + ia, s0 + ib) - p_apply * PKT
                d = self._total_db(prev) - self._total_db(cfg)
                lna_db = lna_db + d * np.exp(-k / (self.settle_tau_s * RATE))
            codes, pk = self.fe.process(x[ia:ib], lna_db, ifgr, cfg["notch"], s0 + ia,
                                        self.scene.oob_dbfs, pkt=PKT)
            out[2 * ia:2 * ib] = codes
            peaks[a - p0:b - p0] = pk
        if self.fe.ovl_dbfs is not None and self.patched:
            # הדמיית PowerOverloadChange: קצה-מצב בחבילה שבה השיא חוצה את הרמה,
            # בזמן ה-callback של אותה חבילה
            thr = db2a(self.fe.ovl_dbfs)
            for j, pk in enumerate(peaks):
                on = bool(pk > thr)
                if on != self._ovl_state:
                    self._ovl_state = on
                    self._log_at(self._T(p0 + j), LOG_INFO, "AIRAM_RF overload=%d" % (1 if on else 0))
        if agc_any:
            p = out.astype(np.float64)
            p_wb = 10 * math.log10(max(np.dot(p, p) / n / 32767.0 ** 2, 1e-20))
            step = max(-self.agc_step_db, min(self.agc_step_db, p_wb - self.agc_setpoint_dbfs))
            self._agc_ifgr = int(max(20, min(59, round(self._agc_ifgr + step))))
            val = (self._agc_ifgr, GR[self.cfg["lna"]])
            if self.patched and val != self._last_gain_logged and self.now - self._last_gain_log >= 1.0:
                self._last_gain_log = self.now
                self._last_gain_logged = val
                self._log(LOG_INFO, "AIRAM_RF gain grdb=%d lna_grdb=%d" % val)
        return out

    # ------------------------------------------------------------ ממשק SoapyRadio
    def _open(self, notch_base):
        self.opened = True
        self.calls.append(("open", notch_base, self.now))
        self.cfg["notch"] = bool(notch_base)

    def read_setting(self, key):
        self.calls.append(("read_setting", key, self.now))
        if key == "rfnotch_ctrl":
            if self.notch_readback is not None:
                return self.notch_readback
            return "true" if self.cfg["notch"] else "false"
        if key == "biasT_ctrl":
            return self.bias_t
        return self.settings.get(key, "")

    def write_setting(self, key, value):
        self.calls.append(("write_setting", key, value, self.now))
        if key == "rfnotch_ctrl":
            nv = value != "false"
            if nv != self.cfg["notch"]:
                self.cfg["notch"] = nv
                self.cfg["cid"] += 1
                if self.streaming:
                    self._schedule(self._first_pkt_at_or_after(self.now + self.notch_latency_s))
        elif key == "biasT_ctrl":
            self.bias_t = "false" if value == "false" else "true"
        else:
            self.settings[key] = value

    def set_sample_rate(self, rate):
        self.calls.append(("set_sample_rate", rate, self.now))

    def set_frequency(self, hz):
        self.calls.append(("set_frequency", hz, self.now))

    def set_freq_correction(self, ppm):
        self.calls.append(("set_freq_correction", ppm, self.now))

    def set_agc(self, on):
        self.calls.append(("set_agc", bool(on), self.now))
        if bool(on) != self.cfg["agc"]:
            self.cfg["agc"] = bool(on)
            self.cfg["cid"] += 1
            if on:
                self._agc_ifgr = self.cfg["ifgr"]
            if self.streaming:
                self._schedule(self._first_pkt_at_or_after(self.now + self.gain_latency_s))

    def set_gain(self, name, value):
        self.calls.append(("set_gain", name, int(value), self.now))
        v = int(value)
        if name == "IFGR":
            if self.cfg["agc"]:
                self._log(LOG_WARNING, "Not updating IFGR gain because AGC is enabled")
                return
            if v == self.cfg["ifgr"]:
                return
            self.cfg["ifgr"] = v
        elif name == "RFGR":
            if v == self.cfg["lna"]:
                return
            self.cfg["lna"] = v
        else:
            return
        self.cfg["cid"] += 1
        if not self.streaming:
            return
        k = self.gain_updates
        self.gain_updates += 1
        if k in self.gr_timeout_on:
            # grChanged לא הגיע בתוך updateTimeout (Settings.cpp:615-623)
            self._advance_to(self.now + 0.5)
            self._log(LOG_WARNING, "Gain reduction update timeout.")
            self._schedule(self._first_pkt_at_or_after(self.now + self.gain_latency_s))
            return
        p = self._first_pkt_at_or_after(self.now + self.gain_latency_s)
        self._schedule(p)
        self._advance_to(self._T(p) + 0.0005)    # polling של 1ms (Settings.cpp:615-620)

    def get_gain(self, name):
        return float(self.cfg["ifgr"] if name == "IFGR" else self.cfg["lna"])

    def start_stream(self):
        self.calls.append(("start_stream", self.now))
        self._log(LOG_INFO, "Using format CS16.")          # Streaming.cpp:251
        if self.patched:
            self._log(LOG_INFO, "AIRAM_RF stream=start")    # ה-patch: לפני sdrplay_api_Init
        self.streaming = True
        self._t0s = self.now
        self._sched_pkts = []
        self._sched_cfgs = []
        self._schedule(0)
        self._fill = 0

    def read(self, buf, n, timeout_us):
        self.reads += 1
        if self.not_supported_at_read is not None and self.reads >= self.not_supported_at_read:
            return NOT_SUPPORTED
        if self.reads in self.overflow_on_reads:
            self._ovf_pending = True
        if self._ovf_pending:
            # acquireReadBuffer: ריקון מלא + OVERFLOW (Streaming.cpp:505-521)
            self.overflows += 1
            self._ring.clear()
            self._ovf_pending = False
            self._dropping = False
            self._fill = self._first_pkt_at_or_after(self.now)
            self._log(LOG_SSI, "O")
            return OVERFLOW
        if not self._ring and timeout_us > 0:
            deadline = self.now + timeout_us / 1e6
            t_c = self._next_completion()
            if t_c <= deadline:
                self._advance_to(t_c)
            if not self._ring:
                self._advance_to(deadline)
        if not self._ring:
            return TIMEOUT
        data = self._ring.pop(0)
        m = data.size // 2
        if n < m:
            raise NotImplementedError("FakeRadio: קריאה חלקית לא ממומשת (הבודק קורא 65536)")
        buf[:2 * m] = data
        return m

    def get_sample_rate(self):
        return float(RATE)

    def get_bandwidth(self):
        return 1536000.0

    def stream_formats(self):
        return ["CS16", "CF32"]

    def gain_range(self, name):
        return [20.0, 59.0, 0.0] if name == "IFGR" else [0.0, 9.0, 0.0]

    def setting_keys(self):
        return ["rfgain_sel", "iqcorr_ctrl", "agc_setpoint", "biasT_ctrl", "rfnotch_ctrl",
                "dabnotch_ctrl"]

    def info(self):
        hwv = {"RSP1B": "6", "RSP1A": "255"}.get(self.hardware, "1")
        return {"driver": "sdrplay", "hardware": self.hardware,
                "info": {"sdrplay_api_api_version": "3.150000", "sdrplay_api_hw_version": hwv}}

    def close(self):
        self.calls.append(("close", self.now))
        self.closed = True
        self.streaming = False


class FakeBackend:
    """אותו ממשק כמו SoapyBackend בבודק."""

    def __init__(self, radio, load_error=False, handler_ok=True, open_error=False):
        self.radio = radio
        self.load_error = load_error
        self.handler_ok = handler_ok
        self.open_error = open_error
        self.open_calls = 0
        self.log_level = None
        self.unregistered = False

    def load(self):
        if self.load_error:
            raise ImportError("SoapySDR")

    def versions(self):
        return {"soapy_api": "0.8.0", "soapy_lib": "0.8.1-fake"}

    def set_log_level_info(self):
        self.log_level = LOG_INFO

    def register_log_handler(self, cb):
        if not self.handler_ok:
            return False
        self.radio._log_cb = cb
        return True

    def unregister_log_handler(self):
        self.radio._log_cb = None
        self.unregistered = True

    def open(self, notch_base):
        self.open_calls += 1
        if self.open_error:
            raise RuntimeError("no available RSP devices found")
        self.radio._open(notch_base)
        return self.radio

    def env_info(self):
        return {"search_paths": ["/usr/local/lib/SoapySDR/modules0.8"],
                "modules": ["/usr/local/lib/SoapySDR/modules0.8/libsdrPlaySupport.so"]}


# ============================================================================
#  (3) הרצה קצה-לקצה: הבודק האמיתי מול FakeRadio
# ============================================================================
NB_DEFAULT = (-85000, -70000, -55000, -40000, 40000, 55000, 70000, 85000)


def probe_params(**kw):
    """פרמטרים תקינים לבודק (סכמת spec §4.2 + עד 7 מצבים). ברירת מחדל: מגדל 132.5,
    מצבים 0/4/7 עם IFGR מפוצה מ-(4, 40) לפי טבלת ה-GR."""
    p = {"v": 1, "run_id": "0123456789abcdef", "phase": "lna", "ref": "tower",
         "freq_hz": 132_500_000, "center_hz": 132_800_000, "rate": RATE,
         "states": [0, 4, 7], "ifgr_start": {"0": 59, "4": 40, "7": 22},
         "notch_base": False, "notch_alternate": False, "guard_buffers": 1,
         "notch_guard_buffers": 10, "measure_buffers": 7, "ratchet_db": 10,
         "nb_offsets_hz": list(NB_DEFAULT), "rail_code": 32767, "max_sec": 180,
         "ovl_margin_buffers": 1}
    p.update(kw)
    return p


def simulate(params, fake, work_dir, *, marker=True, backend_kw=None, **kw):
    """מריץ את webtune/rfcheck_probe.py מול FakeRadio ומחזיר את הפלטים כפי שהם על
    הדיסק: {code, meta, status, end, diagnose, rows, backend, stderr}. שימושי גם
    לבדיקות קצה-לקצה של rfcheck_analysis (rows/meta/end אמיתיים, לא מפוברקים).
    work_dir מכיל רק out/ ו-build-sig (סימן-הבנייה המדומה)."""
    import io
    import json
    import os
    import rfcheck_probe as rp

    os.makedirs(work_dir, exist_ok=True)
    out = rp.OutDir(os.path.join(work_dir, "out"))
    out.reset()
    mark = os.path.join(work_dir, "build-sig")
    if marker:
        with open(mark, "w") as f:
            f.write("ab" * 32 + "\n")
    elif os.path.exists(mark):
        os.unlink(mark)
    be = fake.backend(**(backend_kw or {}))
    err = io.StringIO()
    code = rp.run_probe(params, be, out, clock=fake.clock, wall=fake.wall, marker_path=mark,
                        api_version_path=os.path.join(work_dir, "no-api-version"),
                        stderr=err, **kw)
    res = {}
    od = os.path.join(work_dir, "out")
    for name in ("meta", "status", "end", "diagnose"):
        f = os.path.join(od, name + ".json")
        res[name] = json.load(open(f)) if os.path.exists(f) else None
    rows = os.path.join(od, "rows.jsonl")
    res["rows"] = [json.loads(ln) for ln in open(rows)] if os.path.exists(rows) else []
    res.update(code=code, backend=be, stderr=err.getvalue())
    return res
