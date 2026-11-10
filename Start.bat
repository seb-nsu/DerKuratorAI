@echo off
echo ========================================================
echo Starting DerKurator AI (Enterprise SteerCo Copilot)
echo ========================================================
echo.

REM Change to the project folder
cd /d "%~dp0"

REM Check that the virtual environment exists
IF NOT EXIST ".venv\Scripts\activate.bat" (
    echo Error: virtual environment not found - create it with "python -m venv .venv" first.
    pause
    exit /b
)

REM Activate the environment
call .venv\Scripts\activate.bat

REM Start Streamlit
echo Loading the app...
python -m streamlit run app/main.py

pause
