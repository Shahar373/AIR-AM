# ============================================================================
#  AIR-AM - 🩺 בדיקת RF מקצה לקצה (PR 2 — docs/rf-check-design.md)
# ----------------------------------------------------------------------------
#  שלושת החלקים *האמיתיים* יחד, בלי stub באף שכבה:
#    * הבודק (webtune/rfcheck_probe.py — main_with: קריאת קובץ הפרמטרים שה-app כתב,
#      ‏O_NOFOLLOW/בעלים/whitelist, ניקוי OUT_DIR, לולאת הסלוטים, end.json בעצירה)
#      רץ ב-thread מול tests/rfsim.FakeRadio (שעון וירטואלי, אותה מכניקת טבעת כמו
#      SoapySDRPlay3) — זה ה-"systemctl restart airam-rfcheck" של העולם המדומה;
#    * שכבת הניתוח (rfcheck_analysis) — live_summary/stop_reason/offer_atis/analyze;
#    * המתזמר ב-app.py — POST /api/rfcheck → thread עם TUNE_LOCK → קריאת הפלטים →
#      כלל עצירה → ניתוח → שמירה → `_restore_after_probe` האמיתי → GET → "החל" דרך
#      ‏_voice_tune האמיתי (כתיבת airband.conf).
#  ממוקף רק מה שאין בלי חומרה: systemd (‏_sysctl/_is_active/_services_status) ו-
#  ‏_restart_and_verify של rtl_airband (מסמן "הקול חי" במקום systemctl אמיתי).
#
#  ⚠ שעונים: הבודק רץ על השעון הווירטואלי של FakeRadio (מהיר ~פי 4 מזמן-אמת) — אבל
#  ‏wall=time.time *אמיתי*, כי כלב-השמירה של המתזמר משווה את t_wall של status.json
#  לשעון-הקיר שלו. ה-elapsed של המתזמר (‏offer_atis/‏"time") הוא זמן-אמת.
#  numpy נדרש (rfsim + ה-DSP של הבודק) => importorskip, כמו test_rfcheck_dsp.
# ============================================================================
import io
import json
import os
import threading
import time

import pytest

np = pytest.importorskip("numpy")

import app                      # noqa: E402
import rfcheck_analysis         # noqa: E402
import rfcheck_probe as rp      # noqa: E402
import rfsim                    # noqa: E402

TOWER_FREQ = 134.6


class ProbeWorld:
    """systemd מדומה: rtl_airband (קול) ו-airam-rfcheck — שהפעלתו מריצה את הבודק האמיתי
    (‏rp.main_with) ב-thread מול FakeRadio. Conflicts: הפעלת הבודק עוצרת את הקול; ‏stop =
    SIGTERM (דגל) => הבודק סוגר את המכשיר וכותב end.json, בדיוק כמו ב-main()."""

    def __init__(self, tmp_path, scene_for):
        self.scene_for = scene_for              # freq_hz => (Scene, FrontEnd)
        self.live = "voice"
        self.calls, self.params_seen, self.ends = [], [], []
        self.radios, self.codes, self.restores = [], [], []
        self.lock_held_at_restart = []
        self.th, self.stop_flag = None, None
        self.mark = tmp_path / "build-sig"      # סימן-בנייה מדומה => טלמטריה "ok"
        self.mark.write_text("ab" * 32 + "\n")
        self.api_version = tmp_path / "no-sdrplay-api.version"

    # --- systemd ---
    def sysctl(self, action, svc, timeout=45):
        self.calls.append((action, svc))
        if svc == "rtl_airband" and action == "stop":
            self.live = None if self.live == "voice" else self.live
        elif svc == app.RFCHECK_SERVICE and action == "stop":
            self._stop_probe()
        elif svc == app.RFCHECK_SERVICE and action == "restart":
            self.lock_held_at_restart.append(app.TUNE_LOCK.locked())
            self._stop_probe()
            if self.live == "voice":
                self.live = None                # Conflicts=rtl_airband
            self._start_probe()
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    def _start_probe(self):
        # רק כדי לבחור סצנה לפי התדר; הבודק עצמו קורא את הקובץ בעצמו (main_with)
        params = json.loads(app.RFCHECK_PARAMS_PATH.read_text())
        self.params_seen.append(params)
        scene, fe = self.scene_for(params["freq_hz"])
        radio = rfsim.FakeRadio(scene, fe)
        self.radios.append(radio)
        self.stop_flag = rp.StopFlag()
        err = io.StringIO()

        def run():
            code = rp.main_with(str(app.RFCHECK_PARAMS_PATH), radio.backend(),
                                rp.OutDir(str(app.RFCHECK_RUN_DIR)),
                                allowed_uids={os.getuid()}, stderr=err,
                                stop=self.stop_flag, clock=radio.clock, wall=time.time,
                                marker_path=str(self.mark),
                                api_version_path=str(self.api_version))
            self.codes.append(code)
            try:
                self.ends.append(json.loads((app.RFCHECK_RUN_DIR / "end.json").read_text()))
            except (OSError, ValueError):
                self.ends.append(None)

        self.th = threading.Thread(target=run, daemon=True, name="fake-airam-rfcheck")
        self.th.start()

    def _stop_probe(self):
        if self.th is not None:
            self.stop_flag.set()
            self.th.join(timeout=20)
            assert not self.th.is_alive(), "הבודק לא נעצר אחרי SIGTERM (דגל)"
            self.th = None

    def probe_alive(self):
        return self.th is not None and self.th.is_alive()

    def services(self, names):
        return {n: ("active" if (n == app.RFCHECK_SERVICE and self.probe_alive())
                    or (n == "rtl_airband" and self.live == "voice") else "inactive")
                for n in names}

    def is_active(self, svc):
        return self.services((svc,))[svc] == "active"

    def restart_rtl_airband_ok(self):
        """במקום systemctl restart rtl_airband + אימות: הקול "עלה"."""
        self.live = "voice"
        return None, None, False


@pytest.fixture
def e2e(tmp_path, monkeypatch):
    for name, fn in (("CONFIG_PATH", "airband.conf"), ("STATE_PATH", "state.json"),
                     ("STATS_PATH", "stats.txt"), ("RFLOG_PATH", "rf_log.jsonl"),
                     ("ACARS_ENV_PATH", "acars.env"), ("VDL2_ENV_PATH", "vdl2.env"),
                     ("SATCOM_ENV_PATH", "satcom.env"),
                     ("RFCHECK_PARAMS_PATH", "rfcheck-params.json"),
                     ("RFCHECK_LAST_PATH", "rfcheck_last.json"),
                     ("RFCHECK_LAST_ROWS_PATH", "rfcheck_last_rows.jsonl"),
                     ("RFCHECK_HISTORY_PATH", "rfcheck_history.jsonl"),
                     ("RFCHECK_DIAG_PATH", "rfcheck_diagnose.json"),
                     ("SOAPY_RF_MARK", "no-such-build-sig"),
                     ("SDRPLAY_API_VERSION_PATH", "sdrplay-api.version")):
        monkeypatch.setattr(app, name, tmp_path / fn)
    run = tmp_path / "run"
    run.mkdir()
    monkeypatch.setattr(app, "RFCHECK_RUN_DIR", run)
    # שכבת הניתוח האמיתית (בדיקות אחרות מחליפות אותה ב-stub דרך monkeypatch — לא כאן)
    monkeypatch.setattr(app, "rfcheck_analysis", rfcheck_analysis)

    holder = {}

    def scene_for(freq_hz):
        return holder["scene_for"](freq_hz)
    w = ProbeWorld(tmp_path, scene_for)
    w.set_scene = lambda fn: holder.__setitem__("scene_for", fn)
    monkeypatch.setattr(app, "_sysctl", w.sysctl)
    monkeypatch.setattr(app, "_services_status", w.services)
    monkeypatch.setattr(app, "_is_active", w.is_active)
    monkeypatch.setattr(app, "_restart_and_verify", w.restart_rtl_airband_ok)
    monkeypatch.setattr(app, "_rollback", lambda prev: True)
    monkeypatch.setattr(app, "_journal_tail", lambda svc="rtl_airband", lines=8: "JOURNAL " + svc)
    monkeypatch.setattr(app, "_rtl_airband_start_wall", lambda: None)   # IFGR_ref "הונח" (40)
    monkeypatch.setattr(app, "_sdr_present", lambda: True)
    # זמינות: ה-selftest האמיתי מריץ ‎/usr/bin/python3 עם python3-soapysdr — לא קיים כאן
    monkeypatch.setattr(app, "_rfcheck_availability", lambda force=False: {
        "available": True, "reasons": [], "install_hint": "sudo ./install.sh",
        "telemetry_expected": True, "verified": False, "selftest": None})
    monkeypatch.setattr(app, "RFCHECK_POLL_SEC", 0.05)
    monkeypatch.setattr(app, "RFCHECK_STOP_WAIT_SEC", 0.2)
    # `_restore_after_probe` *האמיתי* (‏_enter_voice => write_config => _restart_and_verify
    # המדומה), עטוף רק כדי לספור קריאות
    real_restore = app._restore_after_probe

    def restore_spy(prev, prev_live):
        w.restores.append((dict(prev), prev_live, app.TUNE_LOCK.locked()))
        return real_restore(prev, prev_live)
    monkeypatch.setattr(app, "_restore_after_probe", restore_spy)

    yield w

    # ניקוי: עוצרים בדיקה שאולי נשארה, ומאפסים את מצב הבקר לבדיקה הבאה
    with app._rfc_lock:
        th, stop, wake = app._rfc["thread"], app._rfc["stop_evt"], app._rfc["wake"]
    if stop:
        stop.set()
    if wake:
        wake.set()
    if th:
        th.join(timeout=30)
    w._stop_probe()
    with app._rfc_lock:
        app._rfc.update(running=False, run_id=None, parent_id=None, kind=None, stage=None,
                        ref=None, freq=None, states=[], result=None, diagnose=None, error=None,
                        error_code=None, ended=None, restore=None, live=None, probe=None,
                        stop_evt=None, finish_evt=None, atis_evt=None, wake=None, thread=None,
                        started_at=None, finished_at=None, config_at_start=None)
    app._rfc_last_cache.update(sig=None, result=None)
    if app.TUNE_LOCK.locked():
        app.TUNE_LOCK.release()


@pytest.fixture
def client(e2e):
    return app.app.test_client()


def _voice_state(**kw):
    st = {**app.DEFAULT_STATE, "app_mode": "voice", "freq": TOWER_FREQ, "mod": "am",
          "agc": True, "rf_gain": 4, "if_gain": 40, "fm_notch": False, "squelch_mode": "auto"}
    st.update(kw)
    app.save_state(st)
    return st


def _post(client, **body):
    r = client.post("/api/rfcheck", json=body)
    return r.status_code, r.get_json()


def _get(client):
    return client.get("/api/rfcheck").get_json()


def _wait(pred, timeout, what):
    t_end = time.monotonic() + timeout
    while time.monotonic() < t_end:
        v = pred()
        if v:
            return v
        time.sleep(0.02)
    raise AssertionError("פג הזמן בהמתנה ל: " + what)


def _wait_done(client, timeout=90):
    with app._rfc_lock:
        th = app._rfc["thread"]
    assert th is not None
    th.join(timeout=timeout)
    assert not th.is_alive(), "ה-thread של הבדיקה לא הסתיים"
    return _get(client)


def _assert_restored_once(w, prev_live="voice"):
    """האינווריאנטה (spec §11.2) מול העולם האמיתי-ככל-האפשר: שחזור *בדיוק פעם אחת*, תחת
    TUNE_LOCK, הבודק לא רץ, הקול חי שוב, הנעילה משוחררת, קובץ הפרמטרים נמחק."""
    assert len(w.restores) == 1, w.restores
    assert w.restores[0][1] == prev_live and w.restores[0][2] is True
    assert not w.probe_alive()
    assert ("stop", app.RFCHECK_SERVICE) in w.calls
    assert not app.TUNE_LOCK.locked()
    assert app._live_mode() == prev_live
    assert app.load_state()["app_mode"] == "voice"
    assert not app.RFCHECK_PARAMS_PATH.exists()
    # הבודק *עצמו* נעצר כמו ב-SIGTERM: סגר את המכשיר וכתב end.json מסודר
    assert w.radios and all(r.closed for r in w.radios)
    assert w.codes and all(c == rp.EXIT_OK for c in w.codes), w.codes
    assert all(e and e["ended"] in ("stopped", "max_sec") for e in w.ends), w.ends


# ============================================================================
#  1. מגדל חזק מאוד: עומס/חיתוך ב-LNA הנמוכים => ההמלצה מתרחקת מהם => "החל"
# ============================================================================
def _strong_tower(freq_hz):
    """נשא AM חזק מאוד (‏+10dBFS בנקודת הייחוס LNA 4/IFGR 40) בשלושה שידורי PTT. אחרי
    שה-ratchet מעלה את ה-IF ל-59: ב-LNA 0/2 השיא עדיין מעל ה-ADC (חיתוך + אירועי
    "AIRAM_RF overload=1" מהדרייבר המדומה) — עובדה; ב-4/7 כבר לא. ⚠ סימולציה: הקצה הקדמי
    ליניארי, כך שההבדלים *בין* 4 ל-7 הם רק רעש ה-ADC מול ה-IF — לא נבדקים כאן."""
    ptt = [(1.5, 6.5), (9.0, 14.0), (17.0, 22.0)]
    return (rfsim.Scene(noise_dbfs=-60.0, carriers=[rfsim.Carrier(10.0, ptt=ptt)]),
            rfsim.FrontEnd(ovl_dbfs=-0.5))


def test_e2e_overload_at_low_lna_recommends_away_and_applies(client, e2e):
    e2e.set_scene(_strong_tower)
    _voice_state(rf_gain=2)                     # הייצור: LNA 2 — בדיוק אחד המצבים שנפסלים
    code, body = _post(client, action="start", states=[0, 2, 4, 7])
    assert code == 200 and body["ok"] is True and body["running"] is True
    run_id = body["run_id"]

    # התקדמות חיה דרך GET (מה שהטלפון רואה כל שנייה) — מהבודק האמיתי, לא מ-stub
    live = _wait(lambda: (lambda d: d if d["running"] and (d.get("probe") or {}).get("rows", 0) >= 8
                          and d["states"] else None)(_get(client)), 60, "התקדמות חיה")
    assert live["phase"] in ("listening", "finishing") and live["kind"] == "lna"
    assert [s["lna"] for s in live["states"]] == [0, 2, 4, 7]
    assert all(s["label"] == f"{9 - s['lna']}/9" for s in live["states"])   # = הסליידר
    assert isinstance(live["progress"], (int, float)) and 0 <= live["progress"] <= 1
    assert live["result"] is None and live["can_extend"] is False

    done = _wait_done(client)
    assert done["running"] is False and done["error"] is None, (done["error"], done["detail"])
    # עצר לבד ברגע שהיעד הסטטיסטי הושג (או עובדה סופית) — לא בגלל זמן/ביטול
    assert done["ended"] == "target", done["ended"]
    res = done["result"]
    assert res["run_id"] == run_id and res["phase"] == "lna" and res["ref"] == "tower"

    # --- הפסיקה: עובדות פוסלות את 0/2, וההמלצה מתרחקת מהם ---
    assert set(res["disqualified"]) >= {0, 2}, res["disqualified"]
    ps = {p["lna"]: p for p in res["per_state"]}
    for s in (0, 2):
        assert "disqualified" in ps[s]["flags"]
        assert ps[s]["op_clip"] >= 2 and ps[s]["ifgr_final"] == 59     # עובדה רק ב-IF ‏59
        assert ps[s]["overload"] == "observed"     # טלמטריה "ok" (handler + marker + שורות)
    assert res["selfcheck"]["telemetry"] == "ok"
    assert res["level"] in ("fact", "stat")
    assert res["headline"] == "reduce_gain_overload"
    rec = res["recommendation"]
    assert rec["rf_gain"] > max(res["disqualified"]) and rec["rf_gain"] in res["candidates"]
    assert rec["if_gain"] is None                 # AGC בייצור => רק ה-LNA (החלטת משתמש 5)
    assert res["apply_allowed"] is True
    assert res["headline_params"]["list"] == [0, 2] and res["headline_params"]["y"] == rec["rf_gain"]
    # "מה לא נבדק" — תמיד חלק מהתוצאה; ולא מאומת => ניסיוני
    assert {"code": "soft_compression_tower"} in res["untestable"]
    assert {"code": "overload_semantics_unverified"} in res["untestable"]

    # --- שחזור, נעילה, שמירה ---
    _assert_restored_once(e2e)
    assert e2e.lock_held_at_restart == [True]
    assert e2e.params_seen[0]["states"] == [0, 2, 4, 7]
    assert e2e.params_seen[0]["ifgr_start"] == {"0": 52, "2": 40, "4": 32, "7": 20}   # פיצוי מ-(2, 40)
    assert json.loads(app.RFCHECK_LAST_PATH.read_text())["run_id"] == run_id
    rows = [json.loads(ln) for ln in app.RFCHECK_LAST_ROWS_PATH.read_text().splitlines()]
    assert rows[0]["kind"] == "ctx" and rows[0]["run_id"] == run_id
    assert sum(1 for r in rows if r["kind"] == "row" and r["tag"] == "tower") >= 40
    hist = app.RFCHECK_HISTORY_PATH.read_text().splitlines()
    assert len(hist) == 1 and json.loads(hist[0])["recommendation"]["rf_gain"] == rec["rf_gain"]

    # --- "החל": דרך _voice_tune האמיתי => state + airband.conf (rfgain_sel תחת AGC) ---
    restarts_before = len(e2e.restores)
    code, ap = _post(client, action="apply", run_id=run_id)
    assert code == 200 and ap["ok"] is True, ap
    st = app.load_state()
    assert st["rf_gain"] == rec["rf_gain"] and st["app_mode"] == "voice" and st["agc"] is True
    assert st["rf_check_applied"]["run_id"] == run_id
    assert st["rf_check_applied"]["rf_gain"] == rec["rf_gain"]
    assert f"rfgain_sel={rec['rf_gain']}" in app.CONFIG_PATH.read_text()
    assert len(e2e.restores) == restarts_before          # "החל" הוא כיוונון, לא שחזור-בדיקה
    assert app._live_mode() == "voice"
    # התוצאה נשארת זמינה אחרי ההחלה (שורדת reload); החלה שנייה של אותה ריצה => 409
    again = _get(client)
    assert again["result"]["run_id"] == run_id
    # ההמלצה כבר בתוקף => לא מפעילים מחדש את rtl_airband בשביל כלום (גם אחרי reload של הדף)
    code, ap2 = _post(client, action="apply", run_id=run_id)
    assert code == 409 and "כבר בתוקף" in ap2["error"]


# ============================================================================
#  2. מגדל שקט => הצעת ATIS (לא אוטומטית) => מעבר תחת אותה נעילה => "tower+atis"
# ============================================================================
ATIS_HZ = int(round(app.RFCHECK_ATIS_FREQ * 1e6))


def _quiet_tower_busy_atis(freq_hz):
    if freq_hz == ATIS_HZ:       # ATIS ‏132.5 — שידור רציף, בלי חיתוך באף מצב
        return (rfsim.Scene(noise_dbfs=-60.0, carriers=[rfsim.Carrier(-25.0, ptt=None)]),
                rfsim.FrontEnd(ovl_dbfs=-0.5))
    return rfsim.Scene(noise_dbfs=-60.0), rfsim.FrontEnd(ovl_dbfs=-0.5)   # המגדל: רק רעש


def test_e2e_no_tower_traffic_offers_atis_and_merges(client, e2e, monkeypatch):
    e2e.set_scene(_quiet_tower_busy_atis)
    # ההצעה מגיעה אחרי 30ש' (זמן-אמת של המתזמר) — מקוצר לבדיקה; הלוגיקה עצמה ללא שינוי
    monkeypatch.setattr(rfcheck_analysis, "ATIS_OFFER_SEC", 1.0)
    _voice_state(rf_gain=4)
    code, body = _post(client, action="start", states=[0, 4, 7])
    assert code == 200
    tower_id = body["run_id"]

    offered = _wait(lambda: (lambda d: d if d["offer_atis"] else None)(_get(client)), 60,
                    "הצעת ATIS")
    # רק *הצעה*: עדיין על המגדל, בלי נשא, והבודק לא הוחלף לבד
    assert offered["running"] and offered["ref"] == "tower" and offered["run_id"] == tower_id
    assert (offered.get("tx_captured") or 0) == 0 and offered["valid"] > 0
    assert len(e2e.params_seen) == 1

    code, _ = _post(client, action="atis")
    assert code == 200
    done = _wait_done(client)
    assert done["error"] is None, (done["error"], done["detail"])
    assert done["ended"] in ("atis_target", "time"), done["ended"]

    # שני הרצות של הבודק האמיתי: המגדל ואז ATIS (פרמטרים חדשים, אותה נעילה, בלי שחזור ביניהן)
    assert len(e2e.params_seen) == 2
    tower_p, atis_p = e2e.params_seen
    assert tower_p["ref"] == "tower" and tower_p["freq_hz"] == int(round(TOWER_FREQ * 1e6))
    assert atis_p["ref"] == "atis" and atis_p["freq_hz"] == ATIS_HZ
    assert atis_p["run_id"] != tower_id and atis_p["states"] == tower_p["states"]
    assert atis_p["ifgr_start"] == tower_p["ifgr_start"]       # אותו IFGR_ref, לא ה-ratchet
    assert atis_p["max_sec"] == rfcheck_analysis.ATIS_MAX_SEC + app.RFCHECK_ATIS_PROBE_EXTRA_SEC
    assert e2e.lock_held_at_restart == [True, True]
    _assert_restored_once(e2e)

    res = done["result"]
    assert res["ref"] == "tower+atis" and res["parent_id"] == tower_id
    assert res["run_id"] == atis_p["run_id"] and res["atis_freq"] == app.RFCHECK_ATIS_FREQ
    assert res["tower"] and res["tower"]["rows"] > 0 and res["tower"]["n_carrier"] == 0
    # ב-ATIS יש נשא רציף => השוואת CNR אמיתית בכל המצבים
    assert all(p["n_carrier"] > 0 and p["cnr_median"] is not None for p in res["per_state"])
    assert res["level"] in ("stat", "indication", "fact")
    rows = [json.loads(ln) for ln in app.RFCHECK_LAST_ROWS_PATH.read_text().splitlines()]
    tags = {r["tag"] for r in rows if r["kind"] == "row"}
    assert tags == {"tower", "atis"}


# ============================================================================
#  3. ביטול באמצע: הבודק נעצר מסודר, שום דבר לא נשמר, התוצאה הקודמת לא נדרסת
# ============================================================================
def test_e2e_abort_mid_run_keeps_previous_result(client, e2e):
    e2e.set_scene(_strong_tower)
    _voice_state(rf_gain=4)
    prev = {"v": 1, "run_id": "feedfacefeedface", "phase": "lna", "level": "stat",
            "headline": "keep_current", "freq": TOWER_FREQ}
    app.RFCHECK_LAST_PATH.write_text(json.dumps(prev))
    code, body = _post(client, action="start", states=[0, 4, 7])
    assert code == 200
    run_id = body["run_id"]
    _wait(lambda: (_get(client).get("probe") or {}).get("rows", 0) >= 6, 60, "שורות ראשונות")
    assert e2e.probe_alive()

    code, _ = _post(client, action="abort")
    assert code == 200
    done = _wait_done(client)
    assert done["ended"] == "abort" and done["error"] is None
    _assert_restored_once(e2e)
    # הבודק האמיתי קיבל את ה"SIGTERM" באמצע ריצה: end.json של *הריצה הזו*, "stopped"
    end = json.loads((app.RFCHECK_RUN_DIR / "end.json").read_text())
    assert end["run_id"] == run_id and end["ended"] == "stopped" and end["slots"] >= 6
    # ביטול = שום דבר לא נשמר; התוצאה הקודמת (על הדיסק) נשארת והיא מה ש-GET מגיש
    assert json.loads(app.RFCHECK_LAST_PATH.read_text()) == prev
    assert not app.RFCHECK_LAST_ROWS_PATH.exists() and not app.RFCHECK_HISTORY_PATH.exists()
    assert done["result"]["run_id"] == prev["run_id"]


# ============================================================================
#  4. השוואת מסנן FM לפי דרישה, אחרי תוצאת LNA: חוסם FM חזק דוחס את ה-LNA => "הדלק"
# ============================================================================
def _fm_blocker(freq_hz):
    """חוסם מחוץ לחלון (שידור FM) שמעמיס את ה-LNA (Rapp) — לא מגיע ל-ADC, אבל דוחס את
    הנשא ואת רעש האנטנה, ורעש המקלט שאחרי ה-LNA נשאר => CNR נמוך. מסנן ה-FM המדומה מחליש
    את החוסם ב-30dB (ובנשא ‏0.5dB) — אז דלוק עדיף מדידתית. נשא רציף: כל זוג ABBA נמדד."""
    return (rfsim.Scene(noise_dbfs=-60.0, carriers=[rfsim.Carrier(-30.0, ptt=None)], oob_dbfs=0.0),
            rfsim.FrontEnd(sat_dbfs=-15.0, rx_noise_dbfs=-62.0, ovl_dbfs=-0.5))


def test_e2e_notch_on_demand_after_lna_result_and_apply(client, e2e):
    e2e.set_scene(_fm_blocker)
    _voice_state(rf_gain=6, fm_notch=False)
    lna_run = "0badc0de0badc0de"
    app.RFCHECK_LAST_PATH.write_text(json.dumps({
        "v": 1, "run_id": lna_run, "phase": "lna", "ref": "tower", "level": "stat",
        "freq": TOWER_FREQ, "headline": "reduce_gain_tie",
        "recommendation": {"rf_gain": 4, "if_gain": None, "fm_notch": None, "basis": "stat"},
        "per_state": [{"lna": 4, "ifgr_final": 41}, {"lna": 6, "ifgr_final": 52}],
        "config_at_start": {"freq": TOWER_FREQ, "agc": True, "fm_notch": False, "rf_gain": 6}}))
    code, body = _post(client, action="start", phase="notch", from_run=lna_run)
    assert code == 200 and body["kind"] == "notch"
    done = _wait_done(client)
    assert done["error"] is None, (done["error"], done["detail"])
    assert done["ended"] == "notch_target", done["ended"]
    _assert_restored_once(e2e)
    # נמדד במצב ה-LNA *המומלץ* של הריצה הקודמת (4, ב-IF הסופי שלה), לא בנוכחי (6)
    p = e2e.params_seen[0]
    assert p["phase"] == "notch" and p["notch_alternate"] is True and p["notch_base"] is False
    assert p["states"] == [4] and p["ifgr_start"] == {"4": 41}
    rows = [json.loads(ln) for ln in app.RFCHECK_LAST_ROWS_PATH.read_text().splitlines()]
    rows = [r for r in rows if r["kind"] == "row"]
    assert [r["notch"] for r in rows[:4]] == [False, True, True, False]       # ABBA
    assert all(r["notch_rb"] == ("true" if r["notch"] else "false") for r in rows)

    res = done["result"]
    assert res["phase"] == "notch" and res["headline"] == "notch_on" and res["level"] == "stat"
    assert res["recommendation"]["fm_notch"] is True and res["recommendation"]["rf_gain"] == 4
    cfg = {c["fm_notch"]: c for c in res["per_config"]}
    assert cfg[True]["cnr_median"] > cfg[False]["cnr_median"]
    assert res["apply_allowed"] is True

    code, ap = _post(client, action="apply", run_id=res["run_id"])
    assert code == 200 and ap["ok"] is True, ap
    st = app.load_state()
    assert st["fm_notch"] is True and st["rf_gain"] == 4
    conf = app.CONFIG_PATH.read_text()
    assert "rfnotch_ctrl=true" in conf and "rfgain_sel=4" in conf
