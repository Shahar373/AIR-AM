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
  zlib1g-dev libxml2-dev libglib2.0-dev \
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
    *)       die "ארכיטקטורה לא נתמכת: $(uname -m). נתמכות: aarch64 / armv7l / x86_64." ;;
  esac
  RUN="/tmp/sdrplay.run"; EXT="/tmp/sdrplay_api"
  curl -fSL --retry 4 -o "$RUN" \
    "https://www.sdrplay.com/software/SDRplay_RSP_API-Linux-${SDRPLAY_VER}.run" \
    || die "הורדת SDRplay API נכשלה. בדוק רשת או עדכן SDRPLAY_VER בראש הסקריפט."
  chmod +x "$RUN"
  rm -rf "$EXT"
  # חילוץ ללא הרצה (makeself) => עוקפים את אישור הרישיון האינטראקטיבי
  "$RUN" --noexec --target "$EXT"

  # שם הספרייה בארכיון לא תאם בדיוק => מאתרים לפי דפוס הארכיטקטורה הספציפי
  # (לא "כל 64" - אחרת על armv7l היינו בוחרים בטעות ספריית arm64).
  if [[ ! -d "$EXT/$APIARCH" ]]; then
    case "$APIARCH" in
      arm64)  ALT="$(find "$EXT" -maxdepth 1 -type d \( -name '*aarch64*' -o -name '*arm64*' \) | head -1)" ;;
      armv7l) ALT="$(find "$EXT" -maxdepth 1 -type d -name '*armv7*' | head -1)" ;;
      x86_64) ALT="$(find "$EXT" -maxdepth 1 -type d \( -name '*x86_64*' -o -name '*amd64*' \) | head -1)" ;;
    esac
    [[ -n "${ALT:-}" ]] && APIARCH="$(basename "$ALT")"
  fi
  [[ -n "$APIARCH" && -d "$EXT/$APIARCH" ]] || die "לא נמצאה ספריית ארכיטקטורה בתוך ה-API."

  # ספריות: בוחרים את הקובץ המלא libsdrplay_api.so.MAJOR.MINOR (שני מקטעי גרסה),
  # לא את ה-symlink הקצר .so.3 - אחרת ה-ln למטה היה מצביע על עצמו.
  LIB="$(ls "$EXT/$APIARCH"/libsdrplay_api.so.*.* 2>/dev/null | head -1)"
  [[ -n "$LIB" ]] || LIB="$(ls "$EXT/$APIARCH"/libsdrplay_api.so.* 2>/dev/null | head -1)"
  [[ -n "$LIB" ]] || die "לא נמצאה ספריית libsdrplay_api ב-API שחולץ."
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
#
# patch שני (STATS_FILE_TIMING): rtl_airband חונק את כתיבת קובץ ה-stats לכל
# 15 שניות (קבוע קשיח ב-output.cpp). מדדי ה-signal/noise עצמם מתעדכנים בכל
# דגימה (moving-average ב-squelch.cpp), אז 15 שניות רק *מסתירים* נתון טרי =>
# כיוון אנטנה בזמן אמת בלתי אפשרי. מורידים ל-1 שנייה: הקובץ ב-/run (tmpfs,
# בלי שחיקת SD) וכתיבה לערוץ יחיד זניחה על Pi.
RTL_PATCH='
s/lame_set_VBR(lame, vbr_mtrh);/lame_set_VBR(lame, vbr_off);/
s/lame_set_brate(lame, 16);/lame_set_brate(lame, 48);/
s/lame_set_out_samplerate(lame, MP3_RATE);/lame_set_out_samplerate(lame, 16000);/
s/sprintf(samplerates, "%d", MP3_RATE);/sprintf(samplerates, "%d", 16000);/
s/static const double STATS_FILE_TIMING = 15.0;/static const double STATS_FILE_TIMING = 1.0;/'
# -DNFM=ON: תמיכת NFM כבויה כברירת מחדל; הממשק מציע NFM אז חובה להפעיל,
# אחרת בחירת NFM => "unknown modulation" וקריסה.
# RTL_VER נעוץ ל-release ידוע-טוב: ה-patch הוא sed על המקור, ו-clone של ענף
# ברירת המחדל היה נשבר בכל שינוי upstream. הגרסה חלק מחתימת הבנייה =>
# העלאת RTL_VER כאן מספיקה כדי לגרור בנייה מחדש בעדכון הבא.
RTL_VER="v5.2.0"   # תבניות ה-patch אומתו מול src/output.cpp של ה-tag הזה
RTL_CMAKE_FLAGS="-DPLATFORM=native -DNFM=ON"
RTL_BUILD_SIG="$(printf '%s' "$RTL_VER $RTL_PATCH $RTL_CMAKE_FLAGS" | sha256sum | awk '{print $1}')"
AIRAM_RTL_MARK="/usr/local/share/airam/rtl_airband.build-sig"

if command -v rtl_airband >/dev/null 2>&1 && [[ "$(cat "$AIRAM_RTL_MARK" 2>/dev/null)" == "$RTL_BUILD_SIG" ]]; then
  log "RTLSDR-Airband ${RTL_VER} (NFM + CBR 48k + stats 1s) כבר מותקן - מדלג."
else
  log "בונה RTLSDR-Airband ${RTL_VER} (NFM + CBR 48kbps + stats כל 1s)..."
  cd "$BUILD_DIR"
  # עץ קיים שאינו על ה-tag הנעוץ (התקנה ישנה לא-נעוצה / העלאת גרסה) => משכפלים מחדש
  if [[ -d RTLSDR-Airband ]] && \
     [[ "$(git -C RTLSDR-Airband describe --tags --exact-match 2>/dev/null)" != "$RTL_VER" ]]; then
    rm -rf RTLSDR-Airband
  fi
  [[ -d RTLSDR-Airband ]] || git clone --depth 1 --branch "$RTL_VER" https://github.com/rtl-airband/RTLSDR-Airband.git
  cd RTLSDR-Airband
  # איפוס לפני patch (אידמפוטנטי); עץ פגום (clone שנקטע) => משכפלים מחדש
  git checkout -- src/output.cpp 2>/dev/null || {
    warn "עץ ה-build פגום - משכפל מחדש את RTLSDR-Airband."
    cd "$BUILD_DIR" && rm -rf RTLSDR-Airband
    git clone --depth 1 --branch "$RTL_VER" https://github.com/rtl-airband/RTLSDR-Airband.git && cd RTLSDR-Airband
  }
  sed -i "$RTL_PATCH" src/output.cpp
  # מאמתים את *כל* ההחלפות. החלפה שהוחמצה (upstream השתנה) => בונים בכל זאת
  # (רדיו עובד עדיף מהתקנה מתה) אבל *לא* כותבים marker, כדי שהבנייה תנוסה
  # שוב בעדכון הבא ולא תישאר "הצלחה" שקטה עם בינארי חצי-מתוקן.
  PATCH_OK=1
  for pat in 'lame_set_VBR(lame, vbr_off);' 'lame_set_brate(lame, 48);' \
             'lame_set_out_samplerate(lame, 16000);' 'sprintf(samplerates, "%d", 16000);' \
             'static const double STATS_FILE_TIMING = 1.0;'; do
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
# 4b. libacars + acarsdec  (מצב משולב: פענוח ACARS דרך SoapySDR/SDRplay)
# ----------------------------------------------------------------------------
# מצב ה-ACARS בממשק מריץ acarsdec על אותו RSP1B (בהחלפה עם rtl_airband).
# libacars נותן פענוח ARINC-622 (אופציונלי אך מומלץ); acarsdec נבנה עם -Dsoapy=ON.
# שער גרסה (לא רק קיום): dumpvdl2 v2.6.0 דורש libacars ≥2.1.0 — התקנה ישנה
# מגרסה קודמת של הסקריפט תיבנה מחדש אוטומטית.
if pkg-config --atleast-version=2.1.0 libacars-2 2>/dev/null; then
  log "libacars (≥2.1.0) כבר מותקן - מדלג."
else
  log "בונה libacars..."
  cd "$BUILD_DIR"
  # checkout קיים => מעדכנים (לא הורסים): "rm -rf ואז clone" היה נכשל בלי רשת
  # ומפיל את כל ההתקנה (set -euo pipefail) גם כשהיה כבר מקור זמין לבנייה.
  if [[ -d libacars ]]; then
    git -C libacars pull --ff-only || true
  else
    git clone https://github.com/szpajder/libacars.git
  fi
  cd libacars && rm -rf build && mkdir build && cd build
  cmake .. && make -j"$(nproc)" && make install && ldconfig
fi

# acarsdec: בנייה מחדש כשמשתנים דגלי ה-cmake (חתימה => אידמפוטנטי כמו rtl_airband)
ACARS_CMAKE_FLAGS="-Dsoapy=ON"
ACARS_BUILD_SIG="$(printf '%s' "$ACARS_CMAKE_FLAGS" | sha256sum | awk '{print $1}')"
AIRAM_ACARS_MARK="/usr/local/share/airam/acarsdec.build-sig"
if command -v acarsdec >/dev/null 2>&1 && [[ "$(cat "$AIRAM_ACARS_MARK" 2>/dev/null)" == "$ACARS_BUILD_SIG" ]]; then
  log "acarsdec (SoapySDR) כבר מותקן - מדלג."
else
  log "בונה acarsdec (SoapySDR)..."
  cd "$BUILD_DIR"
  [[ -d acarsdec ]] || git clone https://github.com/TLeconte/acarsdec.git
  cd acarsdec && rm -rf build && mkdir build && cd build
  # PKG_CONFIG_PATH => כדי ש-cmake ימצא את libacars-2 שהותקן ל-/usr/local
  PKG_CONFIG_PATH="/usr/local/lib/pkgconfig:${PKG_CONFIG_PATH:-}" \
    cmake $ACARS_CMAKE_FLAGS .. && make -j"$(nproc)" && make install
  command -v acarsdec >/dev/null 2>&1 || die "בניית acarsdec נכשלה (בדוק SoapySDR/libacars)."
  mkdir -p "$(dirname "$AIRAM_ACARS_MARK")"
  printf '%s' "$ACARS_BUILD_SIG" > "$AIRAM_ACARS_MARK"
fi

# ----------------------------------------------------------------------------
# 4c. dumpvdl2  (מצב VDL2: פענוח VDL Mode 2 דרך SoapySDR/SDRplay)
# ----------------------------------------------------------------------------
# מצב ה-VDL2 בממשק מריץ dumpvdl2 על אותו RSP1B (בהחלפה עם קול/ACARS). נעוץ
# ל-tag ידוע-טוב (v2.6.0 = הראשון עם תמיכת RSP1B בדרייבר הנייטיבי; מסלול הקלט
# בפועל הוא SoapySDR — המוכח אצלנו עם acarsdec). SoapySDR/libacars מזוהים
# אוטומטית ע"י cmake (בלי דגלים); glib2 הותקן בשלב 1.
DUMPVDL2_VER="v2.6.0"
DUMPVDL2_CMAKE_FLAGS=""
DUMPVDL2_BUILD_SIG="$(printf '%s' "$DUMPVDL2_VER $DUMPVDL2_CMAKE_FLAGS" | sha256sum | awk '{print $1}')"
AIRAM_VDL2_MARK="/usr/local/share/airam/dumpvdl2.build-sig"
if command -v dumpvdl2 >/dev/null 2>&1 && [[ "$(cat "$AIRAM_VDL2_MARK" 2>/dev/null)" == "$DUMPVDL2_BUILD_SIG" ]]; then
  log "dumpvdl2 ${DUMPVDL2_VER} כבר מותקן - מדלג."
else
  log "בונה dumpvdl2 ${DUMPVDL2_VER} (SoapySDR + libacars)..."
  cd "$BUILD_DIR"
  # עץ קיים שאינו על ה-tag הנעוץ (העלאת גרסה / clone שנקטע) => משכפלים מחדש
  if [[ -d dumpvdl2 ]] && \
     [[ "$(git -C dumpvdl2 describe --tags --exact-match 2>/dev/null)" != "$DUMPVDL2_VER" ]]; then
    rm -rf dumpvdl2
  fi
  [[ -d dumpvdl2 ]] || git clone --depth 1 --branch "$DUMPVDL2_VER" https://github.com/szpajder/dumpvdl2.git
  cd dumpvdl2 && rm -rf build && mkdir build && cd build
  # PKG_CONFIG_PATH => כדי ש-cmake ימצא את libacars-2 שהותקן ל-/usr/local
  PKG_CONFIG_PATH="/usr/local/lib/pkgconfig:${PKG_CONFIG_PATH:-}" \
    cmake $DUMPVDL2_CMAKE_FLAGS .. && make -j"$(nproc)" && make install && ldconfig
  command -v dumpvdl2 >/dev/null 2>&1 || die "בניית dumpvdl2 נכשלה (בדוק SoapySDR/libacars/glib2)."
  # אזהרה מוקדמת (לא כישלון): הבינארי חייב לכלול את קלט ה-SoapySDR שה-unit משתמש בו
  dumpvdl2 --version 2>&1 | grep -qi soapysdr \
    || warn "dumpvdl2 נבנה בלי SoapySDR - מצב VDL2 לא יעבוד. ודא libsoapysdr-dev והרץ שוב."
  mkdir -p "$(dirname "$AIRAM_VDL2_MARK")"
  printf '%s' "$DUMPVDL2_BUILD_SIG" > "$AIRAM_VDL2_MARK"
fi

# ----------------------------------------------------------------------------
# 4d. inmarsat-sniffer  (מצב SATCOM: ACARS דרך לוויין Inmarsat, SoapySDR/SDRplay)
# ----------------------------------------------------------------------------
# מצב ה-SATCOM בממשק מריץ inmarsat-sniffer על אותו RSP1B (בהחלפה עם שאר
# המצבים). בינארי CLI עצמאי (*בלי* Qt/GUI, בניגוד ל-JAERO המקורי) שמבוסס על
# ליבת ה-DSP של JAERO (jontio/JAERO, MIT) — נעוץ ל-commit ידוע-טוב (לפרויקט
# אין releases/tags רשמיים, ר' docs/satcom-feasibility.md §2, שם גם אומתה
# הבנייה בפועל). SDRplay מזוהה אוטומטית ע"י cmake דרך libsdrplay_api שהותקן
# בשלב 2 (find_package(SDRplay) מחפש sdrplay_api.h/libsdrplay_api תחת
# /usr/local); libacars-2 כבר בנוי בשלב 4b — אין צורך בבנייה נוספת שלו.
SATCOM_SNIFFER_COMMIT="2827b3a0c7cd349783aeee4621096db14f43264a"
SATCOM_BUILD_SIG="$(printf '%s' "$SATCOM_SNIFFER_COMMIT" | sha256sum | awk '{print $1}')"
AIRAM_SATCOM_MARK="/usr/local/share/airam/inmarsat-sniffer.build-sig"
if command -v inmarsat-sniffer >/dev/null 2>&1 && [[ "$(cat "$AIRAM_SATCOM_MARK" 2>/dev/null)" == "$SATCOM_BUILD_SIG" ]]; then
  log "inmarsat-sniffer כבר מותקן - מדלג."
else
  log "בונה inmarsat-sniffer (SATCOM)..."
  cd "$BUILD_DIR"
  # עץ קיים שאינו על ה-commit הנעוץ (העלאת גרסה) => משכפלים מחדש (כמו dumpvdl2)
  if [[ -d inmarsat-sniffer ]] && \
     [[ "$(git -C inmarsat-sniffer rev-parse HEAD 2>/dev/null)" != "$SATCOM_SNIFFER_COMMIT" ]]; then
    rm -rf inmarsat-sniffer
  fi
  if [[ ! -d inmarsat-sniffer ]]; then
    git clone https://github.com/alphafox02/inmarsat-sniffer.git
    git -C inmarsat-sniffer checkout "$SATCOM_SNIFFER_COMMIT"
  fi
  cd inmarsat-sniffer && rm -rf build && mkdir build && cd build
  # PKG_CONFIG_PATH => כדי ש-cmake ימצא את libacars-2 שהותקן ל-/usr/local (שלב 4b).
  # קונפיג ה-cmake נשמר ללוג זמני: תקציר "SDRplay: enabled/not found" הוא מקור-
  # האמת היחיד לתמיכת SDRplay בפועל (בניגוד ל---help, שמפרט sdrplay[-SERIAL]
  # תמיד בסטטי גם בבנייה בלי הדרייבר — אומת ישירות, לא הנחה).
  CMAKE_LOG="$(mktemp)"
  PKG_CONFIG_PATH="/usr/local/lib/pkgconfig:${PKG_CONFIG_PATH:-}" \
    cmake .. 2>&1 | tee "$CMAKE_LOG"
  make -j"$(nproc)" && make install && ldconfig
  command -v inmarsat-sniffer >/dev/null 2>&1 || die "בניית inmarsat-sniffer נכשלה (בדוק SDRplay API/libacars)."
  # ⚠ סימן-הבנייה נכתב *רק* כשתמיכת SDRplay אושרה בפועל. אחרת הרצה חוזרת של
  # install.sh (אחרי שהמשתמש מתקין את SDRplay API בשלב 2) הייתה רואה
  # "inmarsat-sniffer כבר מותקן" ומדלגת על בנייה מחדש — למרות שהבינארי הקיים
  # עדיין בלי SDRplay בפועל. בלי סימן, הרצה חוזרת תמיד תבנה מחדש עד שהתמיכה תאושר.
  if grep -q "SDRplay: enabled" "$CMAKE_LOG"; then
    mkdir -p "$(dirname "$AIRAM_SATCOM_MARK")"
    printf '%s' "$SATCOM_BUILD_SIG" > "$AIRAM_SATCOM_MARK"
  else
    warn "inmarsat-sniffer נבנה בלי תמיכת SDRplay - מצב SATCOM לא יעבוד. ודא שה-SDRplay API מותקן (שלב 2) והרץ שוב את install.sh (יבנה מחדש אוטומטית - לא נשמר סימן-בנייה בלי תמיכת SDRplay)."
  fi
  rm -f "$CMAKE_LOG"
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
# tripwire: sed על XML עיוור להערות/שינויי פורמט - מוודאים שכל ערך באמת נקלט
# כתג פעיל בתחילת שורה, אחרת מזהירים ברעש (burst שגוי => חזרת ה-latency של 30 שניות).
for kv in "source-password ${SOURCE_PW}" "burst-on-connect 0" "burst-size 0" \
          "queue-size 65536" "source-timeout 10"; do
  tag="${kv% *}"; val="${kv#* }"
  grep -Eq "^[[:space:]]*<$tag>$val</$tag>" "$ICE" \
    || warn "אימות Icecast נכשל: <$tag> אינו $val ב-$ICE - תקן ידנית (פורמט הקובץ השתנה?)"
done
# אפשר את השירות
[[ -f /etc/default/icecast2 ]] && sed -i 's/^ENABLE=.*/ENABLE=true/' /etc/default/icecast2 || true
grep -q "^ENABLE=" /etc/default/icecast2 2>/dev/null || echo "ENABLE=true" >> /etc/default/icecast2
systemctl enable icecast2
systemctl restart icecast2

# ----------------------------------------------------------------------------
# 6. קובץ הגדרות התחלתי + תיקיית state
# ----------------------------------------------------------------------------
log "מתקין קובץ הגדרות התחלתי..."
mkdir -p /etc/rtl_airband /var/lib/airam /var/lib/airam/recordings
[[ -f /etc/rtl_airband/airband.conf ]] || cp "$REPO_DIR/config/airband.conf" /etc/rtl_airband/airband.conf

# ----------------------------------------------------------------------------
# 6b. חיזוק אבטחה: משתמש לא-root לשרת הווב + sudoers ממוקד ל-restart
# ----------------------------------------------------------------------------
log "מגדיר משתמש 'airam' לשרת הווב (הרצה ללא root)..."
id -u airam >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin airam
# הבעלות מאפשרת ל-airam לכתוב את airband.conf, ה-state, הפריסטים וההקלטות
# (rtl_airband כ-root עדיין כותב לתיקיית ההקלטות; airam מוחק/קורא דרך בעלות התיקייה)
chown -R airam:airam /etc/rtl_airband /var/lib/airam
# חברות בקבוצות (אם קיימות): journal לקריאת journalctl, video ל-vcgencmd (api/power)
for grp in systemd-journal video; do
  getent group "$grp" >/dev/null 2>&1 && usermod -aG "$grp" airam || true
done
# sudoers: מתיר ל-airam *רק* את פקודות ה-systemctl המדויקות הדרושות (NOPASSWD), לא יותר.
# מצב משולב: restart/stop לכל אחד מארבעת צרכני ה-SDR => מעבר קול/ACARS/VDL2/SATCOM על SDR אחד.
# reset-failed ל-airam-satcom בלבד: היחידה היחידה עם StartLimitBurst סופי (בטיחות
# bias-T, ר' systemd/airam-satcom.service) — _enter_satcom צריך לאפס תקרה קודמת
# כדי שכניסה ידנית מחדש מה-UI תמיד תעבוד גם אחרי כמה קריסות רצופות.
cat > /etc/sudoers.d/airam <<'EOF'
airam ALL=(root) NOPASSWD: /usr/bin/systemctl restart rtl_airband
airam ALL=(root) NOPASSWD: /usr/bin/systemctl stop rtl_airband
airam ALL=(root) NOPASSWD: /usr/bin/systemctl restart airam-acars
airam ALL=(root) NOPASSWD: /usr/bin/systemctl stop airam-acars
airam ALL=(root) NOPASSWD: /usr/bin/systemctl restart airam-vdl2
airam ALL=(root) NOPASSWD: /usr/bin/systemctl stop airam-vdl2
airam ALL=(root) NOPASSWD: /usr/bin/systemctl restart airam-satcom
airam ALL=(root) NOPASSWD: /usr/bin/systemctl stop airam-satcom
airam ALL=(root) NOPASSWD: /usr/bin/systemctl reset-failed airam-satcom
EOF
chmod 440 /etc/sudoers.d/airam
visudo -cf /etc/sudoers.d/airam >/dev/null || die "קובץ sudoers לא תקין (/etc/sudoers.d/airam)."
# קובץ environment ל-PIN אופציונלי (כבוי כברירת מחדל => חוויית 'בלי סיסמאות' נשמרת)
mkdir -p /etc/airam
if [[ ! -f /etc/airam/airam.env ]]; then
  cat > /etc/airam/airam.env <<'EOF'
# AIR-AM web tuner - משתני סביבה.
# כדי לדרוש PIN לשינוי תדר/הגדרות, בטל את ההערה והגדר ערך (ואז: systemctl restart airam-web):
# AIRAM_PIN=1234
EOF
fi
# הגדרות ACARS/VDL2/SATCOM (ברירת מחדל; airam-web דורס בכל מעבר מצב). בבעלות airam => airam-web יכול לכתוב.
[[ -f /etc/airam/acars.env ]] || cp "$REPO_DIR/config/acars.env" /etc/airam/acars.env
[[ -f /etc/airam/vdl2.env ]] || cp "$REPO_DIR/config/vdl2.env" /etc/airam/vdl2.env
[[ -f /etc/airam/satcom.env ]] || cp "$REPO_DIR/config/satcom.env" /etc/airam/satcom.env
chown -R airam:airam /etc/airam

# ----------------------------------------------------------------------------
# 7. שרת בורר התדרים (web tuner)
# ----------------------------------------------------------------------------
log "מתקין את שרת הווב ל-/opt/airam ..."
mkdir -p /opt/airam/webtune
cp -r "$REPO_DIR/webtune/." /opt/airam/webtune/   # אידמפוטנטי (לא יוצר webtune/webtune)
[[ -f "$REPO_DIR/VERSION" ]] && cp "$REPO_DIR/VERSION" /opt/airam/webtune/VERSION   # הגרסה להצגה בממשק
# שער המוכנות ל-SDRplay (ExecStartPre של rtl_airband)
cp "$REPO_DIR/scripts/airam-wait-sdrplay" /usr/local/bin/
chmod 755 /usr/local/bin/airam-wait-sdrplay
# כלל udev: הפעלה מחדש של שירותי ה-SDR בעת חיבור ה-RSP1B (התאוששות מהירה)
cp "$REPO_DIR/udev/99-airam.rules" /etc/udev/rules.d/
udevadm control --reload-rules 2>/dev/null || true

# ----------------------------------------------------------------------------
# 7b. תמלול ATC (אופציונלי) - whisper.cpp + מודל base.en
#     הפעלה:  INSTALL_WHISPER=1 sudo ./install.sh   (בנייה ארוכה => לא ברירת מחדל)
# ----------------------------------------------------------------------------
if [[ "${INSTALL_WHISPER:-0}" == "1" ]]; then
  log "מתקין תמלול ATC (whisper.cpp + base.en) - עשוי לקחת כמה דקות ..."
  apt-get install -y ffmpeg
  WHISPER_SRC="$BUILD_DIR/whisper.cpp"
  [[ -d "$WHISPER_SRC" ]] || git clone --depth 1 https://github.com/ggml-org/whisper.cpp "$WHISPER_SRC"
  cmake -S "$WHISPER_SRC" -B "$WHISPER_SRC/build" -DCMAKE_BUILD_TYPE=Release
  cmake --build "$WHISPER_SRC/build" -j"$(nproc)" --target whisper-cli
  install -m755 "$WHISPER_SRC/build/bin/whisper-cli" /usr/local/bin/whisper-cli
  mkdir -p /opt/airam/models
  MODEL="/opt/airam/models/ggml-base.en.bin"
  [[ -f "$MODEL" ]] || curl -fL --retry 3 -o "$MODEL" \
    "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin"
  chown -R airam:airam /opt/airam/models
  # הפעלת התמלול בקובץ ה-environment (אידמפוטנטי)
  grep -q '^AIRAM_TRANSCRIBE=' /etc/airam/airam.env 2>/dev/null || \
    printf 'AIRAM_TRANSCRIBE=1\n' >> /etc/airam/airam.env
  log "תמלול ATC הופעל (לכבות: ערוך /etc/airam/airam.env והסר AIRAM_TRANSCRIBE)."
else
  log "תמלול ATC לא הותקן (להפעלה: INSTALL_WHISPER=1 sudo ./install.sh)."
fi

# ----------------------------------------------------------------------------
# 8. שירותי systemd
# ----------------------------------------------------------------------------
log "מתקין שירותי systemd ..."
cp "$REPO_DIR/systemd/sdrplay.service"      /etc/systemd/system/
cp "$REPO_DIR/systemd/rtl_airband.service"  /etc/systemd/system/
cp "$REPO_DIR/systemd/airam-web.service"    /etc/systemd/system/
cp "$REPO_DIR/systemd/airam-acars.service"  /etc/systemd/system/
cp "$REPO_DIR/systemd/airam-vdl2.service"   /etc/systemd/system/
cp "$REPO_DIR/systemd/airam-satcom.service" /etc/systemd/system/
systemctl daemon-reload
# אף צרכן SDR (rtl_airband / airam-acars / airam-vdl2 / airam-satcom) אינו
# enabled בכוונה: אין "מצב ראשי" — airam-web (המתזמר, enabled) קורא את
# state.json באתחול ומשחזר את המצב השמור האחרון, כולל off. Conflicts ב-units
# מבטיח שלא ירוצו יחד.
systemctl enable sdrplay.service airam-web.service
# שדרוג מגרסה ישנה (rtl_airband היה enabled ועלה תמיד באתחול) — אידמפוטנטי.
systemctl disable rtl_airband.service >/dev/null 2>&1 || true
# restart (ולא enable --now שהוא no-op לשירות שכבר רץ!) - אחרת בעדכון
# הבינארי/הקוד/ה-units החדשים לא נטענים והשירותים ממשיכים לרוץ עם הישנים.
# restart של sdrplay מרים דרך PartOf (try-restart) את צרכן ה-SDR *הפעיל כרגע*
# ויהיה אשר יהיה — בלי להעיף משתמשי ACARS/VDL2 לקול כמו ה-restart הגורף הישן.
systemctl restart sdrplay.service || warn "sdrplay.service לא עלה - בדוק חיבור ה-RSP1B."
sleep 2
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
