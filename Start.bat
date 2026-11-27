@echo off
rem Starts DerKurator AI with the demo portfolio. Double-click is enough: the first start creates the Python
rem environment and installs the packages; later starts take seconds.
rem   Start.bat          NiceGUI front end (no Node.js needed)
rem   Start.bat react    React front end (needs Node.js 22 once to build)
setlocal EnableDelayedExpansion
cd /d "%~dp0"
set "FRONT=nicegui"
if /i "%~1"=="react" set "FRONT=react"

rem --- Python environment (once, again when requirements.txt changes) ---
set "PY="
where py >nul 2>nul && (py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>nul && set "PY=py -3")
if not defined PY where python >nul 2>nul && (python -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>nul && set "PY=python")
if not exist .venv\Scripts\python.exe (
  if not defined PY (
    echo Python 3.12 or newer is needed: https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^)
    pause
    exit /b 1
  )
  echo First start: setting up DerKurator AI ^(takes a few minutes^) ...
  !PY! -m venv .venv || (pause & exit /b 1)
)
set "STAMP=.venv\derkurator-installed.txt"
set "NEED=0"
if not exist "%STAMP%" set "NEED=1"
if exist "%STAMP%" for /f %%i in ('powershell -NoProfile -Command "(Get-Item requirements.txt).LastWriteTime -gt (Get-Item '%STAMP%').LastWriteTime"') do if /i "%%i"=="True" set "NEED=1"
if "!NEED!"=="1" (
  .venv\Scripts\python -m pip install --quiet --upgrade pip
  .venv\Scripts\python -m pip install --quiet -r requirements.txt || (echo Installing the packages failed - see above. & pause & exit /b 1)
  >"%STAMP%" echo installed
)
call .venv\Scripts\activate.bat

if /i not "%FRONT%"=="react" goto serve
rem --- React: rebuilt on every start so the page matches the code; packages only once ---
where npm >nul 2>nul || (
  if exist frontends\react\dist\index.html goto serve
  echo Node.js 22 is needed once to build the React front end: https://nodejs.org  ^(or start without "react"^)
  pause
  exit /b 1
)
pushd frontends\react
if not exist node_modules call npm ci || (popd & pause & exit /b 1)
call npm run build || (popd & pause & exit /b 1)
popd

:serve
set "FRONTEND=%FRONT%"
echo Starting (%FRONT%) - the browser opens http://127.0.0.1:8080 when ready. Stop with Ctrl+C or close this window.
start "" /min powershell -NoProfile -Command "for($i=0;$i -lt 180;$i++){try{Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 http://127.0.0.1:8080/api/v1/health | Out-Null; Start-Process http://127.0.0.1:8080; break}catch{Start-Sleep 1}}"
python scripts\serve.py
pause
