@echo off
rem Starts DerKurator AI with the NiceGUI front end. Double-click or run from the repository root.
cd /d "%~dp0"
if exist .venv\Scripts\activate.bat call .venv\Scripts\activate.bat
if exist venv\Scripts\activate.bat call venv\Scripts\activate.bat
set FRONTEND=nicegui
echo Starting - the browser opens http://127.0.0.1:8080 when ready. Stop with Ctrl+C
rem Opens the browser once the server answers (the first start takes a few seconds)
start "" /min powershell -NoProfile -Command "for($i=0;$i -lt 180;$i++){try{Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 http://127.0.0.1:8080/api/v1/health | Out-Null; Start-Process http://127.0.0.1:8080; break}catch{Start-Sleep 1}}"
python scripts\serve.py
pause
