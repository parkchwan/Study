@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv (
  echo [1/3] 처음 실행: 가상환경 만드는 중...
  python -m venv .venv || (echo Python이 설치되어 있지 않습니다. https://www.python.org/downloads/ & pause & exit /b 1)
)
call .venv\Scripts\activate.bat
echo [2/3] 필요한 패키지 확인 중...
pip install -q -r requirements.txt
python migrate.py
if not exist .env (
  copy .env.example .env >nul
  echo.
  echo  .env 파일을 만들었습니다. 메모장에서 GEMINI_API_KEY 를 입력하고 저장한 뒤 창을 닫으세요.
  notepad .env
)
python run.py
pause
