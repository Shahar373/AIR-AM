"""מעבדת ההאזנה (scripts/airam-listening-lab) — docs/audio-comfort-design.md §6.

בלי RNNoise אמיתי: "מסנן" מזויף שמשהה את האות בדיוק N דגימות בודק את מה שבאמת
קריטי — יישור הזמן של הערבוב (בלעדיו: מסנן מסרק) — ואת העיוורון של הדף.
"""
import importlib.machinery
import importlib.util
import json
import math
import random
import shutil
import stat
import sys
import threading
import urllib.request
from array import array
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "airam-listening-lab"

_loader = importlib.machinery.SourceFileLoader("airam_listening_lab", str(SCRIPT))
_spec = importlib.util.spec_from_loader("airam_listening_lab", _loader)
lab = importlib.util.module_from_spec(_spec)
_loader.exec_module(lab)

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg חסר")


def _speechish(n, rate, seed=1):
    """אות דמוי-דיבור: כמה הרמוניות עם מעטפת משתנה + רעש. לא מחזורי (קורלציה חד-משמעית)."""
    rng = random.Random(seed)
    out = array("f")
    for i in range(n):
        t = i / rate
        env = 0.5 + 0.5 * math.sin(2 * math.pi * 3.1 * t) * math.sin(2 * math.pi * 0.7 * t)
        s = sum(math.sin(2 * math.pi * f * t) / k for k, f in enumerate((210, 430, 870, 1320), 1))
        out.append(0.3 * env * s + 0.05 * rng.uniform(-1, 1))
    return out


# --- עקביות מול המוצר --------------------------------------------------------
def test_prod_chain_matches_denoise_script():
    """הבקרה ("prod") חייבת להיות בדיוק השרשרת שהמשתמש שמע ופסל."""
    src = (ROOT / "scripts" / "airam-denoise").read_text(encoding="utf-8")
    parts = [p.split('"')[1] for p in src.splitlines() if p.startswith("POST_AF=")]
    assert len(parts) == 2 and parts[1].startswith("$POST_AF")
    assert lab.PROD_AF == parts[0] + parts[1][len("$POST_AF"):]


def test_rec_name_regex_matches_app():
    import app
    assert lab.REC_NAME_RE.pattern == app._REC_NAME_RE.pattern
    assert lab.REC_BYTES_PER_SEC == app.REC_BYTES_PER_SEC


def test_mix_floor_is_structural():
    """ההבטחה של §4: שום רכיב לא מונחת יותר מ-20·log10(dry), גם כשהמסנן מוחק הכול."""
    dry = array("f", [0.5] * 100)
    wet = array("f", [0.0] * 100)
    for v in lab.VARIANTS:
        if v["kind"] == "rnmix":
            out = lab.mix(dry, wet, 0, v["dry"])
            assert min(out) == pytest.approx(0.5 * v["dry"])
            assert 20 * math.log10(v["dry"]) >= -12.1     # התוויות מבטיחות "עד 6/12dB"


# --- יישור זמן ---------------------------------------------------------------
@pytest.mark.parametrize("lag", [0, 7, 480, 1777])
def test_estimate_lag_finds_pure_delay(lag):
    dry = _speechish(48000, 48000)
    wet = array("f", [0.0] * lag) + dry
    got, conf = lab.estimate_lag(dry, wet)
    assert got == lag and conf > 0.99


def test_estimate_lag_low_confidence_on_unrelated():
    dry = _speechish(48000, 48000, seed=1)
    rng = random.Random(9)
    wet = array("f", [rng.uniform(-1, 1) for _ in range(48000)])
    _, conf = lab.estimate_lag(dry, wet)
    assert conf < lab.LAG_MIN_CONF


def test_consensus_ignores_unreliable_and_refuses_to_guess():
    assert lab.consensus_lag([(480, 0.9), (480, 0.8), (13, 0.95), (999, 0.1)]) == 480
    assert lab.consensus_lag([(480, 0.2), (13, 0.3)]) is None
    assert lab.consensus_lag([]) is None


def test_mix_aligned_has_no_comb():
    """אחרי יישור, ערבוב של אות עם עצמו המושהה = האות עצמו (בלי מסנן מסרק)."""
    lag = 480
    dry = _speechish(9600, 48000)
    wet = array("f", [0.0] * lag) + dry
    out = lab.mix(dry, wet, lag, 0.3)
    body = out[lag:lag + len(dry) - 10]
    assert max(abs(a - b) for a, b in zip(body, dry)) < 1e-6


# --- בחירת הקלטות ------------------------------------------------------------
def test_pick_recordings_round_robin_and_min_length(tmp_path):
    (tmp_path / "saved").mkdir()
    size_ok = int(lab.MIN_SEC * lab.REC_BYTES_PER_SEC) + 10

    def mk(name, size, mtime, sub=""):
        p = tmp_path / sub / name
        p.write_bytes(b"\0" * size)
        import os
        os.utime(p, (mtime, mtime))
        return p

    for k in range(4):
        mk(f"airam_20261113_10000{k}_134600000.mp3", size_ok, 1000 + k)      # מגדל ×4
    mk("airam_20261113_110000_120500000.mp3", size_ok, 900)                  # גישה, ישן יותר
    mk("airam_20261113_120000_121500000.mp3", size_ok, 950, "saved")         # Guard, שמורה
    mk("airam_20261113_130000_121400000.mp3", 100, 2000)                     # קצרה מדי
    mk("notes.mp3", size_ok, 3000)                                           # לא הקלטה שלנו
    got = lab.pick_recordings(tmp_path, 4)
    freqs = [lab.rec_freq(p.name) for p in got]
    assert len(got) == 4
    assert set(freqs[:3]) == {134600000, 120500000, 121500000}               # סבב קודם
    assert 121400000 not in freqs


def test_pick_recordings_missing_dir(tmp_path):
    assert lab.pick_recordings(tmp_path / "nope", 5) == []


# --- תוצאות ועיוורון ---------------------------------------------------------
def _fake_lab():
    return {"recs": [{"name": "a.mp3", "freq_mhz": 134.6, "sec": 5.0, "order": ["prod", "eq", "rn25"]},
                     {"name": "b.mp3", "freq_mhz": 120.5, "sec": 6.0, "order": ["eq", "rn25", "prod"]}],
            "ratings": {"0:0": {"less": True, "unnatural": True},
                        "0:1": {"less": True},
                        "0:2": {"less": True, "missing": True},
                        "1:0": {"less": True},
                        "1:1": {"less": True},
                        "1:2": {"less": True}},
            "done": [0, 1], "skipped": {"sp8": "חסר"}, "rn_lag": 480, "revealed": False}


def test_summary_pass_rule():
    rows = {r["id"]: r for r in lab.summarize(_fake_lab())}
    assert rows["eq"]["clean"] == 2 and rows["eq"]["passes"]
    assert rows["rn25"]["clean"] == 1 and not rows["rn25"]["passes"]       # חצי ≠ רוב
    assert rows["prod"]["unnatural"] == 1 and not rows["prod"]["passes"]


def test_summary_counts_only_done_recordings():
    fl = _fake_lab()
    fl["done"] = [1]
    rows = {r["id"]: r for r in lab.summarize(fl)}
    assert all(r["n"] == 1 for r in rows.values())


def test_public_view_is_blind_until_reveal():
    fl = _fake_lab()
    blob = json.dumps(lab.public_view(fl), ensure_ascii=False)
    for v in lab.VARIANTS:
        assert v["label"] not in blob and f'"{v["id"]}"' not in blob
    fl["revealed"] = True
    view = lab.public_view(fl)
    assert view["recs"][0]["labels"][0] == lab.VARIANT_BY_ID["prod"]["label"]
    assert "summary" in view and "text" in view


def test_parse_range():
    assert lab.parse_range("bytes=0-1", 10) == (0, 1)
    assert lab.parse_range("bytes=5-", 10) == (5, 9)
    assert lab.parse_range("bytes=-3", 10) == (7, 9)
    assert lab.parse_range("bytes=4-99", 10) == (4, 9)
    assert lab.parse_range("bytes=20-", 10) is None
    assert lab.parse_range("bytes=-", 10) is None
    assert lab.parse_range(None, 10) is None


# --- שרת ---------------------------------------------------------------------
@pytest.fixture
def server(tmp_path):
    fl = _fake_lab()
    lab.save_lab(tmp_path, fl)
    (tmp_path / "r0_1.mp3").write_bytes(b"ID3abcdefgh")
    (tmp_path / "secret.txt").write_text("x")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), lab.make_handler(tmp_path, threading.Lock()))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", tmp_path
    srv.shutdown()


def _req(url, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json",
                                                          **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


def test_server_rate_done_reveal(server):
    base, out = server
    code, body, _ = _req(base + "/api/rate", {"rec": 1, "slot": 0, "key": "missing", "value": True})
    assert code == 200 and json.loads(body)["ratings"]["1:0"]["missing"] is True
    assert _req(base + "/api/rate", {"rec": 9, "slot": 0, "key": "less"})[0] == 400
    assert _req(base + "/api/rate", {"rec": 0, "slot": 0, "key": "evil"})[0] == 400
    assert _req(base + "/api/rate", {"rec": 0, "slot": 7, "key": "less"})[0] == 400
    code, body, _ = _req(base + "/api/done", {"rec": 0, "value": False})
    assert json.loads(body)["done"] == [1]
    code, body, _ = _req(base + "/api/reveal", {})
    assert json.loads(body)["revealed"] and (out / "results.txt").exists()
    assert lab.load_lab(out)["ratings"]["1:0"]["missing"] is True             # נשמר לדיסק


def test_server_audio_range_and_no_traversal(server):
    base, _ = server
    code, body, h = _req(base + "/a/r0_1.mp3", headers={"Range": "bytes=0-2"})
    assert code == 206 and body == b"ID3" and h["Content-Range"] == "bytes 0-2/11"
    assert _req(base + "/a/r0_1.mp3")[0] == 200
    assert _req(base + "/a/secret.txt")[0] == 404
    assert _req(base + "/a/..%2Flab.json")[0] == 404
    assert _req(base + "/a/r0_9.mp3")[0] == 404


def test_page_served(server):
    base, _ = server
    code, body, _ = _req(base + "/")
    assert code == 200 and "מעבדת האזנה" in body.decode()


# --- בנייה מקצה לקצה (ffmpeg אמיתי, "RNNoise" מזויף = השהיה) ------------------
FAKE_LAG = 480


def _fake_rnnoise(tmp_path):
    p = tmp_path / "fake-rnnoise"
    p.write_text(f"#!{sys.executable}\n"
                 "import sys\n"
                 f"sys.stdout.buffer.write(b'\\0' * 4 * {FAKE_LAG} + sys.stdin.buffer.read())\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


@needs_ffmpeg
def test_build_end_to_end(tmp_path):
    recs = tmp_path / "rec"
    recs.mkdir()
    pcm = _speechish(16000 * 5, 16000).tobytes()
    paths = []
    for k, f in enumerate((134600000, 120500000)):
        p = recs / f"airam_20261113_10000{k}_{f}.mp3"
        lab.to_mp3(pcm, 16000, p)
        paths.append(p)
    out = tmp_path / "out"
    built = lab.build(paths, out, _fake_rnnoise(tmp_path), rng=random.Random(3), log=lambda *a: None)
    assert built["rn_lag"] == FAKE_LAG
    built_ids = {v["id"] for v in lab.VARIANTS} - set(built["skipped"])
    assert {"eq", "rn50", "rn25", "prod"} <= built_ids
    for i, rec in enumerate(built["recs"]):
        assert sorted(rec["order"]) == sorted(built_ids)
        assert (out / f"r{i}_raw.mp3").stat().st_size > 1000
        for s in range(len(rec["order"])):
            assert (out / f"r{i}_{s}.mp3").stat().st_size > 1000
    assert built["recs"][0]["order"] != built["recs"][1]["order"] or len(built_ids) < 3
    assert lab.load_lab(out)["recs"] == built["recs"]


@needs_ffmpeg
def test_build_without_rnnoise_skips_not_fails(tmp_path):
    p = tmp_path / "airam_20261113_100000_134600000.mp3"
    lab.to_mp3(_speechish(16000 * 4, 16000).tobytes(), 16000, p)
    built = lab.build([p], tmp_path / "out", str(tmp_path / "missing"), log=lambda *a: None)
    assert {"rn50", "rn25", "prod"} <= set(built["skipped"])
    assert "eq" in built["recs"][0]["order"]


@pytest.mark.skipif(lab.load_speex() is None, reason="libspeexdsp חסר")
def test_speex_reduces_noise_and_respects_cap():
    rng = random.Random(5)
    noise = array("f", [0.1 * rng.uniform(-1, 1) for _ in range(16000 * 3)])
    out = lab.floats(lab.speex_denoise(lab.load_speex(), noise.tobytes(), -8))
    tail_in = sum(x * x for x in noise[16000:]) / 32000
    tail_out = sum(x * x for x in out[16000:]) / 32000
    drop_db = 10 * math.log10(tail_out / tail_in)
    assert -12 < drop_db < -2          # הנחית, ולא הרבה מעבר לתקרה של 8dB
