# עזרים משותפים לבדיקות (לא fixtures — ר' conftest.py לאלה).
import types

ORIGIN = {"Origin": "http://localhost"}


def ok_proc(**kw):
    """תוצאת subprocess.run מוצלחת ומזויפת (returncode=0, בלי פלט)."""
    return types.SimpleNamespace(returncode=0, stdout="", stderr="", **kw)
