import sys
from pathlib import Path

# app.py יושב ב-webtune/ (לא חבילה מותקנת) => מוסיפים ל-path לפני import
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "webtune"))

import pytest   # noqa: E402

import app      # noqa: E402


# ‏fixtures משותפים. קובץ שמגדיר fixture באותו שם *דורס* את זה (כך test_sdr/test_security/
# test_xcheck_api מגדירים client בלי `paths`). ‏`paths` עצמו נשאר בכל קובץ — כל קובץ מנתב
# קבוצה אחרת של נתיבים ל-tmp_path, ו-client כאן מקבל את זה של הקובץ שמבקש אותו.
@pytest.fixture
def client(paths):
    return app.app.test_client()


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(app.time, "sleep", lambda *a, **k: None)

# --- בידוד נתיבים: אף בדיקה לא נוגעת ב-/var/lib/airam, /etc, /run (שלב ג2) ---------------
# ‏autouse: כל תכונה מסוג Path בכל מודול של האפליקציה שמצביעה על נתיב-מערכת מנותבת ל-tmp_path
# (המבנה נשמר — ‎`/etc/airam/x.env` ⇒ ‎`<tmp>/_sys/etc/airam/x.env`, כך ש-`.name` ו-`.parent.name`
# נשארים). הבחירה לפי *ערך* ולא לפי רשימת שמות ⇒ מודול חדש (state/experiment/…) נתפס
# אוטומטית, והלולאות הישנות בבדיקות (`setattr(app, name, ...)`) ממשיכות לדרוס מעליו.
_SYSTEM_PREFIXES = ("/var/", "/etc/", "/run/", "/opt/", "/usr/")


def _app_modules():
    mods = []
    for name in ("app", "recordings", "storage", "acars_parse", "adsb", "xcheck", "diag"):
        m = sys.modules.get(name)
        if m is not None:
            mods.append(m)
    return mods


def _system_paths():
    out = []
    for m in _app_modules():
        for k, v in list(vars(m).items()):
            if isinstance(v, Path) and str(v).startswith(_SYSTEM_PREFIXES):
                out.append((m, k, v))
    return out


# הערכים האמיתיים (לפני הבידוד) — לבדיקות שמשוות קבוע-נתיב מול install.sh/יחידות systemd.
REAL_PATHS = {(m.__name__, k): v for m, k, v in _system_paths()}


def real_path(module, name):
    return REAL_PATHS[(module.__name__, name)]


@pytest.fixture(autouse=True)
def _isolate_system_paths(tmp_path, monkeypatch):
    root = tmp_path / "_sys"
    for m, k, v in _system_paths():
        monkeypatch.setattr(m, k, root / str(v).lstrip("/"))
    yield
