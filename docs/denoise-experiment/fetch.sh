#!/usr/bin/env bash
# מוריד את קלטי הניסוי ל-./data (ר' README.md לצדו). לא חלק מההתקנה.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p data && cd data

# דיבור אנושי אמיתי: 3 קטעי LibriSpeech (CC-BY 4.0) מתוך מאגר הדוגמאות של librosa
if [[ ! -d librosa-data ]]; then
  git clone -q --depth 1 --filter=blob:none --no-checkout https://github.com/librosa/data.git librosa-data
  git -C librosa-data checkout -q HEAD -- \
    audio/198-209-0000.hq.ogg audio/3436-172162-0000.hq.ogg audio/5703-47212-0000.hq.ogg
fi

# המודלים הקהילתיים של arnndn (ffmpeg) — חמשת המועמדים שנפסלו
[[ -d rnnoise-models ]] || git clone -q --depth 1 https://github.com/GregorR/rnnoise-models.git

# RNNoise v0.2 — אותו tarball ואותו sha256 כמו install.sh שלב 4e
RN=rnnoise-0.2.tar.gz
SHA=90fce4b00b9ff24c08dbfe31b82ffd43bae383d85c5535676d28b0a2b11c0d37
[[ -f $RN ]] || curl -fsSL -o "$RN" "https://github.com/xiph/rnnoise/releases/download/v0.2/$RN"
echo "$SHA  $RN" | sha256sum -c --status || { echo "sha256 mismatch" >&2; exit 1; }
if [[ ! -x airam-rnnoise ]]; then
  rm -rf rnnoise-0.2 && tar xzf "$RN"
  patch -d rnnoise-0.2 -p1 -s < ../../../tools/rnnoise-0.2-372f7b4-arm-build-fix.patch   # כמו install.sh
  (cd rnnoise-0.2 && ./configure -q --disable-shared --disable-examples --disable-doc >/dev/null && make -s -j"$(nproc)" >/dev/null)
  cc -O2 -o airam-rnnoise ../../../tools/airam-rnnoise.c -Irnnoise-0.2/include rnnoise-0.2/.libs/librnnoise.a -lm
fi
echo "ok — עכשיו: python3 run.py"
