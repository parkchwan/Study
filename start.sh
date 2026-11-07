#!/usr/bin/env bash
# Mac/Linux 실행 스크립트: 더블클릭 또는 ./start.sh
set -e
cd "$(dirname "$0")"
[ -d .venv ] || { echo "[1/3] 가상환경 만드는 중..."; python3 -m venv .venv; }
. .venv/bin/activate
echo "[2/3] 필요한 패키지 확인 중..."
pip install -q -r requirements.txt
if [ ! -f .env ]; then
  cp .env.example .env
  echo ".env 파일을 만들었습니다. GEMINI_API_KEY 를 입력하고 다시 실행하세요: $(pwd)/.env"
  (command -v open >/dev/null && open -e .env) || true
  exit 0
fi
echo "[3/3] 서버 시작: http://localhost:8000"
( sleep 2; (command -v open >/dev/null && open http://localhost:8000) || (command -v xdg-open >/dev/null && xdg-open http://localhost:8000) ) >/dev/null 2>&1 &
exec python -m uvicorn app.main:app --port 8000
