#!/usr/bin/env bash
# ============================================================================
#  AIR-AM  -  התקנה מלאה ל-Raspberry Pi (Pi 5 / Pi 4, Raspberry Pi OS 64-bit)
# ----------------------------------------------------------------------------
#  מתקין הכל אוטומטית: SDRplay API, SoapySDR + SoapySDRPlay3, RTLSDR-Airband,
#  Icecast2 (ללא סיסמה למאזין), שרת בורר התדרים הוובי, ושירותי systemd.
#
#  ⚠️ הרץ *על ה-Pi עצמו*:   chmod +x install.sh && sudo ./install.sh
# ============================================================================
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -n "${SUDO_USER:-}" ]]; then BUILD_DIR="/home/$SUDO_USER/air-am-build"; else BUILD_DIR="/root/air-am-build"; fi
SDRPLAY_VER="3.15.2"     # אם יצא עדכון: עדכן כאן (ודא שהקובץ קיים באתר sdrplay)
SOURCE_PW="airam"        # סיסמת source פנימית; חייבת להיות זהה ל-SOURCE_PW ב-webtune/app.py

log()  { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
warn() { printf '\n\033[1;33m[!] %s\033[0m\n' "$*"; }
die()  { printf '\n\033[1;31m[X] %s\033[0m\n' "$*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "יש להריץ עם sudo (root)."

mkdir -p "$BUILD_DIR"

# ----------------------------------------------------------------------------
# 1. תלויות מערכת
# ----------------------------------------------------------------------------
log "מתקין תלויות (apt)..."
apt-get update
apt-get install -y \
  git cmake build-essential pkg-config curl usbutils \
  libusb-1.0-0-dev \
  libsoapysdr-dev soapysdr-tools \
  libmp3lame-dev libshout3-dev \
  libconfig++-dev libfftw3-dev \
  icecast2 python3 python3-flask

# ----------------------------------------------------------------------------
# 2. SDRplay API  (הורדה + חילוץ + התקנה אוטומטית, ללא אישור רישיון אינטראקטיבי)
# ----------------------------------------------------------------------------
# בדיקה תלוית-גרסה *מלאה* (marker, לא שם קובץ ה-so שמכיל רק major.minor):
# עדכון SDRPLAY_VER - גם patch level כמו 3.15.3 - יתקין מחדש בהרצה הבאה.
SDRPLAY_MARK="/usr/local/share/airam/sdrplay-api.version"
if ldconfig -p | grep -q libsdrplay_api && [[ "$(cat "$SDRPLAY_MARK" 2>/dev/null)" == "$SDRPLAY_VER" ]]; then
  log "SDRplay API v${SDRPLAY_VER} כבר מותקן - מדלג."
else
  log "מתקין SDRplay API v${SDRPLAY_VER}..."
  case "$(uname -m)" in
    aarch64) APIARCH="arm64" ;;
    armv7l)  APIARCH="armv7l" ;;
    x86_64)  APIARCH="x86_64" ;;
    *)       APIARCH="arm64" ;;
  esac
  RUN="/tmp/sdrplay.run"; EXT="/tmp/sdrplay_api"
  curl -fSL --retry 4 -o "$RUN" \
    "https://www.sdrplay.com/software/SDRplay_RSP_API-Linux-${SDRPLAY_VER}.run" \
    || die "הורדת SDRplay API נכשלה. בדוק רשת או עדכן SDRPLAY_VER בראש הסקריפט."
  chmod +x "$RUN"
  rm -rf "$EXT"
  # חילוץ ללא הרצה (makeself) => עוקפים את אישור הרישיון האינטראקטיבי
  "$RUN" --noexec --target "$EXT"

  [[ -d "$EXT/$APIARCH" ]] || APIARCH="$(basename "$(find "$EXT" -maxdepth 1 -type d -name '*64*' | head -1)")"
  [[ -n "$APIARCH" && -d "$EXT/$APIARCH" ]] || die "לא נמצאה ספריית ארכיטקטורה בתוך ה-API."

  # ספריות
  LIB="$(ls "$EXT/$APIARCH"/libsdrplay_api.so.* | head -1)"
  cp -f "$LIB" /usr/local/lib/
  BASE="$(basename "$LIB")"                       # libsdrplay_api.so.3.15
  ln -sf "/usr/local/lib/$BASE" /usr/local/lib/libsdrplay_api.so.3
  ln -sf /usr/local/lib/libsdrplay_api.so.3 /usr/local/lib/libsdrplay_api.so
  # קבצי כותרת (נדרשים לבניית SoapySDRPlay3) - מוצאים איפה שהם, ונכשלים אם חסר
  SDR_HDR="$(find "$EXT" -name sdrplay_api.h -print -quit)"
  [[ -n "$SDR_HDR" ]] || die "sdrplay_api.h לא נמצא ב-API שחולץ ($EXT)."
  cp -f "$(dirname "$SDR_HDR")"/*.h /usr/local/include/
  # שירות ה-API + כללי udev
  cp -f "$EXT/$APIARCH/sdrplay_apiService" /usr/local/bin/
  chmod 755 /usr/local/bin/sdrplay_apiService
  cp -f "$EXT"/*.rules /etc/udev/rules.d/ 2>/dev/null || true
  udevadm control --reload-rules 2>/dev/null || true
  ldconfig
  mkdir -p "$(dirname "$SDRPLAY_MARK")"
  printf '%s' "$SDRPLAY_VER" > "$SDRPLAY_MARK"
fi

# ----------------------------------------------------------------------------
# 3. SoapySDRPlay3
# ----------------------------------------------------------------------------
if SoapySDRUtil --info 2>/dev/null | grep -qi sdrplay; then
  log "SoapySDRPlay3 כבר מותקן - מדלג."
else
  log "בונה SoapySDRPlay3..."
  cd "$BUILD_DIR"
  [[ -d SoapySDRPlay3 ]] || git clone https://github.com/pothosware/SoapySDRPlay3.git
  cd SoapySDRPlay3 && rm -rf build && mkdir build && cd build
  cmake .. && make -j"$(nproc)" && make install && ldconfig
fi

# ----------------------------------------------------------------------------
# 4. RTLSDR-Airband
# ----------------------------------------------------------------------------
# ה-patch ודגלי הבנייה מוגדרים כמשתנים כדי שחתימת הבנייה תיגזר מהם אוטומטית:
# כל שינוי בהם => חתימה חדשה => בנייה מחדש בעדכון הבא (בלי לזכור לעדכן marker ידני).
#
# למה patch? ברירת המחדל של rtl_airband היא MP3 ב-VBR (vbr_mtrh), שבו LAME
# *מתעלם* מ-brate: בשקט (squelch סגור) הזרם צונח ל-~1KB/s והדפדפן ממלא את
# ה-buffer ההתחלתי שלו ~30 שניות (זה מקור ה-latency!). עוברים ל-CBR 48kbps
# @ 16kHz - זרם קבוע וצפוף (6KB/s) => הנגן מתחיל תוך שניות. אין לזה שום knob
# בהגדרות/בנייה של rtl_airband, ולכן patch על המקור. (אומת מול מקור LAME:
# brate נאכף רק ב-vbr_off; ‏48kbps חוקי ל-MPEG-2 @ 16kHz.)
RTL_PATCH='
s/lame_set_VBR(lame, vbr_mtrh);/lame_set_VBR(lame, vbr_off);/
s/lame_set_brate(lame, 16);/lame_set_brate(lame, 48);/
s/lame_set_out_samplerate(lame, MP3_RATE);/lame_set_out_samplerate(lame, 16000);/
s/sprintf(samplerates, "%d", MP3_RATE);/sprintf(samplerates, "%d", 16000);/'
# -DNFM=ON: תמיכת NFM כבויה כברירת מחדל; הממשק מציע NFM אז חובה להפעיל,
# אחרת בחירת NFM => "unknown modulation" וקריסה.
RTL_CMAKE_FLAGS="-DPLATFORM=native -DNFM=ON"
RTL_BUILD_SIG="$(printf '%s' "$RTL_PATCH $RTL_CMAKE_FLAGS" | sha256sum | awk '{print $1}')"
AIRAM_RTL_MARK="/usr/local/share/airam/rtl_airband.build-sig"

if command -v rtl_airband >/dev/null 2>&1 && [[ "$(cat "$AIRAM_RTL_MARK" 2>/dev/null)" == "$RTL_BUILD_SIG" ]]; then
  log "RTLSDR-Airband (NFM + CBR 48k) כבר מותקן - מדלג."
else
  log "בונה RTLSDR-Airband (NFM + CBR 48kbps ל-latency נמוך)..."
  cd "$BUILD_DIR"
  [[ -d RTLSDR-Airband ]] || git clone https://github.com/rtl-airband/RTLSDR-Airband.git
  cd RTLSDR-Airband
  # איפוס לפני patch (אידמפוטנטי); עץ פגום (clone שנקטע) => משכפלים מחדש
  git checkout -- src/output.cpp 2>/dev/null || {
    warn "עץ ה-build פגום - משכפל מחדש את RTLSDR-Airband."
    cd "$BUILD_DIR" && rm -rf RTLSDR-Airband
    git clone https://github.com/rtl-airband/RTLSDR-Airband.git && cd RTLSDR-Airband
  }
  sed -i "$RTL_PATCH" src/output.cpp
  # מאמתים את *כל* ההחלפות. החלפה שהוחמצה (upstream השתנה) => בונים בכל זאת
  # (רדיו עובד עדיף מהתקנה מתה) אבל *לא* כותבים marker, כדי שהבנייה תנוסה
  # שוב בעדכון הבא ולא תישאר "הצלחה" שקטה עם בינארי חצי-מתוקן.
  PATCH_OK=1
  for pat in 'lame_set_VBR(lame, vbr_off);' 'lame_set_brate(lame, 48);' \
             'lame_set_out_samplerate(lame, 16000);' 'sprintf(samplerates, "%d", 16000);'; do
    grep -qF "$pat" src/output.cpp || { PATCH_OK=0; warn "ה-patch לא נתפס: '$pat' (הקוד השתנה ב-upstream?)"; }
  done
  rm -rf build && mkdir build && cd build
  cmake $RTL_CMAKE_FLAGS .. && make -j"$(nproc)" && make install
  if [[ $PATCH_OK -eq 1 ]]; then
    mkdir -p "$(dirname "$AIRAM_RTL_MARK")"
    printf '%s' "$RTL_BUILD_SIG" > "$AIRAM_RTL_MARK"
    rm -f /usr/local/share/airam/.rtl_airband-build*   # markers ישנים מגרסאות קודמות
  else
    warn "patch ה-CBR הוחל חלקית - ייתכן latency גבוה. עדכן את הריפו והרץ שוב."
  fi
fi

# ----------------------------------------------------------------------------
# 5. Icecast2  -  ללא סיסמה למאזין (סיסמת source פנימית קבועה)
# ----------------------------------------------------------------------------
log "מגדיר Icecast2 (מאזינים ללא סיסמה, latency נמוך)..."
ICE=/etc/icecast2/icecast.xml
# סיסמת source פנימית (אידמפוטנטי; מעוגן לתחילת שורה => לא נוגע בתגיות שבתוך הערות XML)
sed -i -E "s#^([[:space:]]*)<source-password>[^<]*</source-password>#\1<source-password>${SOURCE_PW}</source-password>#" "$ICE"

# מבטיח ערך לתג בתוך <limits>: מחליף אם קיים *ולא בהערה*, אחרת מזריק לפני </limits>.
# קריטי: ב-Debian חלק מהתגיות (burst-on-connect) מגיעות בהערה <!-- ... --> - sed
# תמים "מחליף" את הערך בתוך ההערה ולא משנה כלום => burst נשאר 64KB ≈ 30 שניות latency.
ensure_limit() {  # $1=tag  $2=value
  if grep -Eq "^[[:space:]]*<$1>" "$ICE"; then
    sed -i -E "s#^([[:space:]]*)<$1>[^<]*</$1>#\1<$1>$2</$1>#" "$ICE"
  elif grep -q "</limits>" "$ICE"; then
    sed -i -E "s#^([[:space:]]*)</limits>#\1    <$1>$2</$1>\n\1</limits>#" "$ICE"
  else
    warn "לא נמצא <limits> ב-$ICE - הוסף ידנית: <$1>$2</$1>"
  fi
}
# burst-size=0 => אין prefill של buffer ישן בחיבור (זה היה מקור ה-30 שניות).
# queue-size גדול מ-burst (חובה, אחרת ה-source נזרק) ולא מוסיף latency למאזין שעומד בקצב.
ensure_limit burst-on-connect 0
ensure_limit burst-size 0
ensure_limit queue-size 65536
ensure_limit source-timeout 10
# אפשר את השירות
[[ -f /etc/default/icecast2 ]] && sed -i 's/^ENABLE=.*/ENABLE=true/' /etc/default/icecast2 || true
grep -q "^ENABLE=" /etc/default/icecast2 2>/dev/null || echo "ENABLE=true" >> /etc/default/icecast2
systemctl enable icecast2
systemctl restart icecast2

# ----------------------------------------------------------------------------
# 6. קובץ הגדרות התחלתי + תיקיית state
# ----------------------------------------------------------------------------
log "מתקין קובץ הגדרות התחלתי..."
mkdir -p /etc/rtl_airband /var/lib/airam
[[ -f /etc/rtl_airband/airband.conf ]] || cp "$REPO_DIR/config/airband.conf" /etc/rtl_airband/airband.conf

# ----------------------------------------------------------------------------
# 7. שרת בורר התדרים (web tuner)
# ----------------------------------------------------------------------------
log "מתקין את שרת הווב ל-/opt/airam ..."
mkdir -p /opt/airam/webtune
cp -r "$REPO_DIR/webtune/." /opt/airam/webtune/   # אידמפוטנטי (לא יוצר webtune/webtune)
# שער המוכנות ל-SDRplay (ExecStartPre של rtl_airband)
cp "$REPO_DIR/scripts/airam-wait-sdrplay" /usr/local/bin/
chmod 755 /usr/local/bin/airam-wait-sdrplay
# כלל udev: הפעלה מחדש של שירותי ה-SDR בעת חיבור ה-RSP1B (התאוששות מהירה)
cp "$REPO_DIR/udev/99-airam.rules" /etc/udev/rules.d/
udevadm control --reload-rules 2>/dev/null || true

# ----------------------------------------------------------------------------
# 8. שירותי systemd
# ----------------------------------------------------------------------------
log "מתקין שירותי systemd ..."
cp "$REPO_DIR/systemd/sdrplay.service"     /etc/systemd/system/
cp "$REPO_DIR/systemd/rtl_airband.service" /etc/systemd/system/
cp "$REPO_DIR/systemd/airam-web.service"   /etc/systemd/system/
systemctl daemon-reload
systemctl enable sdrplay.service rtl_airband.service airam-web.service
# restart (ולא enable --now שהוא no-op לשירות שכבר רץ!) - אחרת בעדכון
# הבינארי/הקוד/ה-units החדשים לא נטענים והשירותים ממשיכים לרוץ עם הישנים.
systemctl restart sdrplay.service || warn "sdrplay.service לא עלה - בדוק חיבור ה-RSP1B."
sleep 2
systemctl restart rtl_airband.service || warn "rtl_airband לא עלה - בדוק journalctl -u rtl_airband"
systemctl restart airam-web.service || warn "airam-web לא עלה - בדוק journalctl -u airam-web"

IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
log "ההתקנה הסתיימה ✅"
cat <<EOF

  🎧 פתח בטלפון את בורר התדרים:
        http://${IP:-<IP-של-ה-Pi>}:8080

  שם בוחרים פריסט או מקלידים תדר חופשי, וההאזנה מתחילה.
  מאזינים לא צריכים שום סיסמה.

  לוגים:  sudo journalctl -u rtl_airband -f
          sudo journalctl -u airam-web -f
EOF
