import sys
from pathlib import Path

# app.py יושב ב-webtune/ (לא חבילה מותקנת) => מוסיפים ל-path לפני import
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "webtune"))
