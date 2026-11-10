@echo off
rem Starts DerKurator AI with the React front end. Double-click or run from the repository root.
cd /d "%~dp0"
if exist .venv\Scripts\activate.bat call .venv\Scripts\activate.bat
if exist venv\Scripts\activate.bat call venv\Scripts\activate.bat
rem Rebuild on every start so the page matches the current code (a few seconds); install packages only once
where npm >nul 2>nul || (
  if exist frontends\react\dist\index.html goto serve
  echo Node.js 22 is needed once to build the React front end: https://nodejs.org
  pause
  exit /b 1
)
pushd frontends\react
if not exist node_modules call npm ci || (popd & pause & exit /b 1)
call npm run build || (popd & pause & exit /b 1)
popd
:serve
set FRONTEND=react
echo Starting - the browser opens http://127.0.0.1:8080 when ready. Stop with Ctrl+C
rem Opens the browser once the server answers (the first start takes a few seconds)
start "" /min powershell -NoProfile -Command "for($i=0;$i -lt 180;$i++){try{Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 http://127.0.0.1:8080/api/v1/health | Out-Null; Start-Process http://127.0.0.1:8080; break}catch{Start-Sleep 1}}"
python scripts\serve.py
pause
