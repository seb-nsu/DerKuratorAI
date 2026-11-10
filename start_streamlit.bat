@echo off
rem Starts DerKurator AI with the Streamlit front end. Double-click or run from the repository root.
cd /d "%~dp0"
if exist .venv\Scripts\activate.bat call .venv\Scripts\activate.bat
if exist venv\Scripts\activate.bat call venv\Scripts\activate.bat
python -m streamlit run app\main.py
pause
