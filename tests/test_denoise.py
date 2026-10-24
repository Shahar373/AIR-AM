"""סינון רעשים חי (airam-denoise) — docs/denoise-design.md.

מה נבדק כאן, ולמה: הסינון הוא שירות *צד* — כל כשל בו חייב להשאיר את הרדיו
(/live.mp3, ההקלטות) בדיוק כמו שהיה, והחיווי חייב לומר "מסונן" רק כשזה נכון.
לכן: (1) פלט ה-UDP ב-render_config תקין ותמיד קיים; (2) _denoise_sync מיישר
לכוונה ולעולם לא זורק; (3) כניסה לקול לא נכשלת בגללו; (4) API/health/state;
(5) ה-proxy של /stream פותח רק את שני ה-mounts הידועים.
systemd/Icecast ממוקפים — בלי חומרה.
"""
import io
import json

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


@pytest.fixture(autouse=True)
def _clean_ice_cache():
    app._ice_status.update(t=0.0, mounts=None)
    yield
    app._ice_status.update(t=0.0, mounts=None)


@pytest.fixture
def installed(monkeypatch):
    """הרכיבים "מותקנים"."""
    monkeypatch.setattr(app, "_denoise_missing", lambda: [])


class Sys:
    """systemd מדומה: מצב יחידות + רישום פעולות start/stop."""

    def __init__(self, monkeypatch, units=None, start_rc=0):
        self.units = {"rtl_airband": "active", app.DENOISE_SERVICE: "inactive", **(units or {})}
        self.calls = []
        self.start_rc = start_rc
        monkeypatch.setattr(app, "_is_active", lambda svc: self.units.get(svc) == "active")
        monkeypatch.setattr(app, "_services_status",
                            lambda names: {n: self.units.get(n, "unknown") for n in names})
        monkeypatch.setattr(app, "_sysctl", self._sysctl)

    def _sysctl(self, action, svc, timeout=45):
        self.calls.append((action, svc))
        if svc == app.DENOISE_SERVICE:
            if action == "start" and self.start_rc == 0:
                self.units[svc] = "active"
            elif action == "stop":
                self.units[svc] = "inactive"

        class R:
            returncode = self.start_rc if action == "start" else 0
            stderr = "boom" if self.start_rc else ""
        return R()


# --- render_config: פלט ה-UDP ------------------------------------------------

def _outputs_block(cfg):
    return cfg[cfg.index("outputs:"):]


@pytest.mark.parametrize("squelch", ["open", "auto", "manual"])
def test_udp_output_always_present(squelch):
    """תמיד בקונפיג — גם כשהמתג כבוי: הדלקה/כיבוי בלי restart לרדיו."""
    cfg = app.render_config(132.5, "am", True, 40, 4, squelch, 9)
    out = _outputs_block(cfg)
    assert 'type = "udp_stream";' in out
    assert f"dest_port = {app.DENOISE_UDP_PORT};" in out
    assert "continuous = true;" in out           # אחרת Icecast מנתק את /clean בשקט
    # כתובת מספרית בלבד: כשל getaddrinfo כאן מפיל את rtl_airband כולו
    assert 'dest_address = "127.0.0.1";' in out
    # הסדר נשמר: icecast (הנתיב הקיים) ראשון
    assert out.index('type = "icecast"') < out.index('type = "udp_stream"')


def test_udp_output_comma_placement_with_and_without_file_output():
    """libconfig: פסיק בין רשומות, בלי פסיק אחרי האחרונה — שני המסלולים."""
    open_cfg = _outputs_block(app.render_config(132.5, "am", True, 40, 4, "open"))
    assert 'type = "file"' not in open_cfg       # ATIS — בלי הקלטה
    # ה-udp הוא האחרון => נסגר בלי פסיק
    tail = open_cfg[open_cfg.index('type = "udp_stream"'):]
    assert tail.split("}", 1)[1].lstrip().startswith(")")
    auto_cfg = _outputs_block(app.render_config(132.5, "am", True, 40, 4, "auto"))
    tail = auto_cfg[auto_cfg.index('type = "udp_stream"'):]
    assert tail.split("}", 1)[1].startswith(",")  # אחריו בא פלט הקובץ
    assert auto_cfg.index('type = "udp_stream"') < auto_cfg.index('type = "file"')
    for sq in ("open", "auto"):
        cfg = app.render_config(132.5, "am", True, 40, 4, sq)   # הקונפיג המלא, לא רק החתך
        assert cfg.count("{") == cfg.count("}")
        assert cfg.count("(") == cfg.count(")")


def test_config_stale_without_udp_output(paths):
    """שדרוג: קונפיג ישן בלי udp_stream נכתב מחדש באתחול (_boot_restore)."""
    old = app.render_config(132.5, "am", True, 40, 4, "auto").replace("udp_stream", "XXX")
    app.CONFIG_PATH.write_text(old)
    assert app._config_stale() is True
    app.CONFIG_PATH.write_text(app.render_config(132.5, "am", True, 40, 4, "auto"))
    assert app._config_stale() is False


# --- פסק-הדין לחיווי ----------------------------------------------------------

@pytest.mark.parametrize("enabled,missing,voice,service,live,expect", [
    (False, [], True, "active", True, "off"),
    (True, ["ffmpeg"], True, "inactive", None, "unavailable"),
    (True, [], False, "inactive", None, "idle"),
    (True, [], True, "active", True, "live"),
    (True, [], True, "activating", False, "starting"),
    (True, [], True, "active", False, "starting"),
    (True, [], True, "active", None, "unknown"),     # Icecast לא ענה — לא טוענים "מסונן"
    (True, [], True, "failed", False, "failed"),
    (True, [], True, "inactive", None, "failed"),
])
def test_denoise_state(enabled, missing, voice, service, live, expect):
    assert app._denoise_state(enabled, missing, voice, service, live) == expect


# --- Icecast status ----------------------------------------------------------

def _fake_urlopen(payload):
    class R(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    calls = []

    def fake(url, timeout=None):
        calls.append(url)
        if isinstance(payload, Exception):
            raise payload
        return R(json.dumps(payload).encode())
    return fake, calls


def test_icecast_live_mounts_list_and_stub(monkeypatch):
    """mount שמקורו מת נשאר כרשומה בלי stream_start (נצפה) — הוא *לא* חי."""
    fake, _ = _fake_urlopen({"icestats": {"source": [
        {"listenurl": "http://localhost:8000/live.mp3", "stream_start": "x"},
        {"listenurl": "http://localhost:8000/clean.mp3", "listeners": 0},
    ]}})
    monkeypatch.setattr(app.urllib.request, "urlopen", fake)
    assert app._icecast_live_mounts() == {"live.mp3"}


def test_icecast_live_mounts_single_dict_and_none(monkeypatch):
    fake, _ = _fake_urlopen({"icestats": {"source": {
        "listenurl": "http://localhost:8000/clean.mp3", "stream_start": "x"}}})
    monkeypatch.setattr(app.urllib.request, "urlopen", fake)
    assert app._icecast_live_mounts() == {"clean.mp3"}
    app._ice_status.update(t=0.0)
    fake, _ = _fake_urlopen({"icestats": {}})          # אין מקורות בכלל
    monkeypatch.setattr(app.urllib.request, "urlopen", fake)
    assert app._icecast_live_mounts() == set()


def test_icecast_down_is_unknown_not_empty(monkeypatch):
    """Icecast לא עונה => None ("לא ידוע"), לא קבוצה ריקה ("כבוי")."""
    fake, _ = _fake_urlopen(OSError("refused"))
    monkeypatch.setattr(app.urllib.request, "urlopen", fake)
    assert app._icecast_live_mounts() is None


def test_icecast_status_cached(monkeypatch):
    fake, calls = _fake_urlopen({"icestats": {}})
    monkeypatch.setattr(app.urllib.request, "urlopen", fake)
    app._icecast_live_mounts()
    app._icecast_live_mounts()
    assert len(calls) == 1


# --- _denoise_sync ------------------------------------------------------------

def test_sync_starts_when_enabled_and_voice_live(paths, monkeypatch, installed):
    s = Sys(monkeypatch)
    app.save_state({**app.DEFAULT_STATE, "denoise": True})
    app._denoise_sync()
    assert s.calls == [("start", app.DENOISE_SERVICE)]
    app._denoise_sync()                                 # idempotent
    assert s.calls == [("start", app.DENOISE_SERVICE)]


def test_sync_stops_when_disabled(paths, monkeypatch, installed):
    s = Sys(monkeypatch, units={app.DENOISE_SERVICE: "active"})
    app.save_state({**app.DEFAULT_STATE, "denoise": False})
    app._denoise_sync()
    assert s.calls == [("stop", app.DENOISE_SERVICE)]


def test_sync_does_not_start_without_voice(paths, monkeypatch, installed):
    """בלי קול אין מה לסנן — לא מפעילים (ה-PartOf יעצור ממילא)."""
    s = Sys(monkeypatch, units={"rtl_airband": "inactive"})
    app.save_state({**app.DEFAULT_STATE, "denoise": True})
    app._denoise_sync()
    assert s.calls == []


def test_sync_does_not_start_when_not_installed(paths, monkeypatch):
    monkeypatch.setattr(app, "_denoise_missing", lambda: ["airam-rnnoise"])
    s = Sys(monkeypatch)
    app.save_state({**app.DEFAULT_STATE, "denoise": True})
    app._denoise_sync()
    assert s.calls == []


def test_sync_never_raises(paths, monkeypatch, installed):
    """systemctl שזורק/נתקע לא מפיל את הקורא — הקורא יכול להיות _enter_voice."""
    Sys(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("systemctl died")
    monkeypatch.setattr(app, "_sysctl", boom)
    app.save_state({**app.DEFAULT_STATE, "denoise": True})
    app._denoise_sync()                                 # לא זורק


def test_sync_start_failure_logged_not_raised(paths, monkeypatch, installed, caplog):
    Sys(monkeypatch, start_rc=1)
    app.save_state({**app.DEFAULT_STATE, "denoise": True})
    app._denoise_sync()
    assert "start נכשל" in caplog.text


def test_after_voice_is_free_when_disabled(paths, monkeypatch):
    """הנתיב רץ בכל כיוונון — מתג כבוי (ברירת המחדל) = אפס קריאות systemctl."""
    called = []
    monkeypatch.setattr(app, "_denoise_sync", lambda: called.append(1))
    app.save_state({**app.DEFAULT_STATE, "denoise": False})
    app._denoise_after_voice()
    assert called == []
    app.save_state({**app.DEFAULT_STATE, "denoise": True})
    app._denoise_after_voice()
    assert called == [1]


# --- כניסה לקול ---------------------------------------------------------------

def _voice_params():
    params, err = app._parse_tune(app.DEFAULT_STATE)
    assert err is None
    return params


def test_enter_voice_syncs_only_on_success(paths, monkeypatch):
    hooked = []
    monkeypatch.setattr(app, "_denoise_after_voice", lambda: hooked.append(1))
    monkeypatch.setattr(app, "_is_active", lambda svc: False)
    monkeypatch.setattr(app, "_restart_and_verify", lambda: (None, None, False))
    assert app._enter_voice(_voice_params()) == (None, None, False)
    assert hooked == [1]
    monkeypatch.setattr(app, "_restart_and_verify", lambda: ("נכשל", "log", False))
    assert app._enter_voice(_voice_params())[0] == "נכשל"
    assert hooked == [1]                                # לא נקרא בכישלון


def test_enter_voice_unaffected_by_denoise_failure(paths, monkeypatch, installed):
    """הכלל המרכזי: כשל מלא בסינון לא מכשיל את כניסת הקול."""
    monkeypatch.setattr(app, "_is_active", lambda svc: svc == "rtl_airband")
    monkeypatch.setattr(app, "_restart_and_verify", lambda: (None, None, False))

    def boom(*a, **k):
        raise RuntimeError("no systemd")
    monkeypatch.setattr(app, "_services_status", boom)
    monkeypatch.setattr(app, "_sysctl", boom)
    app.save_state({**app.DEFAULT_STATE, "denoise": True})
    assert app._enter_voice(_voice_params()) == (None, None, False)


def test_boot_restore_voice_already_live_syncs(paths, monkeypatch):
    """restart של airam-web כשהקול כבר רץ (שדרוג) — הסינון מיושר גם בלי כניסה מחדש."""
    hooked = []
    monkeypatch.setattr(app, "_denoise_after_voice", lambda: hooked.append(1))
    monkeypatch.setattr(app, "_live_mode", lambda: "voice")
    app.save_state({**app.DEFAULT_STATE, "app_mode": "voice"})
    app.CONFIG_PATH.write_text(app.render_config(132.5, "am", True, 40, 4, "open"))
    app._boot_restore()
    assert hooked == [1]


# --- API ----------------------------------------------------------------------

def test_api_get(client, monkeypatch, installed):
    Sys(monkeypatch)
    d = client.get("/api/denoise").get_json()
    assert d["ok"] is True and d["enabled"] is False and d["state"] == "off"
    assert d["mount"] == app.DENOISE_MOUNT and d["available"] is True


def test_api_post_enable(client, monkeypatch, installed):
    s = Sys(monkeypatch)
    fake, _ = _fake_urlopen({"icestats": {"source": {
        "listenurl": "http://x/clean.mp3", "stream_start": "x"}}})
    monkeypatch.setattr(app.urllib.request, "urlopen", fake)
    r = client.post("/api/denoise", json={"enabled": True})
    d = r.get_json()
    assert r.status_code == 200 and d["enabled"] is True and d["state"] == "live"
    assert app.load_state()["denoise"] is True
    assert ("start", app.DENOISE_SERVICE) in s.calls


def test_api_post_disable(client, monkeypatch, installed):
    s = Sys(monkeypatch, units={app.DENOISE_SERVICE: "active"})
    app.save_state({**app.DEFAULT_STATE, "denoise": True})
    d = client.post("/api/denoise", json={"enabled": False}).get_json()
    assert d["enabled"] is False and d["state"] == "off"
    assert ("stop", app.DENOISE_SERVICE) in s.calls


def test_api_post_not_installed_501_and_state_unchanged(client, monkeypatch):
    """לא שומרים "דלוק" שלא יכול לעבוד — ומחזירים את פקודת ההתקנה."""
    monkeypatch.setattr(app, "_denoise_missing", lambda: ["airam-rnnoise"])
    Sys(monkeypatch)
    r = client.post("/api/denoise", json={"enabled": True})
    d = r.get_json()
    assert r.status_code == 501 and d["ok"] is False
    assert d["install_hint"] == app.DENOISE_INSTALL_HINT
    assert "airam-rnnoise" in d["error"]
    assert app.load_state()["denoise"] is False


@pytest.mark.parametrize("body", [None, {}, {"enabled": "yes"}, {"enabled": 1}, [True]])
def test_api_post_bad_body(client, monkeypatch, installed, body):
    Sys(monkeypatch)
    r = client.post("/api/denoise", json=body)
    assert r.status_code == 400


def test_api_post_guarded(client, monkeypatch, installed):
    s = Sys(monkeypatch)
    r = client.post("/api/denoise", json={"enabled": True},
                    headers={"Origin": "http://evil.example.com"})
    assert r.status_code == 403
    assert s.calls == [] and app.load_state()["denoise"] is False


def test_missing_reports_each_component(monkeypatch, tmp_path):
    monkeypatch.setattr(app.shutil, "which", lambda name: None)
    monkeypatch.setattr(app, "DENOISE_SCRIPT", tmp_path / "airam-denoise")
    monkeypatch.setattr(app, "DENOISE_RNNOISE", tmp_path / "airam-rnnoise")
    assert app._denoise_missing() == ["ffmpeg", "airam-denoise", "airam-rnnoise"]
    for p in (app.DENOISE_SCRIPT, app.DENOISE_RNNOISE):
        p.write_text("#!/bin/sh\n")
        p.chmod(0o755)
    monkeypatch.setattr(app.shutil, "which", lambda name: "/usr/bin/" + name)
    assert app._denoise_missing() == []


# --- health / state -----------------------------------------------------------

def test_health_includes_denoise_and_ok_unaffected(client, monkeypatch, installed):
    """סינון שנפל לא הופך את המערכת ל"תקלה" — הרדיו תקין."""
    def fake_run(cmd, **kw):
        class R:
            returncode = 0
            stdout = "\n".join("failed" if u == app.DENOISE_SERVICE else "active"
                               for u in cmd[2:]) if cmd[1] == "is-active" else ""
            stderr = ""
        return R()
    monkeypatch.setattr(app.subprocess, "run", fake_run)
    fake, _ = _fake_urlopen({"icestats": {}})
    monkeypatch.setattr(app.urllib.request, "urlopen", fake)
    app.save_state({**app.DEFAULT_STATE, "app_mode": "voice", "denoise": True})
    h = client.get("/api/health").get_json()
    assert h["ok"] is True
    assert h["services"][app.DENOISE_SERVICE] == "failed"
    assert h["denoise"]["state"] == "failed"


def test_state_exposes_denoise_fields(client, monkeypatch, installed):
    monkeypatch.setattr(app, "_live_mode", lambda: None)
    st = client.get("/api/state").get_json()
    assert st["denoise"] is False
    assert st["denoise_mount"] == app.DENOISE_MOUNT
    assert st["denoise_available"] is True


# --- /stream proxy ------------------------------------------------------------

@pytest.mark.parametrize("query,mount", [
    ("", "live.mp3"), ("?m=clean", "clean.mp3"),
    ("?m=../admin/stats", "live.mp3"), ("?m=live", "live.mp3"),
])
def test_stream_proxy_mount_whitelist(client, monkeypatch, query, mount):
    seen = []

    class Up(io.BytesIO):
        pass

    def fake(url, timeout=None):
        seen.append(url)
        return Up(b"ID3data")
    monkeypatch.setattr(app.urllib.request, "urlopen", fake)
    r = client.get("/stream" + query)
    assert r.status_code == 200 and r.data == b"ID3data"
    assert seen == [f"http://127.0.0.1:{app.ICECAST_PORT}/{mount}"]


def test_external_playlist_follows_global_switch(client):
    assert "/live.mp3" in client.get("/live.m3u").get_data(as_text=True)
    app.save_state({**app.DEFAULT_STATE, "denoise": True})
    assert f"/{app.DENOISE_MOUNT}" in client.get("/live.m3u").get_data(as_text=True)


# --- עקביות בין הקבצים -------------------------------------------------------
# הפורט, שם ה-mount וה-sudoers מוגדרים בשלושה מקומות (app.py, הסקריפט, install.sh).
# הערות "חייב להיות זהה" לא מספיקות — אי-התאמה כאן היא כשל שקט: הסינון "רץ"
# אבל שום דבר לא מגיע אליו, או שהמתג לא יכול להפעיל אותו.
from pathlib import Path   # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def test_constants_match_script_and_installer():
    script = (ROOT / "scripts" / "airam-denoise").read_text(encoding="utf-8")
    inst = (ROOT / "install.sh").read_text(encoding="utf-8")
    unit = (ROOT / "systemd" / "airam-denoise.service").read_text(encoding="utf-8")
    assert f'AIRAM_DENOISE_PORT:-{app.DENOISE_UDP_PORT}}}' in script
    assert f'AIRAM_DENOISE_MOUNT:-{app.DENOISE_MOUNT}}}' in script
    assert f'AIRAM_SOURCE_PW:-{app.SOURCE_PW}}}' in script
    assert f"<mount-name>/{app.DENOISE_MOUNT}</mount-name>" in inst
    assert f"<fallback-mount>/{app.MOUNT}</fallback-mount>" in inst
    for action in ("start", "stop"):     # בדיוק מה ש-_denoise_sync קורא
        assert f"NOPASSWD: /usr/bin/systemctl {action} {app.DENOISE_SERVICE}\n" in inst
    assert f'cp "$REPO_DIR/systemd/{app.DENOISE_SERVICE}.service"' in inst
    assert f'cp "$REPO_DIR/scripts/{app.DENOISE_SCRIPT.name}" /usr/local/bin/' in inst
    assert f"install -m755 airam-rnnoise {app.DENOISE_RNNOISE}" in inst
    # תיקון ה-ARM של upstream (בלעדיו v0.2 לא נבנה על ה-Pi) — הקובץ קיים ומוחל
    patch = ROOT / "tools" / "rnnoise-0.2-372f7b4-arm-build-fix.patch"
    assert patch.exists() and "vec_neon.h" in patch.read_text(encoding="utf-8")
    assert f'RNNOISE_PATCH="$REPO_DIR/tools/{patch.name}"' in inst
    assert 'patch -p1 -s < "$RNNOISE_PATCH"' in inst
    assert f"ExecStart={app.DENOISE_SCRIPT}" in unit
    assert "PartOf=rtl_airband.service" in unit
    # לא enabled — airam-web מפעיל (שורת מקטע אמיתית; ההערה ביחידה מזכירה את המילה)
    assert "[Install]" not in [line.strip() for line in unit.splitlines()]


def test_unit_never_conflicts_with_sdr_consumers():
    """הסינון חי *לצד* הקול — Conflicts כאן היה עוצר את הרדיו בהדלקת המתג."""
    unit = (ROOT / "systemd" / "airam-denoise.service").read_text(encoding="utf-8")
    assert not any(line.startswith("Conflicts=") for line in unit.splitlines())
