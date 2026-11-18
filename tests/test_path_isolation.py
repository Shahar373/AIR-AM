"""שער הבידוד: אף נתיב-מערכת של האפליקציה לא נשאר מצביע על /var/lib/airam, /etc, /run, /opt, /usr
בזמן בדיקה (conftest._isolate_system_paths). נכשל אם מודול חדש מגדיר נתיב כזה ולא נתפס."""
import conftest


def test_no_system_paths_remain_during_tests():
    leftovers = [f"{m.__name__}.{k}={v}" for m, k, v in conftest._system_paths()]
    assert leftovers == []


def test_isolation_preserves_filename_and_structure():
    import app
    assert app.STATE_PATH.name == "state.json"
    assert app.ACARS_ENV_PATH.parent.name == "airam"
    assert "_sys" in app.STATE_PATH.parts
