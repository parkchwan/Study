#!/usr/bin/env bash
# Mac/Linux 실행 스크립트: 더블클릭 또는 ./start.sh
# 앱 폴더를 옮겨도 동작하도록 가상환경의 python을 직접 실행 (경로가 박힌 activate·pip 스크립트를 쓰지 않음)
set -e
cd "$(dirname "$0")"
PY="$(pwd)/.venv/bin/python"
if [ -x "$PY" ] && ! "$PY" -c "import sys" >/dev/null 2>&1; then
  echo "가상환경이 이 폴더와 맞지 않아 다시 만듭니다..."; rm -rf .venv
fi
[ -x "$PY" ] || { echo "[1/3] 가상환경 만드는 중..."; python3 -m venv .venv; }
echo "[2/3] 필요한 패키지 확인 중..."
if ! "$PY" -m pip install -q --disable-pip-version-check -r requirements.txt; then
  echo "패키지 설치 실패 → 가상환경을 다시 만듭니다..."; rm -rf .venv; python3 -m venv .venv
  "$PY" -m pip install -q --disable-pip-version-check -r requirements.txt
fi
"$PY" migrate.py
if [ ! -f .env ]; then
  cp .env.example .env
  echo ".env 파일을 만들었습니다. GEMINI_API_KEY 를 입력하고 다시 실행하세요: $(pwd)/.env"
  (command -v open >/dev/null && open -e .env) || true
  exit 0
fi
exec "$PY" run.py
