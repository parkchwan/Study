@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 앱 폴더를 다른 곳으로 옮겨도 동작하도록: 경로가 박힌 activate.bat, pip.exe 대신
rem 가상환경의 python.exe를 직접 실행한다.
set "PY=%~dp0.venv\Scripts\python.exe"

if exist "%PY%" (
  "%PY%" -c "import sys" >nul 2>&1
  if errorlevel 1 goto rebuild
)
if not exist "%PY%" goto create
goto install

:rebuild
echo 가상환경이 이 폴더와 맞지 않아 다시 만듭니다. 몇 분 걸립니다...
rmdir /s /q "%~dp0.venv"

:create
echo [1/3] 가상환경 만드는 중...
python -m venv "%~dp0.venv"
if errorlevel 1 goto nopython

:install
echo [2/3] 필요한 패키지 확인 중...
"%PY%" -m pip install -q --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
  if defined RETRIED goto pipfail
  set RETRIED=1
  goto rebuild
)

"%PY%" migrate.py
if not exist .env (
  copy .env.example .env >nul
  echo.
  echo  .env 파일을 만들었습니다. 메모장에서 GEMINI_API_KEY 를 입력하고 저장한 뒤 창을 닫으세요.
  notepad .env
)
"%PY%" run.py
pause
exit /b 0

:nopython
echo Python이 설치되어 있지 않습니다. https://www.python.org/downloads/ 에서 설치하세요.
echo 설치할 때 "Add python.exe to PATH" 를 꼭 체크하세요.
pause
exit /b 1

:pipfail
echo 패키지 설치에 실패했습니다. 인터넷 연결을 확인한 뒤 다시 실행하세요.
pause
exit /b 1
