# ניסוי סינון הרעשים — שחזור

הניסוי שמאחורי ההחלטות ב-[`../denoise-design.md`](../denoise-design.md) §4: השוואת
RNNoise v0.2 מול חמשת המודלים של `arnndn` (ffmpeg), מול `afftdn` הקלאסי, ושתי חלופות
נורמליזציה — על דיבור אנושי אמיתי שעובר סימולציה של פלט ה-AM של rtl_airband.

```bash
cd docs/denoise-experiment
./fetch.sh                                   # קלטים ל-./data (git ‏+ curl ‏+ בניית RNNoise)
pip install numpy scipy soundfile pystoi     # ffmpeg עם arnndn/dynaudnorm נדרש ב-PATH
python3 run.py                               # טבלה לכל תרחיש + data/out/report.json
```

- `data/` מוחרג מ-git (`.gitignore`). קובצי ה-WAV של כל שרשרת נשמרים ב-`data/out/` — אפשר
  (ורצוי) להאזין להם, לא רק לקרוא את המספרים.
- השורה `rnnoise02+dynaudnorm (=production)` היא בדיוק השרשרת של `scripts/airam-denoise`,
  כולל `tools/airam-rnnoise.c` בצינור — לא קירוב שלה.
- **לא חלק מההתקנה ולא רץ ב-CI** — כלי מחקר. ההכרעה הסופית היא האזנה על ה-Pi (§8 במסמך).
