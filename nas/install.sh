#!/bin/sh
# NAS 설치:  sudo sh /volume1/web/drive-index-nas-setup/install.sh
# - /volume1/drive-index-nas 에 설치 (web 폴더 밖 → 키·드라이브 인증이 웹으로 노출되지 않음)
# - rclone(ARM용) 내려받기, env.sh 만들기, 검색 API 켜기
set -e
SRC="$(cd "$(dirname "$0")" && pwd)"
DST="/volume1/drive-index-nas"
[ "$(id -u)" = 0 ] || { echo "sudo 로 실행하세요: sudo sh $0"; exit 1; }

PY=""
for c in python3 /usr/bin/python3 /usr/local/bin/python3 /var/packages/Python3.9/target/usr/bin/python3 \
         /var/packages/python311/target/bin/python3 /var/packages/python310/target/bin/python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 6) else 1)' 2>/dev/null; then
    PY="$(command -v "$c")"; break
  fi
done
[ -n "$PY" ] || { echo "python3 가 없습니다. 패키지 센터에서 'Python 3.9'(또는 SynoCommunity Python 3.11)를 설치한 뒤 다시 실행하세요."; exit 1; }
echo "python: $PY ($("$PY" -V 2>&1)) / CPU: $(uname -m)"

mkdir -p "$DST/work"
for f in dindex.py api_server.py skeleton.py api.sh nightly.sh setkey.sh env.sh.example README.md; do
  [ -f "$SRC/$f" ] && cp "$SRC/$f" "$DST/$f"
done
echo "$PY" > "$DST/.python"
if [ ! -f "$DST/env.sh" ]; then cp "$DST/env.sh.example" "$DST/env.sh"; fi
chmod 700 "$DST"; chmod 600 "$DST/env.sh"; chmod 755 "$DST"/*.sh

if [ ! -x "$DST/rclone" ]; then
  case "$(uname -m)" in
    armv7*|armv8l) A=arm-v7 ;; aarch64|arm64) A=arm64 ;; x86_64) A=amd64 ;; i686|i386) A=386 ;; *) A=arm ;;
  esac
  echo "rclone ($A) 내려받는 중..."
  URL="https://downloads.rclone.org/rclone-current-linux-$A.zip"
  curl -fsSL -o "$DST/rclone.zip" "$URL" 2>/dev/null || wget -q -O "$DST/rclone.zip" "$URL" || true
  "$PY" - "$DST" <<'PYEOF' || echo "rclone 내려받기 실패 → PC에서 rclone-current-linux-$A.zip 을 받아 압축 속 rclone 을 $DST 에 넣고 chmod 755"
import os, sys, zipfile
dst = sys.argv[1]
z = zipfile.ZipFile(os.path.join(dst, "rclone.zip"))
name = [n for n in z.namelist() if n.endswith("/rclone")][0]
open(os.path.join(dst, "rclone"), "wb").write(z.read(name))
os.chmod(os.path.join(dst, "rclone"), 0o755)
PYEOF
  rm -f "$DST/rclone.zip"
fi
[ -x "$DST/rclone" ] && "$DST/rclone" version | head -1

sh "$DST/api.sh" restart || true
echo
echo "설치 끝. 다음 할 일:"
echo " 1) 키 넣기:        sudo sh $DST/setkey.sh"
echo " 2) 드라이브 연결:  sudo $DST/rclone config   (README.md 의 '드라이브 연결' 참고)"
echo " 3) 작업 스케줄러:  매일 01:00  sh $DST/nightly.sh  /  부팅 시  sh $DST/api.sh start"
echo " 4) 설치용 폴더 삭제: rm -rf $SRC"
