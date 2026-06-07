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
SOURCE_PW="airam"        # סיסמת source פנימית (פנימי בלבד; מאזינים לא צריכים סיסמה)

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
  git cmake build-essential pkg-config curl \
  libusb-1.0-0-dev \
  libsoapysdr-dev soapysdr-tools \
  libmp3lame-dev libshout3-dev \
  libconfig++-dev libfftw3-dev \
  icecast2 python3 python3-flask

# ----------------------------------------------------------------------------
# 2. SDRplay API  (הורדה + חילוץ + התקנה אוטומטית, ללא אישור רישיון אינטראקטיבי)
# ----------------------------------------------------------------------------
if ldconfig -p | grep -q libsdrplay_api; then
  log "SDRplay API כבר מותקן - מדלג."
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
  # קבצי כותרת (נדרשים לבניית SoapySDRPlay3)
  cp -f "$EXT"/*.h /usr/local/include/ 2>/dev/null || true
  cp -f "$EXT"/inc/*.h /usr/local/include/ 2>/dev/null || true
  # שירות ה-API + כללי udev
  cp -f "$EXT/$APIARCH/sdrplay_apiService" /usr/local/bin/
  chmod 755 /usr/local/bin/sdrplay_apiService
  cp -f "$EXT"/*.rules /etc/udev/rules.d/ 2>/dev/null || true
  udevadm control --reload-rules 2>/dev/null || true
  ldconfig
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
if command -v rtl_airband >/dev/null 2>&1; then
  log "RTLSDR-Airband כבר מותקן - מדלג."
else
  log "בונה RTLSDR-Airband..."
  cd "$BUILD_DIR"
  [[ -d RTLSDR-Airband ]] || git clone https://github.com/rtl-airband/RTLSDR-Airband.git
  cd RTLSDR-Airband && rm -rf build && mkdir build && cd build
  cmake -DPLATFORM=native .. && make -j"$(nproc)" && make install
fi

# ----------------------------------------------------------------------------
# 5. Icecast2  -  ללא סיסמה למאזין (סיסמת source פנימית קבועה)
# ----------------------------------------------------------------------------
log "מגדיר Icecast2 (מאזינים ללא סיסמה)..."
# ברירת המחדל של דביאן משתמשת ב-'hackme' - מחליפים לערך הפנימי הקבוע שלנו
sed -i "s/>hackme</>${SOURCE_PW}</g" /etc/icecast2/icecast.xml || true
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
mkdir -p /opt/airam
cp -r "$REPO_DIR/webtune" /opt/airam/

# ----------------------------------------------------------------------------
# 8. שירותי systemd
# ----------------------------------------------------------------------------
log "מתקין שירותי systemd ..."
cp "$REPO_DIR/systemd/sdrplay.service"     /etc/systemd/system/
cp "$REPO_DIR/systemd/rtl_airband.service" /etc/systemd/system/
cp "$REPO_DIR/systemd/airam-web.service"   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now sdrplay.service || warn "sdrplay.service לא עלה - בדוק חיבור ה-RSP1B."
sleep 2
systemctl enable --now rtl_airband.service || warn "rtl_airband לא עלה - בדוק journalctl -u rtl_airband"
systemctl enable --now airam-web.service

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
log "ההתקנה הסתיימה ✅"
cat <<EOF

  🎧 פתח בטלפון את בורר התדרים:
        http://${IP:-<IP-של-ה-Pi>}:8080

  שם בוחרים פריסט או מקלידים תדר חופשי, וההאזנה מתחילה.
  מאזינים לא צריכים שום סיסמה.

  לוגים:  sudo journalctl -u rtl_airband -f
          sudo journalctl -u airam-web -f
EOF
