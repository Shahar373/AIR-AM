#!/usr/bin/env bash
# ============================================================================
#  AIR-AM  -  סקריפט התקנה ל-Raspberry Pi (Pi 5 / Pi 4, Raspberry Pi OS 64-bit)
# ----------------------------------------------------------------------------
#  מתקין: תלויות בנייה, SoapySDR, SoapySDRPlay3, RTLSDR-Airband, Icecast2,
#  ומגדיר שירות systemd שמריץ הכל אוטומטית.
#
#  ⚠️ הרץ סקריפט זה *על ה-Pi עצמו*, לא על מחשב אחר.
#  שימוש:   chmod +x install.sh && sudo ./install.sh
# ============================================================================
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="${HOME}/air-am-build"
log() { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
warn() { printf '\n\033[1;33m[!] %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31m[X] %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "יש להריץ עם sudo (root)."

# ----------------------------------------------------------------------------
# 1. תלויות מערכת
# ----------------------------------------------------------------------------
log "מתקין תלויות (apt)..."
apt-get update
apt-get install -y \
  git cmake build-essential pkg-config \
  libusb-1.0-0-dev \
  libsoapysdr-dev soapysdr-tools \
  libmp3lame-dev libshout3-dev \
  libconfig++-dev libfftw3-dev \
  icecast2

mkdir -p "$BUILD_DIR"

# ----------------------------------------------------------------------------
# 2. SDRplay API  (נדרש ל-RSP1B)
# ----------------------------------------------------------------------------
# ה-API של SDRplay הוא קובץ קנייני שמורידים מהאתר הרשמי (מאחורי טופס),
# ולכן אי אפשר להוריד אותו אוטומטית כאן. בדיקה אם כבר מותקן:
if [[ -e /usr/local/lib/libsdrplay_api.so* ]] || ldconfig -p | grep -q sdrplay_api; then
  log "SDRplay API כבר מותקן."
else
  warn "SDRplay API לא נמצא. בצע ידנית פעם אחת:"
  cat <<'EOF'
  ------------------------------------------------------------------
  1) הורד את ה-API ל-Linux ARM64 מ:
        https://www.sdrplay.com/downloads/   (בחר 'API/HW Driver - V3.xx (Linux)')
  2) הקובץ נראה כך:  SDRplay_RSP_API-Linux-3.15.x.run
  3) הרץ:
        chmod +x SDRplay_RSP_API-Linux-*.run
        sudo ./SDRplay_RSP_API-Linux-*.run
     (אשר את הרישיון; הוא יתקין שירות בשם sdrplay)
  4) ודא שהשירות רץ:
        sudo systemctl status sdrplay
  ואז הרץ שוב את ./install.sh
  ------------------------------------------------------------------
EOF
  die "התקן את SDRplay API והרץ שוב."
fi

# ----------------------------------------------------------------------------
# 3. SoapySDRPlay3  (תוסף SoapySDR ל-SDRplay)
# ----------------------------------------------------------------------------
if SoapySDRUtil --info 2>/dev/null | grep -qi sdrplay; then
  log "SoapySDRPlay3 כבר מותקן."
else
  log "בונה SoapySDRPlay3..."
  cd "$BUILD_DIR"
  [[ -d SoapySDRPlay3 ]] || git clone https://github.com/pothosware/SoapySDRPlay3.git
  cd SoapySDRPlay3
  rm -rf build && mkdir build && cd build
  cmake ..
  make -j"$(nproc)"
  make install
  ldconfig
fi

# ----------------------------------------------------------------------------
# 4. RTLSDR-Airband
# ----------------------------------------------------------------------------
if command -v rtl_airband >/dev/null 2>&1; then
  log "RTLSDR-Airband כבר מותקן (דלג, או מחק /usr/local/bin/rtl_airband לבנייה מחדש)."
else
  log "בונה RTLSDR-Airband..."
  cd "$BUILD_DIR"
  [[ -d RTLSDR-Airband ]] || git clone https://github.com/rtl-airband/RTLSDR-Airband.git
  cd RTLSDR-Airband
  rm -rf build && mkdir build && cd build
  # PLATFORM=native => אופטימיזציה ל-CPU של ה-Pi הנוכחי (NEON)
  cmake -DPLATFORM=native ..
  make -j"$(nproc)"
  make install
fi

# ----------------------------------------------------------------------------
# 5. התקנת קובץ ההגדרות
# ----------------------------------------------------------------------------
log "מתקין קובץ הגדרות ל-/etc/rtl_airband/airband.conf ..."
mkdir -p /etc/rtl_airband
if [[ -f /etc/rtl_airband/airband.conf ]]; then
  warn "קיים airband.conf - שומר גיבוי ב-airband.conf.bak ולא דורס."
  cp -n "$REPO_DIR/config/airband.conf" /etc/rtl_airband/airband.conf.new
  echo "    הגרסה החדשה מהריפו נשמרה כ-airband.conf.new להשוואה."
else
  cp "$REPO_DIR/config/airband.conf" /etc/rtl_airband/airband.conf
fi

# ----------------------------------------------------------------------------
# 6. Icecast2
# ----------------------------------------------------------------------------
log "מפעיל את Icecast2 ..."
systemctl enable icecast2
systemctl restart icecast2 || warn "Icecast2 לא עלה - ייתכן שצריך להגדיר סיסמאות ב-/etc/icecast2/icecast.xml"
cat <<'EOF'

  ℹ️  סיסמאות Icecast: ערוך /etc/icecast2/icecast.xml והגדר:
        <source-password>   = הסיסמה שב-airband.conf (password של ה-source)
        <admin-password>    = סיסמת ניהול
        <hostname>          = כתובת ה-IP של ה-Pi
      ואז:  sudo systemctl restart icecast2
EOF

# ----------------------------------------------------------------------------
# 7. שירות systemd
# ----------------------------------------------------------------------------
log "מתקין שירות systemd ..."
cp "$REPO_DIR/systemd/rtl_airband.service" /etc/systemd/system/rtl_airband.service
systemctl daemon-reload
systemctl enable rtl_airband

log "ההתקנה הסתיימה ✅"
cat <<EOF

  השלבים הבאים:
   1) ערוך תדרים:   sudo nano /etc/rtl_airband/airband.conf
   2) הגדר סיסמאות Icecast (ראה למעלה) והפעל מחדש icecast2.
   3) הפעל:         sudo systemctl start rtl_airband
   4) בדוק לוג:     sudo journalctl -u rtl_airband -f
   5) האזן מהטלפון: http://<IP-של-ה-Pi>:8000/guard.mp3   (ב-VLC או בדפדפן)

  IP של ה-Pi:  $(hostname -I 2>/dev/null | awk '{print $1}')
EOF
