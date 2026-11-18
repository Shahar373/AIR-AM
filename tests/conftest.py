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
